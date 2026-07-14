# 2026-07-14 M1.04-M1.06 基础设施初始化脚本

> 摘要：完成 MinIO bucket（6 个）、Milvus collection（fin_kb）、RabbitMQ 拓扑（4+6+DLQ）的声明脚本、配置文件和单元测试

## 关键决策

| # | 决策 | 背景 |
|---|---|---|
| D1 | **Python 脚本 + shell 双轨** | MinIO 提供 `scripts/init_minio.py`（minio-py）和 `deploy/minio/init.sh`（mc CLI）两种初始化方式。Python 脚本用于开发机直接执行；shell 脚本用于容器启动后 docker exec 执行 |
| D2 | **definitions.json + declare_mq.py 双轨** | RabbitMQ 拓扑声明有两种方式：(1) `management.load_definitions` 在容器启动时自动导入 JSON；(2) `scripts/declare_mq.py` 用 pika 程序化声明。两者描述同一拓扑，保持一致性（已通过测试验证） |
| D3 | **DLQ TTL = 7 天, max-length = 10000** | spec §3.1 要求 DLQ 有独立监控告警（长度 > 10），增加 max-length 防止磁盘占满。7 天 TTL 给排查留足时间 |
| D4 | **progress.exchange 使用 fanout** | spec §3.1 明确 progress.exchange 为 fanout 类型。L3 各模块广播进度，L2 ProgressConsumer 按 taskId 路由到 SSE。fanout 的 routing_key 为空字符串 |
| D5 | **Milvus 索引参数与 spec 严格一致** | HNSW M=16, efConstruction=200, 查询 ef=64, 距离度量 IP（内积）。bge-small 输出已归一化，内积等价于余弦相似度 |
| D6 | **ASCII 安全输出** | 所有脚本的 print 输出使用 ASCII 安全字符（`[OK]`/`[ERROR]`/`[SKIP]`），避免 Windows GBK 编码下 emoji 导致的 UnicodeEncodeError |
| D7 | **30 个单元测试覆盖** | 测试覆盖：源码结构验证（字段数/参数检查）、definitions.json 合法性、拓扑一致性、dry-run 执行。无需启动外部服务即可运行 |

## 创建/修改文件清单

### 新建
- `scripts/init_minio.py` — MinIO bucket 初始化（minio-py 客户端，幂等）
- `scripts/init_milvus.py` — Milvus fin_kb collection 创建（HNSW 索引，幂等）
- `scripts/declare_mq.py` — RabbitMQ 拓扑声明（pika 客户端，幂等）
- `deploy/minio/init.sh` — MinIO mc 客户端初始化脚本
- `ai-service/tests/test_m1_init_scripts.py` — 30 个结构验证单元测试

### 修改
- `deploy/rabbitmq/definitions.json` — 完整拓扑定义（4 exchange + 12 queue + 10 binding）
- `deploy/rabbitmq/rabbitmq.conf` — 启用 `management.load_definitions`
- `deploy/docker-compose.yml` — 挂载 definitions.json 到 RabbitMQ 容器
- `docs/progress/m1.md` — M1.04/M1.05/M1.06 打勾

### 依赖
- `minio>=7.2`（Python 包，用于 init_minio.py）
- `pymilvus>=2.4`（Python 包，用于 init_milvus.py）
- `pika>=1.3`（Python 包，用于 declare_mq.py）
- 以上都已列入 `ai-service/pyproject.toml` 的 `[project.optional-dependencies] prod`

## 审查发现与修复

| 严重度 | 问题 | 修复 |
|---|---|---|
| HIGH | `declare_mq.py` 中 `import_definitions_file()` 无条件覆写 `definitions_path` 参数 | 改为仅在 `None` 时计算默认路径 |
| MEDIUM | `init_milvus.py` 中 `except Exception: pass` 静默吞掉所有异常 | 改为仅捕获 `(AttributeError, TypeError)` 并显式 warn |
| MEDIUM | `init_milvus.py` 重试循环中 `except Exception` 过于宽泛 | 保持（连接重试需容忍多种网络异常；脚本退出时进程级清理） |
| MEDIUM | `init_minio.py` 连接失败 `except Exception` 吞具体错误 | 保持（两个 handler 分别处理 S3Error 和非 S3 连接问题，均 sys.exit(1)） |
| LOW | `init.sh` 中用 `2>/dev/null \|\| true` 静默丢弃错误 | 改为 `|| echo "[WARN]"` 明确提示失败 |
| LOW | `declare_mq.py` 中 `field` 未使用 | 移除 |
| LOW | `test_m1_init_scripts.py` 中 `import os`/`import pytest`/`spec` 变量未使用 | 移除 |
| LOW | `test_artifacts_has_7day_expiry` 弱断言 `"7" in source` | 改为限定在 artifacts bucket 代码段内搜索 |
| STYLE | `declare_mq.py` 416 行超 §9.3 建议的 300 行 | 保持（CLI 脚本遵循"one file per task"原则；函数已用 try/finally 正确包裹） |

## 验证结果

- [x] `pytest tests/test_m1_init_scripts.py -v` — 30/30 tests pass
- [x] 三个脚本 `--dry-run` 模式均正常输出
- [x] definitions.json 与 declare_mq.py 拓扑完全一致（自动化测试验证）
- [x] init.sh 与 init_minio.py bucket 列表完全一致（自动化测试验证）
- [x] 所有 emoji 字符已替换为 ASCII 安全字符

## 下一步

- [ ] M1.07 L2 SpringBoot 骨架
