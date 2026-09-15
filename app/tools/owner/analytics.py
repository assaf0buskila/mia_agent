"""Owner analytics tools: SEO/GSC/GA4, LinkedIn and Instagram Insights reads, and
the read-only social capability summary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.capabilities.analytics import analytics_handlers
from app.capabilities.policy import execute_capability
from app.capabilities.search_console import search_console_handlers
from app.core.errors import PermissionDenied
from app.domain.owner.social_capabilities import SocialCapabilities, format_social_capabilities
from app.domain.seo import enrich_seo_ack
from app.domain.tools import AdapterHttpError
from app.integrations.ga4 import build_ga4_port, normalize_ga4_property_id
from app.integrations.instagram_insights import (
    _DEFAULT_OWNER_IG_LIMIT,
    _MAX_IG_INSIGHTS_LIMIT,
    DisabledInstagramInsightsPort,
    build_instagram_insights_port,
    enrich_content_insights_ack,
)
from app.integrations.linkedin import (
    DisabledLinkedInPort,
    build_linkedin_port,
    enrich_linkedin_ack,
)
from app.integrations.search_console import build_search_console_port, resolve_gsc_site_url
from app.integrations.seo_audit import build_seo_audit_port
from app.tools.owner.types import ToolContext, ToolResult, _empty, _house_unavailable


def _metric_value(value: object) -> object:
    """Render real zeroes; only an absent value is unavailable."""
    return "unavailable" if value is None or value == "" else value


def _seo_snapshot(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    del args
    search_console = ctx.search_console
    ga4 = ctx.ga4
    seo_audit = ctx.seo_audit
    if ctx.settings.composio_ready():
        search_console = search_console or build_search_console_port(ctx.settings)
        ga4 = ga4 or build_ga4_port(ctx.settings)
        seo_audit = seo_audit or build_seo_audit_port(ctx.settings)
    if search_console is None or ga4 is None or seo_audit is None:
        return _house_unavailable(ctx, "GSC/GA4")
    text, _outcomes = enrich_seo_ack(
        "",
        search_console,
        ga4,
        seo_audit,
        principal=ctx.principal,
        kill_switch=ctx.kill_switch,
        store=ctx.store,
        settings=ctx.settings,
        demo_active=ctx.demo_active,
    )
    return _empty(text, "SEO ports returned nothing. Check GSC site URL and GA4 property.")


def _website_kpis(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """API-backed, normalized owner KPI read; provider payloads never reach the model."""
    del args
    search_console = ctx.search_console
    ga4 = ctx.ga4
    if ctx.settings.composio_ready():
        search_console = search_console or build_search_console_port(ctx.settings)
        ga4 = ga4 or build_ga4_port(ctx.settings)
    if search_console is None or ga4 is None:
        return _house_unavailable(ctx, "GSC/GA4")
    end_date = (ctx.now or datetime.now(UTC)).date() - timedelta(days=1)
    start_date = end_date - timedelta(days=27)
    date_args = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}

    def call(
        name: str, call_args: dict[str, Any], handlers: dict[str, Any]
    ) -> dict[str, Any] | str:
        try:
            return execute_capability(
                name,
                principal=ctx.principal,
                args=call_args,
                handlers=handlers,
                kill_switch=ctx.kill_switch,
            )
        except PermissionDenied:
            return "denied"
        except AdapterHttpError as exc:
            return exc.tool_status()
        except (RuntimeError, ValueError, OSError):
            return "unavailable"

    traffic = call("analytics.get_traffic", date_args, analytics_handlers(ga4))
    pages = call(
        "search_console.query",
        {**date_args, "dimensions": ["page"]},
        search_console_handlers(search_console),
    )
    queries = call(
        "search_console.query",
        {**date_args, "dimensions": ["query"]},
        search_console_handlers(search_console),
    )
    period = f"{date_args['start_date']} to {date_args['end_date']}"
    site = resolve_gsc_site_url(ctx.settings) or "unknown"
    property_id = (
        normalize_ga4_property_id(ctx.settings.ga4_property_id.strip())
        or ctx.settings.ga4_property_id.strip()
        or "unknown"
    )
    lines = [
        f"Google Search Console and GA4 ({period}); "
        f"GA4 property {property_id}; GSC {site}; numbers from the API:"
    ]
    if isinstance(traffic, str):
        lines.append(f"GA4 traffic: unavailable ({traffic}).")
    else:
        traffic_rows = [row for row in traffic.get("rows", []) if isinstance(row, dict)]
        conversions = traffic.get("conversions", [])
        if not traffic_rows and not conversions:
            lines.append("GA4 traffic: no rows returned for this period.")
        else:
            page_bits = []
            for row in traffic_rows[:5]:
                label = str(row.get("landing_page") or row.get("session_source") or "unknown")
                page_bits.append(
                    f"{label}: users {_metric_value(row.get('users'))}, "
                    f"sessions {_metric_value(row.get('sessions'))}, "
                    f"conversions {_metric_value(row.get('conversions'))}"
                )
            lines.append("GA4 top pages: " + ("; ".join(page_bits) or "unavailable") + ".")
            if conversions:
                lines.append(
                    "GA4 conversion events: "
                    + ", ".join(str(item) for item in conversions[:10])
                    + "."
                )

    for label, result, key in (
        ("GSC top pages", pages, "page"),
        ("GSC top queries", queries, "query"),
    ):
        if isinstance(result, str):
            lines.append(f"{label}: unavailable ({result}).")
            continue
        rows = [row for row in result.get("rows", []) if isinstance(row, dict)]
        if not rows:
            lines.append(f"{label}: no rows returned for this period.")
            continue
        bits = []
        for row in rows[:5]:
            bits.append(
                f"{row.get(key) or 'unknown'}: clicks {_metric_value(row.get('clicks'))}, "
                f"impressions {_metric_value(row.get('impressions'))}, "
                f"CTR {_metric_value(row.get('ctr'))}, "
                f"position {_metric_value(row.get('position'))}"
            )
        lines.append(label + ": " + "; ".join(bits) + ".")
    return ToolResult(ok=True, text="\n".join(lines))


def _linkedin_snapshot(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    full_profile = args.get("full_profile") is True
    port = ctx.linkedin
    if port is None and ctx.settings.composio_ready():
        port = build_linkedin_port(ctx.settings)
    if port is None:
        return _house_unavailable(ctx, "LinkedIn")
    text, outcome = enrich_linkedin_ack(
        "",
        port,
        ctx.kill_switch,
        principal=ctx.principal,
        full_profile=full_profile,
    )
    if outcome.status not in {"ok", "empty"}:
        return ToolResult(ok=False, error=f"LinkedIn profile read failed ({outcome.status}).")
    result = _empty(text, "LinkedIn returned nothing.")
    if outcome.status == "ok" and text.strip():
        result.evidence = "linkedin_profile"
    if full_profile:
        result.max_chars = 8_000
    return result


def _instagram_insights(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    raw_limit = args.get("limit")
    if raw_limit is None or raw_limit == "":
        limit = _DEFAULT_OWNER_IG_LIMIT
    else:
        try:
            limit = int(raw_limit)
        except (TypeError, ValueError):
            return ToolResult(ok=False, error="limit must be an integer")
        limit = max(1, min(limit, _MAX_IG_INSIGHTS_LIMIT))
    insights = ctx.instagram_insights
    if insights is None and ctx.settings.composio_ready():
        insights = build_instagram_insights_port(ctx.settings)
    if insights is None:
        return _house_unavailable(ctx, "Instagram")
    text, outcome = enrich_content_insights_ack(
        "",
        insights,
        ctx.store,
        ctx.kill_switch,
        limit=limit,
        detail=True,
    )
    if outcome.status not in {"ok", "empty", "partial"}:
        return ToolResult(
            ok=False,
            error=f"Instagram insights status: {outcome.status}.",
        )
    return _empty(text, "Instagram insights returned nothing.")


def _social_capabilities(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Capability truth from configuration only -- no provider call.

    Resolves each port with the exact same condition its own read tool uses
    (`_linkedin_snapshot` / `_instagram_insights` above: fall back to the house
    builder only when `ctx.settings.composio_ready()`, never unconditionally) and
    checks the result against that builder's own "disabled" sentinel. Matching the
    condition, not just the builder call, matters for Instagram specifically:
    `build_instagram_insights_port` also has a direct-Graph-token path that
    `_instagram_insights` never reaches when the context has no port and Composio
    is not ready -- an unconditional fallback here would report "available" in
    that exact case while the real read tool reports "not connected". Neither
    builder makes a network call; both only branch on settings and construct a
    port object.
    """
    del args
    linkedin_port = ctx.linkedin
    if linkedin_port is None and ctx.settings.composio_ready():
        linkedin_port = build_linkedin_port(ctx.settings)
    linkedin_configured = linkedin_port is not None and not isinstance(
        linkedin_port, DisabledLinkedInPort
    )

    instagram_port = ctx.instagram_insights
    if instagram_port is None and ctx.settings.composio_ready():
        instagram_port = build_instagram_insights_port(ctx.settings)
    instagram_configured = instagram_port is not None and not isinstance(
        instagram_port, DisabledInstagramInsightsPort
    )

    caps = SocialCapabilities(
        linkedin_configured=linkedin_configured,
        instagram_configured=instagram_configured,
    )
    return ToolResult(ok=True, text=format_social_capabilities(caps))
