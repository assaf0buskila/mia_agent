# Mia VNext rebuild — handoff (2026-08-26)

Session `assaf-agent-69`. Goal was: execute `MIA_REBUILD.MD`. Stopping on usage limits.
This file is the state of play. Read it before touching anything.

---

## 1. The single most important fact

**Master was never production.**

Production ran image `mia:20` (task `mia:22`), whose code lived only on branch
`claude/mia-product-feedback-0bfc90`. Master sat at roughly `mia:16`. A prior session
built the entire VNext rebuild on master, so it was built on a base that had never been
deployed and was missing every feature production had gained since.

Evidence (documents, corroborated across two independently written worktrees):
`docs/BUILD_STATUS.md` and `docs/HANDOFF.md` on the branch, ADR-030/031 Consequences,
commit `7433abf` ("Not deployed — live stays mia:20 / task mia:22").

**Never verified against live AWS.** One command settles it and is worth running:
compare the running task's `imageDigest` against `ee4fab12…` (mia:20) vs `b7a967c9…` (mia:18).

---

## 2. Branches — what exists now

| Branch | Commit | What it is |
|---|---|---|
| `master` | `c565d36` | Pre-existing. ~mia:16. Not production. |
| `claude/mia-product-feedback-0bfc90` | `7433abf` | **Production** code (mia:20) + built-not-deployed ADR-032. |
| `claude/mia-vnext-rebuild` | `c35d005` | Checkpoint of the prior session's rebuild, captured verbatim. |
| `claude/mia-merge-prod` | `d2896d6` | **The good one.** Rebuild + production, merged, 2490 tests green. |

`d2896d6` is the base for all further work.

### Worktree used
The merge was done in an isolated worktree at
`…/scratchpad/merge-wt` because the live repo was being written by another session.
The temp directory may be garbage-collected; **the commits are safe in the repo's object
store regardless.** Clean up the stale registration with `git worktree prune` if needed.

---

## 3. What was actually done

1. **Checkpoint (`c35d005`).** 80 files of uncommitted rebuild work committed verbatim so
   it became revertable. Nothing authored by this session.
2. **Merge (`d2896d6`).** All 12 conflicts resolved. Principle used throughout:
   **production wins on functionality, the rebuild wins on structure.**
   - Production's 14 owner-path hunks were *not* returned to `inbound.py`; they were
     replayed into `app/api/owner.py`, respecting the rebuild's owner/client file split.
   - Production's visitor knowledge (ADR-028) and `meeting_first` were preserved but
     relocated onto the rebuild's `compile_client_graph`.
   - Plumbing added so merged behavior actually flows: `knowledge_lookup` through
     `compile_client_graph`; `meeting_first` through `message_to_client_state` →
     `empty_client_state` → `ClientState` → `empty_state`.
   - Verified: ruff clean, **2490 passed**, no conflict markers, no test weakened.
3. **ADR id collision resolved.** Both sides had used 028–032 for different decisions.
   Production keeps its ids (they are cited in *shipped* code). The rebuild's renumbered:
   028→034, 030→035, 031→036, 032→037. **033 is reserved** for the uncommitted ADR-033 in
   the `0bfc90` worktree. Code comments citing the rebuild ids were updated; production's
   were deliberately left alone.
4. **Two real bugs fixed as a side effect**: `website_meeting_first` restored to
   `config.py` (`.env.example` documented `MIA_WEBSITE_MEETING_FIRST` with no field able to
   read it — `test_deploy_secret_box.py` asserts on that sync); and a genuine contradiction
   in ADR-027's "Alternatives" where the two sides said opposite things about the LinkedIn token.

---

## 4. What the rebuild actually is (audited, do not trust GATES.md)

`GATES.md` shows 16 green gates. **They are green because the tests were written to the
code, not to the behavior.** Audit findings, with evidence:

- **OwnerGraph is inert.** `compile_owner_graph(...).invoke(...)`'s return value is
  discarded at `app/domain/owner_brain.py:263`; the answer comes from a closure over the
  old hand-rolled ReAct loop in `app/graph/owner_agent.py`. If the graph never fires the
  node, the code calls the old path again anyway.
- **ClientGraph** was a wrapper around the old 1-node orchestrator. It has since grown real
  nodes (`load_conversation` → `retrieve_knowledge` → conditional → `sales_turn`/`complete_turn`)
  and is the **only** place `GraphName.CLIENT` is genuinely used.
