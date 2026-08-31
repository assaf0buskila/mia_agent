"""Maintain Mia's configured CRM workbook outside visitor and owner requests."""

from __future__ import annotations

import json
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.integrations.sheets import maintain_crm_workspace


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    status = maintain_crm_workspace(settings)
    print(json.dumps({"crm_workspace": status}))
    if status == "unavailable":
        sys.exit(1)


if __name__ == "__main__":
    main()
