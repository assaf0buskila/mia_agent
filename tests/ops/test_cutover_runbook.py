"""V2-31: ops/assaf-os/CUTOVER_RUNBOOK.md is executable documentation.

A runbook that drifts from the tool it describes is worse than none, so the parts that can be
checked are: the evidence shapes it shows are fed to the real CLI and must activate the cutover
(and each missing key must break the matching precondition), the reason codes, commands, phases
and keys it names are the CLI's own, every owner go of the phone-acceptance list is a go here,
and every ``sudo mia`` command is a command of the VM script.
"""

from __future__ import annotations

import contextlib
import copy
import io
import json
import re
from datetime import timedelta
from pathlib import Path

import pytest
from app.services.os_ledger import api, mirror_projection
from app.workers import os_authority as cli

from tests.fakes.os_ledger_env import OWNER_ID
from tests.workers.test_os_authority import (
    NOW,
    SHA,
    World,
    at,
    good_evidence,
    promote_import,
    stamp,
)

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
    promote_import(world, digest)  # T5: the shown manifest's digest, promoted at this epoch
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


GATE_MARK = re.compile(r"\bG\d\d\b")
HEADING = re.compile(r"^#{2,3} ", re.M)

# Every command that changes production state, the section that runs it, and the owner gate that
# must be marked there. An unmarked line reads as runnable without a go (the runbook's own rule).
GATED_COMMANDS = [
    ("### Step 3", r"sudo mia os-ledger migrate", "G06", False),
    ("### Step 4", r"sudo mia os-provision runtime", "G07", False),
    ("### Step 4", r"sudo mia os-provision retention", "G07", False),
    ("### Step 5", r"sudo mia os-authority init --approved-sha S", "G08", False),
    ("### T4", r"sudo mia os-job os-import", "G24", True),
    ("### T5", r"sudo mia os-job os-import", "G12", False),
    ("### T6", r"sudo mia os-authority activate", "G13", False),
    ("## 5. ", r"sudo mia os-authority rollback", "G19", False),
    ("## 5. ", r"sudo mia os-authority init --rearm", "G23", True),
    ("## 6. ", r"sudo mia os-authority fence", "G22", True),
    ("## 6. ", r"sudo mia os-authority restore-begin", "G21", True),
    ("## 6. ", r"sudo mia os-authority restore-finish", "G21", True),
    ("## 6. ", r"sudo mia restore", "G21", True),
]


def section_of(heading: str) -> str:
    starts = [m.start() for m in HEADING.finditer(RUNBOOK)] + [len(RUNBOOK)]
    for begin, end in zip(starts, starts[1:], strict=False):
        if RUNBOOK[begin:end].startswith(heading):
            return RUNBOOK[begin:end]
    raise AssertionError(heading)


def bullet_with(section: str, command: str) -> str:
    """The top-level bullet of a section (its indented lines included) that runs a command."""
    for bullet in re.split(r"^(?=- )", section, flags=re.M):
        if re.search(command, bullet):
            return bullet
    raise AssertionError(command)


@pytest.mark.parametrize("heading,command,gate,same_bullet", GATED_COMMANDS)
def test_every_production_changing_command_is_marked_with_its_owner_gate(
    heading, command, gate, same_bullet
):
    section = section_of(heading)
    assert re.search(command, section), (heading, command)
    assert gate in GATE_MARK.findall(section), (heading, command, gate)
    if same_bullet:
        # No other line to hide the go on: the command and its gate sit in one bullet.
        assert gate in GATE_MARK.findall(bullet_with(section, command)), (heading, command, gate)


def test_the_owner_gate_list_covers_every_gate_a_step_marks_and_the_three_added_flips():
    body, table = RUNBOOK.split("## 8. Owner gates, in one list", 1)
    rows = dict(re.findall(r"^\| (G\d\d) \| (.+) \|$", table, re.M))
    numbers = list(rows)
    assert numbers == [f"G{n:02d}" for n in range(1, len(numbers) + 1)], "contiguous, in order"
    assert numbers[-1] == "G24", "G22 fence, G23 re-arm and G24 staging were added after G21"
    assert set(GATE_MARK.findall(body)) <= set(numbers), "a step marks a gate the list lacks"
    assert {"G22", "G23", "G24"} <= set(GATE_MARK.findall(body))
    assert "`fence`" in rows["G22"] and "`init --rearm`" in rows["G23"]
    assert "staging" in rows["G24"].lower() and "os-import" in rows["G24"]
    assert "G10 to G24" in RUNBOOK and "G10 to G21" not in RUNBOOK


