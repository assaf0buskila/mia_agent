# Assaf OS unified runtime contract

Implementation contract, 2026-10-12. One existing workbook is the business authority. The user explicitly confirmed approval of MIA proposals through the existing private numeric-owner Telegram interface; the OS reflects the committed shared-sheet write. Browser-owner approval is an equivalent human path. Sheet schema is `schema.js`; transport fields use schema keys. All money fields (`money_ils`) are **integer minor units** in API payloads; native sheet cells are readable major units with an explicit currency field. Display-only calculated money uses the same minor-unit convention. No public website session or model tool can approve a write.

## Binding and signed port

Dedicated settings: `assaf_os_enabled` (default false), `assaf_os_script_url`, `assaf_os_workbook_id`, `assaf_os_workspace_id`, `assaf_os_hmac_secret`, `assaf_os_mode` (`TEST` or `LIVE`). Google browser sessions use `assaf-os:read`, `assaf-os:propose`, `assaf-os:approve`; all are validated server-side for the same numeric allowlisted owner. Existing live CRM behavior is not switched implicitly.

MIA -> Apps Script POST: JSON `{payload: string, signature: lowercase HMAC-SHA256 hex}`. Payload is strict JSON `{api_version:"assaf-os.v1",operation,actor_source:"OS"|"MIA"|"TELEGRAM_APPROVAL",owner_id,workspace_id,workbook_id,request_id,timestamp,data}`. `actor_source` is assigned by the MIA server route/tool/callback context, never accepted in browser/model DTOs. `TELEGRAM_APPROVAL` is available only inside the verified private-owner callback after the existing durable owner-action approval decision, execution claim and target revalidation. Timestamp is epoch seconds. Reply is the same `{payload,signature}` wrapper; its signed payload echoes every request metadata field except `data`, and adds `ok:boolean,data:result_or_error`. Signed error data is `{error:{code}}`; no raw exception/provider payload. HTTP redirects are restricted to the official Apps Script response host and never resend POST. No automatic retry after a write.

Operations: `schema`, `snapshot`, `propose`, `proposal`, `approve`, `reject`, `receipt`. The server binds `propose` source to `OS` or `MIA`; caller/model cannot choose actor/owner/workbook/source. Apps Script accepts only allowlisted operations and the pinned owner/workspace/workbook/mode. `approve` and `reject` additionally require signed actor_source `OS` or `TELEGRAM_APPROVAL`; MIA model context can never execute them. All body/response sizes and rows are bounded; failure returns a sanitized reason code without text, provider payload or secrets.

## Owner HTTP surface

| Cloudflare same-origin | MIA |
|---|---|
| GET /api/os/schema | GET /v1/owner/os/unified/schema |
| GET /api/os | GET /v1/owner/os/unified |
| POST /api/os/proposals | POST /v1/owner/os/unified/proposals |
| GET /api/os/proposals/{id} | GET /v1/owner/os/unified/proposals/{id} |
| POST /api/os/proposals/{id}/approve | POST /v1/owner/os/unified/proposals/{id}/approve |
| POST /api/os/proposals/{id}/reject | POST /v1/owner/os/unified/proposals/{id}/reject |
| GET /api/os/receipts/{request_id} | GET /v1/owner/os/unified/receipts/{request_id} |

No arbitrary upstream URL, sheet/range or source selector. Mutation endpoints require an exact allowed Origin, CSRF, current owner session and scope. HTTP approve/reject are browser-owner actions; Telegram approval uses the verified existing callback. MIA has only read/propose model tools. Existing Google auth/opaque sessions are reused without pretending a Google proof is a Telegram proof.

## Schemas

`GET /schema` -> `{api_version:"assaf-os.v1",source:"google_sheets",mode,workbook_id,workspace_id,schema_version:"assaf-os-unified.v1",money_unit:"minor",tabs:[...schema.tabs],enums:{...schema.enums}}`.

