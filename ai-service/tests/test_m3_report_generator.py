"""M3.05 ReportGenerator NLG 测试（spec §2.3 M10 + plan M3.05）。

测试覆盖：

1. **prompt 构建** — 含 5 段固定标题 / 三表上下文 / 规则结果 / 异常列表 / chat 模板
2. **生成成功** — LLM 返回合法 JSON 5 段，fallback=False
3. **JSON 解析降级** — LLM 输出非 JSON，走模板降级，5 段齐全
4. **sections 数 ≠ 5 降级** — LLM 输出 3 段或 6 段，走模板降级
5. **段标题为空降级** — LLM 输出有空 title/content，走模板降级
6. **LLM 异常降级** — generate 抛异常，走模板降级
7. **不可变性** — 原 FinancialStatement / CheckResult 不被修改
8. **降级模板内容** — 5 段齐全 + 含公司名 / 报告期 / 关键科目 / 规则结果 / 异常列表
9. **KB 检索段落** — None 和空列表都不报错
10. **to_markdown 渲染** — 5 段拼成 Markdown
11. **all_sections_present** — 5 段齐全且 content 非空
12. **围栏解包** — LLM 输出 ```json ... ``` 围栏也能解析

异步测试用 ``asyncio.run()`` 包装（对齐 ``test_m3_llm_reviewer.py`` 风格，
不引入 ``pytest-asyncio`` 依赖）。
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from app.core.config import Settings
from app.modules.generator.prompts import build_report_prompt
from app.modules.generator.report_generator import ReportGenerator
from app.modules.modelhub.llm_loader import GenerateResult
from app.modules.modelhub.modelhub import ModelHub
from app.schemas.reasoning import (
    Anomaly,
    CheckResult,
    RuleResult,
    RuleType,
    Severity,
)
from app.schemas.report import ReportResult, ReportSectionType
from app.schemas.statement import (
    FinancialStatement,
    StatementItem,
    StatementType,
)

# ---------------------------------------------------------------------------
# Stub ModelHub — 对齐 test_m3_llm_reviewer 风格
# ---------------------------------------------------------------------------


class _StubHub(ModelHub):
    """ModelHub 子类，返回预设 GenerateResult 或抛预设异常。

    绕开 ``ModelHub.__init__`` 避免实例化真实 ``LlmLoader``（会触发 torch
    懒加载）。只保留 ``settings`` + ``generate``。
    """

    def __init__(
        self,
        *,
        response_text: str = "",
        prompt_tokens: int = 100,
        completion_tokens: int = 500,
        latency_ms: float = 3000.0,
        generate_error: Exception | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self._response_text = response_text
        self._prompt_tokens = prompt_tokens
        self._completion_tokens = completion_tokens
        self._latency_ms = latency_ms
        self._generate_error = generate_error
        self.generate_calls: list[dict[str, Any]] = []

    def generate(  # type: ignore[override]
        self,
        prompt: str,
        *,
        max_new_tokens: int | None = None,
        temperature: float = 0.0,
        timeout_seconds: float | None = None,
    ) -> GenerateResult:
        """记录调用并返回预设响应或抛预设异常。"""
        self.generate_calls.append(
            {
                "prompt": prompt,
                "max_new_tokens": max_new_tokens,
                "temperature": temperature,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self._generate_error is not None:
            raise self._generate_error
        return GenerateResult(
            text=self._response_text,
            prompt_tokens=self._prompt_tokens,
            completion_tokens=self._completion_tokens,
            latency_ms=self._latency_ms,
        )


def _run(coro: Any) -> Any:
    """同步执行异步 coroutine（替代 pytest-asyncio）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _statement(
    *,
    bs_items: list[StatementItem] | None = None,
    is_items: list[StatementItem] | None = None,
    cf_items: list[StatementItem] | None = None,
    report_period: str = "2025-12-31",
) -> FinancialStatement:
    """构造 FinancialStatement 测试辅助。"""
    statements: dict[StatementType, list[StatementItem]] = {}
    if bs_items is not None:
        statements[StatementType.BALANCE_SHEET] = bs_items
    if is_items is not None:
        statements[StatementType.INCOME_STATEMENT] = is_items
    if cf_items is not None:
        statements[StatementType.CASH_FLOW] = cf_items
    return FinancialStatement(
        report_period=report_period,
        currency="CNY",
        unit="元",
        statements=statements,
    )


