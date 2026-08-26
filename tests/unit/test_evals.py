import inspect
import re
from pathlib import Path

from app.core.capabilities import CapabilityId, require_alive
from app.evals.harness import (
    BUYER_DATASET_VERSION,
    CALENDAR_DATASET_VERSION,
    EVAL_DATASET_VERSION,
    EXTRACT_DATASET_VERSION,
    OBJECTION_DATASET_VERSION,
    ROUTING_DATASET_VERSION,
    SAFETY_DATASET_VERSION,
    SALES_QUALITY_WEIGHTS,
    WEBSITE_HANDOFF_DATASET_VERSION,
    WRITING_DATASET_VERSION,
    WritingCase,
    load_buyer_dataset,
    load_calendar_dataset,
    load_extract_dataset,
    load_objection_dataset,
    load_routing_dataset,
    load_safety_dataset,
    load_sales_dataset,
    load_website_handoff_dataset,
    load_writing_dataset,
    run_buyer_eval,
    run_calendar_eval,
    run_extract_eval,
    run_objection_eval,
    run_routing_eval,
    run_safety_eval,
    run_sales_eval,
    run_website_handoff_eval,
    run_writing_eval,
    score_sales_quality,
)

_WRITING_CATEGORIES = frozenset({
    "discovery",
    "short_answer",
    "technical",
    "objection",
    "price_question",
    "handoff_offer",
    "booking",
    "complaint",
})


def test_run_sales_eval_all_pass() -> None:
    report = run_sales_eval()
    assert report.dataset_version == "sales_v1"
    assert report.failed == 0
    assert report.passed == len(load_sales_dataset())
    assert all(r.passed for r in report.results)
    assert report.quality is not None
    assert report.quality.total == 100.0


def test_sales_dataset_has_fifty_unique_cases() -> None:
    cases = load_sales_dataset()
    # A floor, not a fixed size. Coverage may grow; it must never shrink.
    assert len(cases) >= 50
    case_ids = {case.case_id for case in cases}
    assert len(case_ids) == len(cases)


def test_run_buyer_eval_all_pass() -> None:
    cases = load_buyer_dataset()
    total_turns = sum(len(case.turns) for case in cases)
    report = run_buyer_eval()
    assert report.dataset_version == "buyers_v1"
    assert report.failed == 0
    assert report.passed == total_turns
    assert all(r.passed for r in report.results)
    assert report.quality is not None
    assert report.quality.total == 100.0
    assert set(report.quality.components) == set(SALES_QUALITY_WEIGHTS)


def test_buyer_eval_covers_required_personas() -> None:
    """Every scenario the sales brief names must exist, and none may be dropped."""
    cases = load_buyer_dataset()
    personas = {case.persona for case in cases}
    assert len(personas) == len(cases)
    case_ids = {case.case_id for case in cases}
    assert len(case_ids) == len(cases)
    required = {
        "shoe_store_inventory_he",
        "clinic_missed_calls_en",
        "ecommerce_support_overload_en",
        "restaurant_reservations_he",
        "real_estate_leads_en",
        "one_word_answers_he",
        "hebrew_slang",
        "mixed_hebrew_english",
        "direct_price_question_he",
        "direct_price_question_en",
        "price_objection_he",
        "technical_skeptical",
        "ai_privacy",
        "comparing_vendors",
        "enterprise_committee",
        "requests_human_he",
        "ready_to_book",
        "student_non_buyer",
        "opts_out_he",
    }
    assert required <= case_ids


def test_buyer_eval_is_mostly_multi_turn() -> None:
    """Single-turn cases prove a function. The brief asks for real conversations."""
    cases = load_buyer_dataset()
    multi_turn = [case for case in cases if len(case.turns) >= 3]
    assert len(multi_turn) >= 8


def test_buyer_eval_never_repeats_a_reply_within_a_conversation() -> None:
    """Defect A guard: the same question twice in one conversation is the loop."""
    report = run_buyer_eval()
    by_case: dict[str, list[str]] = {}
    for result in report.results:
        case_id = result.case_id.split("#", 1)[0]
        by_case.setdefault(case_id, []).append(result.reply)
    for case_id, replies in by_case.items():
        assert len(set(replies)) == len(replies), case_id


