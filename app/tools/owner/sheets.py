"""Owner Sheets tools and the deterministic Sheets write authorization grammar.

The grammar below is a security boundary, not formatting: it binds the mutation
target, validates the A1 range, requires every payload cell as an owner-stated JSON
literal, checks explicit non-negated intent, and runs before idempotency claiming and
the adapter. It is kept together on purpose.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from app.capabilities.policy import authorize, execute_capability
from app.capabilities.sheets import sheets_handlers, validate_sheets_write_args
from app.core.errors import InvalidArguments, PermissionDenied
from app.domain.tools import AdapterHttpError
from app.integrations.sheets import DisabledSheetsPort, SheetsPort, build_sheets_port
from app.tools.owner.types import (
    _NOT_CONNECTED,
    ToolContext,
    ToolResult,
    _crm_spreadsheet_id,
    _house_unavailable,
)


def _sheet_args(*, include_values: bool) -> dict[str, Any]:
    spreadsheet_description = (
        "Optional. Defaults to Assaf's locked Contacts workbook. Never ask him for a URL."
    )
    properties: dict[str, Any] = {
        "spreadsheet_id": {
            "type": ["string", "null"],
            "description": spreadsheet_description,
        },
        "range": {
            "type": ["string", "null"] if not include_values else "string",
            "description": (
                "One bounded A1 range, for example Contacts!A1:N20. For reads only, null "
                "uses Contacts!A1:N20. Never 01 Leads."
            ),
        },
    }
    # Strict tool schemas require every property in `required`; a null read range is the
    # explicit lazy-user default. Writes still reject null and URLs in deterministic code.
    required = ["spreadsheet_id", "range"]
    if include_values:
        properties["values"] = {
            "type": "array",
            "items": {"type": "array", "items": {"type": "string"}},
            "description": "1-20 explicit rows of up to 10 literal cells; formulas are forbidden.",
        }
        required.append("values")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _owner_sheets_port(ctx: ToolContext) -> SheetsPort | None:
    port = ctx.sheets
    if port is None:
        port = build_sheets_port(ctx.settings)
    return None if isinstance(port, DisabledSheetsPort) else port


def _sheets_read(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    filled = dict(args)
    if not str(filled.get("spreadsheet_id") or "").strip():
        filled["spreadsheet_id"] = _crm_spreadsheet_id(ctx)
    port = _owner_sheets_port(ctx)
    if port is None:
        return _house_unavailable(ctx, "Sheets")
    try:
        out = execute_capability(
            "sheets.read",
            principal=ctx.principal,
            args=filled,
            handlers=sheets_handlers(
                port, allowed_spreadsheet_ids=ctx.settings.allowed_sheets_spreadsheet_ids()
            ),
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="sheets read denied")
    except AdapterHttpError as exc:
        return ToolResult(ok=False, error=f"Sheets read unavailable ({exc.tool_status()})")
    except (RuntimeError, ValueError, OSError):
        return ToolResult(ok=False, error="Sheets read failed")
    rows = out.get("rows") or []
    if not rows:
        return ToolResult(ok=True, text="The requested Sheet range is empty.")
    return ToolResult(ok=True, text="Sheet values:\n" + "\n".join(" | ".join(row) for row in rows))


def _sheets_list_tabs(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    filled = dict(args)
    if not str(filled.get("spreadsheet_id") or "").strip():
        filled["spreadsheet_id"] = _crm_spreadsheet_id(ctx)
    port = _owner_sheets_port(ctx)
    if port is None:
        return _house_unavailable(ctx, "Sheets")
    try:
        out = execute_capability(
            "sheets.list_tabs",
            principal=ctx.principal,
            args=filled,
            handlers=sheets_handlers(
                port, allowed_spreadsheet_ids=ctx.settings.allowed_sheets_spreadsheet_ids()
            ),
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="sheets tab discovery denied")
    except AdapterHttpError as exc:
        return ToolResult(ok=False, error=f"Sheets tab discovery unavailable ({exc.tool_status()})")
    except (RuntimeError, ValueError, OSError):
        return ToolResult(ok=False, error="Sheets tab discovery failed")
    tabs = out.get("tabs") or []
    if not tabs:
        return ToolResult(ok=True, text="No visible tabs were returned for this Sheet.")
    return ToolResult(ok=True, text="Sheet tabs: " + " | ".join(tabs))


def _sheets_write(ctx: ToolContext, args: dict[str, Any], *, append: bool) -> ToolResult:
    allowed_spreadsheet_ids = ctx.settings.allowed_sheets_spreadsheet_ids()
    if not _has_bound_sheets_write_request(
        ctx.owner_text,
        args,
        append=append,
        allowed_spreadsheet_ids=allowed_spreadsheet_ids,
    ):
        operation = "append" if append else "update"
        return ToolResult(
            ok=False,
            error=(
                f"explicit Sheets {operation} must name the spreadsheet id and range, "
                "with every cell as a JSON-quoted literal"
            ),
        )
    if not ctx.source_ref.strip():
        return ToolResult(ok=False, error="Sheets write requires an owner event reference")
    name = "sheets.append" if append else "sheets.update"
    try:
        authorize(name, principal=ctx.principal, kill_switch=ctx.kill_switch)
        spreadsheet_id, a1_range, values = validate_sheets_write_args(
            args, allowed_spreadsheet_ids=allowed_spreadsheet_ids
        )
    except PermissionDenied:
        return ToolResult(ok=False, error="sheets write denied")
    except InvalidArguments:
        return ToolResult(ok=False, error="invalid Sheets write arguments")
    validated_args = {
        "spreadsheet_id": spreadsheet_id,
        "range": a1_range,
        "values": values,
    }
    port = _owner_sheets_port(ctx)
    if port is None:
        return ToolResult(ok=True, text=_NOT_CONNECTED)
    canonical = json.dumps(
        {"event": ctx.source_ref, "operation": name, "args": validated_args},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    key = sha256(canonical.encode("utf-8")).hexdigest()
    if not ctx.store.claim_operation(scope="owner_sheets_write", key=key):
        return ToolResult(
            ok=True, text="This exact Sheets write was already handled for this owner event."
        )
    try:
        out = execute_capability(
            name,
            principal=ctx.principal,
            args=validated_args,
            handlers=sheets_handlers(port, allowed_spreadsheet_ids=allowed_spreadsheet_ids),
            kill_switch=ctx.kill_switch,
        )
    except PermissionDenied:
        ctx.store.fail_operation(scope="owner_sheets_write", key=key)
        return ToolResult(ok=False, error="sheets write denied")
    except AdapterHttpError as exc:
        # An append may have reached Google before a transport failure. Keep the completed
        # claim so the same owner-event retry cannot duplicate it.
        ctx.store.complete_operation(
            scope="owner_sheets_write", key=key, result_json='{"ok":false}'
        )
        return ToolResult(ok=False, error=f"Sheets write unavailable ({exc.tool_status()})")
    except (RuntimeError, ValueError, OSError):
        ctx.store.fail_operation(scope="owner_sheets_write", key=key)
        return ToolResult(ok=False, error="Sheets write failed")
    ctx.store.complete_operation(scope="owner_sheets_write", key=key, result_json='{"ok":true}')
    count = int(out.get("appended" if append else "updated") or 0)
    return ToolResult(ok=True, text=f"{count} Sheet row(s) {'appended' if append else 'updated'}.")


def _sheets_update(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return _sheets_write(ctx, args, append=False)


def _sheets_append(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return _sheets_write(ctx, args, append=True)


def _has_explicit_sheets_write_request(owner_text: str, *, append: bool) -> bool:
    """Bind one unambiguous, non-negated Sheets operation outside cell literals."""
    text = _sheets_security_view(owner_text)
    sheets_reference = bool(
        re.search(r"\b(?:sheet|sheets|google\s+sheets)\b", text)
        or re.search(r"(?:^|\s)(?:[לב]?גיליון(?:\s+גוגל)?|שיטס)(?=$|\s)", text)
    )
    requested = _sheets_operation_mentions(text, append=append)
    other = _sheets_operation_mentions(text, append=not append)
    # A prohibition is never authorization. A second affirmative operation makes the
    # turn ambiguous: the model must not choose which mutation to perform.
    return (
        sheets_reference
        and requested.count == 1
        and not requested.negated
        and other.count == 0
        and not other.negated
    )


@dataclass(frozen=True)
class _SheetsOperationMentions:
    count: int
    negated: bool


def _sheets_operation_mentions(text: str, *, append: bool) -> _SheetsOperationMentions:
    """Classify explicit mutation verbs after quoted literals have been removed."""
    if append:
        english_verb = r"(?:append|add)"
        hebrew_verb = r"(?:הוסף|הכנס)"
    else:
        english_verb = r"(?:update|fill|enter)"
        hebrew_verb = r"(?:עדכן|מלא)"
    english = rf"\b{english_verb}\b"
    hebrew = rf"(?<![\u0590-\u05ff]){hebrew_verb}(?![\u0590-\u05ff])"
    negated = _has_explicit_sheets_negation(text)
    return _SheetsOperationMentions(
        count=len(re.findall(english, text)) + len(re.findall(hebrew, text)),
        negated=negated,
    )


_JSON_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"')


def _sheets_security_view(text: str, *, quoted_replacement: str = '""') -> str:
    """Mask JSON values, then normalize only the security-matching view.

    This view is deliberately never used to bind payload, spreadsheet ID, or A1 target
    values. Compatibility normalization exposes full-width ASCII; mark and format-control
    removal prevents invisible characters from hiding an instruction or collision.
    """
    masked = _JSON_STRING_RE.sub(quoted_replacement, text)
    normalized = "".join(
        char
        for char in unicodedata.normalize("NFKD", masked)
        if not (unicodedata.category(char).startswith("M") or unicodedata.category(char) == "Cf")
    ).casefold()
    # Grammar placeholders are deliberately uppercase ASCII between private-use guards.
    # Restore only those internal markers after casefolding; raw owner data is never used
    # from this view for target or payload binding.
    return (
        normalized.replace(_SHEETS_CELL.casefold(), _SHEETS_CELL)
        .replace(_SHEETS_ID.casefold(), _SHEETS_ID)
        .replace(_SHEETS_TARGET.casefold(), _SHEETS_TARGET)
    )


_SHEETS_EXPLICIT_NEGATION_RE = re.compile(
    r"\b(?:do\s+not|don['’]?t|never|not)\b|(?<![\u05d0-\u05ea])(?:אל|לא)(?![\u05d0-\u05ea])"
)


def _has_explicit_sheets_negation(text: str) -> bool:
    """Recognize standalone prohibitions despite visually inert marks and controls."""
    return bool(_SHEETS_EXPLICIT_NEGATION_RE.search(_sheets_security_view(text)))


def _has_bound_sheets_write_request(
    owner_text: str,
    args: dict[str, Any],
    *,
    append: bool,
    allowed_spreadsheet_ids: frozenset[str],
) -> bool:
    """Require the model's mutation target and literal payload to appear in this turn.

    The model chooses a pinned tool, but it may not choose a Sheet location or data the
    authenticated owner did not state. This runs before both idempotency claiming and the
    adapter, so a rejected invention has no persistent or external side effect.
    """
    if not _has_explicit_sheets_write_request(owner_text, append=append):
        return False
    if _has_raw_sheets_security_token(owner_text):
        return False
    binding = _sheet_write_binding(args)
    if binding is None:
        return False
    spreadsheet_id, a1_range, values = binding
    if not _has_exact_single_sheets_target(
        owner_text,
        spreadsheet_id=spreadsheet_id,
        a1_range=a1_range,
        allowed_spreadsheet_ids=allowed_spreadsheet_ids,
    ):
        return False
    quoted_cells = _quoted_literals(owner_text)
    if quoted_cells is None or not _has_authorized_sheets_cell_clause(
        owner_text,
        spreadsheet_id=spreadsheet_id,
        a1_range=a1_range,
        append=append,
    ):
        return False
    dimensions = _bounded_a1_dimensions(a1_range)
    if dimensions is None:
        return False
    rows, columns = dimensions
    if len(values) != rows or any(len(row) != columns for row in values):
        return False
    return quoted_cells == [cell for row in values for cell in row]


def _sheet_write_binding(args: dict[str, Any]) -> tuple[str, str, list[list[str]]] | None:
    spreadsheet_id = args.get("spreadsheet_id")
    a1_range = args.get("range")
    values = args.get("values")
    if not isinstance(spreadsheet_id, str) or not isinstance(a1_range, str):
        return None
    if not isinstance(values, list) or not values:
        return None
    if any(not isinstance(row, list) or not row for row in values):
        return None
    if any(not isinstance(cell, str) for row in values for cell in row):
        return None
    cells = [[cell.strip() for cell in row] for row in values]
    if any(not cell for row in cells for cell in row):
        return None
    return spreadsheet_id.strip(), a1_range.strip(), cells


_BOUNDED_A1_RANGE_RE = re.compile(
    r"^(?:[A-Za-z0-9 _-]{1,80}!)?([A-Z]{1,3})([1-9][0-9]{0,5})"
    r"(?::([A-Z]{1,3})([1-9][0-9]{0,5}))?$"
)


def _bounded_a1_dimensions(a1_range: str) -> tuple[int, int] | None:
    """Return the exact rectangular size of a syntactically bounded A1 target."""
    match = _BOUNDED_A1_RANGE_RE.fullmatch(a1_range)
    if match is None:
        return None
    start_column, start_row, end_column, end_row = match.groups()
    end_column = end_column or start_column
    end_row = end_row or start_row
    first_column = _a1_column_number(start_column)
    last_column = _a1_column_number(end_column)
    first_row = int(start_row)
    last_row = int(end_row)
    if last_column < first_column or last_row < first_row:
        return None
    return last_row - first_row + 1, last_column - first_column + 1


def _a1_column_number(column: str) -> int:
    value = 0
    for char in column:
        value = value * 26 + ord(char) - ord("A") + 1
    return value


# Keep English introducers case-insensitive without making the exact owner-stated A1
# target case-insensitive. The target itself must still match the tool arguments raw.
# A preceding approved introducer separated only by non-alphanumeric characters is
# ambiguous: that includes all punctuation (including LOW LINE), whitespace,
# symbols/emoji, marks, and format controls. LOW LINE is deliberately a separator
# here, but an actual following alphanumeric word (for example ``at_foo``) is not.
_SHEETS_TARGET_INTRO_RE = r"(?<!\w)(?:(?i:at|range)|את|בטווח|טווח)(?!\w)\s+"
_SHEETS_TARGET_INTRO_TAIL_RE = re.compile(
    r"(?<![^\W_])(?:(?i:at|range)|את|בטווח|טווח)(?![^\W_])[\W_]*$"
)

# An unquoted residual A1 reference makes the model-selected target ambiguous. Keep this
# ASCII-scoped and case-insensitive: exact selected targets and JSON string literals are
# deliberately never normalized or case-folded.
_A1_CELL = r"\$?(?i:[A-Z]{1,3})\$?[1-9][0-9]{0,5}"
_A1_COLUMN = r"\$?(?i:[A-Z]{1,3})"
_A1_ROW = r"\$?[1-9][0-9]{0,5}"
_A1_REFERENCE_RE = re.compile(
    rf"(?<!\w)(?:{_A1_CELL}(?::{_A1_CELL})?|{_A1_COLUMN}:{_A1_COLUMN}|{_A1_ROW}:{_A1_ROW})(?!\w)"
)


def _has_exact_single_sheets_target(
    owner_text: str,
    *,
    spreadsheet_id: str,
    a1_range: str,
    allowed_spreadsheet_ids: frozenset[str],
) -> bool:
    """Require one complete, unquoted, owner-stated A1 target in this turn."""
    unquoted_text = _JSON_STRING_RE.sub('""', owner_text)
    selected_target = re.compile(
        _SHEETS_TARGET_INTRO_RE + rf"(?P<target>{re.escape(a1_range)})(?![\w!:-])"
    )
    selected = list(selected_target.finditer(unquoted_text))
    if len(selected) != 1:
        return False
    if _SHEETS_TARGET_INTRO_TAIL_RE.search(unquoted_text[: selected[0].start()]):
        return False
    selected_start = selected[0].start("target")
    selected_end = selected[0].end("target")
    target_blanked = _blank_spans(unquoted_text, [(selected_start, selected_end)])
    mentioned_ids = {
        candidate
        for candidate in allowed_spreadsheet_ids
        if _has_complete_token(target_blanked, candidate)
    }
    # Spreadsheet IDs are exact opaque authorization data, but only one exact raw
    # mention may bind this write. Do not let repeated A1-looking ID tokens conceal
    # a second target. An overlapping selected bare range is not a second ID mention.
    id_matches = [
        match
        for match in _complete_token_matches(unquoted_text, spreadsheet_id)
        if match.end() <= selected_start or match.start() >= selected_end
    ]
    if len(id_matches) != 1:
        return False
    ignored_spans = [(selected_start, selected_end), id_matches[0].span()]
    remaining = _blank_spans(unquoted_text, ignored_spans)
    residual = "".join(
        char
        for char in remaining
        if not (unicodedata.category(char).startswith("M") or unicodedata.category(char) == "Cf")
    )
    return (
        mentioned_ids.issubset({spreadsheet_id})
        and _has_complete_token(unquoted_text, spreadsheet_id)
        and not _A1_REFERENCE_RE.search(residual)
    )


def _quoted_literals(text: str) -> list[str] | None:
    """Decode every quoted candidate; malformed/non-string JSON fails closed."""
    literals: list[str] = []
    covered = [False] * len(text)
    for match in _JSON_STRING_RE.finditer(text):
        start, end = match.span()
        covered[start:end] = [True] * (end - start)
        try:
            value = json.loads(match.group())
        except json.JSONDecodeError:
            return None
        if not isinstance(value, str):
            return None
        literals.append(value)
    if any(char == '"' and not covered[index] for index, char in enumerate(text)):
        return None
    return literals


_SHEETS_SENTINEL_START = "\ue000"
_SHEETS_SENTINEL_END = "\ue001"
_SHEETS_CELL = f"{_SHEETS_SENTINEL_START}C{_SHEETS_SENTINEL_END}"
_SHEETS_ID = f"{_SHEETS_SENTINEL_START}I{_SHEETS_SENTINEL_END}"
_SHEETS_TARGET = f"{_SHEETS_SENTINEL_START}T{_SHEETS_SENTINEL_END}"
_SHEETS_READABLE_SENTINEL_RE = re.compile(r"(?<!\w)(?:CELL|ID|TARGET)(?!\w)", re.IGNORECASE)
_SHEETS_ENGLISH_CELL_LIST = (
    rf"{_SHEETS_CELL}(?:\s*(?:,|;|\b(?:and|or|plus|with)\b)\s*{_SHEETS_CELL})*"
)
_SHEETS_HEBREW_CELL_LIST = rf"{_SHEETS_CELL}(?:\s*(?:,|;|ו(?:[-\u05be])?)\s*{_SHEETS_CELL})*"


def _has_raw_sheets_security_token(text: str) -> bool:
    """Reject public grammar placeholders and private sentinels outside JSON data."""
    unquoted = _sheets_security_view(text)
    return (
        _SHEETS_SENTINEL_START in unquoted
        or _SHEETS_SENTINEL_END in unquoted
        or bool(_SHEETS_READABLE_SENTINEL_RE.search(unquoted))
    )


def _has_authorized_sheets_cell_clause(
    text: str,
    *,
    spreadsheet_id: str,
    a1_range: str,
    append: bool,
) -> bool:
    """Accept only one complete, explicit Sheets mutation request.

    This is a positive authorization grammar over a security view: exact JSON strings,
    the selected ID, and the selected target become sentinels, while the raw request is
    never normalized for target or literal comparison elsewhere. Anything left in a
    value slot is therefore an unquoted extra cell and fails closed.
    """
    if _has_raw_sheets_security_token(text):
        return False
    # Assign raw ID/target roles before normalizing. Thus the security view can expose
    # disguised instructions without ever authorizing a normalized ID, A1 target, or
    # quoted payload value.
    view = _JSON_STRING_RE.sub(f" {_SHEETS_CELL} ", text)
    english_verb = r"(?:append|add)" if append else r"(?:update|fill|enter)"
    hebrew_verb = r"(?:הוסף|הכנס)" if append else r"(?:עדכן|מלא)"
    english_values_first = re.compile(
        rf"(?:the )?{_SHEETS_ENGLISH_CELL_LIST}(?:\s+to)?\s+{_SHEETS_ID}\s+"
        rf"(?:at|range)\s+{_SHEETS_TARGET}(?:\s+in\s+(?:the\s+)?(?:google\s+)?sheets?)?",
        re.IGNORECASE,
    )
    english_target_first = re.compile(
        rf"(?:the )?{_SHEETS_ID}\s+(?:at|range)\s+{_SHEETS_TARGET}\s+with\s+"
        rf"{_SHEETS_ENGLISH_CELL_LIST}"
        r"(?:\s+in\s+(?:the\s+)?(?:google\s+)?sheets?)?",
        re.IGNORECASE,
    )
    hebrew_values_first = re.compile(
        rf"(?:את\s+)?{_SHEETS_HEBREW_CELL_LIST}\s+(?:לגיליון(?:\s+גוגל)?|בגיליון(?:\s+גוגל)?|שיטס)\s+{_SHEETS_ID}\s+"
        rf"(?:בטווח|טווח)\s+{_SHEETS_TARGET}"
    )
    hebrew_target_first = re.compile(
        rf"את\s+{_SHEETS_TARGET}\s+(?:בגיליון(?:\s+גוגל)?|לגיליון)\s+{_SHEETS_ID}\s+ב-\s*{_SHEETS_HEBREW_CELL_LIST}"
    )
    # These are the only product-positive prefaces already exercised by owner requests.
    # The punctuation in the longer English form is intentional: unknown prose never
    # becomes authorization merely because a later suffix resembles a valid operation.
    harmless_preface = r"(?:(?:please\s+record\s+this\s+now:\s+)|(?:please\s+)|(?:אלופה\s+))?"
    for raw_request_view in _sheets_clause_views(view, spreadsheet_id, a1_range):
        request_view = _sheets_security_view(raw_request_view, quoted_replacement='""')
        if (
            re.fullmatch(
                rf"{harmless_preface}\b{english_verb}\b\s+{english_values_first.pattern}",
                request_view,
                re.IGNORECASE,
            )
            or re.fullmatch(
                rf"{harmless_preface}\b{english_verb}\b\s+{english_target_first.pattern}",
                request_view,
                re.IGNORECASE,
            )
            or re.fullmatch(
                rf"{harmless_preface}(?<![\u0590-\u05ff]){hebrew_verb}(?![\u0590-\u05ff])\s+{hebrew_values_first.pattern}",
                request_view,
            )
            or re.fullmatch(
                rf"{harmless_preface}(?<![\u0590-\u05ff]){hebrew_verb}(?![\u0590-\u05ff])\s+{hebrew_target_first.pattern}",
                request_view,
            )
        ):
            return True
    return False


def _sheets_clause_views(view: str, spreadsheet_id: str, a1_range: str) -> list[str]:
    """Create grammar views with ID/target roles assigned by the matched word order."""
    if spreadsheet_id != a1_range:
        return [
            re.sub(
                r"\s+",
                " ",
                _replace_complete_tokens(
                    _replace_complete_tokens(view, spreadsheet_id, _SHEETS_ID),
                    a1_range,
                    _SHEETS_TARGET,
                ),
            ).strip()
        ]
    matches = _complete_token_matches(view, spreadsheet_id)
    if len(matches) != 2:
        return []
    return [
        re.sub(r"\s+", " ", _replace_token_spans(view, matches, roles)).strip()
        for roles in ((_SHEETS_ID, _SHEETS_TARGET), (_SHEETS_TARGET, _SHEETS_ID))
    ]


def _replace_token_spans(
    text: str, matches: list[re.Match[str]], replacements: tuple[str, str]
) -> str:
    chars = list(text)
    for match, replacement in reversed(list(zip(matches, replacements, strict=True))):
        chars[match.start() : match.end()] = f" {replacement} "
    return "".join(chars)


def _replace_complete_tokens(text: str, target: str, replacement: str) -> str:
    return re.sub(
        rf"(?<![\w!:-]){re.escape(target)}(?![\w!:-])",
        f" {replacement} ",
        text,
    )


def _replace_nth_complete_token(
    text: str, target: str, replacement: str, *, occurrence: int
) -> str:
    matches = _complete_token_matches(text, target)
    if len(matches) < occurrence:
        return text
    start, end = matches[occurrence - 1].span()
    return text[:start] + f" {replacement} " + text[end:]


def _has_complete_token(text: str, target: str) -> bool:
    """Do not let a target be authorized by a prefix/suffix of a longer ID or A1 range."""
    if not target:
        return False
    return bool(_complete_token_matches(text, target))


def _complete_token_matches(text: str, target: str) -> list[re.Match[str]]:
    """Find exact raw tokens without treating a prefix/suffix as authorization."""
    if not target:
        return []
    return list(re.finditer(rf"(?<![\w!:-]){re.escape(target)}(?![\w!:-])", text))


def _blank_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace exact spans without shifting offsets or masking unrelated text."""
    chars = list(text)
    for start, end in spans:
        chars[start:end] = " " * (end - start)
    return "".join(chars)
