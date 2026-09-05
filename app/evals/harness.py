import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, field_validator

from app.domain.extract import extract_sales_signals
from app.domain.followup_voice import compose_follow_up_draft
from app.domain.humanity import lint_customer_reply
from app.domain.meetings.availability import carve_policy_slots
from app.domain.owner.briefs import DailyBriefSnapshot, format_daily_brief
from app.domain.owner.tasks import OwnerTaskType, classify_owner_task
from app.domain.sales import (
    NextAction,
    ObjectionKind,
    SalesState,
    compute_missing_fields,
    mark_action_delivered,
    select_next_action,
    times_asked,
)
from app.graph.replies import LANG_EN, LANG_HE, reply_for
from app.integrations.calendar import TimeSlot
from app.integrations.research import ResearchSnippet, sanitize_snippets

EVAL_DATASET_VERSION = "sales_v1"
BUYER_DATASET_VERSION = "buyers_v1"
WEBSITE_HANDOFF_DATASET_VERSION = "website_handoff_v1"
WRITING_DATASET_VERSION = "writing_v1"
EXTRACT_DATASET_VERSION = "extract_v1"
ROUTING_DATASET_VERSION = "routing_v1"
OBJECTION_DATASET_VERSION = "objection_v1"
CALENDAR_DATASET_VERSION = "calendar_v1"
SAFETY_DATASET_VERSION = "safety_v1"
GOLD_DATASET_VERSION = "mia_sales_gold"
_DATASETS_DIR = Path(__file__).resolve().parent / "datasets"
_SALES_DATASET_PATH = _DATASETS_DIR / f"{EVAL_DATASET_VERSION}.json"
_BUYER_DATASET_PATH = _DATASETS_DIR / f"{BUYER_DATASET_VERSION}.json"
_WEBSITE_HANDOFF_DATASET_PATH = _DATASETS_DIR / f"{WEBSITE_HANDOFF_DATASET_VERSION}.json"
_WRITING_DATASET_PATH = _DATASETS_DIR / f"{WRITING_DATASET_VERSION}.json"
_EXTRACT_DATASET_PATH = _DATASETS_DIR / f"{EXTRACT_DATASET_VERSION}.json"
_ROUTING_DATASET_PATH = _DATASETS_DIR / f"{ROUTING_DATASET_VERSION}.json"
_OBJECTION_DATASET_PATH = _DATASETS_DIR / f"{OBJECTION_DATASET_VERSION}.json"
_CALENDAR_DATASET_PATH = _DATASETS_DIR / f"{CALENDAR_DATASET_VERSION}.json"
_SAFETY_DATASET_PATH = _DATASETS_DIR / f"{SAFETY_DATASET_VERSION}.json"
_GOLD_DATASET_PATH = _DATASETS_DIR / f"{GOLD_DATASET_VERSION}.jsonl"
_VALID_SAFETY_KINDS = frozenset({"sales", "snippet"})
_WORKFLOW_ASK_SUBSTRING = "יום רגיל בעסק"
_PITCH_ACTIONS = frozenset({"offer_hypothesis", "offer_meeting", "qualify"})
_ROI_FORBIDDEN_SUBSTRINGS = ("%", "ROI", "החזר השקעה", "₪")

SALES_QUALITY_WEIGHTS = {
    "workflow_pain": 25,
    "solution_fit": 20,
    "qualification": 15,
    "buyer_experience": 15,
    "next_step": 10,
    "truth": 10,
    "efficiency": 5,
}

ACTION_COMPONENTS: dict[str, list[str]] = {
    "understand_workflow": ["workflow_pain", "buyer_experience"],
    "deepen_pain": ["workflow_pain", "buyer_experience"],
    "reflect": ["workflow_pain", "buyer_experience"],
    "quantify": ["workflow_pain", "efficiency"],
    "offer_hypothesis": ["solution_fit"],
    "qualify": ["qualification", "next_step", "efficiency"],
    "offer_meeting": ["next_step"],
    "offer_whatsapp": ["next_step", "buyer_experience"],
    "handoff": ["next_step"],
    "stop": ["next_step"],
    "handle_objection": ["buyer_experience", "truth"],
    "disqualify": ["next_step", "truth"],
}

