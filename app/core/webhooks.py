import base64
import hashlib
import hmac
import time

from app.core.errors import WebhookRejected

_MAX_SKEW_SECONDS = 300


def verify_webhook(
    *,
    secret: str,
    body: bytes,
    signature_hex: str,
    timestamp: int,
    now: int | None = None,
) -> None:
    """HMAC-SHA256 + timestamp window. Rejects spoofed and replayed webhooks."""
    if not secret:
        raise WebhookRejected("webhook secret is not configured")
    current = now if now is not None else int(time.time())
    if abs(current - timestamp) > _MAX_SKEW_SECONDS:
        raise WebhookRejected("webhook timestamp outside replay window")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature_hex.lower()):
        raise WebhookRejected("webhook signature mismatch")


def verify_meta_signature(*, secret: str, body: bytes, header: str) -> None:
    """Meta Cloud API X-Hub-Signature-256: sha256=<hmac>."""
    if not secret:
        raise WebhookRejected("webhook secret is not configured")
    if not header.startswith("sha256="):
        raise WebhookRejected("missing sha256 signature")
    digest = header.removeprefix("sha256=")
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, digest.lower()):
        raise WebhookRejected("webhook signature mismatch")


def verify_composio_signature(
    *,
    secret: str,
    body: bytes,
    webhook_id: str,
    webhook_timestamp: str,
    webhook_signature: str,
    now: int | None = None,
) -> None:
    """Composio trigger webhook: HMAC-SHA256 over id.timestamp.rawBody, base64 digest."""
    if not secret:
        raise WebhookRejected("webhook secret is not configured")
    if not webhook_id or not webhook_timestamp or not webhook_signature:
        raise WebhookRejected("missing composio webhook headers")
    try:
        timestamp = int(webhook_timestamp)
    except ValueError as exc:
        raise WebhookRejected("webhook timestamp invalid") from exc
    current = now if now is not None else int(time.time())
    if abs(current - timestamp) > _MAX_SKEW_SECONDS:
        raise WebhookRejected("webhook timestamp outside replay window")
    signed = f"{webhook_id}.{webhook_timestamp}.".encode() + body
    expected = base64.b64encode(
        hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    ).decode("ascii")
    provided = (
        webhook_signature.split(",", 1)[1].strip()
        if "," in webhook_signature
        else webhook_signature.strip()
    )
    try:
        if not hmac.compare_digest(expected, provided):
            raise WebhookRejected("webhook signature mismatch")
    except TypeError as exc:
        raise WebhookRejected("webhook signature mismatch") from exc


def verify_telegram_secret(*, secret: str, header: str) -> None:
    """Telegram setWebhook secret_token → X-Telegram-Bot-Api-Secret-Token."""
    if not secret:
        raise WebhookRejected("webhook secret is not configured")
    if not header:
        raise WebhookRejected("missing telegram webhook secret")
    try:
        if not hmac.compare_digest(secret, header):
            raise WebhookRejected("webhook signature mismatch")
    except (TypeError, ValueError) as exc:
        raise WebhookRejected("webhook signature mismatch") from exc
