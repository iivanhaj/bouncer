"""Request body size limit: step 1 of the pipeline, before auth (DESIGN.md).

Two reasons it exists:
- A huge body costs memory and parsing time before anything else can reject it.
- The budget headroom assumes a request's prompt can't exceed max_body_bytes tokens
  (DESIGN.md §2). Without this limit, that assumption, and the "never exceeds" guarantee
  built on it, would be false.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import GatewayError, error_response

_MESSAGE = "Request body too large: at most {limit} bytes"


def _too_large(limit: int) -> GatewayError:
    return GatewayError(413, _MESSAGE.format(limit=limit), "invalid_request_error", "request_too_large")


class BodySizeLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Fast path: the client declared the size up front.
        for name, value in scope["headers"]:
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_bytes:
                    error = _too_large(self.max_bytes)
                    response = error_response(error.status_code, error.message, error.type, error.code)
                    await response(scope, receive, send)
                    return

        # Slow path: no Content-Length (chunked upload), or a client that lied about it. Count
        # bytes as they arrive and stop reading as soon as the limit is passed. The GatewayError
        # is raised while the app reads the body, so the normal error handler turns it into a 413.
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _too_large(self.max_bytes)
            return message

        await self.app(scope, limited_receive, send)
