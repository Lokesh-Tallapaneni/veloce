"""HTML-safe string utilities - `Markup` and `escape`.

`Markup` is a `str` subclass that signals "already-escaped". Templating
engines (Jinja2) check `__html__()` to decide whether to re-escape:
plain strings get HTML-escaped on output, `Markup` instances pass
through verbatim.

`escape(value)` HTML-escapes its argument and returns a `Markup`. If
the value already implements `__html__` (e.g. a `Markup` instance or
any object claiming pre-escaped output), the result of that method is
wrapped instead.

Spec: WHATWG HTML Sec. 13 - the five HTML-significant characters
(`& < > " '`) become their named or numeric character references.
"""

from __future__ import annotations

from typing import Any


def _arg_to_safe(arg: Any) -> Any:
    if hasattr(arg, "__html__"):
        return Markup(arg)
    if isinstance(arg, str):
        return escape(arg)
    return arg


class Markup(str):
    """A string flagged as already HTML-safe.

    Equivalent to `markupsafe.Markup` for the subset Veloce's templating
    rely on. Concatenation with a non-`Markup` string escapes the
    other operand first so an injection cannot sneak in via `+`.
    """

    __slots__ = ()

    def __new__(cls, value: Any = "") -> Markup:
        if hasattr(value, "__html__"):
            value = value.__html__()
        return super().__new__(cls, value)

    def __html__(self) -> str:
        return str(self)

    def __add__(self, other: Any) -> Markup:
        if isinstance(other, str):
            return self.__class__(str.__add__(self, escape(other)))
        return NotImplemented

    def __radd__(self, other: Any) -> Markup:
        if isinstance(other, str):
            return self.__class__(str.__add__(escape(other), self))
        return NotImplemented

    def __mod__(self, args: Any) -> Markup:
        # `%` formatting escapes every interpolated value that doesn't
        # already advertise itself as safe.
        if isinstance(args, tuple):
            escaped: tuple[Any, ...] = tuple(_arg_to_safe(a) for a in args)
            return self.__class__(str.__mod__(self, escaped))
        return self.__class__(str.__mod__(self, _arg_to_safe(args)))

    def __repr__(self) -> str:
        return f"Markup({str.__repr__(self)})"


# Pre-compute the translation table once.
_HTML_ESCAPE_TABLE = str.maketrans(
    {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&#34;",
        "'": "&#39;",
    }
)


def escape(value: Any) -> Markup:
    """HTML-escape `value` and wrap in `Markup`.

    Objects that implement `__html__()` are trusted: their return is
    wrapped as-is. Otherwise the value is `str()`-coerced and the five
    HTML-significant characters are replaced with numeric character
    references (per WHATWG HTML Sec. 13).
    """
    if hasattr(value, "__html__"):
        return Markup(value.__html__())
    return Markup(str(value).translate(_HTML_ESCAPE_TABLE))
