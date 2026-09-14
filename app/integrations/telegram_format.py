"""Telegram message rendering.

Formatting is built here, not left to the model. The model writes prose; this module owns
structure, escaping, Hebrew date phrasing, length limits and buttons — so output is
consistent instead of however the LLM felt that turn.

**parse_mode is HTML, not MarkdownV2.** MarkdownV2 requires escaping 18 characters under
three different context-dependent rules, and every value this bot interpolates is a
landmine there: `lead_ab12` (underscore), `a.b@x.co.il` (dots), decimals, parentheses.
HTML needs exactly three characters escaped (`<`, `>`, `&`) under one uniform rule,
supports every entity MarkdownV2 does, and `html.escape` is stdlib. Hebrew codepoints are
above U+007F and unaffected by either scheme.

Bidi note: Telegram documents no RTL control for plain `sendMessage` (`is_rtl` exists only
on rich messages). A Hebrew line ending in a Latin/numeric token reorders visibly, so
LTR runs are wrapped in Unicode isolates. That is a Unicode-standard technique, not a
Telegram-documented one, and is worth eyeballing on a real client.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from html import escape
from zoneinfo import ZoneInfo

# sendMessage: "1-4096 characters after entities parsing". Overflow behaviour is not
# documented, so chunk client-side rather than relying on the server.
MAX_MESSAGE_CHARS = 4096
# Leave room for the continuation marker when splitting.
_CHUNK_BUDGET = 3900
# callback_data is "1-64 bytes"; Hebrew is 2 bytes/char in UTF-8, so payloads stay ASCII.
MAX_CALLBACK_BYTES = 64

# Unicode bidi isolates. FSI...PDI keeps an LTR run from reordering the Hebrew around it.
_FSI = "⁨"
_PDI = "⁩"

_HEBREW_MONTHS = (
    "ינואר", "פברואר", "מרץ", "אפריל", "מאי", "יוני",
    "יולי", "אוגוסט", "ספטמבר", "אוקטובר", "נובמבר", "דצמבר",
)
_HEBREW_WEEKDAYS = (
    "יום שני", "יום שלישי", "יום רביעי", "יום חמישי",
    "יום שישי", "יום שבת", "יום ראשון",
)


class CallbackDataTooLong(ValueError):
    """Raised when callback_data would exceed the documented 64-byte limit."""


def esc(value: object) -> str:
    """Escape arbitrary data for parse_mode='HTML'.

    The docs require replacing `<`, `>` and `&` that are not part of a tag or entity.
    `quote=False` produces exactly that set; `"` needs no escaping outside attributes.
    """
    return escape(str(value), quote=False)


# render_owner_markdown: a small, safe subset of Markdown -> Telegram HTML for the owner
# reply prose. None of `esc`, `<`, `>` or `&` are touched by HTML escaping, so these
# regexes run *after* `esc()` and still see the model's own `**`, backticks, `#` and `-`
# exactly as written.
_FENCE_RE = re.compile(r"```(?:[^\n]*\n)?(.*?)```", re.DOTALL)
_BOLD_INLINE_RE = re.compile(r"\*\*(?!\s)([^\n*]+?)(?<!\s)\*\*")
_CODE_INLINE_RE = re.compile(r"`([^`\n]+)`")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET_RE = re.compile(r"^(\s*)[-*]\s+(.*)$")
_PRE_FENCE_TOKEN = "\x00PRE{}\x00"


def render_owner_markdown(text: str) -> str:
    """Escape owner-reply prose, then re-apply a tiny allowlisted Markdown subset.

    Escaping happens FIRST, over the whole text, so a literal `<b>` written by the
    model (or echoed from a provider) can never become live HTML — it stays
    `&lt;b&gt;`. Only after that do balanced `**bold**`, `` `code` ``, fenced ``` code
    blocks ```, `#`/`##`/`###` headings and leading `-`/`* ` bullets turn into real
    Telegram entities. An unmatched `**` (no closing pair on the same line) is left
    exactly as typed. Nothing produced here spans a newline except `<pre>`, so
    `split_message` never has to cut inside a tag other than a fenced block.
    """
    escaped = esc(text)

    fences: list[str] = []

    def _stash_fence(match: re.Match[str]) -> str:
        body = match.group(1)
        if body.startswith("\n"):
            body = body[1:]
        if body.endswith("\n"):
            body = body[:-1]
        fences.append(f"<pre>{body}</pre>")
        return _PRE_FENCE_TOKEN.format(len(fences) - 1)

    without_fences = _FENCE_RE.sub(_stash_fence, escaped)
    rendered = "\n".join(_render_owner_line(line) for line in without_fences.split("\n"))
    for index, block in enumerate(fences):
        rendered = rendered.replace(_PRE_FENCE_TOKEN.format(index), block)
    return rendered


def _render_owner_line(line: str) -> str:
    heading = _HEADING_RE.match(line)
    if heading:
        body = _apply_inline_markdown(heading.group(2).strip())
        return f"<b>{body}</b>" if body else ""
    bullet = _BULLET_RE.match(line)
    if bullet:
        indent, body = bullet.groups()
        return f"{indent}• {_apply_inline_markdown(body)}"
    return _apply_inline_markdown(line)


def _apply_inline_markdown(text: str) -> str:
    text = _BOLD_INLINE_RE.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _CODE_INLINE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", text)
    return text


def isolate(value: object) -> str:
    """Wrap an LTR run so it does not reorder inside a Hebrew sentence."""
    text = str(value)
    if not text:
        return ""
    return f"{_FSI}{text}{_PDI}"


def code(value: object) -> str:
    """Monospace and tap-to-copy in Telegram clients. Ideal for ids and emails."""
    return f"<code>{esc(value)}</code>"


def bold(value: object) -> str:
    return f"<b>{esc(value)}</b>"


def italic(value: object) -> str:
    return f"<i>{esc(value)}</i>"


def link(url: str, label: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{esc(label)}</a>'


def blockquote(value: object, *, expandable: bool = False) -> str:
    """Collapse detail behind a tap. The best scannable primitive Telegram offers."""
    tag = "<blockquote expandable>" if expandable else "<blockquote>"
    return f"{tag}{esc(value)}</blockquote>"


def callback_data(value: str) -> str:
    """Validate a callback payload against the documented byte limit."""
    encoded = value.encode("utf-8")
    if not encoded:
        raise CallbackDataTooLong("callback_data must not be empty")
    if len(encoded) > MAX_CALLBACK_BYTES:
        raise CallbackDataTooLong(
            f"callback_data is {len(encoded)} bytes, max {MAX_CALLBACK_BYTES}"
        )
    return value


def approval_keyboard(
    token: str, *, approve_label: str = "אישור", reject_label: str = "ביטול"
) -> dict:
    """One-tap approve/reject row.

    `style` gives native green/red on current clients and degrades to default styling on
    older ones. The Hebrew label lives in `text`; `callback_data` stays ASCII.
    """
    return {
        "inline_keyboard": [
            [
                {
                    "text": f"✅ {approve_label}",
                    "callback_data": callback_data(f"ok:{token}"),
                    "style": "success",
                },
                {
                    "text": f"✖️ {reject_label}",
                    "callback_data": callback_data(f"no:{token}"),
                    "style": "danger",
                },
            ]
        ]
    }


def parse_callback_token(data: str) -> tuple[str, str]:
    """Split `ok:<token>` / `no:<token>` into `(decision, token)`.

    Anything else returns `("", "")`. The docs warn a callback can be replayed against a
    message that no longer carries that button, so callers must stay idempotent.
    """
    if not data or ":" not in data:
        return "", ""
    prefix, _, token = data.partition(":")
    if prefix == "ok":
        return "approve", token
    if prefix == "no":
        return "reject", token
    return "", ""


def hebrew_date(value: date | datetime, *, timezone: str = "Asia/Jerusalem") -> str:
    """`23 באוגוסט 2026` — how a person says a date, not an ISO string."""
    moment = _localize(value, timezone)
    return f"{moment.day} ב{_HEBREW_MONTHS[moment.month - 1]} {moment.year}"


def hebrew_datetime(value: datetime, *, timezone: str = "Asia/Jerusalem") -> str:
    """`יום ראשון, 23 באוגוסט, 14:30` — weekday first, the way meetings are discussed."""
    moment = _localize(value, timezone)
    weekday = _HEBREW_WEEKDAYS[moment.weekday()]
    month = _HEBREW_MONTHS[moment.month - 1]
    clock = isolate(f"{moment.hour:02d}:{moment.minute:02d}")
    return f"{weekday}, {moment.day} ב{month}, {clock}"


def relative_hebrew_day(
    value: date | datetime, *, today: date, timezone: str = "Asia/Jerusalem"
) -> str:
    """Say 'היום' and 'מחר' like a person, and fall back to a real date otherwise."""
    moment = _localize(value, timezone)
    target = moment.date() if isinstance(moment, datetime) else moment
    delta = (target - today).days
    if delta == 0:
        return "היום"
    if delta == 1:
        return "מחר"
    if delta == -1:
        return "אתמול"
    return hebrew_date(target, timezone=timezone)


def _localize(value: date | datetime, timezone: str):
    if not isinstance(value, datetime):
        return value
    try:
        zone = ZoneInfo(timezone)
    except (KeyError, ValueError):
        return value
    if value.tzinfo is None:
        return value
    return value.astimezone(zone)


def section(title: str, body: str, *, icon: str = "") -> str:
    """A titled block. Telegram uses a proportional font, so never pad for alignment."""
    head = f"{icon} {bold(title)}".strip() if icon else bold(title)
    cleaned = body.strip()
    if not cleaned:
        return head
    return f"{head}\n{cleaned}"


def bullets(items: list[str]) -> str:
    """A real list. Escaped per item, so arbitrary data is safe."""
    return "\n".join(f"• {esc(item)}" for item in items if str(item).strip())


def key_values(pairs: list[tuple[str, str]], *, monospace_values: bool = False) -> str:
    """`label: value` lines. Values can be monospaced so ids stay tap-to-copy and LTR."""
    lines: list[str] = []
    for label, value in pairs:
        if not str(value).strip():
            continue
        rendered = code(value) if monospace_values else esc(value)
        lines.append(f"{bold(label)}: {rendered}")
    return "\n".join(lines)


def join_sections(*blocks: str) -> str:
    """Blank line between logical sections. Empty blocks are dropped."""
    return "\n\n".join(block.strip() for block in blocks if block and block.strip())


_PRE_BLOCK_RE = re.compile(r"<pre>.*?</pre>", re.DOTALL)


def _clear_of_pre_blocks(text: str, cut: int) -> int:
    """Nudge a candidate cut index outside any `<pre>...</pre>` span it falls inside.

    Prefers deferring the whole block to the next chunk (cut right before it); only
    cuts right after it when the block starts at the very beginning of `text`, since
    deferring there would produce an empty chunk and stall the loop.
    """
    for match in _PRE_BLOCK_RE.finditer(text):
        if match.start() < cut < match.end():
            return match.start() if match.start() > 0 else match.end()
    return cut


def split_message(text: str, *, limit: int = _CHUNK_BUDGET) -> list[str]:
    """Chunk to stay under the 4096 limit, preferring paragraph then line boundaries.

    Splitting never lands inside an HTML tag other than `<pre>`, because it only cuts
    on newlines and this module never emits any other tag containing one. A `<pre>`
    block CAN contain newlines (that is the point of rendering a fenced code block
    literally), so a candidate cut is nudged outside any `<pre>...</pre>` span it would
    otherwise fall inside — even if that means one chunk runs past `limit` to keep the
    block whole and every chunk independently valid HTML.
    """
    cleaned = text.strip()
    if not cleaned:
        return []
    if len(cleaned) <= limit:
        return [cleaned]
    chunks: list[str] = []
    remaining = cleaned
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = limit
        cut = _clear_of_pre_blocks(remaining, cut)
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return [chunk for chunk in chunks if chunk]


def plain_text_length(html: str) -> int:
    """Rendered length. The 4096 limit applies "after entities parsing", so tags are free."""
    result: list[str] = []
    inside = False
    for char in html:
        if char == "<":
            inside = True
            continue
        if char == ">":
            inside = False
            continue
        if not inside:
            result.append(char)
    return len("".join(result))