def test_fence_is_not_a_standing_emergency_stop_unless_the_owner_wrote_it_down():
    section = section_of("## 6. ")
    assert "not pre-approved" in section and "G22" in section


def test_restore_procedure_pins_limits_epoch_and_refusals():
    section = section_of("## 6. ")
    assert "not an automated command" not in section
    for text in ("--expected-epoch E", "restore.json", "ledger-fence.json",
                 "restore_incomplete", "restore_rpo_exceeded", "restore_evidence_invalid",
                 "restore_epoch_mismatch", "restore_epoch_required", "main_role_unsafe",
                 "ledger_role_unsafe", "restore_rto_exceeded", "15 min", "2 h",
                 "sudo mia os-authority status --approved-sha S"):
        assert text in section, text
    assert section.index("sudo mia restore") < section.index("os-authority restore-begin")
    assert (section.index("os-authority restore-begin")
            < section.index("os-authority restore-finish"))
    # WP1/E35 allows publishing under freeze: containment must survive app restart after restore.
    assert section.index("MIA_KILL_SWITCH=true") < section.index("sudo mia restore")
    assert "WP1/E35" in section and "A freeze alone is insufficient" in section


def test_restore_containment_order_exception_and_reason_table():
    section = section_of("## 6. ")
    assert section.index("MIA_KILL_SWITCH=true") < section.index("os-authority fence")
    assert "first fence impossible / evidence directory lost" in section
    assert "owner's explicit exception procedure" in section
    assert "fence_record_minted_after_restore" in section and "null" in section
    assert "restore-attempt.json" in section and "pre_restore_fence_record_present" in section
    assert "Always restore through `sudo mia restore`" in section
    assert "otherwise the incident timing signal is lost" in section
    assert "run status after `mia restore` and compare the epoch" in section
    table = RUNBOOK.split("Other refusals of the CLI:", 1)[1]
    for code in ("restore_incomplete", "restore_rpo_exceeded", "restore_evidence_invalid",
                 "restore_epoch_mismatch", "restore_epoch_required", "restore_epoch_invalid",
                 "restore_rto_exceeded"):
        assert re.search(rf"^\| `{code}` \|", table, re.M), code


def test_every_sudo_mia_command_is_a_command_of_the_vm_script():
    arms = set(re.findall(r"^  ([a-z|-]+)\)$", SCRIPT, re.M))
    commands = {name for arm_ in arms for name in arm_.split("|")}
    used = set(re.findall(r"sudo mia ([a-z-]+)", RUNBOOK))
    assert used and used <= commands, sorted(used - commands)
    assert {"os-authority", "os-provision", "os-job", "os-ledger", "settings", "jobs", "backup",
            "restore"} <= used


def test_the_t1_and_t2_rollbacks_say_a_retry_redoes_t2_and_the_old_drain_is_set_aside():
    # V2-31b finding 2: the rollback only unfreezes, so the next freeze must not inherit its drain.
    flat = re.sub(r"\s+", " ", section_of("### T1"))
    assert "Each freeze starts a new drain" in flat and "drain-superseded-<UTC>.json" in flat
    rollback = flat.split("**Rollback**", 1)[1]
    assert "A retry redoes T1 and T2" in rollback and "`drain_stale`" in rollback
    # V2-31b review 2: the exit itself (the revert plan) voids the drain, the drain phase does too,
    # and the age bound is only the backstop.
    assert "the `--phase revert` plan above, **sets `drain.json` aside**" in rollback
    assert "and so does the retry's `freeze-plan --phase drain`" in rollback
    assert "as a backstop" in rollback
    step = re.sub(r"\s+", " ", section_of("## 5. "))
    assert "and sets `drain.json` aside (it ends the freeze that drain belongs to)" in step
    t2 = re.sub(r"\s+", " ", section_of("### T2")).split("**Rollback**", 1)[1]
    assert "A retry redoes T2" in t2 and "sets it aside" in t2
    # The numbers and names the text states are the CLI's own.
    assert cli.DRAIN_MAX_AGE == timedelta(hours=2)
    assert "more than 2 hours before `fence_probe_at`" in flat
    assert re.search(r"^\| `drain\.json` \|.*drain-superseded-.*\(`drain_stale`\)", RUNBOOK, re.M)
    assert "`drain_stale`" in RUNBOOK.split("## 7. Reason codes", 1)[1]


