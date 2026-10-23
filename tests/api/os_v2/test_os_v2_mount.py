"""The v2 router mount (V2-16s), as amended by §22 E17, E19 and E24.

E19: the flag is the gate. V2-24's four owner-direct routes are mounted on the app. They answer
404 while ``assaf_os_owner_direct_enabled`` is off (an invalid value coerces to off) or the backend
is not postgres. ``app.router.routes`` holds included-router wrappers, so the mounted paths are
read from the app's OpenAPI schema.

E24: V2-19 fills the reads router with exactly its two §9 routes, and V2-26 (§12.3) fills the
statements router with exactly its nine. Every other v2 router stays empty, except V2-24's commits
router, which carries exactly its four routes.
"""

import pytest
from app.api.os_v2 import commits, reads, statements
from app.core.config import MiaEnv, Settings, get_settings
from app.main import app
from fastapi.testclient import TestClient

COMMITS_ROUTES = {
    ("POST", "/v1/owner/os/unified/commits"),
    ("POST", "/v1/owner/os/unified/commits/{request_id}/undo"),
    ("POST", "/v1/owner/os/unified/commits/{request_id}/abandon"),
    ("GET", "/v1/owner/os/unified/deleted"),
}
# §22 E24: V2-19 (§9) fills the reads router with exactly these two routes.
READS_ROUTES = {
    ("GET", "/v1/owner/os/unified/pending"),
    ("GET", "/v1/owner/os/unified/proposal-requests/{request_id}"),
}
# §12.3 (V2-26): the statements router serves exactly these nine routes. Their mount is also pinned
# by test_os_v2_statements_mount.py.
STATEMENT_ROUTES = {
    ("GET", "/v1/owner/os/unified/statements"),
    ("GET", "/v1/owner/os/unified/statements/{statement_id}"),
    ("POST", "/v1/owner/os/unified/statement-batches"),
    ("GET", "/v1/owner/os/unified/statement-batches/{proposal_id}"),
    ("GET", "/v1/owner/os/unified/statement-batches/{proposal_id}/lines"),
    ("POST", "/v1/owner/os/unified/statement-batches/{proposal_id}/draft"),
    ("POST", "/v1/owner/os/unified/statement-batches/{proposal_id}/approve"),
    ("POST", "/v1/owner/os/unified/statement-batches/{proposal_id}/reject"),
    ("GET", "/v1/owner/os/unified/merchant-rules"),
}
STATEMENT_SEGMENTS = {"statements", "statement-batches", "merchant-rules"}
# Segments that only v2 chunks may mount. V2-24 owns commits and deleted; the others stay unmounted.
V2_ONLY_SEGMENTS = {
    "commits",
    "deleted",
    "assistant-credentials",
}
# receipts is shared with v1 (GET by request id); no v2 chunk has added to it yet.
V1_RECEIPTS = {("GET", "/v1/owner/os/unified/receipts/{request_id}")}
REQUEST_ID = "12345678-1234-4234-8234-123456789012"
CALLS = (
    ("POST", "/v1/owner/os/unified/commits"),
    ("POST", f"/v1/owner/os/unified/commits/{REQUEST_ID}/undo"),
    ("POST", f"/v1/owner/os/unified/commits/{REQUEST_ID}/abandon"),
    ("GET", "/v1/owner/os/unified/deleted"),
)


def _mounted() -> set[tuple[str, str]]:
    return {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }


def _segment(path: str) -> str:
    parts = path.split("/")
    return parts[5] if path.startswith("/v1/owner/os/unified/") and len(parts) > 5 else ""


def test_v2_routers_mount_with_zero_routes(v2_routers):
    for router, prefix in v2_routers:
        assert router.prefix == prefix
        if router is commits.router:
            assert {(m, r.path) for r in router.routes for m in r.methods} == COMMITS_ROUTES
        elif router is reads.router:
            assert {(m, r.path) for r in router.routes for m in r.methods} == READS_ROUTES
        elif router is statements.router:
            assert {(m, r.path) for r in router.routes for m in r.methods} == STATEMENT_ROUTES
        else:
            assert router.routes == []
    mounted = _mounted()
    paths = {path for _, path in mounted}
    assert READS_ROUTES <= mounted
    assert not any(p.startswith(("/v1/assistant", "/v1/owner/auth/step-up")) for p in paths)
    assert {(m, p) for m, p in mounted if _segment(p) in V2_ONLY_SEGMENTS} == COMMITS_ROUTES
    assert {(m, p) for m, p in mounted if _segment(p) in STATEMENT_SEGMENTS} == STATEMENT_ROUTES
    assert {(m, p) for m, p in mounted if _segment(p) == "receipts"} == V1_RECEIPTS


@pytest.mark.parametrize(
    "changes",
    [
        {},  # the default: owner-direct off
        {"assaf_os_owner_direct_enabled": "maybe"},  # an invalid value coerces to off
        {"assaf_os_owner_direct_enabled": True, "assaf_os_backend": "sheets"},
    ],
    ids=["default-off", "invalid-coerced-off", "on-but-sheets"],
)
def test_the_mounted_owner_direct_routes_are_404_unless_the_flag_and_postgres(changes):
    config = Settings(_env_file=None, env=MiaEnv.TEST, **changes)
    assert config.assaf_os_owner_direct_enabled is (changes == {
        "assaf_os_owner_direct_enabled": True, "assaf_os_backend": "sheets"})
    app.dependency_overrides[get_settings] = lambda: config
    try:
        client = TestClient(app, base_url="https://testserver")
        for method, path in CALLS:
            response = client.request(method, path, json={} if method == "POST" else None)
            assert response.status_code == 404, (method, path, response.text)
            assert response.json()["error"]["code"] == "operation_unavailable", (method, path)
    finally:
        app.dependency_overrides.pop(get_settings, None)
