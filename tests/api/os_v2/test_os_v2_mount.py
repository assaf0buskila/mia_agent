from app.main import app


def test_v2_routers_mount_with_zero_routes(v2_routers):
    mounted = {id(route) for route in app.router.routes}
    for router, prefix in v2_routers:
        assert router.prefix == prefix
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
