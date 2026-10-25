"""Cursor pagination for the MCP list methods — opaque cursors over a registry.

Pagination is opt-in (`mount_mcp(page_size=...)`): `cursor`/`nextCursor` are
optional in the spec, so a client may read only the first page, and a server
that paginated on its own would silently hide the rest of its catalogue.

The cursor is opaque - the spec requires clients not parse it - and records both
the position and the key of the last item emitted, because registries are
mutable at runtime and a pure offset would skip or repeat an unrelated entry.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Iterable, Sequence
from typing import TypeVar

from veloce.contrib.mcp.errors import InvalidParamsError

T = TypeVar("T")

# The plaintext is "<index>:<key>"; a key may itself contain a colon (a resource
# URI does), so the split is on the first one only.
_CURSOR_SEPARATOR = ":"


def encode_cursor(index: int, key: str) -> str:
    """Return the opaque cursor naming the last item emitted."""
    raw = f"{index}{_CURSOR_SEPARATOR}{key}".encode()
    # Padded on purpose - NOT `_internal._b64encode`, which strips `=` for the
    # RFC 7515 / RFC 7636 unpadded form; cursors must keep decoding after a restart.
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
    # It moved: find the key where it is now, not whatever took its position.
    for position, entry in enumerate(entries):
        if key_of(entry) == key:
            return position + 1
    # Gone entirely. Resume where it was - the only position that neither
    # replays the whole catalogue nor skips to its end.
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
            # No cursor was ever issued, so answering the full list anyway
            # would tell the client its position was honoured.
            raise InvalidParamsError("this server does not paginate its list methods")
        return items, None

    entries = items if isinstance(items, Sequence) else list(items)
    start = 0 if cursor is None else _resume_at(entries, key_of, cursor)
    window = entries[start : start + size]
    if not window or start + size >= len(entries):
        return window, None
    return window, encode_cursor(start + size - 1, key_of(window[-1]))
