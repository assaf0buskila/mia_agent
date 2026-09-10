"""Owner-requested refresh of configured public website knowledge."""

from __future__ import annotations

import re
from typing import Any

from app.brain.knowledge import HttpDocumentFetcher, IngestReport, ingest_website
from app.capabilities.policy import authorize
from app.capabilities.registry import KNOWLEDGE_REFRESH
from app.core.errors import PermissionDenied
from app.tools.owner.types import OUTCOME_PARTIAL, ToolContext, ToolResult

_REFRESH_ACTION_RE = re.compile(
    r"^(?:\b(?:refresh|update|reload|sync|re[ -]?ingest)\b|"
    r"(?:לעדכן|לרענן|לסנכרן|תעדכן|תעדכני|עדכן|עדכני|תרענן|תרענני|רענן|רענני|"
    r"סנכרן|סנכרני))",
    re.IGNORECASE,
)
_WEBSITE_RE = re.compile(r"(?:\b(?:website|site)\b|אתר|מהאתר|באתר)", re.IGNORECASE)
_KNOWLEDGE_RE = re.compile(
    r"(?:\b(?:knowledge|content|corpus|sources?)\b|ידע|תוכן|מקורות)", re.IGNORECASE
)
_DIRECT_REQUEST_PREFIX_RE = re.compile(
    r"^(?:(?:mia|מיה)\s*[,;:\-]?\s*)?"
    r"(?:(?:please|can you|could you|would you|i want you to|i'd like you to)\s+|"
    r"(?:בבקשה|נא|אפשר|תוכלי|תוכל|אני מבקש שת|אני רוצה שת)\s+)*",
    re.IGNORECASE,
)


def _has_explicit_website_knowledge_refresh_request(owner_text: str) -> bool:
    """Require the current owner's words to name both the action and its target."""
    text = owner_text.strip()
    if not text or text.startswith(("\"", "'", "`", "“", "‘")):
        return False
    direct_text = _DIRECT_REQUEST_PREFIX_RE.sub("", text, count=1)
    return bool(
        _REFRESH_ACTION_RE.search(direct_text)
        and _WEBSITE_RE.search(text)
        and _KNOWLEDGE_RE.search(text)
    )


def _report_line(report: IngestReport) -> str:
    if report.status == "ingested":
        return f"{report.source_id} ({report.chunks} chunks)"
    if report.status == "unchanged":
        return report.source_id
    detail = "empty source" if report.status == "empty" else "fetch failed"
    return f"{report.source_id} ({detail})"


def _summary(reports: list[IngestReport]) -> ToolResult:
    updated = [report for report in reports if report.status == "ingested"]
    unchanged = [report for report in reports if report.status == "unchanged"]
    failed = [report for report in reports if report.status not in {"ingested", "unchanged"}]
    lines = [
        f"Website knowledge refresh checked {len(reports)} configured sources.",
        "Updated: " + (", ".join(_report_line(report) for report in updated) or "none"),
        "Unchanged: " + (", ".join(_report_line(report) for report in unchanged) or "none"),
        "Failures: " + (", ".join(_report_line(report) for report in failed) or "none"),
    ]
    text = "\n".join(lines)
    if failed and len(failed) == len(reports):
        return ToolResult(ok=False, text=text, error="all configured knowledge sources failed")
    if failed:
        return ToolResult(ok=True, text=text, outcome=OUTCOME_PARTIAL)
    return ToolResult(ok=True, text=text)


def _refresh_website_knowledge(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Refresh only configured public sources after a current, explicit owner request."""
    del args
    try:
        authorize(
            KNOWLEDGE_REFRESH,
            principal=ctx.principal,
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="website knowledge refresh denied")
    actor_id = ctx.principal.actor_id.strip()
    owner_ids = ctx.settings.telegram_owner_user_id_set()
    if not (
        actor_id
        and actor_id.isascii()
        and actor_id.isdigit()
        and actor_id in owner_ids
    ):
        return ToolResult(ok=False, error="website knowledge refresh requires numeric owner access")
    if not _has_explicit_website_knowledge_refresh_request(ctx.owner_text):
        return ToolResult(
            ok=False,
            error="website knowledge refresh requires an explicit request in this owner message",
        )
    sources = ctx.settings.knowledge_source_list()
    if not ctx.settings.website_url.strip() or not sources:
        return ToolResult(ok=False, error="no website knowledge sources are configured")

    reports = ingest_website(
        ctx.brain,
        website_url=ctx.settings.website_url,
        sources=sources,
        fetcher=HttpDocumentFetcher(),
        embedding_port=ctx.embedding_port,
        force=False,
    )
    return _summary(reports)
