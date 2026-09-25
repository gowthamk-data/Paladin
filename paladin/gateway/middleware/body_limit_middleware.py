"""Bound the size of an inbound request body before any handler or dependency runs.

This is deliberately a simple Content-Length pre-check rather than a streaming byte counter:
the API takes every parameter as a query string, so the useful limits are the per-field ones
(see ``MAX_PROMPT_CHARS`` in :mod:`gateway.routes.query_routes`). This guard exists to stop a
hostile client from making the process read an unbounded payload at all.

Limitation: a client that omits ``Content-Length`` (chunked transfer encoding) is not rejected
here; it is still bounded by the per-field limits in the route handlers.
"""

import json

from starlette.types import ASGIApp, Receive, Scope, Send


class MaxBodySizeMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                too_large = int(declared) > self.max_bytes
            except ValueError:
                too_large = True
            if too_large:
                await self._reject(scope, send)
                return

        await self.app(scope, receive, send)

    async def _reject(self, scope: Scope, send: Send) -> None:
        request_id = (scope.get("state") or {}).get("correlation_id", "unknown")
        body = json.dumps(
            {
                "detail": f"Request body exceeds the {self.max_bytes} byte limit",
                "request_id": request_id,
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
