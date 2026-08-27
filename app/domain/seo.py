"""Owner SEO workflow — read-only GSC/GA4/audit enrich and recommendation persist."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app.capabilities.analytics import analytics_handlers
from app.capabilities.policy import execute_capability
from app.capabilities.search_console import search_console_handlers
from app.capabilities.types import Principal
from app.core.config import Settings, get_settings
from app.core.errors import PermissionDenied, PolicyDenied
from app.core.risk import RiskAction, RiskLevel, assert_allowed
from app.domain.ai_runs import elapsed_ms
from app.domain.tools import AdapterHttpError, ToolOutcome
from app.integrations.ga4 import (
    Ga4PivotRow,
    Ga4Port,
    _ga4_outcome,
    format_conversion_events_block,
    format_ga4_rows_block,
)
from app.integrations.search_console import (
    SearchAnalyticsRow,
    SearchConsolePort,
    _gsc_outcome,
    find_weak_ctr_page,
    format_gsc_rows_block,
)
from app.integrations.seo_audit import SeoAuditPort, _seo_audit_outcome, format_audit_block

if TYPE_CHECKING:
    from app.db.store import LeadStore

SEO_SCOPE = "site"
_GSC_DIMENSIONS = ["page"]


class SeoRecommendation(BaseModel):
    problem: str
    evidence: str
    why: str
    change: str
    metric: str


def _default_read_dates(settings: Settings | None = None) -> tuple[str, str]:
    """GSC requires YYYY-MM-DD. Use 28 completed local days ending yesterday."""
    timezone = "Asia/Jerusalem"
    if settings is not None and settings.calendar_timezone.strip():
        timezone = settings.calendar_timezone.strip()
    local_today = datetime.now(ZoneInfo(timezone)).date()
    end = local_today - timedelta(days=1)
    start = end - timedelta(days=27)
    return start.isoformat(), end.isoformat()


def _build_recommendation(
    *,
    gsc_rows: list[SearchAnalyticsRow],
    audit_present: bool,
    audit_h1_count: int | None,
    audit_title: str,
) -> SeoRecommendation | None:
    weak = find_weak_ctr_page(gsc_rows)
    if weak is not None:
        label = (weak.page or weak.query).strip()[:120]
        return SeoRecommendation(
            problem="CTR נמוך בדף עם הצגות",
            evidence=f"{label} — CTR {weak.ctr or '?'} / הצגות {weak.impressions or '?'}",
            why="בין השורות שהוחזרו CTR נמוך יחסית לשאר",
            change="הצע שינוי כותרת/תיאור meta לדף — רק אחרי אישור",
            metric="CTR ב-GSC לדף זה",
        )
    if audit_present and audit_h1_count is not None and audit_h1_count != 1:
        if audit_h1_count == 0:
            problem = "אין H1 בדף הבית"
            change = "הוסף H1 אחד ברור — רק אחרי אישור"
        else:
            problem = "יותר מ-H1 אחד בדף הבית"
            change = "אחד H1 בלבד — רק אחרי אישור"
        return SeoRecommendation(
            problem=problem,
            evidence=f"h1_count={audit_h1_count}",
            why="מבנה כותרות משפיע על SEO ונגישות",
            change=change,
            metric="h1_count=1",
        )
    if audit_present and not audit_title.strip():
        return SeoRecommendation(
            problem="חסרה כותרת title",
            evidence="title ריק בביקורת דף הבית",
            why="title הוא אות ראשון בתוצאות חיפוש",
            change="הצע title — רק אחרי אישור",
            metric="title קיים ב-scrape",
        )
    return None


def apply_seo_recommendation_policy(
    store: LeadStore,
    *,
    rec: SeoRecommendation,
    kill_switch: bool,
    demo_active: bool,
) -> None:
    if demo_active or kill_switch:
        return
    try:
        assert_allowed(
            RiskAction(name="seo_recommend_persist", risk=RiskLevel.R1_LOW_WRITE),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        return
    store.upsert_seo_recommendation(
        scope=SEO_SCOPE,
        problem=rec.problem[:255],
        evidence=rec.evidence[:255],
        why=rec.why[:255],
        change=rec.change[:255],
        metric=rec.metric[:255],
    )


def enrich_seo_ack(
    ack: str,
    gsc: SearchConsolePort,
    ga4: Ga4Port,
    audit: SeoAuditPort,
    *,
    principal: Principal,
    kill_switch: bool,
    store: LeadStore | None = None,
    settings: Settings | None = None,
    demo_active: bool = False,
    now: datetime | None = None,
) -> tuple[str, list[ToolOutcome]]:
    """Append SEO facts from live/disabled ports. Returns ack and tool outcomes."""
    settings = settings or get_settings()
    effective_now = now or datetime.now(UTC)
    outcomes: list[ToolOutcome] = []
    blocks: list[str] = []
    gsc_rows: list[SearchAnalyticsRow] = []
    audit_snapshot = None
    start_date, end_date = _default_read_dates(settings)

    started = perf_counter()
    try:
        payload = execute_capability(
            "search_console.query",
            principal=principal,
            args={
                "start_date": start_date,
                "end_date": end_date,
                "dimensions": _GSC_DIMENSIONS,
            },
            handlers=search_console_handlers(gsc),
            kill_switch=kill_switch,
        )
        latency = elapsed_ms(started)
        gsc_rows = [
            SearchAnalyticsRow(
                page=str(item.get("page") or ""),
                query=str(item.get("query") or ""),
                clicks=item.get("clicks") if item.get("clicks") is not None else None,
                impressions=(
                    item.get("impressions") if item.get("impressions") is not None else None
                ),
                ctr=item.get("ctr") if item.get("ctr") is not None else None,
                position=item.get("position") if item.get("position") is not None else None,
            )
            for item in (payload.get("rows") or [])
            if isinstance(item, dict)
        ]
        block = format_gsc_rows_block(gsc_rows)
        if block:
            blocks.append(block)
        outcomes.append(
            _gsc_outcome(
                base_status="ok" if gsc_rows else "empty",
                present=bool(gsc_rows),
                result_count=len(gsc_rows),
                latency_ms=latency,
                now=effective_now,
            )
        )
    except PermissionDenied:
        outcomes.append(
            ToolOutcome(tool="gsc_search_analytics", status="denied", result_count=0)
        )
    except AdapterHttpError as exc:
        outcomes.append(
            _gsc_outcome(
                base_status=exc.tool_status(),
                present=False,
                result_count=0,
                latency_ms=elapsed_ms(started),
                now=effective_now,
            )
        )
    except (RuntimeError, PolicyDenied, ValueError, OSError):
        outcomes.append(
            _gsc_outcome(
                base_status="error",
                present=False,
                result_count=0,
                latency_ms=elapsed_ms(started),
                now=effective_now,
            )
        )

    started = perf_counter()
    try:
        payload = execute_capability(
            "analytics.get_traffic",
            principal=principal,
            args={"start_date": start_date, "end_date": end_date},
            handlers=analytics_handlers(ga4),
            kill_switch=kill_switch,
        )
        latency = elapsed_ms(started)
        pivot_rows = [
            Ga4PivotRow(
                landing_page=str(item.get("landing_page") or ""),
                session_source=str(item.get("session_source") or ""),
                sessions=item.get("sessions") if item.get("sessions") is not None else None,
                engaged_sessions=(
                    item.get("engaged_sessions")
                    if item.get("engaged_sessions") is not None
                    else None
                ),
            )
            for item in (payload.get("rows") or [])
            if isinstance(item, dict)
        ]
        conversions = [
            str(item)
            for item in (payload.get("conversions") or [])
            if str(item).strip()
        ]
        ga4_block = format_ga4_rows_block(pivot_rows)
        conv_block = format_conversion_events_block(conversions)
        if ga4_block:
            blocks.append(ga4_block)
        if conv_block:
            blocks.append(conv_block)
        count = len(pivot_rows) + len(conversions)
        outcomes.append(
            _ga4_outcome(
                base_status="ok" if count else "empty",
                present=count > 0,
                result_count=len(pivot_rows),
                latency_ms=latency,
                now=effective_now,
            )
        )
    except PermissionDenied:
        outcomes.append(
            ToolOutcome(tool="ga4_pivot_report", status="denied", result_count=0)
        )
    except AdapterHttpError as exc:
        outcomes.append(
            _ga4_outcome(
                base_status=exc.tool_status(),
                present=False,
                result_count=0,
                latency_ms=elapsed_ms(started),
                now=effective_now,
            )
        )
    except (RuntimeError, PolicyDenied, ValueError, OSError):
        outcomes.append(
            _ga4_outcome(
                base_status="error",
                present=False,
                result_count=0,
                latency_ms=elapsed_ms(started),
                now=effective_now,
            )
        )

    try:
        assert_allowed(
            RiskAction(name="seo_audit_read", risk=RiskLevel.R0_READ),
            kill_switch=kill_switch,
        )
    except PolicyDenied:
        outcomes.append(ToolOutcome(tool="seo_audit", status="denied", result_count=0))
    else:
        started = perf_counter()
        try:
            audit_snapshot = audit.audit_homepage()
            latency = elapsed_ms(started)
            audit_block = format_audit_block(audit_snapshot) if audit_snapshot else ""
            if audit_block:
                blocks.append(audit_block)
            outcomes.append(
                _seo_audit_outcome(
                    base_status="ok" if audit_snapshot else "empty",
                    present=audit_snapshot is not None,
                    result_count=1 if audit_snapshot else 0,
                    latency_ms=latency,
                    now=effective_now,
                )
            )
        except AdapterHttpError as exc:
            outcomes.append(
                _seo_audit_outcome(
                    base_status=exc.tool_status(),
                    present=False,
                    result_count=0,
                    latency_ms=elapsed_ms(started),
                    now=effective_now,
                )
            )
        except (RuntimeError, PolicyDenied, ValueError, OSError):
            outcomes.append(
                _seo_audit_outcome(
                    base_status="error",
                    present=False,
                    result_count=0,
                    latency_ms=elapsed_ms(started),
                    now=effective_now,
                )
            )

    enriched = ack
    if blocks:
        enriched = f"{ack}\n\n" + "\n\n".join(blocks)

    rec = _build_recommendation(
        gsc_rows=gsc_rows,
        audit_present=audit_snapshot is not None,
        audit_h1_count=audit_snapshot.h1_count if audit_snapshot else None,
        audit_title=audit_snapshot.title if audit_snapshot else "",
    )
    if rec is not None:
        enriched = (
            f"{enriched}\n\nהמלצה:\n"
            f"בעיה: {rec.problem}\n"
            f"ראיה: {rec.evidence}\n"
            f"למה: {rec.why}\n"
            f"שינוי: {rec.change}\n"
            f"מדד: {rec.metric}"
        )
        if store is not None:
            apply_seo_recommendation_policy(
                store,
                rec=rec,
                kill_switch=kill_switch,
                demo_active=demo_active,
            )

    if (
        not blocks
        and not kill_switch
        and all(outcome.status in {"empty", "denied"} for outcome in outcomes)
        and outcomes
    ):
        enriched = f"{ack}\n\nאין נתוני חיפוש או אנליטיקה כרגע."

    return enriched, outcomes
