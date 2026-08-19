"""Transport-level guards for the hosted Streamable HTTP surface."""

from __future__ import annotations

from starlette.types import ASGIApp, Receive, Scope, Send

METHOD_NOT_ALLOWED_BODY = b'{"error":"method_not_allowed"}'


class StandaloneGetRejectionMiddleware:
    """Answer a standalone ``GET`` with 405 instead of a stream we can never speak on.

    The hosted server runs stateless, so it never initiates messages, but the SDK still
    accepts ``GET`` on the MCP endpoint and holds the connection open for a server-sent
    stream that stays silent forever. A client that probes with ``GET`` before anything
    else — Codex CLI does exactly that while discovering OAuth — waits until it gives up,
    and the same hang reproduces on production. The specification is explicit that a
    server which does not offer an SSE stream at this endpoint returns 405.

    Only the MCP endpoint itself is affected: discovery documents are served by ``GET``
    on their own paths and must keep working.
    """

    def __init__(self, app: ASGIApp, *, endpoint_path: str) -> None:
        self._app = app
        self._endpoint_path = endpoint_path.rstrip("/") or "/"

    def _is_endpoint(self, path: str) -> bool:
        return (path.rstrip("/") or "/") == self._endpoint_path

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") == "GET"
            and self._is_endpoint(scope.get("path", ""))
        ):
            await send(
                {
                    "type": "http.response.start",
                    "status": 405,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(METHOD_NOT_ALLOWED_BODY)).encode("ascii")),
                        (b"allow", b"POST"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": METHOD_NOT_ALLOWED_BODY})
            return
        await self._app(scope, receive, send)
