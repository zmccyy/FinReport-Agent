"""Regression tests for M1.16 end-to-end acceptance assertions."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
E2E_SCRIPT = REPOSITORY_ROOT / "scripts" / "e2e_m116.py"


def load_e2e_module() -> ModuleType:
    """Load the E2E helper functions without requiring the browser package in AI CI."""
    spec = importlib.util.spec_from_file_location("e2e_m116", E2E_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    playwright_module = ModuleType("playwright")
    sync_api_module = ModuleType("playwright.sync_api")
    sync_api_module.Page = object
    sync_api_module.sync_playwright = object
    playwright_module.sync_api = sync_api_module

    with patch.dict(
        sys.modules,
        {"playwright": playwright_module, "playwright.sync_api": sync_api_module},
    ):
        spec.loader.exec_module(module)
    return module


class FakeLocator:
    """Minimal Playwright-locator double for E2E helper assertions."""

    def __init__(self, *, count: int, visible: bool = True, text: str = "") -> None:
        self._count = count
        self._visible = visible
        self._text = text
        self.filter_kwargs: dict[str, str] | None = None
        self.locator_selector: str | None = None
        self.locator_kwargs: dict[str, str] | None = None
        self.wait_timeout: int | None = None

    def filter(self, **kwargs: str) -> FakeLocator:
        """Capture the row filter request and return the prepared result."""
        self.filter_kwargs = kwargs
        return self

    @property
    def first(self) -> FakeLocator:
        """Return the first matching row."""
        return self

    def count(self) -> int:
        """Return the prepared matching-row count."""
        return self._count

    def get_by_text(self, text: str, *, exact: bool) -> FakeLocator:
        """Return the report-row status cell locator while asserting a strict lookup."""
        assert text == "已完成"
        assert exact is True
        return self

    def locator(self, selector: str, **kwargs: str) -> FakeLocator:
        """Return a child locator while preserving selector constraints."""
        self.locator_selector = selector
        self.locator_kwargs = kwargs
        return self

    def inner_text(self) -> str:
        """Return the prepared task-card text."""
        return self._text

    def wait_for(self, *, timeout: int) -> None:
        """Record the requested wait timeout."""
        self.wait_timeout = timeout

    def is_visible(self) -> bool:
        """Return the prepared visibility result."""
        return self._visible


class FakePage:
    """Minimal Playwright-page double for report-row assertions."""

    def __init__(self, locator: FakeLocator) -> None:
        self._locator = locator

    def locator(self, selector: str) -> FakeLocator:
        """Return a prepared table-row locator for the expected selector."""
        assert selector == ".el-table__body-wrapper tr"
        return self._locator


def test_extract_task_id_returns_id_from_progress_route() -> None:
    """The E2E script must bind completion assertions to the current task route."""
    e2e = load_e2e_module()

    assert (
        e2e.extract_task_id("http://localhost:5173/tasks/task-123/progress")
        == "task-123"
    )


def test_extract_task_id_rejects_non_progress_route() -> None:
    """An unexpected route must fail instead of silently testing another task."""
    e2e = load_e2e_module()

    with pytest.raises(AssertionError, match="Expected task progress URL"):
        e2e.extract_task_id("http://localhost:5173/reports")


def test_assert_completed_report_row_requires_exact_completed_row() -> None:
    """The report-list validation must require one company row and a completed status."""
    e2e = load_e2e_module()
    locator = FakeLocator(count=1)

    e2e.assert_completed_report_row(FakePage(locator), "贵州茅台")

    assert locator.filter_kwargs == {"has_text": "贵州茅台"}


def test_assert_completed_report_row_rejects_missing_row() -> None:
    """The E2E script must fail when the uploaded company's row is absent."""
    e2e = load_e2e_module()

    with pytest.raises(AssertionError, match="Expected exactly one report row"):
        e2e.assert_completed_report_row(FakePage(FakeLocator(count=0)), "贵州茅台")


def test_assert_task_progress_completed_targets_the_completion_panel() -> None:
    """Completion waiting must target the dedicated panel, not duplicate status text."""
    e2e = load_e2e_module()
    task_id = "task-12345678"
    locator = FakeLocator(count=1, text="任务 task-1234… 解析完成")

    e2e.assert_task_progress_completed(
        type(
            "Page",
            (),
            {
                "url": f"http://localhost:5173/tasks/{task_id}/progress",
                "locator": lambda _self, selector: locator,
            },
        )(),
        task_id,
    )

    assert locator.locator_selector == ".done__title"
    assert locator.locator_kwargs == {"has_text": "解析完成"}
    assert locator.wait_timeout == 60_000


def test_screenshot_directory_is_portable_repository_artifact_path() -> None:
    """Browser diagnostics must be written to a portable path that CI can upload."""
    e2e = load_e2e_module()

    assert e2e.SCREENSHOT_DIRECTORY == REPOSITORY_ROOT / "artifacts" / "m116"
