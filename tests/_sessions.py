"""Seeding a request with a modified session, for the cookie-writing tests.

The six-line "build a modified session on a synthetic request plus a response
to write onto" block was repeated verbatim ten times across the two cookie
modules. If `Session` stops exposing a settable `.modified`, or the reserved
state key is renamed, that was ten identical edits - and a missed one fails with
an unrelated-looking `AttributeError` raised inside `process_response`.
"""

from __future__ import annotations

from typing import Any

from tests.conftest import make_request
from veloce import Request, Response, Session


def seeded_session_response(
    payload: dict[str, Any], headers: dict[str, str] | None = None
) -> tuple[Request, Response]:
    """A request carrying `payload` as a modified session, and a fresh response.

    The response is the one the caller hands to `process_response`; the session
    is marked modified so the middleware writes rather than skipping.
    """
    request = make_request(path="/x", headers=headers or {})
    session = Session(payload)
    session.modified = True
    request.state["session"] = session
    return request, Response(200, b"ok")
