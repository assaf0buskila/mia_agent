"""Campaign pre-launch gate (§20.3). Persist-only; never Meta writes or launch."""
from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING

from pydantic import BaseModel, field_validator, model_validator

from app.core.errors import PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.pacing import campaign_label, parse_monthly_budget

if TYPE_CHECKING:
    from app.core.config import Settings
    from app.db.store import LeadStore

_LAUNCH_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_OBJECTIVE_PATTERN = re.compile(r"^(leads|traffic|awareness)$")
_LEAD_PATH_PATTERN = re.compile(r"^(website|whatsapp|instagram)$")

ALLOWLISTED_CHECK_IDS = frozenset(
    {
        "tracking_utms",
        "source_attribution",
        "lead_capture",
        "sheet_tabs",
        "alert_thresholds",
        "e2e_test",
        "campaign_config",
    }
)

_CHECK_LABELS_HE = {
    "tracking_utms": "מעקב UTM",
    "source_attribution": "ייחוס מקור",
    "lead_capture": "קליטת לידים",
    "sheet_tabs": "טאבי שיטס",
    "alert_thresholds": "סף התראה",
    "e2e_test": "בדיקת קצה לקצה",
    "campaign_config": "הגדרות קמפיין",
}


def should_run_prelaunch(settings: Settings) -> bool:
    return campaign_label(settings) != "account"


def parse_launch_date(value: str) -> str | None:
    stripped = value.strip()
    if _LAUNCH_DATE_PATTERN.fullmatch(stripped) is None:
        return None
    try:
        date.fromisoformat(stripped)
    except ValueError:
        return None
    return stripped


def parse_objective(value: str) -> str | None:
    stripped = value.strip()
    if _OBJECTIVE_PATTERN.fullmatch(stripped) is None:
        return None
    return stripped


def parse_lead_path(value: str) -> str | None:
    stripped = value.strip()
    if _LEAD_PATH_PATTERN.fullmatch(stripped) is None:
        return None
    return stripped


def _lead_capture_ok(settings: Settings, lead_path: str) -> bool:
    if lead_path == "website":
        return settings.website_url.strip().startswith("https://")
    if lead_path == "whatsapp":
        return bool(
            settings.whatsapp_verify_token.strip()
            and settings.whatsapp_access_token.strip()
            and settings.whatsapp_phone_number_id.strip()
        )
    if lead_path == "instagram":
        return bool(
            settings.instagram_verify_token.strip()
            and settings.instagram_access_token.strip()
            and settings.instagram_account_id.strip()
        )
    return False


class PrelaunchSnapshot(BaseModel):
    campaign: str
    launch_date: str = ""
    objective: str = ""
    lead_path: str = ""
    ready: bool
    failed_checks: str = ""
    skipped: bool = False

    @field_validator("failed_checks")
    @classmethod
    def _validate_failed_checks(cls, value: str) -> str:
        if not value:
            return ""
        parts = [part for part in value.split(",") if part]
        for part in parts:
            if part not in ALLOWLISTED_CHECK_IDS:
                raise ValueError(f"unknown check id: {part}")
        return ",".join(sorted(parts))

    @model_validator(mode="after")
    def _ready_failed_consistent(self) -> PrelaunchSnapshot:
        if self.ready and self.failed_checks:
            raise ValueError("ready True requires empty failed_checks")
        return self


def evaluate_prelaunch(settings: Settings) -> PrelaunchSnapshot:
    campaign = campaign_label(settings)
    if campaign == "account":
        return PrelaunchSnapshot(
            campaign=campaign,
            ready=False,
            failed_checks="",
            skipped=True,
        )
    launch_date = parse_launch_date(settings.campaign_launch_date) or ""
    objective = parse_objective(settings.campaign_objective) or ""
    lead_path = parse_lead_path(settings.campaign_lead_path) or ""
    utm_ok = lead_path in {"website", "instagram"}
    checks: dict[str, bool] = {
        "campaign_config": bool(launch_date and objective and lead_path),
        "tracking_utms": utm_ok,
        "source_attribution": utm_ok,
        "lead_capture": _lead_capture_ok(settings, lead_path) if lead_path else False,
        "sheet_tabs": bool(settings.sheets_spreadsheet_id.strip()),
        "alert_thresholds": parse_monthly_budget(settings.campaign_monthly_budget) is not None,
        "e2e_test": settings.campaign_e2e_tested == "true",
    }
    failed = sorted(check_id for check_id, ok in checks.items() if not ok)
    ready = not failed
    return PrelaunchSnapshot(
        campaign=campaign,
        launch_date=launch_date,
        objective=objective,
        lead_path=lead_path,
        ready=ready,
        failed_checks=",".join(failed),
    )


def format_prelaunch_line(snapshot: PrelaunchSnapshot) -> str:
    if snapshot.ready:
        return "שער טרום-השקה: מוכן"
    line = "שער טרום-השקה: לא מוכן"
    if snapshot.failed_checks:
        labels = [
            _CHECK_LABELS_HE[check_id]
            for check_id in snapshot.failed_checks.split(",")
            if check_id in _CHECK_LABELS_HE
        ]
        if labels:
            line = f"{line} חסר: {', '.join(labels)}"
    return line


def apply_prelaunch_policy(
    store: LeadStore,
    *,
    snapshot: PrelaunchSnapshot,
    kill_switch: bool,
    demo_active: bool,
    scope: str = "account",
) -> None:
    if kill_switch or demo_active or snapshot.skipped:
        return
    try:
        assert_allowed(
            RiskAction(name="campaign_prelaunch_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    store.upsert_campaign_prelaunch(
        scope=scope,
        campaign=snapshot.campaign,
        launch_date=snapshot.launch_date,
        objective=snapshot.objective,
        lead_path=snapshot.lead_path,
        ready=snapshot.ready,
        failed_checks=snapshot.failed_checks,
    )