def test_mutated_expected_action_fails() -> None:
    cases = load_sales_dataset()
    mutated = cases[0].model_copy(update={"expected_action": "disqualify"})
    report = run_sales_eval(cases=[mutated])
    assert report.failed == 1
    assert report.results[0].passed is False


def test_mutated_buyer_expected_action_fails() -> None:
    cases = load_buyer_dataset()
    mutated_turn = cases[0].turns[0].model_copy(update={"expected_action": "disqualify"})
    mutated = cases[0].model_copy(update={"turns": [mutated_turn] + cases[0].turns[1:]})
    report = run_buyer_eval(cases=[mutated])
    assert report.failed >= 1
    assert report.results[0].passed is False


def test_mutated_buyer_first_turn_lowers_quality_score() -> None:
    cases = load_buyer_dataset()
    mutated_turn = cases[0].turns[0].model_copy(
        update={"expected_reply_contains": "__no_such_reply__"}
    )
    mutated = cases[0].model_copy(update={"turns": [mutated_turn] + cases[0].turns[1:]})
    report = run_buyer_eval(cases=[mutated])
    assert report.quality is not None
    assert report.quality.total < 100.0
    assert report.quality.components["workflow_pain"] < 1.0


def test_sales_quality_weights_sum_to_100() -> None:
    assert sum(SALES_QUALITY_WEIGHTS.values()) == 100


def test_score_sales_quality_empty_results() -> None:
    score = score_sales_quality([])
    assert score.total == 0.0
    assert score.components == {}
    assert score.weights_used == {}


def test_harness_has_no_langsmith_or_llm_judge() -> None:
    module_source = Path("app/evals/harness.py").read_text(encoding="utf-8")
    lowered = module_source.lower()
    assert "langsmith" not in lowered
    assert "llm" not in lowered
    assert "judge" not in lowered


def test_harness_has_no_file_writes() -> None:
    module_source = Path("app/evals/harness.py").read_text(encoding="utf-8")
    assert 'open(' not in module_source
    assert ".write(" not in module_source
    assert "store.save" not in module_source
    assert "write_text" not in module_source


def test_run_sales_eval_source_excludes_learning_and_inbound() -> None:
    source = inspect.getsource(run_sales_eval)
    assert "learning" not in source
    assert "inbound" not in source


def test_run_buyer_eval_source_excludes_learning_and_inbound() -> None:
    source = inspect.getsource(run_buyer_eval)
    assert "learning" not in source
    assert "inbound" not in source


def test_require_alive_graph_lab_and_canonical_events_pass() -> None:
    require_alive(CapabilityId.GRAPH_LAB)
    require_alive(CapabilityId.CANONICAL_EVENTS)


def test_eval_dataset_version_constant() -> None:
    assert EVAL_DATASET_VERSION == "sales_v1"
    assert BUYER_DATASET_VERSION == "buyers_v1"
    assert WRITING_DATASET_VERSION == "writing_v1"
    assert EXTRACT_DATASET_VERSION == "extract_v1"
    assert ROUTING_DATASET_VERSION == "routing_v1"
    assert OBJECTION_DATASET_VERSION == "objection_v1"
    assert CALENDAR_DATASET_VERSION == "calendar_v1"
    assert SAFETY_DATASET_VERSION == "safety_v1"
    assert WEBSITE_HANDOFF_DATASET_VERSION == "website_handoff_v1"


def test_run_website_handoff_eval_all_pass() -> None:
    cases = load_website_handoff_dataset()
    total_turns = sum(len(case.turns) for case in cases)
    report = run_website_handoff_eval()
    assert report.dataset_version == "website_handoff_v1"
    assert report.failed == 0
    assert report.passed == total_turns
    assert all(r.passed for r in report.results)
    assert report.quality is None


