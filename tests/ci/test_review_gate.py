"""V2-01: the review-gate commit status that makes cross-agent review a visible process control.

Claude and GPT both push as the same GitHub user, so this gate is process enforcement and not
identity enforcement. These tests pin what it does decide: a PASS counts only when it is for the
exact head SHA, comes from an allowed reviewer login, carries a reviewer run id, and names an
agent other than the PR's author agent. They also pin the workflow that publishes the status
(trigger set, permissions, pinned actions) and the templates the gate parses.
"""

from __future__ import annotations

import io
import json
import re
import subprocess
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml
from scripts.ci import review_gate

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "review-gate.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
PR_TEMPLATE = ROOT / ".github" / "pull_request_template.md"
ISSUE_TEMPLATE = ROOT / ".github" / "ISSUE_TEMPLATE" / "os-v2-chunk.yml"
COWORK = ROOT / "docs" / "COWORK.md"
AGENTS_MD = ROOT / "AGENTS.md"

HEAD = "3f9c1d2e4b5a69788796a5b4c3d2e1f001122334"
OLD = "0123456789abcdef0123456789abcdef01234567"
REVIEWER_LOGIN = "assaf0buskila"
ALLOWED = frozenset({REVIEWER_LOGIN})
STATES = (
    "CODE_CHECKED",
    "LOCAL_TESTED",
    "INTEGRATION_TESTED",
    "STAGING_VERIFIED",
    "DEPLOYED",
    "PHONE_VERIFIED",
    "BLOCKED",
    "NOT_INCLUDED",
)


def pr_body(
    *,
    chunk: str | None = "V2-01",
    author: str | None = "claude",
    reviewer: str | None = "codex",
    evidence: tuple[str, ...] | None = ("- cowork queue: LOCAL_TESTED",),
) -> str:
    lines = ["Adds the review gate.", ""]
    if chunk is not None:
        lines.append(f"Chunk: {chunk}")
    if author is not None:
        lines.append(f"Author-Agent: {author}")
    if reviewer is not None:
        lines.append(f"Reviewer-Agent: {reviewer}")
    lines.append("")
    if evidence is not None:
        lines += ["## Evidence", "", *evidence, ""]
    lines += ["## Gates run", "", "pytest, ruff.", ""]
    return "\n".join(lines)


def verdict_text(
    *,
    agent: str = "codex",
    sha: str = HEAD,
    verdict: str = "PASS",
    run: str | None = "codex-job-7f3a2c",
) -> str:
    lines = [f"Reviewer-Agent: {agent}"]
    if run is not None:
        lines.append(f"Reviewer-Run: {run}")
    lines += [f"Head-SHA: {sha}", f"Verdict: {verdict}"]
    return "\n".join(lines)


def comment(
    text: str | None = None,
    *,
    login: str = REVIEWER_LOGIN,
    cid: int = 1,
    at: str = "2026-10-15T10:00:00Z",
) -> dict:
    body = verdict_text() if text is None else text
    return {"id": cid, "user": {"login": login}, "created_at": at, "body": body}


def decide(
    comments: list[dict],
    *,
    body: str | None = None,
    head_ref: str = "claude/os-v2-01-cowork-queue",
    head_sha: str = HEAD,
    allowed: frozenset[str] = ALLOWED,
) -> review_gate.Decision:
    return review_gate.evaluate(
        head_sha=head_sha,
        head_ref=head_ref,
        body=pr_body() if body is None else body,
        comments=comments,
        allowed_reviewers=allowed,
    )


def assert_fails(decision: review_gate.Decision, *needles: str) -> None:
    assert not decision.passed, decision.reason
    assert decision.state == "failure"
    for needle in needles:
        assert needle.lower() in decision.reason.lower(), decision.reason


# ---------------------------------------------------------------- the verdict rules


def test_other_agents_pass_at_the_head_sha_passes() -> None:
    decision = decide([comment()])
    assert decision.passed, decision.reason
    assert decision.state == "success"


def test_pass_with_nits_passes() -> None:
    assert decide([comment(verdict_text(verdict="PASS_WITH_NITS"))]).passed


def test_gate_is_symmetric_for_a_codex_authored_pr() -> None:
    body = pr_body(author="codex", reviewer="claude")
    ref = "codex/os-v2-01-cowork-queue"
    reviewed_by_claude = comment(verdict_text(agent="claude"))
    assert decide([reviewed_by_claude], body=body, head_ref=ref).passed
    assert_fails(decide([comment()], body=body, head_ref=ref), "author agent")


def test_only_pass_from_the_author_agent_fails() -> None:
    decision = decide([comment(verdict_text(agent="claude"))])
    assert_fails(decision, "author agent", "claude")


def test_pass_for_an_older_sha_fails() -> None:
    assert_fails(decide([comment(verdict_text(sha=OLD))]), "no reviewer verdict", HEAD[:7])


