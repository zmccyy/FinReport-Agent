# M2.04 ModelHub 4-bit 7B 加载器实现决策记录

> **日期**：2026-07-20
> **任务**：M2.04 L3 M11 ModelHub 加载 4-bit 7B
> **状态**：已完成

---

## 背景

实现 L3 M11 ModelHub 入口（spec §2.3 M11、§3.5 场景路由、§8.1 6GB VRAM 约束、§10.3 降级链）：加载 Qwen2.5-7B-Instruct GPTQ-Int4 模型并提供 `generate()` 接口；后续 M2.06+ 的 Extractor 与 M4+ 的 ReAct 问答都通过 `ModelHub.generate()` 调用本地 7B 推理。本批任务依赖 M2.01–M2.03 已建立的 L3 FastAPI 骨架与 AiException → 500 处理器。

---

## 决策列表

### D1: 单模型驻存 + key-based no-op（spec §8.1 显存预算）

**初始问题**：6GB VRAM 只能装 1 个 7B(4bit ~5GB) 或 1 个 1.5B + 1 个 small；如果 EXTRACT 链路用 1.5B、REASON 链路用 7B 互相来回切换会导致显存抖动（spec Q5）。

**决策**：`LlmLoader` 用 `_loaded_key` 跟踪当前驻存的逻辑模型名（`"7b"` / `"1.5b"`），重复 `load(same_key)` 直接 no-op（`is_loaded()` 检查）；切到不同 key 时先 `backend.unload()` 释放显存再 `backend.load()`。M2.05 的 `model_lock` 在此基础上加 Redis 分布式锁防多 worker 抖动。

### D2: GPTQ-Int4 走 transformers 自动检测，NF4 显式构造 BitsAndBytesConfig

**初始问题**：7B 用 `Qwen2.5-7B-Instruct-GPTQ-Int4`（已预量化），1.5B 用 `Qwen2.5-1.5B-Instruct` + QLoRA NF4。两种量化路径的 `quantization_config` 入参形式不同。

**决策**：`TransformersBackend._build_quant_config(quant, transformers)`：
- `gptq-int4`：返回 `None`。Qwen2.5-7B-Instruct-GPTQ-Int4 的 `config.json` 已含 GPTQ 量化配置，transformers `from_pretrained` 会自动应用，传 `None` 即可。
- `nf4`：返回 `BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16, bnb_4bit_use_double_quant=True)`，让 transformers 在 load 时现量化（spec §4.5 训练/推理量化策略）。
- 其他 quant 标签：抛 `ModelLoadException("Unsupported quantization: ...")`。

### D3: 懒加载 torch/transformers（AGENTS.md §8.2 业务代码禁止直接 import transformers）

**初始问题**：业务代码不能 `import transformers`；同时单测环境无法拉真实 7B 模型（显存+权重下载）。

**决策**：
- `TransformersBackend` 模块顶层不 import torch/transformers；`load()` 内部用 `_lazy_import_torch()` / `_lazy_import_transformers()` 动态导入，导入失败映射为 `ModelLoadException("torch is required..." / "transformers is required...")`。
- `LlmBackend` 是 `typing.Protocol`，单测注入 `_FakeBackend` 实现即可隔离真实推理。
- `TransformersBackend` 单测通过 `monkeypatch.setitem(sys.modules, "torch", torch_stub)` + `sys.modules["transformers"] = stub` 注入 `_StubTransformers` / `_StubModel` / `_StubTokenizer` / `_StubTensor`，无需 GPU 也能验证 load/generate/unload/timeout/OOM 全路径。

### D4: 推理超时用 daemon 线程，不用 signal.SIGALRM

**初始问题**：spec §10.3 降级链要求"本地 7B 超时 (>60s) 切 API 72B"；但 `signal.SIGALRM` 在 Windows 不支持。

**决策**：`TransformersBackend.generate()` 启 daemon 线程跑 `model.generate(**inputs, **kwargs)`，主线程 `thread.join(timeout=timeout_seconds)`：
- 线程仍存活 → 超时 → `self._reset()` 释放模型（防显存泄漏） → 抛 `InferenceTimeoutException(f"LLM generate exceeded {timeout}s")`，调用方捕获后切 API 72B。
- 线程结束但有异常 → 检查 `_is_oom(error)`，OOM 触发 `_reset()` + `ModelLoadException`；其他异常 → `AiException`。
- 线程正常结束 → 取 `outputs[0][prompt_tokens:]` 解码为 text，记录 `prompt_tokens` / `completion_tokens` / `latency_ms` / `first_token_ms`。

