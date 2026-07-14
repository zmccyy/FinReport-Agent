# 2026-07-14 开工准备

> 摘要：完成项目开工前的所有准备工作 — 文档审查、环境验证、仓库初始化

## 关键决策

| # | 决策 | 背景 |
|---|---|---|
| D1 | **Java 21 LTS**（非 Java 17） | CLAUDE.md 和设计文档均为 Java 21，实现计划曾误写 Java 17 |
| D2 | **bitsandbytes 0.49.2 原生 Windows 可用** | 深度审查曾认为必须 WSL2，实测 4-bit Linear 在 Windows 上通过 |
| D3 | **PyTorch 2.5.1+cu121**（非 2.3.x） | conda 环境已有 2.5.1，向后兼容，不降级 |
| D4 | **12 周时间表偏乐观** | 最大变量是 M4 微调数据质量，标注量建议从 5k 砍到 1.5-2k |
| D5 | **M1 全程 mock，模型可并行下载** | Qwen2.5-7B GGUF 最晚 M2.04 前到位（W3 初），M1 有 2 周下载窗口 |

## 已完成事项

### 文档
- [x] 审查 CLAUDE.md + 设计文档 + 实现计划（逐节审查报告）
- [x] 深度对抗性审查（可行性/遗漏点/架构压力测试/执行策略）
- [x] 修复 C6 idempotency_key 定义（taskId+step+retry → taskId+step）
- [x] 补充任务状态机 Mermaid 图（spec §3.2.1，含取消/重试/超时/三表并行）
- [x] 创建 6 个里程碑进度文件 docs/progress/m{1-6}.md

### 仓库
- [x] `git init` + `.gitignore` + `.editorconfig`
- [x] 4 次提交：骨架 → 审查报告 → 状态机+模型脚本 → M1 进度

### 环境
- [x] Python 3.11.15 (conda env1-py311) — 含 PyTorch/transformers/peft/accelerate/bitsandbytes
- [x] Java 21.0.11 LTS — javac 就绪
- [x] Docker 29.6.1 + Compose v5.3.0 — 运行中
- [x] Node.js v24.14.0

### 脚本
- [x] `scripts/download_models.py` — 支持 `--list` / `--required` / `--all`
- [x] `data/benchmark/README.md` — 测试年报下载指引

## 审查发现的关键风险

| 风险 | 级别 | 对策 |
|---|---|---|
| M4 微调效果不达预期 | 高 | 标注量砍半保质量；7B 做 teacher model 自动标注 |
| 8 容器 + 模型内存压力 | 中 | 开发时按需启动；M5 前不需要 Milvus |
| ECharts 服务端渲染在 Python 生态不畅 | 中 | 建议用 pyecharts 或 matplotlib 替代 |
| M3 异常检测同比/环比需历史数据 | 中 | 第一版只做科目逻辑异常 |

## 下一步

- [ ] M1.02 Docker Compose 编排 8 service
- [ ] 后台下载 Qwen2.5-7B-Instruct GGUF (~5GB)
- [ ] 手动下载 3 份测试年报 PDF
