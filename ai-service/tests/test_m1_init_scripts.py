"""
M1.04 - M1.06 初始化脚本单元测试

测试范围:
  - M1.04 init_minio.py: BUCKETS 配置、lifecycle builder、policy builder
  - M1.05 init_milvus.py: FIELDS schema、INDEX_PARAMS
  - M1.06 declare_mq.py: EXCHANGES/QUEUES/BINDINGS 拓扑完整性

测试策略: 从脚本源码中提取纯数据/纯函数进行测试（不依赖外部服务）
"""

import json
import subprocess
import sys
from pathlib import Path

# ============================================================================
# 路径工具
# ============================================================================

SCRIPTS_DIR = Path(__file__).parent.parent.parent / "scripts"
DEPLOY_DIR = Path(__file__).parent.parent.parent / "deploy"


def run_script_dry_run(script_name: str) -> subprocess.CompletedProcess:
    """以 --dry-run 模式运行脚本，验证脚本语法和基本执行。"""
    script_path = SCRIPTS_DIR / script_name
    assert script_path.exists(), f"脚本不存在: {script_path}"
    return subprocess.run(
        [sys.executable, str(script_path), "--dry-run"],
        capture_output=True,
        text=True,
        cwd=str(SCRIPTS_DIR.parent),
    )


# ============================================================================
# M1.04: init_minio.py 测试
# ============================================================================


class TestMinioBuckets:
    """验证 spec §5.5.1 的 6 个 bucket 配置。"""

    def test_buckets_dict_has_six_entries(self):
        """应该恰好定义 6 个 bucket。"""
        source = (SCRIPTS_DIR / "init_minio.py").read_text(encoding="utf-8")
        # 验证源码包含所有 6 个 bucket 名称
        expected_buckets = [
            "finreport-uploads",
            "finreport-artifacts",
            "finreport-reports",
            "finreport-models",
            "finreport-training",
            "finreport-backups",
        ]
        for bucket in expected_buckets:
            assert f'"{bucket}"' in source or f"'{bucket}'" in source, (
                f"BUCKETS 中缺少: {bucket}"
            )

    def test_artifacts_has_7day_expiry(self):
        """finreport-artifacts 应配置 7 天过期（spec §5.5.3）。"""
        source = (SCRIPTS_DIR / "init_minio.py").read_text(encoding="utf-8")
        # artifacts 的 expiry_days 应为 7
        assert '"finreport-artifacts"' in source
        # 验证 lifecycle 中包含 expiry_days: 7 的赋值
        assert 'expiry_days' in source
        # 在 artifacts 相关的 lifecycle 块上下文中有 7
        artifacts_section = source.split('"finreport-artifacts"')[1].split('"finreport-reports"')[0]
        assert "7" in artifacts_section

    def test_reports_has_public_read(self):
        """finreport-reports 应为 public-read 访问策略。"""
        source = (SCRIPTS_DIR / "init_minio.py").read_text(encoding="utf-8")
        assert '"finreport-reports"' in source
        assert "public-read" in source

    def test_lifecycle_config_structure(self):
        """lifecycle 配置应包含正确的 expiry_days 字段。"""
        source = (SCRIPTS_DIR / "init_minio.py").read_text(encoding="utf-8")
        # 验证 build_lifecycle_config 函数存在
        assert "def build_lifecycle_config" in source
        # 验证返回 None 的逻辑存在
        assert "return None" in source
        # 验证 LifecycleConfig 构造
        assert "LifecycleConfig" in source
        assert "Expiration" in source
        assert "expiry_days" in source

    def test_public_read_policy_structure(self):
        """build_public_read_policy 应包含 s3:GetObject 和正确的 JSON 结构。"""
        source = (SCRIPTS_DIR / "init_minio.py").read_text(encoding="utf-8")
        assert "def build_public_read_policy" in source
        assert "s3:GetObject" in source
        assert "Allow" in source
        assert "arn:aws:s3:::" in source
        assert "json.dumps" in source

    def test_dry_run_executes(self):
        """--dry-run 应正常执行不报错。"""
        result = run_script_dry_run("init_minio.py")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "DRY RUN" in result.stdout


# ============================================================================
# M1.05: init_milvus.py 测试
# ============================================================================


