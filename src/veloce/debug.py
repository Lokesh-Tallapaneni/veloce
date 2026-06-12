"""Debug — development-mode HTML traceback page.

When an unhandled exception escapes a handler and the application is running
in debug mode, Veloce renders the traceback as a styled HTML page instead of
the plain "Internal Server Error" body. The page shows the exception type and
message, each frame's file path, line number and function name, a short
source-context window read from ``linecache``, and - for syntax errors - the
offending source line with a caret marking the failing column. Chained
exceptions, PEP 678 notes, and PEP 654 exception groups are all rendered.

Scope note: this is a *read-only* traceback viewer, not an interactive
in-browser console. It deliberately stops short of Werkzeug-style behaviour -
no evaluating shell, no frame-local inspection over the wire, no PIN
authentication, no endpoint that executes user-supplied code. Those features
turn a debug page into a remote-code-execution surface, so they are out of
scope until they can ship behind an explicit security review. The page emits no
``<form>``, no ``<input>``, no JavaScript that posts back, and makes no network
calls. The "interactive debugger" feature is therefore delivered as a navigable
HTML traceback; live evaluation remains intentionally unimplemented.

Every value interpolated into the markup - file paths, source lines, the
exception message - is escaped with ``html.escape`` so exception content cannot
inject markup (reflected XSS).
"""

from __future__ import annotations

import html
import linecache
import traceback

from veloce._internal import _BaseExceptionGroup

# Number of source lines shown on each side of the failing line.
_CONTEXT_RADIUS = 3

# Placeholder emitted when an exception's ``str()`` itself raises. Mirrors the
# stdlib ``traceback`` module so a broken ``__str__`` cannot crash the renderer.
_STR_FAILED = "<exception str() failed>"

# Separators the stdlib ``traceback`` module prints between linked exceptions.
_CAUSE_MESSAGE = "The above exception was the direct cause of the following exception:"
_CONTEXT_MESSAGE = "During handling of the above exception, another exception occurred:"


# ── String safety ─────────────────────────────────────────


def _safe_str(value: object) -> str:
    """Return ``str(value)``, falling back to a placeholder if it raises.

    A handler may surface an exception whose ``__str__`` itself raises (or
    returns a non-string). Rendering must never raise a second exception, so on
    any failure we emit the same placeholder the stdlib ``traceback`` module
    uses rather than propagating.
    """
    try:
        result = str(value)
    except Exception:
        return _STR_FAILED
    return result


# ── HTML rendering ────────────────────────────────────────


_STYLE = """\
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0;\
background:#f6f7f9;color:#1b1f23}
header{background:#7a1f2b;color:#fff;padding:1.2rem 1.6rem}
header h1{margin:0;font-size:1.1rem;font-weight:600}
header p{margin:.4rem 0 0;font-family:ui-monospace,SFMono-Regular,Menlo,\
Consolas,monospace;font-size:.95rem;white-space:pre-wrap}
main{padding:1.2rem 1.6rem}
.frame{background:#fff;border:1px solid #e1e4e8;border-radius:6px;\
margin-bottom:1rem;overflow:hidden}
.frame-head{padding:.6rem .9rem;border-bottom:1px solid #e1e4e8;\
font-size:.9rem}
.frame-head .loc{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,\
monospace}
.frame-head .func{color:#6a737d}
.src{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,\
monospace;font-size:.85rem;line-height:1.5;overflow-x:auto}
.src .line{display:flex;padding:0 .9rem;white-space:pre}
.src .line.cur{background:#fff3cd}
.src .ln{display:inline-block;min-width:3.5rem;color:#a0a6ad;\
text-align:right;padding-right:1rem;user-select:none}
.note{color:#6a737d;font-size:.8rem;padding:.4rem .9rem}
.chain{color:#6a737d;font-size:.9rem;margin:1.2rem 0 .6rem;\
font-style:italic}
.exc{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;\
font-size:.95rem;font-weight:600;margin:.6rem 0}
.exc-notes{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;\
font-size:.9rem;white-space:pre-wrap;margin:.3rem 0 .9rem;color:#6a737d}
.syntax{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;\
font-size:.85rem;white-space:pre;margin:.3rem 0 .9rem;background:#fff3cd;\
border:1px solid #ffe69c;border-radius:6px;padding:.6rem .9rem;overflow-x:auto}
.group{border-left:3px solid #7a1f2b;padding-left:1rem;margin:.6rem 0 .6rem .2rem}
.group-head{color:#6a737d;font-size:.85rem;margin:.2rem 0 .6rem}
"""


def _render_frame(filename: str, lineno: int, name: str) -> str:
    """Return the HTML fragment for a single traceback frame."""
    parts = [
        '<div class="frame"><div class="frame-head">',
        '<span class="loc">',
        html.escape(filename),
        ":",
        str(int(lineno)),
        '</span> <span class="func">in ',
        html.escape(name),
        "</span></div>",
    ]

    start = max(1, lineno - _CONTEXT_RADIUS)
    end = lineno + _CONTEXT_RADIUS
    rows: list[str] = []
    for num in range(start, end + 1):
        source = linecache.getline(filename, num)
        if not source:
            continue
        cls = "line cur" if num == lineno else "line"
        rows.append(
            f'<div class="{cls}"><span class="ln">{num}</span>'
            f"<span>{html.escape(source.rstrip(chr(10)))}</span></div>"
        )

    if rows:
        parts.append('<div class="src">')
        parts.extend(rows)
        parts.append("</div>")
    else:
        parts.append('<div class="note">source not available</div>')

    parts.append("</div>")
    return "".join(parts)


