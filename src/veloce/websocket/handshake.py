"""WebSocket handshake helpers — the accept key and option validation."""

from __future__ import annotations

import base64
import hashlib
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    pass


# RFC 6455 Sec. 1.3: `Sec-WebSocket-Accept` is base64(SHA-1(key + GUID)).
_WS_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def compute_accept(key: str) -> str:
    """Compute the RFC 6455 Sec. 1.3 `Sec-WebSocket-Accept` value for `key`.

    SHA-1 is mandated by the spec for the handshake fingerprint; it is not used
    here as a security primitive.
    """
    return base64.b64encode(hashlib.sha1((key + _WS_ACCEPT_GUID).encode()).digest()).decode()


def _validate_positive_seconds(value: float | None, name: str) -> None:
    """Reject a non-finite or non-positive duration in seconds.

    `None` disables the feature. NaN fails `> 0` while Infinity passes it but is
    meaningless as a deadline, so `math.isfinite` rejects both.
    """
    if value is not None and not (math.isfinite(value) and value > 0):
        raise ValueError(f"{name} must be a finite positive number of seconds, got {value!r}")


def _validate_idle_timeout(idle_timeout: float | None) -> None:
    """Reject a non-finite or non-positive idle timeout."""
    _validate_positive_seconds(idle_timeout, "idle_timeout")


def _validate_heartbeat(heartbeat: float | None) -> None:
    """Reject a non-finite or non-positive heartbeat interval."""
    _validate_positive_seconds(heartbeat, "heartbeat")
