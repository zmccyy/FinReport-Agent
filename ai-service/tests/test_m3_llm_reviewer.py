"""M3.02 LLM 复核勾稽测试（spec §2.3 M8 + plan M3.02）。

测试覆盖：

1. **prompt 构建** — 含规则名 / 数值字段 / 三表上下文 / chat 模板
2. **复核成功** — LLM 返回合法 JSON，note 回填 + llm_reviewed=True
3. **JSON 解析降级** — LLM 输出非 JSON，保留原 note + 追加标记
4. **LLM 异常降级** — generate 抛 AiException，保留原 note + 追加标记
5. **触发条件** — INFO / CRITICAL 跳过；WARN / ERROR 才复核
6. **集成用例** — 故意不平衡财报 → RuleEngine 产 ERROR → LLMReviewer 复核
7. **不可变性** — 原 CheckResult 不被修改
8. **is_explained=false** — note 追加"建议人工排查"后缀

异步测试用 ``asyncio.run()`` 包装（对齐 ``test_m2_parse_handler.py`` 风格，
不引入 ``pytest-asyncio`` 依赖）。
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import pytest

from app.core.config import Settings
from app.modules.modelhub.llm_loader import GenerateResult
from app.modules.modelhub.modelhub import ModelHub
from app.modules.reasoner.llm_reviewer import (
    LLMReviewer,
    build_review_prompt,
)
from app.modules.reasoner.rule_engine import RuleEngine
from app.schemas.reasoning import (
    CheckResult,
    RuleResult,
    RuleType,
    Severity,
    StatementSnapshot,
)
from app.schemas.statement import StatementType

# ---------------------------------------------------------------------------
# Stub ModelHub — 对齐 test_m2_extractor 风格
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
        prompt_tokens: int = 12,
        completion_tokens: int = 34,
        latency_ms: float = 100.0,
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


def _snapshot(
    bs: dict[str, Decimal] | None = None,
    is_: dict[str, Decimal] | None = None,
    cf: dict[str, Decimal] | None = None,
    report_period: str = "2025-12-31",
) -> StatementSnapshot:
    """构造 StatementSnapshot 测试辅助。"""
    statements: dict[StatementType, dict[str, Decimal]] = {}
    if bs is not None:
        statements[StatementType.BALANCE_SHEET] = bs
    if is_ is not None:
        statements[StatementType.INCOME_STATEMENT] = is_
    if cf is not None:
        statements[StatementType.CASH_FLOW] = cf
    return StatementSnapshot(
        report_period=report_period,
        currency="CNY",
        unit="元",
        statements=statements,
    )


def _unbalanced_snapshot() -> StatementSnapshot:
    """故意不平衡的财报 — 资产 280亿 ≠ 负债 60亿 + 权益 210亿（差 10亿）。

    用于触发规则 1 的 ERROR 严重度，便于测试 LLM 复核。
    """
    return _snapshot(
        bs={
            "资产总计": Decimal("280000000000.0"),
            "负债合计": Decimal("60000000000.0"),
            "所有者权益合计": Decimal("210000000000.0"),  # 故意少 10亿
        },
        is_={"净利润": Decimal("85000000000.0")},
        cf={"经营活动产生的现金流量净额": Decimal("85000000000.0")},
    )


def _make_rule_result(
    *,
    rule_type: RuleType = RuleType.BALANCE_SHEET_IDENTITY,
    severity: Severity = Severity.ERROR,
    is_pass: bool = False,
    note: str = "资产负债恒等式不成立",
    expected: Decimal | None = Decimal("270000000000.0"),
    actual: Decimal | None = Decimal("280000000000.0"),
    diff: Decimal | None = Decimal("10000000000.0"),
) -> RuleResult:
    """构造单条 RuleResult 测试辅助。"""
    return RuleResult(
        rule_type=rule_type,
        rule_name=rule_type.chinese_name,
        expected=expected,
        actual=actual,
        diff=diff,
        is_pass=is_pass,
        severity=severity,
        note=note,
    )


@pytest.fixture
def unbalanced_check_result() -> CheckResult:
    """构造一个含 ERROR 规则的 CheckResult（直接构造，不走 RuleEngine）。"""
    return CheckResult(
        rules=[
            _make_rule_result(severity=Severity.ERROR),
        ],
        anomalies=[],
        confidence=0.5,
        report_period="2025-12-31",
    )


# ---------------------------------------------------------------------------
# build_review_prompt
# ---------------------------------------------------------------------------


class TestBuildReviewPrompt:
    """prompt 构建测试。"""

    def test_should_include_rule_name_and_values(self) -> None:
        """prompt 应包含规则名、数值字段、chat 模板标记。"""
        rule = _make_rule_result()
        snapshot = _unbalanced_snapshot()

        prompt = build_review_prompt(rule, snapshot)

        assert "资产=负债+所有者权益" in prompt
        assert "balance_sheet_identity" in prompt
        assert "270000000000.00" in prompt  # expected
        assert "280000000000.00" in prompt  # actual
        assert "10000000000.00" in prompt  # diff
        assert "<|im_start|>system" in prompt
        assert "<|im_start|>assistant" in prompt

    def test_should_include_statement_context(self) -> None:
        """prompt 应包含三表科目上下文。"""
        rule = _make_rule_result()
        snapshot = _unbalanced_snapshot()

        prompt = build_review_prompt(rule, snapshot)

        assert "资产负债表" in prompt
        assert "资产总计" in prompt
        assert "利润表" in prompt
        assert "净利润" in prompt
        assert "现金流量表" in prompt

    def test_should_handle_none_values(self) -> None:
        """expected/actual/diff 为 None 时应输出 N/A。"""
        rule = _make_rule_result(
            expected=None,
            actual=None,
            diff=None,
            severity=Severity.CRITICAL,
            note="缺失必需科目",
        )
        snapshot = _snapshot()

        prompt = build_review_prompt(rule, snapshot)

        assert "N/A" in prompt
        assert "缺失必需科目" in prompt

    def test_should_limit_statement_items_to_15(self) -> None:
        """三表上下文每表最多 15 个科目，避免 prompt 过长。"""
        rule = _make_rule_result()
        many_items = {f"科目{i}": Decimal(i) for i in range(30)}
        snapshot = _snapshot(bs=many_items)

        prompt = build_review_prompt(rule, snapshot)

        assert "科目0" in prompt
        assert "科目14" in prompt
        assert "科目15" not in prompt  # 第 16 个不出现


# ---------------------------------------------------------------------------
# LLMReviewer.review — 触发条件
# ---------------------------------------------------------------------------


class TestReviewTriggerConditions:
    """复核触发条件测试。"""

    def test_should_skip_info_rule(self) -> None:
        """INFO 规则（通过）不应触发 LLM 复核。"""
        hub = _StubHub(response_text='{"reason": "不应被调用", "is_explained": true}')
        reviewer = LLMReviewer(hub)
        check = CheckResult(
            rules=[_make_rule_result(severity=Severity.INFO, is_pass=True, note="")],
            confidence=1.0,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        assert len(hub.generate_calls) == 0
        assert result.rules[0].llm_reviewed is False
        assert result.rules[0].note == ""

    def test_should_skip_critical_rule(self) -> None:
        """CRITICAL 规则（科目缺失）不应触发 LLM 复核。"""
        hub = _StubHub(response_text='{"reason": "不应被调用", "is_explained": true}')
        reviewer = LLMReviewer(hub)
        check = CheckResult(
            rules=[
                _make_rule_result(
                    severity=Severity.CRITICAL,
                    note="缺失必需科目: 资产总计",
                    expected=None,
                    actual=None,
                    diff=None,
                )
            ],
            confidence=0.0,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        assert len(hub.generate_calls) == 0
        assert result.rules[0].llm_reviewed is False
        assert "缺失必需科目" in result.rules[0].note

    def test_should_review_warn_rule(self) -> None:
        """WARN 规则应触发 LLM 复核。"""
        hub = _StubHub(response_text='{"reason": "分红未披露", "is_explained": true}')
        reviewer = LLMReviewer(hub)
        check = CheckResult(
            rules=[_make_rule_result(severity=Severity.WARN)],
            confidence=0.5,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        assert len(hub.generate_calls) == 1
        assert result.rules[0].llm_reviewed is True

    def test_should_review_error_rule(self) -> None:
        """ERROR 规则应触发 LLM 复核。"""
        hub = _StubHub(response_text='{"reason": "科目分类错误", "is_explained": true}')
        reviewer = LLMReviewer(hub)
        check = CheckResult(
            rules=[_make_rule_result(severity=Severity.ERROR)],
            confidence=0.5,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        assert len(hub.generate_calls) == 1
        assert result.rules[0].llm_reviewed is True


# ---------------------------------------------------------------------------
# LLMReviewer.review — 成功路径
# ---------------------------------------------------------------------------


class TestReviewSuccess:
    """复核成功路径测试。"""

    def test_should_fill_note_from_llm(self) -> None:
        """LLM 返回合法 JSON，note 应被回填 + llm_reviewed=True。"""
        hub = _StubHub(
            response_text='{"reason": "资产端可能含其他权益重分类", "is_explained": true}'
        )
        reviewer = LLMReviewer(hub)
        check = CheckResult(
            rules=[_make_rule_result(note="资产负债恒等式不成立")],
            confidence=0.5,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        assert result.rules[0].llm_reviewed is True
        assert "[LLM 复核]" in result.rules[0].note
        assert "资产端可能含其他权益重分类" in result.rules[0].note
        # 原 note 应保留
        assert "资产负债恒等式不成立" in result.rules[0].note

    def test_should_append_manual_review_hint_when_not_explained(self) -> None:
        """is_explained=false 时 note 应追加"建议人工排查"。"""
        hub = _StubHub(response_text='{"reason": "无法定位差异来源", "is_explained": false}')
        reviewer = LLMReviewer(hub)
        check = CheckResult(
            rules=[_make_rule_result()],
            confidence=0.5,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        assert result.rules[0].llm_reviewed is True
        assert "建议人工排查" in result.rules[0].note

    def test_should_parse_json_with_code_fence(self) -> None:
        """LLM 输出含 ```json 围栏时应能解析。"""
        hub = _StubHub(response_text='```json\n{"reason": "围栏测试", "is_explained": true}\n```')
        reviewer = LLMReviewer(hub)
        check = CheckResult(
            rules=[_make_rule_result()],
            confidence=0.5,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        assert result.rules[0].llm_reviewed is True
        assert "围栏测试" in result.rules[0].note

    def test_should_pass_generate_kwargs(self) -> None:
        """generate 应收到复核专用参数（max_tokens / temperature / timeout）。"""
        hub = _StubHub(response_text='{"reason": "ok", "is_explained": true}')
        reviewer = LLMReviewer(
            hub,
            max_new_tokens=256,
            temperature=0.2,
            timeout_seconds=30.0,
        )
        check = CheckResult(
            rules=[_make_rule_result()],
            confidence=0.5,
            report_period="2025-12-31",
        )

        _run(reviewer.review(check, _unbalanced_snapshot()))

        call = hub.generate_calls[0]
        assert call["max_new_tokens"] == 256
        assert call["temperature"] == 0.2
        assert call["timeout_seconds"] == 30.0


# ---------------------------------------------------------------------------
# LLMReviewer.review — 降级路径
# ---------------------------------------------------------------------------


class TestReviewFallback:
    """复核降级路径测试。"""

    def test_should_fallback_when_llm_output_not_json(self) -> None:
        """LLM 输出非 JSON 时应保留原 note + 追加降级标记。"""
        hub = _StubHub(response_text="这不是 JSON，是纯文本说明")
        reviewer = LLMReviewer(hub)
        original_note = "资产负债恒等式不成立"
        check = CheckResult(
            rules=[_make_rule_result(note=original_note)],
            confidence=0.5,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        assert result.rules[0].llm_reviewed is False
        assert original_note in result.rules[0].note
        assert "[LLM 输出无法解析为 JSON]" in result.rules[0].note

    def test_should_fallback_when_llm_raises_exception(self) -> None:
        """generate 抛异常时应保留原 note + 追加降级标记。"""
        from app.core.exceptions import AiException

        hub = _StubHub(generate_error=AiException("No LLM loaded"))
        reviewer = LLMReviewer(hub)
        original_note = "资产负债恒等式不成立"
        check = CheckResult(
            rules=[_make_rule_result(note=original_note)],
            confidence=0.5,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        assert result.rules[0].llm_reviewed is False
        assert original_note in result.rules[0].note
        assert "[LLM 复核失败" in result.rules[0].note
        assert "No LLM loaded" in result.rules[0].note

    def test_should_fallback_when_reason_empty(self) -> None:
        """LLM 返回空 reason 时应降级。"""
        hub = _StubHub(response_text='{"reason": "", "is_explained": true}')
        reviewer = LLMReviewer(hub)
        check = CheckResult(
            rules=[_make_rule_result(note="原提示")],
            confidence=0.5,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        assert result.rules[0].llm_reviewed is False
        assert "[LLM 返回空 reason]" in result.rules[0].note
        assert "原提示" in result.rules[0].note

    def test_should_fallback_when_timeout(self) -> None:
        """generate 抛 TimeoutError 时应降级为超时标记。

        注：``asyncio.to_thread`` 本身不会把 ``InferenceTimeoutException``
        转 ``TimeoutError``；这里直接让 stub 抛 ``TimeoutError`` 模拟。
        """
        hub = _StubHub(generate_error=TimeoutError("thread timeout"))
        reviewer = LLMReviewer(hub)
        check = CheckResult(
            rules=[_make_rule_result(note="原提示")],
            confidence=0.5,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        assert result.rules[0].llm_reviewed is False
        assert "[LLM 复核超时]" in result.rules[0].note

    def test_should_not_block_other_rules_on_fallback(self) -> None:
        """单条规则降级不应影响其他规则的复核。"""
        hub = _StubHub(response_text='{"reason": "第二条解释", "is_explained": true}')
        reviewer = LLMReviewer(hub)
        # 第一条规则 generate 时抛异常（用 call_count 控制）
        original_generate = hub.generate

        call_count = {"n": 0}

        def _generate_with_error_on_first(*args: Any, **kwargs: Any) -> GenerateResult:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("first call boom")
            return original_generate(*args, **kwargs)

        hub.generate = _generate_with_error_on_first  # type: ignore[assignment]

        check = CheckResult(
            rules=[
                _make_rule_result(
                    rule_type=RuleType.BALANCE_SHEET_IDENTITY,
                    severity=Severity.ERROR,
                    note="第一条失败",
                ),
                _make_rule_result(
                    rule_type=RuleType.NET_INCOME_TO_RETAINED,
                    severity=Severity.WARN,
                    note="第二条失败",
                ),
            ],
            confidence=0.3,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        # 第一条降级
        assert result.rules[0].llm_reviewed is False
        assert "[LLM 复核失败" in result.rules[0].note
        # 第二条成功
        assert result.rules[1].llm_reviewed is True
        assert "第二条解释" in result.rules[1].note


# ---------------------------------------------------------------------------
# 不可变性 + 集成
# ---------------------------------------------------------------------------


class TestImmutability:
    """原 CheckResult 不应被修改。"""

    def test_should_not_mutate_original_check_result(
        self,
        unbalanced_check_result: CheckResult,
    ) -> None:
        """review 应产出新对象，原 CheckResult 不变。"""
        hub = _StubHub(response_text='{"reason": "新解释", "is_explained": true}')
        reviewer = LLMReviewer(hub)
        original_note = unbalanced_check_result.rules[0].note
        original_reviewed = unbalanced_check_result.rules[0].llm_reviewed

        result = _run(reviewer.review(unbalanced_check_result, _unbalanced_snapshot()))

        # 原对象不变
        assert unbalanced_check_result.rules[0].note == original_note
        assert unbalanced_check_result.rules[0].llm_reviewed is original_reviewed
        # 新对象被修改
        assert result.rules[0].note != original_note
        assert result.rules[0].llm_reviewed is True


class TestIntegrationWithRuleEngine:
    """与 RuleEngine 集成测试 — 故意不平衡财报端到端复核。"""

    def test_should_review_unbalanced_balance_sheet(self) -> None:
        """故意不平衡财报：RuleEngine 产 ERROR → LLMReviewer 复核回填 note。

        验收标准（plan M3.02）：构造一个故意不平衡的财报测试。
        """
        hub = _StubHub(
            response_text=json.dumps(
                {
                    "reason": "所有者权益合计可能漏列其他综合收益或归母/少数股东权益拆分差异",
                    "is_explained": True,
                },
                ensure_ascii=False,
            )
        )
        engine = RuleEngine()
        reviewer = LLMReviewer(hub)

        snapshot = _unbalanced_snapshot()
        check_result = engine.check(snapshot)

        # 规则 1 应失败、severity=ERROR
        rule1 = check_result.rules[0]
        assert rule1.rule_type == RuleType.BALANCE_SHEET_IDENTITY
        assert rule1.is_pass is False
        assert rule1.severity == Severity.ERROR
        assert rule1.llm_reviewed is False

        # LLM 复核
        reviewed = _run(reviewer.review(check_result, snapshot))

        # 规则 1 应被复核
        reviewed_rule1 = reviewed.rules[0]
        assert reviewed_rule1.llm_reviewed is True
        assert "[LLM 复核]" in reviewed_rule1.note
        assert "其他综合收益" in reviewed_rule1.note

        # 其他规则保持原状（规则 2/3 视科目命中情况，可能 CRITICAL/INFO 不复核）
        for rule in reviewed.rules[1:]:
            assert rule.llm_reviewed is False  # 非 WARN/ERROR 不复核

    def test_should_review_only_failed_rules_in_mixed_check(self) -> None:
        """混合 CheckResult：只复核 WARN/ERROR，INFO/CRITICAL 跳过。"""
        hub = _StubHub(response_text='{"reason": "差异解释", "is_explained": true}')
        reviewer = LLMReviewer(hub)
        check = CheckResult(
            rules=[
                _make_rule_result(
                    rule_type=RuleType.BALANCE_SHEET_IDENTITY,
                    severity=Severity.INFO,
                    is_pass=True,
                    note="",
                ),
                _make_rule_result(
                    rule_type=RuleType.NET_INCOME_TO_RETAINED,
                    severity=Severity.WARN,
                    note="规则2失败",
                ),
                _make_rule_result(
                    rule_type=RuleType.CASH_FLOW_VS_NET_INCOME,
                    severity=Severity.CRITICAL,
                    note="缺失科目",
                ),
                _make_rule_result(
                    rule_type=RuleType.BALANCE_SHEET_IDENTITY,
                    severity=Severity.ERROR,
                    note="规则4失败",
                ),
            ],
            confidence=0.25,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        # 只有规则 2 和 4 被复核
        assert len(hub.generate_calls) == 2
        assert result.rules[0].llm_reviewed is False  # INFO
        assert result.rules[1].llm_reviewed is True  # WARN
        assert result.rules[2].llm_reviewed is False  # CRITICAL
        assert result.rules[3].llm_reviewed is True  # ERROR


# ---------------------------------------------------------------------------
# confidence 保持不变
# ---------------------------------------------------------------------------


class TestConfidencePreserved:
    """LLM 复核不应改变 confidence（只解释差异，不改通过/失败状态）。"""

    def test_should_preserve_confidence(self) -> None:
        """复核前后 confidence 应一致。"""
        hub = _StubHub(response_text='{"reason": "解释", "is_explained": true}')
        reviewer = LLMReviewer(hub)
        original_confidence = 0.42
        check = CheckResult(
            rules=[_make_rule_result()],
            confidence=original_confidence,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))

        assert result.confidence == original_confidence


# ---------------------------------------------------------------------------
# to_dict 序列化（确保 llm_reviewed 字段可序列化到 L2）
# ---------------------------------------------------------------------------


class TestSerialization:
    """llm_reviewed 字段应可序列化到 L2 progress payload。"""

    def test_to_dict_should_include_llm_reviewed(self) -> None:
        """CheckResult.to_dict 应包含 llm_reviewed 字段。"""
        hub = _StubHub(response_text='{"reason": "序列化测试", "is_explained": true}')
        reviewer = LLMReviewer(hub)
        check = CheckResult(
            rules=[_make_rule_result()],
            confidence=0.5,
            report_period="2025-12-31",
        )

        result = _run(reviewer.review(check, _unbalanced_snapshot()))
        payload = result.to_dict()
        serialized = json.dumps(payload, ensure_ascii=False)

        assert '"llm_reviewed"' in serialized
        assert "序列化测试" in serialized
