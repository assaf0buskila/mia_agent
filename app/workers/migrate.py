"""Apply additive SQL migrations; schema only — never sends or customer writes."""

import json
import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.migrate import apply_migrations
from app.db.session import get_engine, init_db


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    engine = get_engine()
    # Prod image skips create_all on API boot. First-boot tables are SQLAlchemy
    # metadata; migrations/*.sql only add columns/tables for existing DBs.
    init_db()
    summary = apply_migrations(engine)
    print(json.dumps(summary.model_dump()))
    if summary.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