### D5: OOM 启发式检测 + 自动 reset

**初始问题**：torch/transformers/bitsandbytes 不同版本抛 OOM 的异常类型与消息不一致（`RuntimeError("CUDA out of memory...")` / `torch.cuda.OutOfMemoryError` / bitsandbytes 自定义异常），无法精确捕获。

**决策**：`_is_oom(error: BaseException) -> bool` 通过 `str(error).lower()` 匹配 `"out of memory"` / `"cuda oom"`；load 与 generate 两处都调用，OOM 触发 `_reset()` + `ModelLoadException`，让上层按 spec §10.3 "本地 7B OOM → 释放模型 → 重启 ModelHub → 重试 1 次 → 转 API" 降级。

### D6: Scene 路由表 + LLM_SCENES frozenset

**初始问题**：spec §3.5 规定 4 个 scene（EXTRACT/REASON/EMBED/LAYOUT），但 M2.04 只需实现 7B REASON 路径；EMBED/LAYOUT 走各自的引擎（bge-small-zh LoRA / LayoutLMv3），不能通过 `LlmLoader` 加载。

**决策**：
- `SCENE_MODEL_MAP: dict[Scene, tuple[str, str]]` 集中维护 scene → (model_key, quant) 映射；`ModelHub.route(scene)` 直接查表返回。
- `LLM_SCENES = frozenset({Scene.EXTRACT, Scene.REASON})` 标记走 `LlmLoader` 的场景。
- `ModelHub.load_for_scene(scene)`：非 LLM_SCENES 抛 `AiException(f"Scene {scene.value} does not route through LlmLoader in M2.04")`；LLM_SCENES 走 `route → load_llm`。
- `ModelHub.embed(texts)`：M2.04 抛 `AiException("ModelHub.embed() is not implemented in M2.04; bge-small-zh LoRA embedder lands in M5")`。

### D7: Settings 配置驱动 + 模块级单例

**初始问题**：M2.04 多个组件（LlmLoader 默认 generate kwargs、ModelHub 模型路径）需要配置；FastAPI 端点需要进程内单例。

**决策**：
- `Settings` 新增字段（`model_7b_path` / `model_15b_path` / `model_embed_path` / `model_load_timeout_seconds=300` / `model_generate_timeout_seconds=60` / `model_max_new_tokens=1024` / `model_quant_7b="gptq-int4"` / `model_quant_15b="nf4"`），全部走 pydantic-settings 环境变量驱动。
- `ModelHub` 模块级 `_DEFAULT_HUB` + `get_modelhub()` 懒加载单例；`reset_modelhub(hub)` 测试辅助。
- `app/api/models.py` 用 FastAPI `Depends(get_modelhub_dep)` 注入；测试用 `app.dependency_overrides[get_modelhub_dep] = lambda: hub` 替换。

### D8: 路径解析走 Path.expanduser()，OS-native 分隔符

**初始问题**：`ModelHub._resolve_model_path(name)` 用 `str(Path(path).expanduser())`，Windows 上 `models/Qwen2.5-7B-Instruct-GPTQ-Int4` 被规范化为 `models\Qwen2.5-7B-Instruct-GPTQ-Int4`，与测试期望的正斜杠字符串不匹配。

**决策**：保持代码用 `Path.expanduser()`（让 OS 接收 native 路径）；测试断言改用 `str(Path("models/Qwen2.5-7B-Instruct-GPTQ-Int4"))` 跟随平台规范，避免硬编码斜杠方向。

### D9: 修复 M2.02 遗留的 table_recognizer.py black 违规

**初始问题**：M2.04 跑 `black --check app/ tests/` 时发现 `app/modules/parser/table_recognizer.py` 第 166 行长度超过 88 字符（`boxes: Any = TableRecognizer._field(layout_res, "boxes", default=None) if layout_res else None`），是 M2.01-M2.03 commit `9d04e61` 遗留的格式违规，本批任务为通过质量门一并修复。

**决策**：把该行拆为多行三元表达式。修复与 M2.04 主线无关，但是质量门 DoD 必需。

---

## 已完成 checklist