def test_website_handoff_dataset_has_shoe_and_prelaunch_personas() -> None:
    cases = load_website_handoff_dataset()
    ids = {case.case_id for case in cases}
    assert {
        "shoe_inventory_sheets",
        "prelaunch_clothing_website",
        "greeting_only_never_offers_whatsapp",
        "wants_human_never_offers_whatsapp",
    } <= ids
    shoe = next(case for case in cases if case.case_id == "shoe_inventory_sheets")
    actions = [turn.expected_action for turn in shoe.turns]
    assert actions[0] == "understand_workflow"
    assert "offer_whatsapp" in actions
    assert "understand_workflow" not in actions[1:]
    prelaunch = next(case for case in cases if case.case_id == "prelaunch_clothing_website")
    assert prelaunch.turns[1].expected_action == "offer_whatsapp"


def test_website_handoff_offers_whatsapp_within_six_turns_when_engaged() -> None:
    """Phase 3: engaged discovery reaches the offer, and it never lands on a greeting."""
    cases = load_website_handoff_dataset()
    engaged = [
        case
        for case in cases
        if any(turn.expected_action == "offer_whatsapp" for turn in case.turns)
    ]
    assert len(engaged) >= 3
    for case in engaged:
        offer_index = next(
            index
            for index, turn in enumerate(case.turns)
            if turn.expected_action == "offer_whatsapp"
        )
        assert 1 <= offer_index <= 5, case.case_id


def test_website_handoff_never_offers_whatsapp_to_a_greeting_or_a_human_request() -> None:
    cases = load_website_handoff_dataset()
    for case_id in (
        "greeting_only_never_offers_whatsapp",
        "wants_human_never_offers_whatsapp",
    ):
        case = next(item for item in cases if item.case_id == case_id)
        actions = [turn.expected_action for turn in case.turns]
        assert "offer_whatsapp" not in actions


def test_mutated_website_handoff_expected_action_fails() -> None:
    cases = load_website_handoff_dataset()
    mutated_turn = cases[0].turns[0].model_copy(update={"expected_action": "offer_whatsapp"})
    mutated = cases[0].model_copy(update={"turns": [mutated_turn]})
    report = run_website_handoff_eval(cases=[mutated])
    assert report.failed == 1
    assert report.results[0].passed is False


def test_run_website_handoff_eval_source_excludes_learning_and_inbound() -> None:
    source = inspect.getsource(run_website_handoff_eval)
    assert "learning" not in source
    assert "inbound" not in source


def test_run_safety_eval_all_pass() -> None:
    report = run_safety_eval()
    assert report.dataset_version == "safety_v1"
    assert report.failed == 0
    assert report.passed == len(load_safety_dataset())
    assert all(r.passed for r in report.results)
    assert report.quality is None


def test_safety_dataset_has_twenty_unique_cases() -> None:
    cases = load_safety_dataset()
    assert len(cases) == 20
    case_ids = {case.case_id for case in cases}
    assert len(case_ids) == 20


def test_safety_dataset_sales_hebrew_mix() -> None:
    cases = load_safety_dataset()
    sales_cases = [case for case in cases if case.kind == "sales"]
    hebrew = sum(
        1 for case in sales_cases if any("\u0590" <= char <= "\u05ff" for char in case.user)
    )
    assert hebrew >= 2


def test_safety_dataset_has_no_pii_markers() -> None:
    cases = load_safety_dataset()
    for case in cases:
        blob = case.user
        if case.kind == "snippet":
            blob = f"{case.case_id} {case.snippets}"
        assert "@" not in blob
        assert "972" not in blob
        assert "+972" not in blob


def test_mutated_safety_expected_action_fails() -> None:
    cases = load_safety_dataset()
    sales_case = next(case for case in cases if case.kind == "sales")
    mutated = sales_case.model_copy(update={"expected_action": "disqualify"})
    report = run_safety_eval(cases=[mutated])
    assert report.failed == 1
    assert report.results[0].passed is False


def test_run_safety_eval_source_excludes_inbound_and_composio() -> None:
    source = inspect.getsource(run_safety_eval)
    assert "inbound" not in source
    assert "composio" not in source


