# Local GET export preparation

This branch starts at reviewed local milestone
`f36a2fbccb03da583304ad8a8b0e07bd65711c7d`. It prepares an export reader and
dry provenance manifest using invented fixtures exclusively. It neither contains
a Google transport nor grants permission to read business records.

## Contracts

`assaf_os_export_reader.py` defaults off. A frozen binding contains one workbook,
workspace, authority epoch, source epoch, schema version, exact bounded A1 chunks,
and a preparation guard range. The reader constructs only fixed Sheets values
GET requests. The sealed synthetic transport stores strict JSON bytes and records
those requests. It has no provider client, credentials, URL callback or write
implementation. Exact built-in JSON validation precedes copying and decoding.
Requests, rows, columns, nesting and cumulative bytes are bounded. An exhausted
byte budget stops further reads.

Each pass reads the guard, raw and formula values, then the guard again. There are
two passes. Missing responses, changed values or changed guards produce an
inconclusive extract with no usable records. A matching result remains a fixture
candidate. A mutation occurring and reverting between observations can escape
double reading; an approved source freeze is still needed for cutover evidence.
The six-column guard and source/coverage/header evidence tables are invented
preparation fixtures. They are not asserted to exist in the current workbook.

`assaf_os_export_provenance.py` validates the entire reader envelope, binding,
canonical read hashes, response accounting and all four guards. It maps normalized
fixtures or physical rows matching the frozen local Hebrew schema descriptor.
It preserves raw/formula cells, source ranges and row locations, IDs, revisions,
accepted snapshots and fingerprints, audit journals, coverage and header evidence.
Missing or conflicting evidence prevents a validated candidate. Native acceptance
is explicitly distinct from human approval. The output is a dry manifest, never
an importer batch or business write.

Native money has an explicit major-unit contract; accepted snapshot input uses
minor units. Dates must be supported unambiguous business dates. References must
resolve to exact unique fixture identities. Unsupported serial dates, ambiguous
references, formulas in authoritative fields, missing histories, conflicting
revisions, duplicates and invalid refund graphs are unresolved or quarantined.
Calculated cells remain evidence and cannot supply authoritative financial input.

`assaf_os_export_schema.json` is an exact checked-in projection of the seven entity
tabs in local `ops/assaf-os/schema.js`, including Hebrew headers, physical field
order, roles and validation descriptors. A Node parity test checks the projection.
The native fixture oracle uses the actual local Apps Script helpers; it is not a
live Google test.

## Verification evidence

Frozen implementation verified on 2026-10-14:

| Check | Result |
| --- | --- |
| Full repository Python suite | 3,328 passed, 187 skipped; 156.89 seconds |
| All prototype cases with skip reasons | 121 passed, 76 skipped; 3.50 seconds |
| New export cases, including both physical/normalized integration modes | 55 passed |
| Independent HEAVY adversarial probes | 25 passed; review PASS, no blockers |
| Local Assaf OS Node tests | 68 passed |
| Widget behavioral check | Passed |
| Ruff, all `app` and `tests` | Passed |
| Oracle Node syntax | Passed |

The full suite emitted 1,245 existing dependency deprecation warnings. The 76
prototype skips require the explicitly selected disposable PostgreSQL URL. The
other 111 skips are the previously existing opt-in suite cases. New export tests
had no skips. Pytest logs stay in ignored `.cache/`, notably
`export-final-root.log` and `export-prototypes-final.log`. The independent review
log is `C:/Users/lenovo/AppData/Local/Temp/os-export-review-final.log`.

The full suite ran offline on this same Windows computer with installed local
dependencies. An earlier restricted unit runner was stopped without producing a
result; the completed full run is the reported repository evidence.

Reviewed SHA256:

```text
assaf_os_export_reader.py
E9A2AC201DC36178FB45AC36B8BC7A5E54D481127E96632135CB8CB37EDFEEA5
assaf_os_export_provenance.py
1F9354E2BA34191EFB6810D48C19CC7FC205AFD8C908FD5B7BE7B8561C0AD382
assaf_os_export_schema.json
367DB2AD26977F58BC5C29072FE2AB15776284BA1DFA02D752B8A1E2E97D76B5
assaf_os_native_export_oracle.cjs
581ECAFFCCD203A2F9A19DA4B79A5599E9738A8EAB6E4F1A42BF07CCBC202971
test_assaf_os_export_reader.py
8EF04587BDEBAE7D56ECF4AE1815126715E133C4D6A58834EE335C214D161B88
test_assaf_os_export_provenance.py
39FF863910254EAEFCBE636084A38DAF25EF18546F18D15AA74677E96D22BB43
test_assaf_os_export_integration.py
6D7CBB3E58DDC975982E9D3D92EA60D3804523E3E81E985DA00B627A0C57FD8C
```

Audit linkage validates bounded source evidence and preserves immutable journal
contents; it is not cryptographic verification of a human approval.

Docker Desktop's Linux engine was unavailable in this run. No export-test database
container was created. Existing opt-in PostgreSQL cases are reported as skipped,
not replaced with mocks or another database. The previous milestone's actual
disposable PostgreSQL validation remains historical evidence only.

## Remaining gates

Before real data: approve the exact read-only workbook/ranges and source identity;
verify protected encrypted staging, access and retention; inspect actual headers,
date/reference representations and complete source audits; resolve missing or
conflicting provenance. The source's existing `snapshot` operation reconciles and
can write, so it must not be used as a read-only exporter.

Before authority change: independently validate the full real dataset and business
parity, fence all writers, settle outstanding proposals/unknown decisions, freeze
and reconcile the final source, restore-test the independent backup, approve the
mirror and rollback design, and obtain explicit whole-workspace cutover approval.

No Google requests, real records, credentials, production imports or routes,
Cloudflare authentication, paid resources, push, merge, deployment or authority
switch occurred. The synthetic import guard, previous branches, existing runtime,
and other worktrees are preserved.
