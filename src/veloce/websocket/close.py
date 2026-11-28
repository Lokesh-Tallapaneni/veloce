"""WebSocket close codes — which a peer may send and how an outbound one is cleaned."""

from __future__ import annotations

import logging

from veloce.status import (
    WS_1000_NORMAL_CLOSURE,
)

# Close codes a peer may send (RFC 6455 Sec. 7.4). Reserved, undefined and
# unassigned ones answer 1002; >=3000 is taken as given.
_PEER_CLOSE_CODES_OK = frozenset(
    {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014}
)

_logger = logging.getLogger(__name__)


def _sanitise_close(code: int, reason: str) -> tuple[int, str]:
    """Normalise an outbound close code and reason to what may go on the wire.

    RFC 6455 Sec. 5.5 caps a control frame at 125 bytes, so the reason is at most
    123 after the 2-byte code; the truncation walks back to a codepoint boundary
    so the frame stays valid UTF-8. Sec. 7.4.1 reserves 1005, 1006 and 1015 for
    local use and forbids them, and anything outside the assigned ranges, from
    appearing on the wire.

    An out-of-range code is coerced rather than raised, because `close` runs on a
    teardown path that suppresses exceptions: a raise would skip the close
    entirely and turn a bad code into the 1006 this exists to avoid.
    """
    if code > 4999 or (code < 3000 and code not in _PEER_CLOSE_CODES_OK):
        _logger.warning(
            "WebSocket close code %s may not appear on the wire (RFC 6455 Sec. 7.4.1); "
            "closing with %s instead",
            code,
            WS_1000_NORMAL_CLOSURE,
        )
        code = WS_1000_NORMAL_CLOSURE
    if not reason:
        return code, ""
    encoded = reason.encode("utf-8")
    if len(encoded) <= 123:
        return code, reason
    encoded = encoded[:123]
    while encoded:
        try:
            return code, encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return code, ""
