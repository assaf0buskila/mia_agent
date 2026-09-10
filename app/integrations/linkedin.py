"""LinkedIn own-profile read port.

Production adapter: Composio ``LINKEDIN`` toolkit version ``20260724_00``,
pin ``LINKEDIN_GET_MY_INFO`` only when ``MIA_COMPOSIO_API_KEY`` and
``MIA_COMPOSIO_USER_ID`` are set. Managed OAuth **Yes**.
Never post, comment, delete, DM, or upload this slice.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from app.capabilities.policy import execute_capability
from app.capabilities.types import Principal
from app.core.config import Settings
from app.core.errors import PermissionDenied, PolicyDenied
from app.domain.ai_runs import elapsed_ms
from app.domain.policies.freshness import overlay_stale, stamp_freshness
from app.domain.tools import (
    AdapterHttpError,
    AdapterResponseError,
    AdapterSchemaError,
    ToolOutcome,
)

COMPOSIO_LINKEDIN_VERSION = "20260724_00"
COMPOSIO_GET_MY_INFO_TOOL = "LINKEDIN_GET_MY_INFO"
_COMPOSIO_EXECUTE_URL = (
    f"https://backend.composio.dev/api/v3.1/tools/execute/{COMPOSIO_GET_MY_INFO_TOOL}"
)


_SHORT_TEXT_LIMIT = 240
_ABOUT_LIMIT = 1_200
_DESCRIPTION_LIMIT = 500
_URL_LIMIT = 500
_MAX_EXPERIENCE = 10
_MAX_EDUCATION = 8
_MAX_SKILLS = 20
_MAX_LANGUAGES = 12
MAX_FULL_PROFILE_CHARS = 8_000


class LinkedInExperience(BaseModel):
    title: str = ""
    company: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    description: str = ""


class LinkedInEducation(BaseModel):
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    start_date: str = ""
    end_date: str = ""


class LinkedInProfile(BaseModel):
    name: str = ""
    headline: str = ""
    about: str = ""
    location: str = ""
    industry: str = ""
    profile_url: str = ""
    public_identifier: str = ""
    experience: list[LinkedInExperience] = Field(default_factory=list)
    education: list[LinkedInEducation] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)

    def missing_sections(self) -> list[str]:
        present = {
            "name": bool(self.name),
            "headline": bool(self.headline),
            "about": bool(self.about),
            "location": bool(self.location),
            "industry": bool(self.industry),
            "public_profile": bool(self.profile_url or self.public_identifier),
            "experience": bool(self.experience),
            "education": bool(self.education),
            "skills": bool(self.skills),
            "languages": bool(self.languages),
        }
        return [name for name, is_present in present.items() if not is_present]


class LinkedInPort(Protocol):
    def get_my_profile(self) -> LinkedInProfile | None: ...


class DisabledLinkedInPort:
    def get_my_profile(self) -> LinkedInProfile | None:
        return None


class ComposioLinkedInPort:
    """Live Composio execute adapter for LINKEDIN_GET_MY_INFO. Raises AdapterHttpError on HTTP."""

    def __init__(
        self,
        *,
        api_key: str,
        user_id: str,
        client: httpx.Client | None = None,
    ) -> None:
        self._api_key = api_key
        self._user_id = user_id
        self._client = client

    def get_my_profile(self) -> LinkedInProfile | None:
        payload = {
            "user_id": self._user_id,
            "version": COMPOSIO_LINKEDIN_VERSION,
            "arguments": {},
        }
        headers = {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }
        try:
            if self._client is not None:
                response = self._client.post(
                    _COMPOSIO_EXECUTE_URL,
                    json=payload,
                    headers=headers,
                )
            else:
                with httpx.Client(timeout=20.0) as client:
                    response = client.post(
                        _COMPOSIO_EXECUTE_URL,
                        json=payload,
                        headers=headers,
                    )
        except httpx.HTTPError as exc:
            raise AdapterHttpError(None) from exc
        if response.status_code >= 400:
            raise AdapterHttpError(response.status_code)
        try:
            body = response.json()
            if not isinstance(body, dict) or not isinstance(body.get("successful"), bool):
                raise AdapterSchemaError()
            if body["successful"] is False:
                raise AdapterResponseError()
            data = body.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except json.JSONDecodeError:
                    raise AdapterSchemaError() from None
            if not isinstance(data, dict):
                raise AdapterSchemaError()
            return _map_data_to_profile(data)
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            IndexError,
        ):
            raise AdapterSchemaError() from None


class FakeLinkedInPort:
    """Test double. Returns configured snapshot or None."""

    def __init__(self, snapshot: LinkedInProfile | None = None) -> None:
        self._snapshot = snapshot

    def get_my_profile(self) -> LinkedInProfile | None:
        return self._snapshot


def _non_empty_str(value: object, *, limit: int = _SHORT_TEXT_LIMIT) -> str | None:
    if isinstance(value, str):
        stripped = " ".join(value.split())
        if stripped:
            return stripped[:limit]
    return None


def _localized_text(value: object, *, limit: int = _SHORT_TEXT_LIMIT) -> str | None:
    """Accept a plain string or LinkedIn `{localized: {locale: text}}` objects."""
    direct = _non_empty_str(value, limit=limit)
    if direct:
        return direct
    if not isinstance(value, dict):
        return None
    localized = value.get("localized")
    if isinstance(localized, dict):
        for item in localized.values():
            found = _non_empty_str(item, limit=limit)
            if found:
                return found
    return None


def _first_text(data: dict[str, Any], *keys: str, limit: int = _SHORT_TEXT_LIMIT) -> str:
    for key in keys:
        value = data.get(key)
        found = _localized_text(value, limit=limit)
        if found:
            return found[:limit]
        if isinstance(value, dict):
            for nested_key in ("name", "localizedName", "preferredLocalizedName"):
                found = _non_empty_str(value.get(nested_key), limit=limit)
                if found:
                    return found
    return ""


def _date_text(value: object) -> str:
    direct = _non_empty_str(value, limit=32)
    if direct:
        return direct
    if not isinstance(value, dict):
        return ""
    parts: list[str] = []
    for key in ("year", "month", "day"):
        part = value.get(key)
        if isinstance(part, int) and not isinstance(part, bool):
            parts.append(str(part))
        elif isinstance(part, str) and part.strip().isdigit():
            parts.append(part.strip())
    return "-".join(parts)[:32]


def _list_items(data: dict[str, Any], *keys: str) -> list[object]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for nested_key in ("elements", "items", "values"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return nested
    return []


def _map_experience(data: dict[str, Any]) -> list[LinkedInExperience]:
    mapped: list[LinkedInExperience] = []
    for raw in _list_items(data, "experience", "experiences", "positions")[:_MAX_EXPERIENCE]:
        if not isinstance(raw, dict):
            continue
        period = raw.get("timePeriod") or raw.get("dateRange")
        period = period if isinstance(period, dict) else {}
        item = LinkedInExperience(
            title=_first_text(raw, "title", "role"),
            company=_first_text(raw, "companyName", "company", "organizationName"),
            location=_first_text(raw, "locationName", "location"),
            start_date=_date_text(
                raw.get("startDate") or raw.get("start_date") or period.get("startDate")
            ),
            end_date=_date_text(
                raw.get("endDate") or raw.get("end_date") or period.get("endDate")
            ),
            description=_first_text(raw, "description", "summary", limit=_DESCRIPTION_LIMIT),
        )
        if any(item.model_dump().values()):
            mapped.append(item)
    return mapped


def _map_education(data: dict[str, Any]) -> list[LinkedInEducation]:
    mapped: list[LinkedInEducation] = []
    for raw in _list_items(data, "education", "educations")[:_MAX_EDUCATION]:
        if not isinstance(raw, dict):
            continue
        period = raw.get("timePeriod") or raw.get("dateRange")
        period = period if isinstance(period, dict) else {}
        item = LinkedInEducation(
            school=_first_text(raw, "schoolName", "school", "institutionName"),
            degree=_first_text(raw, "degreeName", "degree"),
            field_of_study=_first_text(raw, "fieldOfStudy", "field_of_study"),
            start_date=_date_text(
                raw.get("startDate") or raw.get("start_date") or period.get("startDate")
            ),
            end_date=_date_text(
                raw.get("endDate") or raw.get("end_date") or period.get("endDate")
            ),
        )
        if any(item.model_dump().values()):
            mapped.append(item)
    return mapped


def _map_named_list(data: dict[str, Any], keys: tuple[str, ...], *, limit: int) -> list[str]:
    values: list[str] = []
    for raw in _list_items(data, *keys)[:limit]:
        if isinstance(raw, dict):
            found = _first_text(raw, "name", "localizedName", "language", "skill")
        else:
            found = _non_empty_str(raw) or ""
        if found and found not in values:
            values.append(found)
    return values


def _public_profile_url(data: dict[str, Any]) -> str:
    candidate = _first_text(data, "publicProfileUrl", "profileUrl", limit=_URL_LIMIT)
    if not candidate:
        return ""
    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "linkedin.com" or host.endswith(".linkedin.com")):
        return ""
    return candidate


def _join_name_parts(*parts: object) -> str:
    strings: list[str] = []
    for part in parts:
        found = _localized_text(part)
        if found:
            strings.append(found)
    return " ".join(strings)


def _map_data_to_profile(data: dict[str, Any]) -> LinkedInProfile | None:
    name = _localized_text(data.get("name"))
    if not name:
        name = _join_name_parts(
            data.get("localizedFirstName"),
            data.get("localizedLastName"),
        )
    if not name:
        name = _join_name_parts(data.get("firstName"), data.get("lastName"))
    headline = _localized_text(data.get("headline"))
    if not headline:
        headline = _localized_text(data.get("localizedHeadline"))
    profile = LinkedInProfile(
        name=(name or "")[:_SHORT_TEXT_LIMIT],
        headline=headline or "",
        about=_first_text(data, "about", "summary", limit=_ABOUT_LIMIT),
        location=_first_text(data, "locationName", "geoLocationName", "location"),
        industry=_first_text(data, "industryName", "industry"),
        profile_url=_public_profile_url(data),
        public_identifier=_first_text(data, "vanityName", "publicIdentifier"),
        experience=_map_experience(data),
        education=_map_education(data),
        skills=_map_named_list(data, ("skills",), limit=_MAX_SKILLS),
        languages=_map_named_list(data, ("languages",), limit=_MAX_LANGUAGES),
    )
    if not any(profile.model_dump().values()):
        return None
    return profile


def format_profile_line(profile: LinkedInProfile) -> str:
    """One-line snapshot. Missing fields are omitted, never invented."""
    if profile.name and profile.headline:
        return f"פרופיל: {profile.name} — {profile.headline}."
    if profile.headline:
        return f"פרופיל: {profile.headline}."
    if profile.name:
        return f"פרופיל: {profile.name}."
    return ""


def format_full_profile(profile: LinkedInProfile) -> str:
    """Bounded factual profile text; absent provider fields are named explicitly."""
    lines: list[str] = []
    summary = format_profile_line(profile)
    if summary:
        lines.append(summary)
    for label, value in (
        ("אודות", profile.about),
        ("מיקום", profile.location),
        ("תעשייה", profile.industry),
        ("קישור ציבורי", profile.profile_url),
        ("מזהה ציבורי", profile.public_identifier),
    ):
        if value:
            lines.append(f"{label}: {value}")
    if profile.experience:
        lines.append("ניסיון:")
        for item in profile.experience:
            role = " · ".join(part for part in (item.title, item.company) if part)
            dates = " עד ".join(part for part in (item.start_date, item.end_date) if part)
            details = " | ".join(part for part in (role, item.location, dates) if part)
            if item.description:
                details = f"{details} | {item.description}" if details else item.description
            lines.append(f"• {details}")
    if profile.education:
        lines.append("השכלה:")
        for item in profile.education:
            study = " · ".join(part for part in (item.degree, item.field_of_study) if part)
            dates = " עד ".join(part for part in (item.start_date, item.end_date) if part)
            details = " | ".join(part for part in (item.school, study, dates) if part)
            lines.append(f"• {details}")
    if profile.skills:
        lines.append("כישורים: " + ", ".join(profile.skills))
    if profile.languages:
        lines.append("שפות: " + ", ".join(profile.languages))

    labels = {
        "name": "שם",
        "headline": "כותרת",
        "about": "אודות",
        "location": "מיקום",
        "industry": "תעשייה",
        "public_profile": "קישור ציבורי",
        "experience": "ניסיון",
        "education": "השכלה",
        "skills": "כישורים",
        "languages": "שפות",
    }
    missing = [labels[name] for name in profile.missing_sections()]
    if missing:
        lines.append("לא נמסר בתוצאת הפרופיל: " + ", ".join(missing) + ".")
    return "\n".join(lines)[:MAX_FULL_PROFILE_CHARS].rstrip()


def _linkedin_profile_outcome(
    *,
    base_status: str,
    present: bool,
    result_count: int,
    latency_ms: int,
    now: datetime,
) -> ToolOutcome:
    stamp = stamp_freshness(
        "linkedin_profile",
        present=present,
        fetched_at=now,
        now=now,
    )
    return ToolOutcome(
        tool="linkedin_profile",
        status=overlay_stale(base_status=base_status, stamp=stamp),
        result_count=result_count,
        latency_ms=latency_ms,
        freshness=stamp.status,
    )


def enrich_linkedin_ack(
    ack: str,
    port: LinkedInPort,
    kill_switch: bool,
    *,
    principal: Principal,
    full_profile: bool = False,
) -> tuple[str, ToolOutcome]:
    """Append own-profile snapshot to owner linkedin ack. Never raises; never posts."""
    from app.capabilities.linkedin import linkedin_handlers

    now = datetime.now(UTC)
    started = perf_counter()
    try:
        payload = execute_capability(
            "linkedin.get_profile",
            principal=principal,
            args={},
            handlers=linkedin_handlers(port),
            kill_switch=kill_switch,
        )
        latency = elapsed_ms(started)
        if not payload.get("found"):
            return ack, _linkedin_profile_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=latency,
                now=now,
            )
        profile = LinkedInProfile.model_validate(payload.get("profile") or payload)
        line = format_full_profile(profile) if full_profile else format_profile_line(profile)
        if not line:
            return ack, _linkedin_profile_outcome(
                base_status="empty",
                present=False,
                result_count=0,
                latency_ms=latency,
                now=now,
            )
        return (
            f"{ack}\n\n{line}",
            _linkedin_profile_outcome(
                base_status="ok",
                present=True,
                result_count=1,
                latency_ms=latency,
                now=now,
            ),
        )
    except PermissionDenied:
        return ack, ToolOutcome(
            tool="linkedin_profile",
            status="denied",
            result_count=0,
            freshness="",
        )
    except AdapterHttpError as exc:
        return ack, _linkedin_profile_outcome(
            base_status=exc.tool_status(),
            present=False,
            result_count=0,
            latency_ms=elapsed_ms(started),
            now=now,
        )
    except (RuntimeError, PolicyDenied, ValueError, OSError):
        return ack, _linkedin_profile_outcome(
            base_status="error",
            present=False,
            result_count=0,
            latency_ms=elapsed_ms(started),
            now=now,
        )


def build_linkedin_port(settings: Settings) -> LinkedInPort:
    api_key = settings.composio_api_key.strip()
    user_id = settings.composio_user_id.strip()
    if api_key and user_id:
        return ComposioLinkedInPort(api_key=api_key, user_id=user_id)
    return DisabledLinkedInPort()