def test_a_new_push_invalidates_a_pass_given_to_the_previous_head() -> None:
    comments = [comment()]
    assert decide(comments).passed
    assert_fails(decide(comments, head_sha=OLD), "no reviewer verdict")


@pytest.mark.parametrize("comments", [[], [comment("LGTM, ship it")]], ids=["none", "prose-only"])
def test_no_verdict_fails(comments: list[dict]) -> None:
    assert_fails(decide(comments), "no reviewer verdict")


@pytest.mark.parametrize("sha", [HEAD[:7], HEAD[:12], HEAD[:-1], HEAD + "0", "HEAD", ""])
def test_the_verdict_must_name_the_full_head_sha(sha: str) -> None:
    assert_fails(decide([comment(verdict_text(sha=sha))]), "no reviewer verdict")


def test_sha_and_field_names_are_case_insensitive() -> None:
    text = (
        f"reviewer-agent: Codex\nREVIEWER-RUN: codex-job-7f3a2c\nhead-sha: {HEAD.upper()}\n"
        "verdict: pass"
    )
    assert decide([comment(text)]).passed


@pytest.mark.parametrize(
    "verdict", ["PASSED", "PASS WITH NITS", "PASS-WITH-NITS", "PASS_WITH_NITS maybe", "LGTM", ""]
)
def test_only_the_exact_verdict_words_pass(verdict: str) -> None:
    assert_fails(decide([comment(verdict_text(verdict=verdict))]), "no reviewer verdict")


@pytest.mark.parametrize("verdict", ["FAIL", "NEEDS_CHANGES"])
def test_a_fail_verdict_fails(verdict: str) -> None:
    assert_fails(decide([comment(verdict_text(verdict=verdict))]), verdict)


def test_a_later_fail_from_the_reviewer_overrides_an_earlier_pass() -> None:
    comments = [
        comment(verdict_text(), cid=1, at="2026-10-15T10:00:00Z"),
        comment(verdict_text(verdict="FAIL"), cid=2, at="2026-10-15T11:00:00Z"),
    ]
    assert_fails(decide(comments), "FAIL")


def test_a_later_pass_overrides_an_earlier_fail_whatever_the_list_order() -> None:
    comments = [
        comment(verdict_text(), cid=2, at="2026-10-15T11:00:00Z"),
        comment(verdict_text(verdict="FAIL"), cid=1, at="2026-10-15T10:00:00Z"),
    ]
    assert decide(comments).passed


def test_a_fail_posted_under_the_author_tag_neither_blocks_nor_grants() -> None:
    reviewer_pass = comment(verdict_text(), cid=1)
    author_fail = comment(verdict_text(agent="claude", verdict="FAIL"), cid=2)
    assert decide([reviewer_pass, author_fail]).passed
    assert_fails(decide([author_fail]), "author agent")


def test_the_reviewer_login_must_be_allowed() -> None:
    assert_fails(decide([comment(login="someone-else")]), "no reviewer verdict")
    assert_fails(decide([comment()], allowed=frozenset()), "no reviewer verdict")


def test_login_comparison_ignores_case() -> None:
    assert decide([comment(login="Assaf0Buskila")]).passed
    assert decide([comment()], allowed=frozenset({"ASSAF0BUSKILA"})).passed


@pytest.mark.parametrize("run", [None, "", "   ", "<job-id>", "x", "two words"])
def test_the_verdict_must_cite_a_reviewer_run_id(run: str | None) -> None:
    assert_fails(decide([comment(verdict_text(run=run))]), "no reviewer verdict")


@pytest.mark.parametrize("agent", ["gpt", "someone", "claude-codex", ""])
def test_the_reviewer_agent_must_be_claude_or_codex(agent: str) -> None:
    assert_fails(decide([comment(verdict_text(agent=agent))]), "no reviewer verdict")


def test_conflicting_duplicate_lines_make_a_comment_void() -> None:
    text = verdict_text(verdict="FAIL") + "\nVerdict: PASS"
    assert_fails(decide([comment(text)]), "no reviewer verdict")
    other_sha = verdict_text() + f"\nHead-SHA: {OLD}"
    assert_fails(decide([comment(other_sha)]), "no reviewer verdict")


def test_the_same_line_repeated_is_not_a_conflict() -> None:
    assert decide([comment(verdict_text() + "\nVerdict: PASS")]).passed


