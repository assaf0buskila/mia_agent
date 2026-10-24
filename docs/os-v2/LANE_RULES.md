# Lane rules: Assaf OS v2 parallel build sessions (2026-10-18)

Every build session started by the coordinator follows these rules. The coordinator session is
`local_2ed22c80-8046-4682-a4b0-13cae62a5348`.

## Sources of truth
- Epic: assaf0buskila/mia_agent#153.
- Contract: `C:\Users\lenovo\Documents\Codex\2026-10-14\task-2\V2_PG_CONTRACTS.md`, FROZEN v1 plus the
  §22 errata E1..E23. Read only the sections your brief names. §19 is the merge order, §20 the gates.
- When code and a doc disagree, the code wins.

## Budget (Assaf's usage limits are the real constraint)
- At most ONE subagent at a time. Preferred: you build it yourself, then spawn exactly one fresh `opus`
  reviewer (read-only) per review round.
- If you see a usage warning or hit a rate limit: stop, commit WIP to your local branch (do not push),
  write resume notes in your PR or in a `.cache/RESUME.md`, and tell the coordinator.

## Hard rules
- Never read `.env*`. No production mutation, deploy, `gcloud`, `ssh`, Secret Manager or live
  external write.
- Do NOT merge. The coordinator merges in §19 order once review is PASS and CI is green.
- Never force-push or rebase; use normal merges. Use `git -c core.autocrlf=false` for every git
  command. Never write `git config` (a linked worktree's config is shared).
- LF only. Verify with `git cat-file blob HEAD:<path> | tr -cd '\r' | wc -c` (must be 0) or
  `git ls-files --eol`. NEVER `grep $'\r'`, which is vacuous in this Git Bash.
- Write source with Write/Edit, not bash heredocs, which mangle backslashes.
- Python handling non-ASCII text must read files with `encoding='utf-8'` or set
  `PYTHONIOENCODING=utf-8`, never piped stdin (cp1255 here). Check `[ -s file ]` before any
  `gh ... --body-file`.
- Use a disposable PostgreSQL only, on your assigned port. Docker: `--pull=never`, a unique
  container name, and stop only your own container.
- Run tests in the foreground with output redirected to `.cache/*.log`. Never weaken an existing
  assertion. Tests first: red, then green.
- Two failed attempts on one problem: stop, write a minimal repro and diagnosis, and tell the
  coordinator.

## Gates (unless your brief says more)
- `MIA_ENV=test uv run --offline pytest --basetemp=.cache/<unique> -p no:warnings -rs > .cache/<unique>.log 2>&1`
- the focused PostgreSQL suites for your chunk, with 0 PG skips;
- `uv run --offline ruff check app tests ops`
- `node tests/unit/widget_behavior.test.js`

## PR and review format (the V2-01 review-gate parses these)
- Branch prefix `claude/`. Open a draft PR with base `master` unless your brief says otherwise.
- The body must contain these lines, exactly:
  - `Chunk: V2-NN`
  - `Author-Agent: claude`
  - `Reviewer-Agent: claude`
- It also needs a `## Evidence` section of `- capability: STATE` bullets. There must be no colon
  inside the capability. STATE is one of CODE_CHECKED, LOCAL_TESTED, INTEGRATION_TESTED,
  NOT_INCLUDED, BLOCKED.
- End the body with: 🤖 Generated with [Claude Code](https://claude.com/claude-code)
- Commits end with: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Review (contract §22 E15): a fresh `opus` reviewer, read-only, gets the requirement, the
  three-dot diff `origin/master...HEAD` and your test report as claims to verify.
  - Fixes are re-reviewed at the final SHA.
  - On PASS, post a verdict comment with these lines: `Reviewer-Agent: claude`,
    `Reviewer-Run: <id>`, `Head-SHA: <sha>`, `Verdict: PASS|PASS_WITH_NITS`, then a
    `## Evidence` section.
  - Then mark the PR ready.
- Report to the coordinator with SendMessage at three points: PR opened, review verdict, blocked.
  Report verdict, SHA, gates with counts, and deviations. No file dumps.

## Disposable PostgreSQL ports in use
56218 V2-10, 56230 V2-14b, 56234 V2-16, 56236 V2-19, 56238/56239 V2-18, 56240 V2-30, 56242 V2-31,
56244 V2-26, 56246 V2-16b (executor), 56221-56225 builders (56225 V2-30b).
