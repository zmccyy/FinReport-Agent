# M2.01–M2.03 文档解析实现决策记录

> **日期**：2026-07-20
> **任务**：M2.01 DocumentParser / M2.02 表格识别 / M2.03 OCR 兜底
> **状态**：已完成

---

## 背景

实现 L3 M6 文档解析模块：PyMuPDF 提取页面文本+图像（DPI=200），PP-StructureV2/PP-StructureV3 版式分析与表格还原（HTML），扫描件走 PaddleOCR 兜底。输出 `Document{pages:[Page{blocks:[TextBlock|TableBlock|FigureBlock]}]}` 对象（spec §2.3 M6）。本批任务依赖 M1.14（FastAPI + RabbitMQ mock 链路）。

---

## 决策列表

### D1: 扫描页判定改为"有/无 PyMuPDF 文本层"

**初始问题**：第一版启发式用 `MIN_TEXT_CHARS_PER_PAGE=15` + 按页面面积阈值判定扫描页。茅台式极短标题页（如"第一页 标题"共 7 字）被错误判为扫描件，触发不必要的 OCR。

**决策**：只看 PyMuPDF `get_text("dict")` 是否产生文本 block：有 block 即有数字文本层 → 非扫描页；零 block 且 `page.get_text("text")` 也为空 → 扫描页。旧的面积阈值代码废弃。

### D2: PaddleOCR 3.x API 适配 + 2.x legacy 回退

**初始问题**：计划文档/spec 描述的 `PPStructure` 在 paddleocr 3.7.0 中已移除，被 `PPStructureV3`（`predict()` API）取代；`PaddleOCR` 也从 2.x 的 `use_angle_cls/use_gpu/show_log` kwargs 改为 3.x 的 `OCROptions`。

**决策**：
- `TableRecognizer._ensure_engine`：优先 `from paddleocr import PPStructureV3` → `_PPStructureV3Engine` 包装 `predict()` 返回的 page 结果，把 `table_res_list` 里的 `pred_html`/`table_region` 投影到旧 `{type, bbox, res:{html}}` region shape；导入失败再回退 legacy `PPStructure`。
- `OcrFallback._ensure_engine`：构造时先尝试 2.x kwargs，`TypeError` 则改用 3.x 默认构造，再用 `_PaddleOCREngine` 把 `predict()` 的 `rec_polys/rec_texts/rec_scores` 映射回 `[box, (text, score)]` line shape。
- 引擎全部走"惰性构造 + 单例缓存"，构造失败抛 `AiException`。

### D3: LayoutAnalyzer / OcrProvider Protocol 注入

**初始问题**：PP-Structure / PaddleOCR 是重型依赖，首次构造会拉模型权重（modelscope），单测里拉真实 Paddle 不可行。

**决策**：`DocumentParser` 通过 `LayoutAnalyzer`、`OcrProvider` 两个 `Protocol` 类型注入。真实运行时把 `TableRecognizer` / `OcrFallback` 实例传进去；单测传 fake。PP-Structure 与 PaddleOCR 的失败各自被 `try/except` 吞掉并日志告警，不让某一页的 OCR/表格错误拖垮整篇 PDF 解析。

### D4: numpy 钉死 1.26.x（paddleocr 默认 2.x 破坏 torch）

**初始问题**：paddleocr 3.7.0 的依赖链默认拉 `numpy 2.3.5`，导致 conda env1-py311 里的 `torch 2.5.1+cu121` 加载 `shm.dll` 失败（numpy 2.x 与 torch 1.x ABI 不兼容）。

**决策**：`pip install "numpy<2"` 把 numpy 钉回 1.26.4。paddle 3.3.1 / paddleocr 3.7.0 / torch 2.5.1+cu121 / bitsandbytes 0.49.2 在 numpy 1.26.4 下共存验证通过。
**承诺**：后续 stage 不能擅自放开 numpy 到 2.x；如需升级必须同步升级 torch 到支持 numpy 2.x 的版本（torch ≥2.2 在 Linux/mac 可用，但 Windows 仍需 1.26.x，见 M1 已验证的 Windows bitsandbytes 路径）。

