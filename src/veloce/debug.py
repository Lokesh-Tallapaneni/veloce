"""Development-mode HTML traceback renderer.

When an unhandled exception escapes a handler and the application is running
in debug mode, Veloce renders the traceback as a styled HTML page instead of
the plain "Internal Server Error" body. The page is strictly read-only: it
shows the exception type and message, each frame's file path, line number and
function name, and a short source-context window read from ``linecache``.

This is deliberately *not* an interactive debugger. There is no evaluating
console, no frame-local inspection over the wire, no PIN authentication and no
endpoint that executes user-supplied code. Everything that could turn the page
into a remote-code-execution surface is intentionally absent — the page emits
no ``<form>``, no ``<input>``, no JavaScript that posts back, and makes no
network calls. Anyone wanting Werkzeug-parity debugger behaviour must build it
behind an explicit security review.

Every value interpolated into the markup — file paths, source lines, the
exception message — is escaped with ``html.escape`` so exception content cannot
inject markup (reflected XSS).
"""

from __future__ import annotations

import html
import linecache
import traceback

# Number of source lines shown on each side of the failing line.
_CONTEXT_RADIUS = 3

# Placeholder emitted when an exception's ``str()`` itself raises. Mirrors the
# stdlib ``traceback`` module so a broken ``__str__`` cannot crash the renderer.
_STR_FAILED = "<exception str() failed>"

# Separators the stdlib ``traceback`` module prints between linked exceptions.
_CAUSE_MESSAGE = "The above exception was the direct cause of the following exception:"
_CONTEXT_MESSAGE = "During handling of the above exception, another exception occurred:"


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


def _render_notes(exc: BaseException) -> str:
    """Return the HTML for an exception's ``__notes__`` (PEP 678), or empty."""
    notes = getattr(exc, "__notes__", None)
    if not notes:
        return ""
    text = "\n".join(_safe_str(note) for note in notes)
    return '<div class="exc-notes">' + html.escape(text) + "</div>"


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


def _render_exception(exc: BaseException) -> str:
    """Render one exception's label, frames and notes (no chain separators)."""
    frames = [
        _render_frame(f.filename, f.lineno or 0, f.name)
        for f in traceback.extract_tb(exc.__traceback__)
    ]
    return '<div class="exc">' + _exc_label(exc) + "</div>" + "".join(frames) + _render_notes(exc)


def render_traceback_html(exc: BaseException) -> str:
    """Render an exception and its traceback as a self-contained HTML page.

    The returned string is a complete ``text/html`` document. It is read-only:
    it contains no scripts, forms, inputs, or any affordance that evaluates
    code or contacts the server. Intended for development use only; never serve
    it from a production deployment.

    Chained exceptions (``raise ... from``, implicit context) and per-exception
    ``__notes__`` are preserved, matching the plain-text ``traceback`` output
    the debug response previously produced.
    """
    chain = _exc_chain(exc)

    sections: list[str] = []
    for index, (linked, separator) in enumerate(chain):
        if index > 0 and separator:
            sections.append('<div class="chain">' + html.escape(separator) + "</div>")
        sections.append(_render_exception(linked))

    head = _exc_label(exc)
    title = html.escape(exc.__class__.__qualname__)

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>" + title + "</title>"
        "<style>" + _STYLE + "</style></head><body>"
        "<header><h1>Unhandled exception</h1><p>" + head + "</p></header>"
        "<main>" + "".join(sections) + "</main>"
        "</body></html>"
    )