_OWNER_BRIEF_FORBIDDEN = ("absolutely!", "let's dive in")
_APOSTROPHE_FOLD = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'"})


class EvalCase(BaseModel):
    case_id: str
    sales: dict
    expected_action: str
    expected_reply_contains: str


class BuyerTurn(BaseModel):
    user: str
    expected_action: str
    expected_reply_contains: str
    expect: dict[str, object] = {}


class BuyerCase(BaseModel):
    case_id: str
    persona: str
    turns: list[BuyerTurn]


class WritingCase(BaseModel):
    case_id: str
    category: str
    language: str
    kind: str
    sales: dict = {}
    user: str = ""
    expected_action: str = ""
    expected_reply_contains: str = ""
    candidate: str = ""
    brief: dict = {}


class ExtractCase(BaseModel):
    case_id: str
    user: str
    sales: dict = {}
    expect: dict

    @field_validator("expect")
    @classmethod
    def expect_must_be_known_sales_fields(cls, value: dict) -> dict:
        if not value:
            raise ValueError("expect must be non-empty")
        known = set(SalesState.model_fields) - {"lead_id"}
        unknown = set(value) - known
        if unknown:
            raise ValueError(f"unknown expect keys: {sorted(unknown)}")
        return value


class ObjectionCase(BaseModel):
    case_id: str
    user: str
    sales: dict = {}
    expected_objection: str | None
    expected_action: str
    expected_reply_contains: str

    @field_validator("user")
    @classmethod
    def user_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("user must be non-empty")
        return value

    @field_validator("expected_action")
    @classmethod
    def expected_action_must_be_next_action(cls, value: str) -> str:
        valid = {action.value for action in NextAction}
        if value not in valid:
            raise ValueError(f"expected_action must be one of {sorted(valid)}")
        return value

    @field_validator("expected_objection")
    @classmethod
    def expected_objection_must_be_kind_or_none(
        cls, value: str | None
    ) -> str | None:
        if value is None:
            return value
        valid = {kind.value for kind in ObjectionKind}
        if value not in valid:
            raise ValueError(f"expected_objection must be one of {sorted(valid)}")
        return value


class CalendarGap(BaseModel):
    start: str
    end: str


class CalendarCase(BaseModel):
    case_id: str
    timezone: str
    now: str
    gaps: list[CalendarGap]
    expected_slot_starts: list[str]

    @field_validator("case_id")
    @classmethod
    def case_id_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("case_id must be non-empty")
        return value

    @field_validator("timezone")
    @classmethod
    def timezone_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("timezone must be non-empty")
        return value

    @field_validator("expected_slot_starts")
    @classmethod
    def expected_slot_starts_must_be_strings(cls, value: list[str]) -> list[str]:
        for item in value:
            if not isinstance(item, str):
                raise ValueError("expected_slot_starts items must be strings")
        return value


class SafetySnippet(BaseModel):
    title: str
    url: str
    excerpt: str = ""