### D5: 关于 Git 暂存改动的处置

**初始问题**：工作区起始状态 `scripts/download_models.py` 有暂存改动（modelscope 源 + `--required/--list/--source` 增强 + GPTQ-Int4 清单），未提交。

**决策**：该改动是 M1.17 CI 管线范围内的独立 cleanup，与 M2 解析无关。在 `feature/M1.17-ci-pipeline` 上单独 commit (`79c97d1 chore(deploy): revamp download_models.py ...`) 后再切出 `feature/M2.01-2.03-document-parser` 分支。

---

## 已完成 checklist

- [x] `app/schemas/document.py` — Document/Page/Block 模型
- [x] `app/modules/parser/document_parser.py` — PyMuPDF 解析 + DPI=200 渲染 + LayoutAnalyzer/OcrProvider 注入
- [x] `app/modules/parser/table_recognizer.py` — PPStructureV3 优先 + 2.x 回退 + HTML→rows 提取
- [x] `app/modules/parser/ocr_fallback.py` — PaddleOCR 3.x predict 适配 + 2.x kwargs 回退
- [x] `app/api/parse.py` — `POST /parse/upload` 真实解析（依赖注入 parser）
- [x] `app/main.py` — `AiException` → 500 JSON 处理器
- [x] `pyproject.toml` — prod 依赖登记 `pymupdf / paddlepaddle / paddleocr / python-multipart`
- [x] 31 个新单元测试 + 7 个端点/集成测试；ai-service 总 89 测试全绿
- [x] 覆盖率：整体 93%，parser 三件套 91–94%，`api/parse.py` 100%（M2 阈值 ≥80%）
- [x] `ruff check` / `black --check` 全绿
- [x] `docs/progress/m2.md` 打勾并追加交付说明

## 发现的风险

1. **PPStructureV3 / PaddleOCR 真实推理未做端到端验证**：3.x `predict()` 的输出 shape（`table_res_list.pred_html` / `table_region`、`rec_polys/rec_texts/rec_scores`）来自 API 文档与 signature 推断，未实际下载模型跑通。首次端到端在 M2.12（真实年报）验证时如出现 shape 偏差，需调整 `_PPStructureV3Engine._extract_table_regions` / `_PaddleOCREngine.__call__` 的字段映射。
2. **L2 parse MQ 消费者仍是 M1 mock**：`app/modules/parser/handler.py` 仍返回 mock `{"document":{"source":...,"pageCount":1}}`，没有从 MinIO 拉 PDF + 调真实解析器。真实 L3 MQ 路径的拉取编排留给 M2.12（或一个独立的 M2 任务来收口）。本批只负责 DocumentParser 实现与 `/parse/upload` 直接上传路径。
3. **PyMuPDF 中文字体缺省**：`Page.insert_text` 用默认 base14 字体，中文会渲染成 "?"。本批测试用 ASCII 生成 PDF 验证 parser，避免依赖字体子集；真实年报 PDF 有嵌入字体不受影响。
4. **paddleocr 3.7.0 拉入大量 transitive deps**（modelscope、paddlex、opencv-contrib、shapely、ruamel.yaml 等）。虽是 paddleocr 官方依赖仍属"环境变重"的代价；M2.04+ 的 ModelHub 加载需注意 paddle torch 与 bitsandbytes 共存。

## 下一步行动项

- M2.04：ModelHub 加载 4-bit 7B（依赖 D4 的 numpy 钉版约束）
- M2.05：显存调度 + model_lock
- M2.06/M2.07：Extractor + JSON Schema 校验
- 在 M2.12 真实年报链路首次跑通时，验证并修正 `_PPStructureV3Engine` / `_PaddleOCREngine` 的结果映射
- 补一个独立的"M2.x：L3 MQ parse handler 接真实 DocumentParser + MinIO"任务收尾 L2↔L3 解析链路