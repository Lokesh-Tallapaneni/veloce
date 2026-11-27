"""Incremental UTF-8 validation for streamed WebSocket text payloads."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    pass


class _Utf8Validator:
    """Incremental UTF-8 validator for streamed TEXT payloads.

    RFC 6455 Sec. 8.1 requires TEXT payloads to be valid UTF-8 and Sec. 5.6 lets
    a message arrive as several continuation fragments, so this remembers the
    partial multi-byte sequence straddling a fragment boundary and rejects a bad
    byte on the first offending fragment rather than at the end.

    A compact form of the Unicode UTF-8 acceptance automaton, so overlong
    encodings, surrogate halves (U+D800-U+DFFF) and values past U+10FFFF are
    rejected without decoding to `str`. `feed` returns False on the first invalid
    byte; `done` is True only at a codepoint boundary.
    """

    __slots__ = ("_need", "_lo", "_hi")

    def __init__(self) -> None:
        self._need = 0
        self._lo = 0x80
        self._hi = 0xBF

    @property
    def done(self) -> bool:
        return self._need == 0

    def feed(self, chunk: bytes | bytearray) -> bool:
        need = self._need
        lo = self._lo
        hi = self._hi
        for byte in chunk:
            if need == 0:
                if byte < 0x80:
                    continue
                if 0xC2 <= byte <= 0xDF:
                    need, lo, hi = 1, 0x80, 0xBF
                elif byte == 0xE0:
                    need, lo, hi = 2, 0xA0, 0xBF
                elif 0xE1 <= byte <= 0xEC or byte in (0xEE, 0xEF):
                    need, lo, hi = 2, 0x80, 0xBF
                elif byte == 0xED:
                    need, lo, hi = 2, 0x80, 0x9F
                elif byte == 0xF0:
                    need, lo, hi = 3, 0x90, 0xBF
                elif 0xF1 <= byte <= 0xF3:
                    need, lo, hi = 3, 0x80, 0xBF
                elif byte == 0xF4:
                    need, lo, hi = 3, 0x80, 0x8F
                else:
                    return False
            else:
                if byte < lo or byte > hi:
                    return False
                need -= 1
                lo, hi = 0x80, 0xBF
        self._need = need
        self._lo = lo
        self._hi = hi
        return True
