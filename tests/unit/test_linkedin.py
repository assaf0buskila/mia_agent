import inspect
import json
from types import SimpleNamespace

import httpx
import pytest
from app.capabilities.linkedin import linkedin_get_profile
from app.capabilities.types import Principal
from app.core.config import Settings
from app.domain.tools import AdapterHttpError, AdapterResponseError, AdapterSchemaError
from app.integrations.linkedin import (
    COMPOSIO_GET_MY_INFO_TOOL,
    COMPOSIO_LINKEDIN_VERSION,
    ComposioLinkedInPort,
    DisabledLinkedInPort,
    FakeLinkedInPort,
    LinkedInEducation,
    LinkedInExperience,
    LinkedInPort,
    LinkedInProfile,
    build_linkedin_port,
    enrich_linkedin_ack,
    format_full_profile,
    format_profile_line,
)
from app.tools.owner.analytics import _linkedin_snapshot

OWNER_LINKEDIN_PHONE = "972509990009"
_OWNER = Principal.owner(source="test")

SAMPLE_PROFILE = LinkedInProfile(
    name="Assaf Web",
    headline="Growth & Sales Operator at AssafWeb",
)


def test_fake_returns_snapshot_disabled_returns_none() -> None:
    fake = FakeLinkedInPort(SAMPLE_PROFILE)
    disabled = DisabledLinkedInPort()
    assert fake.get_my_profile() == SAMPLE_PROFILE
    assert disabled.get_my_profile() is None


def test_format_profile_line_omits_missing_fields() -> None:
    assert format_profile_line(LinkedInProfile(headline="Builder")) == "פרופיל: Builder."
    assert format_profile_line(LinkedInProfile(name="Assaf")) == "פרופיל: Assaf."
    assert format_profile_line(LinkedInProfile()) == ""


def test_full_profile_formatter_is_factual_and_names_missing_sections() -> None:
    profile = LinkedInProfile(
        name="Assaf Web",
        headline="Growth operator",
        about="I build practical sales systems.",
        location="Tel Aviv",
        experience=[
            LinkedInExperience(
                title="Founder",
                company="AssafWeb",
                start_date="2024-1",
                description="Web and AI sales systems",
            )
        ],
        education=[LinkedInEducation(school="Example University", degree="BSc")],
        skills=["Sales", "Automation"],
    )

    rendered = format_full_profile(profile)

    assert rendered.startswith("פרופיל: Assaf Web — Growth operator.")
    assert "I build practical sales systems." in rendered
    assert "Founder · AssafWeb" in rendered
    assert "Example University | BSc" in rendered
    assert "כישורים: Sales, Automation" in rendered
    assert "לא נמסר בתוצאת הפרופיל: תעשייה, קישור ציבורי, שפות." in rendered
    assert "not supported" not in rendered.lower()


def test_enrich_linkedin_ack_full_profile_preserves_capability_fields() -> None:
    profile = LinkedInProfile(
        name="Assaf Web",
        headline="Builder",
        location="Israel",
        languages=["Hebrew", "English"],
    )
    enriched, outcome = enrich_linkedin_ack(
        "",
        FakeLinkedInPort(profile),
        kill_switch=False,
        principal=_OWNER,
        full_profile=True,
    )

    assert outcome.status == "ok"
    assert "מיקום: Israel" in enriched
    assert "שפות: Hebrew, English" in enriched
    assert "לא נמסר בתוצאת הפרופיל:" in enriched


def test_capability_returns_typed_profile_and_machine_readable_missing_sections() -> None:
    result = linkedin_get_profile(
        FakeLinkedInPort(LinkedInProfile(name="Assaf", skills=["Automation"])),
        {},
    )

    assert result["name"] == "Assaf"
    assert result["profile"]["skills"] == ["Automation"]
    assert "headline" in result["missing_sections"]
    assert "skills" not in result["missing_sections"]


