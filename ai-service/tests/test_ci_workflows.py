"""M1.17 GitHub Actions workflow contract tests."""

from __future__ import annotations

from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS_DIRECTORY = REPOSITORY_ROOT / ".github" / "workflows"


def read_workflow(filename: str) -> str:
    """Return a GitHub Actions workflow definition as UTF-8 text."""
    workflow_path = WORKFLOWS_DIRECTORY / filename
    assert workflow_path.is_file(), f"Missing workflow: {workflow_path}"
    return workflow_path.read_text(encoding="utf-8")


def test_backend_ci_runs_quality_unit_and_integration_checks() -> None:
    """Backend CI should execute the documented Maven quality gates."""
    workflow = read_workflow("backend-ci.yml")

    for expected in (
        "pull_request:",
        "actions/checkout@v4",
        "actions/setup-java@v4",
        'java-version: "21"',
        "./mvnw test",
        "./mvnw verify",
        "./mvnw checkstyle:check spotbugs:check",
        "./mvnw verify -Pintegration",
    ):
        assert expected in workflow


def test_ai_service_ci_runs_quality_tests_and_service_dry_run() -> None:
    """AI-service CI should run formatting, tests, and the M1 mock service dry run."""
    workflow = read_workflow("ai-service-ci.yml")

    for expected in (
        "pull_request:",
        "actions/checkout@v4",
        "actions/setup-python@v5",
        'python-version: "3.11"',
        "ruff check app/ tests/",
        "black --check app/ tests/",
        "pytest tests/ -v --cov=app --cov-fail-under=80",
        ".[prod,dev]",
        "/internal/health",
    ):
        assert expected in workflow


def test_frontend_ci_runs_quality_build_and_playwright_smoke() -> None:
    """Frontend CI should run lint, production build, and the existing E2E smoke test."""
    workflow = read_workflow("frontend-ci.yml")

    for expected in (
        "pull_request:",
        "actions/checkout@v4",
        "actions/setup-node@v4",
        'node-version: "20"',
        "npm ci",
        "npm run lint",
        "npm run build",
        "docker compose",
        "deploy/docker-compose.yml",
        "deploy/docker-compose.dev.yml",
        "playwright",
        "scripts/e2e_m116.py",
    ):
        assert expected in workflow
