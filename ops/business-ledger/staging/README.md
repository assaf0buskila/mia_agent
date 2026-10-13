# Business Ledger private staging overlay

This directory adapts the already reviewed `ops/owner-os-staging` package. It does not deploy anything and contains no credential, private configuration, workbook data, or provider response.

The candidate keeps the existing VM, Caddy-only public ports, numeric Telegram owner allowlist, secure cookies, trusted-proxy pin, separate PostgreSQL roles, synthetic database, bot, workbook and workspace. It adds only the exact TEST Business routes and a bounded Google CONNECT proxy. Quick Lead, reminder delivery, owner actions and all other provider workers remain disabled.

## Prepare after both CI heads and images exist

Run `prepare.py` only from clean backend and mobile checkouts whose `HEAD` equals the supplied forty-hex CI head. Supply the existing reviewed package directory, the approved public Apps Script `/exec` URL and TEST workbook ID. The script imports the original package's archive extractor, verifies both Git commits, and emits locked build contexts. Build and test those two images locally, then run `finalize.py` with their exact manifest/config digests. Finalization rechecks every prepared byte before emitting the immutable ZIP and image lock. Neither script builds, pushes, deploys, contacts Google, or changes GCP.

The generated candidate must still pass independent review, image/archive verification, compose rendering, offline tests and a fresh memory measurement before any separately approved deployment. A green application CI run does not prove the generated images, VM, Google service, iPhone flow, or provider behavior.

Required runtime operator inputs remain local to the staging host:

- `staging.vars`: existing host, numeric owners, bot public identifiers, exact locked TEST service URL/workbook/workspace, and `OS_STAGE_BUSINESS_TEST_ENABLED=true`.
- `secrets/business_ledger_hmac_secret`: the existing TEST key, UID 10001, mode 0600, under the existing 0700 secrets directory.
- Existing bot and database secret files with their current permissions.

Start/upgrade commands are intentionally not included here. The release procedure must use the generated manifest, both verified image locks, the existing rollback bundle, session revocation, and the project's separately approved deployment runbook.

## Network contract

Mia and the proxy share a private `ledger_proxy` bridge. The proxy binds only its fixed private address and joins the existing outbound edge bridge for DNS/TLS egress. It accepts only `CONNECT script.google.com:443` and `CONNECT script.googleusercontent.com:443`, resolves every connection, and rejects loopback, private, link-local, multicast, reserved, unspecified and metadata addresses. Headers, idle time, connect time, stream lifetime and byte counts are bounded. Logs contain reason codes and booleans only. BFF and PostgreSQL remain on isolated networks and receive no proxy endpoint.

The application still validates exact TEST mode, numeric owner, owner-session scopes, locked workspace, locked service URL and a server-only HMAC key before transport. The proxy is an additional network boundary, not an authorization substitute.

DNS resolution is asynchronous and separately bounded; one total deadline covers header parsing, DNS, every connection attempt and both tunnel streams. Numerical upstream IPs are used only after every DNS answer passes the public-address guard.

## Upgrade and rollback boundary

Preparation rejects any migration delta from the original approved backend. The bootstrap accepts exactly the existing database/backend marker keys, validates auth/read roles and only then advances the backend marker. It never imports customer data or changes the Google workbook during packaging.

Rollback uses the old application images and configuration against the existing staging PostgreSQL volume. Revoke sessions using the original operator command, stop the candidate including its proxy, and restore the old services without running the old `init`/bootstrap: that old bootstrap deliberately rejects an upgraded SHA marker. The original session-revocation command and API readiness do not require the old SHA marker. Verify cookie rejection, old health/SHA and resource isolation before declaring rollback successful. Container verification of this rollback is a release gate, not a completed VM action.