def test_run_calendar_eval_all_pass() -> None:
    report = run_calendar_eval()
    assert report.dataset_version == "calendar_v1"
    assert report.failed == 0
    assert report.passed == len(load_calendar_dataset())
    assert all(r.passed for r in report.results)
    assert report.quality is None


def test_calendar_dataset_has_twenty_unique_cases() -> None:
    cases = load_calendar_dataset()
    assert len(cases) == 20
    case_ids = {case.case_id for case in cases}
    assert len(case_ids) == 20


def test_calendar_sun_2h_three_slots_expects_three_starts() -> None:
    cases = load_calendar_dataset()
    case = next(c for c in cases if c.case_id == "cal_sun_2h_three_slots")
    assert len(case.expected_slot_starts) == 3


def test_mutated_calendar_expected_slot_starts_fails() -> None:
    cases = load_calendar_dataset()
    mutated = cases[0].model_copy(
        update={"expected_slot_starts": ["2099-01-01T00:00:00Z"]}
    )
    report = run_calendar_eval(cases=[mutated])
    assert report.failed == 1
    assert report.results[0].passed is False


def test_run_calendar_eval_source_excludes_inbound_and_composio() -> None:
    source = inspect.getsource(run_calendar_eval)
    assert "inbound" not in source
    assert "composio" not in source


def test_run_objection_eval_all_pass() -> None:
    report = run_objection_eval()
    assert report.dataset_version == "objection_v1"
    assert report.failed == 0
    assert report.passed == len(load_objection_dataset())
    assert all(r.passed for r in report.results)
    assert report.quality is None


def test_objection_dataset_has_twenty_unique_cases() -> None:
    cases = load_objection_dataset()
    assert len(cases) == 20
    case_ids = {case.case_id for case in cases}
    assert len(case_ids) == 20


def test_objection_dataset_hebrew_and_english_mix() -> None:
    cases = load_objection_dataset()
    hebrew = sum(
        1 for case in cases if any("\u0590" <= char <= "\u05ff" for char in case.user)
    )
    english_only = sum(
        1
        for case in cases
        if not any("\u0590" <= char <= "\u05ff" for char in case.user)
    )
    assert hebrew >= 6
    assert english_only >= 6


def test_objection_dataset_has_no_pii_markers() -> None:
    cases = load_objection_dataset()
    for case in cases:
        assert "@" not in case.user
        assert "972" not in case.user
        assert "+972" not in case.user


def test_mutated_objection_expected_objection_fails() -> None:
    cases = load_objection_dataset()
    mutated = cases[0].model_copy(update={"expected_objection": "ai_trust"})
    report = run_objection_eval(cases=[mutated])
    assert report.failed == 1
    assert report.results[0].passed is False


def test_run_objection_eval_source_excludes_inbound() -> None:
    source = inspect.getsource(run_objection_eval)
    assert "inbound" not in source


def test_run_routing_eval_all_pass() -> None:
    report = run_routing_eval()
    assert report.dataset_version == "routing_v1"
    assert report.failed == 0
    assert report.passed == len(load_routing_dataset())
    assert all(r.passed for r in report.results)
    assert report.quality is None


def test_routing_dataset_has_twenty_unique_cases() -> None:
    cases = load_routing_dataset()
    assert len(cases) == 20
    case_ids = {case.case_id for case in cases}
    assert len(case_ids) == 20


def test_routing_dataset_hebrew_and_english_mix() -> None:
    cases = load_routing_dataset()
    hebrew = sum(
        1 for case in cases if any("\u0590" <= char <= "\u05ff" for char in case.user)
    )
    english_only = sum(
        1
        for case in cases
        if not any("\u0590" <= char <= "\u05ff" for char in case.user)
    )
    assert hebrew >= 6
    assert english_only >= 6


def test_routing_dataset_has_no_pii_markers() -> None:
    cases = load_routing_dataset()
    for case in cases:
        assert "@" not in case.user
        assert "972" not in case.user
        assert "+972" not in case.user