def test_the_ledger_keys_come_at_t1_and_the_switch_flips_only_the_backend_in_the_text():
    # Contract section 22 E29: the import of T4/T5 needs the ledger keys before the switch.
    assert cli.plan_lines("drain", "E") == [
        "MIA_ASSAF_OS_FREEZE=true", "MIA_ASSAF_OS_LEDGER_ENABLED=true",
        "MIA_ASSAF_OS_AUTHORITY_EPOCH=E",
    ]
    assert cli.plan_lines("switch", "E") == ["MIA_ASSAF_OS_BACKEND=postgres"]
    t1 = re.sub(r"\s+", " ", section_of("### T1"))
    command, rollback = t1.split("**Rollback**", 1)
    for key in cli.plan_lines("drain", "E"):
        assert f"`{key}`" in command, key
    assert "with the backend still `sheets`" in command and "E29" in command
    assert "`authorize.for_worker` refuses `epoch_unset`" in command
    assert "`backend_not_postgres`" in command and "test_os_e29_sheets_with_ledger_keys" in command
    # The T1/T2 rollback returns the ledger keys with the existing revert phase, and the freeze.
    assert "--phase revert" in rollback and "MIA_ASSAF_OS_FREEZE=false" in rollback
    for key in cli.plan_lines("revert", 0):
        assert f"`{key}`" in rollback, key
    # T4 and T5 name the precondition, T6's switch prints only the backend.
    for step in ("### T4", "### T5"):
        flat = re.sub(r"\s+", " ", section_of(step))
        assert "T1 keys" in flat and "**Precondition**" in flat, step
    t6 = re.sub(r"\s+", " ", section_of("### T6"))
    switch = t6.split("- **Switch**", 1)[1].split("- **Verify**", 1)[0]
    assert "`MIA_ASSAF_OS_BACKEND=postgres` and nothing else" in switch
    assert "set at T1 and are not repeated" in switch and "`drain_keys_not_applied`" in switch
    assert "`MIA_ASSAF_OS_LEDGER_ENABLED=true`, `MIA_ASSAF_OS_AUTHORITY_EPOCH=E`." not in switch
    rule = RUNBOOK.split("5. **Keys that matter**", 1)[1].split("6. **", 1)[0]
    keys_rule = re.sub(r"\s+", " ", rule)
    assert "T1 sets `FREEZE`, `LEDGER_ENABLED` and `AUTHORITY_EPOCH`" in keys_rule
    assert "T6 sets `BACKEND` only" in keys_rule
    assert re.search(r"^\| `drain_keys_not_applied` \|", RUNBOOK, re.M)


def test_the_text_names_the_import_not_promoted_refusal_where_the_operator_meets_it():
    # V2-31b finding 8: one refusal beyond the contract's 15, so it is NOT in the 15-row table.
    assert cli.PROMOTE_GATE == "import_not_promoted" and cli.PROMOTE_GATE not in cli.PRECONDITIONS
    codes_table = RUNBOOK.split("## 7. Reason codes", 1)[1]
    assert cli.PROMOTE_GATE not in codes_table.split("Other refusals of the CLI", 1)[0]
    assert re.search(rf"^\| `{cli.PROMOTE_GATE}` \|", codes_table, re.M)
    for step in ("### T5", "### T6"):
        assert f"`{cli.PROMOTE_GATE}`" in re.sub(r"\s+", " ", section_of(step)), step
    t6 = re.sub(r"\s+", " ", section_of("### T6"))
    assert "`no_import_receipt`" in t6 and "binds the digest in `manifest.json`" in t6


def t7_create_step() -> str:
    t7 = section_of("### T7")
    start = t7.index("- **Create the eight tabs**")
    return t7[start:t7.index("- **Command**", start)]


def test_t7_creates_the_eight_mirror_tabs_from_the_mirror_schema_in_code():
    # V2-31b finding 7 (E11.4): the runbook must say how the tabs are created and where their names
    # and headers come from, not only that they must exist.
    step = t7_create_step()
    flat = re.sub(r"\s+", " ", step)
    precondition = re.sub(r"\s+", " ", section_of("### T7").split("- **Create the eight tabs**")[0])
    # The tabs the precondition names are exactly the module's, in its order.
    named = precondition.split("There is no", 1)[0].split("exact names:", 1)[1]
    assert tuple(re.findall(r"`([^`]+)`", named)) == mirror_projection.MIRROR_TABS
    assert "create them with the next bullet" in precondition
    # The header source it cites exists, by name and by path.
    for cited in ("MIRROR_TABS", "headers()", "_SUMMARY_FIELDS"):
        assert cited in flat, cited
    assert hasattr(mirror_projection, "MIRROR_TABS") and callable(mirror_projection.headers)
    assert hasattr(mirror_projection, "_SUMMARY_FIELDS")
    for path in ("app/services/os_ledger/mirror_projection.py",
                 "app/prototypes/assaf_os_export_schema.json", "ops/assaf-os/schema.js"):
        assert path in flat and (ROOT / path).is_file(), path
    assert "no `addSheet` call" in precondition and "Leave them empty" in flat