def _maotai_statement() -> FinancialStatement:
    """构造茅台样式的财报（简化版，含三表关键科目）。"""
    return _statement(
        bs_items=[
            StatementItem(item="货币资金", value=150000000000.0),
            StatementItem(item="资产总计", value=280000000000.0),
            StatementItem(item="负债合计", value=60000000000.0),
            StatementItem(item="所有者权益合计", value=220000000000.0),
        ],
        is_items=[
            StatementItem(item="营业收入", value=130000000000.0),
            StatementItem(item="净利润", value=85000000000.0),
        ],
        cf_items=[
            StatementItem(item="经营活动产生的现金流量净额", value=90000000000.0),
        ],
    )


def _make_rule_result(
    *,
    rule_type: RuleType = RuleType.BALANCE_SHEET_IDENTITY,
    severity: Severity = Severity.INFO,
    is_pass: bool = True,
    note: str = "",
    diff: Decimal | None = None,
) -> RuleResult:
    """构造单条 RuleResult 测试辅助。"""
    return RuleResult(
        rule_type=rule_type,
        rule_name=rule_type.chinese_name,
        expected=Decimal("270000000000.0"),
        actual=Decimal("280000000000.0"),
        diff=diff,
        is_pass=is_pass,
        severity=severity,
        note=note,
    )


def _check_result(
    *,
    rules: list[RuleResult] | None = None,
    anomalies: list[Anomaly] | None = None,
    confidence: float = 0.9,
) -> CheckResult:
    """构造 CheckResult 测试辅助。"""
    return CheckResult(
        rules=(
            rules
            if rules is not None
            else [_make_rule_result(is_pass=True, severity=Severity.INFO)]
        ),
        anomalies=anomalies if anomalies is not None else [],
        confidence=confidence,
        report_period="2025-12-31",
    )


def _valid_llm_response(
    *,
    titles: list[str] | None = None,
    contents: list[str] | None = None,
) -> str:
    """构造合法 5 段 JSON 响应。"""
    default_titles = ["公司概况", "财务概览", "三表分析", "异常与风险", "结论"]
    default_contents = [
        "贵州茅台酒股份有限公司，报告期末日 2025-12-31。",
        "本期营业收入 1300 亿元，净利润 850 亿元。",
        "资产负债表资产总计 2800 亿元，负债合计 600 亿元。",
        "勾稽规则全部通过，未检出异常。",
        "整体财务数据一致性良好，建议持续关注。",
    ]
    titles = titles if titles is not None else default_titles
    contents = contents if contents is not None else default_contents
    return json.dumps(
        {"sections": [{"title": t, "content": c} for t, c in zip(titles, contents)]},
        ensure_ascii=False,
    )


@pytest.fixture
def maotai_statement() -> FinancialStatement:
    """茅台样式财报。"""
    return _maotai_statement()


@pytest.fixture
def maotai_check() -> CheckResult:
    """茅台样式勾稽结果（全部通过）。"""
    return _check_result()


# ---------------------------------------------------------------------------
# build_report_prompt
# ---------------------------------------------------------------------------


