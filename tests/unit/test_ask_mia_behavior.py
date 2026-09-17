from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

import pytest
from app.db.models import CrmContactRow
from app.db.session import get_session_factory
from app.db.site_v2 import SiteV2MessageRow
from app.main import app
from app.surfaces.site_v2 import SITE_V2_ACTIONS, SiteV2Reply
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.unit.test_website_v2 import _headers, _new, _SiteClient


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


# A `//` that follows a colon is a URL scheme, not a comment. Without this the truncation
# ate the rest of any line holding an https:// literal -- including a branch sharing that
# line -- and, because `mentions` is counted after truncation too, the completeness check
# below stayed silent about it. That is the failure mode this whole file exists to prevent.
_COMMENT = re.compile(r"(?<!:)//")


def _code_lines(source: str) -> list[str]:
    """Drop whole-line and trailing `//` comments so a name mentioned in prose is data.

    Line-oriented on purpose: a real JS parser is not worth carrying here, and the only
    thing this has to get right is that `next_action` written in a comment is not a
    branch. `_actions` cross-checks the count, so an occurrence this misses is loud.
    """
    out = []
    for line in source.split("\n"):
        marker = _COMMENT.search(line)
        out.append(line if marker is None else line[: marker.start()])
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

    # The widget is full of wa.me URLs. A branch sharing a line with one must still be seen,
    # and must not be silently dropped along with the rest of the line.
    scheme = tmp_path / "scheme.js"
    scheme.write_text(
        "var u = 'https://wa.me/x'; if (data.next_action === 'handoff') boom();\n",
        encoding="utf-8",
    )
    assert _actions(scheme, _BRANCH) == {"handoff"}


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


# --- the two wire paths -------------------------------------------------------
#
# SiteV2Reply is not the only carrier of next_action, and refusing to construct is not the
# right answer on a path that has already written to the database. These two pin the other
# half of the invariant: what the server puts on the wire is always a name the widget reads.


def test_a_replayed_legacy_action_is_degraded_before_it_reaches_the_wire(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A row stored before 40747c8 replays through begin_site_message, not SiteV2Reply."""
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: _SiteClient())
    with TestClient(app) as client:
        session_id, credential = _new(client)
        payload = {"text": "יש לי סטודיו לפילאטיס", "client_message_id": "legacy-1"}
        first = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json=payload,
            headers=_headers(credential),
        )
        assert first.status_code == 200, first.text
        with get_session_factory()() as db:
            row = db.scalar(
                select(SiteV2MessageRow).where(
                    SiteV2MessageRow.session_id == session_id,
                    SiteV2MessageRow.client_message_id == "legacy-1",
                )
            )
            assert row is not None and row.status == "completed"
            stored = json.loads(row.response_json)
            stored["next_action"] = "confirm_contact"
            row.response_json = json.dumps(stored, ensure_ascii=False)
            db.commit()
        with caplog.at_level(logging.ERROR, logger="app.surfaces.site_v2"):
            replay = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json=payload,
                headers=_headers(credential),
            )
        assert replay.status_code == 200, replay.text
        # Not 'confirm_contact': the widget has no branch for it, so the honest label for
        # what it will actually do with this reply is the fall-through.
        assert replay.json()["next_action"] == "answer"
        assert replay.json()["message"] == first.json()["message"]
        assert "site reply rejected reason=next_action_not_in_vocabulary" in [
            record.getMessage() for record in caplog.records
        ]


def test_an_unreadable_action_degrades_rather_than_rolling_back_the_capture(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The guard runs after the contact save, so it must never be the thing that loses it.

    SITE_V2_ACTIONS is narrowed here to put the turn path in the shape a future action
    would create: a value the widget cannot read, chosen after everything is written.
    """
    monkeypatch.setenv("MIA_WEBSITE_V2_ENABLED", "true")
    monkeypatch.setenv("MIA_CRM_V2_ENABLED", "true")
    monkeypatch.setattr("app.surfaces.site_v2.build_site_client", lambda _settings: _SiteClient())
    monkeypatch.setattr("app.surfaces.site_v2.SITE_V2_ACTIONS", frozenset({"answer"}))
    with TestClient(app) as client:
        session_id, credential = _new(client)
        with caplog.at_level(logging.ERROR, logger="app.surfaces.site_v2"):
            captured = client.post(
                f"/v1/website/sessions/{session_id}/messages",
                json={
                    "text": "תחזרו אליי בבקשה",
                    # A number of this file's own: CRM v2 resolves a contact by phone across
                    # conversations, so reusing another test's fixture makes that test's row
                    # belong to this session and its own assertion fail.
                    "phone": "+44 20 7946 0321",
                    "client_message_id": "capture-1",
                },
                headers=_headers(credential),
            )
        assert captured.status_code == 200, captured.text
        assert captured.json()["next_action"] == "answer"
        assert "site reply rejected reason=next_action_not_in_vocabulary" in [
            record.getMessage() for record in caplog.records
        ]
        with get_session_factory()() as db:
            contact = db.scalar(
                select(CrmContactRow).where(CrmContactRow.conversation_id == session_id)
            )
            assert contact is not None, "the capture must survive an unreadable label"
