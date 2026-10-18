"""The V2-26 statement routes are mounted on the app, found through ``app.openapi()`` (§22 E19).

``app.router.routes`` holds included-router wrappers under FastAPI 0.141, so a path or id check on
it proves nothing. The OpenAPI schema lists every mounted operation.
"""

from app.api.os_v2.statements import router
from app.main import app

from tests.api.os_v2.test_os_v2_mount import STATEMENT_ROUTES


def mounted() -> set[tuple[str, str]]:
    return {(method.upper(), path) for path, operations in app.openapi()["paths"].items()
            for method in operations}


def test_the_nine_statement_routes_are_mounted_and_nothing_else_is_added():
    found = mounted()
    assert STATEMENT_ROUTES <= found
    segments = {"statements", "statement-batches", "merchant-rules"}
    unified = {(m, p) for m, p in found if p.startswith("/v1/owner/os/unified/")
               and p.split("/")[5] in segments}
    assert unified == STATEMENT_ROUTES


def test_every_statement_route_is_gated_by_the_flag_guard():
    for route in router.routes:
        names = {dependency.call.__name__ for dependency in route.dependant.dependencies}
        assert "route_guard" in names, route.path
