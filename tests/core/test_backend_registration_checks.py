"""A backend that cannot do its job is refused when it is handed over.

Each extension point takes any object that satisfies its protocol, subclassing
optional. These tests pin the other half of that: the same object is refused at
registration, naming what is wrong, instead of failing on the first request that
reaches the missing method.
"""

from __future__ import annotations

import pytest

from veloce import (
    BackendError,
    CacheBackend,
    CacheError,
    FixedWindow,
    RateLimitError,
    RateLimitMiddleware,
    ServerSessionMiddleware,
    SessionError,
    Veloce,
    cached,
)
from veloce.protocols import _require_backend
from veloce.testclient import TestClient

# ── the shared check ─────────────────────────────────────────────────


def test_a_missing_method_is_named():
    class Half:
        async def get(self, key):
            return None

    with pytest.raises(TypeError, match=r"does not satisfy CacheBackend: missing set, delete"):
        _require_backend(Half(), CacheBackend, role="cache")


def test_a_synchronous_method_is_refused_where_the_protocol_is_async():
    class Blocking:
        def get(self, key):
            return None

        async def set(self, key, value, ttl):
            return None

        async def delete(self, key):
            return None

    with pytest.raises(TypeError, match=r"get must be `async def`"):
        _require_backend(Blocking(), CacheBackend, role="cache")


def test_a_satisfying_object_passes_without_inheriting_anything():
    class Duck:
        async def get(self, key):
            return None

        async def set(self, key, value, ttl):
            return None

        async def delete(self, key):
            return None

    _require_backend(Duck(), CacheBackend, role="cache")


# ── result cache ─────────────────────────────────────────────────────


def test_cached_refuses_a_bad_cache_at_decoration_not_at_the_first_call():
    class NoSet:
        async def get(self, key):
            return None

    with pytest.raises(TypeError, match="cache NoSet does not satisfy CacheBackend"):
        cached(NoSet(), ttl=10)


async def test_cached_accepts_a_duck_typed_cache():
    store: dict[str, bytes] = {}

    class Duck:
        async def get(self, key):
            return store.get(key)

        async def set(self, key, value, ttl):
            store[key] = value

        async def delete(self, key):
            store.pop(key, None)

    calls = []

    @cached(Duck(), ttl=10)
    async def compute(n: int) -> dict:
        calls.append(n)
        return {"n": n}

    assert await compute(2) == {"n": 2}
    assert await compute(2) == {"n": 2}
    assert calls == [2]


# ── server-side sessions ─────────────────────────────────────────────


def test_a_session_store_without_delete_is_refused_at_construction():
    class Partial:
        async def read(self, session_id):
            return None

        async def write(self, session_id, data, max_age):
            return None

    with pytest.raises(TypeError, match="session store Partial does not satisfy"):
        ServerSessionMiddleware(store=Partial())


def test_a_duck_typed_session_store_works_end_to_end():
    saved: dict[str, dict] = {}

    class Duck:
        async def read(self, session_id):
            return saved.get(session_id)

        async def write(self, session_id, data, max_age):
            saved[session_id] = dict(data)

        async def delete(self, session_id):
            saved.pop(session_id, None)

    app = Veloce(openapi_url=None)
    app.add_middleware(ServerSessionMiddleware(store=Duck()))

    @app.get("/set")
    async def set_it(request) -> dict:
        request.session["seen"] = True
        return {"ok": True}

    @app.get("/get")
    async def get_it(request) -> dict:
        return {"seen": request.session.get("seen", False)}

    client = TestClient(app)
    assert client.get("/set").json() == {"ok": True}
    # `replace` and `touch` are not on the duck: the middleware supplies them.
    assert client.get("/get").json() == {"seen": True}
    assert saved


# ── rate limiting ────────────────────────────────────────────────────


def test_a_rate_limit_backend_without_evaluate_is_refused():
    class Empty:
        pass

    with pytest.raises(TypeError, match="rate-limit backend Empty does not satisfy"):
        RateLimitMiddleware(strategy=FixedWindow(5), backend=Empty())


# ── JSON provider ────────────────────────────────────────────────────


def test_assigning_a_json_provider_without_loads_is_refused():
    class OnlyDumps:
        def dumps(self, obj, **kwargs):
            return b"{}"

    app = Veloce(openapi_url=None)
    with pytest.raises(TypeError, match="JSON provider OnlyDumps does not satisfy"):
        app.json = OnlyDumps()


def test_a_provider_with_dumps_and_loads_is_accepted():
    class Plain:
        def dumps(self, obj, **kwargs):
            return b"{}"

        def loads(self, data):
            return {}

    app = Veloce(openapi_url=None)
    app.json = Plain()
    assert isinstance(app.json, Plain)


# ── a refusal is its subsystem's error and a BackendError ────────────


def test_a_bad_cache_is_a_cache_error_and_a_backend_error():
    class Empty:
        pass

    with pytest.raises(CacheError) as excinfo:
        cached(Empty(), ttl=5)
    assert isinstance(excinfo.value, BackendError)
    assert isinstance(excinfo.value, TypeError)


def test_a_bad_session_store_is_a_session_error():
    class Empty:
        pass

    with pytest.raises(SessionError):
        ServerSessionMiddleware(store=Empty())


def test_a_bad_rate_limit_backend_is_a_rate_limit_error():
    class Empty:
        pass

    with pytest.raises(RateLimitError):
        RateLimitMiddleware(strategy=FixedWindow(5), backend=Empty())


def test_the_refusals_do_not_cross_subsystems():
    class Empty:
        pass

    with pytest.raises(BackendError) as excinfo:
        cached(Empty(), ttl=5)
    assert not isinstance(excinfo.value, SessionError)
    assert not isinstance(excinfo.value, RateLimitError)
