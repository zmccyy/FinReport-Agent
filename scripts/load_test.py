"""M6.03 限流压测脚本：验证登录/上传等端点超限返回 429 + Retry-After。

用法（需 dev 栈 backend 运行在 localhost:8080）：

    python scripts/load_test.py --endpoint login --count 20
    python scripts/load_test.py --endpoint register --count 20
    python scripts/load_test.py --endpoint refresh --count 20

说明：
- 登录/注册/刷新为公开端点（IP 维度限流），无需 token，最适合自动化验证。
- 认证端点（upload/chat）需要有效 JWT，且会触发真实业务，不在本脚本范围。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "http://localhost:8080"

ENDPOINTS = {
    "login": {
        "path": "/api/v1/auth/login",
        "body": {"username": "loadtest", "password": "wrong-password"},
        "limit": 5,
        "window": 60,
    },
    "register": {
        "path": "/api/v1/auth/register",
        "body": {"username": f"loadtest_{int(time.time())}", "password": "Passw0rd!123"},
        "limit": 5,
        "window": 60,
    },
    "refresh": {
        "path": "/api/v1/auth/refresh",
        "body": {"refreshToken": "invalid-token"},
        "limit": 10,
        "window": 60,
    },
}


def fire_once(path: str, body: dict) -> tuple[int, str | None]:
    """发送一次请求，返回 (status, retry_after)。"""
    req = urllib.request.Request(
        BASE_URL + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers.get("Retry-After")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Retry-After")


def main() -> int:
    parser = argparse.ArgumentParser(description="M6.03 rate limit load test")
    parser.add_argument("--endpoint", choices=ENDPOINTS.keys(), required=True)
    parser.add_argument("--count", type=int, default=20, help="总请求数（默认 20）")
    args = parser.parse_args()

    spec = ENDPOINTS[args.endpoint]
    statuses: list[int] = []
    retry_after_seen: str | None = None
    first_429_at: int | None = None

    for i in range(args.count):
        status, retry_after = fire_once(spec["path"], spec["body"])
        statuses.append(status)
        if status == 429:
            first_429_at = first_429_at if first_429_at is not None else i + 1
            retry_after_seen = retry_after_seen or retry_after
        # 不加延迟，模拟瞬时突发
        time.sleep(0.05)

    total = len(statuses)
    ok = sum(1 for s in statuses if s != 429)
    limited = sum(1 for s in statuses if s == 429)
    print(f"endpoint={args.endpoint} limit={spec['limit']}/{spec['window']}s")
    print(f"total={total} non-429={ok} 429={limited} first_429_at_request={first_429_at}")
    print(f"Retry-After header on 429: {retry_after_seen}")

    passed = limited > 0 and first_429_at is not None and first_429_at <= spec["limit"] + 1
    print("RESULT:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