def test_owner_tool_reports_provider_failure_instead_of_masking_it_as_empty() -> None:
    class HttpErrorLinkedInPort:
        def get_my_profile(self) -> LinkedInProfile | None:
            raise AdapterHttpError(500)

    ctx = SimpleNamespace(
        linkedin=HttpErrorLinkedInPort(),
        settings=Settings(_env_file=None),
        kill_switch=False,
        principal=_OWNER,
    )

    result = _linkedin_snapshot(ctx, {"full_profile": True})  # type: ignore[arg-type]

    assert result.ok is False
    assert "LinkedIn profile read failed" in result.error
    assert "returned nothing" not in result.error




def test_protocol_has_no_create_post_delete_comment_dm_upload_methods() -> None:
    forbidden = ("create", "post", "delete", "comment", "dm", "upload")
    protocol_methods = {
        name for name, _ in inspect.getmembers(LinkedInPort, predicate=inspect.isfunction)
    }
    for name in protocol_methods:
        lowered = name.lower()
        assert not any(token in lowered for token in forbidden)

    for impl in (
        DisabledLinkedInPort(),
        FakeLinkedInPort(SAMPLE_PROFILE),
        ComposioLinkedInPort(api_key="cmp-test", user_id="user-123"),
    ):
        for name in dir(impl):
            if name.startswith("_"):
                continue
            lowered = name.lower()
            assert not any(token in lowered for token in forbidden)


def test_build_linkedin_port_live_when_both_credentials_set() -> None:
    settings = Settings(
        composio_api_key="cmp-live",
        composio_user_id="user-123",
    )
    port = build_linkedin_port(settings)
    assert isinstance(port, ComposioLinkedInPort)
    assert not isinstance(port, DisabledLinkedInPort)


@pytest.mark.parametrize(
    "api_key,user_id",
    [
        ("", ""),
        ("cmp-live", ""),
        ("", "user-123"),
        ("   ", "user-123"),
        ("cmp-live", "   "),
    ],
)
def test_build_linkedin_port_disabled_when_any_credential_missing(
    api_key: str,
    user_id: str,
) -> None:
    settings = Settings(composio_api_key=api_key, composio_user_id=user_id)
    port = build_linkedin_port(settings)
    assert isinstance(port, DisabledLinkedInPort)


def test_composio_linkedin_port_http_500_raises_adapter_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.get_my_profile()
    assert exc_info.value.status_code == 500


class _RaisingHttpClient:
    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.HTTPError("network error")


def test_composio_linkedin_port_network_error_raises_adapter_error() -> None:
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=_RaisingHttpClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.get_my_profile()
    assert exc_info.value.status_code is None


def test_composio_linkedin_port_unsuccessful_response_is_not_reported_as_empty() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": {}, "error": "tool failed", "successful": False},
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    with pytest.raises(AdapterResponseError) as exc_info:
        port.get_my_profile()
    assert exc_info.value.tool_status() == "error"


def test_composio_linkedin_port_schema_mismatch_is_not_reported_as_empty() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": ["not a profile object"], "successful": True},
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    with pytest.raises(AdapterSchemaError) as exc_info:
        port.get_my_profile()
    assert exc_info.value.tool_status() == "malformed"


def test_composio_linkedin_port_request_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "data": {"localizedFirstName": "Assaf", "localizedHeadline": "Builder"},
                "error": None,
                "successful": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-abc",
        client=client,
    )
    port.get_my_profile()

    assert str(captured["url"]).endswith(f"/{COMPOSIO_GET_MY_INFO_TOOL}")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["user_id"] == "user-abc"
    assert body["version"] == COMPOSIO_LINKEDIN_VERSION
    arguments = body["arguments"]
    assert isinstance(arguments, dict)
    assert arguments == {}
    assert "text" not in body
    assert "text" not in arguments
    serialized = json.dumps(body)
    for forbidden in (
        "CREATE",
        "DELETE",
        "COMMENT",
        "UPLOAD",
        "POST",
    ):
        assert forbidden not in serialized.upper()


def test_composio_linkedin_port_maps_localized_name_and_headline() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "localizedFirstName": "Assaf",
                    "localizedLastName": "Web",
                    "localizedHeadline": "Growth & Sales Operator at AssafWeb",
                },
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    profile = port.get_my_profile()
    assert profile == LinkedInProfile(
        name="Assaf Web",
        headline="Growth & Sales Operator at AssafWeb",
    )


