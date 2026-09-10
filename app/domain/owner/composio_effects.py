"""Finite safety registry for owner-requested Composio side effects.

The live catalog supplies schemas, but it does not decide whether Mia may mutate a
resource.  This module is the shared proposal/execution policy for the small set of
effects whose target semantics are known.  Unknown existing-object mutations remain
unavailable even after approval.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class EffectRoute(StrEnum):
    GENERIC_CREATE = "generic_create"
    SNAPSHOT_WRITE = "snapshot_write"
    NAMED_WORKFLOW = "named_workflow"
    DENIED = "denied"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ComposioEffect:
    slug: str
    route: EffectRoute
    workflow: str = ""
    reader_slug: str = ""
    identity_argument: str = ""
    coverage: str = ""


# This is also the auditable coverage manifest.  Entries are exact documented slugs;
# pattern fallbacks below only preserve generic creation and a discovered LinkedIn
# own-profile editor with the already-pinned current-profile reader.
COMPOSIO_EFFECT_COVERAGE: tuple[ComposioEffect, ...] = (
    ComposioEffect(
        "GMAIL_CREATE_EMAIL_DRAFT",
        EffectRoute.NAMED_WORKFLOW,
        workflow="gmail_create_draft",
        coverage="supported through the typed draft proposal",
    ),
    ComposioEffect(
        "GMAIL_SEND_DRAFT",
        EffectRoute.SNAPSHOT_WRITE,
        reader_slug="GMAIL_GET_DRAFT",
        identity_argument="draft_id",
        coverage="supported after exact approval and a fresh draft read",
    ),
    ComposioEffect(
        "GMAIL_SEND_EMAIL",
        EffectRoute.UNAVAILABLE,
        workflow="gmail_create_draft_then_send",
        coverage="use the draft then approved-send workflow",
    ),
    ComposioEffect(
        "GMAIL_REPLY_TO_THREAD",
        EffectRoute.UNAVAILABLE,
        workflow="gmail_create_draft_then_send",
        coverage="direct reply is unavailable; use an exact draft",
    ),
    ComposioEffect(
        "GMAIL_FORWARD_MESSAGE",
        EffectRoute.UNAVAILABLE,
        workflow="gmail_create_draft_then_send",
        coverage="direct forward is unavailable; use an exact draft",
    ),
    ComposioEffect(
        "GOOGLECALENDAR_CREATE_EVENT",
        EffectRoute.NAMED_WORKFLOW,
        workflow="calendar_create_meeting",
        coverage="supported through the typed calendar proposal",
    ),
    ComposioEffect(
        "GOOGLECALENDAR_PATCH_EVENT",
        EffectRoute.NAMED_WORKFLOW,
        workflow="calendar_reschedule",
        coverage="supported through typed read-before-reschedule",
    ),
    ComposioEffect(
        "GOOGLESHEETS_VALUES_UPDATE",
        EffectRoute.NAMED_WORKFLOW,
        workflow="sheets_update",
        coverage="bounded Sheet writes; Contacts routes through CRM",
    ),
    ComposioEffect(
        "GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND",
        EffectRoute.NAMED_WORKFLOW,
        workflow="sheets_append",
        coverage="bounded Sheet writes; Contacts routes through CRM",
    ),
    ComposioEffect(
        "GOOGLESHEETS_UPSERT_ROWS",
        EffectRoute.NAMED_WORKFLOW,
        workflow="crm_upsert",
        coverage="CRM writes require the typed CRM identity workflow",
    ),
    ComposioEffect(
        "WHATSAPP_SEND_MESSAGE",
        EffectRoute.DENIED,
        coverage="retired agent transport",
    ),
    ComposioEffect(
        "WHATSAPP_SEND_TEMPLATE_MESSAGE",
        EffectRoute.DENIED,
        coverage="retired agent transport",
    ),
)

_EXACT = {item.slug: item for item in COMPOSIO_EFFECT_COVERAGE}
_META_ADS_PROVIDER_ALIASES = frozenset({"METAADS", "FACEBOOKADS"})
_META_ADS_SLUG_PREFIXES = (
    "METAADS_",
    "FACEBOOKADS_",
    "META_ADS_",
    "FACEBOOK_ADS_",
)
_META_ADS_READ_WORDS = frozenset(
    {
        "CHECK",
        "COUNT",
        "DESCRIBE",
        "DOWNLOAD",
        "FETCH",
        "FIND",
        "GET",
        "LIST",
        "LOOKUP",
        "PREVIEW",
        "QUERY",
        "READ",
        "RETRIEVE",
        "SEARCH",
        "VIEW",
    }
)
_META_ADS_MUTATION_WORDS = frozenset(
    {
        "AD",
        "ADS",
        "BID",
        "BUDGET",
        "CAMPAIGN",
        "CREATE",
        "EDIT",
        "ENABLE",
        "DISABLE",
        "LAUNCH",
        "PAUSE",
        "PATCH",
        "POST",
        "PUBLISH",
        "SET",
        "START",
        "STOP",
        "UPDATE",
    }
)
_EXISTING_MUTATIONS = frozenset(
    {
        "ACCEPT", "ACKNOWLEDGE", "APPROVE", "ARCHIVE", "ASSIGN", "ATTACH",
        "BLOCK", "CANCEL", "CLOSE", "COMMENT", "COMPLETE", "DECLINE", "DELETE",
        "DISABLE", "EDIT", "ENABLE", "JOIN", "LEAVE", "MARK", "MERGE", "MOVE",
        "PATCH", "REJECT", "REMOVE", "RESTORE", "RESCHEDULE", "REVOKE", "SET",
        "SUBSCRIBE", "TERMINATE", "UNSUBSCRIBE", "UPDATE",
    }
)


def is_prohibited_meta_ads_mutation(slug: str, toolkit: str = "") -> bool:
    """Recognize Meta Ads mutations before generic approval can bind them.

    Composio has exposed both ``METAADS`` and ``FACEBOOKADS`` names.  Read-like
    operations remain eligible for their separately verified read routes; every
    operation without a read verb that targets an ads mutation stays prohibited.
    """
    action = slug.strip().upper()
    provider = toolkit.strip().upper().replace("_", "")
    aliases = _META_ADS_PROVIDER_ALIASES
    is_meta_ads = provider in aliases or action.startswith(_META_ADS_SLUG_PREFIXES)
    if not is_meta_ads:
        return False
    words = frozenset(action.split("_"))
    if words & _META_ADS_READ_WORDS:
        return False
    return bool(words & _META_ADS_MUTATION_WORDS)


def composio_effect(slug: str, toolkit: str = "") -> ComposioEffect:
    action = slug.strip().upper()
    provider = toolkit.strip().upper()
    if not provider and "_" in action:
        provider = action.split("_", 1)[0]
    if is_prohibited_meta_ads_mutation(action, provider):
        return ComposioEffect(
            action,
            EffectRoute.DENIED,
            coverage="Meta Ads mutations are prohibited regardless of approval",
        )
    exact = _EXACT.get(action)
    if exact is not None:
        return exact
    words = frozenset(action.split("_"))
    if provider == "WHATSAPP":
        return ComposioEffect(
            action,
            EffectRoute.DENIED,
            coverage="retired agent transport",
        )
    if provider == "INSTAGRAM" and words & (
        _EXISTING_MUTATIONS | {"CREATE", "POST", "PUBLISH"}
    ):
        return ComposioEffect(action, EffectRoute.DENIED, coverage="Instagram is analytics-only")
    if provider == "LINKEDIN":
        if words & {"MESSAGE", "DM", "INMAIL"}:
            return ComposioEffect(
                action,
                EffectRoute.DENIED,
                coverage="direct messages unavailable",
            )
    if "CREATE" in words and not words & _EXISTING_MUTATIONS:
        return ComposioEffect(action, EffectRoute.GENERIC_CREATE, coverage="new resource")
    return ComposioEffect(
        action,
        EffectRoute.UNAVAILABLE,
        coverage="no typed current-resource reader",
    )


class _SnapshotCatalog(Protocol):
    def detail(self, slug: str): ...
    def execute_read(
        self, tool, arguments: dict[str, Any], *, connected_account_id: str | None = None
    ) -> dict[str, Any] | None: ...


def snapshot_effect_target(
    catalog: _SnapshotCatalog,
    effect: ComposioEffect,
    arguments: Mapping[str, Any],
    *,
    connected_account_id: str,
) -> dict[str, Any] | None:
    """Read and hash the exact current resource without persisting provider content."""
    if effect.route is not EffectRoute.SNAPSHOT_WRITE or not effect.reader_slug:
        return None
    reader_arguments: dict[str, Any] = {}
    identity: dict[str, str] = {}
    if effect.identity_argument:
        value = arguments.get(effect.identity_argument)
        if not isinstance(value, str) or not value.strip():
            return None
        identity[effect.identity_argument] = value.strip()
        reader_arguments[effect.identity_argument] = value.strip()
    reader = catalog.detail(effect.reader_slug)
    if reader is None:
        return None
    response = catalog.execute_read(
        reader,
        reader_arguments,
        connected_account_id=connected_account_id,
    )
    if not isinstance(response, dict) or response.get("successful") is not True:
        return None
    data = response.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return None
    if not isinstance(data, dict) or not data:
        return None
    if effect.slug == "GMAIL_SEND_DRAFT":
        requested = identity.get("draft_id", "")
        draft = data.get("draft")
        draft = draft if isinstance(draft, dict) else {}
        returned = next(
            (
                value.strip()
                for value in (
                    data.get("id"),
                    data.get("draft_id"),
                    data.get("draftId"),
                    draft.get("id"),
                    draft.get("draft_id"),
                    draft.get("draftId"),
                )
                if isinstance(value, str) and value.strip()
            ),
            "",
        )
        if not returned or returned != requested:
            return None
    try:
        encoded = json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError):
        return None
    if not encoded or len(encoded) > 128 * 1024:
        return None
    return {
        "reader_slug": effect.reader_slug,
        "identity": identity,
        "state_hash": hashlib.sha256(encoded).hexdigest(),
    }
