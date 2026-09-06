"""Mutation-style proof: plausible action labels cannot hide bad conversation copy."""

from dataclasses import replace

import pytest
from app.core.config import Settings
from app.evals.predeploy.checks import discovery_topics_in, website_cta_visible
from app.evals.predeploy.runner import check_website_run, run_website_scenario
from app.evals.predeploy.sandbox import SealedOwnerPort, sealed_settings
from app.evals.predeploy.scenarios import SiteRun, website_scenarios
from app.surfaces.crm import FakeContactsCrm


@pytest.mark.parametrize("scenario", website_scenarios(), ids=lambda s: s.scenario_id)
def test_offline_scenarios_satisfy_structure_but_cannot_claim_model_acceptance(scenario):
    run = run_website_scenario(
        scenario, settings=sealed_settings(Settings(_env_file=None)), reply_port=None
    )
    failures = check_website_run(scenario, run)
    expected = (
        (
            "conversion scenario used zero model tokens; fallback is not live proof",
            "every conversion reply used fallback; billed tokens alone are not proof",
        )
        if scenario.conversion_gate
        else ()
    )
    assert failures == expected


def _scenario():
    return next(s for s in website_scenarios() if s.scenario_id == "site_nail_conversion")


def _run():
    value = "אפשר לבחון הקלה בתיאום התורים. השאירו טלפון או אימייל כדי להמשיך עם אסף."
    return SiteRun(
        scenario_id=_scenario().scenario_id,
        replies=("מה גוזל לכם הכי הרבה זמן מול הלקוחות היום?", value, value),
        actions=("answer", "ask_contact", "ask_contact"),
        crm=FakeContactsCrm(),
        owner_port=SealedOwnerPort(),
        crm_wrote=False,
        owner_pinged=False,
        crm_writes=0,
        tokens_in=10,
        tokens_out=10,
        turn_states=(
            dict(
                business_known=True, friction_known=False, value_shown=False, discovery_questions=1
            ),
            dict(business_known=True, friction_known=True, value_shown=True, discovery_questions=1),
            dict(business_known=True, friction_known=True, value_shown=True, discovery_questions=1),
        ),
        whatsapp_urls=(None, None, None),
        turn_tokens=(0, 20, 0),
        model_replies_used=(False, True, False),
    )


def test_known_good_conversion_is_accepted():
    assert check_website_run(_scenario(), _run()) == ()


@pytest.mark.parametrize(
    "question",
    [
        "מה אתם עושים הכי הרבה?",
        "מה חוזר על עצמו?",
        "מה אתם מטפלים בו הכי הרבה?",
        "איזה חלק חוזר שוב ושוב?",
        "What takes the most time?",
        "מה חוזר שוב ושוב אצלכם?",
        "מה אתם עושים כל יום?",
        "What keeps happening every day?",
    ],
)
def test_semantically_repeated_friction_fails_even_with_correct_actions(question):
    run = _run()
    broken = replace(run, replies=(run.replies[0], question, run.replies[2]))
    assert any("closed topic friction" in p for p in check_website_run(_scenario(), broken))


def test_business_question_rejected_on_first_turn_when_visitor_already_explained():
    run = _run()
    broken = replace(run, replies=("מה העסק עושה בפועל?", *run.replies[1:]))
    assert any("closed topic business" in p for p in check_website_run(_scenario(), broken))


def test_bad_intermediate_reply_cannot_hide_behind_good_final_reply():
    run = _run()
    broken = replace(run, replies=("זה יעלה 400 ₪", *run.replies[1:]))
    assert any(
        "turn 1 stated an unpublished price" in p for p in check_website_run(_scenario(), broken)
    )


def test_zero_token_fallback_fails_conversion_gate():
    broken = replace(_run(), turn_tokens=(0, 0, 0))
    assert any("zero model tokens" in p for p in check_website_run(_scenario(), broken))


def test_value_without_contact_action_fails():
    broken = replace(_run(), actions=("answer", "answer", "ask_contact"))
    assert any("turn 2 continued discovery" in p for p in check_website_run(_scenario(), broken))


def test_questionless_value_is_not_mistaken_for_discovery():
    assert discovery_topics_in("תיאום התורים וההודעות חוזר על עצמו, ואפשר לבחון הקלה.") == ()


@pytest.mark.parametrize("action", ["confirm_contact", "handoff"])
def test_contact_confirmation_exposes_valid_whatsapp_cta(action):
    assert website_cta_visible(action, "https://wa.me/972501234567?text=hello")


def test_internal_value_flag_cannot_mask_contact_only_copy():
    run = _run()
    contact = "צריך טלפון או אימייל כדי להמשיך עם אסף."
    broken = replace(run, replies=(run.replies[0], contact, contact))
    assert any("without showing value" in p for p in check_website_run(_scenario(), broken))


def test_contact_question_is_allowed_after_concrete_value():
    run = _run()
    reply = "אפשר לבדוק איך להקל על תיאום התורים. אפשר טלפון או אימייל?"
    assert (
        check_website_run(_scenario(), replace(run, replies=(run.replies[0], reply, reply))) == ()
    )


def test_billed_tokens_do_not_mask_all_fallback_replies():
    broken = replace(_run(), model_replies_used=(False, False, False))
    assert any(
        "every conversion reply used fallback" in p for p in check_website_run(_scenario(), broken)
    )


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://wa.me.evil.test/972501234567",
        "https://user@wa.me/972501234567",
        "https://wa.me:bad/123",
        "https://wa.me/",
    ],
)
def test_invalid_whatsapp_urls_are_never_accepted(url):
    assert not website_cta_visible("confirm_contact", url)


def test_real_turn_loop_records_every_reply_and_accepted_model_usage():
    from app.core.config import get_settings
    from app.evals.predeploy.runner import run_website_scenario
    from app.evals.predeploy.sandbox import sealed_settings
    from app.integrations.sales_reply import ComposeResult

    class ValuePort:
        def compose(self, **kwargs):
            assert kwargs["context"].website_value_only
            return ComposeResult(
                text="אפשר לבחון הקלה בתיאום התורים והודעות הלקוחות.",
                tokens_in=12,
                tokens_out=8,
            )

    run = run_website_scenario(
        _scenario(), settings=sealed_settings(get_settings()), reply_port=ValuePort()
    )
    assert len(run.replies) == len(run.turn_states) == len(run.actions) == 3
    assert run.actions[-1] == "ask_contact"
    assert any(run.model_replies_used)
    assert check_website_run(_scenario(), run) == ()
