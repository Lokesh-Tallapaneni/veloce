"""Request header assembly for the test client."""

from __future__ import annotations

from collections.abc import (
    Mapping,
    Sequence,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    pass


#: A mapping, or ordered pairs when a header must appear more than once.
#: Private: callers pass the value, not the name of its type.
_HeadersArg = Mapping[str, str] | Sequence[tuple[str, str]] | None


def _header_pairs(headers: _HeadersArg) -> list[tuple[str, str]]:
    """Normalise a headers argument to ordered `(name, value)` pairs.

    A mapping cannot carry a header twice and HTTP allows it - two
    `Authorization` headers, several `X-Forwarded-For` hops - so pairs are
    accepted and kept in order.
    """
    if headers is None:
        return []
    if isinstance(headers, Mapping):
        return list(headers.items())
    return [(name, value) for name, value in headers]


def _has_header(pairs: list[tuple[str, str]], name: str) -> bool:
    """True when `name` is already present, whatever its case."""
    lowered = name.lower()
    return any(key.lower() == lowered for key, _ in pairs)


def _set_header(pairs: list[tuple[str, str]], name: str, value: str) -> None:
    """Replace every occurrence of `name` with one pair carrying `value`."""
    lowered = name.lower()
    pairs[:] = [(key, existing) for key, existing in pairs if key.lower() != lowered]
    pairs.append((name, value))


def _build_request_headers(
    base: dict[str, str],
    cookies: dict[str, str],
    extra: _HeadersArg,
) -> list[tuple[str, str]]:
    """Merge base headers, the cookie jar and per-call `extra` into ordered pairs.

    The single home for the merge order, so the sync and async test clients
    stay in lockstep.
    """
    extra_pairs = _header_pairs(extra)
    # A per-call header replaces a client-level one of the same name, but repeats
    # within the call are kept - which is why overridden names are dropped first.
    overridden = {name.lower() for name, _ in extra_pairs}
    merged = [(name, value) for name, value in base.items() if name.lower() not in overridden]
    if cookies and "cookie" not in overridden:
        merged.append(("cookie", "; ".join(f"{k}={v}" for k, v in cookies.items())))
    merged.extend(extra_pairs)
    return merged
