"""The v2 router mount (V2-16s), as amended by §22 E24 (V2-19's reads) on E19's method.

``app.router.routes`` holds included-router wrappers, so the mounted paths are read from the app's
OpenAPI schema. V2-19 fills the reads router with exactly its two §9 routes, and V2-26 (§12.3) fills
the statements router with exactly its nine. Every other v2 router stays empty, and V2-24 adds its
commits routes on top.
"""

from app.api.os_v2 import reads, statements
from app.main import app

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
# Segments that only later v2 chunks may mount. None is mounted yet.
V2_ONLY_SEGMENTS = {
    "commits",
    "deleted",
    "assistant-credentials",
}
# receipts is shared with v1 (GET by request id); no v2 chunk has added to it yet.
V1_RECEIPTS = {("GET", "/v1/owner/os/unified/receipts/{request_id}")}


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
        if router is reads.router:
            assert {(m, r.path) for r in router.routes for m in r.methods} == READS_ROUTES
        elif router is statements.router:
            assert {(m, r.path) for r in router.routes for m in r.methods} == STATEMENT_ROUTES
        else:
            assert router.routes == []
    mounted = _mounted()
    paths = {path for _, path in mounted}
    assert READS_ROUTES <= mounted
    assert not any(p.startswith(("/v1/assistant", "/v1/owner/auth/step-up")) for p in paths)
    assert not {(m, p) for m, p in mounted if _segment(p) in V2_ONLY_SEGMENTS}
    assert {(m, p) for m, p in mounted if _segment(p) in STATEMENT_SEGMENTS} == STATEMENT_ROUTES
    assert {(m, p) for m, p in mounted if _segment(p) == "receipts"} == V1_RECEIPTS