def test_verdict_inside_a_code_fence_html_comment_or_quote_does_not_count() -> None:
    inner = verdict_text()
    fenced = f"Template for the reviewer:\n```\n{inner}\n```\n"
    tilde = f"~~~text\n{inner}\n~~~\n"
    hidden = f"<!--\n{inner}\n-->\n"
    quoted = "\n".join(f"> {line}" for line in inner.splitlines())
    for text in (fenced, tilde, hidden, quoted):
        assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_an_unclosed_html_comment_or_fence_hides_the_rest_as_markdown_does() -> None:
    inner = verdict_text()
    for text in (f"<!--\n{inner}\n", f"```\n{inner}\n"):
        assert_fails(decide([comment(text)]), "no reviewer verdict")


def test_a_verdict_after_a_closed_fence_counts() -> None:
    text = "```\nexample only\n```\n" + verdict_text()
    assert decide([comment(text)]).passed


def test_a_comment_marker_inside_code_does_not_hide_what_follows() -> None:
    inline = "The PR removes the `<!--` hack and the ``<!--`` one.\n\n" + verdict_text()
    fenced = "Quoted from the diff:\n```html\n<!-- hack\n```\n" + verdict_text()
    tilde = "~~~\n<!--\n~~~\n" + verdict_text()
    for text in (inline, fenced, tilde):
        assert decide([comment(text)]).passed, text


def test_a_pr_body_that_quotes_a_comment_marker_in_code_still_reads_its_evidence() -> None:
    inline = pr_body().replace("Adds the review gate.", "Removes the `<!--` hack.")
    fenced = pr_body().replace("Adds the review gate.", "```html\n<!--\n```")
    for body in (inline, fenced):
        assert decide([comment()], body=body).passed, body


def test_a_real_html_comment_still_hides_its_text_after_a_stray_backtick() -> None:
    # An unpaired backtick is not a code span, so the marker after it opens a real comment.
    stray = "a stray ` tick <!--\n" + verdict_text() + "\n-->\n"
    assert_fails(decide([comment(stray)]), "no reviewer verdict")
    # A comment closed on the same line hides only itself.
    inline = "Reviewer-Agent: codex\nReviewer-Run: codex-job-7f3a2c\nHead-SHA: " + HEAD
    assert decide([comment(inline + "\nVerdict: PASS <!-- fine -->")]).passed
    assert_fails(decide([comment(inline + "\n<!-- Verdict: PASS -->")]), "no reviewer verdict")


def test_a_fence_opener_inside_an_html_comment_is_just_hidden_text() -> None:
    assert decide([comment("<!--\n```\n-->\n" + verdict_text())]).passed


def test_crlf_bodies_are_read_like_lf_bodies() -> None:
    assert decide([comment(verdict_text().replace("\n", "\r\n"))]).passed
    assert decide([comment()], body=pr_body().replace("\n", "\r\n")).passed


def test_prose_around_the_fields_is_fine() -> None:
    text = "Reviewed the diff at the head.\n\n" + verdict_text() + "\n\nNits are in the thread."
    assert decide([comment(text)]).passed


def test_comments_without_a_user_or_body_are_skipped() -> None:
    odd = [{"id": 5, "user": None, "body": None, "created_at": "2026-10-15T09:00:00Z"}, {}]
    assert decide([*odd, comment()]).passed


# ---------------------------------------------------------------- the PR body rules


@pytest.mark.parametrize(
    ("body", "needle"),
    [
        (pr_body(chunk=None), "Chunk"),
        (pr_body(chunk="V2-1"), "Chunk"),
        (pr_body(chunk="<V2-NN>"), "Chunk"),
        (pr_body(author=None), "Author-Agent"),
        (pr_body(author="gpt"), "Author-Agent"),
        (pr_body(author="<claude|codex>"), "Author-Agent"),
        (pr_body(reviewer=None), "Reviewer-Agent"),
        (pr_body(reviewer="claude"), "Reviewer-Agent"),
        (pr_body(reviewer="gpt"), "Reviewer-Agent"),
    ],
    ids=[
        "chunk-missing",
        "chunk-short",
        "chunk-placeholder",
        "author-missing",
        "author-unknown",
        "author-placeholder",
        "reviewer-missing",
        "reviewer-same-as-author",
        "reviewer-unknown",
    ],
)
def test_a_pr_body_without_valid_chunk_and_agent_tags_fails(body: str, needle: str) -> None:
    assert_fails(decide([comment()], body=body), needle)


def test_conflicting_author_tags_in_the_pr_body_fail() -> None:
    body = pr_body() + "\nAuthor-Agent: codex\n"
    assert_fails(decide([comment()], body=body), "Author-Agent")


def test_the_branch_prefix_must_match_the_author_agent() -> None:
    assert_fails(
        decide([comment()], head_ref="codex/os-v2-01-cowork-queue"),
        "branch",
        "claude",
    )
    # A branch outside the claude/ and codex/ families is not second-guessed.
    assert decide([comment()], head_ref="hotfix/pr-gate").passed


def test_a_missing_evidence_section_fails() -> None:
    assert_fails(decide([comment()], body=pr_body(evidence=None)), "Evidence")
    assert_fails(decide([comment()], body=pr_body(evidence=())), "Evidence")


