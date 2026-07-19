#!/usr/bin/env python3
"""Mock L3 进度注入器（M1 验收辅助脚本）。

背景：L3 FastAPI worker（M1.13/M1.14）尚未实现，上传财报后 PARSE/EXTRACT/CHECK/REPORT
各阶段无人消费。本脚本模拟 L3 worker，按状态机预期顺序向 ``progress.exchange``
（fanout → q.progress.results）发布进度消息，驱动 L2 ProgressConsumer → SSE 全链路，
用于 M1.16「登录 → 上传 → SSE 进度可视化」端到端验收。

用法：
    python scripts/inject_mock_progress.py <taskId> [--interval 1.2] [--host localhost]

序列（与 TaskOrchestrator 进度常量一致）：
    PARSE 15 → EXTRACT 55 → CHECK 75 → REPORT 100（REPORT SUCCESS 触发 done 事件）

注意：handleStepProgress 仅响应 SUCCESS/FAILED；RUNNING 仅透传 SSE 用于前端展示。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

import pika

# (step, status, progress) — 顺序即状态机推进顺序
SEQUENCE: list[tuple[str, str, int]] = [
    ("PARSE", "RUNNING", 5),
    ("PARSE", "SUCCESS", 15),
    ("EXTRACT_BS", "SUCCESS", 30),
    ("EXTRACT_IS", "SUCCESS", 40),
    ("EXTRACT_CF", "SUCCESS", 55),
    ("CHECK", "SUCCESS", 75),
    ("REPORT", "SUCCESS", 100),
]


def inject(task_id: str, host: str, port: int, interval: float) -> None:
    params = pika.ConnectionParameters(
        host=host,
        port=port,
        credentials=pika.PlainCredentials("guest", "guest"),
    )
    connection = pika.BlockingConnection(params)
    channel = connection.channel()
    trace_id = uuid.uuid4().hex

    print(f"[inject] taskId={task_id} → progress.exchange ({host}:{port})", flush=True)
    for step, status, progress in SEQUENCE:
        body = {
            "taskId": task_id,
            "step": step,
            "status": status,
            "progress": progress,
        }
        channel.basic_publish(
            exchange="progress.exchange",
            routing_key="",  # fanout 忽略 routing key
            body=json.dumps(body).encode("utf-8"),
            properties=pika.BasicProperties(
                content_type="application/json",
                delivery_mode=2,  # 持久化
                headers={"traceId": trace_id},
            ),
        )
        print(f"[inject] {step} {status} progress={progress}", flush=True)
        time.sleep(interval)

    connection.close()
    print("[inject] 完成（REPORT SUCCESS 应触发 done 事件）", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock L3 进度注入器")
    parser.add_argument("task_id", help="目标任务 ID（上传接口返回）")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=5672)
    parser.add_argument("--interval", type=float, default=1.2, help="每条消息间隔秒数")
    args = parser.parse_args()

    try:
        inject(args.task_id, args.host, args.port, args.interval)
    except Exception as exc:  # noqa: BLE001
        print(f"[inject] 失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
