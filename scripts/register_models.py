#!/usr/bin/env python3
"""M4.10 model_registry 注册脚本 — deepseek LLM + bge embedding。

按 spec §4.5（2026-08-16 语义调整）向 MySQL ``model_registry`` 写入三条
PRODUCTION 记录：

    | task   | base_model      | adapter_path             |
    |--------|-----------------|--------------------------|
    | extract| {LLM_API_MODEL} | api:{base_url_host}      |
    | reason | {LLM_API_MODEL} | api:{base_url_host}      |
    | embed  | bge-small-zh-v1.5 | models/bge-small-zh-v1.5 |

LLM 模型名默认按优先级取：``--model`` 参数 > 环境变量 ``LLM_API_MODEL`` >
``deploy/.env`` 中的 ``LLM_API_MODEL`` > ``deepseek-chat``，保证注册内容
与部署实际使用的模型一致（Key 本身不入库）。

幂等：``uk_task_version`` 唯一键 + ``INSERT ... ON DUPLICATE KEY UPDATE``，
重复执行只刷新 status/metrics/trained_at，不产生重复行。

用法::

    python scripts/register_models.py                       # 默认连接 localhost:3306
    python scripts/register_models.py --metrics-extract '{"f1": 0.93}'
    python scripts/register_models.py --model deepseek-chat --version v1

依赖：pymysql（宿主机 ``pip install pymysql``；ai-service 容器内自带）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_ENV = REPOSITORY_ROOT / "deploy" / ".env"

EMBED_MODEL = "bge-small-zh-v1.5"
EMBED_ADAPTER_PATH = "models/bge-small-zh-v1.5"
EMBED_REVISION = "hf:BAAI/bge-small-zh-v1.5"


def _load_deploy_env(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines from deploy/.env (comments/blank skipped)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def resolve_llm_model(cli_model: str | None) -> str:
    """Resolve the LLM model name: CLI arg > env var > deploy/.env > default."""
    if cli_model:
        return cli_model
    env_model = os.environ.get("LLM_API_MODEL", "").strip()
    if env_model:
        return env_model
    return _load_deploy_env(DEPLOY_ENV).get("LLM_API_MODEL", "").strip() or "deepseek-chat"


def _host_of(base_url: str) -> str:
    """Extract host[:port] from a base URL for the adapter_path marker."""
    raw = base_url.strip()
    if "://" in raw:
        raw = raw.split("://", 1)[1]
    return raw.rstrip("/")


def _parse_metrics(raw: str | None) -> str | None:
    """Validate an optional JSON metrics string for the registry row."""
    if not raw:
        return None
    try:
        json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError(f"--metrics-* 不是合法 JSON: {error}") from error
    return raw


def register(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    llm_model: str,
    llm_base_url: str,
    version: str,
    metrics_extract: str | None,
    metrics_reason: str | None,
    metrics_embed: str | None,
) -> list[dict[str, Any]]:
    """Upsert the three model_registry rows and return them.

    Raises:
        RuntimeError: When pymysql is missing or MySQL is unreachable.
    """
    try:
        import pymysql
    except ImportError as error:
        raise RuntimeError(
            "pymysql 未安装：宿主机请运行 pip install pymysql"
        ) from error

    api_marker = f"api://{_host_of(llm_base_url)}"
    rows = [
        {
            "task": "extract",
            "base_model": llm_model,
            "adapter_path": api_marker,
            "metrics": metrics_extract,
        },
        {
            "task": "reason",
            "base_model": llm_model,
            "adapter_path": api_marker,
            "metrics": metrics_reason,
        },
        {
            "task": "embed",
            "base_model": EMBED_MODEL,
            "adapter_path": EMBED_ADAPTER_PATH,
            "metrics": metrics_embed,
        },
    ]

    insert_sql = (
        "INSERT INTO model_registry "
        "(task, base_model, adapter_path, version, metrics, status, data_version, "
        " train_cmd_hash, trained_at) "
        "VALUES (%s, %s, %s, %s, %s, 'PRODUCTION', NULL, %s, NOW()) "
        "ON DUPLICATE KEY UPDATE "
        " base_model = VALUES(base_model), "
        " adapter_path = VALUES(adapter_path), "
        " status = 'PRODUCTION', "
        " train_cmd_hash = VALUES(train_cmd_hash), "
        " trained_at = NOW()"
    )

    connection = pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            for row in rows:
                cursor.execute(
                    insert_sql,
                    (
                        row["task"],
                        row["base_model"],
                        row["adapter_path"],
                        version,
                        row["metrics"],
                        EMBED_REVISION if row["task"] == "embed" else api_marker,
                    ),
                )
                # metrics 仅在显式传入时更新，避免复测前的旧值被清空。
                if row["metrics"] is not None:
                    cursor.execute(
                        "UPDATE model_registry SET metrics = %s "
                        "WHERE task = %s AND version = %s",
                        (row["metrics"], row["task"], version),
                    )
            cursor.execute(
                "SELECT id, task, base_model, adapter_path, version, metrics, "
                "status, trained_at FROM model_registry ORDER BY id"
            )
            return [dict(zip(("id", "task", "base_model", "adapter_path",
                              "version", "metrics", "status", "trained_at"), r))
                    for r in cursor.fetchall()]
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="M4.10 model_registry 注册")
    parser.add_argument("--host", default=os.environ.get("MYSQL_HOST", "localhost"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("MYSQL_PORT", "3306")))
    parser.add_argument("--user", default=os.environ.get("MYSQL_USER", "finreport"))
    parser.add_argument("--password", default=os.environ.get("MYSQL_PASSWORD", "finreport"))
    parser.add_argument("--database", default=os.environ.get("MYSQL_DATABASE", "finreport"))
    parser.add_argument("--model", default=None,
                        help="LLM 模型名（默认: LLM_API_MODEL 环境变量/deploy/.env → deepseek-chat）")
    parser.add_argument("--base-url",
                        default=os.environ.get("LLM_API_BASE_URL", "https://api.deepseek.com"))
    parser.add_argument("--version", default="v1", help="注册版本号（默认 v1）")
    parser.add_argument("--metrics-extract", default=None,
                        help="extract 行 metrics JSON（如 eval_m2_f1 产出）")
    parser.add_argument("--metrics-reason", default=None, help="reason 行 metrics JSON")
    parser.add_argument("--metrics-embed", default=None, help="embed 行 metrics JSON")
    args = parser.parse_args()

    metrics_extract = _parse_metrics(args.metrics_extract)
    metrics_reason = _parse_metrics(args.metrics_reason)
    metrics_embed = _parse_metrics(args.metrics_embed)

    llm_model = resolve_llm_model(args.model)
    print(f"[register_models] LLM 模型 = {llm_model}（base_url={args.base_url}）")

    try:
        rows = register(
            host=args.host,
            port=args.port,
            user=args.user,
            password=args.password,
            database=args.database,
            llm_model=llm_model,
            llm_base_url=args.base_url,
            version=args.version,
            metrics_extract=metrics_extract,
            metrics_reason=metrics_reason,
            metrics_embed=metrics_embed,
        )
    except RuntimeError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"[register_models] 已注册/更新 {len(rows)} 行：")
    for row in rows:
        metrics = row["metrics"] or "-"
        print(
            f"  #{row['id']} task={row['task']:<8} base_model={row['base_model']:<22} "
            f"version={row['version']} status={row['status']} "
            f"adapter={row['adapter_path']} metrics={metrics}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