@pytest.mark.parametrize("state", STATES)
def test_every_documented_evidence_state_is_accepted(state: str) -> None:
    assert decide([comment()], body=pr_body(evidence=(f"- cowork queue: {state}",))).passed


def test_several_capabilities_each_carry_their_own_state() -> None:
    evidence = (
        "- review gate: LOCAL_TESTED",
        "* PR template: CODE_CHECKED",
        "- branch protection: NOT_INCLUDED",
    )
    assert decide([comment()], body=pr_body(evidence=evidence)).passed


@pytest.mark.parametrize(
    "line",
    [
        "- cowork queue: 90%",
        "- cowork queue: 90% done",
        "- cowork queue: LOCAL_TESTED 90%",
        "- cowork queue: LOCAL_TESTED (90% of cases)",
        "- cowork queue: ninety percent",
        "- cowork queue: ninety per cent",
    ],
)
def test_a_percentage_in_the_evidence_section_fails(line: str) -> None:
    assert_fails(decide([comment()], body=pr_body(evidence=(line,))), "percentage")


def test_the_word_percentile_is_not_a_percentage() -> None:
    evidence = ("- p95 percentile latency: LOCAL_TESTED",)
    assert decide([comment()], body=pr_body(evidence=evidence)).passed


@pytest.mark.parametrize(
    "line",
    [
        "- cowork queue: DONE",
        "- cowork queue: local_tested",
        "- cowork queue: LOCAL_TESTED, DEPLOYED",
        "- cowork queue: LOCAL_TESTED: DEPLOYED",
        "- cowork queue: LOCAL_TESTED (pytest passed)",
        "- cowork queue",
        "- <capability>: <STATE>",
        "cowork queue: LOCAL_TESTED",
        "Everything works.",
    ],
)
def test_an_evidence_line_must_end_in_exactly_one_known_state(line: str) -> None:
    assert_fails(decide([comment()], body=pr_body(evidence=(line,))), "Evidence")


def test_one_bad_evidence_line_fails_even_beside_good_ones() -> None:
    evidence = ("- review gate: LOCAL_TESTED", "- phone: PHONE_TESTED")
    assert_fails(decide([comment()], body=pr_body(evidence=evidence)), "Evidence")


def test_a_percentage_outside_the_evidence_section_is_not_the_gates_business() -> None:
    body = pr_body() + "\nCoverage went from 80% to 85%.\n"
    assert decide([comment()], body=body).passed


def test_body_fields_inside_html_comments_and_fences_do_not_count() -> None:
    hidden = "<!--\nChunk: V2-01\nAuthor-Agent: claude\nReviewer-Agent: codex\n-->\n"
    body = hidden + "## Evidence\n- cowork queue: LOCAL_TESTED\n"
    assert_fails(decide([comment()], body=body), "Chunk")
    fenced = "```\nChunk: V2-01\nAuthor-Agent: claude\nReviewer-Agent: codex\n```\n"
    assert_fails(decide([comment()], body=fenced + "## Evidence\n- a: CODE_CHECKED\n"), "Chunk")


def test_an_empty_or_missing_pr_body_fails() -> None:
    assert_fails(decide([comment()], body=""), "Chunk")


def test_a_passing_verdict_cannot_rescue_a_bad_body_and_vice_versa() -> None:
    assert_fails(decide([comment()], body=pr_body(evidence=("- a: 50%",))), "percentage")
    assert_fails(decide([]), "no reviewer verdict")


def test_every_body_problem_is_reported_not_only_the_first() -> None:
    decision = decide([comment()], body=pr_body(chunk=None, author="gpt", evidence=None))
    assert_fails(decision, "Chunk", "Author-Agent", "Evidence")


# ---------------------------------------------------------------- the published status


class FakeApi:
    """Stands in for GitHubApi: the PR comes from here, so the head SHA is read at run time."""

    def __init__(
        self,
        pr: dict,
        comments: list[dict],
        *,
        comments_error: Exception | None = None,
        post_errors: list[Exception] | None = None,
    ):
        self.pr = pr
        self.comments = comments
        self.comments_error = comments_error
        self.post_errors = list(post_errors or [])  # raised by the first POSTs, in order
        self.paths: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def get_json(self, path: str) -> dict:
        self.paths.append(path)
        return self.pr

    def list_all(self, path: str) -> list[dict]:
        self.paths.append(path)
        if self.comments_error:
            raise self.comments_error
        return self.comments

    def post_json(self, path: str, payload: dict) -> dict:
        if self.post_errors:
            raise self.post_errors.pop(0)
        self.posts.append((path, payload))
        return {}


