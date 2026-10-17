<!--
The review-gate workflow (scripts/ci/review_gate.py) reads this body. Keep the three field lines
and the Evidence section in the shape below. The rules are in docs/COWORK.md.
-->

Chunk: V2-NN
Author-Agent: <claude|codex>
Reviewer-Agent: <claude|codex>

## Summary

What changed and why, in two or three sentences. Link the chunk issue: Closes #<issue>.

## Evidence

<!--
One bullet per capability, written as "- <capability>: <STATE>" where STATE is exactly one of
CODE_CHECKED, LOCAL_TESTED, INTEGRATION_TESTED, STAGING_VERIFIED, DEPLOYED, PHONE_VERIFIED,
BLOCKED, NOT_INCLUDED. Never a percentage. Nothing but bullets in this section.
-->

- <capability>: <STATE>

## Gates run

Commands and results (log paths under .cache/), the CI jobs, and anything not run.

## Review

The other agent reviews the three-dot diff `origin/master...<head sha>` and the session posts one
comment on this PR for the exact head SHA, in plain lines like the example. A new push needs a
new verdict. Record the same reviewer run id in HANDOFF.md.

```text
Reviewer-Agent: <claude|codex>
Reviewer-Run: <Codex job id or subagent id>
Head-SHA: <full 40-character head SHA>
Verdict: PASS | PASS_WITH_NITS | FAIL | NEEDS_CHANGES
```

Deploy is a separate go from Assaf. Merging this PR deploys nothing.
