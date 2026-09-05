"""The predeploy suite's own logic, proven without a single model call.

Everything here is deterministic on purpose. The scenarios themselves need real keys and
cost money; the scoring, the gate, the seal and the report shape do not, and those are
exactly the parts that must not be allowed to rot. If this file ever needs a network
connection to pass, the suite has stopped being opt-in.
"""

from __future__ import annotations

import json

import pytest
from app.core.config import get_settings
from app.evals.predeploy.checks import contact_details_in, present, unexpected_numbers
from app.evals.predeploy.gate import PREDEPLOY_FLAG, gate_status
from app.evals.predeploy.report import (
    EXIT_BLOCKED,
    EXIT_GREEN,
    EXIT_SKIPPED,
    GATE_BLOCKED,
    GATE_GREEN,
    GATE_SKIPPED,
    HARD_SAFETY_RUNS,
    AttemptResult,
    ScenarioResult,
    build_report,
    exit_code,
    human_summary,
    pass_at_1_rate,
    pass_hat_3_rate,
)
from app.evals.predeploy.runner import run_suite
from app.evals.predeploy.sandbox import (
    SEALED_ENV,
    SealBroken,
    assert_sealed,
    sealed_settings,
)
from app.evals.predeploy.scenarios import (
    hard_safety_ids,
    owner_scenarios,
    scenario_ids,
    website_scenarios,
)

LIVE_ENV = {
    PREDEPLOY_FLAG: "1",
    "MIA_OPENAI_API_KEY": "sk-not-a-real-key",
    "MIA_SALES_MODEL": "some-sales-model",
    "MIA_OWNER_AGENT_MODEL": "some-owner-model",
}


def _attempts(scenario_id: str, outcomes: list[bool]) -> tuple[AttemptResult, ...]:
    return tuple(
        AttemptResult(
            scenario_id=scenario_id,
            attempt=index,
            passed=passed,
            failures=() if passed else ("synthetic failure",),
            tool_path=("answer",),
            latency_ms=100 * index,
            tokens_in=10,
            tokens_out=5,
        )
        for index, passed in enumerate(outcomes, start=1)
    )


def _scenario(
    scenario_id: str,
    outcomes: list[bool],
    *,
    hard_safety: bool = False,
    surface: str = "website",
) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=scenario_id,
        surface=surface,
        hard_safety=hard_safety,
        attempts=_attempts(scenario_id, outcomes),
    )


# ------------------------------------------------------------------ opt-in gate


def test_gate_is_closed_with_an_empty_environment() -> None:
    status = gate_status({})
    assert status.enabled() is False
    assert any(PREDEPLOY_FLAG in reason for reason in status.reasons)


def test_gate_is_closed_when_the_flag_is_set_but_no_model_is_configured() -> None:
    status = gate_status({PREDEPLOY_FLAG: "1"})
    assert status.opted_in is True
    assert status.enabled() is False
    assert status.site_ready is False
    assert status.owner_ready is False


def test_gate_is_closed_when_models_exist_but_nobody_opted_in() -> None:
    env = dict(LIVE_ENV)
    env.pop(PREDEPLOY_FLAG)
    status = gate_status(env)
    assert status.site_ready is True
    assert status.owner_ready is True
    assert status.enabled() is False


def test_gate_opens_with_the_flag_and_openai_credentials() -> None:
    assert gate_status(LIVE_ENV).enabled() is True


def test_gate_opens_on_a_gemini_only_configuration() -> None:
    status = gate_status(
        {
            PREDEPLOY_FLAG: "yes",
            "MIA_GEMINI_API_KEY": "g-key",
            "MIA_SALES_GEMINI_MODEL": "gemini-sales",
            "MIA_OWNER_AGENT_GEMINI_MODEL": "gemini-owner",
        }
    )
    assert status.enabled() is True


def test_gate_needs_both_surfaces_before_it_reports_ready() -> None:
    # An OpenAI key with only a sales model still reaches the owner agent, because the
    # owner chain falls back to the sales models. Dropping the key closes both.
    status = gate_status({PREDEPLOY_FLAG: "1", "MIA_SALES_MODEL": "m"})
    assert status.site_ready is False
    assert status.owner_ready is False
    assert status.enabled() is False


# ------------------------------------------------------- Pass@1, Pass^3, the 3/3 rule


def test_pass_at_1_reads_only_the_first_attempt() -> None:
    results = [
        _scenario("a", [True, False, False]),
        _scenario("b", [False, True, True]),
    ]
    assert pass_at_1_rate(results) == 0.5
    assert results[0].pass_at_1() is True
    assert results[1].pass_at_1() is False


def test_pass_hat_3_scores_only_scenarios_that_ran_three_times() -> None:
    results = [
        _scenario("three_clean", [True, True, True]),
        _scenario("three_flaky", [True, True, False]),
        _scenario("single_run", [True]),
    ]
    assert results[0].pass_hat_3() is True
    assert results[1].pass_hat_3() is False
    assert results[2].pass_hat_3() is None
    # The single-run scenario is excluded, not counted as a miss.
    assert pass_hat_3_rate(results) == 0.5


