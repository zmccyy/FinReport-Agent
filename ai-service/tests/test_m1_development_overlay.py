"""Regression coverage for M1 local-development Compose settings."""

from __future__ import annotations

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT_COMPOSE_FILE = REPOSITORY_ROOT / "deploy" / "docker-compose.dev.yml"


def test_frontend_dev_overlay_uses_versioned_api_base_urls() -> None:
    """Browser-origin API and SSE URLs must include the backend's /api/v1 prefix."""
    compose_definition = DEVELOPMENT_COMPOSE_FILE.read_text(encoding="utf-8")

    assert "VITE_API_BASE_URL: http://localhost:8080/api/v1" in compose_definition
    assert "VITE_SSE_BASE_URL: http://localhost:8080/api/v1" in compose_definition
