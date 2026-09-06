"""
CI pin: Cloud Tasks worker endpoints MUST be OIDC-gated with the documented
service-account email and audience.

The three worker endpoints — ``/poll-incident/{event_id}``, ``/d4h-sync-yes``,
and ``/delete-template/{template_id}`` — sit behind Cloud Run's ``allUsers``
``run.invoker`` grant (necessary so the browser can reach ``GET /`` without a
Cloud Run identity token). Their ONLY auth is the application-layer
``_verify_oidc_request(...)`` call inside each handler, which decodes the
bearer's OIDC claims and rejects anything that isn't signed by Google for
the ``everbridge-poll-sa@<project>.iam.gserviceaccount.com`` subject with the
service URL as audience.

A future refactor that removes the ``_verify_oidc_request`` call — or wires
it to the wrong helper — would make the workers anonymously callable, and
the resulting requests would fire real Everbridge/Slack/D4H mutations
(send SMS, write attendance, create channels). This pin makes such a
regression fail at test-collection time.

Established by PR-S of the 2026-05 security review. Pinned by static
analysis of ``backend/main.py`` AST rather than by FastAPI integration test:
``main.py``'s heavyweight Vertex/httpx/google-cloud-tasks/slack-sdk imports
are not available in the local pytest env (see ``test_poll_incident.py``
header for the historical reason).
"""

import ast
from pathlib import Path

import pytest


# Routes that MUST be OIDC-gated. Order is the order of @app.post decorators
# in main.py at the time of PR-S.
WORKER_ROUTES = [
    "/delete-template/{template_id}",
    "/d4h-sync-yes",
    "/poll-incident/{event_id}",
]


def _find_route_handlers(main_path: Path) -> dict[str, ast.AST]:
    """Map {route_pattern: FunctionDef} for every ``@app.post(<route>)`` decorator.

    Tracks both sync and async function defs. Routes that take a path
    parameter (``{event_id}``) are stored verbatim — the test compares the
    string supplied to ``@app.post()``, not a parsed shape.
    """
    tree = ast.parse(main_path.read_text())
    handlers: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr != "post":
                continue
            if dec.args and isinstance(dec.args[0], ast.Constant):
                route = dec.args[0].value
                if isinstance(route, str):
                    handlers[route] = node
    return handlers


def _find_verify_oidc_call(func_node: ast.AST) -> ast.Call | None:
    """Find the first ``_verify_oidc_request(...)`` call inside a function body."""
    for sub in ast.walk(func_node):
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
            if sub.func.id == "_verify_oidc_request":
                return sub
    return None


def _kwarg_callee(call_node: ast.Call, kwarg_name: str) -> str | None:
    """For a kwarg passed as ``kwarg=<Helper>()``, return the helper function name.

    Returns None if the kwarg isn't present, or its value isn't a Call to a
    bare Name (e.g. a literal string is supplied instead — that would
    indicate a hardcode bypass of the helper, which we want to flag).
    """
    for kw in call_node.keywords:
        if kw.arg != kwarg_name:
            continue
        value = kw.value
        if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
            return value.func.id
        return None
    return None


@pytest.fixture(scope="module")
def main_handlers() -> dict[str, ast.AST]:
    main_path = Path(__file__).parent / "main.py"
    return _find_route_handlers(main_path)


class TestWorkerEndpointsAreOIDCGated:
    """Static-analysis pin: each worker handler calls _verify_oidc_request
    with the documented expected_email + expected_audience helpers.
    """

    @pytest.mark.parametrize("route", WORKER_ROUTES)
    def test_endpoint_calls_verify_oidc_request(self, main_handlers, route):
        func = main_handlers.get(route)
        assert func is not None, (
            f"@app.post({route!r}) handler not found in main.py — "
            f"either renamed or removed. The CI pin must be updated to "
            f"match the new endpoint shape (or this endpoint genuinely "
            f"no longer exists, in which case remove it from WORKER_ROUTES)."
        )
        call = _find_verify_oidc_call(func)
        assert call is not None, (
            f"Handler for {route} does not call _verify_oidc_request. "
            f"This endpoint is publicly reachable via the allUsers Cloud Run "
            f"invoker grant — removing the OIDC check makes it anonymously "
            f"callable, which would let any unauthenticated caller drive real "
            f"Everbridge / Slack / D4H mutations."
        )

    @pytest.mark.parametrize("route", WORKER_ROUTES)
    def test_endpoint_uses_expected_email_helper(self, main_handlers, route):
        func = main_handlers[route]
        call = _find_verify_oidc_call(func)
        callee = _kwarg_callee(call, "expected_email")
        assert callee == "_expected_poll_sa_email", (
            f"Handler for {route} passes expected_email={callee!r}, "
            f"but it must be _expected_poll_sa_email() (the documented "
            f"helper that builds the everbridge-poll-sa@<project> email "
            f"from PROJECT_ID). Wiring drift — could mean a hardcoded "
            f"email shadow or a typoed helper name."
        )

    @pytest.mark.parametrize("route", WORKER_ROUTES)
    def test_endpoint_uses_expected_audience_helper(self, main_handlers, route):
        func = main_handlers[route]
        call = _find_verify_oidc_call(func)
        callee = _kwarg_callee(call, "expected_audience")
        assert callee == "_expected_oidc_audience", (
            f"Handler for {route} passes expected_audience={callee!r}, "
            f"but it must be _expected_oidc_audience() (the documented "
            f"helper that returns CLOUD_RUN_SERVICE_URL). Wiring drift — "
            f"a hardcoded or empty audience would silently accept any "
            f"token signed for any project."
        )
