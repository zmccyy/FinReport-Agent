#!/usr/bin/env python3
"""
声明 RabbitMQ 拓扑 — M1.06

用法:
    python scripts/declare_mq.py                        # 使用默认连接参数
    python scripts/declare_mq.py --host localhost --port 5672
    python scripts/declare_mq.py --dry-run               # 仅打印拓扑预览
    python scripts/declare_mq.py --import-definitions     # 通过 HTTP API 导入 definitions.json

依赖:
    pip install pika
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass

# ============================================================================
# 拓扑定义：spec §3.1 — 4 exchange + 6 核心队列 + DLQ
# ============================================================================

# DLQ 消息 TTL：7 天（毫秒）
DLQ_TTL_MS = 7 * 24 * 60 * 60 * 1000  # 604800000

# DLQ 最大长度
DLQ_MAX_LENGTH = 10000


@dataclass
class ExchangeDef:
    name: str
    type: str  # direct / fanout / topic
    durable: bool = True
    description: str = ""


@dataclass
class QueueDef:
    name: str
    durable: bool = True
    has_dlq: bool = True  # spec §3.1: 所有队列配置 DLQ
    prefetch: int = 1     # spec §3.1: prefetch_count=1


@dataclass
class BindingDef:
    exchange: str
    queue: str
    routing_key: str


# 4 个 exchange
EXCHANGES = [
    ExchangeDef(name="task.exchange", type="direct", description="L2 → L3 任务下发"),
    ExchangeDef(name="progress.exchange", type="fanout", description="L3 → L2 进度广播"),
    ExchangeDef(name="chat.exchange", type="direct", description="L2 → L3 问答请求"),
    ExchangeDef(name="kb.exchange", type="topic", description="离线知识库构建"),
]

# 6 个核心队列（每个自动配 DLQ: q.{name}.dlq）
QUEUES = [
    QueueDef(name="q.parse.requests"),
    QueueDef(name="q.extract.requests"),
    QueueDef(name="q.reason.requests"),   # M8/M10 共用
    QueueDef(name="q.progress.results"),
    QueueDef(name="q.chat.requests"),
    QueueDef(name="q.kb.build"),
]

# 绑定关系
BINDINGS = [
    # task.exchange (direct)
    BindingDef(exchange="task.exchange", queue="q.parse.requests", routing_key="parse"),
    BindingDef(exchange="task.exchange", queue="q.extract.requests", routing_key="extract.bs"),
    BindingDef(exchange="task.exchange", queue="q.extract.requests", routing_key="extract.is"),
    BindingDef(exchange="task.exchange", queue="q.extract.requests", routing_key="extract.cf"),
    BindingDef(exchange="task.exchange", queue="q.reason.requests", routing_key="check"),
    BindingDef(exchange="task.exchange", queue="q.reason.requests", routing_key="report"),
    # progress.exchange (fanout) — routing_key 为空
    BindingDef(exchange="progress.exchange", queue="q.progress.results", routing_key=""),
    # chat.exchange (direct)
    BindingDef(exchange="chat.exchange", queue="q.chat.requests", routing_key="chat"),
    # kb.exchange (topic)
    BindingDef(exchange="kb.exchange", queue="q.kb.build", routing_key="kb.build.report"),
    BindingDef(exchange="kb.exchange", queue="q.kb.build", routing_key="kb.build.industry"),
]


def print_topology():
    """打印完整拓扑预览。"""
    print("=" * 70)
    print("  FinReport Agent — RabbitMQ 拓扑 (spec §3.1)")
    print("=" * 70)

    print(f"\n[Exchange] ({len(EXCHANGES)}):")
    print(f"  {'名称':<24} {'类型':<10} {'说明'}")
    print(f"  {'-'*24} {'-'*10} {'-'*30}")
    for ex in EXCHANGES:
        print(f"  {ex.name:<24} {ex.type:<10} {ex.description}")

    print(f"\n[Queue] ({len(QUEUES)} core + {len(QUEUES)} DLQ = {len(QUEUES)*2}):")
    print(f"  {'核心队列':<28} {'死信队列':<28}")
    print(f"  {'-'*28} {'-'*28}")
    for q in QUEUES:
        dlq_name = f"{q.name}.dlq"
        print(f"  {q.name:<28} {dlq_name:<28}")

    print(f"\n[Binding] ({len(BINDINGS)}):")
    print(f"  {'Exchange':<24} → {'Queue':<24}  routing_key: {''}")
    print(f"  {'-'*24}   {'-'*24}  {'-'*20}")
    for b in BINDINGS:
        print(f"  {b.exchange:<24} → {b.queue:<24}  {b.routing_key or '(空/fanout)'}")

    print(f"\n[Config]:")
    print(f"  durable=true | delivery_mode=2 | prefetch_count=1")
    print(f"  DLQ TTL: 7 天 | DLQ max-length: {DLQ_MAX_LENGTH}")
    print()


def declare_topology(
    host: str = "localhost",
    port: int = 5672,
    user: str = "guest",
    password: str = "guest",
    vhost: str = "/",
    dry_run: bool = False,
) -> bool:
    """
    幂等声明 RabbitMQ 拓扑（exchange + queue + DLQ + binding）。

    使用 pika 逐个声明，重复执行安全。
    """
    if dry_run:
        print_topology()
        return True

    try:
        import pika
    except ImportError:
        print("请先安装 pika: pip install pika")
        sys.exit(1)

    # --- 连接 RabbitMQ ---
    print(f"连接 RabbitMQ: {host}:{port} (user={user}, vhost={vhost})")
    credentials = pika.PlainCredentials(user, password)
    parameters = pika.ConnectionParameters(
        host=host,
        port=port,
        virtual_host=vhost,
        credentials=credentials,
        heartbeat=30,
        blocked_connection_timeout=300,
    )

    max_retries = 10
    for i in range(max_retries):
        try:
            connection = pika.BlockingConnection(parameters)
            break
        except pika.exceptions.AMQPConnectionError:
            if i < max_retries - 1:
                print(f"  等待 RabbitMQ 就绪... ({i+1}/{max_retries})")
                time.sleep(3)
            else:
                print(f"[ERROR] 无法连接 RabbitMQ ({host}:{port})")
                print("   请确认: docker compose -f deploy/docker-compose.yml up -d rabbitmq")
                sys.exit(1)

    channel = connection.channel()
    print(f"[OK] 已连接到 RabbitMQ ({host}:{port})\n")

    try:
        # --- 声明 exchange ---
        print("[Exchange] 声明:")
        for ex in EXCHANGES:
            channel.exchange_declare(
                exchange=ex.name,
                exchange_type=ex.type,
                durable=ex.durable,
                auto_delete=False,
            )
            print(f"  [OK] {ex.name} ({ex.type}) -- {ex.description}")

        # --- 声明核心队列 + DLQ ---
        print("\n[Queue] 声明 Queue + DLQ:")
        for q in QUEUES:
            dlq_name = f"{q.name}.dlq"

            # 核心队列 — 配置 DLX 指向 DLQ
            channel.queue_declare(
                queue=q.name,
                durable=q.durable,
                auto_delete=False,
                arguments={
                    "x-dead-letter-exchange": "",           # 使用 default exchange
                    "x-dead-letter-routing-key": dlq_name,  # 路由到 DLQ
                },
            )

            # DLQ — TTL 7 天 + 长度限制
            channel.queue_declare(
                queue=dlq_name,
                durable=q.durable,
                auto_delete=False,
                arguments={
                    "x-message-ttl": DLQ_TTL_MS,
                    "x-max-length": DLQ_MAX_LENGTH,
                },
            )

            print(f"  [OK] {q.name}  ->  {dlq_name} (TTL=7d, max_len={DLQ_MAX_LENGTH})")

        # --- 绑定 ---
        print("\n[Binding] 声明:")
        for b in BINDINGS:
            channel.queue_bind(
                exchange=b.exchange,
                queue=b.queue,
                routing_key=b.routing_key,
            )
            rk = b.routing_key or "(fanout)"
            print(f"  [OK] {b.exchange}  --[{rk}]-->  {b.queue}")

        # --- 设置 QoS ---
        # 注意：basic_qos 仅对当前 channel 的消费者生效，不持久化到 broker。
        # 各消费者（ai-service workers）必须在自己的 channel 上设置 prefetch_count=1（spec §3.1）。
        # 此处调用仅防止未来在此 channel 上直接消费的场景（占位）。
        channel.basic_qos(prefetch_count=1)

        # --- 汇总 ---
        print(f"\n{'='*70}")
        print(f"拓扑声明完成:")
        print(f"  {len(EXCHANGES)} exchange")
        print(f"  {len(QUEUES)} 核心队列 + {len(QUEUES)} DLQ = {len(QUEUES)*2} 队列")
        print(f"  {len(BINDINGS)} binding")
        print(f"\n验证: 打开 http://{host}:15672 管理界面查看 Queues/Exchanges 页签")
        print(f"{'='*70}")

    finally:
        connection.close()

    return True


def import_definitions_file(
    host: str = "localhost",
    port: int = 15672,
    user: str = "guest",
    password: str = "guest",
    definitions_path: str | None = None,
) -> bool:
    """
    通过 RabbitMQ Management HTTP API 导入 definitions.json。

    RabbitMQ 启动时会自动加载 definitions.json（如果配置了 management.load_definitions），
    此函数用于手动/后续导入场景。
    """
    import base64

    if definitions_path is None:
        definitions_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "deploy/rabbitmq/definitions.json",
        )

    if not os.path.exists(definitions_path):
        print(f"[ERROR] 找不到 definitions 文件: {definitions_path}")
        sys.exit(1)

    with open(definitions_path, "r", encoding="utf-8") as f:
        body = f.read()

    url = f"http://{host}:{port}/api/definitions"
    credentials = base64.b64encode(f"{user}:{password}".encode()).decode()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Basic {credentials}",
    }

    req = urllib.request.Request(url, data=body.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"[OK] definitions.json 已导入 (HTTP {resp.status})")
            return True
    except urllib.error.URLError as e:
        print(f"[ERROR] 导入失败: {e}")
        sys.exit(1)


def delete_topology(
    host: str = "localhost",
    port: int = 5672,
    user: str = "guest",
    password: str = "guest",
    vhost: str = "/",
):
    """删除所有拓扑（用于重建）。谨慎使用！"""
    try:
        import pika
    except ImportError:
        print("请先安装 pika: pip install pika")
        sys.exit(1)

    credentials = pika.PlainCredentials(user, password)
    parameters = pika.ConnectionParameters(
        host=host, port=port, virtual_host=vhost, credentials=credentials,
    )
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()

    print("[WARN] 删除 RabbitMQ 拓扑...")

    # 删除所有队列（核心 + DLQ）
    for q in QUEUES:
        channel.queue_delete(q.name)
        channel.queue_delete(f"{q.name}.dlq")
        print(f"  [DEL] {q.name} + dlq")

    # 删除所有 exchange
    for ex in EXCHANGES:
        channel.exchange_delete(ex.name)
        print(f"  [DEL] {ex.name}")

    connection.close()
    print("[OK] 拓扑已清空")


def main():
    parser = argparse.ArgumentParser(
        description="声明 FinReport Agent RabbitMQ 拓扑（spec §3.1）",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("RABBITMQ_HOST", "localhost"),
        help="RabbitMQ 地址（默认: localhost）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("RABBITMQ_PORT", "5672")),
        help="AMQP 端口（默认: 5672）",
    )
    parser.add_argument(
        "--mgmt-port",
        type=int,
        default=int(os.environ.get("RABBITMQ_MGMT_PORT", "15672")),
        help="Management HTTP API 端口（默认: 15672）",
    )
    parser.add_argument(
        "--user",
        default=os.environ.get("RABBITMQ_USER", "guest"),
        help="RabbitMQ 用户名（默认: guest）",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("RABBITMQ_PASS", "guest"),
        help="RabbitMQ 密码（默认: guest）",
    )
    parser.add_argument(
        "--vhost",
        default="/",
        help="Virtual host（默认: /）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅打印拓扑预览，不实际声明",
    )
    parser.add_argument(
        "--import-definitions",
        action="store_true",
        help="通过 HTTP API 导入 definitions.json（而非用 pika 逐个声明）",
    )
    parser.add_argument(
        "--definitions-path",
        default=None,
        help="definitions.json 的路径（默认: deploy/rabbitmq/definitions.json）",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="[WARN] 删除所有拓扑（用于重建）",
    )
    args = parser.parse_args()

    if args.delete:
        delete_topology(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            vhost=args.vhost,
        )
    elif args.import_definitions:
        import_definitions_file(
            host=args.host,
            port=args.mgmt_port,
            user=args.user,
            password=args.password,
            definitions_path=args.definitions_path or "deploy/rabbitmq/definitions.json",
        )
    else:
        # 默认：pika 逐个声明（幂等）
        declare_topology(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            vhost=args.vhost,
            dry_run=args.dry_run,
        )


if __name__ == "__main__":
    main()
