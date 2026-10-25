"""CacheControl — parsed view of a `Cache-Control` header.

RFC 9111 Sec. 5.2 defines the directives: a bare token (`no-cache`) or a
`key=value` pair (`max-age=3600`), where numeric values are integer seconds.
Boolean directives read back as True/False attributes and numeric ones as
integer-or-None; the string round-trip preserves source order.
"""

from __future__ import annotations

from typing import Any

from veloce._header_parsing import unquote_value

_BOOL_DIRECTIVES = frozenset(
    {
        "no_cache",
        "no_store",
        "no_transform",
        "must_revalidate",
        "proxy_revalidate",
        "public",
        "private",
        "immutable",
        "only_if_cached",
    }
)

_INT_DIRECTIVES = frozenset(
    {
        "max_age",
        "s_maxage",
        "min_fresh",
        "max_stale",
        "stale_while_revalidate",
        "stale_if_error",
    }
)


def _to_attr(name: str) -> str:
    """Convert wire directive name (`max-age`) to attribute (`max_age`)."""
    return name.replace("-", "_")


def _to_wire(name: str) -> str:
    """Convert attribute (`max_age`) to wire directive (`max-age`)."""
    return name.replace("_", "-")


class CacheControl:
    """Parsed view of a `Cache-Control` header."""

    __slots__ = ("_directives",)

    def __init__(self, header: str | None = "") -> None:
        # True for bare flags, int for numeric directives, str for unknown.
        self._directives: dict[str, Any] = {}
        if not header:
            return
        for token in header.split(","):
            token = token.strip()
            if not token:
                continue
            if "=" in token:
                k, _, v = token.partition("=")
                k = _to_attr(k.strip().lower())
                v = unquote_value(v)
                if k in _INT_DIRECTIVES:
                    # RFC 9111 Sec. 5.2 delta-seconds is `1*DIGIT`, so a
                    # malformed value is dropped, not stored on an int attribute.
                    if v.isdigit():
                        self._directives[k] = int(v)
                else:
                    self._directives[k] = v
            else:
                self._directives[_to_attr(token.lower())] = True

    def to_header(self) -> str:
        """Serialise back to a `Cache-Control` header value.

        Preserves source-observed order; user-set directives append in set order.
        """
        parts: list[str] = []
        for k, v in self._directives.items():
            wire = _to_wire(k)
            # int formats identically to str, so both share the name=value branch.
            if v is True:
                parts.append(wire)
            else:
                parts.append(f"{wire}={v}")
        return ", ".join(parts)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in _BOOL_DIRECTIVES:
            return self._directives.get(name, False)
        if name in _INT_DIRECTIVES:
            return self._directives.get(name)
        # Unknown directive: return what's stored, or None.
        return self._directives.get(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "_directives":
            object.__setattr__(self, name, value)
            return
        if value is None or value is False:
            self._directives.pop(name, None)
            return
        self._directives[name] = value

    def __contains__(self, name: str) -> bool:
        return _to_attr(name) in self._directives

    def __bool__(self) -> bool:
        return bool(self._directives)

    def __str__(self) -> str:
        return self.to_header()

    def __repr__(self) -> str:
        return f"CacheControl({self.to_header()!r})"
