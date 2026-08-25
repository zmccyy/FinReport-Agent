# 决策记录：M5.09 问答链路集成测试与阶段验收

> 日期：2026-08-25 | 类型：里程碑阶段总结 + 重大 bug 修复记录

## 背景

M5.09（plan §M5.09）要求问答链路集成测试：5 个标准问题关键事实命中率 ≥ 0.85、
首 token < 15 s，验证方式为 JUnit 集成测试。验收前基线：Python 6 个用例失败、
dev 容器内知识库问答（search_kb）不可用。

## 决策列表

1. **ChatIntegrationIT 双模式设计**（plan 字面文件名 `ChatIntegrationTest.java`
   与项目自 M2.12 起的 `*IT.java` 命名约定冲突，沿用 SlaIntegrationIT 先例
   命名 `ChatIntegrationIT.java` 并在类注释说明）：
   - mock 模式（默认，`mvn verify -Pintegration` 自动执行）：`@SpyBean`
     ChatStreamProxy stub canned 事件流，回归 L2 链路机制（会话 CRUD / 归属
     校验 / SSE 事件序透传 / done 落库 / 首 token SLA 常量固化 / error 透传 /
     空消息拒绝）。
   - real 模式（`-Dchat.it.real-l3=true` 门控，`@EnabledIfSystemProperty`）：
     不 stub，走 dev 栈真实 L3（DeepSeek API + Milvus 知识库），执行 5 个
     标准问题并断言命中率与首 token；验收时手动运行，CI 默认跳过。
2. **qa_questions.json 基准以 dev 库真实抽取值为准**（非 M2.12 ground truth
   近似值）：报告期内报告 id 漂移，取冒烟以来稳定的 `report_id=17`（茅台
   2025）；key_facts 同时放「原始元值」与「亿级约数」两种表达，命中判定为
   去空格/逗号后的子串匹配；direction 字段约束 q2 的方向表述。
3. **dev 镜像补装 torch（CPU）+ sentence-transformers**：dev stage 自 M4 起
   刻意不装（镜像瘦身），导致 dev 容器内 search_kb 的 embedding 直接失败——
   M5.07 冒烟在宿主 uvicorn 上跑掩盖了该缺陷。修复复用 runtime 的
   `TORCH_SPEC`/`TORCH_INSTALL_ARGS` ARG（CPU wheel），其余 prod 重量依赖
   （paddle / weasyprint / matplotlib）仍惰性不装。dev 镜像体积增加可接受：
   prod runtime 才是交付物（< 3GB 验收保持），dev 是开发验证环境。
4. **test_m1_init_scripts.py 改从 `_milvus_schema.py` 读取**：M5.07 审查修复
   引入 schema 共享模块后，测试仍按旧路径加载/断言文本导致 6 个用例回归；
   保持断言意图不变，仅切换数据来源为单一事实来源。
5. **spotbugs 4 项修复 + FlywayMigrationIT V9 同步**（mattpocock code-review
   前的阶段验收自审发现）：ChatDtos/ChatStreamRequest/SessionContext 的
   EI_EXPOSE_REP（record 防御性拷贝）与 ChatService 三目重复调用（spotbugs
   NP 误判，取局部变量）为 M5.05/5.06 遗留；FlywayMigrationIT 硬编码 V8
   （8 迁移/12 表）未随 M5.07 的 V9 更新。均因 DoD「CI 流水线全绿」
   （AGENTS.md §14.1 #5）必须修复，归入 M5.09 验收修复。
6. **命中判定防否定误判 + 首 token 口径**（mattpocock code-review 发现）：
   qa_questions.json 增 `forbid_words` 字段（q3 配「不成立/未通过/不一致…」），
   正向结论词命中但含否定表述判未命中；「首 token < 15s」按项目口径以首个
   生成事件（thought）计时（与 L3 chat.py 注释、M5.04 冒烟一致），口径在
   ChatIntegrationIT 类注释固化；验收门槛（命中率/首 token）改由
   qa_questions.json acceptance 块读取，消除双源漂移。

## 已完成的 checklist

- [x] qa_questions.json（5 问 + key_facts + 验收口径）
- [x] ChatIntegrationIT mock 模式 5 用例全绿（BUILD SUCCESS）
- [x] real 模式验收：命中率 5/5 = 1.00（≥ 0.85），首 token 1.8–3.7 s（< 15 s）
- [x] Python 全量 604 passed（修复 6 个回归用例）；ruff/black 通过
- [x] Java 全量 360 tests 全绿
- [x] docs/eval/m5-qa-hitrate.md 验收报告
- [x] docs/progress/m5.md M5.09 与阶段验收 4 项打勾

## 发现的风险

- search_kb 全库检索（无 report 过滤），问句必须足够特定才能命中目标公司；
  多公司场景下可考虑按 report_id 过滤 doc_id（M6.08 30 份基准评估时评估必要性）。
- dev 镜像体积因 torch 增加约 800MB，本地开发重建时间变长；CI/共享环境
  首次拉取注意磁盘。
- real 模式验收依赖 dev 栈运行 + LLM_API_KEY，CI 不覆盖；M6.08 端到端评估
  时可复用 qa_questions.json 与命中判定逻辑。

## 下一步行动项

- [ ] 提交 M5.09（本记录配套提交：qa_questions.json + ChatIntegrationIT +
      Dockerfile dev 依赖 + test_m1_init_scripts 修复 + 文档）
- [ ] 执行 mattpocock code-review，修复审查发现后合并
- [ ] M6 阶段（前端打磨 / 限流 / 可观测性 / 30 份端到端评估）
