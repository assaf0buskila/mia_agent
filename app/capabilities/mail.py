"""Owner mail.read — Gmail fetch behind capability/policy, not a Composio slug in the graph."""

from __future__ import annotations

from typing import Any

from app.core.errors import InvalidArguments
from app.integrations.gmail import GmailPort, InboundEmail


def mail_read(port: GmailPort, args: dict[str, Any]) -> dict[str, Any]:
    message_id = str(args.get("message_id") or "").strip()
    if not message_id:
        raise InvalidArguments("message_id is required")
    email: InboundEmail | None = port.fetch_message(message_id)
    if email is None:
        return {"found": False, "message_id": message_id}
    return {
        "found": True,
        "message_id": email.message_id,
        "thread_id": email.thread_id,
        "subject": email.subject,
        "sender": email.sender,
        "text": email.text[:2000],
        "timestamp": email.timestamp,
    }


def mail_search(port: GmailPort, args: dict[str, Any]) -> dict[str, Any]:
    """Owner mail.search — recent inbox, or a Gmail query when one is given.

    `mail.search` has been a registered capability with no handler, so the two owner
    tools that need it (`gmail_inbox`, `gmail_search`) called the port directly and
    were gated only by registry membership. This is the missing half.
    """
    query = str(args.get("query") or "").strip()
    rows = port.search(query) if query else port.list_recent()
    return {"query": query, "rows": list(rows)}


def mail_handlers(port: GmailPort) -> dict[str, Any]:
    return {
        "mail.read": lambda args: mail_read(port, args),
        "mail.search": lambda args: mail_search(port, args),
    }
