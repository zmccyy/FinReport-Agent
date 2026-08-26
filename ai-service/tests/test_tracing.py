"""M6.07 tracing 模块测试：默认关闭 / 显式开启 / 依赖缺失降级。"""

from __future__ import annotations

from fastapi import FastAPI

from app.core import tracing


def test_tracing_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_TRACES_ENABLED", raising=False)
    app = FastAPI()
    assert tracing.setup_tracing(app) is False


def test_tracing_disabled_when_env_false(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_TRACES_ENABLED", "false")
    assert tracing.tracing_enabled() is False


def test_tracing_enabled_flag_parsing(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_TRACES_ENABLED", "TRUE")
    assert tracing.tracing_enabled() is True
    monkeypatch.setenv("OTEL_TRACES_ENABLED", " true ")
    assert tracing.tracing_enabled() is True


def test_setup_skips_gracefully_when_deps_missing(monkeypatch) -> None:
    """依赖缺失（本地最小环境）时 setup_tracing 返回 False 不抛异常。"""
    monkeypatch.setenv("OTEL_TRACES_ENABLED", "true")
    # 模拟 ImportError：清空模块缓存中的 opentelemetry 导入路径
    import builtins
    import importlib

    real_import = builtins.__import__

    def fake_import(name: str, *args, **kwargs):
        if name.startswith("opentelemetry"):
            raise ImportError(f"No module named {name!r} (simulated)")
        return real_import(name, *args, **kwargs)

    builtins.__import__ = fake_import
    try:
        # 确保未缓存的子模块导入走 fake_import
        import sys

        for mod in list(sys.modules):
            if mod.startswith("opentelemetry") and "instrumentation" in mod:
                del sys.modules[mod]
        importlib.reload(tracing)
        assert tracing.setup_tracing(FastAPI()) is False
    finally:
        builtins.__import__ = real_import
        importlib.reload(tracing)
