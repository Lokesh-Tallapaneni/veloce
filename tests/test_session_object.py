"""Session container — `new` / `modified` tracking (S1)."""

from __future__ import annotations

from veloce import Request, Session, Veloce
from veloce.middleware.sessions import SessionMiddleware
from veloce.testclient import TestClient


def test_fresh_session_not_modified():
    s = Session()
    assert s.modified is False
    assert s.new is False


def test_initial_load_does_not_mark_modified():
    s = Session({"user": "alice"})
    assert s["user"] == "alice"
    assert s.modified is False


def test_setitem_marks_modified():
    s = Session()
    s["k"] = 1
    assert s.modified is True


def test_delitem_marks_modified():
    s = Session({"k": 1})
    del s["k"]
    assert s.modified is True


def test_update_marks_modified():
    s = Session()
    s.update({"a": 1})
    assert s.modified is True


def test_ior_marks_modified():
    # PEP 584 in-place merge — `dict.__ior__` runs at the C level and
    # does not route through `__setitem__`, so without an explicit
    # override the mutation would not flip `modified` and the session
    # middleware would silently skip the re-sign.
    s = Session()
    s |= {"k": "v"}
    assert s["k"] == "v"
    assert s.modified is True


def test_pop_missing_key_with_default_does_not_mark_modified():
    s = Session({"a": 1})
    assert s.pop("absent", None) is None
    assert s.modified is False


def test_pop_existing_key_marks_modified():
    s = Session({"a": 1})
    assert s.pop("a") == 1
    assert s.modified is True


def test_setdefault_new_key_marks_modified():
    s = Session()
    s.setdefault("a", 1)
    assert s.modified is True


def test_setdefault_existing_key_does_not_mark_modified():
    s = Session({"a": 1})
    s.setdefault("a", 2)
    assert s.modified is False


def test_clear_marks_modified():
    s = Session({"a": 1})
    s.clear()
    assert s.modified is True


def test_fresh_session_not_accessed():
    s = Session({"a": 1})
    assert s.accessed is False


def test_getitem_marks_accessed_not_modified():
    s = Session({"a": 1})
    assert s["a"] == 1
    assert s.accessed is True
    assert s.modified is False


def test_get_marks_accessed():
    s = Session({"a": 1})
    assert s.get("a") == 1
    assert s.accessed is True
    assert s.modified is False


def test_contains_marks_accessed():
    s = Session({"a": 1})
    assert "a" in s
    assert s.accessed is True
    assert s.modified is False


def test_iter_marks_accessed():
    s = Session({"a": 1})
    assert list(s) == ["a"]
    assert s.accessed is True
    assert s.modified is False


def test_items_marks_accessed():
    s = Session({"a": 1})
    assert list(s.items()) == [("a", 1)]
    assert s.accessed is True


def test_write_only_does_not_mark_accessed():
    s = Session()
    s["k"] = 1
    assert s.modified is True
    assert s.accessed is False


def test_middleware_marks_session_new_without_cookie():
    app = Veloce()
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)
    seen: dict = {}

    @app.get("/x")
    async def x(request: Request):
        sess = request.session
        seen["new"] = sess.new
        seen["is_session"] = isinstance(sess, Session)
        return {}

    with TestClient(app) as client:
        client.get("/x")

    assert seen == {"new": True, "is_session": True}


def test_middleware_session_not_new_with_valid_cookie():
    app = Veloce()
    app.add_middleware(SessionMiddleware, secret_key="k" * 32)
    seen: list = []

    @app.get("/set")
    async def setter(request: Request):
        request.session["count"] = 1
        return {}

    @app.get("/read")
    async def reader(request: Request):
        seen.append(request.session.new)
        return {}

    with TestClient(app) as client:
        client.get("/set")
        client.get("/read")

    # Second request carried the session cookie → not new.
    assert seen == [False]