def test_hard_safety_needs_three_of_three() -> None:
    flaky = _scenario("site_abuse", [True, True, False], hard_safety=True)
    assert flaky.pass_at_1() is True
    assert flaky.gate_passed() is False
    clean = _scenario("site_abuse", [True, True, True], hard_safety=True)
    assert clean.gate_passed() is True


def test_hard_safety_run_once_does_not_pass_the_gate() -> None:
    once = _scenario("site_legal", [True], hard_safety=True)
    assert once.pass_at_1() is True
    assert once.runs() < HARD_SAFETY_RUNS
    assert once.gate_passed() is False


def test_ordinary_scenario_is_judged_on_its_first_attempt() -> None:
    assert _scenario("site_greeting", [True]).gate_passed() is True
    assert _scenario("site_greeting", [False]).gate_passed() is False


# ---------------------------------------------------------------- report shape


def test_report_shape_is_json_serializable_and_carries_the_agreed_keys() -> None:
    report = build_report(
        [
            _scenario("site_greeting", [True]),
            _scenario("site_abuse", [True, True, True], hard_safety=True),
            _scenario("owner_free_conversation", [True], surface="owner"),
        ]
    )
    for key in (
        "suite",
        "version",
        "generated_at",
        "opted_in",
        "gate",
        "skipped_reasons",
        "suite_failures",
        "totals",
        "scenarios",
        "failures",
    ):
        assert key in report
    totals = report["totals"]
    for key in (
        "scenarios",
        "website",
        "owner",
        "attempts",
        "hard_safety_scenarios",
        "hard_safety_passed",
        "pass_at_1",
        "pass_hat_3",
        "blocked_scenarios",
        "tokens_in",
        "tokens_out",
        "latency_ms_p50",
        "latency_ms_p95",
        "latency_ms_max",
    ):
        assert key in totals
    assert totals["scenarios"] == 3
    assert totals["website"] == 2
    assert totals["owner"] == 1
    assert totals["attempts"] == 5
    assert totals["hard_safety_scenarios"] == 1
    assert totals["hard_safety_passed"] == 1
    assert report["gate"] == GATE_GREEN
    assert exit_code(report) == EXIT_GREEN
    # The report is written to disk as JSON; it must survive the round trip.
    assert json.loads(json.dumps(report, ensure_ascii=False))["suite"] == report["suite"]


def test_a_failed_hard_safety_scenario_blocks_the_gate_and_names_itself() -> None:
    report = build_report(
        [
            _scenario("site_greeting", [True]),
            _scenario("site_unknown_price", [True, False, True], hard_safety=True),
        ]
    )
    assert report["gate"] == GATE_BLOCKED
    assert exit_code(report) == EXIT_BLOCKED
    assert [entry["scenario_id"] for entry in report["failures"]] == ["site_unknown_price"]
    assert report["failures"][0]["hard_safety"] is True
    assert "site_unknown_price" in human_summary(report)


def test_a_suite_level_failure_blocks_even_when_every_scenario_passed() -> None:
    report = build_report(
        [_scenario("site_greeting", [True])],
        suite_failures=["no website turn billed a single token"],
    )
    assert report["gate"] == GATE_BLOCKED
    assert exit_code(report) == EXIT_BLOCKED
    assert "SUITE" in human_summary(report)


def test_a_skipped_report_never_looks_like_a_pass() -> None:
    report = build_report((), reasons=["MIA_PREDEPLOY_EVAL is not set to 1"])
    assert report["gate"] == GATE_SKIPPED
    assert report["opted_in"] is False
    assert exit_code(report) == EXIT_SKIPPED
    summary = human_summary(report)
    assert "proved nothing" in summary
    assert "MIA_PREDEPLOY_EVAL" in summary


# ---------------------------------------------------- skip behaviour of the runner


def test_run_suite_skips_and_explains_when_nobody_opted_in() -> None:
    report = run_suite(env={})
    assert report["gate"] == GATE_SKIPPED
    assert report["scenarios"] == []
    assert report["skipped_reasons"]
    assert exit_code(report) == EXIT_SKIPPED


def test_run_suite_skips_when_the_flag_is_set_but_keys_are_missing() -> None:
    report = run_suite(env={PREDEPLOY_FLAG: "1"})
    assert report["gate"] == GATE_SKIPPED
    assert any("model" in reason.lower() for reason in report["skipped_reasons"])


# ------------------------------------------------------------------ the seal


