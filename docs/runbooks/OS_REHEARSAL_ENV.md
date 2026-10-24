# V2-35a: owner-run TEST environment

Status: NOT_INCLUDED by contract §22 E36 (2026-10-24, Assaf); kept for reference

אסף, החזרה מתבצעת בעותק זמני של הגיליון ובמסד נתונים מקומי. צור לעותק
פריסת Apps Script חדשה, הגדר TEST ומפתח חתימה חדש שנשאר אצלך בלבד.
בדוק שהמזהים שונים מאלה של סביבת הייצור. בסיום סגור את הפריסה, מחק את
העותק ותעד זאת. המסמך מכין את החזרה; הוא אינו אישור לשינוי בייצור.

Status: preparation only. No copy, deployment, PG instance or rehearsal has been
created by this change. Binding sources: frozen `V2_PG_CONTRACTS.md` v1 §§0, 16,
18, 19; the V2-35a issue; the source references below. The checklist is written
before execution. Record evidence locally under `ops/assaf-os/rehearsal/local/`,
which its new `.gitignore` excludes. Never attach secrets or workbook exports to
an issue, chat or commit. The copy contains private business data.

## Steps for Assaf

1. Choose a UTC run ID `YYYYMMDDTHHMMSSZ` and record it. Open the current production
   book and its bound Script **read-only**. Record the production workbook ID,
   every active deployment ID and `/exec` URL, and the visible web-app access and
   execution settings in the local pins/evidence files described below. Do not
   read `.env` or copy the production HMAC. Confirm the pins with the coordinator;
   missing or stale pins make isolation unproven.
2. In Google Sheets choose **File > Make a copy**. Name it exactly
   `ASSAF OS REHEARSAL TEST - <run-id>` (replace `<run-id>`). Put it in a private
   folder; do not select sharing with the same people. Record its new workbook
   ID from `/spreadsheets/d/<id>/edit`. Check all tabs, including hidden
   `_assaf_os_*` journals, were copied. Do not run the installer on this populated
   copy. Keep inherited journal evidence for the drain/reconcile rehearsal;
   never replay copied pending decisions.
3. **From the copy**, choose **Extensions > Apps Script**. Rename its bound project
   `ASSAF OS REHEARSAL TEST SCRIPT - <run-id>`. Check the project ID differs from
   production and the editor was opened from the copy. Inspect existing triggers
   and deployments. Before running any function, replace all binding properties
   in step 5. Install the reviewed release's six files: the five source files below,
   plus `appsscript.json`. Verify the bundle digest, which covers the five source
   files only (rule below). Ensure Advanced Sheets **v4** is enabled. Installable
   triggers must be verified separately; do not assume they are copied or reinstall
   the workbook.

   **Bundle digest rule.**
   - Take `schema.js`, `core.js`, `runtime.js`, `UnifiedSheets.gs`, `UnifiedRuntime.gs`,
     in that order, from `ops/assaf-os/`. `appsscript.json` is excluded.
   - For each file: convert CRLF to LF, strip trailing whitespace at the end of the file
     (not per line), and end with exactly one newline.
   - Join the five files with a newline, and take the SHA-256 of the UTF-8 bytes.

   This reproduces the recorded Script version 10 digest
   `660e6c72de1de2f0268df87dae01b4385cbb4b15c256cabac851c134f5801f89` from
   backend `bda48fa`. From the repository root, at the release's commit:

   ```powershell
   python -c "import hashlib,pathlib; f=['schema.js','core.js','runtime.js','UnifiedSheets.gs','UnifiedRuntime.gs']; t=[pathlib.Path('ops/assaf-os',n).read_bytes().decode('utf-8').replace('\r\n','\n').rstrip()+'\n' for n in f]; print(hashlib.sha256('\n'.join(t).encode('utf-8')).hexdigest())"
   ```
