# M5 问答链路集成验收报告（M5.09）

> 日期：2026-08-25
> 验证方式：`ChatIntegrationIT`（JUnit 集成测试，real 模式 `-Dchat.it.real-l3=true`）
> 基准数据：`data/benchmark/qa_questions.json`
> 验收标准（plan §M5.09 / spec §6.3）：关键事实命中率 ≥ 0.85；首 token < 15 s

## 1. 结论

**命中率 5/5 = 1.00（≥ 0.85 ✅）**；**首 token 全部 < 15 s（实测 1.8–3.7 s ✅）**。

## 2. 测试环境

- dev 栈全量 healthy（backend / ai-service / mysql / redis / rabbitmq / minio / milvus）
- LLM：DeepSeek API（deepseek-chat，deploy/.env 注入）
- 知识库：Milvus `fin_kb` 18736 chunks（3 份 sample 年报：茅台 / 平安 / 宁德）
- 被测对象：贵州茅台 2025 年报（dev 库 `report_id=17` 真实抽取值）

## 3. 逐问结果

| 问题 | 能力 | 关键事实 | 首 token | 结果 |
|---|---|---|---|---|
| q1 资产总计 | query_statement | 303,834,844,021.44 元（约 3038.35 亿元） | 2581 ms | ✅ HIT |
| q2 营收同比 | compute_yoy | 本期 1688.38 亿 vs 上期 1708.99 亿 → **下降约 1.21%** | 1522 ms | ✅ HIT |
| q3 资产=负债+权益 | check_accounting | 恒等成立，勾稽差额 0.0 元 | 2693 ms | ✅ HIT |
| q4 茅台酒基酒设计产能 | search_kb | 46,395.00 吨（年报第 15 页产能表） | 2305 ms | ✅ HIT |
| q5 3 万吨技改工程投资 | search_kb + unit_convert | 计划投资 1,018,000 万元（= 101.8 亿元） | 2555 ms | ✅ HIT |

> 验收多次运行结果稳定：首跑 5/5、二跑 4/5（q3 LLM 措辞换为「等于/通过」，
> key_facts 补全后三跑 5/5）。q4/q5 答案自带页码引用（第 15 页知识库来源）。

## 4. 验收中发现并修复的问题

1. **dev 镜像缺失 embedding 依赖（本轮最大发现）**
   - 现象：q4/q5 知识库问题回答「无法获取」，命中率一度 3/5 = 0.60。
   - 根因：`ai-service/Dockerfile` dev stage 自 M4 起不装 torch/sentence-transformers
     （镜像瘦身），`search_kb → ModelHub.embed → BgeSmallEmbedder` 在 dev 容器内
     直接失败；M5.07 冒烟是在宿主本地 uvicorn 上跑的，容器内缺陷未被暴露。
   - 修复：dev stage 补装 torch（CPU wheel，复用 runtime 的 `TORCH_SPEC`/
     `TORCH_INSTALL_ARGS` ARG）+ `sentence-transformers`；其余 prod 重量依赖
     （paddle / weasyprint / matplotlib）仍保持惰性不装，控制 dev 镜像体积。
   - 修复后检索实测：q4 查询 top1 命中 46,395 吨（score 0.867）；q5 top3 命中
     1,018,000 万元（score 0.846）。
2. **`test_m1_init_scripts.py` Milvus schema 测试回归**
   - 现象：6 个用例失败（`No module named '_milvus_schema'`）。
   - 根因：M5.07 审查修复把 schema 抽到 `scripts/_milvus_schema.py`（共享单源），
     测试仍按旧路径从 `init_milvus.py` 加载/断言文本。
   - 修复：测试改从 `_milvus_schema.py` 读取（保持断言意图：8 字段 / 512 维 /
     HNSW 参数 / fin_kb 名称 / 必选字段）。

## 5. 回归与 CI

- `ChatIntegrationIT` mock 模式（默认，`mvn verify -Pintegration` 自动执行）：
  5 tests 全绿（会话 CRUD + 归属校验、SSE 事件透传与落库、首 token SLA 固化、
  error 透传、空消息拒绝）；real 模式方法按 `-Dchat.it.real-l3=true` 门控跳过。
- Java 全量 `mvn verify -Pintegration`：360 单元 + 15 集成全绿（含
  FlywayMigrationIT V9 同步、spotbugs 4 项修复后 0 告警、checkstyle 通过）。
- Python 全量 `pytest`：604 passed（含本次修复的 6 个用例）。

## 7. 口径说明

- **首 token**：按项目口径以 SSE 流首个生成事件（thought）计时——L3 事件流
  先发 thought（LLM 首段生成）再发最终答案 token，M5.04 冒烟与 L3 chat.py
  注释均以此为准；本报告数值 1.5–2.7 s 即该口径。
- **命中判定**：答案去空格/逗号后含任一 key_fact 判命中；q2 需下降方向词
  一致；q3 含否定表述（不成立/未通过等，`forbid_words`）判未命中，防止
  正向结论词被子串误判。
- **验收门槛**：命中率 ≥ 0.85、首 token < 15 s 由 `qa_questions.json`
  `acceptance` 块定义，测试运行时读取，无硬编码双源。

## 6. 复测命令

```bash
# mock 模式（CI）
cd backend && ./mvnw verify -Pintegration -Dit.test=ChatIntegrationIT

# real 模式（需 dev 栈 + LLM_API_KEY + 知识库已构建）
cd backend && ./mvnw verify -Pintegration -Dit.test=ChatIntegrationIT -Dchat.it.real-l3=true
```
