"""The `TestResponse` returned by the test clients."""

from __future__ import annotations

from typing import Any

from multidict import CIMultiDict

import veloce._json as _json
from veloce._constants import (
    HEADER_CONTENT_TYPE,
    HEADER_SET_COOKIE,
)
from veloce._protocol_constants import (
    SET_COOKIE_JOINER,
)
from veloce.testclient._cookies import _SET_COOKIE_LOWER, _parse_set_cookie_first_pair


class TestResponse:
    """What every `TestClient` / `AsyncTestClient` call returns.

    A read-only view of one ASGI response cycle: `status_code`, the decoded
    `text` and parsed `json()`, a case-insensitive `headers` mapping, the parsed
    `cookies`, and `raw_headers` for what the mapping flattens (several
    `Set-Cookie` lines join into one `headers` value). The clients build it.

    Usage::

        with TestClient(app) as client:
            response = client.get("/items/1")

        assert response.status_code == 200
        assert response.json() == {"id": 1}
        assert response.headers["content-type"].startswith("application/json")
    """

    __slots__ = (
        "status_code",
        "body",
        "headers",
        "content_type",
        "cookies",
        "raw_headers",
    )

    def __init__(
        self,
        status_code: int,
        body: bytes,
        raw_headers: list[tuple[bytes, bytes]],
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.raw_headers = raw_headers
        # Case-insensitive, so `headers["Content-Type"]` works whether or not the app
        # lower-cased its keys. Set-Cookie joins here; `raw_headers` keeps the list.
        flat: CIMultiDict[str] = CIMultiDict()
        set_cookies: list[str] = []
        for k, v in raw_headers:
            name = k.decode("latin-1")
            value = v.decode("latin-1")
            if name.lower() == _SET_COOKIE_LOWER:
                set_cookies.append(value)
            else:
                flat[name] = value
        if set_cookies:
            flat[HEADER_SET_COOKIE] = SET_COOKIE_JOINER.join(set_cookies)
        self.headers = flat
        self.content_type = flat.get(HEADER_CONTENT_TYPE) or ""
        # Parse cookies from all Set-Cookie headers; each cookie's first
        # `name=value` segment wins.
        self.cookies: dict[str, str] = {}
        for line in set_cookies:
            pair = _parse_set_cookie_first_pair(line)
            if pair is not None:
                self.cookies[pair[0]] = pair[1]

    def json(self) -> Any:
        """Parse the response body as JSON and return the result."""
        return _json.loads(self.body)

    @property
    def text(self) -> str:
        """Decode the response body as UTF-8 text."""
        return self.body.decode("utf-8", errors="replace")

    @property
    def content(self) -> bytes:
        """Alias for `body`, matching `httpx.Response`'s own attribute name."""
        return self.body

    def __repr__(self) -> str:
        return f"<TestResponse [{self.status_code}]>"