4. Generate the rehearsal HMAC locally, once. From the repository root run:

   ```powershell
   uv run --offline python -c "from pathlib import Path; import secrets; p=Path('ops/assaf-os/rehearsal/local/hmac-secret.txt'); p.parent.mkdir(parents=True, exist_ok=True); f=p.open('x', encoding='utf-8', newline=''); f.write(secrets.token_hex(32)); f.close()"
   ```

   This writes 64 random hex characters without printing them and refuses to
   overwrite an existing file. Open the file only in a local editor. Transfer it
   directly into the copy's Script Properties and local `rehearsal.env`; never
   paste it into chat, terminal commands, screenshots, Sheets cells or logs.
   Restrict the local directory to your OS account (Unix: `chmod 700 local` and
   `chmod 600` on secret files; Windows: check the directory's Security tab).
5. In **Project Settings > Script Properties**, set these exact names:

   | Script Property | Rehearsal value | Source |
   |---|---|---|
   | `ASSAF_OS_OWNER_ID` | Assaf's numeric Telegram owner ID, never username | `UnifiedRuntime.gs:43`, validation `:49` |
   | `ASSAF_OS_WORKSPACE_ID` | `rehearsal-<run-id>`, also used by local MIA | `UnifiedRuntime.gs:44`, validation `:50` |
   | `ASSAF_OS_WORKBOOK_ID` | **Copy** workbook ID | `UnifiedRuntime.gs:45`, validation `:51` |
   | `ASSAF_OS_MODE` | `TEST` | `UnifiedRuntime.gs:46`, validation `:52` |
   | `ASSAF_OS_HMAC_SECRET` | Newly generated local secret | `UnifiedRuntime.gs:47`, minimum length `:53` |

   Paths above are under `ops/assaf-os/`. `UnifiedSheets.gs:24-31` confirms the
   binding names; it does not set a secret. `ASSAF_OS_INSTALL_CONFIRMATION`
   (`UnifiedSheets.gs:22`) is an installer gate, **not** needed for this populated
   copy. The `ASSAF_OS_SETUP_*` names (`:34-36`) are optional **User Properties**
   for a setup helper, not the runtime Script Properties. Leave those helpers
   unused. `ASSAF_OS_REFERENCE_VALIDATION_*` are runtime cache properties, not
   connection settings. Run only `preflightConfiguredUnifiedWorkbook` and review
   its result before authorizing any copy-only repair. Restart local MIA after
   any Script publish or property change to invalidate its binding caches.
6. Select **Deploy > New deployment > Web app** in the **copy's** project. Name
   the deployment `ASSAF OS REHEARSAL TEST - <run-id>`. The checked-in manifest
   `ops/assaf-os/appsscript.json:10` specifies **Execute as: Me**
   (`USER_DEPLOYING`) and **Who has access: Anyone** (`ANYONE_ANONYMOUS`).
   Compare these with production's actual deployment settings recorded in step 1.
   If they differ, stop for the coordinator to resolve; the manifest does not
   prove current console settings. Deploy a **new** web app, never edit production's
   deployment. Record the deployment ID and `/exec` URL locally, and verify the
   deployment ID is the URL's `/s/<deployment-id>/exec` component.
7. Create `ops/assaf-os/rehearsal/local/production-pins.json` with the JSON keys
   `script_urls`, `deployment_ids`, `workbook_ids`, each a **nonempty array of
   exact production strings** from step 1. Include every still-usable production
   deployment, not only the latest. There are no production values in the repo.
   Copy `rehearsal.env.example` to `local/rehearsal.env` and fill it in a local
   editor as unquoted `NAME=value`, one per line (no shell expansion):

   | Local name | Value source |
   |---|---|
   | `MIA_DATABASE_URL` | `postgresql+psycopg://postgres@127.0.0.1:56217/rehearsal_test` for disposable bootstrap only |
   | `MIA_ASSAF_OS_SCRIPT_URL` | New TEST deployment `/exec` URL |
   | `REHEARSAL_DEPLOYMENT_ID` | New TEST deployment ID (launcher metadata, not a MIA setting) |
   | `MIA_ASSAF_OS_WORKBOOK_ID` | Copy workbook ID |
   | `MIA_ASSAF_OS_WORKSPACE_ID` | Same rehearsal workspace as step 5 |
   | `MIA_ASSAF_OS_MODE` | `TEST` |
   | `MIA_ASSAF_OS_HMAC_SECRET` | Same new secret as step 5 |
   | `MIA_TELEGRAM_OWNER_USER_IDS` | Same single numeric owner as step 5 |

   Leave the optional `MIA_OWNER_OS_*_DATABASE_URL` fields blank until provisioning
   rehearsal-only roles. All supplied DSNs must target the same loopback host,
   explicit port and database. No DSN query parameters or hostname aliases are
   accepted. `127.0.0.1` and `::1` are examples of accepted loopback IP literals.
   No SSH tunnel or port forward to production may use that local port.
8. Follow [local stack usage](../../ops/assaf-os/rehearsal/README.md) from the
   repository root. Start with `check`, then `prepare --sha <full-release-sha>`.
   If the coordinator has started the disposable container, omit `--start-pg`.
   Otherwise the optional flag creates only the documented disposable PG16.
   Review and run the printed MIA command. It points at the TEST URL and loopback
   PG from the guarded file. Verify `/health` on `http://127.0.0.1:58000` reports
   the intended SHA; obtain a signed schema response through the release's
   authenticated owner port and check TEST mode, copy workbook and workspace.
   A local configuration check alone cannot prove the remote Script binding.
   Owner login/TEST Telegram ingress and scoped mirror credentials are integration
   prerequisites in [the full rehearsal plan](OS_REHEARSAL_PLAN.md), not enabled
   by this bootstrap. Do not point production's bot/webhook at this instance.
9. On the copy only, verify/recreate one installable **From spreadsheet > On edit**
   trigger for `reconcileAssafOsEdit` if required by V2-31's integrated runbook.
   Record trigger ID and authorization without copying tokens. Continue to the
   full rehearsal only when its dependencies, read-only binding probe and checklist
   pass. This preparation does not run T1-T7 or prove cutover readiness.
10. **Teardown, after saving sanitized evidence:** stop local MIA. In the copy's
    **Deploy > Manage deployments**, select the recorded TEST deployment and
    **Archive** it; verify it is archived. Remove copy-only triggers, then move
    the exact copy workbook and its bound project to Trash. Verify its ID is in
    Trash; permanently delete that exact copy if the agreed retention policy
    requires it, and record whether it is trashed or permanently deleted.
    Remove the rehearsal mirror workbook if created. Stop only the recorded
    disposable container; its tmpfs data disappears. Remove the detached worktree
    using `git worktree remove <exact-path>` only after checking it is the recorded
    clean rehearsal tree, without `--force`. Remove local HMAC/config, private
    exports and dumps according to the agreed retention. Record who deleted each
    resource, UTC time, exact local evidence reference and any remaining retained
    copy. Never mark teardown complete while a deployment/copy remains unaccounted.

## Evidence checklist (fill before starting; update with observations)

States: `NOT_RUN`, `CODE_CHECKED`, `LOCAL_TESTED`, `REHEARSAL_VERIFIED`,
`PRODUCTION_VERIFIED`, `OWNER_ACCEPTED`, `BLOCKED`, `NOT_INCLUDED`.
These identify evidence scope, not a completion percentage. A failed observation
is `BLOCKED` with a reason code. Keep identifiers in the private evidence file;
public reports contain reason codes and booleans only.

| Check | Pass criterion | State | Evidence reference / UTC / owner |
|---|---|---|---|
| Production pins | Complete, current and confirmed; every set nonempty | NOT_RUN | |
| Private workbook copy | Exact name; distinct workbook/project IDs; hidden tabs present | NOT_RUN | |
| Fresh secret and TEST binding | All five properties match local config; new HMAC | NOT_RUN | |
| Deployment access | Console settings equal verified production settings and source | NOT_RUN | |
| New deployment | New ID/URL; production comparisons all unequal | NOT_RUN | |
| Local files ignored | `git check-ignore` succeeds for both env and pins | NOT_RUN | |
| Guard / PG / SHA | Guard passes; PG16; detached clean exact release | NOT_RUN | |
| Remote schema | Signed response matches copy/workspace/TEST | NOT_RUN | |
| Trigger | Copy-bound only; ID and authorization recorded | NOT_RUN | |
| Teardown | Deployment archived, copies deleted, local secret/data disposition recorded | NOT_RUN | |
