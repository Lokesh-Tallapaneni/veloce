"""Reach into a generated OpenAPI document from a test.

Two one-liners were written out across the OpenAPI cluster: fetching the served
document, and pulling one operation's parameter list out of it. Five modules
carried the first under two different names and three carried the second
byte-identically, which is the shape `tests/_asgi_drive.py` and `tests/_mcp.py`
were created for.

`document` goes through `TestClient` rather than calling `app.openapi()`,
because a difference between the generated document and the served one is a
defect these tests exist to catch. A module asserting something about
generation itself should call `get_openapi_schema(app)` directly instead.
"""

from __future__ import annotations

from veloce import Veloce
from veloce.testclient import TestClient


def document(source: Veloce | TestClient, url: str = "/openapi.json") -> dict:
    """The OpenAPI document as a client receives it.

    Takes an app, or a `TestClient` a module has already built - twenty-six
    sites wrote `client.get("/openapi.json").json()` because building a second
    client to fetch the document would have been wasteful, and so kept the
    one-liner instead of the helper.
    """
    client = source if isinstance(source, TestClient) else TestClient(source)
    return client.get(url).json()


def parameters(schema: dict, path: str, method: str = "get") -> list[dict]:
    """One operation's parameter list, empty when it declares none."""
    return schema["paths"][path][method].get("parameters", [])


def parameter(schema: dict, path: str, name: str, method: str = "get") -> dict | None:
    """One named parameter of one operation, or `None` when it declares none.

    Three modules wrote `next((p for p in parameters(...) if p["name"] == n), None)`
    and one wrote the same search as a loop with a sentinel, so a change to how a
    parameter is located had four places to land.
    """
    for candidate in parameters(schema, path, method):
        if candidate["name"] == name:
            return candidate
    return None
