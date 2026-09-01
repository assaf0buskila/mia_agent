"""Wipe Mia Postgres/SQLite data. Schema and schema_migrations stay."""

from __future__ import annotations

import argparse
import json
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine
from app.db.wipe import wipe_all_data

_CONFIRM = "fresh-start"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--confirm",
        required=True,
        help=f"Must be exactly {_CONFIRM!r} to run.",
    )
    args = parser.parse_args()
    if args.confirm != _CONFIRM:
        sys.exit(f"refusing wipe: pass --confirm {_CONFIRM}")

    settings = get_settings()
    configure_logging(settings.log_level)
    engine = get_engine()
    wiped = wipe_all_data(engine)
    print(json.dumps({"wiped_tables": wiped, "count": len(wiped)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
