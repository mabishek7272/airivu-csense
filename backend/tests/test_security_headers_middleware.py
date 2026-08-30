"""SecurityHeadersMiddleware (CHECKLIST: "DAST baseline scan against local stack") - two
real findings from scripts/dast_baseline.py (OWASP ZAP), fixed at the one shared
middleware every service already installs. A minimal Starlette app, not either real
service, so this stays fast and dependency-free (no DB, no settings, no real stack).
"""
from __future__ import annotations

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from csense_shared.middleware import SecurityHeadersMiddleware


def _make_app() -> Starlette:
    async def ok(request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/ping", ok)])
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def test_x_content_type_options_is_set():
    client = TestClient(_make_app())
    response = client.get("/ping")
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_cross_origin_resource_policy_is_set():
    client = TestClient(_make_app())
    response = client.get("/ping")
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"


def test_headers_are_set_on_a_typed_exception_handlers_response():
    """A deliberate error response - this codebase's own `ApiError`/`api_error_handler`
    shape (every "every failure looks the same" 401, every 402/404/422/429 this session
    built) - must not slip past the middleware unpatched. This registers a
    *specific*-type handler (not `Exception`/500), the same as `api_error_handler` is
    registered in both real services: Starlette routes a specific-type handler through
    `ExceptionMiddleware`, which sits inside `add_middleware`-registered middleware, so
    the converted response really does flow back through this one."""
    class Boom(Exception):
        pass

    async def boom(request):
        raise Boom("deliberate")

    async def handle_boom(request: Request, exc: Exception):
        return JSONResponse({"error": str(exc)}, status_code=422)

    app = Starlette(routes=[Route("/boom", boom)], exception_handlers={Boom: handle_boom})
    app.add_middleware(SecurityHeadersMiddleware)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/boom")
    assert response.status_code == 422
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("Cross-Origin-Resource-Policy") == "same-origin"


def test_a_catch_all_500_handler_is_a_named_architectural_boundary_not_silently_broken():
    """A handler registered for the bare `Exception` type (this codebase's own
    `unhandled_exception_handler` - truly unexpected bugs, not a deliberate error path)
    is routed by Starlette to `ServerErrorMiddleware`, which is *outermost* - genuinely
    unreachable by any `add_middleware`-registered middleware, ASGI or otherwise. Every
    real, deliberate error response in this codebase goes through `ApiError` instead
    (see the test above) - this one case is a documented, architectural gap, not
    something quietly missing that a future contributor should "fix" by contorting the
    middleware; asserting the gap here means a future Starlette version closing it would
    be noticed, not assumed away."""
    async def boom(request):
        raise ValueError("deliberate, truly unexpected")

    async def handle_boom(request: Request, exc: Exception):
        return JSONResponse({"error": str(exc)}, status_code=500)

    app = Starlette(routes=[Route("/boom", boom)], exception_handlers={Exception: handle_boom})
    app.add_middleware(SecurityHeadersMiddleware)
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get("/boom")
    assert response.status_code == 500
    assert response.headers.get("X-Content-Type-Options") is None
