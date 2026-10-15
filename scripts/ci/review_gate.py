"""Publish the ``review-gate`` commit status for one pull request.

Claude and GPT push as the same GitHub user, so this is process enforcement, not identity
enforcement. Assaf's deploy go stays the real control. The status is ``success`` only when:

* the PR body names a chunk (``Chunk: V2-NN``), an author agent and the other agent as reviewer,
  and an ``## Evidence`` section of ``- <capability>: <STATE>`` lines with no percentage;
* a comment from an allowed reviewer login states ``Verdict: PASS`` or ``PASS_WITH_NITS`` for
  the exact head SHA the API reports now, with a ``Reviewer-Run`` id, and its
  ``Reviewer-Agent`` is not the author agent. The latest such verdict decides.

Decision logic is the pure ``evaluate``; ``run`` and ``GitHubApi`` are the thin I/O around it.
Run as ``python3 scripts/ci/review_gate.py`` with GITHUB_TOKEN, GITHUB_REPOSITORY, PR_NUMBER and
REVIEW_GATE_ALLOWED_REVIEWERS (comma separated logins) set. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

CONTEXT = "review-gate"
AGENTS = frozenset({"claude", "codex"})
EVIDENCE_STATES = (
    "CODE_CHECKED",
    "LOCAL_TESTED",
    "INTEGRATION_TESTED",
    "STAGING_VERIFIED",
    "DEPLOYED",
    "PHONE_VERIFIED",
    "BLOCKED",
    "NOT_INCLUDED",
)
PASS_VERDICTS = frozenset({"PASS", "PASS_WITH_NITS"})
VERDICTS = PASS_VERDICTS | {"FAIL", "NEEDS_CHANGES"}
DESCRIPTION_LIMIT = 140  # GitHub rejects a longer status description with 422.

_HTML_COMMENT = re.compile(r"<!--.*?(?:-->|\Z)", re.S)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)[ \t#]*$")
_CHUNK = re.compile(r"V2-\d{2}")
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{2,99}")
_EVIDENCE_LINE = re.compile(r"[-*][ \t]+\S.*:[ \t]*(?:" + "|".join(EVIDENCE_STATES) + ")")
_PERCENT = re.compile(r"%|\bpercent", re.I)
_BRANCH_AGENT = re.compile(r"(claude|codex)/")
_REPO = re.compile(r"[\w.-]+/[\w.-]+")
_PR_NUMBER = re.compile(r"[1-9]\d{0,9}")


@dataclass(frozen=True)
class Decision:
    passed: bool
    reason: str

    @property
    def state(self) -> str:
        return "success" if self.passed else "failure"


@dataclass(frozen=True)
class _Verdict:
    agent: str
    verdict: str
    run: str
    order: tuple[str, int]


def _visible(text: str | None) -> str:
    """Keep what a reader sees as ordinary text: no HTML comments, no fenced code blocks."""
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    kept: list[str] = []
    fence: str | None = None
    for line in _HTML_COMMENT.sub("", text).split("\n"):
        opening = _FENCE.match(line)
        if fence is None:
            if opening:
                fence = opening.group(1)
            else:
                kept.append(line)
            continue
        stripped = line.strip()
        if stripped and set(stripped) == {fence[0]} and len(stripped) >= len(fence):
            fence = None
    return "\n".join(kept)


def _single(text: str, name: str, norm: Callable[[str], str] = str.lower) -> str | None:
    """The one value of a ``Name: value`` line, or None when absent, empty or contradictory."""
    pattern = re.compile(rf"^ {{0,3}}{re.escape(name)}:[ \t]*(.*?)[ \t]*$", re.I | re.M)
    values = {norm(value) for value in pattern.findall(text)}
    return values.pop() if len(values) == 1 else None


def _evidence_lines(text: str) -> list[str] | None:
    lines: list[str] = []
    found = in_section = False
    for line in text.split("\n"):
        heading = _HEADING.match(line)
        if heading:
            in_section = heading.group(1).strip().lower() == "evidence"
            found = found or in_section
        elif in_section and line.strip():
            lines.append(line.strip())
    return lines if found else None


def _check_body(body: str | None, head_ref: str) -> tuple[str | None, list[str]]:
    """Return the author agent (when valid) and every problem found in the PR body."""
    text = _visible(body)
    problems: list[str] = []
    chunk = _single(text, "Chunk", str.upper)
    if chunk is None or not _CHUNK.fullmatch(chunk):
        problems.append("Chunk must be one line like V2-01")
    author = _single(text, "Author-Agent")
    if author not in AGENTS:
        problems.append("Author-Agent must be exactly one of claude, codex")
        author = None
    reviewer = _single(text, "Reviewer-Agent")
    if reviewer not in AGENTS or reviewer == author:
        problems.append("Reviewer-Agent must be the agent that did not author the PR")
    branch = _BRANCH_AGENT.match(head_ref or "")
    if author and branch and branch.group(1) != author:
        problems.append(f"branch {head_ref} is {branch.group(1)}'s but Author-Agent is {author}")
    lines = _evidence_lines(text)
    if not lines:
        problems.append("Evidence section is missing or empty")
    else:
        if any(_PERCENT.search(line) for line in lines):
            problems.append("Evidence uses a percentage; state one evidence state instead")
        bad = next((line for line in lines if not _EVIDENCE_LINE.fullmatch(line)), None)
        if bad is not None:
            problems.append(f"Evidence line must read '- <capability>: <STATE>' (got: {bad[:40]})")
    return author, problems


def _parse_verdict(
    comment: Mapping[str, Any], head_sha: str, allowed: frozenset[str]
) -> _Verdict | None:
    login = str((comment.get("user") or {}).get("login") or "").lower()
    if login not in allowed:
        return None
    text = _visible(comment.get("body"))
    agent = _single(text, "Reviewer-Agent")
    verdict = _single(text, "Verdict", str.upper)
    run = _single(text, "Reviewer-Run", str)
    if _single(text, "Head-SHA") != head_sha.lower() or agent not in AGENTS:
        return None
    if verdict not in VERDICTS or run is None or not _RUN_ID.fullmatch(run):
        return None
    order = (str(comment.get("created_at") or ""), int(comment.get("id") or 0))
    return _Verdict(agent, verdict, run, order)


def evaluate(
    *,
    head_sha: str,
    head_ref: str,
    body: str | None,
    comments: Iterable[Mapping[str, Any]],
    allowed_reviewers: Iterable[str],
) -> Decision:
    allowed = frozenset(login.lower() for login in allowed_reviewers)
    short = head_sha[:7]
    author, problems = _check_body(body, head_ref)
    if problems:
        return Decision(False, "; ".join(problems))
    verdicts = [v for c in comments if (v := _parse_verdict(c, head_sha, allowed))]
    independent = sorted((v for v in verdicts if v.agent != author), key=lambda v: v.order)
    if not independent:
        if verdicts:
            return Decision(False, f"only the author agent ({author}) reviewed head {short}")
        return Decision(False, f"no reviewer verdict for head {short} from an allowed reviewer")
    latest = independent[-1]
    if latest.verdict in PASS_VERDICTS:
        return Decision(True, f"{latest.verdict} by {latest.agent} ({latest.run}) for head {short}")
    return Decision(False, f"latest verdict for head {short} is {latest.verdict} by {latest.agent}")


class GitHubApi:
    """The three GitHub REST calls the gate needs. ``opener`` is injectable for tests."""

    def __init__(self, token: str, *, base: str = "https://api.github.com", opener=None) -> None:
        self._token = token
        self._base = base.rstrip("/")
        self._open = opener or urllib.request.urlopen

    def _call(self, method: str, path: str, payload: Mapping[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(self._base + path, data=data, method=method)
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("Authorization", f"Bearer {self._token}")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "mia-review-gate")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        with self._open(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8") or "null")

    def get_json(self, path: str) -> Any:
        return self._call("GET", path)

    def post_json(self, path: str, payload: Mapping[str, Any]) -> Any:
        return self._call("POST", path, payload)

    def list_all(self, path: str, *, per_page: int = 100, max_pages: int = 50) -> list[Any]:
        sep = "&" if "?" in path else "?"
        items: list[Any] = []
        for page in range(1, max_pages + 1):
            batch = self.get_json(f"{path}{sep}per_page={per_page}&page={page}")
            items.extend(batch)
            if len(batch) < per_page:
                return items
        raise RuntimeError(f"{path} has more than {max_pages * per_page} items")


def _publish(
    api: Any, repo: str, sha: str, state: str, reason: str, target_url: str | None
) -> None:
    if len(reason) > DESCRIPTION_LIMIT:
        reason = reason[: DESCRIPTION_LIMIT - 3] + "..."
    payload = {"state": state, "context": CONTEXT, "description": reason}
    if target_url:
        payload["target_url"] = target_url
    api.post_json(f"/repos/{repo}/statuses/{sha}", payload)


def run(
    api: Any,
    *,
    repo: str,
    number: int,
    allowed_reviewers: Iterable[str],
    target_url: str | None = None,
) -> Decision | None:
    """Evaluate one PR against the API as it is now and publish the status on its head SHA."""
    pull = api.get_json(f"/repos/{repo}/pulls/{number}")
    if pull.get("state") != "open":
        print(f"{CONTEXT}: PR #{number} is not open; nothing to publish")
        return None
    sha = pull["head"]["sha"]
    try:
        comments = api.list_all(f"/repos/{repo}/issues/{number}/comments")
        decision = evaluate(
            head_sha=sha,
            head_ref=pull["head"].get("ref") or "",
            body=pull.get("body"),
            comments=comments,
            allowed_reviewers=allowed_reviewers,
        )
    except Exception:
        # Fail closed: a stale success on this SHA must not outlive a gate that cannot evaluate.
        _publish(api, repo, sha, "error", "review-gate could not evaluate; see the run", target_url)
        raise
    _publish(api, repo, sha, decision.state, decision.reason, target_url)
    print(f"{CONTEXT} {decision.state} for {sha[:7]}: {decision.reason}")
    return decision


def main(env: Mapping[str, str] | None = None, api: Any = None) -> int:
    env = os.environ if env is None else env
    token = (env.get("GITHUB_TOKEN") or "").strip()
    repo = env.get("GITHUB_REPOSITORY") or ""
    number = env.get("PR_NUMBER") or ""
    allowed = frozenset(
        part.strip().lower()
        for part in (env.get("REVIEW_GATE_ALLOWED_REVIEWERS") or "").split(",")
        if part.strip()
    )
    problems = []
    if not token:
        problems.append("GITHUB_TOKEN is not set")
    if not _REPO.fullmatch(repo):
        problems.append("GITHUB_REPOSITORY must be owner/name")
    if not _PR_NUMBER.fullmatch(number):
        problems.append("PR_NUMBER must be a positive integer")
    if not allowed:
        problems.append("REVIEW_GATE_ALLOWED_REVIEWERS names no login")
    if problems:
        print(f"{CONTEXT}: " + "; ".join(problems), file=sys.stderr)
        return 2
    if api is None:
        api = GitHubApi(token, base=env.get("GITHUB_API_URL") or "https://api.github.com")
    server, run_id = env.get("GITHUB_SERVER_URL"), env.get("GITHUB_RUN_ID")
    target_url = f"{server}/{repo}/actions/runs/{run_id}" if server and run_id else None
    run(api, repo=repo, number=int(number), allowed_reviewers=allowed, target_url=target_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
