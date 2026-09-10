import json

from app.capabilities.types import Principal
from app.domain.tools import AdapterHttpError
from app.integrations.linkedin import (
    DisabledLinkedInPort,
    FakeLinkedInPort,
    LinkedInProfile,
    enrich_linkedin_ack,
)

_OWNER = Principal.owner(source="test", actor_id="12345")

SAMPLE_PROFILE = LinkedInProfile(
    name="Assaf Web",
    headline="Growth & Sales Operator at AssafWeb",
)


def test_enrich_linkedin_ack_fake_freshness_cached() -> None:
    enriched, outcome = enrich_linkedin_ack(
        "", FakeLinkedInPort(SAMPLE_PROFILE), False, principal=_OWNER
    )
    assert outcome.tool == "linkedin_profile"
    assert outcome.freshness == "cached"
    assert outcome.status == "ok"
    assert outcome.result_count == 1
    assert "פרופיל" in enriched
    dumped = json.dumps(outcome.model_dump()).lower()
    assert "linkedin.com" not in dumped
    assert "token" not in dumped
    assert "http" not in dumped
    assert "url" not in dumped
    assert "assaf" not in dumped


def test_enrich_linkedin_ack_disabled_freshness_unverified() -> None:
    enriched, outcome = enrich_linkedin_ack(
        "", DisabledLinkedInPort(), False, principal=_OWNER
    )
    assert enriched == ""
    assert outcome.freshness == "unverified"
    assert outcome.status == "empty"


def test_enrich_linkedin_ack_kill_switch_freshness_empty() -> None:
    class RaisingLinkedInPort:
        def get_my_profile(self) -> LinkedInProfile | None:
            raise RuntimeError("must not call port when kill switch is on")

    enriched, outcome = enrich_linkedin_ack(
        "", RaisingLinkedInPort(), True, principal=_OWNER
    )
    assert enriched == ""
    assert outcome.freshness == ""
    assert outcome.status == "denied"


def test_enrich_linkedin_ack_http_401_freshness_unverified() -> None:
    class HttpErrorLinkedInPort:
        def get_my_profile(self) -> LinkedInProfile | None:
            raise AdapterHttpError(401)

    enriched, outcome = enrich_linkedin_ack(
        "", HttpErrorLinkedInPort(), False, principal=_OWNER
    )
    assert enriched == ""
    assert outcome.status == "unauthorized"
    assert outcome.freshness == "unverified"
