"""Owner-only, on-demand Composio tool discovery.

The model is deliberately given three small meta-tools instead of a provider catalog.  This
module obtains the catalog only for ACTIVE connections owned by ``MIA_COMPOSIO_USER_ID``,
fetches a selected tool's current input schema, validates arguments locally, and executes
only deterministic read operations.  Writes are classified in Python and intentionally
stop at the existing approval workflow boundary; a provider schema or an owner utterance
never grants an action a lower risk.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from time import monotonic
from typing import Any

import httpx

from app.core.config import Settings
from app.core.risk import RiskLevel

_BASE = "https://backend.composio.dev/api/v3.1"
_TIMEOUT = 20.0
_MAX_RESULTS = 12
_MAX_SCHEMA_CHARS = 12_000
_MAX_RESULT_CHARS = 3_000
_PAGE_LIMIT = 500
_CACHE_TTL_SECONDS = 300.0
_SLUG_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,160}$")
_READ_WORDS = frozenset(
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
_DENY_WORDS = frozenset({"DELETE", "REMOVE", "REVOKE", "CLEAR", "TERMINATE", "DESTROY"})
_MARKETING_WORDS = frozenset(
    {"PUBLISH", "POST", "CAMPAIGN", "AD", "ADS", "BUDGET", "BID"}
)
_WRITE_WORDS = frozenset(
    {
        "ACCEPT",
        "ACKNOWLEDGE",
        "ADD",
        "APPROVE",
        "ARCHIVE",
        "ASSIGN",
        "ATTACH",
        "BLOCK",
        "CANCEL",
        "CLOSE",
        "COMMENT",
        "COMPLETE",
        "COMMIT",
        "COPY",
        "CREATE",
        "DECLINE",
        "DISABLE",
        "EDIT",
        "ENABLE",
        "EXECUTE",
        "FOLLOW",
        "FORWARD",
        "FORK",
        "GENERATE",
        "IMPORT",
        "INVITE",
        "JOIN",
        "LEAVE",
        "LIKE",
        "LOCK",
        "MARK",
        "MERGE",
        "MESSAGE",
        "MOVE",
        "MUTE",
        "PATCH",
        "PAUSE",
        "PAY",
        "PIN",
        "REACT",
        "REOPEN",
        "REPLY",
        "RESCHEDULE",
        "SEND",
        "SET",
        "SHARE",
        "SCHEDULE",
        "STAR",
        "START",
        "STOP",
        "SUBSCRIBE",
        "TRIGGER",
        "UNARCHIVE",
        "UNBLOCK",
        "UNFOLLOW",
        "UNLOCK",
        "UNLIKE",
        "UNMUTE",
        "UNPIN",
        "UNSUBSCRIBE",
        "UPDATE",
        "UPLOAD",
        "VOTE",
        "WATCH",
        "WRITE",
    }
)
_COMPOUND_WORDS = frozenset({"AND", "OR", "THEN"})

# Official destructive slugs. There is no Composio tool named delete-lead;
# GOOGLESHEETS_DELETE_DIMENSION is the product meaning of deleting a lead row.
# There is no GMAIL_DELETE_FOREVER slug; the delete-forever class below is the
# official permanent-delete set. Recoverable trash (GMAIL_MOVE_TO_TRASH /
# GMAIL_MOVE_THREAD_TO_TRASH) is not in this set.
DENIED_COMPOSIO_SLUGS: frozenset[str] = frozenset(
    {
        "GOOGLESHEETS_DELETE_DIMENSION",
        "GOOGLESHEETS_CLEAR_VALUES",
        "GOOGLESHEETS_SPREADSHEETS_VALUES_BATCH_CLEAR",
        "GOOGLESHEETS_BATCH_CLEAR_VALUES_BY_DATA_FILTER",
        "GOOGLESHEETS_DELETE_SHEET",
        "GOOGLESHEETS_DELETE_CHART",
        "GOOGLESHEETS_EXECUTE_SQL",
        "INSTAGRAM_DELETE_COMMENT",
        "INSTAGRAM_DELETE_MESSAGGER_PROFILE",
        "LINKEDIN_DELETE_LINKED_IN_POST",
        "LINKEDIN_DELETE_POST",
        "LINKEDIN_DELETE_UGC_POST",
        "GMAIL_DELETE_MESSAGE",
        "GMAIL_BATCH_DELETE_MESSAGES",
        "GMAIL_DELETE_THREAD",
        "GMAIL_DELETE_DRAFT",
        "GMAIL_DELETE_FILTER",
        "GMAIL_DELETE_LABEL",
        "GOOGLE_SEARCH_CONSOLE_DELETE_SITE",
    }
)

# Official Gmail send slugs. Owner Telegram may use the named draft/approve
# path (GMAIL_SEND_DRAFT). Generic catalog execute, cron, and visitors must
# never auto-fire any of these. There is no slug GMAIL_SEND.
OWNER_REQUESTED_GMAIL_SEND_SLUGS: frozenset[str] = frozenset(
    {
        "GMAIL_SEND_EMAIL",
        "GMAIL_SEND_DRAFT",
        "GMAIL_REPLY_TO_THREAD",
        "GMAIL_FORWARD_MESSAGE",
    }
)

# Send-like slugs that exist on allowlisted apps but must not auto-fire.
NEVER_AUTO_SEND_SLUGS: frozenset[str] = OWNER_REQUESTED_GMAIL_SEND_SLUGS | {
    "GOOGLE_ANALYTICS_SEND_EVENTS",
}

# Adapter-pinned Sheets writes already in this repo. Classifier treats them as
# low-risk writes, not destructive. Generic catalog execute still refuses them
# so they keep the named allowlisted Sheets path.
SHEETS_BOUNDED_WRITE_SLUGS: frozenset[str] = frozenset(
    {
        "GOOGLESHEETS_UPSERT_ROWS",
        "GOOGLESHEETS_VALUES_UPDATE",
        "GOOGLESHEETS_SPREADSHEETS_VALUES_APPEND",
    }
)

# Publish exists on the official pages. These must never auto-fire; LinkedIn
# non-delete writes already have a named Telegram approval path.
NEVER_AUTO_PUBLISH_SLUGS: frozenset[str] = frozenset(
    {
        "INSTAGRAM_POST_IG_USER_MEDIA",
        "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
        "INSTAGRAM_CREATE_MEDIA_CONTAINER",
        "INSTAGRAM_CREATE_POST",
        "LINKEDIN_CREATE_LINKED_IN_POST",
        "LINKEDIN_POST_UPDATE",
    }
)


@dataclass(frozen=True)
class CatalogTool:
    slug: str
    toolkit: str
    description: str
    input_schema: dict[str, Any]
    version: str = ""


class ComposioCatalog:
    """Small REST adapter. Results are cached process-wide, never in prompts."""

    _toolkits_cache: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}
    _search_cache: dict[
        tuple[str, str, tuple[str, ...], str], tuple[float, tuple[CatalogTool, ...]]
    ] = {}
    _detail_cache: dict[tuple[str, str, str], tuple[float, CatalogTool | None]] = {}

    def __init__(self, *, api_key: str, user_id: str, client: httpx.Client | None = None) -> None:
        self._api_key, self._user_id = api_key, user_id
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=_TIMEOUT)
        self._active_lookup_authoritative = False
        # A Composio user id is scoped by project. Namespace process caches by a
        # one-way API-key fingerprint so two projects using the same user id cannot
        # share authorization/catalog state. The credential itself is never stored in
        # a cache key, result, prompt, or log.
        self._project_cache_key = sha256(api_key.encode("utf-8")).hexdigest()[:16]

    def __enter__(self) -> ComposioCatalog:
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._owns_client:
            self._client.close()

    @classmethod
    def from_settings(cls, settings: Settings) -> ComposioCatalog | None:
        if not settings.composio_ready():
            return None
        return cls(
            api_key=settings.composio_api_key.strip(), user_id=settings.composio_user_id.strip()
        )

    @classmethod
    def reset_cache(cls) -> None:
        cls._toolkits_cache.clear()
        cls._search_cache.clear()
        cls._detail_cache.clear()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any] | None:
        headers = {"x-api-key": self._api_key}
        try:
            response = self._client.request(
                method, f"{_BASE}{path}", headers=headers, **kwargs
            )
        except httpx.HTTPError:
            return None
        if response.status_code >= 400:
            return None
        try:
            body = response.json()
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def active_toolkits(self) -> tuple[str, ...]:
        cache_key = (self._project_cache_key, self._user_id)
        cached = self._toolkits_cache.get(cache_key)
        if cached is not None and cached[0] > monotonic():
            self._active_lookup_authoritative = True
            return cached[1]
        self._toolkits_cache.pop(cache_key, None)
        names: list[str] = []
        cursor = ""
        seen_cursors: set[str] = set()
        while True:
            params = {
                "user_ids": self._user_id,
                "statuses": "ACTIVE",
                "limit": str(_PAGE_LIMIT),
            }
            if cursor:
                params["cursor"] = cursor
            body = self._request("GET", "/connected_accounts", params=params)
            if body is None:
                self._active_lookup_authoritative = False
                return ()
            for item in (body or {}).get("items", []):
                if not isinstance(item, dict):
                    continue
                # Filters are a convenience, never the authorization boundary. Validate
                # the documented response fields in case an API/proxy ignores a query.
                if (
                    item.get("user_id") != self._user_id
                    or item.get("status") != "ACTIVE"
                    or item.get("is_disabled") is not False
                ):
                    continue
                toolkit = item.get("toolkit") if isinstance(item, dict) else None
                slug = toolkit.get("slug") if isinstance(toolkit, dict) else None
                if isinstance(slug, str) and slug.strip():
                    names.append(slug.strip().upper())
            next_cursor = (body or {}).get("next_cursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        result = tuple(dict.fromkeys(names))
        self._toolkits_cache[cache_key] = (monotonic() + _CACHE_TTL_SECONDS, result)
        self._active_lookup_authoritative = True
        return result

    @staticmethod
    def _schema(item: dict[str, Any]) -> dict[str, Any]:
        for key in ("input_schema", "input_parameters", "parameters", "inputSchema"):
            candidate = item.get(key)
            if isinstance(candidate, dict):
                if isinstance(candidate.get("properties"), dict):
                    return candidate
                # Composio's v3/v3.1 tool endpoints publish ``input_parameters`` as
                # a property map whose individual definitions carry ``required``.
                # Normalize that documented wire shape into the object JSON-Schema
                # shape used by the local validator and owner meta-tool.
                properties: dict[str, Any] = {}
                required: list[str] = []
                for name, raw_definition in candidate.items():
                    if not isinstance(name, str) or not isinstance(raw_definition, dict):
                        return {}
                    definition = dict(raw_definition)
                    if definition.pop("required", False) is True:
                        required.append(name)
                    properties[name] = definition
                return {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                }
        return {}

    @classmethod
    def _tool(cls, item: dict[str, Any], toolkit: str) -> CatalogTool | None:
        slug = item.get("slug") or item.get("tool_slug")
        if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
            return None
        tool_toolkit = item.get("toolkit")
        if isinstance(tool_toolkit, dict):
            tool_toolkit = tool_toolkit.get("slug")
        resolved_toolkit = str(tool_toolkit or toolkit).upper()
        return CatalogTool(
            slug=slug,
            toolkit=resolved_toolkit,
            description=str(item.get("description") or item.get("name") or ""),
            input_schema=cls._schema(item),
            version=str(item.get("version") or ""),
        )

    def search(self, query: str, toolkit: str = "") -> tuple[CatalogTool, ...]:
        needle = query.strip().lower()
        active_toolkits = self.active_toolkits()
        if not self._active_lookup_authoritative:
            return ()
        toolkits = (toolkit.upper(),) if toolkit.strip() else active_toolkits
        active = frozenset(active_toolkits)
        cache_key = (self._project_cache_key, self._user_id, toolkits, needle)
        cached = self._search_cache.get(cache_key)
        if cached is not None and cached[0] > monotonic():
            return cached[1]
        self._search_cache.pop(cache_key, None)
        candidates: list[CatalogTool] = []
        complete = True
        for item in toolkits:
            if item not in active:
                continue
            body = self._request(
                "GET",
                "/tools",
                params={
                    "toolkit_slug": item,
                    "query": needle,
                    "limit": str(_MAX_RESULTS),
                    "include_deprecated": "false",
                },
            )
            if body is None:
                complete = False
                continue
            raw = body.get("items") or body.get("data") or []
            for row in raw:
                tool = self._tool(row, item) if isinstance(row, dict) else None
                if tool is None or tool.toolkit != item:
                    continue
                candidates.append(tool)
                if len(candidates) >= _MAX_RESULTS:
                    result = tuple(candidates)
                    if complete:
                        self._search_cache[cache_key] = (
                            monotonic() + _CACHE_TTL_SECONDS,
                            result,
                        )
                    return result
        result = tuple(candidates[:_MAX_RESULTS])
        if complete:
            self._search_cache[cache_key] = (monotonic() + _CACHE_TTL_SECONDS, result)
        return result

    def detail(self, slug: str) -> CatalogTool | None:
        if not _SLUG_RE.fullmatch(slug):
            return None
        key = (self._project_cache_key, self._user_id, slug)
        cached = self._detail_cache.get(key)
        if cached is not None and cached[0] > monotonic():
            return cached[1]
        self._detail_cache.pop(key, None)
        active = frozenset(self.active_toolkits())
        if not self._active_lookup_authoritative:
            return None
        body = self._request("GET", f"/tools/{slug}")
        if body is None:
            return None
        item = (body or {}).get("item") or (body or {}).get("data") or body
        detail = self._tool(item, "") if isinstance(item, dict) else None
        # The exact tool response must name one of this owner's independently verified
        # ACTIVE toolkits. A guessed slug from any other toolkit is not authorized.
        if (
            detail is None
            or detail.slug != slug
            or detail.toolkit not in active
            or not detail.input_schema
        ):
            detail = None
        self._detail_cache[key] = (monotonic() + _CACHE_TTL_SECONDS, detail)
        return detail

    def execute(self, tool: CatalogTool, arguments: dict[str, Any]) -> dict[str, Any] | None:
        payload: dict[str, Any] = {"user_id": self._user_id, "arguments": arguments}
        if tool.version:
            payload["version"] = tool.version
        body = self._request("POST", f"/tools/execute/{tool.slug}", json=payload)
        if body is None or body.get("successful") is not True:
            return None
        return body

    # Kept as the narrow read-facing name for existing callers.  Side-effect callers
    # must use the approval-bound domain workflow, never this compatibility method.
    def execute_read(self, tool: CatalogTool, arguments: dict[str, Any]) -> dict[str, Any] | None:
        return self.execute(tool, arguments)


def risk_for_slug(slug: str, toolkit: str = "") -> RiskLevel:
    """Conservative deterministic classification; unknown is approval, never read."""
    action = slug.upper()
    if action in DENIED_COMPOSIO_SLUGS:
        return RiskLevel.R5_DESTRUCTIVE
    if action in SHEETS_BOUNDED_WRITE_SLUGS:
        return RiskLevel.R1_LOW_WRITE
    toolkit_prefix = f"{toolkit.strip().upper()}_" if toolkit.strip() else ""
    if toolkit_prefix and action.startswith(toolkit_prefix):
        action = action[len(toolkit_prefix) :]
    words = frozenset(action.split("_"))
    if words & _DENY_WORDS:
        return RiskLevel.R5_DESTRUCTIVE
    if words & _MARKETING_WORDS:
        return RiskLevel.R4_FINANCIAL_MARKETING
    if words & _WRITE_WORDS:
        return RiskLevel.R3_COMMERCIAL
    # A compound slug can hide a second action after an innocent read verb (for
    # example LIST_AND_JOIN). Without provider risk metadata, refuse the ambiguity.
    if words & _COMPOUND_WORDS:
        return RiskLevel.R3_COMMERCIAL
    if words & _READ_WORDS:
        return RiskLevel.R0_READ
    return RiskLevel.R3_COMMERCIAL


def validate_arguments(schema: dict[str, Any], value: object, *, path: str = "arguments") -> str:
    """Validate common JSON-Schema constraints before the provider's final validation."""
    if not isinstance(value, dict):
        return f"{path} must be an object"
    if not schema:
        return "tool schema is missing"
    if not isinstance(schema.get("properties"), dict):
        return "tool schema has no object properties"
    return _validate_schema_value(schema, value, path)


