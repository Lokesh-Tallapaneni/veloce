"""Secret - an explicit wrapper that resists accidental plaintext disclosure.

A non-str-subclass, non-ABC wrapper around a ``str`` or ``bytes`` secret.
Unlike a ``str`` subclass it can intercept every leak path -- ``repr``,
``str``, ``format``/f-strings, and ``%`` interpolation all render
``'***'`` -- so the plaintext only escapes through the explicit
``reveal()`` method. Equality is constant-time (RFC 2104-style
``hmac.compare_digest``) and the wrapper is unhashable to keep it out of
key-based structures. JSON encoders refuse to serialise it.

Usage::

    secret = Secret("topsecret")
    print(secret)            # -> ***
    Signer(secret).dumps(x)  # accepted; unwrapped internally
    secret.reveal()          # -> "topsecret"
"""

from __future__ import annotations

import hmac
from typing import Any


class Secret:
    """Hold a str/bytes secret while resisting accidental disclosure.

    Usage::

        token = Secret(os.environ["API_TOKEN"])
        send(token.reveal())
    """

    __slots__ = ("_value",)

    def __init__(self, value: str | bytes) -> None:
        if not isinstance(value, (str, bytes)):
            raise TypeError("Secret value must be str or bytes")
        self._value = value

    def reveal(self) -> str | bytes:
        """Return the wrapped plaintext. The only way to obtain it."""
        return self._value

    def __repr__(self) -> str:
        return "Secret('***')"

    def __str__(self) -> str:
        return "***"

    def __format__(self, _spec: str) -> str:
        return "***"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            a = self._value.encode("utf-8") if isinstance(self._value, str) else self._value
            b = other._value.encode("utf-8") if isinstance(other._value, str) else other._value
            return hmac.compare_digest(a, b)
        return NotImplemented

    def __hash__(self) -> Any:
        raise TypeError("Secret is unhashable to avoid leaking into key-based structures")

    def __bool__(self) -> bool:
        return bool(self._value)
