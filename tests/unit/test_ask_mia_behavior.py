from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

import pytest
from app.surfaces.site_v2 import SITE_V2_ACTIONS, SiteV2Reply


def test_widget_contact_conversion_behavior() -> None:
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["node", "tests/unit/widget_behavior.test.js"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "widget behavioral checks passed" in result.stdout


# --- the widget/server action vocabulary -------------------------------------
#
# 40747c8 narrowed the server to SITE_V2_ACTIONS and nothing failed, because the only
# assertion anywhere was a subset one. These tests assert in BOTH directions: a name the
# widget branches on that the server cannot emit fails, and a name the server can emit
# that the widget silently ignores fails too.

_ROOT = Path(__file__).resolve().parents[2]
_WIDGET = _ROOT / "app" / "web" / "ask_mia.js"
_FAKE_SERVER = _ROOT / "tests" / "unit" / "widget_behavior.test.js"

# `data.next_action === 'contact_saved'` in the widget, `next_action: 'answer'` in the
# fake server. Both quote styles and `==` are accepted so a reformat cannot hide a branch.
_BRANCH = re.compile(r"next_action\s*={2,3}\s*['\"]([A-Za-z_]+)['\"]")
_EMITTED = re.compile(r"next_action\s*:\s*['\"]([A-Za-z_]+)['\"]")


def _code_lines(source: str) -> list[str]:
    """Drop whole-line and trailing `//` comments so a name mentioned in prose is data.

    Line-oriented on purpose: a real JS parser is not worth carrying here, and the only
    thing this has to get right is that `next_action` written in a comment is not a
    branch. `_actions` cross-checks the count, so an occurrence this misses is loud.
    """
    out = []
    for line in source.split("\n"):
        marker = line.find("//")
        out.append(line if marker < 0 else line[:marker])
    return out


def _actions(path: Path, pattern: re.Pattern[str]) -> set[str]:
    lines = _code_lines(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    matched = 0
    mentions = 0
    for line in lines:
        mentions += line.count("next_action")
        for hit in pattern.finditer(line):
            found.add(hit.group(1))
            matched += 1
    # Completeness: every `next_action` left in code was part of a branch the pattern
    # understood. A switch statement, a lookup table or a computed name would show up
    # here as an unmatched mention rather than passing silently as an empty set.
    assert matched == mentions, (
        f"{path.name}: {mentions} next_action mentions in code but "
        f"{matched} matched the branch pattern {pattern.pattern!r}"
    )
    return found


def test_the_vocabulary_extractor_sees_branches_and_not_prose(tmp_path: Path) -> None:
    """The extractor is a regex over source, so it gets its own both-directions proof."""
    spliced = tmp_path / "spliced.js"
    spliced.write_text("if (data.next_action === 'zzz') boom();\n", encoding="utf-8")
    assert _actions(spliced, _BRANCH) == {"zzz"}

    prose = tmp_path / "prose.js"
    prose.write_text(
        "// the old next_action === 'handoff' arm is gone\n"
        "var msg = 'ask_contact';\n",
        encoding="utf-8",
    )
    assert _actions(prose, _BRANCH) == set()

    hidden = tmp_path / "hidden.js"
    hidden.write_text("switch (data.next_action) { case 'handoff': break; }\n", encoding="utf-8")
    with pytest.raises(AssertionError, match="matched the branch pattern"):
        _actions(hidden, _BRANCH)


def test_widget_and_server_speak_one_vocabulary() -> None:
    branched = _actions(_WIDGET, _BRANCH)
    # Direction 1 -- the defect. Every name the widget reacts to must be one the server
    # can produce. 'ask_contact', 'confirm_contact' and 'handoff' all failed this.
    assert branched <= SITE_V2_ACTIONS, sorted(branched - SITE_V2_ACTIONS)
    # Direction 2 -- what makes this stronger than the `<=` pin that let the drift happen.
    # 'answer' is the deliberate fall-through: it needs no branch because a plain reply is
    # already painted above. Any OTHER action the server gains must be handled here.
    assert SITE_V2_ACTIONS - branched == {"answer"}, sorted(SITE_V2_ACTIONS - branched)

    # The fake server in the node suite is the widget's only other counterparty, and it
    # was the only implementation of the dead vocabulary anywhere.
    assert _actions(_FAKE_SERVER, _EMITTED) == set(SITE_V2_ACTIONS)


def test_site_reply_cannot_name_an_action_the_widget_does_not_know() -> None:
    for action in sorted(SITE_V2_ACTIONS):
        assert SiteV2Reply("ok", action).next_action == action
    assert SiteV2Reply(message="ok").next_action == "answer"
    for dead in ("ask_contact", "confirm_contact", "handoff", ""):
        with pytest.raises(ValueError, match="SITE_V2_ACTIONS"):
            SiteV2Reply("ok", dead)


def test_a_rejected_site_action_is_logged_with_a_reason_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="app.surfaces.site_v2"):
        with pytest.raises(ValueError):
            SiteV2Reply("ok", "handoff")
    messages = [record.getMessage() for record in caplog.records]
    assert "site reply rejected reason=next_action_not_in_vocabulary" in messages
    # The reply text and the rejected value stay out of the log line.
    assert not any("handoff" in message or "ok" in message.split() for message in messages)


def test_the_inline_contact_capture_form_is_retired() -> None:
    """Retired in h2d: the form opened only on 'ask_contact', which the server cannot emit.

    Its submit handler read the two live actions as a failed capture, so had the form ever
    been reachable a visitor whose contact WAS saved would have been told to try again.
    """
    source = _WIDGET.read_text(encoding="utf-8")
    for gone in ("showContactCapture", "postContact", "ask-mia-contact", "contactBlock"):
        assert gone not in source, gone