def test_mutated_routing_expected_type_fails() -> None:
    cases = load_routing_dataset()
    mutated = cases[0].model_copy(update={"expected_type": "sales"})
    report = run_routing_eval(cases=[mutated])
    assert report.failed == 1
    assert report.results[0].passed is False


def test_run_routing_eval_source_excludes_learning_and_inbound() -> None:
    source = inspect.getsource(run_routing_eval)
    assert "learning" not in source
    assert "inbound" not in source


def test_run_extract_eval_all_pass() -> None:
    report = run_extract_eval()
    assert report.dataset_version == "extract_v1"
    assert report.failed == 0
    assert report.passed == len(load_extract_dataset())
    assert all(r.passed for r in report.results)
    assert report.quality is None


def test_extract_dataset_has_thirty_unique_cases() -> None:
    cases = load_extract_dataset()
    assert len(cases) == 30
    case_ids = {case.case_id for case in cases}
    assert len(case_ids) == 30


def test_extract_dataset_hebrew_and_english_mix() -> None:
    cases = load_extract_dataset()
    hebrew = sum(
        1 for case in cases if any("\u0590" <= char <= "\u05ff" for char in case.user)
    )
    english_only = sum(
        1
        for case in cases
        if not any("\u0590" <= char <= "\u05ff" for char in case.user)
    )
    assert hebrew >= 12
    assert english_only >= 12


def test_extract_dataset_has_no_pii_markers() -> None:
    cases = load_extract_dataset()
    for case in cases:
        assert "@" not in case.user
        assert "972" not in case.user
        assert "+972" not in case.user


def test_mutated_extract_expect_fails() -> None:
    cases = load_extract_dataset()
    mutated = cases[0].model_copy(
        update={"expect": {**cases[0].expect, "workflow_known": False}}
    )
    report = run_extract_eval(cases=[mutated])
    assert report.failed == 1
    assert report.results[0].passed is False


def test_run_extract_eval_source_excludes_learning_and_inbound() -> None:
    source = inspect.getsource(run_extract_eval)
    assert "learning" not in source
    assert "inbound" not in source


def test_run_writing_eval_all_pass() -> None:
    report = run_writing_eval()
    assert report.dataset_version == "writing_v1"
    assert report.failed == 0
    assert report.passed == len(load_writing_dataset())
    assert all(r.passed for r in report.results)
    assert report.quality is None


def test_writing_eval_covers_eight_categories_he_and_en() -> None:
    """Owner-facing generators are Hebrew-only, so they are scored in Hebrew alone."""
    cases = load_writing_dataset()
    hebrew_only = {"follow_up", "owner_report"}
    by_category: dict[str, set[str]] = {}
    for case in cases:
        if case.category == "anti_pattern":
            continue
        by_category.setdefault(case.category, set()).add(case.language)
    assert _WRITING_CATEGORIES <= set(by_category)
    for category in _WRITING_CATEGORIES:
        assert "he" in by_category[category]
        assert "en" in by_category[category]
    for category in hebrew_only:
        assert by_category.get(category) == {"he"}


def test_writing_english_cases_are_scored_against_english_copy() -> None:
    """A Hebrew expectation on an English case means English was never graded."""
    hebrew = re.compile(r"[\u0590-\u05FF]")
    for case in load_writing_dataset():
        if case.language != "en" or case.kind not in {"buyer", "sales"}:
            continue
        assert not hebrew.search(case.expected_reply_contains), case.case_id


def test_mutated_writing_case_fails() -> None:
    cases = load_writing_dataset()
    mutated = cases[0].model_copy(update={"expected_reply_contains": "__no_such_reply__"})
    report = run_writing_eval(cases=[mutated])
    assert report.failed == 1
    assert report.results[0].passed is False


def test_lint_failure_fails_writing_case() -> None:
    bad_lint_fail = WritingCase(
        case_id="lint_fail_should_be_dirty",
        category="anti_pattern",
        language="n/a",
        kind="lint_fail",
        expected_action="lint_fail",
        candidate="ספר לי קצת",
    )
    report = run_writing_eval(cases=[bad_lint_fail])
    assert report.failed == 1
    assert report.results[0].passed is False
