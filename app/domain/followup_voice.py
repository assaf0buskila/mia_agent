"""Human Voice Standard copy for prospect follow-up customer messages."""

MEETING_OFFERED_FOLLOW_UP = "עדיין לא קבענו שיחה. אם זה עדיין רלוונטי, אפשר לבחור מועד."


def compose_follow_up_draft(*, reason: str) -> str:
    if reason == "meeting_offered":
        return MEETING_OFFERED_FOLLOW_UP
    return ""
