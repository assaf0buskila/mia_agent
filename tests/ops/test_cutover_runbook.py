"""V2-31: ops/assaf-os/CUTOVER_RUNBOOK.md is executable documentation.

A runbook that drifts from the tool it describes is worse than none, so the parts that can be
checked are: the evidence shapes it shows are fed to the real CLI and must activate the cutover
(and each missing key must break the matching precondition), the reason codes, commands, phases
and keys it names are the CLI's own, every owner go of the phone-acceptance list is a go here,
and every ``sudo mia`` command is a command of the VM script.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from app.services.os_ledger import api
from app.workers import os_authority as cli

from tests.fakes.os_ledger_env import OWNER_ID
from tests.workers.test_os_authority import NOW, SHA, World, at, good_evidence, stamp

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = (ROOT / "ops/assaf-os/CUTOVER_RUNBOOK.md").read_text(encoding="utf-8")
PHONE = (ROOT / "docs/runbooks/OS_PHONE_ACCEPTANCE.md").read_text(encoding="utf-8")
SCRIPT = (ROOT / "ops/gcp/vm/mia").read_text(encoding="utf-8")
SCHEMA_JS = (ROOT / "ops/assaf-os/schema.js").read_text(encoding="utf-8")

EXAMPLE = re.compile(r"<!-- evidence:(\S+) -->\n```json\n(.*?)\n```", re.S)
RELATIVE = re.compile(r"\[UTC-(\d+)([MHD])\]")
UNITS = {"M": "minutes", "H": "hours", "D": "days"}


def examples() -> dict[str, dict]:
    found = {name: json.loads(body) for name, body in EXAMPLE.findall(RUNBOOK)}
    assert set(found) == {
        cli.BACKUP, cli.DRILL, cli.FRONTEND, cli.JOURNAL, cli.MANIFEST,
    }, "the runbook shows every operator evidence file"
    return found


def realised(value, digest: str):
    """Replace the bracketed placeholders of a shown example with concrete values."""
    if isinstance(value, dict):
        return {k: realised(v, digest) for k, v in value.items()}
    if isinstance(value, list):
        return [realised(v, digest) for v in value]
    if not isinstance(value, str):
        return value
    match = RELATIVE.fullmatch(value)
    if match:
        return stamp(NOW - timedelta(**{UNITS[match.group(2)]: int(match.group(1))}))
    return {
        "[STAMP]": "20261020T115500Z", "[CF-VERSION]": "ce8332db-4a1e", "[SHA256]": "c" * 64,
        "[DIGEST]": digest, "[OWNER-ID]": OWNER_ID,
    }.get(value, value.replace("[STAMP]", "20261020T115500Z"))


def documented_world(tmp_path, *, mutate=None) -> World:
    world = World(tmp_path)
    assert world.run("init").code == 0
    good_evidence(world)  # the files the CLI writes itself, and a baseline for the rest
    shown = examples()
    digest = api.digest(shown[cli.MANIFEST]["manifest"])
    for name, document in shown.items():
        document = realised(copy.deepcopy(document), digest)
        if mutate and mutate[0] == name:
            document = mutate[1](document)
        world.put(name, document)
    return world


def test_the_shown_evidence_activates_the_cutover(tmp_path):
    world = documented_world(tmp_path)
    run = world.run("activate")
    assert run.code == 0, run.err
    assert [c["status"] for c in run.out["checks"]] == ["pass"] * len(cli.PRECONDITIONS)


DROPS = [
    (cli.BACKUP, "last_success_utc", "backup_too_old"),
    (cli.DRILL, "started_utc", "restore_drill_too_old"),
    (cli.DRILL, "passed", "restore_drill_too_old"),
    (cli.FRONTEND, "cloudflare_version", "fe_v2_reader_not_live"),
    (cli.FRONTEND, "bundle_sha256", "fe_v2_reader_not_live"),
    (cli.FRONTEND, "recorded_at", "fe_v2_reader_not_live"),
    (cli.FRONTEND, "v", "fe_v2_reader_not_live"),
    (cli.JOURNAL, "measured_at", "journal_capacity"),
    (cli.JOURNAL, "max_rows", "journal_capacity"),
    (cli.JOURNAL, "tabs", "journal_capacity"),
    (cli.MANIFEST, "manifest_digest", "manifest_not_signed"),
    (cli.MANIFEST, "signoff", "manifest_not_signed"),
    (cli.MANIFEST, "xlsx_captured_at", "capture_before_fence"),
]


@pytest.mark.parametrize("name,key,code", DROPS)
def test_each_key_the_runbook_shows_is_needed(tmp_path, name, key, code):
    world = documented_world(
        tmp_path, mutate=(name, lambda d: {k: v for k, v in d.items() if k != key})
    )
    run = world.run("activate")
    refused = run.err.removeprefix("os_authority_refused:").strip().split(",")
    assert run.code == 1 and code in refused, (name, key, run.err)


@pytest.mark.parametrize("key", ["owner_id", "signed_at", "manifest_digest", "xlsx_captured_at",
                                 "drift_changed_cells"])
def test_each_sign_off_key_the_runbook_shows_is_needed(tmp_path, key):
    def drop(document):
        document["signoff"] = {k: v for k, v in document["signoff"].items() if k != key}
        return document

    run = documented_world(tmp_path, mutate=(cli.MANIFEST, drop)).run("activate")
    assert run.code == 1 and "manifest_not_signed" in run.err


def test_an_extra_key_in_a_shown_file_is_refused(tmp_path):
    for name in (cli.FRONTEND, cli.JOURNAL, cli.MANIFEST):
        world = documented_world(tmp_path / name, mutate=(name, lambda d: {**d, "extra": 1}))
        assert world.run("activate").code == 1, name


# ---------------------------------------------------------- the text names the tool's own things


def table_codes(section: str) -> list[str]:
    return re.findall(r"^\| `([a-z0-9_]+)` \|", section, re.M)


def test_the_reason_code_table_is_the_contract_table_in_order():
    section = RUNBOOK.split("## 7. Reason codes", 1)[1].split("Other refusals of the CLI", 1)[0]
    assert tuple(table_codes(section)) == cli.PRECONDITIONS


def test_every_command_phase_flag_and_evidence_file_is_named():
    for command in cli.COMMANDS:
        assert re.search(rf"os-authority {re.escape(command)}\b", RUNBOOK), command
    for phase in cli.PHASES:
        assert f"--phase {phase}" in RUNBOOK, phase
    for flag in ("--drain", "--final-accept", "--rearm", "--approved-sha"):
        assert flag in RUNBOOK, flag
    for name in (cli.DRAIN, cli.PREFLIGHT, cli.ACTIVATION, cli.BACKUP, cli.DRILL, cli.FRONTEND,
                 cli.JOURNAL, cli.MANIFEST):
        assert f"`{name}`" in RUNBOOK, name


def test_every_key_a_phase_flips_is_printed_in_the_runbook():
    for phase in cli.PHASES:
        for line in cli.plan_lines(phase, "E"):
            assert line in RUNBOOK or line.replace("=E", "=E,") in RUNBOOK, (phase, line)


def test_the_freeze_stays_on_until_the_open_phase_in_the_text():
    assert "**The freeze stays on.**" in RUNBOOK
    assert "MIA_ASSAF_OS_FREEZE=false" in RUNBOOK.split("## 4. The first write", 1)[1]
    assert "MIA_ASSAF_OS_FREEZE=false" not in cli.plan_lines("switch", 1)


def test_every_owner_go_of_the_phone_acceptance_list_is_a_go_here():
    section = PHONE.split("## Separate Assaf go for each production step", 1)[1]
    section = section.split("## Subsequent feature releases", 1)[0]
    items = re.findall(r"^- \[ \] GO: (.+?)\.?$", section, re.M)
    assert len(items) == 16

    def plain(text: str) -> str:
        return re.sub(r"[*`]", "", text).lower()

    # The distinctive words of each phone-list item, in the list's order.
    needles = [
        "backend deployment", "frontend deployment", "apps script deployment",
        "main-role demotion", "schema migration", "role grants", "authority initialization",
        "drain/freeze", "script properties fence", "import promotion", "authority activation",
        "backend switch", "enable mirror", "first non-import pg write", "phone acceptance",
        "onedit trigger",
    ]
    for item, needle in zip(items, needles, strict=True):
        assert needle in plain(item), (item, needle)
    body, table = RUNBOOK.split("## 8. Owner gates, in one list", 1)
    flowing = re.sub(r"\s+", " ", plain(body))
    for needle in needles:
        # Each phone-list go is both a "Go Gnn" mark in its step and a row of the final table.
        assert re.search(r"go g\d\d [^|]{0,150}?" + re.escape(needle), flowing), needle
        assert needle in plain(table), needle


def test_every_sudo_mia_command_is_a_command_of_the_vm_script():
    arms = set(re.findall(r"^  ([a-z|-]+)\)$", SCRIPT, re.M))
    commands = {name for arm_ in arms for name in arm_.split("|")}
    used = set(re.findall(r"sudo mia ([a-z-]+)", RUNBOOK))
    assert used and used <= commands, sorted(used - commands)
    assert {"os-authority", "os-provision", "os-job", "os-ledger", "settings", "jobs", "backup",
            "restore"} <= used


def test_the_archive_name_is_the_contracts_en_dash_literal():
    assert "`ARCHIVE – edits ignored`" in RUNBOOK and "–" in RUNBOOK
    stripped = RUNBOOK.replace("`ARCHIVE - read only`", "")
    assert "ARCHIVE - " not in stripped, "the hyphen form only appears in the conflict note"


def test_the_mirror_tabs_are_the_schemas_tabs_without_settings():
    named = ["סיכום", "לידים", "לקוחות", "עבודות", "תקבולים", "שעות עבודה", "הוצאות והשקעות",
             "משימות"]
    for tab in named:
        assert f'name: "{tab}"' in SCHEMA_JS and f"`{tab}`" in RUNBOOK, tab
    assert "הגדרות" not in RUNBOOK
    assert len(named) == 8


def test_the_runbook_has_no_credentials_and_lf_endings():
    assert "\r" not in RUNBOOK
    assert not re.search(r"postgresql://[^\s`]*:[^\s`@]+@", RUNBOOK)
    assert not re.search(r"[0-9a-f]{40}", RUNBOOK.replace(SHA, ""))
    assert "T1" in RUNBOOK and "T7" in RUNBOOK
    for step in ("### T1", "### T2", "### T3", "### T4", "### T5", "### T6", "### T7"):
        assert step in RUNBOOK, step


def test_every_step_names_its_verification_and_a_rollback():
    sections = re.split(r"^### ", RUNBOOK, flags=re.M)[1:]
    names = [s.splitlines()[0] for s in sections]
    assert len(names) >= 14, names
    for section in sections:
        assert "**Verify**" in section, section.splitlines()[0]
        assert "**Rollback**" in section or "rollback" in section.lower(), section.splitlines()[0]


def test_the_fixed_clock_of_the_examples_matches_the_world():
    assert at() == NOW
