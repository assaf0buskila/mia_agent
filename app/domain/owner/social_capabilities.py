"""Social capability truth: what Mia can actually do on LinkedIn and Instagram.

Computed from configuration only -- the caller resolves each port the same way the
real read tools do (see `app/tools/owner/analytics.py::_social_capabilities`) and
this module only formats the result. No provider call happens anywhere in this
file. Instagram publishing stays policy-denied regardless of configuration
(`app/domain/owner/composio_effects.py`, unchanged); LinkedIn writes stay on their
named approval path (`app/domain/owner/linkedin_writes.py`, unchanged). Nothing
here grants a new write capability.

**The rendered text is Hebrew, and the layer boundary is deliberate.** This is the
owner-facing capability *answer*, and Assaf reads it -- directly when
`connection_audit`-style rendering splices tool text into a message, and through
the model otherwise. Every comparable owner read (`connection_audit`, `status`,
`reads`, `briefs`) answers in Hebrew; this one answered in English. What stays
English is model-facing prompt text: the `ToolSpec.description`s in
`app/tools/registries/owner_tools.py` and `SOCIAL_WRITING_RULE` in
`app/graph/owner_agent.py` are instructions to the model, not something Assaf sees.

Brand names stay Latin and stay *bare* here. `telegram_format.owner_text()` is the
single egress normaliser and isolates every LTR run once the surrounding string is
Hebrew, so `LinkedIn` and `Instagram` are bidi-isolated for free at send time.
Hand-isolating them in this file would apply a builder-level primitive (meant for
one known field: an id, a URL, a date) to whole prose, and would be the wrong
layer.

One phrase this module must NOT be trusted to carry: `connection_audit._status`
classifies a probe as unconnected by matching the literal English "not connected"
/ "not configured" in the probed tool's text. That marker is produced by
`app/tools/owner/types.py::_house_unavailable`, and `social_capabilities` is not
one of the audit's probes -- the two never met, which is what makes translating
this file safe. `test_connection_audit_not_connected_marker_is_independent_of_this_module`
pins both halves of that.
"""

from __future__ import annotations

from dataclasses import dataclass

_AVAILABLE = "זמין"
_NOT_CONFIGURED = "לא מוגדר"


@dataclass(frozen=True)
class SocialCapabilities:
    linkedin_configured: bool
    instagram_configured: bool


def format_social_capabilities(caps: SocialCapabilities) -> str:
    """One factual line per capability -- never a blanket "connected" verdict."""
    linkedin_read = _AVAILABLE if caps.linkedin_configured else _NOT_CONFIGURED
    instagram_read = _AVAILABLE if caps.instagram_configured else _NOT_CONFIGURED
    # Proposing a LinkedIn action needs an active LinkedIn connection to validate
    # the schema against (`propose_linkedin_write`); with none configured, the
    # honest answer is "not available", not a description of the approval flow
    # as if it were currently usable.
    linkedin_write_line = (
        "- פוסט או תגובה ב-LinkedIn: רק באישור מדויק ואז ביצוע. עוד לא אומת "
        "בשידור חי, ואין בדיקה חוזרת עצמאית שזה באמת מופיע ב-LinkedIn אחר כך."
        if caps.linkedin_configured
        else "- פוסט או תגובה ב-LinkedIn: לא זמין, אין חיבור LinkedIn פעיל "
        "להציע מולו."
    )
    lines = [
        "יכולות רשתות חברתיות (לפי ההגדרות בלבד, לא בדיקה חיה מול הספק):",
        f"- קריאת פרופיל LinkedIn: {linkedin_read}. הפרופיל של אסף בלבד, בלי "
        "נתוני חשיפה, עוקבים או ביצועי פוסטים בשום מסלול.",
        linkedin_write_line,
        f"- קריאת תובנות Instagram: {instagram_read}. מדדים ברמת פוסט בלבד; מדד "
        "חסר חוזר כלא זמין, אף פעם לא כאפס ולא מומצא.",
        "- פרסום ב-Instagram: לא זמין, חסום במדיניות, בלי תלות בהגדרות.",
        "- הודעות ישירות ומודעות ב-Instagram: לא זמינים בשום מסלול.",
        "- אין תזמון באף אחת מהפלטפורמות.",
        "- טיוטה, כיתוב או רעיון לתוכן הם חומר לכתוב ממנו, לא פוסט שפורסם; כדי "
        "לצאת הם עדיין צריכים אישור נפרד משלהם.",
    ]
    return "\n".join(lines)
