"""`Request.session` is typed as the object it returns.

It was annotated `dict[str, Any]` while always returning a `Session`, so the
framework cast around its own annotation in two places and users lost
`.permanent` / `.modified` / `.regenerate_id` from the type.
"""

from __future__ import annotations

import pathlib

from veloce import Session, Veloce
from veloce.middleware.sessions import SessionMiddleware
from veloce.testclient import TestClient

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "veloce"
_REQUEST_PY = _SRC / "http" / "request.py"
_SECURITY_SESSION_PY = _SRC / "security" / "session.py"


def test_the_session_property_is_annotated_as_a_session():
    """POSITIVE: the declared return type names what the property returns."""
    source = _REQUEST_PY.read_text(encoding="utf-8")
    assert "def session(self) -> Session:" in source, (
        "Request.session should declare the type it returns"
    )


def test_no_cast_works_around_the_session_annotation():
    """NEGATIVE: a cast here means the annotation is lying again."""
    source = _SECURITY_SESSION_PY.read_text(encoding="utf-8")
    assert 'cast("Session", request.session)' not in source, (
        "the cast exists only to work around a wrong annotation; "
        "if it is needed again the annotation regressed"
    )


def test_the_session_is_a_session_at_runtime():
    """POSITIVE: the annotation matches the object, not just the source text."""
    app = Veloce()
    app.add_middleware(SessionMiddleware(secret_key="k" * 32))
    seen: list[type] = []

    @app.get("/s")
    async def read_session(request):
        seen.append(type(request.session))
        request.session["x"] = 1
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/s").status_code == 200

    assert seen and issubclass(seen[0], Session)


def test_the_session_still_behaves_as_a_mapping():
    """NEGATIVE: widening the type must not change the mapping behaviour."""
    app = Veloce()
    app.add_middleware(SessionMiddleware(secret_key="k" * 32))

    @app.get("/s")
    async def read_session(request):
        request.session["a"] = 1
        return {"keys": list(request.session.keys()), "a": request.session.get("a")}

    with TestClient(app) as client:
        assert client.get("/s").json() == {"keys": ["a"], "a": 1}