`GET /` -> same binding metadata plus `{observed_at,coverage:"complete"|"partial"|"unknown",entity_coverage:{leads,clients,works,payments,time,expenses,tasks},records:{leads:[],clients:[],works:[],payments:[],time:[],expenses:[],tasks:[]},summary:{coverage,monthly:[]},issues:[{code,entity,record_id}],pending_proposals:[...]}`. Each entity coverage uses the same three-value enum. Monthly is always an array. A monthly row has exactly `{month,currency,paid_income_ils,paid_expenses_ils,net_cash_ils,recorded_minutes,effective_hourly_ils,coverage,reason_codes}`; money/time metrics are nullable. Currency totals are separate and never converted implicitly. Never use missing/unavailable data as zero. Rows contain only canonical schema fields; no hidden journal/secret fields. Existing legacy TEST tabs are excluded from this snapshot. Native Date values use the pinned business timezone Asia/Jerusalem.

Propose body: `{request_id,entity,record_id,expected_revision,input}`; entity is `leads|clients|works|payments|time|expenses|tasks|promote_lead`. Record ID is null for creation, otherwise the entity's stable ID. Expected revision is 0 for creation. `input` contains only schema `role=input` fields; system/calculated fields are rejected. Record IDs, timestamps, source and revisions are server-generated. An `input` amount is an integer in minor units. For promotion record_id is the lead ID, preserving person ID; it cannot create an ambiguous duplicate client. Frontend controls render Hebrew labels and submit these keys.

Propose returns `{api_version,source,mode,workbook_id,workspace_id,status:"pending",proposal_id,request_id,approval_request_id,rejection_request_id,actor_source,expires_at,entity,record_id,expected_revision,input,before,after}`. Approval/rejection request IDs are stable UUID metadata minted by the shared service for the proposal. Proposal is immutable, owner-bound, expiring (10 minutes), request-idempotent and includes a target/dependency fingerprint taken from accepted current input. No business row changes during propose. A MIA proposal also has an immutable durable owner-action envelope in the existing approval store, with expiry narrowed to the shared proposal expiry; the Telegram card displays the same exact change.

Approve/reject body: `{request_id}` matching the stored `approval_request_id` or `rejection_request_id`. The proposal ID lives in the path. Both OS and Telegram use these same stable IDs: a proposal commits at most once across channels. Approval reads stored data and revalidates current raw/accepted target immediately before commit. No model/caller replacement input is accepted here. Business rows plus accepted-state journal and immutable receipt commit together with Advanced Sheets batchUpdate. On ambiguous outcomes, only query the original receipt; do not replay approve with a new ID.

Receipt: `{api_version,source,mode,workbook_id,workspace_id,status,request_id,proposal_id,entity,record_id,revision,committed_at,actor_source,reason_codes}`. Only `status:"committed"` means a business write succeeded. Rejected/expired/target_changed are terminal explicit statuses. Unknown has nullable target/commit fields and reason `receipt_not_found`; it remains unknown and blocks another mutation until resolved. A receipt read uses a fresh transport request ID and the original decision ID in `data.request_id`. Mutation idempotency fingerprints exclude the fresh transport timestamp while signature freshness is still verified. Promotion before/after are `{lead,client}` and `before.client` may be null.

## Native edit acceptance

User edits input columns. The installable edit trigger and explicit read/reconciliation path validate complete native rows, allocate IDs for new valid rows, version changed valid rows and retain accepted snapshots. They do not run approvals or external sends. API/script writes invoke validation directly because native edit triggers do not run for API/script calls. Invalid rows, metadata/header/formula drift and unresolved same-field changes produce coverage/issues; do not silently delete or overwrite user input.

Script locks serialize script writers only; they cannot lock the workbook owner editing cells. Use target raw fingerprint/revision checks, field-limited writes, durable before/after snapshots and conflict visibility. Do not claim a cross-channel atomic lock or guaranteed no-loss simultaneous editing. A detected conflict requires owner review. Reconcile triggers and scheduled repair must not auto-execute MIA proposals.

The existing workbook has a legacy two-column `הגדרות` tab. New installer must preflight and preserve those inputs; append the new compatible description/update columns only after exact header validation. All other current legacy tabs/data stay unchanged. No imported synthetic amounts become new business revenue.
