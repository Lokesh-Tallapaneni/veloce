"""Small serving-protocol helpers — TCP keepalive, HEAD body stripping, hook shapes."""

from __future__ import annotations

import contextlib
import logging
import socket as _socket
from typing import Protocol

_logger = logging.getLogger(__name__)


def _enable_tcp_keepalive(
    sock: _socket.socket,
    idle: int | None,
    interval: int | None,
    count: int | None,
) -> None:
    """Turn on OS-level TCP keepalive for an accepted connection socket.

    SO_KEEPALIVE lets the kernel reap a half-open connection that vanished
    without a FIN, which the idle timer never observes. The tuning options are
    platform-specific - Windows exposes none of them - so each is probed and
    skipped when absent, and failures are swallowed as best-effort hardening.
    """
    with contextlib.suppress(OSError):
        sock.setsockopt(_socket.SOL_SOCKET, _socket.SO_KEEPALIVE, 1)
    idle_opt = getattr(_socket, "TCP_KEEPIDLE", None)
    if idle_opt is None:
        idle_opt = getattr(_socket, "TCP_KEEPALIVE", None)
    for value, opt in (
        (idle, idle_opt),
        (interval, getattr(_socket, "TCP_KEEPINTVL", None)),
        (count, getattr(_socket, "TCP_KEEPCNT", None)),
    ):
        if value is None or opt is None:
            continue
        with contextlib.suppress(OSError):
            sock.setsockopt(_socket.IPPROTO_TCP, opt, value)


def _strip_response_body(encoded: bytes) -> bytes:
    """Return the header section of an encoded response, dropping the body.

    A HEAD response keeps the header section (with the would-be Content-Length)
    but sends no body (RFC 9110 Sec. 9.3.2). The separator is always present.
    """
    return encoded[: encoded.index(b"\r\n\r\n") + 4]


class _RequestCompleteHook(Protocol):
    def __call__(self) -> None: ...


class _KeepServingPredicate(Protocol):
    def __call__(self) -> bool: ...