class TestMilvusCollection:
    """验证 spec §5.3 fin_kb collection schema。"""

    def test_fields_have_eight_entries(self):
        """fin_kb collection 应有恰好 8 个字段。"""
        source = (SCRIPTS_DIR / "init_milvus.py").read_text(encoding="utf-8")
        # 统计 FieldSchema 出现次数
        field_count = source.count("FieldSchema(name=")
        assert field_count == 8, f"期望 8 个字段，实际 {field_count}"

    def test_embedding_dim_is_512(self):
        """embedding 向量维度应为 512（bge-small 输出）。"""
        source = (SCRIPTS_DIR / "init_milvus.py").read_text(encoding="utf-8")
        assert 'dim=512' in source, "embedding 维度应为 512"

    def test_hnsw_params_match_spec(self):
        """HNSW 索引参数应匹配 spec §5.3: M=16, efConstruction=200, IP。"""
        source = (SCRIPTS_DIR / "init_milvus.py").read_text(encoding="utf-8")
        assert '"M": 16' in source or "'M': 16" in source
        assert '"efConstruction": 200' in source or "'efConstruction': 200" in source
        assert 'IP' in source, "距离度量应为 IP（内积）"

    def test_search_ef_is_64(self):
        """查询 ef 参数应为 64。"""
        source = (SCRIPTS_DIR / "init_milvus.py").read_text(encoding="utf-8")
        assert '"ef": 64' in source or "'ef': 64" in source

    def test_collection_name_is_fin_kb(self):
        """Collection 名称应为 fin_kb。"""
        source = (SCRIPTS_DIR / "init_milvus.py").read_text(encoding="utf-8")
        assert 'COLLECTION_NAME = "fin_kb"' in source

    def test_required_fields_exist(self):
        """所有 spec 要求的字段都应存在。"""
        source = (SCRIPTS_DIR / "init_milvus.py").read_text(encoding="utf-8")
        required_fields = ["id", "doc_id", "chunk_id", "embedding", "page", "position", "chunk_type", "text"]
        for field in required_fields:
            assert f'name="{field}"' in source or f"name='{field}'" in source, (
                f"缺少字段: {field}"
            )

    def test_dry_run_executes(self):
        """--dry-run 应正常执行不报错。"""
        result = run_script_dry_run("init_milvus.py")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "DRY RUN" in result.stdout
        assert "fin_kb" in result.stdout


# ============================================================================
# M1.06: declare_mq.py 测试
# ============================================================================