def pull(
    *, sha: str = HEAD, ref: str = "claude/os-v2-01-cowork-queue", state: str = "open", body=None
) -> dict:
    return {
        "state": state,
        "body": pr_body() if body is None else body,
        "head": {"sha": sha, "ref": ref},
    }


def publish(api: FakeApi, **kwargs):
    return review_gate.run(api, repo="owner/repo", number=7, allowed_reviewers=ALLOWED, **kwargs)


def test_status_is_written_to_the_head_sha_the_api_reports_now() -> None:
    api = FakeApi(pull(sha=HEAD), [comment(verdict_text(sha=OLD))])
    decision = publish(api)
    assert not decision.passed
    assert api.paths[:2] == ["/repos/owner/repo/pulls/7", "/repos/owner/repo/issues/7/comments"]
    assert api.paths[2:] == ["/repos/owner/repo/pulls/7"]
    [(path, payload)] = api.posts
    assert path == f"/repos/owner/repo/statuses/{HEAD}"
    assert payload["context"] == "review-gate"
    assert payload["state"] == "failure"


def test_a_passing_verdict_publishes_success() -> None:
    api = FakeApi(pull(), [comment()])
    assert publish(api).passed
    [(path, payload)] = api.posts
    assert path == f"/repos/owner/repo/statuses/{HEAD}"
    assert payload["state"] == "success"
    assert payload["context"] == "review-gate"


def test_status_description_fits_githubs_limit_and_target_url_is_passed_on() -> None:
    long_body = pr_body(chunk=None, author="gpt", evidence=("- cap: 5%",))
    api = FakeApi(pull(body=long_body), [comment()])
    decision = publish(api, target_url="https://github.com/owner/repo/actions/runs/1")
    assert len(decision.reason) > 140
    [(_, payload)] = api.posts
    assert 100 < len(payload["description"]) <= 140
    assert payload["description"].startswith(decision.reason[:100])
    assert payload["target_url"] == "https://github.com/owner/repo/actions/runs/1"


def test_a_closed_pr_publishes_nothing() -> None:
    api = FakeApi(pull(state="closed"), [comment()])
    assert publish(api) is None
    assert api.posts == []


def test_an_unreadable_comment_list_publishes_error_and_still_fails_the_job() -> None:
    api = FakeApi(pull(), [], comments_error=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        publish(api)
    [(path, payload)] = api.posts
    assert path == f"/repos/owner/repo/statuses/{HEAD}"
    assert payload["state"] == "error"
    assert payload["context"] == "review-gate"


def test_a_failed_final_publish_still_tries_to_write_an_error_status() -> None:
    # A transient 5xx on the verdict POST must not leave an older success alive on this SHA.
    api = FakeApi(pull(), [comment()], post_errors=[RuntimeError("503 on publish")])
    with pytest.raises(RuntimeError, match="503 on publish"):
        publish(api)
    [(path, payload)] = api.posts
    assert path == f"/repos/owner/repo/statuses/{HEAD}"
    assert payload["state"] == "error"
    assert payload["context"] == "review-gate"


def test_when_even_the_error_status_cannot_be_written_the_first_failure_surfaces() -> None:
    api = FakeApi(pull(), [comment()], post_errors=[RuntimeError("first"), RuntimeError("second")])
    with pytest.raises(RuntimeError, match="^first$"):
        publish(api)
    assert api.posts == []


def test_a_missing_pr_body_is_read_as_empty() -> None:
    pr = pull()
    pr["body"] = None
    api = FakeApi(pr, [comment()])
    assert not publish(api).passed


class FakeResponse:
    def __init__(self, payload) -> None:
        self.data = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self.data

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc) -> bool:
        return False


class FakeOpener:
    def __init__(self, responder) -> None:
        self.responder = responder
        self.requests: list = []

    def __call__(self, request, timeout=None):
        self.requests.append((request, timeout))
        return FakeResponse(self.responder(request))


def make_api(opener: FakeOpener) -> review_gate.GitHubApi:
    return review_gate.GitHubApi("tok-123", base="https://api.example.test", opener=opener)


def test_list_all_follows_pages_until_a_short_page() -> None:
    pages = {"1": [{"id": i} for i in range(100)], "2": [{"id": 100}, {"id": 101}]}

    def responder(request):
        return pages[re.search(r"[?&]page=(\d+)", request.full_url).group(1)]

    opener = FakeOpener(responder)
    items = make_api(opener).list_all("/repos/o/r/issues/7/comments")
    assert [item["id"] for item in items] == list(range(102))
    urls = [request.full_url for request, _ in opener.requests]
    assert len(urls) == 2
    prefix = "https://api.example.test/repos/o/r/issues/7/comments?"
    assert all(url.startswith(prefix) for url in urls)
    assert all("per_page=100" in url for url in urls)