def _exc_label(exc: BaseException) -> str:
    """Return the escaped ``Type: message`` label for a single exception."""
    exc_type = html.escape(exc.__class__.__qualname__)
    message = _safe_str(exc)
    return exc_type + (": " + html.escape(message) if message else "")


def _render_syntax_caret(exc: SyntaxError) -> str:
    """Return the offending source line and a caret marker for a syntax error.

    ``SyntaxError`` (and its ``IndentationError`` / ``TabError`` subclasses)
    carry the failing source text and column on ``exc.text`` / ``exc.offset``
    rather than in a traceback frame, so ``traceback.extract_tb`` never surfaces
    them. This mirrors the stdlib ``traceback`` rendering: it strips the common
    leading whitespace, then underlines the failing column (or column span when
    ``end_offset`` is available) with carets.
    """
    text = exc.text
    if not text:
        return ""
    line = text.rstrip("\n").rstrip()
    stripped = line.lstrip()
    indent = len(line) - len(stripped)

    rows = ["<span>" + html.escape(stripped) + "</span>"]

    offset = exc.offset
    if isinstance(offset, int):
        # exc.offset is 1-based and counts from the start of the unstripped
        # line; rebase onto the leading-whitespace-stripped line we display.
        start = max(offset - 1 - indent, 0)
        end_offset = getattr(exc, "end_offset", None)
        span = end_offset - offset if isinstance(end_offset, int) and end_offset > offset else 1
        # Clamp the caret span to the visible text so a bogus offset cannot
        # produce an unbounded run of carets.
        span = max(min(span, len(stripped) - start), 1)
        caret = " " * start + "^" * span
        rows.append("<span>" + html.escape(caret) + "</span>")

    return '<div class="syntax">' + "\n".join(rows) + "</div>"


def _render_notes(exc: BaseException) -> str:
    """Return the HTML for an exception's ``__notes__`` (PEP 678), or empty."""
    notes = getattr(exc, "__notes__", None)
    if not notes:
        return ""
    text = "\n".join(_safe_str(note) for note in notes)
    return '<div class="exc-notes">' + html.escape(text) + "</div>"


# ── Exception chain ───────────────────────────────────────


def _exc_chain(exc: BaseException) -> list[tuple[BaseException, str]]:
    """Return the linked exception chain, oldest first.

    Each entry pairs an exception with the separator printed *before* it (empty
    for the oldest), matching the stdlib ``traceback`` semantics: ``__cause__``
    (explicit ``raise ... from``) wins over the implicit ``__context__``, and
    ``__suppress_context__`` hides the context link.
    """
    chain: list[tuple[BaseException, str]] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if current.__cause__ is not None:
            chain.append((current, _CAUSE_MESSAGE))
            current = current.__cause__
        elif current.__context__ is not None and not current.__suppress_context__:
            chain.append((current, _CONTEXT_MESSAGE))
            current = current.__context__
        else:
            chain.append((current, ""))
            current = None
    chain.reverse()
    return chain


def _render_exception(exc: BaseException, seen: set[int]) -> str:
    """Render one exception's label, frames, notes and any group children.

    For a ``BaseExceptionGroup`` (PEP 654), each contained exception is rendered
    with its own cause/context chain inside an indented block, so async
    ``TaskGroup`` failures surface every nested error rather than just the group
    wrapper. ``seen`` carries the set of already-rendered exception ids to guard
    against cycles shared across the cause/context/group edges.
    """
    frames = [
        _render_frame(f.filename, f.lineno or 0, f.name)
        for f in traceback.extract_tb(exc.__traceback__)
    ]
    syntax = _render_syntax_caret(exc) if isinstance(exc, SyntaxError) else ""
    parts = [
        '<div class="exc">',
        _exc_label(exc),
        "</div>",
        "".join(frames),
        syntax,
        _render_notes(exc),
    ]

    if _BaseExceptionGroup is not None and isinstance(exc, _BaseExceptionGroup):
        children = getattr(exc, "exceptions", ())
        count = len(children)
        for index, child in enumerate(children, start=1):
            head = f"sub-exception {index} of {count}:"
            parts.append('<div class="group-head">' + html.escape(head) + "</div>")
            parts.append('<div class="group">')
            parts.append(_render_chain(child, seen))
            parts.append("</div>")

    return "".join(parts)


def _render_chain(exc: BaseException, seen: set[int]) -> str:
    """Render an exception's full cause/context chain, oldest first."""
    sections: list[str] = []
    for index, (linked, separator) in enumerate(_exc_chain(exc)):
        if id(linked) in seen:
            continue
        seen.add(id(linked))
        if index > 0 and separator:
            sections.append('<div class="chain">' + html.escape(separator) + "</div>")
        sections.append(_render_exception(linked, seen))
    return "".join(sections)


# ── Public entry point ────────────────────────────────────


def render_traceback_html(exc: BaseException) -> str:
    """Render an exception and its traceback as a self-contained HTML page.

    The returned string is a complete ``text/html`` document. It is read-only:
    it contains no scripts, forms, inputs, or any affordance that evaluates
    code or contacts the server. Intended for development use only; never serve
    it from a production deployment.

    Chained exceptions (``raise ... from``, implicit context), PEP 654
    exception groups, and per-exception ``__notes__`` are preserved, matching
    the structure of the plain-text ``traceback`` output the debug response
    previously produced.
    """
    body = _render_chain(exc, set())

    head = _exc_label(exc)
    title = html.escape(exc.__class__.__qualname__)

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>" + title + "</title>"
        "<style>" + _STYLE + "</style></head><body>"
        "<header><h1>Unhandled exception</h1><p>" + head + "</p></header>"
        "<main>" + body + "</main>"
        "</body></html>"
    )