def _validate_schema_value(schema: dict[str, Any], value: object, path: str) -> str:
    alternatives = schema.get("anyOf")
    if isinstance(alternatives, list) and alternatives:
        if not any(
            isinstance(option, dict) and not _validate_schema_value(option, value, path)
            for option in alternatives
        ):
            return f"{path} does not match any allowed schema"
    alternatives = schema.get("oneOf")
    if isinstance(alternatives, list) and alternatives:
        matches = sum(
            1
            for option in alternatives
            if isinstance(option, dict) and not _validate_schema_value(option, value, path)
        )
        if matches != 1:
            return f"{path} does not match exactly one allowed schema"
    if "const" in schema and value != schema["const"]:
        return f"{path} does not match the required value"
    allowed_values = schema.get("enum")
    if isinstance(allowed_values, list) and value not in allowed_values:
        return f"{path} is not an allowed value"
    allowed = schema.get("type")
    allowed_types = allowed if isinstance(allowed, list) else [allowed]
    if allowed is not None and not any(
        _matches_type(value, candidate) for candidate in allowed_types
    ):
        return f"{path} does not match the tool schema"
    if isinstance(value, str):
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if isinstance(minimum, int) and len(value) < minimum:
            return f"{path} is shorter than the tool schema allows"
        if isinstance(maximum, int) and len(value) > maximum:
            return f"{path} is longer than the tool schema allows"
        pattern = schema.get("pattern")
        if isinstance(pattern, str):
            try:
                if re.search(pattern, value) is None:
                    return f"{path} does not match the required pattern"
            except re.error:
                return f"{path} has an unsupported provider pattern"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, comparison, message in (
            ("minimum", lambda left, right: left < right, "is below the minimum"),
            ("maximum", lambda left, right: left > right, "is above the maximum"),
            (
                "exclusiveMinimum",
                lambda left, right: left <= right,
                "is below the exclusive minimum",
            ),
            (
                "exclusiveMaximum",
                lambda left, right: left >= right,
                "is above the exclusive maximum",
            ),
        ):
            bound = schema.get(key)
            if isinstance(bound, (int, float)) and comparison(value, bound):
                return f"{path} {message}"
    if isinstance(value, list):
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if isinstance(minimum, int) and len(value) < minimum:
            return f"{path} has too few items"
        if isinstance(maximum, int) and len(value) > maximum:
            return f"{path} has too many items"
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                problem = _validate_schema_value(item_schema, item, f"{path}[{index}]")
                if problem:
                    return problem
    if not isinstance(value, dict):
        return ""
    properties = schema.get("properties")
    if properties is None:
        return ""
    if not isinstance(properties, dict):
        return f"{path} has invalid object properties"
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(key, str) for key in required):
        return "tool schema has invalid required fields"
    for key in required:
        if key not in value:
            return f"{path}.{key} is required"
    if schema.get("additionalProperties") is False:
        extra = set(value) - set(properties)
        if extra:
            return f"{path} has unknown field {sorted(extra)[0]}"
    for key, item in value.items():
        definition = properties.get(key)
        if not isinstance(definition, dict):
            return f"{path}.{key} is not in the tool schema"
        problem = _validate_schema_value(definition, item, f"{path}.{key}")
        if problem:
            return problem
    return ""