def test_sealed_settings_keeps_model_config_and_strips_every_integration_credential() -> None:
    base = get_settings().model_copy(
        update={
            "openai_api_key": "sk-model-key",
            "sales_model": "sales-model",
            "owner_agent_model": "owner-model",
            "composio_api_key": "live-composio",
            "composio_user_id": "live-user",
            "telegram_bot_token": "live-telegram",
            "whatsapp_access_token": "live-whatsapp",
            "gmail_send": True,
            "calendar_write": True,
            "database_url": "postgresql://live/mia",
        }
    )
    sealed = sealed_settings(base)
    assert sealed.openai_api_key == "sk-model-key"
    assert sealed.sales_model == "sales-model"
    assert sealed.owner_agent_model == "owner-model"
    assert sealed.composio_api_key == ""
    assert sealed.composio_user_id == ""
    assert sealed.telegram_bot_token == ""
    assert sealed.whatsapp_access_token == ""
    assert sealed.gmail_send is False
    assert sealed.calendar_write is False
    assert ":memory:" in sealed.database_url
    assert_sealed(sealed)


def test_assert_sealed_refuses_a_configuration_that_can_reach_production() -> None:
    live = sealed_settings(get_settings()).model_copy(
        update={"composio_api_key": "live", "composio_user_id": "live"}
    )
    with pytest.raises(SealBroken):
        assert_sealed(live)


def test_assert_sealed_refuses_a_real_database() -> None:
    live = sealed_settings(get_settings()).model_copy(
        update={"database_url": "postgresql://live/mia"}
    )
    with pytest.raises(SealBroken):
        assert_sealed(live)


def test_sealed_env_pins_the_process_to_a_throwaway_database() -> None:
    # Constant only: applying it is the runner's job, and this test must stay side-effect
    # free so it cannot disturb the rest of the suite.
    assert ":memory:" in SEALED_ENV["MIA_DATABASE_URL"]
    assert SEALED_ENV["MIA_ENV"] == "test"
    assert SEALED_ENV["MIA_COMPOSIO_API_KEY"] == ""
    assert SEALED_ENV["MIA_TELEGRAM_BOT_TOKEN"] == ""
    assert SEALED_ENV["MIA_GMAIL_SEND"] == "false"
    assert SEALED_ENV["MIA_CALENDAR_WRITE"] == "false"


# ------------------------------------------------------------- scenario inventory


def test_the_suite_covers_seventeen_website_and_ten_owner_scenarios() -> None:
    assert len(website_scenarios()) == 17
    assert len(owner_scenarios()) == 10
    ids = scenario_ids()
    assert len(ids) == 27
    assert len(set(ids)) == 27


def test_every_scenario_explains_why_it_exists() -> None:
    for scenario in (*website_scenarios(), *owner_scenarios()):
        assert scenario.why.strip(), f"{scenario.scenario_id} has no stated purpose"


def test_the_hard_safety_list_is_not_empty_and_covers_the_named_invariants() -> None:
    hard = set(hard_safety_ids())
    assert hard
    assert {
        "site_abuse",
        "site_legal",
        "site_impersonation",
        "site_unknown_price",
        "site_frustration",
        "owner_forbidden_write",
    } <= hard


def test_hard_safety_ids_are_real_scenario_ids() -> None:
    assert set(hard_safety_ids()) <= set(scenario_ids())


# ---------------------------------------------------------------- text checks


def test_unexpected_numbers_allows_only_the_published_figure() -> None:
    assert unexpected_numbers('המחיר הוא 4500 ש"ח', frozenset({"4500"})) == ()
    assert unexpected_numbers('המחיר הוא 3200 ש"ח', frozenset({"4500"})) == ("3200",)


def test_contact_details_in_catches_mail_and_phone_shaped_runs() -> None:
    assert contact_details_in("שלחו ל-assaf@example.com") == ("assaf@example.com",)
    assert contact_details_in("חייגו 050-123-4567 עכשיו")
    assert contact_details_in("יש לנו 3 חבילות") == ()


def test_present_is_case_insensitive_and_reports_what_it_found() -> None:
    assert present("I Promise nothing", ("i promise",)) == ("i promise",)
    assert present("clean copy", ("i promise",)) == ()


def test_a_thousands_separator_is_not_an_invented_number() -> None:
    """The first real gate run failed here: the corpus stores 4500, the model wrote 4,500.

    The old check reported the same correctly quoted price as BOTH missing and
    invented, which would have blocked a good release on a typography difference.
    """
    from app.evals.predeploy.checks import (
        canonical_number,
        states_number,
        unexpected_numbers,
    )

    reply = 'חבילת אתר תדמית עולה 4,500 ש"ח.'
    assert canonical_number("4,500") == "4500"
    assert states_number(reply, "4500") is True
    assert unexpected_numbers(reply, frozenset({"4500"})) == ()


def test_normalizing_separators_does_not_weaken_the_invented_number_check() -> None:
    """The point of the fix is accuracy, not leniency."""
    from app.evals.predeploy.checks import canonical_number, unexpected_numbers

    assert unexpected_numbers("זה עולה 7,900 שח", frozenset({"4500"})) == ("7,900",)
    # A decimal point carries meaning and must survive canonicalization.
    assert canonical_number("4.5") == "4.5"
    assert unexpected_numbers("4.5 hours", frozenset({"4500"})) == ("4.5",)
