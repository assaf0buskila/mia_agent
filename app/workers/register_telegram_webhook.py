"""Re-register the Telegram webhook with sticky allowed_updates.

Run: ``uv run mia-telegram-webhook`` (or as an ECS one-off command).
"""

from __future__ import annotations

import asyncio
import json

from app.core.config import get_settings
from app.integrations.telegram import ALLOWED_UPDATES, TelegramPort


async def _register() -> dict[str, object]:
    settings = get_settings()
    url = settings.public_base_url.rstrip("/") + "/v1/telegram/webhook"
    port = TelegramPort(bot_token=settings.telegram_bot_token)
    await port.set_webhook(url, secret_token=settings.telegram_webhook_secret)
    info = await port.get_webhook_info()
    return {
        "url": info.get("url"),
        "pending_update_count": info.get("pending_update_count"),
        "allowed_updates": info.get("allowed_updates") or list(ALLOWED_UPDATES),
        "last_error_message": info.get("last_error_message") or "",
    }


def main() -> None:
    print(json.dumps(asyncio.run(_register()), ensure_ascii=False))


if __name__ == "__main__":
    main()