class TestBuildReportPrompt:
    """prompt 构建测试。"""

    def test_should_include_chat_template_markers(self) -> None:
        """prompt 应含 chat-style 模板标记。"""
        prompt = build_report_prompt(
            _maotai_statement(),
            _check_result(),
        )
        assert "<|im_start|>system" in prompt
        assert "<|im_start|>user" in prompt
        assert "<|im_start|>assistant" in prompt

    def test_should_include_5_section_titles(self) -> None:
        """prompt 应含 5 段固定标题（公司概况/财务概览/三表分析/异常与风险/结论）。"""
        prompt = build_report_prompt(
            _maotai_statement(),
            _check_result(),
        )
        assert "公司概况" in prompt
        assert "财务概览" in prompt
        assert "三表分析" in prompt
        assert "异常与风险" in prompt
        assert "结论" in prompt

    def test_should_include_company_info(self) -> None:
        """prompt 应含公司名 / 股票代码 / 报告期 / 单位。"""
        prompt = build_report_prompt(
            _maotai_statement(),
            _check_result(),
            company_name="贵州茅台",
            company_code="600519",
        )
        assert "贵州茅台" in prompt
        assert "600519" in prompt
        assert "2025-12-31" in prompt
        assert "CNY" in prompt
        assert "元" in prompt

    def test_should_include_statement_context(self) -> None:
        """prompt 应含三表科目上下文。"""
        prompt = build_report_prompt(
            _maotai_statement(),
            _check_result(),
        )
        assert "资产负债表" in prompt
        assert "资产总计" in prompt
        assert "280000000000.00" in prompt
        assert "利润表" in prompt
        assert "营业收入" in prompt
        assert "现金流量表" in prompt
        assert "经营活动产生的现金流量净额" in prompt

    def test_should_include_rules_and_anomalies(self) -> None:
        """prompt 应含勾稽规则结果与异常列表。"""
        check = _check_result(
            rules=[
                _make_rule_result(
                    is_pass=False,
                    severity=Severity.ERROR,
                    diff=Decimal("10000000000.0"),
                    note="差异超容差",
                )
            ],
            anomalies=[
                Anomaly(
                    item_name="应收账款",
                    anomaly_type="yoy_change",
                    severity=Severity.WARN,
                    description="同比增长 50%",
                )
            ],
            confidence=0.5,
        )
        prompt = build_report_prompt(_maotai_statement(), check)
        assert "资产=负债+所有者权益" in prompt
        assert "失败" in prompt
        assert "ERROR" in prompt
        assert "10000000000.00" in prompt
        assert "差异超容差" in prompt
        assert "应收账款" in prompt
        assert "yoy_change" in prompt

    def test_should_handle_no_anomalies(self) -> None:
        """无异常时 prompt 应含 (无异常) 标记。"""
        prompt = build_report_prompt(
            _maotai_statement(),
            _check_result(),
        )
        assert "(无异常)" in prompt

    def test_should_handle_no_kb_snippets(self) -> None:
        """无检索段落时 prompt 应含 (无检索段落) 标记。"""
        prompt = build_report_prompt(
            _maotai_statement(),
            _check_result(),
            kb_snippets=None,
        )
        assert "(无检索段落)" in prompt

    def test_should_include_kb_snippets(self) -> None:
        """有检索段落时 prompt 应含段落内容。"""
        prompt = build_report_prompt(
            _maotai_statement(),
            _check_result(),
            kb_snippets=["茅台 2024 年报披露净利润同比增长 15%", "行业龙头地位稳固"],
        )
        assert "茅台 2024 年报披露" in prompt
        assert "行业龙头地位稳固" in prompt

    def test_should_truncate_long_snippets(self) -> None:
        """过长检索段落应截断到 500 字符。"""
        long_snippet = "甲" * 600
        prompt = build_report_prompt(
            _maotai_statement(),
            _check_result(),
            kb_snippets=[long_snippet],
        )
        assert "甲" * 500 in prompt
        assert "甲" * 600 not in prompt  # 截断后只剩 500 个

    def test_should_limit_statement_items_to_15(self) -> None:
        """三表上下文每表最多 15 个科目，避免 prompt 过长。"""
        many_items = [StatementItem(item=f"科目{i}", value=float(i)) for i in range(30)]
        statement = _statement(bs_items=many_items)
        prompt = build_report_prompt(statement, _check_result())
        assert "科目0" in prompt
        assert "科目14" in prompt
        assert "科目15" not in prompt  # 第 16 个不出现


# ---------------------------------------------------------------------------
# ReportGenerator.generate — 成功路径
# ---------------------------------------------------------------------------


