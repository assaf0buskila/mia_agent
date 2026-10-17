# GET-only export prerequisite: synthetic local milestone

Base f36a2fbccb03da583304ad8a8b0e07bd65711c7d; branch codex/os-readonly-export-local.

Objective: prepare a default-off, fixture-only GET reader and explicit real-source
provenance mapping without real records, network access or importer activation.
The existing synthetic importer's guard and metadata remain unchanged.

Ownership: one MID author owns export reader/request allowlists and focused tests;
one MID author owns provenance mapping/domain parity and focused tests. Root owns
combined synthetic fixtures/tests, environment, evidence and local checkpoint.
A fresh HEAVY reviewer independently examines the complete deliverable.

Reader contract: frozen workbook/range allowlists, generated GET-only Sheets value
read requests, bounded chunks/bytes/requests, no caller-selected URL/verb/operation,
no HTTP client or credentials. Only exact fixture transport objects are accepted;
the transport records requests and rejects any non-GET/mutation/unknown range.
Read guard metadata and all data twice; differences or failed reads are inconclusive.
Matching reads are a synthetic candidate, not a transactional Google snapshot or
cutover proof. An approved freeze remains a later requirement.

Mapping contract: preserve source ID/schema/epoch/range/observation/read fingerprints,
raw/formula evidence and exact accepted records/revisions/input digests. Explicitly
distinguish source evidence from native acceptance and human approval. Never mint
missing IDs, revisions or acceptance. Validate seven-entity money/date/refund/
reference rules and coverage; unresolved/missing/changed provenance quarantines.
Produce a dry manifest only; never load records into the guarded shadow importer
or business ledger. All records in this milestone are invented fixtures.

Acceptance: default-off/no-network guards; method/route/range/workbook injection
refused before reads; chunk/response/depth limits; drift, lost reads and ABA-style
guard changes inconclusive; accepted-vs-raw mismatches and missing identities/history
visible; valid seven-family fixtures preserve financial parity. Combined tests must
prove no source write and no importer/business side effects. Independent PASS plus
focused and required repository checks, exact evidence and clean local commit.

No .env, Google requests, real records, new credentials, production access/migrations,
external sharing, push/merge/deploy, authority switch or Cloudflare authentication.
Usage limits must be reported, never evaded. Later permissions still require exact
read-only workbook/ranges, encrypted storage/retention and approved source identity.