def test_requests_carry_the_token_json_body_and_a_timeout() -> None:
    opener = FakeOpener(lambda request: {"ok": True})
    api = make_api(opener)
    assert api.get_json("/repos/o/r/pulls/7") == {"ok": True}
    api.post_json("/repos/o/r/statuses/abc", {"state": "success"})
    (get, get_timeout), (post, post_timeout) = opener.requests
    assert get.get_method() == "GET"
    assert post.get_method() == "POST"
    assert get.get_header("Authorization") == "Bearer tok-123"
    assert post.get_header("Authorization") == "Bearer tok-123"
    assert post.get_header("Content-type") == "application/json"
    assert json.loads(post.data) == {"state": "success"}
    assert get_timeout and post_timeout


def test_an_http_error_is_not_swallowed() -> None:
    def explode(request, timeout=None):
        raise HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b"{}"))

    api = review_gate.GitHubApi("tok", base="https://api.example.test", opener=explode)
    with pytest.raises(HTTPError):
        api.get_json("/repos/o/r/pulls/7")


def good_env(**overrides: str) -> dict[str, str]:
    env = {
        "GITHUB_TOKEN": "tok",
        "GITHUB_REPOSITORY": "owner/repo",
        "PR_NUMBER": "7",
        "REVIEW_GATE_ALLOWED_REVIEWERS": "assaf0buskila, Other-Login",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_RUN_ID": "99",
    }
    env.update(overrides)
    return env


def test_main_publishes_and_exits_zero_even_when_the_gate_says_failure() -> None:
    api = FakeApi(pull(), [])
    assert review_gate.main(good_env(), api=api) == 0
    [(path, payload)] = api.posts
    assert path.endswith(f"/statuses/{HEAD}")
    assert payload["state"] == "failure"
    assert payload["target_url"] == "https://github.com/owner/repo/actions/runs/99"


def test_main_reads_a_comma_separated_allow_list() -> None:
    api = FakeApi(pull(), [comment(login="other-login")])
    assert review_gate.main(good_env(), api=api) == 0
    assert api.posts[0][1]["state"] == "success"


@pytest.mark.parametrize(
    "overrides",
    [
        {"GITHUB_TOKEN": ""},
        {"GITHUB_REPOSITORY": "not-a-repo"},
        {"GITHUB_REPOSITORY": "a/b/c"},
        {"PR_NUMBER": ""},
        {"PR_NUMBER": "7; rm -rf /"},
        {"PR_NUMBER": "0"},
        {"REVIEW_GATE_ALLOWED_REVIEWERS": ""},
    ],
)
def test_main_refuses_a_bad_environment_and_publishes_nothing(overrides: dict[str, str]) -> None:
    api = FakeApi(pull(), [comment()])
    assert review_gate.main(good_env(**overrides), api=api) == 2
    assert api.posts == []
    assert api.paths == []


# ---------------------------------------------------------------- the workflow file


def workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def triggers() -> dict:
    doc = workflow()
    # PyYAML (YAML 1.1) reads the bare key `on` as the boolean True.
    return doc["on"] if "on" in doc else doc[True]


def job() -> dict:
    jobs = workflow()["jobs"]
    assert list(jobs) == ["evaluate"]
    return jobs["evaluate"]


def test_workflow_runs_on_pull_request_updates_and_pr_comments_only() -> None:
    on = triggers()
    assert set(on) == {"pull_request_target", "issue_comment"}
    assert set(on["pull_request_target"]["types"]) == {
        "opened", "reopened", "synchronize", "edited"
    }
    assert on["pull_request_target"]["branches"] == ["master"]
    assert set(on["issue_comment"]["types"]) == {"created", "edited", "deleted"}


def test_workflow_never_uses_untrusted_or_unrelated_triggers() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    for forbidden in ("pull_request:", "workflow_run", "workflow_dispatch", "push:"):
        assert forbidden not in text


def test_permissions_are_empty_at_the_top_and_least_on_the_job() -> None:
    assert workflow()["permissions"] == {}
    assert job()["permissions"] == {
        "contents": "read",
        "pull-requests": "read",
        "statuses": "write",
    }


def test_only_the_job_that_writes_the_status_has_a_write_permission() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert re.findall(r"^\s*([\w-]+): write\s*$", text, re.M) == ["statuses"]
    assert "write-all" not in text


def test_job_is_limited_to_pull_requests_and_has_a_timeout() -> None:
    evaluate = job()
    assert evaluate["if"] == (
        "github.event_name == 'pull_request_target' || github.event.issue.pull_request"
    )
    assert evaluate["runs-on"] == "ubuntu-latest"
    assert evaluate["timeout-minutes"] <= 10


def test_job_and_step_keys_cannot_hide_a_failure() -> None:
    evaluate = job()
    assert set(evaluate) <= {"if", "runs-on", "timeout-minutes", "permissions", "env", "steps"}
    for step in evaluate["steps"]:
        assert set(step) <= {"name", "uses", "with", "run", "env"}
        assert ("uses" in step) != ("run" in step)