def test_the_t7_print_command_prints_each_tab_with_its_header_row():
    command = re.search(r'`uv run --offline python -X utf8 -c "(.+?)"`', t7_create_step(), re.S)
    assert command, "the step shows the command"
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(compile(command.group(1), "<runbook T7>", "exec"), {})  # noqa: S102
    lines = out.getvalue().splitlines()
    assert len(lines) == 8
    expected_headers = {"סיכום": "summary"} | {
        tab: entity for entity, tab in mirror_projection.ENTITY_TABS.items()}
    for line, tab in zip(lines, mirror_projection.MIRROR_TABS, strict=True):
        cells = line.split(" | ")
        assert cells[0] == tab
        assert tuple(cells[1:]) == mirror_projection.headers(expected_headers[tab])
    # The worker really writes row 1 itself (so the tabs can be left empty).
    first = mirror_projection.write_ranges(
        mirror_projection.Projection(0, {tab: (mirror_projection.headers(expected_headers[tab]),)
                                         for tab in mirror_projection.MIRROR_TABS}), None)
    assert [rows[0] for _, rows in first] == [
        list(mirror_projection.headers(expected_headers[tab]))
        for tab in mirror_projection.MIRROR_TABS]
    assert [anchor for anchor, _ in first] == [
        mirror_projection.tab_anchor(tab) for tab in mirror_projection.MIRROR_TABS]


def test_the_t3_rollback_requires_the_live_sighting_and_the_break_record_and_reruns_on_unknown():
    # V2-31b review 3: the step whose whole job is to record fence_broken_at must say how to know
    # that it did, and what to do on an `unknown` probe (which writes no break).
    rollback = re.sub(r"\s+", " ", section_of("### T3")).split("- **Rollback**", 1)[1]
    step = rollback.split("The rollback lifts the fence", 1)[0]
    assert "do not go on until both of these hold" in step
    assert '(a) its JSON prints `"sheets": "live"`' in step
    assert "(b) `sudo cat /var/lib/mia/os-authority/preflight.json` shows `fence_broken_at`" in step
    assert '`"sheets": "unknown"`' in step and "**run it again**" in step
    # The names it relies on are the CLI's own: the evidence file, the key, and the printed state.
    assert cli.PREFLIGHT == "preflight.json"
    fenced = {"v": 1, "kind": "preflight", "epoch": 1, "release_sha": SHA, "sheets": "unavailable",
              "fence_probe_at": stamp(NOW)}
    live = cli._fence_record(fenced, 1, SHA, "live", NOW + timedelta(minutes=5))
    assert live["sheets"] == "live" and live["fence_broken_at"] == stamp(
        NOW + timedelta(minutes=5))
    unknown = cli._fence_record(fenced, 1, SHA, "unknown", NOW + timedelta(minutes=6))
    assert unknown["sheets"] == "unknown" and "fence_broken_at" not in unknown


def test_the_text_says_an_unknown_probe_does_not_void_the_captures():
    # V2-31b finding 3: the CLI carries fence_probe_at through `unknown`; the runbook must say so
    # at the two places an operator meets it (T3's probe and T6's preflight).
    t3 = re.sub(r"\s+", " ", section_of("### T3"))
    assert "`sheets: unknown`" in t3 and "does not void the captures" in t3
    t6 = re.sub(r"\s+", " ", section_of("### T6"))
    assert "`sheets: unknown`" in t6 and "does not void the T4 captures" in t6
    assert "an `unknown` probe does not" in RUNBOOK.split("## 7. Reason codes", 1)[1]


def test_the_text_says_a_rollback_counts_again_after_the_fence_and_prints_no_steps_then():
    # V2-31b finding 4: the race between the receipt count and the fence.
    section = re.sub(r"\s+", " ", section_of("## 5. "))
    assert "before the fence **and again after it**" in section
    assert "leaves `core` **fenced** and prints **no steps**" in section
    assert "follow section 6" in section
    assert "slipped in between" in RUNBOOK.split("## 7. Reason codes", 1)[1]
    # V2-31b review: the post-fence refusal has its own reason, named where the operator reads it.
    assert "`rollback_after_first_write,core_left_fenced`" in section
    assert "`\"core\": \"fenced\"`" in section and "Read the second reason" in section
    codes_table = RUNBOOK.split("## 7. Reason codes", 1)[1]
    assert re.search(r"^\| `core_left_fenced` \|", codes_table, re.M)
    assert "it adds `core_left_fenced`" in codes_table


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
