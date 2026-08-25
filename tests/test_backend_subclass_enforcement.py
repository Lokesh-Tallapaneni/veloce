"""A backend that forgets a method is refused where it is written, not where it runs.

`Cache` and `SessionStore` are the two extension points a deployment is most
likely to implement itself - a Redis cache, a database-backed session store -
and both accepted an incomplete subclass without complaint:

    class MyCache(Cache):
        async def get(self, key): ...
        # `set` and `delete` forgotten

    MyCache()            # constructs fine
    await c.set(...)     # NotImplementedError, in production, on a write path

The base classes now check at subclass definition, which is the same moment
`RateLimitStrategy` and `RateLimitBackend` already check for `__slots__`. A
missing method is a `TypeError` naming the class and the methods, raised on
`import`.

The check runs once per subclass definition and never per request.

Abstractness is expressed with `NotImplementedError` on the base rather than
`abc.ABC`, per the project's error conventions, so the check compares against
the base's own methods rather than an ABC registry - and a subclass of a
concrete backend (`InMemoryCache`) inherits real implementations and stays
legal, which is what makes this safe to add to a shipped base class.
"""

from __future__ import annotations

import pytest

from veloce import InMemoryCache
from veloce.cache import Cache
from veloce.sessions import InMemorySessionStore, SessionStore

# ── an incomplete backend is refused at definition ───────────────────


def test_a_cache_missing_every_method_is_refused():
    """The defect: this used to construct and fail later, on a live request."""
    with pytest.raises(TypeError, match="Cache"):

        class Incomplete(Cache):
            __slots__ = ()


def test_a_cache_missing_one_method_is_refused():
    with pytest.raises(TypeError, match="delete"):

        class MissingDelete(Cache):
            __slots__ = ()

            async def get(self, key: str) -> bytes | None:
                return None

            async def set(self, key: str, value: bytes, ttl: int) -> None:
                return None


def test_the_error_names_the_class_and_the_missing_methods():
    """A contributor should not have to guess what is absent."""
    with pytest.raises(TypeError) as excinfo:

        class Sparse(Cache):
            __slots__ = ()

            async def get(self, key: str) -> bytes | None:
                return None

    message = str(excinfo.value)
    assert "Sparse" in message
    assert "set" in message and "delete" in message
    assert "get" not in message.split(":", 1)[1]


def test_a_session_store_missing_every_method_is_refused():
    with pytest.raises(TypeError, match="SessionStore|read|write|delete"):

        class Incomplete(SessionStore):
            __slots__ = ()


def test_a_session_store_missing_one_method_is_refused():
    with pytest.raises(TypeError, match="delete"):

        class MissingDelete(SessionStore):
            __slots__ = ()

            async def read(self, session_id: str) -> dict | None:
                return None

            async def write(self, session_id: str, data: dict, max_age: int) -> None:
                return None


# ── a complete backend is accepted ───────────────────────────────────


def test_a_complete_cache_is_accepted():
    class Complete(Cache):
        __slots__ = ()

        async def get(self, key: str) -> bytes | None:
            return None

        async def set(self, key: str, value: bytes, ttl: int) -> None:
            return None

        async def delete(self, key: str) -> None:
            return None

    assert Complete() is not None


def test_a_complete_session_store_is_accepted():
    class Complete(SessionStore):
        __slots__ = ()

        async def read(self, session_id: str) -> dict | None:
            return None

        async def write(self, session_id: str, data: dict, max_age: int) -> None:
            return None

        async def delete(self, session_id: str) -> None:
            return None

    assert Complete() is not None


def test_the_shipped_backends_still_define():
    """The check must not have broken what the framework itself ships."""
    assert InMemoryCache() is not None
    assert InMemorySessionStore() is not None


def test_a_subclass_of_a_concrete_backend_is_accepted():
    """Inheriting real implementations satisfies the check - the common way a
    deployment specialises a shipped backend."""

    class Tweaked(InMemoryCache):
        __slots__ = ()

    assert Tweaked() is not None


def test_a_subclass_of_a_concrete_backend_may_override_one_method():
    class LoggingCache(InMemoryCache):
        __slots__ = ()

        async def delete(self, key: str) -> None:
            await super().delete(key)

    assert LoggingCache() is not None


# ── the check does not disturb behaviour ─────────────────────────────


async def test_a_custom_cache_still_works_end_to_end():
    store: dict[str, bytes] = {}

    class DictCache(Cache):
        __slots__ = ()

        async def get(self, key: str) -> bytes | None:
            return store.get(key)

        async def set(self, key: str, value: bytes, ttl: int) -> None:
            store[key] = value

        async def delete(self, key: str) -> None:
            store.pop(key, None)

    from veloce import cached

    calls: list[int] = []

    @cached(DictCache(), ttl=60)
    async def build(n: int) -> dict:
        calls.append(n)
        return {"n": n}

    assert await build(1) == {"n": 1}
    assert await build(1) == {"n": 1}
    assert calls == [1]


def test_a_custom_session_store_still_works_end_to_end():
    from veloce import ServerSessionMiddleware, Veloce
    from veloce.testclient import TestClient

    saved: dict[str, dict] = {}

    class DictStore(SessionStore):
        __slots__ = ()

        async def read(self, session_id: str) -> dict | None:
            return saved.get(session_id)

        async def write(self, session_id: str, data: dict, max_age: int) -> None:
            saved[session_id] = data

        async def delete(self, session_id: str) -> None:
            saved.pop(session_id, None)

    app = Veloce(openapi_url=None)
    app.add_middleware(ServerSessionMiddleware(store=DictStore()))

    @app.get("/set")
    async def set_it(request) -> dict:
        request.session["seen"] = True
        return {"ok": True}

    @app.get("/get")
    async def get_it(request) -> dict:
        return {"seen": request.session.get("seen", False)}

    client = TestClient(app)
    assert client.get("/set").json() == {"ok": True}
    assert client.get("/get").json() == {"seen": True}
    assert saved


# ── the existing slots enforcement is unchanged ──────────────────────


def test_a_strategy_without_slots_is_still_refused():
    """The pattern this check follows must keep working."""
    from veloce import RateLimitStrategy

    with pytest.raises(TypeError, match="__slots__"):

        class NoSlots(RateLimitStrategy):
            pass


def test_a_backend_without_slots_is_still_refused():
    from veloce import RateLimitBackend

    with pytest.raises(TypeError, match="__slots__"):

        class NoSlots(RateLimitBackend):
            pass
