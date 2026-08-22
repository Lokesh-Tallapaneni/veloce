"""Cursor pagination for the MCP list methods — opaque cursors over a registry.

A server with many primitives sends its whole catalogue in one response, and
every byte of it lands in the agent's context window. The spec's answer is an
optional cursor: a list method accepts `cursor` and answers with `nextCursor`
while more remain, so a client walks the catalogue a page at a time.

Pagination is opt-in (`mount_mcp(page_size=...)`). `cursor`/`nextCursor` are
optional in the spec, so a client is free to ignore `nextCursor` and read only
the first page - which would silently hide the rest of the catalogue from every
existing client if a server started paginating on its own. A server that has not
opted in answers exactly as before, and pays nothing for the option.

The cursor is opaque to the client (the spec requires that clients not parse it)
and records *both* the position and the key of the last item emitted. Position
alone is wrong the moment the catalogue changes between pages - registries are
mutable at runtime, and a tool registered or removed mid-walk shifts every later
index, so a pure offset silently skips or repeats an unrelated entry. Position
alone is also the only thing that is O(1). Recording both keeps the fast path:
the index is checked first and used when the key still sits there, and only a
catalogue that actually moved pays the scan to find the key again.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

from veloce.contrib.mcp.errors import InvalidParamsError

T = TypeVar("T")

# The cursor's plaintext is "<index>:<key>". A key may itself contain a colon (a
# resource URI does), so the split is on the first one only.
_CURSOR_SEPARATOR = ":"


def encode_cursor(index: int, key: str) -> str:
    """Return the opaque cursor naming the last item emitted."""
    raw = f"{index}{_CURSOR_SEPARATOR}{key}".encode()
    # Padded on purpose - NOT `_internal._b64encode`, which strips `=` for the
    # RFC 7515 / RFC 7636 unpadded form. A cursor is an opaque round-trip token
    # whose only contract is with `decode_cursor` below, and cursors already
    # issued by a running process must keep decoding across a restart.
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> tuple[int, str]:
    """Return the `(index, key)` a cursor records, rejecting a malformed one."""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode()
        index_text, key = raw.split(_CURSOR_SEPARATOR, 1)
        index = int(index_text)
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise InvalidParamsError(f"invalid cursor: {cursor!r}") from exc
    if index < 0:
        raise InvalidParamsError(f"invalid cursor: {cursor!r}")
    return index, key


def _resume_at(entries: Sequence[T], key_of: Callable[[T], str], cursor: str) -> int:
    """Return the index just past the item a cursor names."""
    index, key = decode_cursor(cursor)
    # The catalogue usually has not moved, so check the recorded position first.
    if index < len(entries) and key_of(entries[index]) == key:
        return index + 1
    # It moved. Find the key where it is now, so the walk continues from the same
    # item rather than from a position that now belongs to something else.
    for position, entry in enumerate(entries):
        if key_of(entry) == key:
            return position + 1
    # The item itself is gone. Resume from where it was: the spec makes no
    # promise about entries added or removed mid-walk, and this is the only
    # position that neither replays the whole catalogue nor skips to its end.
    return min(index, len(entries))


def paginate(
    items: Iterable[T],
    key_of: Callable[[T], str],
    cursor: str | None,
    size: int | None,
) -> tuple[Iterable[T], str | None]:
    """Return one page of `items` and the cursor for the next, if any.

    With no page size the items are returned untouched - no copy, no slice - so a
    server that has not opted in pays one comparison.
    """
    if size is None:
        if cursor is not None:
            # No cursor was ever issued, so any cursor presented is not one of
            # ours. Answering the full list anyway would tell the client its
            # position was honoured.
            raise InvalidParamsError("this server does not paginate its list methods")
        return items, None

    entries = items if isinstance(items, Sequence) else list(items)
    start = 0 if cursor is None else _resume_at(entries, key_of, cursor)
    window = entries[start : start + size]
    if not window or start + size >= len(entries):
        return window, None
    return window, encode_cursor(start + size - 1, key_of(window[-1]))