- [x] `app/core/exceptions.py` — 新增 `ModelLoadException` / `InferenceTimeoutException`
- [x] `app/core/config.py` — ModelHub Settings 字段
- [x] `app/modules/modelhub/llm_loader.py` — `LlmBackend` Protocol + `TransformersBackend` + `LlmLoader` + 量化配置 + OOM 检测 + 超时守护
- [x] `app/modules/modelhub/modelhub.py` — `Scene` / `SCENE_MODEL_MAP` / `ModelHub` / `get_modelhub()` / `reset_modelhub()`
- [x] `app/modules/modelhub/__init__.py` — package 导出
- [x] `app/schemas/models.py` — LoadModel / Generate / Status / Unload Pydantic 模型
- [x] `app/api/models.py` — 4 个 `/internal/models/*` 端点 + 依赖注入
- [x] `app/main.py` — 注册 `models_router`
- [x] `pyproject.toml` — 追加 `torch / transformers / accelerate / bitsandbytes` 版本约束
- [x] `tests/test_m2_modelhub.py` — 41 个测试（LlmLoader 9 + ModelHub 12 + TransformersBackend 8 + `_is_oom` 2 + HTTP 端点 6 + 路径/异常 4）
- [x] `app/modules/parser/table_recognizer.py` — 修复 M2.02 遗留的 black 违规（D9）
- [x] ai-service 测试总数 141 全绿（新增 41，原 100 全保留）
- [x] 覆盖率：整体 94%，M2.04 全部模块 ≥ 92%（M2 阈值 ≥80%）
- [x] `ruff check` / `black --check` 全绿
- [x] `docs/progress/m2.md` 打勾 M2.04 并追加交付说明

## 发现的风险

1. **7B 真实加载未端到端验证**：本次只跑通 stub 路径，真实 GPTQ-Int4 权重下载 + `from_pretrained` 加载 + `model.generate()` 推理未在 RTX 4050 Mobile 6GB 上验证。M2.12 真实年报链路首次跑通时需确认：(a) 6GB VRAM 是否真能装下 7B(4bit ~5GB) + 推理 activation；(b) 首 token < 5s SLA 是否达成；(c) `device_map="auto"` 在单卡上是否正常。若 OOM，需启用 `max_memory={0: "5GiB"}` 限制或换 1.5B 兜底。
2. **bitsandbytes Windows 兼容性**：M2.01-M2.03 决策记录 D4 提到 numpy 必须钉 1.26.x。本次新增 `bitsandbytes>=0.43` 依赖，已知 bitsandbytes 0.43.x 在 Windows + torch 2.5.1+cu121 下需要 `LLM_CUDA_HOME` 环境变量或预编译 wheel；若 pip install 失败，参考 M1 的 Windows bitsandbytes 路径。
3. **daemon 线程超时后模型状态不一致**：超时路径调用 `self._reset()` 清空 `_model/_tokenizer/_model_path/_device`，但 daemon 线程仍在跑 `model.generate()`，可能继续占用显存直至结束。spec §10.3 的"释放模型 → 重启 ModelHub"流程需要 M2.05 的 `model_lock` + 进程级重启才能真正回收显存；当前 M2.04 只做了状态清理，daemon 线程的实际释放依赖 Python GC。
4. **`_is_oom` 误报风险**：基于字符串匹配的启发式可能漏报 `torch.cuda.OutOfMemoryError`（torch 2.x 新异常类型，message 不一定含 "out of memory"）或误报业务异常中恰好含 "oom" 字样的情况。M2.12 真实跑通后建议补充 `isinstance(error, torch.cuda.OutOfMemoryError)` 类型检查。
5. **`embed()` / EMBED / LAYOUT 都是 stub**：M2.04 只实现了 LLM 路径。M5（bge-small-zh LoRA）和 M4（LayoutLMv3）需各自实现 `EmbedBackend` / `LayoutBackend` Protocol 与各自的 `load` / `infer` 方法，并在 `ModelHub` 增加路由分支。

## 下一步行动项

- M2.05：显存调度 + `model_lock`（Redis 分布式锁 `fin:lock:model:{modelName}`，prefetch_count=1，spec §8.1）
- M2.06：Extractor（7B 通用 prompt 三表抽取，路由到 `ModelHub.generate(scene=EXTRACT)`）— 注意 M2.06 spec 描述用 1.5B QLoRA，但 SCENE_MODEL_MAP 已映射 EXTRACT→1.5b nf4，需确认 T1 训练后的 1.5B adapter 是否合并到基座
- M2.07：抽取结果 JSON Schema 校验（validator 重试 temp=0.1 → 改用 7B 兜底，spec §10.3）
- M2.12：真实年报端到端验证 7B 加载与推理 SLA（首 token < 5s、推理不 OOM）
