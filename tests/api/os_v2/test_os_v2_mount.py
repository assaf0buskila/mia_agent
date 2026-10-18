from app.api.os_v2 import statements
from app.main import app

# §12.3 (V2-26): the statements router serves exactly these nine routes. Their mount is pinned,
# through app.openapi(), by test_os_v2_statements_mount.py.
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


def test_v2_routers_mount_with_zero_routes(v2_routers):
    mounted = {id(route) for route in app.router.routes}
    for router, prefix in v2_routers:
        assert router.prefix == prefix
        if router is statements.router:
            assert {(m, r.path) for r in router.routes for m in r.methods} == STATEMENT_ROUTES
        else:
            assert router.routes == []
        assert not mounted & {id(route) for route in router.routes}
    paths = {getattr(route, "path", "") for route in app.router.routes}
    assert not any(p.startswith(("/v1/assistant", "/v1/owner/auth/step-up")) for p in paths)
    assert not any(
        p.startswith("/v1/owner/os/unified/")
        and p.split("/")[5]
        in {
            "commits",
            "deleted",
            "assistant-credentials",
            "receipts",
        }
        for p in paths
    )