- **Capability isolation is not yet a boundary.** `graph=` is caller-declared, not derived
  from request context, and every live owner call site hardcodes `GraphName.OWNER`. A real
  boundary means deriving it from the request.
- **Only 2 of 14 new `test_vnext_*` files** would fail if a live route were rewired to the
  old path — and both do it by grepping source text, not by executing a route.
- **Phase L cannot be executed as written.** Only ~582 LOC (1.5%) is removable without
  rewiring, and none of it is old architecture. `app/graph/{orchestrator,owner_agent,replies}.py`
  are all still live. The rebuild *grafted onto* the old tree and the graft runs inward:
  deleting the old code deletes the new code's entry points. "Meaningfully smaller" needs
  ~2,535 LOC of rewiring first.

### Known defects still open (found, not yet fixed)
- `app/services/finalization.py:83` — idempotency keyed on `(kind, lead_id)`, **not**
  `conversation_id`, contradicting PLAN.md's own contract. A returning visitor's second
  conversation gets **no owner ping, ever**.
- `store.try_insert_owner_notification` is SELECT-then-INSERT, not `ON CONFLICT` — two
  concurrent `/end` requests raise `IntegrityError` → 500.
- `app/domain/hot_handoff.py` sent unconditionally after an upsert that cannot report
  failure. A third session appeared to be fixing this concurrently — **re-verify before acting.**
- `mail.create_draft` declared `WRITE` with `confirmation_required=False`.
- Dead on arrival: `app/services/voice.py` (9 lines, zero app callers),
  `app/channels/telegram.py` (zero production callers).
- Duplicate pairs: three Telegram senders, three tool/capability registries, two policy
  engines, two conversation-state schemas.

---

## 5. Assaf's decisions already made (do not relitigate)

1. Branch `0bfc90` **is** production → merge it first, rebuild on top. ✅ done
2. **Finish the rebuild for real** — make the graphs actually own their turns, derive
   `graph=` from request context so isolation is a real boundary, rewrite the tautological
   tests to hit routes, then Phase L. ⬅ **this is the remaining work**
3. Checkpoint before touching anything. ✅ done

---

## 6. Next steps, in order

1. **Settle production identity for real** — the one `imageDigest` command in §1.
2. **Land the merge into the live tree.** Blocked while another session writes there
   (see §7). Then: `git checkout claude/mia-merge-prod` or merge it into the working branch.
3. **Reconcile the fourth state**: the `0bfc90` worktree has ~23 uncommitted paths incl. an
   ADR-033 that reimplements the Apify work a *second* time with a different env var
   (`MIA_APIFY_API_TOKEN` vs `MIA_APIFY_TOKEN`) and a different REST path. Pick one.
4. **Then the real rebuild** (decision 2 above), gate by gate, ideally in a worktree.
5. Cosmetic: in `docs/DECISIONS.md` the renumbered 034–037 bodies sit *before* 028–032.
   The index table is correctly ordered. An 80-line block move; left out of the merge commit
   deliberately.

---

## 7. Coordination hazard — read this

At least **three** sessions have been writing to
`c:\Users\lenovo\Desktop\assaf project\assaf_agent`:
- this one (`assaf-agent-69`),
- `assaf-agent-f5` — **read-only**, doing a dead-code inventory; it confirmed it never wrote,
  and it asked to be pinged once the merge lands so it can re-run against the real base,
- **an unidentified third writer** that produced `PLAN.md`/`GATES.md` and was still editing
  `app/api/inbound.py`, `app/agents/client/graph.py`, `app/workers/due_scan.py` at 23:38.

The live tree's dirty set went 10 → 19 → 80 paths during this session. Do not run a merge or
a Phase L deletion in that tree until you know it is quiet. `git worktree list` shows three
worktrees; the branch worktree is separately dirty.

---

## 8. Guardrails that held and should keep holding

- Do not inspect `.env`. Names only in `.env.example`.
- Do not auto-deploy. Nothing here has been deployed; live remains `mia:20`.
- Never weaken a test to make a suite green.
- `.gitignore` already covers `.env`, `*.db`, `deploy/local/`, `__pycache__` — verified before
  the checkpoint commit.