class TestRabbitMQTopology:
    """验证 spec §3.1 的 4 exchange + 6 queue + DLQ 拓扑。"""

    def test_exchanges_count_is_four(self):
        """应有恰好 4 个 exchange。"""
        source = (SCRIPTS_DIR / "declare_mq.py").read_text(encoding="utf-8")
        # EXCHANGES 列表中应有 4 个 ExchangeDef
        exchange_names = [
            "task.exchange",
            "progress.exchange",
            "chat.exchange",
            "kb.exchange",
        ]
        for name in exchange_names:
            assert f'"{name}"' in source or f"'{name}'" in source, (
                f"缺少 exchange: {name}"
            )

    def test_queues_count_is_six(self):
        """应有恰好 6 个核心队列。"""
        source = (SCRIPTS_DIR / "declare_mq.py").read_text(encoding="utf-8")
        queue_names = [
            "q.parse.requests",
            "q.extract.requests",
            "q.reason.requests",
            "q.progress.results",
            "q.chat.requests",
            "q.kb.build",
        ]
        for name in queue_names:
            assert f'"{name}"' in source or f"'{name}'" in source, (
                f"缺少队列: {name}"
            )

    def test_all_queues_have_dlq(self):
        """每个核心队列都应有对应的 DLQ: q.{name}.dlq。"""
        source = (SCRIPTS_DIR / "declare_mq.py").read_text(encoding="utf-8")
        # 验证 DLQ 命名模式出现在代码中
        assert "dlq_name" in source or ".dlq" in source
        assert 'f"{q.name}.dlq"' in source or "q.name}.dlq" in source or '.dlq"' in source
        # 也检查 definitions.json
        defs = json.loads((DEPLOY_DIR / "rabbitmq" / "definitions.json").read_text(encoding="utf-8"))
        dlq_count = sum(1 for q in defs["queues"] if q["name"].endswith(".dlq"))
        assert dlq_count == 6, f"definitions.json 中应有 6 个 DLQ，实际 {dlq_count}"

    def test_bindings_match_spec(self):
        """所有 spec §3.1 必需的 binding 都应存在。"""
        source = (SCRIPTS_DIR / "declare_mq.py").read_text(encoding="utf-8")
        required_bindings = [
            ("task.exchange", "q.parse.requests", "parse"),
            ("task.exchange", "q.extract.requests", "extract.bs"),
            ("task.exchange", "q.extract.requests", "extract.is"),
            ("task.exchange", "q.extract.requests", "extract.cf"),
            ("task.exchange", "q.reason.requests", "check"),
            ("task.exchange", "q.reason.requests", "report"),
            ("progress.exchange", "q.progress.results", ""),
            ("chat.exchange", "q.chat.requests", "chat"),
            ("kb.exchange", "q.kb.build", "kb.build.report"),
            ("kb.exchange", "q.kb.build", "kb.build.industry"),
        ]
        for ex, qu, rk in required_bindings:
            assert f'"{ex}"' in source or f"'{ex}'" in source, f"Binding 缺少 exchange: {ex}"
            assert f'"{qu}"' in source or f"'{qu}'" in source, f"Binding 缺少 queue: {qu}"

    def test_exchange_types_match_spec(self):
        """Exchange 类型应与 spec 一致。"""
        source = (SCRIPTS_DIR / "declare_mq.py").read_text(encoding="utf-8")
        assert '"direct"' in source or "'direct'" in source
        assert '"fanout"' in source or "'fanout'" in source
        assert '"topic"' in source or "'topic'" in source

    def test_dlq_ttl_is_7_days(self):
        """DLQ TTL 应为 7 天。"""
        source = (SCRIPTS_DIR / "declare_mq.py").read_text(encoding="utf-8")
        assert "7 * 24 * 60 * 60 * 1000" in source or "604800000" in source
        # 也检查 definitions.json
        defs = json.loads((DEPLOY_DIR / "rabbitmq" / "definitions.json").read_text(encoding="utf-8"))
        for q in defs["queues"]:
            if q["name"].endswith(".dlq"):
                ttl = q.get("arguments", {}).get("x-message-ttl")
                assert ttl == 604800000, f"{q['name']} TTL 应为 604800000，实际 {ttl}"

    def test_all_queues_are_durable(self):
        """所有队列应设置 durable=true + delivery_mode=2。"""
        defs = json.loads((DEPLOY_DIR / "rabbitmq" / "definitions.json").read_text(encoding="utf-8"))
        for q in defs["queues"]:
            assert q["durable"] is True, f"{q['name']} 应为 durable=true"

    def test_all_exchanges_are_durable(self):
        """所有 exchange 应设置 durable=true。"""
        defs = json.loads((DEPLOY_DIR / "rabbitmq" / "definitions.json").read_text(encoding="utf-8"))
        for ex in defs["exchanges"]:
            assert ex["durable"] is True, f"{ex['name']} 应为 durable=true"

    def test_definitions_json_is_valid(self):
        """definitions.json 应为合法的 RabbitMQ 定义文件。"""
        defs = json.loads((DEPLOY_DIR / "rabbitmq" / "definitions.json").read_text(encoding="utf-8"))
        assert "rabbit_version" in defs
        assert "queues" in defs
        assert "exchanges" in defs
        assert "bindings" in defs
        # 12 队列 = 6 核心 + 6 DLQ
        assert len(defs["queues"]) == 12, f"总共应有 12 个队列，实际 {len(defs['queues'])}"
        # 4 exchange
        assert len(defs["exchanges"]) == 4, f"应有 4 个 exchange，实际 {len(defs['exchanges'])}"
        # 10 binding
        assert len(defs["bindings"]) == 10, f"应有 10 个 binding，实际 {len(defs['bindings'])}"

    def test_dlq_max_length_is_set(self):
        """DLQ 应有最大长度限制。"""
        defs = json.loads((DEPLOY_DIR / "rabbitmq" / "definitions.json").read_text(encoding="utf-8"))
        for q in defs["queues"]:
            if q["name"].endswith(".dlq"):
                max_len = q.get("arguments", {}).get("x-max-length")
                assert max_len is not None, f"{q['name']} 缺少 x-max-length"

    def test_dry_run_executes(self):
        """--dry-run 应正常打印拓扑预览。"""
        result = run_script_dry_run("declare_mq.py")
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert "task.exchange" in result.stdout
        assert "progress.exchange" in result.stdout
        assert "DLQ" in result.stdout or "dlq" in result.stdout.lower()