def test_every_action_is_pinned_to_a_commit_sha_and_matches_ci() -> None:
    uses = [step["uses"] for step in job()["steps"] if "uses" in step]
    assert uses
    for ref in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", ref), ref
    ci_checkout = {
        step["uses"]
        for ci_job in yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"].values()
        for step in ci_job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
    }
    assert len(ci_checkout) == 1
    assert uses[0] in ci_checkout
    checkout = job()["steps"][0]
    assert checkout["with"] == {
        "ref": "${{ github.event.repository.default_branch }}",
        "persist-credentials": False,
    }


def test_the_only_run_step_is_the_script_and_no_expression_reaches_a_shell() -> None:
    runs = [step["run"] for step in job()["steps"] if "run" in step]
    assert runs == ["python3 scripts/ci/review_gate.py"]
    assert "${{" not in "".join(runs)


def test_the_pr_number_and_token_reach_the_script_through_env() -> None:
    [step] = [step for step in job()["steps"] if "run" in step]
    assert step["env"] == {
        "GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "PR_NUMBER": "${{ github.event.pull_request.number || github.event.issue.number }}",
    }


def test_allowed_reviewer_logins_are_pinned_in_the_workflow() -> None:
    assert job()["env"] == {"REVIEW_GATE_ALLOWED_REVIEWERS": "assaf0buskila"}


def test_runs_for_one_pr_cancel_each_other_so_the_newest_evaluation_wins() -> None:
    concurrency = workflow()["concurrency"]
    assert concurrency["cancel-in-progress"] is True
    group = concurrency["group"]
    assert "github.event.pull_request.number || github.event.issue.number" in group


def test_status_context_is_review_gate_and_no_check_run_shares_that_name() -> None:
    assert review_gate.CONTEXT == "review-gate"
    assert "review-gate" not in workflow()["jobs"]
    for name in (job().get("name"), *(step.get("name") for step in job()["steps"])):
        assert name != "review-gate"


# ---------------------------------------------------------------- templates and docs


def filled_template(*, author: str = "claude", reviewer: str = "codex") -> str:
    text = PR_TEMPLATE.read_text(encoding="utf-8")
    for field, value in (
        ("Chunk", "V2-01"),
        ("Author-Agent", author),
        ("Reviewer-Agent", reviewer),
    ):
        # The reviewer-comment example further down repeats Reviewer-Agent, so replace the first.
        text, count = re.subn(rf"(?m)^{field}:.*$", f"{field}: {value}", text, count=1)
        assert count == 1, f"the template must carry a {field}: line"
    text, count = re.subn(r"(?m)^- <capability>: <STATE>$", "- cowork queue: LOCAL_TESTED", text)
    assert count == 1, "the template must carry one placeholder evidence line"
    return text


def test_an_unedited_pr_template_does_not_pass_the_gate() -> None:
    template = PR_TEMPLATE.read_text(encoding="utf-8")
    assert_fails(decide([comment()], body=template), "Chunk", "Author-Agent", "Evidence")


def test_a_filled_in_pr_template_passes_the_body_rules() -> None:
    assert decide([comment()], body=filled_template()).passed
    codex = filled_template(author="codex", reviewer="claude")
    reviewed = comment(verdict_text(agent="claude"))
    assert decide([reviewed], body=codex, head_ref="codex/os-v2-01-x").passed


def test_pr_template_lists_every_evidence_state_and_forbids_percentages() -> None:
    template = PR_TEMPLATE.read_text(encoding="utf-8")
    for state in STATES:
        assert state in template
    assert "percentage" in template.lower()
    assert "Verdict:" in template and "Head-SHA:" in template and "Reviewer-Run:" in template


def test_the_template_example_verdict_is_not_itself_a_verdict() -> None:
    # The template shows reviewers the comment format. It must sit in a fence or an HTML comment,
    # so pasting the whole template into a comment cannot grant a PASS.
    template = PR_TEMPLATE.read_text(encoding="utf-8")
    assert_fails(decide([comment(template)]), "no reviewer verdict")


def issue_form() -> dict:
    return yaml.safe_load(ISSUE_TEMPLATE.read_text(encoding="utf-8"))