class SafetyCase(BaseModel):
    case_id: str
    kind: str
    user: str = ""
    sales: dict = {}
    expected_action: str = ""
    expected_reply_contains: str = ""
    forbidden: list[str] = []
    expect: dict[str, object] = {}
    snippets: list[SafetySnippet] = []
    expect_kept: int = -1

    @field_validator("kind")
    @classmethod
    def kind_must_be_allowed(cls, value: str) -> str:
        if value not in _VALID_SAFETY_KINDS:
            raise ValueError(f"kind must be one of {sorted(_VALID_SAFETY_KINDS)}")
        return value

    @field_validator("user")
    @classmethod
    def sales_user_must_be_non_empty(cls, value: str, info) -> str:
        kind = info.data.get("kind")
        if kind == "sales" and not value.strip():
            raise ValueError("user must be non-empty for sales cases")
        return value

    @field_validator("expected_action")
    @classmethod
    def sales_expected_action_must_be_next_action(cls, value: str, info) -> str:
        kind = info.data.get("kind")
        if kind != "sales":
            return value
        valid = {action.value for action in NextAction}
        if value not in valid:
            raise ValueError(f"expected_action must be one of {sorted(valid)}")
        return value

    @field_validator("expect")
    @classmethod
    def expect_keys_must_be_known_sales_fields(cls, value: dict) -> dict:
        if not value:
            return value
        known = set(SalesState.model_fields) - {"lead_id"}
        unknown = set(value) - known
        if unknown:
            raise ValueError(f"unknown expect keys: {sorted(unknown)}")
        return value

    @field_validator("expect_kept")
    @classmethod
    def snippet_expect_kept_must_be_non_negative(cls, value: int, info) -> int:
        kind = info.data.get("kind")
        if kind == "snippet" and value < 0:
            raise ValueError("expect_kept must be >= 0 for snippet cases")
        return value

    @field_validator("snippets")
    @classmethod
    def snippet_cases_require_snippets(cls, value: list[SafetySnippet], info) -> list:
        kind = info.data.get("kind")
        if kind == "snippet" and not value:
            raise ValueError("snippets must be non-empty for snippet cases")
        return value


class RouteCase(BaseModel):
    case_id: str
    user: str
    expected_type: str
    expected_clarification: bool

    @field_validator("user")
    @classmethod
    def user_must_be_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("user must be non-empty")
        return value

    @field_validator("expected_type")
    @classmethod
    def expected_type_must_be_owner_task(cls, value: str) -> str:
        valid = {task_type.value for task_type in OwnerTaskType}
        if value not in valid:
            raise ValueError(f"expected_type must be one of {sorted(valid)}")
        return value


class HiddenTruth(BaseModel):
    must_ask_workflow: bool = False
    must_not_pitch: bool = False
    must_not_invent_roi: bool = False


class GoldCase(BaseModel):
    case_id: str
    sales: dict = {}
    user: str | None = None
    expected_action: str
    expected_reply_contains: str
    hidden_truth: HiddenTruth = HiddenTruth()


class CaseResult(BaseModel):
    case_id: str
    passed: bool
    expected_action: str
    actual_action: str
    reply: str


class SalesQualityScore(BaseModel):
    total: float
    components: dict[str, float]
    weights_used: dict[str, float]


class EvalReport(BaseModel):
    dataset_version: str
    passed: int
    failed: int
    results: list[CaseResult]
    quality: SalesQualityScore | None = None


def _fold_owner_text(text: str) -> str:
    return text.translate(_APOSTROPHE_FOLD).lower()


def _owner_brief_forbidden(text: str) -> bool:
    folded = _fold_owner_text(text)
    return any(phrase in folded for phrase in _OWNER_BRIEF_FORBIDDEN)


def score_sales_quality(results: list[CaseResult]) -> SalesQualityScore:
    if not results:
        return SalesQualityScore(total=0.0, components={}, weights_used={})

    component_passed: dict[str, int] = {}
    component_total: dict[str, int] = {}

    for result in results:
        components = ACTION_COMPONENTS.get(result.expected_action, [])
        for component in components:
            component_total[component] = component_total.get(component, 0) + 1
            if result.passed:
                component_passed[component] = component_passed.get(component, 0) + 1

    covered = [name for name in SALES_QUALITY_WEIGHTS if component_total.get(name, 0) > 0]
    if not covered:
        return SalesQualityScore(total=0.0, components={}, weights_used={})

    raw_weight_sum = sum(SALES_QUALITY_WEIGHTS[name] for name in covered)
    weights_used = {
        name: SALES_QUALITY_WEIGHTS[name] / raw_weight_sum * 100 for name in covered
    }
    components = {
        name: component_passed.get(name, 0) / component_total[name] for name in covered
    }
    total = round(sum(components[name] * weights_used[name] for name in covered), 1)

    return SalesQualityScore(
        total=total,
        components=components,
        weights_used=weights_used,
    )


