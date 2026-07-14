"""
健康检查端点。

提供 /internal/health 用于 Docker Compose healthcheck
和运维探活。M1.13 将扩展添加各组件拨测。
"""

from fastapi import APIRouter

router = APIRouter(tags=["system"])


@router.get("/internal/health")
async def health():
    """系统健康检查。

    Returns:
        dict: 包含状态、服务名和时间戳。
    """
    return {
        "status": "UP",
        "service": "finreport-ai-service",
    }
