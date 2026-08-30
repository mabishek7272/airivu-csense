"""Correlation-ID and security-headers middleware, shared by every service.

`CorrelationIdMiddleware`: TRD §10.1's `X-Correlation-ID`, accepted or generated and
returned on every response.

`SecurityHeadersMiddleware`: two real findings from the CHECKLIST's own DAST baseline
scan (`scripts/dast_baseline.py`, OWASP ZAP against the real running services), fixed at
the one shared place rather than per-router - a header a future new endpoint might
otherwise forget to set is not a header at all.
"""
from __future__ import annotations

import uuid

from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

CORRELATION_HEADER = "X-Correlation-ID"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        correlation_id = request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = correlation_id
        return response


class SecurityHeadersMiddleware:
    """`X-Content-Type-Options: nosniff` - stops a browser from MIME-sniffing a JSON
    response body into something it will execute (ZAP's "X-Content-Type-Options Header
    Missing", every endpoint).

    `Cross-Origin-Resource-Policy: same-origin` - opts every response out of being
    fetched as a subresource (`<script src>`, `<img>`) from another origin, closing the
    Spectre-class side-channel ZAP's "Cross-Origin-Resource-Policy Header Missing" names.
    `same-origin`, not the more permissive `same-site` or `cross-origin`, is correct here
    specifically because Traefik serves each frontend and its own API on the *same*
    origin (path-routed under `app.localhost`/`console.localhost`) - the one place a
    legitimate cross-origin caller exists is a local Vite dev server making a CORS
    `fetch()` request, which CORP does not govern (CORP restricts subresource embedding;
    CORS already governs `fetch`/XHR access separately, via each app's own
    `customer_crm_origins`/`developer_console_origins` allow-list).

    **A plain ASGI middleware, deliberately not `BaseHTTPMiddleware`.** `BaseHTTPMiddleware`
    runs the downstream app in a background task and only sees what comes back through
    that task's own response object - a well-documented Starlette gotcha is that a
    response built by an `add_exception_handler` handler (this codebase's own
    `api_error_handler`/`unhandled_exception_handler`, registered on both services) does
    not reliably flow back through it. These headers exist specifically to protect
    against a browser doing something unexpected with a response body, and a 401/404/422
    error response is exactly as much a target for that as a 200 - skipping error
    responses would defeat the point. Operating at the raw ASGI `send` level instead
    catches every response, including ones exception handlers produce.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                headers["Cross-Origin-Resource-Policy"] = "same-origin"
            await send(message)

        await self.app(scope, receive, send_with_headers)