def load_sales_dataset() -> list[EvalCase]:
    raw = json.loads(_SALES_DATASET_PATH.read_text(encoding="utf-8"))
    return [EvalCase.model_validate(item) for item in raw]


def load_buyer_dataset() -> list[BuyerCase]:
    raw = json.loads(_BUYER_DATASET_PATH.read_text(encoding="utf-8"))
    return [BuyerCase.model_validate(item) for item in raw]


def load_website_handoff_dataset() -> list[BuyerCase]:
    raw = json.loads(_WEBSITE_HANDOFF_DATASET_PATH.read_text(encoding="utf-8"))
    return [BuyerCase.model_validate(item) for item in raw]


def load_writing_dataset() -> list[WritingCase]:
    raw = json.loads(_WRITING_DATASET_PATH.read_text(encoding="utf-8"))
    return [WritingCase.model_validate(item) for item in raw]


def load_extract_dataset() -> list[ExtractCase]:
    raw = json.loads(_EXTRACT_DATASET_PATH.read_text(encoding="utf-8"))
    return [ExtractCase.model_validate(item) for item in raw]


def load_routing_dataset() -> list[RouteCase]:
    raw = json.loads(_ROUTING_DATASET_PATH.read_text(encoding="utf-8"))
    return [RouteCase.model_validate(item) for item in raw]


def load_objection_dataset() -> list[ObjectionCase]:
    raw = json.loads(_OBJECTION_DATASET_PATH.read_text(encoding="utf-8"))
    return [ObjectionCase.model_validate(item) for item in raw]


def load_calendar_dataset() -> list[CalendarCase]:
    raw = json.loads(_CALENDAR_DATASET_PATH.read_text(encoding="utf-8"))
    return [CalendarCase.model_validate(item) for item in raw]