def test_composio_linkedin_port_maps_name_and_headline_fields() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "name": "Assaf Web",
                    "headline": "Growth & Sales Operator at AssafWeb",
                },
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    profile = port.get_my_profile()
    assert profile == SAMPLE_PROFILE


def test_composio_linkedin_port_empty_name_and_headline_returns_none() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {"localizedFirstName": "", "localizedHeadline": "   "},
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    assert port.get_my_profile() is None


def test_composio_linkedin_port_maps_nested_localized_objects() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "firstName": {"localized": {"en_US": "Assaf"}},
                    "lastName": {"localized": {"en_US": "Web"}},
                    "headline": {"localized": {"en_US": "Builder"}},
                },
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    )
    profile = port.get_my_profile()
    assert profile == LinkedInProfile(name="Assaf Web", headline="Builder")


def test_composio_linkedin_port_maps_allowlisted_full_profile_and_discards_unknowns() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "name": " Assaf   Web ",
                    "headline": "Founder",
                    "summary": "Builds\nAI sales systems",
                    "geoLocationName": "Tel Aviv",
                    "industryName": "Software",
                    "publicProfileUrl": "https://www.linkedin.com/in/assaf-web",
                    "vanityName": "assaf-web",
                    "positions": [
                        {
                            "title": "Founder",
                            "company": {"name": "AssafWeb"},
                            "timePeriod": {"startDate": {"year": 2024, "month": 2}},
                            "description": "AI sales systems",
                        }
                    ],
                    "educations": [
                        {
                            "schoolName": "Example University",
                            "degreeName": "BSc",
                            "fieldOfStudy": "Computer Science",
                        }
                    ],
                    "skills": [{"name": "Automation"}, "Sales"],
                    "languages": [{"language": "Hebrew"}, "English"],
                    "emailAddress": "private@example.com",
                    "accessToken": "secret-token",
                    "arbitraryProviderPayload": {"secret": "do-not-copy"},
                },
                "error": None,
                "successful": True,
            },
        )
    )
    client = httpx.Client(transport=transport)
    profile = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=client,
    ).get_my_profile()

    assert profile is not None
    assert profile.name == "Assaf Web"
    assert profile.about == "Builds AI sales systems"
    assert profile.location == "Tel Aviv"
    assert profile.industry == "Software"
    assert profile.experience[0].company == "AssafWeb"
    assert profile.experience[0].start_date == "2024-2"
    assert profile.education[0].field_of_study == "Computer Science"
    assert profile.skills == ["Automation", "Sales"]
    assert profile.languages == ["Hebrew", "English"]
    serialized = json.dumps(profile.model_dump())
    assert "private@example.com" not in serialized
    assert "secret-token" not in serialized
    assert "do-not-copy" not in serialized


def test_full_profile_mapping_enforces_section_and_text_bounds() -> None:
    data = {
        "name": "A" * 500,
        "summary": "B" * 2_000,
        "positions": [{"title": f"Role {index}", "description": "D" * 900} for index in range(20)],
        "educations": [{"schoolName": f"School {index}"} for index in range(20)],
        "skills": [f"Skill {index}" for index in range(40)],
        "languages": [f"Language {index}" for index in range(20)],
    }
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"data": data, "error": None, "successful": True},
        )
    )
    profile = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=httpx.Client(transport=transport),
    ).get_my_profile()

    assert profile is not None
    assert len(profile.name) == 240
    assert len(profile.about) == 1_200
    assert len(profile.experience) == 10
    assert len(profile.experience[0].description) == 500
    assert len(profile.education) == 8
    assert len(profile.skills) == 20
    assert len(profile.languages) == 12
    assert len(format_full_profile(profile)) <= 8_000


def test_non_linkedin_profile_url_is_discarded() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "data": {
                    "name": "Assaf",
                    "publicProfileUrl": "javascript:alert(1)",
                },
                "successful": True,
            },
        )
    )
    profile = ComposioLinkedInPort(
        api_key="cmp-test",
        user_id="user-123",
        client=httpx.Client(transport=transport),
    ).get_my_profile()

    assert profile is not None
    assert profile.profile_url == ""
