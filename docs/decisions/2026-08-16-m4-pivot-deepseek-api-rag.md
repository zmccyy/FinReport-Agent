# M4 方向变更：放弃本地训练，切换 DeepSeek API + 自研轻量 RAG

> 日期：2026-08-16 | 状态：已批准（用户驱动）
> 关联计划：`.trae/documents/m4-pivot-deepseek-api-rag.md`（v2）

## 背景

- 原方案（spec §4.5）：M4 在 RTX 4050 Mobile 6GB VRAM 上完成 T1（抽取 QLoRA）/ T2（embedding LoRA）/ T3（LayoutLMv3）三个模型的微调。
- 实际评估：6GB VRAM 下训练与推理不可行（7B 4-bit 推理 ~5GB，QLoRA 训练 ~5.2GB，无余量），模型下载 ~8.2GB、标注 5k 样本成本高，本地训练路线不现实。
- M1-M3 已交付的处理链路中 extract / check / report 三个 handler 仍是 mock，等待模型接入——API 化后可立即全链路贯通。

## 决策列表

| # | 决策 | 说明 |
|---|---|---|
| 1 | LLM 推理切换 DeepSeek 官方 API（deepseek-chat） | OpenAI 兼容协议，复用 httpx，不引入 openai SDK；Extractor / LLMReviewer / ReportGenerator 全部走 API |
| 2 | 本地 GPU 栈彻底移除 | TransformersBackend / VramScheduler / ModelLock / torch CUDA / transformers / bitsandbytes 依赖 + 8 项本地模型配置 |
| 3 | Embedding 用本地 bge-small-zh-v1.5 CPU 推理 | sentence-transformers，~95MB，512 维，与现有 Milvus `fin_kb` dim=512 匹配，collection 不重建 |
| 4 | RAG 自研轻量方案 | Milvus + 自写 chunk/retrieve，不引入 LangChain/LlamaIndex（AGENTS.md §9.2 约束），M5 实施 |
| 5 | M4 范围 = API 化重构 + 全链路去 mock | 不只 extract：check（RuleEngine/AnomalyDetector/LLMReviewer）与 report（ReportGenerator/ChartRenderer/PdfConverter）同步接入 |
| 6 | 中间数据通路：parse 产物落 MinIO + L3 只读 MySQL | L2 payload 不携带上游 result（buildPayload 只读任务初始 payload）；extract 从 MinIO 拉 `parsed/{taskId}.json`；check/report 只读查询 L2 已事务写入的 MySQL 表（compose `MYSQL_*` 死配置启用，架构预留兑现） |
| 7 | report 路由从 reasoner 切到 generator handler | consumer.py 原将 `report` 路由到 reason_handler（错位），新增 `generator/handler.py` 归位 |
| 8 | Scene.LAYOUT 删除 | LayoutLMv3 训练取消，表格识别继续用 PP-Structure |

## 原 M4 任务作废声明

原 `docs/progress/m4.md` 中 M4.01–M4.15（数据采集 / Web 标注 / QLoRA 训练 / embedding LoRA / LayoutLMv3 / A/B 对比等 15 个任务）**全部作废**，替换为新的 M4.01–M4.10（见当次重写后的 progress/m4.md）。未开工任何原任务，无沉没成本。

## 已完成 Checklist

- [x] 三项关键决策经用户确认（LLM 供应商 / RAG 方案 / RAG 时机）
- [x] Phase 1 探索：确认 extract/check/report 三处 mock + 数据通路断链（L2 buildPayload 不含上游 result）
- [x] 确认 L2 写入侧契约：StatementWriter（M2.09）/ CheckResultWriter（M3.04）/ ReportArtifactWriter（M3.08）
- [x] v2 计划落档并批准
- [x] M4.01 架构变更落档（决策记录 + spec 第 4 章重写 + AGENTS/skills/progress 同步）
- [x] M4.02 DeepSeekBackend + ModelHub API 路由改造（13 例 mock httpx 单测；提交 15425e6）
- [x] 全模块代码审查 + bug 修复批次（15425e6..5c057f9，5 个修复提交）：
  - L3：MQ 消费者复用事件循环；幂等键对齐 L2 契约（taskId:step 小写）
  - L2：convertAndSend 阻塞 IO 移交 boundedElastic；markDispatchFailed 包裹事务；
    SseEmitterPool.complete() 移除已终结 sink（内存泄漏）；ProgressConsumer dedup
    命中改 ack（原误 nack 进 DLQ）；PDF 查重移除全局 md5 回退（跨用户命中漏洞）；
    三表查询合并单次批量（spec §12.2）
  - L1：四处组件请求序号防竞态；SSE 重连收到有效事件即重置退避；localStorage
    命名空间改 uid 隔离；登录 redirect 防开放重定向；ReportDetail watch 路由参数
- [x] 全栈回归：Java 338/338、Python 510 passed、前端 lint + type-check 通过
- [x] 推送 origin/main（a6d77db..5c057f9）

## 发现的风险

- parse 开启表格识别后可能逼近 90s SLA（配置化开关留退路）
- 年报合并/母公司同名表格筛选靠启发式（取行数最多），M4.10 真实年报验证
- DeepSeek 429 限流（重试 2 次指数退避兜底）
- AnomalyDetector 单期数据仅 logic_conflict 类异常生效（多期对比留 M5）
- SSE 语义变更（complete 后进程内 sink 即删）：晚到/重连客户端的终态事件
  完全依赖 RedisSseEventStore + TaskController 终态判断，Redis 不可用时重连
  客户端将收不到终态信号（前端有心跳/超时兜底，可接受）
- 本地 Windows 开发环境两个已知干扰（CI 不受影响）：black 在 anaconda 环境
  挂起（用 ruff + CI 兜底）；JaCoCo 残留插桩类要求 `mvnw clean test`

## 下一步行动项

按 v2 计划顺序执行，每任务一个 commit：

1. **M4.03 数据通路-解析侧**：minio `put_bytes` 客户端 + parse 产物落
   MinIO（`parsed/{taskId}.json`）+ 表格识别开启
2. **M4.04 extractor 去 mock**：MinIO 拉 parse 产物 + 合并/母公司表格筛选 +
   三表真实抽取（DeepSeek json_mode）
3. **M4.05 check 去 mock**：L3 只读 MySQL 客户端 + RuleEngine /
   AnomalyDetector / LLMReviewer 接入（M3.04 契约）
4. **M4.06 generator 归位**：新增 report handler + 路由从 reasoner 切换
   （报告 + 图表 + PDF，M3.08 契约）
5. **M4.08 GPU 栈移除**：vram_scheduler / TransformersBackend / torch CUDA
   依赖 / 8 项本地模型配置 + 测试清理
6. **M4.07 BgeSmallEmbedder 实装**：ModelHub.embed() 512 维归一化（M5 RAG 前置）
7. **M4.09 部署调整**：Dockerfile CPU wheel + compose 注入 LLM_API_KEY +
   download_models 精简
8. **M4.10 注册与端到端**：model_registry 注册 deepseek-chat + bge-small-zh；
   真实 PDF 全链路验证 + eval_m2_f1 复测（F1 ≥ 0.85）