def test_issue_template_asks_for_every_chunk_field() -> None:
    form = issue_form()
    assert form["name"] and form["description"] and form["title"].startswith("[V2-")
    assert "os-v2" in form["labels"]
    fields = {item["id"]: item for item in form["body"] if "id" in item}
    expected = {
        "chunk_id",
        "tier",
        "author_agent",
        "reviewer_agent",
        "goal",
        "owned_files",
        "must_not_touch",
        "invariants",
        "stop_conditions",
        "return_format",
        "evidence_states",
    }
    assert set(fields) == expected
    for name in expected:
        assert fields[name]["validations"]["required"] is True, name
        assert fields[name]["attributes"]["label"], name
    assert fields["author_agent"]["attributes"]["options"] == ["claude", "codex"]
    assert fields["reviewer_agent"]["attributes"]["options"] == ["claude", "codex"]
    states = fields["evidence_states"]["attributes"]
    assert all(state in states["description"] for state in STATES)
    assert "percentage" in states["description"].lower()


def test_cowork_doc_states_the_rules_and_the_owner_actions() -> None:
    text = COWORK.read_text(encoding="utf-8")
    for needle in (
        "HANDOFF.md",
        "TASKS.md",
        "review-gate",
        "gpt-6-astra",
        "Reviewer-Run",
        "Head-SHA",
        "claude|codex/os-v2-",
        "branch protection",
        "Assaf",
        "process",
    ):
        assert needle in text, needle
    lowered = text.lower()
    assert "never reviews" in lowered
    assert "deploy" in lowered and "go" in lowered
    for required in ("checks", "postgres", "container", "os-ledger-postgres"):
        assert required in text


def test_agents_md_has_one_pointer_line_to_the_cowork_doc() -> None:
    lines = [line for line in AGENTS_MD.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert sum("docs/COWORK.md" in line for line in lines) == 1
    assert any("The author never reviews their own work" in line for line in lines)


def test_cowork_doc_records_the_open_decisions_and_the_stale_status_caveat() -> None:
    text = COWORK.read_text(encoding="utf-8")
    lowered = text.lower()
    # A red `evaluate` job can leave an older status on the SHA; say so and say how to clear it.
    assert "stale" in lowered
    assert "re-run" in lowered
    # Turning protection on makes every PR to master follow the cross-agent rule; Assaf decides.
    assert "CLAUDE.md" in text
    assert "every pull request" in lowered or "every pr" in lowered
    # The status writer executes trusted code; the PR cannot replace its own evaluator.
    assert "only that trusted copy" in lowered
    assert "create the missing" in lowered


# ---------------------------------------------------------------- committed line endings

# Whether a file is checked out with LF or CRLF depends on the machine (core.autocrlf=true on
# Windows writes CRLF to the working tree), so the bytes on disk say nothing about what was
# committed. The index does: `git ls-files --eol` reports it as `i/<eol>`.
LF_FILES = (
    WORKFLOW,
    PR_TEMPLATE,
    ISSUE_TEMPLATE,
    COWORK,
    AGENTS_MD,
    ROOT / "scripts" / "ci" / "review_gate.py",
)


def parse_index_eol(listing: str) -> dict[str, str]:
    """Map path -> line ending recorded in the index, from `git ls-files --eol` output."""
    found: dict[str, str] = {}
    for line in listing.splitlines():
        match = re.fullmatch(r"i/(\S+)\s+w/\S+\s+attr/[^\t]*\t(.+)", line.rstrip("\r"))
        if match:
            found[match.group(2)] = match.group(1)
    return found


def git_index_eol(paths: tuple[Path, ...]) -> dict[str, str] | None:
    """Index line endings for paths, or None when ROOT is not the top of a git work tree."""
    relative = [path.relative_to(ROOT).as_posix() for path in paths]
    try:
        prefix = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--show-prefix"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        listing = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "--eol", "--", *relative],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if prefix.returncode != 0 or prefix.stdout.strip() or listing.returncode != 0:
        return None  # no git, not a work tree, or ROOT is only a folder inside another repo
    return parse_index_eol(listing.stdout)


def test_the_eol_listing_parser_reads_lf_crlf_and_mixed_index_entries() -> None:
    listing = (
        "i/lf    w/crlf  attr/                 \tdocs/COWORK.md\n"
        "i/crlf  w/crlf  attr/text eol=lf      \t.github/workflows/review-gate.yml\r\n"
        "i/mixed w/lf    attr/                 \tAGENTS.md\n"
        "i/none  w/none  attr/                 \ttests/ci/__init__.py\n"
        "not a listing line\n"
    )
    assert parse_index_eol(listing) == {
        "docs/COWORK.md": "lf",
        ".github/workflows/review-gate.yml": "crlf",
        "AGENTS.md": "mixed",
        "tests/ci/__init__.py": "none",
    }


def test_committed_files_use_lf_line_endings() -> None:
    eol = git_index_eol(LF_FILES)
    if eol is None:
        pytest.skip("not a git work tree, so the committed line endings cannot be read")
    for path in LF_FILES:
        relative = path.relative_to(ROOT).as_posix()
        assert relative in eol, f"{relative} is not tracked by git"
        assert eol[relative] == "lf", f"{relative} is committed with {eol[relative]} line endings"
