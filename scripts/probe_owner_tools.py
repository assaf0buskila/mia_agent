"""Owner release audit: bounded reads only; no provider writes or raw user data in output.

Run in the release container with existing injected settings. Each local database
transaction is rolled back. Output tool names, outcome labels and field presence only.
The image does not package scripts: pass this source using run_ecs_command.py
--task-definition mia:REV -- python -c SOURCE. Nonzero exit means a failed check.
"""

import json
import logging
import sys
from datetime import UTC, datetime

from app.brain.embeddings import build_embedding_port
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import get_settings
from app.core.risk import RiskLevel
from app.db.session import get_session_factory
from app.db.store import LeadStore
from app.domain.owner.brain import bind_owner_house_ports
from app.integrations.composio_catalog import (
    ComposioCatalog,
    risk_for_slug,
    schema_text,
    validate_arguments,
)
from app.tools.registries.owner_tools import ToolContext, execute_tool, tool_names


def main():
    logging.disable(logging.CRITICAL)
    failures = 0
    checked = 0
    settings = get_settings()
    house = bind_owner_house_ports(settings)
    reads = {
        "linkedin_snapshot": {"full_profile": True},
        "gmail_inbox": {},
        "calendar_agenda": {"range": "today"},
        "calendar_availability": {},
        "instagram_insights": {"limit": 1},
        "website_kpis": {},
        "seo_snapshot": {},
        "research_search": {"query": "AssafWeb"},
        "sheets_list_tabs": {},
        "crm_search": {"query": "release probe reserved.invalid"},
        "sheets_read": {"spreadsheet_id": settings.resolved_sheets_spreadsheet_id(),
                        "range": "Contacts!A1:N2"},
        "search_knowledge": {"query": "AssafWeb services"},
        "search_memory": {"query": "owner preferences"},
        "pending_approvals": {},
        "hot_leads": {},
        "website_conversations": {},
    }
    print(json.dumps({"kind": "registry", "registered": len(tool_names())}), flush=True)
    for name, args in reads.items():
        checked += 1
        try:
            with get_session_factory()() as db:
                ctx = ToolContext(
                    store=LeadStore(db), brain=BrainStore(db), settings=settings,
                    principal=Principal.owner(source="authorized_release_probe"),
                    embedding_port=build_embedding_port(settings), **house,
                    now=datetime.now(UTC), kill_switch=settings.kill_switch,
                )
                try:
                    result = execute_tool(name, args, ctx)
                    # Some legacy adapters return an explanatory empty/unavailable
                    # message with ok=True. Retain that distinction in this report.
                    blob = (result.text + " " + result.error).casefold()
                    unavailable = any(word in blob for word in (
                        "not configured", "not connected", "unavailable", "returned nothing",
                        "לא מחובר", "לא זמין", "לא מוגדר", "לא הצלח", "לא חזר",
                    ))
                    failures += int(
                        not result.ok or unavailable or result.outcome_label() != "success"
                    )
                    print(json.dumps({"kind": "read", "tool": name, "ok": result.ok,
                                      "outcome": result.outcome_label(),
                                      "unavailable": unavailable}), flush=True)
                finally:
                    db.rollback()
        except Exception as exc:
            failures += 1
            print(json.dumps({"kind": "read", "tool": name, "ok": False,
                              "error_type": type(exc).__name__}), flush=True)
    try:
        catalog = ComposioCatalog.from_settings(settings)
        if catalog is None:
            print(json.dumps({"kind": "catalog", "ok": False}), flush=True)
            print(json.dumps({"kind": "summary", "checked": checked + 1,
                              "failures": failures + 1, "result": "FAIL"}), flush=True)
            return 1
        with catalog:
            toolkits = catalog.active_toolkits()
            failures += int(not toolkits)
            for toolkit in toolkits:
                checked += 1
                found = catalog.search("", toolkit, limit=50)
                sample = found[0] if found else None
                detail = catalog.detail(sample.slug) if sample else None
                failures += int(not (detail and schema_text(detail)))
                print(json.dumps({"kind": "catalog", "toolkit": toolkit,
                                  "listed": len(found), "schema_ok": bool(
                                      detail and schema_text(detail))}), flush=True)
            checked += 1
            profile_tool = catalog.detail("LINKEDIN_GET_MY_INFO")
            if (profile_tool and risk_for_slug(profile_tool.slug, profile_tool.toolkit)
                    is RiskLevel.R0_READ and not validate_arguments(profile_tool.input_schema, {})):
                result = catalog.execute_read(profile_tool, {})
                data = (result or {}).get("data")
                if isinstance(data, str):
                    data = json.loads(data)
                profile_ok = (isinstance(data, dict) and bool(data)
                              and result.get("successful") is not False)
                failures += int(not profile_ok)
                print(json.dumps({"kind": "linkedin_fields", "ok": profile_ok,
                                  "fields": sorted(data)[:50] if isinstance(data, dict) else []}),
                      flush=True)
            else:
                failures += 1
    except Exception as exc:
        failures += 1
        print(json.dumps({"kind": "catalog", "ok": False,
                          "error_type": type(exc).__name__}), flush=True)

    print(json.dumps({"kind": "summary", "checked": checked, "failures": failures,
                      "result": "PASS" if failures == 0 else "FAIL"}), flush=True)
    return int(failures > 0)


if __name__ == "__main__":
    sys.exit(main())