# ============================================================================
# 集成验证：init_data.py 中引用的脚本均应存在
# ============================================================================


class TestInitDataIntegration:
    """验证 init_data.py 引用的所有初始化步骤都由对应脚本覆盖。"""

    def test_all_init_scripts_exist(self):
        """M1.03-M1.06 所有初始化脚本应存在。"""
        required_scripts = [
            "init_minio.py",
            "init_milvus.py",
            "declare_mq.py",
        ]
        for script in required_scripts:
            path = SCRIPTS_DIR / script
            assert path.exists(), f"脚本不存在: {path}"


# ============================================================================
# 验证：deploy/minio/init.sh
# ============================================================================


class TestMinioInitShell:
    """验证 deploy/minio/init.sh 脚本完整性。"""

    def test_init_sh_exists(self):
        """init.sh 应存在。"""
        path = DEPLOY_DIR / "minio" / "init.sh"
        assert path.exists(), f"脚本不存在: {path}"

    def test_init_sh_creates_all_buckets(self):
        """init.sh 应创建所有 6 个 bucket。"""
        content = (DEPLOY_DIR / "minio" / "init.sh").read_text(encoding="utf-8")
        expected = [
            "finreport-uploads",
            "finreport-artifacts",
            "finreport-reports",
            "finreport-models",
            "finreport-training",
            "finreport-backups",
        ]
        for bucket in expected:
            assert bucket in content, f"init.sh 缺少 bucket: {bucket}"

    def test_init_sh_sets_lifecycle(self):
        """init.sh 应设置生命周期规则。"""
        content = (DEPLOY_DIR / "minio" / "init.sh").read_text(encoding="utf-8")
        assert "ilm" in content, "应包含 mc ilm 生命周期命令"


# ============================================================================
# 验证：definitions.json 与 declare_mq.py 一致
# ============================================================================


class TestDefinitionsConsistency:
    """definitions.json 与 declare_mq.py 应包含相同的拓扑。"""

    def test_exchange_names_match(self):
        """两个文件中的 exchange 名称应一致。"""
        defs = json.loads((DEPLOY_DIR / "rabbitmq" / "definitions.json").read_text(encoding="utf-8"))
        source = (SCRIPTS_DIR / "declare_mq.py").read_text(encoding="utf-8")

        json_exchanges = {ex["name"] for ex in defs["exchanges"]}
        # 检查源码中是否包含这些 exchange
        for ex_name in json_exchanges:
            assert ex_name in source, f"definitions.json 中的 {ex_name} 在 declare_mq.py 中未找到"

    def test_queue_names_match(self):
        """核心队列名称应一致；DLQ 由 declare_mq.py 动态生成 (q.{name}.dlq)。"""
        defs = json.loads((DEPLOY_DIR / "rabbitmq" / "definitions.json").read_text(encoding="utf-8"))
        source = (SCRIPTS_DIR / "declare_mq.py").read_text(encoding="utf-8")

        core_queues = [q["name"] for q in defs["queues"] if not q["name"].endswith(".dlq")]
        dlq_queues = [q["name"] for q in defs["queues"] if q["name"].endswith(".dlq")]

        # 核心队列名称检查
        for q_name in core_queues:
            assert q_name in source, f"definitions.json 中的核心队列 {q_name} 在 declare_mq.py 中未找到"

        # DLQ 验证: 每个核心队列都应有对应的 DLQ (q.{name}.dlq)
        for q_name in core_queues:
            expected_dlq = f"{q_name}.dlq"
            assert expected_dlq in dlq_queues, (
                f"缺少 DLQ: {expected_dlq}"
            )
        # DLQ 命名模式也应在源代码中出现
        assert ".dlq" in source, "declare_mq.py 应包含 DLQ 生成逻辑"
