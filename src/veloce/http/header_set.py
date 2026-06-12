"""`HeaderSet` — ordered mutable set with comma-separated header serialisation.

A datastructure for headers whose value is a list of
tokens (`Allow`, `Vary`, `Access-Control-Allow-Methods`, ...). Case-insensitive
on lookup; preserves insertion order on iteration and serialisation.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator


class HeaderSet:
    """Ordered, case-insensitive set of header tokens.

    Used for headers like `Vary` and `Allow` that carry a comma-joined
    token list:

    - `__contains__` is case-insensitive.
    - `add` / `discard` / `remove` mutate in place.
    - `to_header()` round-trips to a comma-separated header value.
    - Iteration yields items in insertion order.
    """

    __slots__ = ("_items", "_lower")

    def __init__(self, value: Iterable[str] | str | None = None) -> None:
        self._items: list[str] = []  # insertion-ordered originals
        self._lower: set[str] = set()  # lower-cased lookup index
        if value is None:
            return
        if isinstance(value, str):
            tokens = [v.strip() for v in value.split(",") if v.strip()]
        else:
            tokens = [v.strip() for v in value if v and v.strip()]
        for token in tokens:
            self.add(token)

    def add(self, header: str) -> None:
        """Add `header` to the set. No-op if already present (case-insensitive)."""
        key = header.lower()
        if key in self._lower:
            return
        self._lower.add(key)
        self._items.append(header)

    def discard(self, header: str) -> None:
        """Remove `header` if present. Never raises (set semantics)."""
        key = header.lower()
        if key not in self._lower:
            return
        self._lower.remove(key)
        self._items = [x for x in self._items if x.lower() != key]

    def remove(self, header: str) -> None:
        """Remove `header`; raise `KeyError` if absent."""
        key = header.lower()
        if key not in self._lower:
            raise KeyError(header)
        self.discard(header)

    def update(self, headers: Iterable[str]) -> None:
        for h in headers:
            self.add(h)

    def clear(self) -> None:
        self._items.clear()
        self._lower.clear()

    def to_header(self) -> str:
        """Serialise as a comma-separated header value (insertion order)."""
        return ", ".join(self._items)

    def __contains__(self, header: str) -> bool:
        return header.lower() in self._lower

    def __iter__(self) -> Iterator[str]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __bool__(self) -> bool:
        return bool(self._items)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, HeaderSet):
            return self._lower == other._lower
        if isinstance(other, (list, tuple, set, frozenset)):
            return self._lower == {str(x).lower() for x in other}
        return NotImplemented

    def __repr__(self) -> str:
        return f"HeaderSet({self._items!r})"

    def __str__(self) -> str:
        return self.to_header()