def _matches_type(value: object, expected: object) -> bool:
    return {
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "array": isinstance(value, list),
        "object": isinstance(value, dict),
        "null": value is None,
    }.get(expected, False)


def schema_text(tool: CatalogTool) -> str | None:
    rendered = json.dumps(tool.input_schema, ensure_ascii=False, separators=(",", ":"))
    return rendered if len(rendered) <= _MAX_SCHEMA_CHARS else None


def bounded_result_text(value: dict[str, Any]) -> str:
    """Return valid bounded JSON while retaining continuation metadata."""
    rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(rendered) <= _MAX_RESULT_CHARS:
        return rendered
    continuation: dict[str, object] = {}
    _collect_continuation(value, continuation)
    preview_size = 1_800
    while preview_size >= 0:
        wrapper = json.dumps(
            {
                "truncated": True,
                "continuation": continuation,
                "preview": rendered[:preview_size],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(wrapper) <= _MAX_RESULT_CHARS:
            return wrapper
        preview_size -= 200
    return '{"truncated":true,"continuation":{},"preview":""}'


def _collect_continuation(
    value: object, output: dict[str, object], *, path: str = "", depth: int = 0
) -> None:
    if depth > 5 or len(output) >= 20:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            normalized = str(key).lower()
            if (
                normalized in {"next_cursor", "cursor", "next_page_token", "page_token"}
                or "has_more" in normalized
                or "has_next" in normalized
            ) and isinstance(item, (str, int, float, bool, type(None))):
                output[child_path] = item[:240] if isinstance(item, str) else item
            _collect_continuation(item, output, path=child_path, depth=depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value[:20]):
            _collect_continuation(item, output, path=f"{path}[{index}]", depth=depth + 1)
