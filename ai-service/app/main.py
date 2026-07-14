"""
FinReport Agent — L3 AI 服务层入口。

FastAPI 应用，提供文档解析、科目抽取、勾稽核对、报告生成、
Agent 问答等 AI 能力的 HTTP + MQ 接口。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理。

    启动时初始化核心组件，关闭时清理资源。
    M1.13 前为占位实现。
    """
    # TODO: M1.13 — 初始化 ModelHub、MQ consumer、Redis 连接池
    yield
    # TODO: M1.13 — 关闭 MQ 连接、释放模型


app = FastAPI(
    title="FinReport AI Service",
    description="A 股上市公司财报深度解析 Agent — L3 AI 服务层",
    version="0.1.0",
    lifespan=lifespan,
)

# 注册路由
app.include_router(health_router)