def load_safety_dataset() -> list[SafetyCase]:
    raw = json.loads(_SAFETY_DATASET_PATH.read_text(encoding="utf-8"))
    return [SafetyCase.model_validate(item) for item in raw]


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_slot_start_utc(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slot_starts_label(starts: list[str]) -> str:
    return ",".join(starts) if starts else "none"


def load_gold_dataset() -> list[GoldCase]:
    lines = _GOLD_DATASET_PATH.read_text(encoding="utf-8").splitlines()
    return [GoldCase.model_validate(json.loads(line)) for line in lines if line.strip()]


def _with_missing_fields(state: SalesState) -> SalesState:
    return state.model_copy(
        update={"missing_fields": compute_missing_fields(state)}
    )


def _hidden_truth_ok(case: GoldCase, actual_action: str, reply: str) -> bool:
    truth = case.hidden_truth
    if truth.must_ask_workflow:
        if actual_action != "understand_workflow":
            return False
        if _WORKFLOW_ASK_SUBSTRING not in reply:
            return False
    if truth.must_not_pitch:
        if actual_action in _PITCH_ACTIONS and actual_action != case.expected_action:
            return False
    if truth.must_not_invent_roi:
        if any(token in reply for token in _ROI_FORBIDDEN_SUBSTRINGS):
            return False
    return True


def _sales_field_matches(sales: SalesState, key: str, expected: object) -> bool:
    actual = getattr(sales, key)
    if actual is None:
        return expected is None
    if hasattr(actual, "value"):
        return actual.value == expected
    return actual == expected


def run_sales_eval(cases: list[EvalCase] | None = None) -> EvalReport:
    """Pure function. select_next_action + reply_for. No DB, no ports, no file writes."""
    eval_cases = cases if cases is not None else load_sales_dataset()
    results: list[CaseResult] = []
    passed = 0
    failed = 0

    for case in eval_cases:
        state = SalesState(lead_id=case.case_id, **case.sales)
        action = select_next_action(state)
        reply = reply_for("website", action, sales=state)
        action_ok = action.value == case.expected_action
        reply_ok = case.expected_reply_contains in reply
        lint_ok = lint_customer_reply(reply).ok
        case_passed = action_ok and reply_ok and lint_ok

        if case_passed:
            passed += 1
        else:
            failed += 1

        results.append(
            CaseResult(
                case_id=case.case_id,
                passed=case_passed,
                expected_action=case.expected_action,
                actual_action=action.value,
                reply=reply,
            )
        )

    return EvalReport(
        dataset_version=EVAL_DATASET_VERSION,
        passed=passed,
        failed=failed,
        results=results,
        quality=score_sales_quality(results),
    )


def run_gold_eval(cases: list[GoldCase] | None = None) -> EvalReport:
    """Gold replay: hidden-truth + NBA + reply substring + humanity lint. No DB, no ports."""
    eval_cases = cases if cases is not None else load_gold_dataset()
    results: list[CaseResult] = []
    passed = 0
    failed = 0

    for case in eval_cases:
        state = _with_missing_fields(
            SalesState(lead_id=case.case_id, **case.sales)
        )
        if case.user:
            state = extract_sales_signals(state, case.user)
            state = _with_missing_fields(state)
        action = select_next_action(state)
        reply = reply_for("website", action, sales=state)

        action_ok = action.value == case.expected_action
        reply_ok = case.expected_reply_contains in reply
        lint_ok = lint_customer_reply(reply).ok
        truth_ok = _hidden_truth_ok(case, action.value, reply)
        case_passed = action_ok and reply_ok and lint_ok and truth_ok

        if case_passed:
            passed += 1
        else:
            failed += 1

        results.append(
            CaseResult(
                case_id=case.case_id,
                passed=case_passed,
                expected_action=case.expected_action,
                actual_action=action.value,
                reply=reply,
            )
        )

    return EvalReport(
        dataset_version=GOLD_DATASET_VERSION,
        passed=passed,
        failed=failed,
        results=results,
        quality=score_sales_quality(results),
    )


def run_buyer_eval(cases: list[BuyerCase] | None = None) -> EvalReport:
    """Multi-turn extract → NBA → mark → reply replay. No DB, no ports, no file writes."""
    eval_cases = cases if cases is not None else load_buyer_dataset()
    results: list[CaseResult] = []
    passed = 0
    failed = 0

    for case in eval_cases:
        sales = SalesState(lead_id=case.case_id)
        sent: list[str] = []
        for turn_index, turn in enumerate(case.turns):
            sales = extract_sales_signals(sales, turn.user)
            action = select_next_action(sales)
            # Mirror the orchestrator: a rung asked before gets its second phrasing.
            repeat_ask = times_asked(sales, action) > 0
            sales = mark_action_delivered(sales, action)
            reply = reply_for("website", action, sales=sales, repeat_ask=repeat_ask)

            action_ok = action.value == turn.expected_action
            reply_ok = turn.expected_reply_contains in reply
            lint_ok = lint_customer_reply(reply).ok
            expect_ok = all(
                _sales_field_matches(sales, key, expected)
                for key, expected in turn.expect.items()
            )
            not_repeated = reply not in sent
            sent.append(reply)
            turn_passed = (
                action_ok and reply_ok and expect_ok and lint_ok and not_repeated
            )

            if turn_passed:
                passed += 1
            else:
                failed += 1

            results.append(
                CaseResult(
                    case_id=f"{case.case_id}:{turn_index}",
                    passed=turn_passed,
                    expected_action=turn.expected_action,
                    actual_action=action.value,
                    reply=reply,
                )
            )

    return EvalReport(
        dataset_version=BUYER_DATASET_VERSION,
        passed=passed,
        failed=failed,
        results=results,
        quality=score_sales_quality(results),
    )


def run_website_handoff_eval(cases: list[BuyerCase] | None = None) -> EvalReport:
    """Website multi-turn extract → NBA(channel=website) → mark → reply.

    Proves progressive discovery without looping the opening question, and a
    timely WhatsApp offer. No DB, no ports, no file writes.
    """
    eval_cases = cases if cases is not None else load_website_handoff_dataset()
    results: list[CaseResult] = []
    passed = 0
    failed = 0

    for case in eval_cases:
        sales = SalesState(lead_id=case.case_id)
        sent: list[str] = []
        for turn_index, turn in enumerate(case.turns):
            sales = extract_sales_signals(sales, turn.user)
            action = select_next_action(sales, channel="website")
            repeat_ask = times_asked(sales, action) > 0
            sales = mark_action_delivered(sales, action)
            reply = reply_for("website", action, sales=sales, repeat_ask=repeat_ask)

            action_ok = action.value == turn.expected_action
            reply_ok = turn.expected_reply_contains in reply
            lint_ok = lint_customer_reply(reply).ok
            expect_ok = all(
                _sales_field_matches(sales, key, expected)
                for key, expected in turn.expect.items()
            )
            no_loop = True
            if turn.expected_action != NextAction.UNDERSTAND_WORKFLOW.value:
                no_loop = _WORKFLOW_ASK_SUBSTRING not in reply
            not_repeated = reply not in sent
            sent.append(reply)
            turn_passed = (
                action_ok
                and reply_ok
                and expect_ok
                and lint_ok
                and no_loop
                and not_repeated
            )

            if turn_passed:
                passed += 1
            else:
                failed += 1

            results.append(
                CaseResult(
                    case_id=f"{case.case_id}:{turn_index}",
                    passed=turn_passed,
                    expected_action=turn.expected_action,
                    actual_action=action.value,
                    reply=reply,
                )
            )

    return EvalReport(
        dataset_version=WEBSITE_HANDOFF_DATASET_VERSION,
        passed=passed,
        failed=failed,
        results=results,
        quality=None,
    )


def _writing_language(case: WritingCase) -> str:
    """English cases must be scored against the English copy, not a translation."""
    return LANG_EN if case.language == LANG_EN else LANG_HE


def _run_writing_sales(case: WritingCase) -> tuple[str, str, bool]:
    state = SalesState(lead_id=case.case_id, **case.sales)
    action = select_next_action(state)
    reply = reply_for(
        "website", action, sales=state, language=_writing_language(case)
    )
    action_ok = action.value == case.expected_action
    reply_ok = case.expected_reply_contains in reply
    lint_ok = lint_customer_reply(reply).ok
    return action.value, reply, action_ok and reply_ok and lint_ok


def _run_writing_buyer(case: WritingCase) -> tuple[str, str, bool]:
    sales = SalesState(lead_id=case.case_id)
    sales = extract_sales_signals(sales, case.user)
    action = select_next_action(sales)
    reply = reply_for(
        "website", action, sales=sales, language=_writing_language(case)
    )
    action_ok = action.value == case.expected_action
    reply_ok = case.expected_reply_contains in reply
    lint_ok = lint_customer_reply(reply).ok
    return action.value, reply, action_ok and reply_ok and lint_ok


def _run_writing_follow_up(case: WritingCase) -> tuple[str, str, bool]:
    reply = compose_follow_up_draft(reason="meeting_offered")
    reply_ok = case.expected_reply_contains in reply
    lint_ok = lint_customer_reply(reply).ok
    return case.expected_action, reply, reply_ok and lint_ok


def _run_writing_owner_brief(case: WritingCase) -> tuple[str, str, bool]:
    snapshot = DailyBriefSnapshot.model_validate(case.brief)
    reply = format_daily_brief(snapshot)
    reply_ok = case.expected_reply_contains in reply
    style_ok = not _owner_brief_forbidden(reply)
    return case.expected_action, reply, reply_ok and style_ok


def _run_writing_lint_fail(case: WritingCase) -> tuple[str, str, bool]:
    lint_ok = lint_customer_reply(case.candidate).ok
    return case.expected_action, case.candidate, not lint_ok


def run_extract_eval(cases: list[ExtractCase] | None = None) -> EvalReport:
    """Isolated extract replay via extract_sales_signals. No DB, no ports, no file writes."""
    eval_cases = cases if cases is not None else load_extract_dataset()
    results: list[CaseResult] = []
    passed = 0
    failed = 0

    for case in eval_cases:
        state = SalesState(lead_id=case.case_id, **case.sales)
        updated = extract_sales_signals(state, case.user)
        expect_ok = all(
            _sales_field_matches(updated, key, expected)
            for key, expected in case.expect.items()
        )
        case_passed = expect_ok

        if case_passed:
            passed += 1
        else:
            failed += 1

        results.append(
            CaseResult(
                case_id=case.case_id,
                passed=case_passed,
                expected_action="extract",
                actual_action="extract" if case_passed else "mismatch",
                reply="",
            )
        )

    return EvalReport(
        dataset_version=EXTRACT_DATASET_VERSION,
        passed=passed,
        failed=failed,
        results=results,
        quality=None,
    )


def run_objection_eval(cases: list[ObjectionCase] | None = None) -> EvalReport:
    """Extract → NBA → reply replay for objection bucket. No DB, ports, or file writes."""
    eval_cases = cases if cases is not None else load_objection_dataset()
    results: list[CaseResult] = []
    passed = 0
    failed = 0

    for case in eval_cases:
        state = SalesState(lead_id=case.case_id, **case.sales)
        updated = extract_sales_signals(state, case.user)
        actual_objection = (
            updated.active_objection.value if updated.active_objection else None
        )
        action = select_next_action(updated)
        reply = reply_for("website", action, sales=updated)

        objection_ok = actual_objection == case.expected_objection
        action_ok = action.value == case.expected_action
        reply_ok = case.expected_reply_contains in reply
        lint_ok = lint_customer_reply(reply).ok
        case_passed = objection_ok and action_ok and reply_ok and lint_ok

        if case_passed:
            passed += 1
        else:
            failed += 1

        results.append(
            CaseResult(
                case_id=case.case_id,
                passed=case_passed,
                expected_action=case.expected_action,
                actual_action=action.value,
                reply=reply,
            )
        )

    return EvalReport(
        dataset_version=OBJECTION_DATASET_VERSION,
        passed=passed,
        failed=failed,
        results=results,
        quality=None,
    )


def run_calendar_eval(cases: list[CalendarCase] | None = None) -> EvalReport:
    """Isolated ADR-012 carve_policy_slots replay. No DB, ports, or file writes."""
    eval_cases = cases if cases is not None else load_calendar_dataset()
    results: list[CaseResult] = []
    passed = 0
    failed = 0

    for case in eval_cases:
        gaps = [
            TimeSlot(start=_parse_iso(gap.start), end=_parse_iso(gap.end))
            for gap in case.gaps
        ]
        parsed_now = _parse_iso(case.now)
        slots = carve_policy_slots(gaps, timezone=case.timezone, now=parsed_now)
        actual_starts = [_format_slot_start_utc(slot.start) for slot in slots]
        expected_starts = case.expected_slot_starts
        case_passed = actual_starts == expected_starts

        if case_passed:
            passed += 1
        else:
            failed += 1

        results.append(
            CaseResult(
                case_id=case.case_id,
                passed=case_passed,
                expected_action=_slot_starts_label(expected_starts),
                actual_action=_slot_starts_label(actual_starts),
                reply="",
            )
        )

    return EvalReport(
        dataset_version=CALENDAR_DATASET_VERSION,
        passed=passed,
        failed=failed,
        results=results,
        quality=None,
    )


def _reply_has_forbidden(reply: str, forbidden: list[str]) -> bool:
    lowered = reply.lower()
    return any(token.lower() in lowered for token in forbidden)


def _snippet_text_clean(snippet: ResearchSnippet) -> bool:
    for field in (snippet.title, snippet.excerpt):
        if any(char in field for char in ("\n", "\r", "\t")):
            return False
    return True


def _run_safety_sales(case: SafetyCase) -> tuple[str, str, bool]:
    state = SalesState(lead_id=case.case_id, **case.sales)
    updated = extract_sales_signals(state, case.user)
    expect_ok = all(
        _sales_field_matches(updated, key, expected)
        for key, expected in case.expect.items()
    )
    action = select_next_action(updated)
    reply = reply_for("website", action, sales=updated)
    action_ok = action.value == case.expected_action
    reply_ok = case.expected_reply_contains in reply
    lint_ok = lint_customer_reply(reply).ok
    forbidden_ok = not _reply_has_forbidden(reply, case.forbidden)
    case_passed = action_ok and reply_ok and lint_ok and expect_ok and forbidden_ok
    return action.value, reply, case_passed


def _run_safety_snippet(case: SafetyCase) -> tuple[str, str, bool]:
    raw = [
        ResearchSnippet(title=item.title, url=item.url, excerpt=item.excerpt)
        for item in case.snippets
    ]
    cleaned = sanitize_snippets(raw)
    count_ok = len(cleaned) == case.expect_kept
    text_ok = all(_snippet_text_clean(snippet) for snippet in cleaned)
    case_passed = count_ok and text_ok
    return f"kept={case.expect_kept}", f"kept={len(cleaned)}", case_passed


def run_safety_eval(cases: list[SafetyCase] | None = None) -> EvalReport:
    """Safety replay: sales extract→NBA→reply or snippet sanitizer. No DB or ports."""
    eval_cases = cases if cases is not None else load_safety_dataset()
    results: list[CaseResult] = []
    passed = 0
    failed = 0

    for case in eval_cases:
        if case.kind == "sales":
            actual_action, reply, case_passed = _run_safety_sales(case)
            expected_action = case.expected_action
        else:
            expected_action, actual_action, case_passed = _run_safety_snippet(case)
            reply = actual_action

        if case_passed:
            passed += 1
        else:
            failed += 1

        results.append(
            CaseResult(
                case_id=case.case_id,
                passed=case_passed,
                expected_action=expected_action,
                actual_action=actual_action,
                reply=reply,
            )
        )

    return EvalReport(
        dataset_version=SAFETY_DATASET_VERSION,
        passed=passed,
        failed=failed,
        results=results,
        quality=None,
    )


def run_routing_eval(cases: list[RouteCase] | None = None) -> EvalReport:
    """Isolated owner classify via classify_owner_task. No DB, ports, or file writes."""
    eval_cases = cases if cases is not None else load_routing_dataset()
    results: list[CaseResult] = []
    passed = 0
    failed = 0

    for case in eval_cases:
        decision = classify_owner_task(case.user)
        type_ok = decision.task_type.value == case.expected_type
        clarification_ok = decision.needs_clarification == case.expected_clarification
        case_passed = type_ok and clarification_ok

        if case_passed:
            passed += 1
        else:
            failed += 1

        results.append(
            CaseResult(
                case_id=case.case_id,
                passed=case_passed,
                expected_action=case.expected_type,
                actual_action=decision.task_type.value,
                reply="",
            )
        )

    return EvalReport(
        dataset_version=ROUTING_DATASET_VERSION,
        passed=passed,
        failed=failed,
        results=results,
        quality=None,
    )


def run_writing_eval(cases: list[WritingCase] | None = None) -> EvalReport:
    """Local writing suite: canned paths + anti-pattern lint fails. No DB, no ports."""
    eval_cases = cases if cases is not None else load_writing_dataset()
    results: list[CaseResult] = []
    passed = 0
    failed = 0
    runners = {
        "sales": _run_writing_sales,
        "buyer": _run_writing_buyer,
        "follow_up": _run_writing_follow_up,
        "owner_brief": _run_writing_owner_brief,
        "lint_fail": _run_writing_lint_fail,
    }

    for case in eval_cases:
        runner = runners.get(case.kind)
        if runner is None:
            actual_action = "unknown"
            reply = ""
            case_passed = False
        else:
            actual_action, reply, case_passed = runner(case)

        if case_passed:
            passed += 1
        else:
            failed += 1

        expected_action = case.expected_action or case.kind
        results.append(
            CaseResult(
                case_id=case.case_id,
                passed=case_passed,
                expected_action=expected_action,
                actual_action=actual_action,
                reply=reply,
            )
        )

    return EvalReport(
        dataset_version=WRITING_DATASET_VERSION,
        passed=passed,
        failed=failed,
        results=results,
        quality=None,
    )
