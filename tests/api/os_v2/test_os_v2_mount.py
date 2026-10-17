from app.api.os_v2 import commits
from app.main import app

# §22 E17: V2-24 fills the commits router with exactly its four §11.1 routes, still unmounted.
COMMITS_ROUTES = {
    ("POST", "/v1/owner/os/unified/commits"),
    ("POST", "/v1/owner/os/unified/commits/{request_id}/undo"),
    ("POST", "/v1/owner/os/unified/commits/{request_id}/abandon"),
    ("GET", "/v1/owner/os/unified/deleted"),
}


def test_v2_routers_mount_with_zero_routes(v2_routers):
    mounted = {id(route) for route in app.router.routes}
    for router, prefix in v2_routers:
        assert router.prefix == prefix
        if router is commits.router:
            assert {(m, r.path) for r in router.routes for m in r.methods} == COMMITS_ROUTES
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
            "statements",
            "statement-batches",
            "merchant-rules",
            "assistant-credentials",
            "receipts",
        }
        for p in paths
    )