class TestGenerateSuccess:
    """生成成功路径测试。"""

    def test_should_return_5_sections_on_valid_json(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 返回合法 5 段 JSON 时，返回 5 段报告 + fallback=False。"""
        hub = _StubHub(response_text=_valid_llm_response())
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.success is True
        assert result.fallback is False
        assert len(result.sections) == 5
        assert result.sections[0].section_type == ReportSectionType.COMPANY_OVERVIEW
        assert result.sections[1].section_type == ReportSectionType.FINANCIAL_OVERVIEW
        assert result.sections[2].section_type == ReportSectionType.STATEMENT_ANALYSIS
        assert result.sections[3].section_type == ReportSectionType.ANOMALY_AND_RISK
        assert result.sections[4].section_type == ReportSectionType.CONCLUSION
        assert all(s.content.strip() for s in result.sections)
        assert result.error is None
        assert result.report_period == "2025-12-31"
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 500
        assert result.latency_ms == 3000.0

    def test_should_unwrap_json_fence(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 输出 ```json ... ``` 围栏也能解析。"""
        raw = "```json\n" + _valid_llm_response() + "\n```"
        hub = _StubHub(response_text=raw)
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.success is True
        assert len(result.sections) == 5

    def test_should_pass_temperature_and_timeout(self) -> None:
        """构造器参数应透传到 generate 调用。"""
        hub = _StubHub(response_text=_valid_llm_response())
        gen = ReportGenerator(
            hub,
            max_new_tokens=1024,
            temperature=0.5,
            timeout_seconds=30.0,
        )
        _run(gen.generate(_maotai_statement(), _check_result()))
        assert len(hub.generate_calls) == 1
        call = hub.generate_calls[0]
        assert call["max_new_tokens"] == 1024
        assert call["temperature"] == 0.5
        assert call["timeout_seconds"] == 30.0

    def test_should_pass_company_info_to_prompt(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """公司名 / 股票代码应透传到 prompt。"""
        hub = _StubHub(response_text=_valid_llm_response())
        gen = ReportGenerator(hub)

        _run(
            gen.generate(
                maotai_statement,
                maotai_check,
                company_name="贵州茅台",
                company_code="600519",
            )
        )

        assert len(hub.generate_calls) == 1
        prompt = hub.generate_calls[0]["prompt"]
        assert "贵州茅台" in prompt
        assert "600519" in prompt

    def test_should_pass_kb_snippets_to_prompt(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """kb_snippets 应透传到 prompt。"""
        hub = _StubHub(response_text=_valid_llm_response())
        gen = ReportGenerator(hub)

        _run(
            gen.generate(
                maotai_statement,
                maotai_check,
                kb_snippets=["茅台行业地位稳固"],
            )
        )

        prompt = hub.generate_calls[0]["prompt"]
        assert "茅台行业地位稳固" in prompt


# ---------------------------------------------------------------------------
# ReportGenerator.generate — 降级路径
# ---------------------------------------------------------------------------


class TestGenerateFallback:
    """降级路径测试。"""

    def test_should_fallback_on_non_json_output(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 输出非 JSON 时降级到模板。"""
        hub = _StubHub(response_text="这不是 JSON")
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.success is False
        assert result.fallback is True
        assert len(result.sections) == 5  # 模板仍 5 段齐全
        assert "无法解析为 JSON" in (result.error or "")
        assert result.raw_text == "这不是 JSON"

    def test_should_fallback_on_empty_output(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 输出空字符串时降级到模板。"""
        hub = _StubHub(response_text="")
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert len(result.sections) == 5

    def test_should_fallback_on_wrong_section_count(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 输出 3 段时降级到模板。"""
        raw = json.dumps(
            {
                "sections": [
                    {"title": "公司概况", "content": "..."},
                    {"title": "财务概览", "content": "..."},
                    {"title": "结论", "content": "..."},
                ]
            },
            ensure_ascii=False,
        )
        hub = _StubHub(response_text=raw)
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert len(result.sections) == 5  # 模板仍 5 段齐全
        assert "sections 字段不合规" in (result.error or "")

    def test_should_fallback_on_six_sections(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 输出 6 段时降级到模板。"""
        sections = [{"title": f"段{i}", "content": f"内容{i}"} for i in range(6)]
        raw = json.dumps({"sections": sections}, ensure_ascii=False)
        hub = _StubHub(response_text=raw)
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert len(result.sections) == 5

    def test_should_fallback_on_empty_title(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 输出空 title 时降级到模板。"""
        sections = [{"title": f"段{i}", "content": f"内容{i}"} for i in range(4)] + [
            {"title": "", "content": "内容5"}
        ]
        raw = json.dumps({"sections": sections}, ensure_ascii=False)
        hub = _StubHub(response_text=raw)
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert len(result.sections) == 5

    def test_should_fallback_on_empty_content(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 输出空 content 时降级到模板。"""
        sections = [{"title": f"段{i}", "content": f"内容{i}"} for i in range(4)] + [
            {"title": "结论", "content": ""}
        ]
        raw = json.dumps({"sections": sections}, ensure_ascii=False)
        hub = _StubHub(response_text=raw)
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert len(result.sections) == 5

    def test_should_fallback_on_non_dict_output(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 输出 JSON list 时降级到模板。"""
        hub = _StubHub(response_text="[1, 2, 3]")
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert len(result.sections) == 5

    def test_should_fallback_on_sections_not_list(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 输出 sections 不是 list 时降级到模板。"""
        raw = json.dumps({"sections": "不是 list"}, ensure_ascii=False)
        hub = _StubHub(response_text=raw)
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert len(result.sections) == 5

    def test_should_fallback_on_section_item_not_dict(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """sections list 中某元素不是 dict 时降级到模板。"""
        raw = json.dumps({"sections": ["not a dict", 2, 3, 4, 5]}, ensure_ascii=False)
        hub = _StubHub(response_text=raw)
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert len(result.sections) == 5

    def test_should_fallback_on_garbage_with_braces(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 输出含 ``{...}`` 但不是合法 JSON 时降级到模板。

        覆盖 ``_extract_json_object`` 三层降级路径：直接解析失败 →
        围栏不命中 → 贪婪匹配 ``{...}`` 子串后再次失败 → 返回 None。
        """
        hub = _StubHub(response_text="prefix {not valid json} suffix")
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert len(result.sections) == 5

    def test_should_fallback_on_fenced_garbage(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 输出 ```json ... ``` 围栏包裹的非法 JSON 时降级到模板。

        覆盖 ``_extract_json_object`` 围栏解包后 json.loads 失败的中间分支。
        """
        hub = _StubHub(response_text="```json\n{not valid json}\n```")
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert len(result.sections) == 5

    def test_should_fallback_on_generate_exception(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """generate 抛异常时降级到模板。"""
        hub = _StubHub(
            generate_error=RuntimeError("CUDA OOM"),
        )
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert len(result.sections) == 5
        assert "CUDA OOM" in (result.error or "")
        assert result.raw_text == ""  # 异常路径无 raw_text

    def test_should_fallback_on_generate_timeout(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """generate 抛 TimeoutError 时降级到模板并标记超时。"""
        hub = _StubHub(generate_error=TimeoutError("45s 超时"))
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert "超时" in (result.error or "")

    def test_should_preserve_metrics_on_fallback(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """降级时应保留 LLM 调用的 metrics（tokens / latency）。"""
        hub = _StubHub(
            response_text="invalid json",
            prompt_tokens=200,
            completion_tokens=300,
            latency_ms=5000.0,
        )
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.fallback is True
        assert result.prompt_tokens == 200
        assert result.completion_tokens == 300
        assert result.latency_ms == 5000.0


# ---------------------------------------------------------------------------
# ReportGenerator.generate — 不可变性
# ---------------------------------------------------------------------------


class TestImmutability:
    """不可变性测试。"""

    def test_should_not_mutate_statement(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """generate 不应修改 FinancialStatement。"""
        original_period = maotai_statement.report_period
        original_bs_count = len(
            maotai_statement.statements[StatementType.BALANCE_SHEET]
        )
        original_is_count = len(
            maotai_statement.statements[StatementType.INCOME_STATEMENT]
        )
        original_cf_count = len(maotai_statement.statements[StatementType.CASH_FLOW])

        hub = _StubHub(response_text=_valid_llm_response())
        gen = ReportGenerator(hub)
        _run(gen.generate(maotai_statement, maotai_check))

        assert maotai_statement.report_period == original_period
        assert (
            len(maotai_statement.statements[StatementType.BALANCE_SHEET])
            == original_bs_count
        )
        assert (
            len(maotai_statement.statements[StatementType.INCOME_STATEMENT])
            == original_is_count
        )
        assert (
            len(maotai_statement.statements[StatementType.CASH_FLOW])
            == original_cf_count
        )

    def test_should_not_mutate_check_result(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """generate 不应修改 CheckResult。"""
        original_rules_count = len(maotai_check.rules)
        original_anomalies_count = len(maotai_check.anomalies)
        original_confidence = maotai_check.confidence
        original_notes = [r.note for r in maotai_check.rules]

        hub = _StubHub(response_text=_valid_llm_response())
        gen = ReportGenerator(hub)
        _run(gen.generate(maotai_statement, maotai_check))

        assert len(maotai_check.rules) == original_rules_count
        assert len(maotai_check.anomalies) == original_anomalies_count
        assert maotai_check.confidence == original_confidence
        assert [r.note for r in maotai_check.rules] == original_notes


# ---------------------------------------------------------------------------
# 降级模板内容
# ---------------------------------------------------------------------------


class TestFallbackContent:
    """降级模板内容测试。"""

    def test_should_include_company_info_in_fallback(self) -> None:
        """降级报告应含公司名 / 股票代码 / 报告期。"""
        hub = _StubHub(response_text="invalid")
        gen = ReportGenerator(hub)

        result = _run(
            gen.generate(
                _maotai_statement(),
                _check_result(),
                company_name="贵州茅台",
                company_code="600519",
            )
        )

        assert result.fallback is True
        overview = result.sections[0].content
        assert "贵州茅台" in overview
        assert "600519" in overview
        assert "2025-12-31" in overview

    def test_should_include_key_metrics_in_fallback(self) -> None:
        """降级报告应含资产总计 / 负债合计 / 净利润等关键科目。"""
        hub = _StubHub(response_text="invalid")
        gen = ReportGenerator(hub)

        result = _run(gen.generate(_maotai_statement(), _check_result()))

        financial = result.sections[1].content
        assert "资产总计" in financial
        assert "280000000000.00" in financial
        assert "负债合计" in financial
        assert "60000000000.00" in financial
        assert "营业收入" in financial
        assert "净利润" in financial

    def test_should_include_rules_in_fallback(self) -> None:
        """降级报告应含勾稽规则结果。"""
        check = _check_result(
            rules=[
                _make_rule_result(
                    is_pass=False,
                    severity=Severity.ERROR,
                    diff=Decimal("10000000000.0"),
                    note="差异超容差",
                )
            ],
            confidence=0.5,
        )
        hub = _StubHub(response_text="invalid")
        gen = ReportGenerator(hub)

        result = _run(gen.generate(_maotai_statement(), check))

        risk = result.sections[3].content
        assert "资产=负债+所有者权益" in risk
        assert "失败" in risk
        assert "ERROR" in risk
        assert "10000000000" in risk
        assert "差异超容差" in risk

    def test_should_include_anomalies_in_fallback(self) -> None:
        """降级报告应含异常列表。"""
        check = _check_result(
            anomalies=[
                Anomaly(
                    item_name="应收账款",
                    anomaly_type="yoy_change",
                    severity=Severity.WARN,
                    description="同比增长 50%",
                )
            ],
        )
        hub = _StubHub(response_text="invalid")
        gen = ReportGenerator(hub)

        result = _run(gen.generate(_maotai_statement(), check))

        risk = result.sections[3].content
        assert "应收账款" in risk
        assert "yoy_change" in risk
        assert "同比增长 50%" in risk

    def test_should_indicate_all_pass_when_no_issues(self) -> None:
        """全部规则通过且无异常时结论应说明整体一致性良好。"""
        hub = _StubHub(response_text="invalid")
        gen = ReportGenerator(hub)

        result = _run(
            gen.generate(
                _maotai_statement(),
                _check_result(
                    rules=[_make_rule_result(is_pass=True, severity=Severity.INFO)],
                    anomalies=[],
                    confidence=1.0,
                ),
            )
        )

        conclusion = result.sections[4].content
        assert "全部通过" in conclusion or "一致性良好" in conclusion

    def test_should_indicate_issues_when_rules_fail(self) -> None:
        """有规则失败时结论应说明失败数量。"""
        check = _check_result(
            rules=[
                _make_rule_result(
                    is_pass=False,
                    severity=Severity.ERROR,
                    diff=Decimal("10000000000.0"),
                ),
                _make_rule_result(
                    rule_type=RuleType.NET_INCOME_TO_RETAINED,
                    is_pass=True,
                    severity=Severity.INFO,
                ),
                _make_rule_result(
                    rule_type=RuleType.CASH_FLOW_VS_NET_INCOME,
                    is_pass=True,
                    severity=Severity.INFO,
                ),
            ],
            confidence=0.3,
        )
        hub = _StubHub(response_text="invalid")
        gen = ReportGenerator(hub)

        result = _run(gen.generate(_maotai_statement(), check))

        conclusion = result.sections[4].content
        assert "1 条失败" in conclusion

    def test_fallback_should_have_5_non_empty_sections(self) -> None:
        """降级报告必须 5 段齐全且 content 非空。"""
        hub = _StubHub(response_text="invalid")
        gen = ReportGenerator(hub)

        result = _run(gen.generate(_maotai_statement(), _check_result()))

        assert result.all_sections_present is True
        assert len(result.sections) == 5


# ---------------------------------------------------------------------------
# to_markdown / all_sections_present
# ---------------------------------------------------------------------------


class TestReportResultHelpers:
    """ReportResult 辅助方法测试。"""

    def test_to_markdown_should_render_5_sections(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """to_markdown 应渲染 5 段为 Markdown 文本。"""
        hub = _StubHub(response_text=_valid_llm_response())
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))
        markdown = result.to_markdown()

        assert "# 公司概况" in markdown
        assert "# 财务概览" in markdown
        assert "# 三表分析" in markdown
        assert "# 异常与风险" in markdown
        assert "# 结论" in markdown
        # 段落之间应有空行分隔
        assert "\n\n# " in markdown

    def test_all_sections_present_on_success(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """成功路径下 all_sections_present 应为 True。"""
        hub = _StubHub(response_text=_valid_llm_response())
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.all_sections_present is True

    def test_all_sections_present_false_when_count_not_5(self) -> None:
        """sections 数量 ≠ 5 时 all_sections_present 应为 False。"""
        from app.schemas.report import ReportSection

        result = ReportResult(
            sections=[
                ReportSection(
                    section_type=ReportSectionType.COMPANY_OVERVIEW,
                    title="公司概况",
                    content="x",
                )
            ]
        )
        assert result.all_sections_present is False

    def test_all_sections_present_false_when_empty_content(self) -> None:
        """sections 5 段但某段 content 为空时应为 False。"""
        from app.schemas.report import ReportSection

        result = ReportResult(
            sections=[
                ReportSection(section_type=t, title=t.chinese_name, content="")
                for t in [
                    ReportSectionType.COMPANY_OVERVIEW,
                    ReportSectionType.FINANCIAL_OVERVIEW,
                    ReportSectionType.STATEMENT_ANALYSIS,
                    ReportSectionType.ANOMALY_AND_RISK,
                    ReportSectionType.CONCLUSION,
                ]
            ]
        )
        assert result.all_sections_present is False

    def test_success_property_distinguishes_paths(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """success 属性应正确区分成功与降级路径。"""
        # 成功路径
        hub_ok = _StubHub(response_text=_valid_llm_response())
        gen_ok = ReportGenerator(hub_ok)
        ok_result = _run(gen_ok.generate(maotai_statement, maotai_check))
        assert ok_result.success is True

        # 降级路径
        hub_bad = _StubHub(response_text="invalid")
        gen_bad = ReportGenerator(hub_bad)
        bad_result = _run(gen_bad.generate(maotai_statement, maotai_check))
        assert bad_result.success is False


# ---------------------------------------------------------------------------
# 默认参数与边界
# ---------------------------------------------------------------------------


class TestDefaultsAndEdgeCases:
    """默认参数与边界测试。"""

    def test_default_max_new_tokens_is_2048(self) -> None:
        """默认 max_new_tokens 应为 2048（5 段报告较长）。"""
        hub = _StubHub(response_text=_valid_llm_response())
        gen = ReportGenerator(hub)
        assert gen.max_new_tokens == 2048

    def test_default_temperature_is_0_3(self) -> None:
        """默认 temperature 应为 0.3（自然语言但偏低避免幻觉）。"""
        hub = _StubHub(response_text=_valid_llm_response())
        gen = ReportGenerator(hub)
        assert gen.temperature == 0.3

    def test_default_timeout_is_45s(self) -> None:
        """默认 timeout 应为 45s（对齐 spec §3.7 REPORT SLA）。"""
        hub = _StubHub(response_text=_valid_llm_response())
        gen = ReportGenerator(hub)
        assert gen.timeout_seconds == 45.0

    def test_should_work_with_empty_statements(self) -> None:
        """三表全空时应仍能生成（降级路径）报告。"""
        empty_statement = FinancialStatement(
            report_period="2025-12-31",
            currency="CNY",
            unit="元",
            statements={},
        )
        hub = _StubHub(response_text="invalid")
        gen = ReportGenerator(hub)

        result = _run(gen.generate(empty_statement, _check_result()))

        assert result.fallback is True
        assert len(result.sections) == 5

    def test_should_work_with_empty_check_result(self) -> None:
        """CheckResult rules / anomalies 全空时应仍能生成（降级路径）。"""
        empty_check = CheckResult(
            rules=[],
            anomalies=[],
            confidence=0.0,
            report_period="2025-12-31",
        )
        hub = _StubHub(response_text="invalid")
        gen = ReportGenerator(hub)

        result = _run(gen.generate(_maotai_statement(), empty_check))

        assert result.fallback is True
        assert len(result.sections) == 5
        # 模板对空规则结果应有占位
        risk = result.sections[3].content
        assert "（无规则结果）" in risk
        assert "（无异常）" in risk

    def test_section_type_order_matches_chinese_name(self) -> None:
        """5 段 section_type 顺序应与 chinese_name 顺序一致。"""
        hub = _StubHub(response_text=_valid_llm_response())
        gen = ReportGenerator(hub)

        result = _run(gen.generate(_maotai_statement(), _check_result()))

        expected = [
            ReportSectionType.COMPANY_OVERVIEW,
            ReportSectionType.FINANCIAL_OVERVIEW,
            ReportSectionType.STATEMENT_ANALYSIS,
            ReportSectionType.ANOMALY_AND_RISK,
            ReportSectionType.CONCLUSION,
        ]
        actual = [s.section_type for s in result.sections]
        assert actual == expected

    def test_section_titles_can_differ_from_default(
        self,
        maotai_statement: FinancialStatement,
        maotai_check: CheckResult,
    ) -> None:
        """LLM 自定义 title 时按位置归一为 5 段固定 type，title 保留。"""
        # LLM 给段 1 起名"贵州茅台公司概况"，仍归一为 COMPANY_OVERVIEW。
        titles = [
            "贵州茅台公司概况",
            "财务概览",
            "三表分析",
            "异常与风险",
            "结论",
        ]
        contents = [f"内容{i}" for i in range(5)]
        raw = json.dumps(
            {
                "sections": [
                    {"title": t, "content": c} for t, c in zip(titles, contents)
                ]
            },
            ensure_ascii=False,
        )
        hub = _StubHub(response_text=raw)
        gen = ReportGenerator(hub)

        result = _run(gen.generate(maotai_statement, maotai_check))

        assert result.success is True
        assert result.sections[0].title == "贵州茅台公司概况"
        assert result.sections[0].section_type == ReportSectionType.COMPANY_OVERVIEW
