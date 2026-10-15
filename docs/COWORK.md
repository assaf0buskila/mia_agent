# Claude and GPT co-work through GitHub

GitHub issues and PRs are the shared work queue and the review record. They are not the state of
record: `HANDOFF.md` and `TASKS.md` stay that, and `AGENTS.md` stays the contract. Rules for
Assaf OS v2 chunks (roadmap: assaf0buskila/mia_agent#153).

## Flow

1. **Issue.** One per chunk, from the `Assaf OS v2 chunk` template: id, goal, owned files, must
   not touch, invariants, stop conditions, return format, target evidence states.
2. **Branch.** `claude|codex/os-v2-<id>-<slug>` from the latest `origin/master`, in its own
   worktree. The prefix names the author agent.
3. **Tests first, then the smallest change,** gates run with output redirected under `.cache/`.
4. **PR.** Fill in the PR template: `Chunk`, `Author-Agent`, `Reviewer-Agent` and one evidence
   line per capability. Evidence states are `CODE_CHECKED`, `LOCAL_TESTED`,
   `INTEGRATION_TESTED`, `STAGING_VERIFIED`, `DEPLOYED`, `PHONE_VERIFIED`, `BLOCKED` and
   `NOT_INCLUDED`. Never a percentage.
5. **Review by the other agent,** on the three-dot diff `origin/master...<head sha>`.
6. **Merge** when the verdict is PASS and CI is green on that exact head.
7. **Deploy** only with Assaf's explicit go, from a clean checkout at the CI-green SHA
   (`README.md`). Nothing in this flow deploys.

## Roles

The author never reviews their own work. Whoever authored, the other agent reviews.

| Tier | Claude Code | GPT through Codex |
|---|---|---|
| HEAVY | `opus` | `gpt-6-astra` |
| MID | `sonnet` | `gpt-5.6-sol` |
| CHEAP | `haiku` | `gpt-5.6-luna` |

Review is HEAVY. At most 2 agents run at once, builders and reviewers together (`CLAUDE.md`).

### Running GPT through the Codex plugin

- Once: `/codex:setup`.
- Review a Claude-authored PR: `/codex:rescue --model gpt-6-astra --wait <prompt>`. Say in the
  prompt that the run is read-only (the plugin defaults to write-capable), give it the
  three-dot diff and the requirement, and ask it to finish with the four verdict lines below.
- The job id from `/codex:status` or `/codex:result <job-id>` is the `Reviewer-Run`.
- Codex-authored PR: a fresh read-only `opus` subagent reviews, and its subagent id is the
  `Reviewer-Run`.
- Neither agent has its own GitHub identity. The orchestrating session posts the verdict
  comment with `gh pr comment`, with the reviewer's findings above the four lines.

## The verdict comment

Plain lines, not in a code fence, quote or HTML comment, for the exact head SHA:

```text
Reviewer-Agent: codex
Reviewer-Run: <Codex job id or subagent id>
Head-SHA: <full 40-character head SHA>
Verdict: PASS
```

`Verdict` is `PASS`, `PASS_WITH_NITS`, `FAIL` or `NEEDS_CHANGES`. Record the same run id in
`HANDOFF.md` so a reader can audit it. A new push changes the head SHA, so it needs a new verdict.

## The review-gate status

`.github/workflows/review-gate.yml` runs `scripts/ci/review_gate.py` on PR open, push, body edit
and on PR comments. It reads the PR from the API at that moment and writes the commit status
`review-gate` on the current head SHA. It is `success` only when all of these hold:

- the PR body has a valid `Chunk: V2-NN`, an `Author-Agent` and the other agent as
  `Reviewer-Agent`, and an `## Evidence` section of `- <capability>: <STATE>` bullets with no
  percentage. A `claude/` or `codex/` branch must match the `Author-Agent`;
- a comment from an allowed reviewer login (`REVIEW_GATE_ALLOWED_REVIEWERS` in the workflow,
  currently `assaf0buskila`) gives a verdict for that exact 40-character SHA and a `Reviewer-Run`;
- that comment's `Reviewer-Agent` is not the `Author-Agent`, and the latest such verdict is
  `PASS` or `PASS_WITH_NITS`.

Anything else is `failure`, and a gate that cannot evaluate writes `error`. The status and the
workflow are called `review-gate`; the job is `evaluate`, so the two never share a name.

A red `evaluate` job means the status on that SHA may be stale: the run could not read or write
through the API, and the best-effort `error` status it then tries may not have landed either.
Re-run the job before trusting the status.

The PR that adds the gate cannot be retriggered by a comment, because comment events run the
workflow from the default branch. After posting the verdict there, edit the PR body or re-run the
workflow run. If a run fails with a 403 when it reads comments, add `issues: read` to the job's
permissions in the workflow.

## What this is not

This is a process control, not security. Both agents push and comment as the same GitHub user, and
the agent tags are self-attested by the session that posts them. Until branch protection is on,
`gh pr merge` merges regardless of the status. Assaf's deploy go is the real gate.

Two limits to know. On `pull_request` events the workflow runs the checked-out PR's script, and on
comment events the default branch's, so a PR that edits the gate is graded by its own copy until a
follow-up makes `pull_request` runs check out the base SHA (impossible before the script exists on
`master`). And a verdict comment is read as Markdown: one that is inside a code fence, a quote or
an HTML comment does not count, while a `<!--` inside a fence or a backtick code span is literal.

## Owner actions (Assaf, not done by agents)

GitHub settings are writes that Assaf makes or explicitly approves. After the PR that adds this
gate has merged:

1. **Decide the scope of `review-gate` before turning protection on.** It needs a `Chunk: V2-NN`
   line and a verdict from the other agent, so once it is required, every pull request to
   `master` has to meet that: Mia campaign chunk PRs (`claude/mia-<chunk>-<slug>`), hotfixes and
   docs PRs included, which have no V2 chunk id. `CLAUDE.md` ("Review and merge") still lets a
   fresh `opus` subagent review a Claude-authored PR and calls Codex review optional, which the
   gate would reject. Either update that line so the cross-agent rule applies everywhere, or keep
   `review-gate` out of the required checks (advisory), or agree a documented non-V2 path. Assaf
   decides.
2. **Branch protection (or a ruleset) on `master`** requiring the status checks `checks`,
   `postgres`, `container`, `os-ledger-postgres` and, per the decision above, `review-gate`.
3. **Labels:** create the missing ones of `os-v2` (the issue template applies it),
   `tier:heavy|mid|cheap`, `lane:backend|frontend` and `process`. Some already exist.
4. **Project board** for the `os-v2` issues, if wanted.
