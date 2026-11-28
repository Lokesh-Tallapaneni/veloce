"""Request — the public request object, assembled from its focused layers."""

from __future__ import annotations

from veloce.http.request._body import BodyMixin


class Request(BodyMixin):
    """Incoming HTTP request with lazy attribute parsing.

    All expensive operations (JSON parsing, cookie parsing, URL construction,
    form/multipart parsing) are deferred until accessed — zero overhead for
    properties you don't use.

    Usage::

        @app.get("/users/{user_id}")
        async def show(request: Request, user_id: str):
            data = await request.json()
            agent = request.headers.get("user-agent", "")
            return {"id": user_id, "agent": agent, "body": data}
    """

    __slots__ = ()
