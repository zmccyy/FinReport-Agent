# M2 阶段全面审查（M1.x + M2.01–M2.06）

> 审查日期：2026-07-20  
> 审查范围：截止到 M2.06 及之前的所有 L3 ai-service 代码 + M1 测试 / 部署 / CI 资产  
> 审查人：AI 助手（Codex）  
> 触发：用户请求「对前面已完成的工作进行全面审查，自行完成测试，审查后修改 bug 并交付审查结果」  
> 关联文档：`docs/progress/m2.md` / `docs/superpowers/specs/2026-07-13-finreport-agent-design.md` / `docs/superpowers/plans/2026-07-13-finreport-agent-implementation-plan.md`

---

## 1. 背景

M2.06 交付后，ai-service 测试基线为 226 个测试全绿、覆盖率 94%。本次审查的目的是：

1. 静态审查 M1.x + M2.01–M2.06 全部代码（31 源文件 + 14 测试文件）
2. 在干净环境（anaconda Python 3.13 + 必要依赖）下重跑全套测试，验证基线
3. 识别并修复 bug
4. 交付审查报告

## 2. 审查范围

### 源文件（31 个）

| 模块 | 文件 |
|---|---|
| schemas | `document.py` / `parse.py` / `task.py` / `models.py` / `statement.py` |
| core | `config.py` / `exceptions.py` / `redis_client.py` / `minio_client.py` |
| utils | `logger.py` |
| parser | `document_parser.py` / `table_recognizer.py` / `ocr_fallback.py` / `parser_factory.py` / `handler.py` |
| modelhub | `llm_loader.py` / `modelhub.py` / `vram_scheduler.py` / `__init__.py` |
| extractor | `prompts.py` / `extractor.py` / `handler.py` |
| reasoner | `handler.py`（M1 mock） |
| mq | `consumer.py` / `producer.py` |
| api | `health.py` / `parse.py` / `models.py` |
| main | `main.py` |

### 测试文件（14 个）

`test_m1_l3_service.py` / `test_m1_mq_reconnect.py` / `test_m1_init_scripts.py` / `test_m1_development_overlay.py` / `test_ci_workflows.py` / `test_e2e_m116_helpers.py` / `test_m2_document_parser.py` / `test_m2_table_recognizer.py` / `test_m2_ocr_fallback.py` / `test_m2_parse_handler.py` / `test_m2_parse_endpoint.py` / `test_m2_parser_factory.py` / `test_m2_modelhub.py` / `test_m2_vram_scheduler.py` / `test_m2_extractor.py` + `conftest.py`

## 3. 审查方法

1. **静态审查**：逐文件 `Read` 全部 31 源文件 + 14 测试文件，评估代码规范、类型完整性、mock 合理性、边界覆盖
2. **基线测试**：在 anaconda Python 3.13 + python-multipart/httpx 环境下运行 `pytest tests/ --cov=app --cov-report=term-missing`
3. **质量门**：`ruff check app/ tests/` + `black --check app/ tests/`（black 用 `format_str` 编程式调用以规避 Windows 中文路径 hang）
4. **Bug 修复**：对发现的 bug 最小化修复，重跑测试验证

## 4. 测试基线（修复前）

```
平台：win32 / Python 3.13.9 / pytest-8.4.2
收集：175 items / 3 errors（python-multipart 缺失，安装后解决）
结果：2 failed, 224 passed
覆盖率：94%
ruff：All checks passed
black：All checks passed（55 files）
```

## 5. 发现的 Bug

### Bug #1：`_build_quant_config` 在 NF4 路径硬编码 `import torch`

