"""Offline regression checks for the source trusted by the status-writing job."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.ci.test_review_gate import COWORK, ROOT, WORKFLOW, job, triggers


def test_privileged_workflow_uses_only_trusted_events_and_default_branch_code() -> None:
    assert set(triggers()) == {"pull_request_target", "issue_comment"}
    checkout, publisher = job()["steps"]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {
        "ref": "${{ github.event.repository.default_branch }}",
        "persist-credentials": False,
    }
    assert publisher["run"] == "python3 scripts/ci/review_gate.py"


@pytest.mark.parametrize("fork", [False, True], ids=["same-repository", "fork"])
def test_tampered_pr_publisher_is_never_selected(tmp_path: Path, fork: bool) -> None:
    """Model checkout refs with separate local trees, then execute the selected publisher.

    The PR copy fabricates success without a review. The default-branch copy must evaluate
    the empty review as failure for every configured event, even for a non-default PR base.
    This exercises source selection offline; it does not emulate the GitHub Actions runner.
    """
    trusted = tmp_path / "default"
    untrusted = tmp_path / ("fork" if fork else "pr")
    for tree in (trusted, untrusted):
        (tree / "scripts" / "ci").mkdir(parents=True)
    relative = Path("scripts/ci/review_gate.py")
    shutil.copyfile(ROOT / relative, trusted / relative)
    (untrusted / relative).write_text(
        "from types import SimpleNamespace\n"
        "def evaluate(**kwargs):\n"
        "    return SimpleNamespace(state='success')\n",
        encoding="utf-8",
        newline="\n",
    )
    checkout = job()["steps"][0]
    configured_ref = checkout["with"].get("ref")
    refs = {
        "${{ github.event.repository.default_branch }}": trusted,
        "${{ github.event.pull_request.head.sha }}": untrusted,
        "${{ github.event.pull_request.head.ref }}": untrusted,
        "${{ github.head_ref }}": untrusted,
        # A contributor-controlled non-default target is not the trusted default branch.
        "${{ github.event.pull_request.base.sha }}": untrusted,
        "${{ github.base_ref }}": untrusted,
    }
    probe = (
        "import runpy; "
        "gate = runpy.run_path('scripts/ci/review_gate.py'); "
        "print(gate['evaluate'](head_sha='a' * 40, head_ref='claude/test', "
        "body='', comments=[], allowed_reviewers=['assaf0buskila']).state)"
    )
    for event in triggers():
        if configured_ref is None:
            # checkout's implicit ref follows the event: PR merge, PR target, or default.
            tree = trusted if event == "issue_comment" else untrusted
        else:
            assert configured_ref in refs, f"unverified checkout ref: {configured_ref}"
            tree = refs[configured_ref]
        result = subprocess.run(
            [sys.executable, "-I", "-c", probe],
            cwd=tree,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        assert result.stdout.strip() == "failure", (
            f"{event} executed the PR's fabricated-success publisher"
        )


def test_workflow_and_docs_disclose_the_accepted_shared_login_limit() -> None:
    for path in (WORKFLOW, COWORK):
        text = path.read_text(encoding="utf-8").lower()
        assert "process aid" in text
        assert "not a security boundary" in text
        assert "same github login" in text
        assert "self-attested" in text
    assert "#109" in COWORK.read_text(encoding="utf-8")
