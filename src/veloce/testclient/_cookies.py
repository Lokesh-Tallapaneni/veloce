"""Cookie handling for the test client — Set-Cookie parsing and the jar view."""

from __future__ import annotations

from collections.abc import (
    Iterator,
    KeysView,
)
from typing import TYPE_CHECKING

from veloce._constants import (
    HEADER_SET_COOKIE,
)
from veloce.http.cookies import iter_cookies

if TYPE_CHECKING:  # pragma: no cover
    from veloce.testclient.async_client import AsyncTestClient
    from veloce.testclient.client import TestClient

    pass

# Lowercased once, so the per-header scan does not recompute it.
_SET_COOKIE_LOWER = HEADER_SET_COOKIE.lower()


def _decode_cookie_value(value: str) -> str:
    """Return what a handler is given for a jar value, undoing `dump_cookie`.

    The jar keeps the wire form so it can re-emit it verbatim; every read surface
    reports this instead, so a test asserts on what the server parses. Through
    `iter_cookies`, the one cookie reader, rather than repeating its unquoting.
    """
    for _name, decoded in iter_cookies("v=" + value):
        return decoded
    return value


def _parse_set_cookie_first_pair(value: str, *, decode: bool = True) -> tuple[str, str] | None:
    """Return the `(name, value)` of a Set-Cookie header's first segment.

    The leading `name=value` pair is the cookie itself and the rest are
    attributes; `None` when the first segment has no `=`.

    `decode=True` percent-decodes through `iter_cookies`, so a test asserts on
    what the handler is given. `decode=False` keeps the wire form the jar stores,
    which it re-emits as a browser does.
    """
    first = value.split(";", 1)[0].strip()
    if "=" not in first:
        return None
    if decode:
        for name, decoded in iter_cookies(first):
            return name, decoded
        return None
    cname, _, cval = first.partition("=")
    return cname.strip(), cval.strip()


def _apply_set_cookie_to_jar(jar: dict[str, str], raw_headers: list[tuple[bytes, bytes]]) -> None:
    """Update `jar` from the response's `Set-Cookie` headers.

    Honours `Max-Age=0` as a deletion signal (RFC 6265 Sec. 5.2.2). Both test
    clients share this, so a fix to the cookie semantics applies to sync and
    async at once.
    """
    for name_bytes, value_bytes in raw_headers:
        name = name_bytes.decode("latin-1")
        if name.lower() != _SET_COOKIE_LOWER:
            continue
        value = value_bytes.decode("latin-1")
        # `decode=False`: the jar re-emits the bytes verbatim, as a browser does.
        pair = _parse_set_cookie_first_pair(value, decode=False)
        if pair is None:
            continue
        cname, cval = pair
        rest = value.split(";")[1:]
        is_deletion = any("max-age=0" in seg.strip().lower() for seg in rest)
        if is_deletion:
            jar.pop(cname, None)
        else:
            jar[cname] = cval


class _TestClientCookies:
    """Live view of a `TestClient`'s cookie jar.

    Backs onto `TestClient._cookies`, so a cookie added here applies to every
    later request and one the server sends merges into the same dict.
    """

    __slots__ = ("_client",)

    def __init__(self, client: TestClient | AsyncTestClient) -> None:
        self._client = client

    def __getitem__(self, key: str) -> str:
        return _decode_cookie_value(self._client._cookies[key])

    def __setitem__(self, key: str, value: str) -> None:
        self._client._cookies[key] = value

    def __delitem__(self, key: str) -> None:
        del self._client._cookies[key]

    def __contains__(self, key: str) -> bool:
        return key in self._client._cookies

    def __iter__(self) -> Iterator[str]:
        return iter(self._client._cookies)

    def __len__(self) -> int:
        return len(self._client._cookies)

    def get(self, key: str, default: str | None = None) -> str | None:
        raw = self._client._cookies.get(key)
        return default if raw is None else _decode_cookie_value(raw)

    def clear(self) -> None:
        self._client._cookies.clear()

    def update(self, other: dict[str, str]) -> None:
        self._client._cookies.update(other)

    def items(self) -> list[tuple[str, str]]:
        return [(k, _decode_cookie_value(v)) for k, v in self._client._cookies.items()]

    def keys(self) -> KeysView[str]:
        return self._client._cookies.keys()

    def values(self) -> list[str]:
        return [_decode_cookie_value(v) for v in self._client._cookies.values()]

    def __repr__(self) -> str:
        return f"<TestClient cookies: {dict(self.items())}>"