**位置**：[ai-service/app/modules/modelhub/llm_loader.py:311-340](file:///e:/项目/FinReport%20Agent/ai-service/app/modules/modelhub/llm_loader.py)

**症状**：`test_build_quant_config_nf4_returns_bitsandbytes_config` 在无 torch 环境下失败：

```
app.core.exceptions.ModelLoadException: torch is required for nf4 quantization
```

**根因**：`TransformersBackend._build_quant_config(quant, transformers)` 静态方法在 NF4 分支内部 `import torch` 并用 `torch.bfloat16` 作为 `bnb_4bit_compute_dtype`。测试只用 `_StubTransformers`（提供 `BitsAndBytesConfig` stub）但没 stub torch，导致 `import torch` 在 CI / 无 GPU 环境失败。

**修复**：
1. 给 `_build_quant_config` 加 `torch_module: Any = None` 可选参数
2. `load()` 调用时显式传 `torch_module=torch`（已在 `_lazy_import_torch()` 拿到）
3. 测试 `test_build_quant_config_nf4_returns_bitsandbytes_config` 注入 `types.ModuleType("torch")` stub
4. 新增 `test_build_quant_config_nf4_raises_when_torch_missing` 覆盖 torch 真缺失时的错误路径

**收益**：
- 函数纯度提升（依赖注入而非隐式 import）
- 单元测试可在任意 Python 环境运行（不要求 torch 安装）
- 生产路径行为不变（`load()` 仍走 `_lazy_import_torch()`）

### Bug #2：`test_transformers_backend_load_raises_on_missing_transformers` 未 stub torch

**位置**：[ai-service/tests/test_m2_modelhub.py:433-441](file:///e:/项目/FinReport%20Agent/ai-service/tests/test_m2_modelhub.py)

**症状**：测试只 `_block_import(monkeypatch, "transformers")`，但 `TransformersBackend.load()` 的实现是：

```python
def load(self, model_path, quant):
    torch = _lazy_import_torch()          # ← 先调用
    transformers = _lazy_import_transformers()
    ...
```

在无 torch 环境下，`_lazy_import_torch()` 先抛 `"torch is required for LLM inference"`，测试 expect `"transformers is required"` 不匹配：

```
AssertionError: Regex pattern did not match.
 Regex: 'transformers is required'
 Input: 'torch is required for LLM inference; install per spec §1'
```

**根因**：测试作者假设 torch 在 CI 环境一定可用，只 block transformers。但项目 spec §1 把 torch 列为可选 prod 依赖（仅 M2.04+ 启用），CI 跑 ruff/black/pytest 时不强制安装 torch。

**修复**：
1. 抽取 `_install_torch_stub(monkeypatch)` 辅助函数（从 `_install_transformers_stub` 提取公共部分）
2. 在 `test_transformers_backend_load_raises_on_missing_transformers` 中先 `_install_torch_stub(monkeypatch)` 让 `_lazy_import_torch()` 成功，再 `_block_import(monkeypatch, "transformers")` 验证 transformers 错误路径
3. `_install_transformers_stub` 改为内部调 `_install_torch_stub` 保持 DRY

**收益**：
- 测试在无 torch 环境下也稳定（符合 spec §1 依赖策略）
- `_install_torch_stub` 可被其他需要"先让 torch 可用再验证其他错误路径"的测试复用

## 6. 修复验证

### 测试结果（修复后）

```
平台：win32 / Python 3.13.9 / pytest-8.4.2
结果：227 passed（原 224 + 2 修复 + 1 新增 = 227）
覆盖率：94%（与修复前持平）
ruff：All checks passed
black：All checks passed（55 files）
```

### 覆盖率明细（关键模块）

| 模块 | 覆盖率 | 备注 |
|---|---|---|
| `app/schemas/statement.py` | 100% | M2.06 |
| `app/schemas/models.py` | 100% | M2.04 |
| `app/schemas/parse.py` | 100% | M1 |
| `app/schemas/task.py` | 100% | M1 |
| `app/schemas/document.py` | 97% | M2.01 |
| `app/modules/modelhub/modelhub.py` | 100% | M2.04 |
| `app/modules/modelhub/vram_scheduler.py` | 97% | M2.05 |
| `app/modules/modelhub/llm_loader.py` | 93% | M2.04（修复后 +1 stmts） |
| `app/modules/extractor/extractor.py` | 95% | M2.06 |
| `app/modules/extractor/prompts.py` | 100% | M2.06 |
| `app/modules/parser/document_parser.py` | 91% | M2.01 |
| `app/modules/parser/table_recognizer.py` | 92% | M2.02 |
| `app/modules/parser/ocr_fallback.py` | 92% | M2.03 |
| `app/core/redis_client.py` | 84% | M2.05 |
| `app/core/minio_client.py` | 91% | M1 |
| `app/main.py` | 100% | M1 |
| `app/api/parse.py` | 100% | M2.01 |
| `app/api/models.py` | 96% | M2.04 |
| `app/api/health.py` | 100% | M1 |
| `app/mq/consumer.py` | 85% | M1 |
| `app/mq/producer.py` | 96% | M1 |
| **TOTAL** | **94%** | 1424 stmts / 88 miss |

## 7. 静态审查观察（非 bug，记录备案）

### 7.1 `ModelHub.is_loaded_status` 测试 monkey-patch

[tests/test_m2_modelhub.py:385-391](file:///e:/项目/FinReport%20Agent/ai-service/tests/test_m2_modelhub.py) 在测试文件里给 `ModelHub` 类动态绑定了 `is_loaded_status` 方法（测试 helper），production `ModelHub` 没有此方法。

**评估**：测试 helper 在测试文件内 monkey-patch 类方法是常见做法（不污染 production 代码），可接受。但建议未来用 `hub.llm_loader.is_loaded()` 直接断言，避免类属性注入。**不阻断本次审查**。

### 7.2 `_is_oom` 布尔运算可读性

[ai-service/app/modules/modelhub/llm_loader.py:488-503](file:///e:/项目/FinReport%20Agent/ai-service/app/modules/modelhub/llm_loader.py)：

```python
return (
    "out of memory" in message
    or "cuda oom" in message
    or "oom" in message
    and "cuda" in message
)
```

Python `and` 优先级高于 `or`，逻辑等价于：
```python
("out of memory") or ("cuda oom") or ("oom" and "cuda")
```

逻辑正确但可读性差。**建议**：加括号或拆成 `any([...])`。**不阻断本次审查**（逻辑正确，已有 2 个测试覆盖）。

### 7.3 `VramScheduler.__init__` Redis 客户端 unwrap 逻辑

[ai-service/app/modules/modelhub/vram_scheduler.py:233-241](file:///e:/项目/FinReport%20Agent/ai-service/app/modules/modelhub/vram_scheduler.py)：

```python
if redis_client is None:
    redis_client = get_redis_client().client
elif isinstance(redis_client, RedisClient):
    redis_client = redis_client.client
self._client: RedisLike = redis_client
```

三态 unwrap（None / RedisClient / RedisLike）逻辑分散。**评估**：可接受，因为测试需要直接注入 `_FakeRedis`（绕过 `RedisClient` 包装）。**不阻断本次审查**。

### 7.4 `_resolve_model_path` layoutlm 路径硬编码

[ai-service/app/modules/modelhub/modelhub.py:205-214](file:///e:/项目/FinReport%20Agent/ai-service/app/modules/modelhub/modelhub.py)：

```python
mapping = {
    "7b": self.settings.model_7b_path,
    "1.5b": self.settings.model_15b_path,
    "bge": self.settings.model_embed_path,
    "layoutlm": "models/layoutlmv3-base",  # ← 硬编码
}
```

**评估**：spec §4.7 把 LayoutLM 列为 M4+ 交付物，M2.04 只是占位。**建议**：M4 实现时加 `settings.model_layoutlm_path`。**不阻断本次审查**。

### 7.5 测试文件 `test_m1_init_scripts.py` 用源码字符串匹配验证常量

测试通过 `(SCRIPTS_DIR / "init_minio.py").read_text()` 读源码然后 `assert '"finreport-uploads"' in source` 验证 bucket 配置。这是字符串匹配而非语义验证，重构脚本时会假阳性。

**评估**：M1 已交付验收，本次审查不动 M1 资产。**不阻断本次审查**。

## 8. 风险评估

| 风险 | 等级 | 缓解 |
|---|---|---|
| 无 torch/transformers 环境跑测试失败 | 已消除 | Bug #1 / #2 已修复 |
| spec §8.1 6GB VRAM 约束未在测试中验证 | 中 | 单元测试用 stub backend；M2.12 集成测试需真实 GPU 验证 |
| MQ 拓扑变更未触发 CI 失败 | 低 | `test_m1_init_scripts.py::TestDefinitionsConsistency` 已对 `definitions.json` vs `declare_mq.py` 做交叉校验 |
| PaddleOCR 3.x API 后续版本变更 | 中 | `_PPStructureV3Engine` / `_PaddleOCREngine` 已对 3.x `predict()` 和 2.x legacy 做双路径适配；M2.12 需重测 |
| Redis 分布式锁 Lua 脚本在集群模式语义变化 | 低 | 当前 spec §3.9 单 Redis 实例；M3+ 上集群时需换 `RedLock` 或 `SET NX EX` + CAS 跨节点 |
| M2.06 Extractor 不持有 model_lock 的契约被 M2.08 编排破坏 | 中 | 已在 `docs/decisions/2026-07-20-m2-06-extractor.md` 记录；M2.08 实现时需 review |

## 9. 已完成 Checklist

- [x] 静态审查 31 个源文件
- [x] 静态审查 14 个测试文件
- [x] 干净环境跑全套测试（anaconda Python 3.13）
- [x] 识别 2 个 bug
- [x] 修复 Bug #1（`_build_quant_config` 加 `torch_module` 参数）
- [x] 修复 Bug #2（`test_transformers_backend_load_raises_on_missing_transformers` 加 torch stub）
- [x] 重跑全套测试：227 passed
- [x] ruff check 全绿
- [x] black 全绿（55 files）
- [x] 覆盖率 94%（高于 80% 门槛）
- [x] 撰写审查报告

## 10. 下一步行动项

1. **M2.07 L3 M7 抽取结果校验**：实现 validator + retry + 解析率指标。复用 `build_retry_prompt(error_hint=...)` + `Extractor._parse` 的 `ExtractionResult.success=False` 信号；temp 降到 0.1 重试 1 次后仍失败转 7B（spec §10.3 降级链）
2. **M2.08 L2 三表并行抽取编排**：在 L2 orchestrator 用 `VramScheduler.load_for_scene_with_lock(Scene.EXTRACT)` 持有 lock，调 L3 `extract` handler；busy 时 `nack(requeue=True)` + sleep `model_lock_retry_seconds`
3. **M2.12 集成测试**：真实年报端到端，验证 6GB VRAM 单模型驻存约束、PP-StructureV3 表格识别准确率、Qwen2.5-7B-Instruct-GPTQ-Int4 抽取 F1
4. **技术债**（低优先级）：
   - 给 `_is_oom` 加括号或 `any([...))` 改写
   - M4 实现时给 LayoutLM 加 `settings.model_layoutlm_path`
   - `ModelHub.is_loaded_status` 测试 helper 改用 `hub.llm_loader.is_loaded()` 直接断言

## 11. 结论

M2.01–M2.06 代码质量符合 AGENTS.md / CLAUDE.md 规范：

- **架构合规**：5 层分层清晰，L3 模块边界（parser / modelhub / extractor / mq / api）与 spec §2.3 一致
- **GPU 约束合规**：单模型驻存 + Redis 分布式锁 + LRU evict_idle 三层防护符合 spec §8.1
- **测试覆盖合规**：94% 覆盖率，高于 80% 门槛；单元/集成/E2E 分布与 spec §6.2 一致
- **代码规范合规**：ruff + black 全绿；JSDoc / Pydantic / async 风格统一
- **降级链合规**：spec §10.3 的 OOM / 超时 / JSON 解析失败降级路径在代码 + 测试中可追溯

发现的 2 个 bug 均为"测试在无 torch 环境下失败"的环境适配问题，非生产逻辑错误。修复后 227 测试全绿，可进入 M2.07 实现。
