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


# ── the same guard on every other base meant for subclassing ─────────
#
# Six bases in the tree declare abstract methods with `NotImplementedError`
# bodies. Two (`Cache`, `SessionStore`) are covered above; these are the rest.
# Each was checked against its existing subclasses before the guard was added -
# all eleven built-in converters, both shipped JSON providers, `MethodView`, and
# all four MCP registries already satisfy it.


def test_a_view_without_dispatch_request_is_refused():
    from veloce import View

    with pytest.raises(TypeError, match="dispatch_request"):

        class Bad(View):
            pass


def test_a_complete_view_is_accepted():
    from veloce import View

    class Good(View):
        async def dispatch_request(self, *args, **kwargs):
            return {}

    assert Good is not None


def test_a_method_view_subclass_is_accepted():
    """`MethodView` supplies `dispatch_request`, so a verb-only subclass is fine
    - the shape almost every user of class-based views actually writes."""
    from veloce import MethodView

    class UserView(MethodView):
        async def get(self):
            return {}

    assert UserView is not None


def test_a_converter_without_match_is_refused():
    from veloce.routing.converters import _Converter

    with pytest.raises(TypeError, match="match"):

        class Bad(_Converter):
            __slots__ = ()


def test_a_complete_converter_is_accepted():
    from veloce.routing.converters import _Converter

    class Good(_Converter):
        __slots__ = ()

        def match(self, value: str):
            return True, value

    assert Good is not None


def test_every_builtin_converter_satisfies_the_guard():
    """The negative direction: the guard must not have broken what ships."""
    from veloce.routing import converters

    built_in = [
        obj
        for obj in vars(converters).values()
        if isinstance(obj, type)
        and issubclass(obj, converters._Converter)
        and obj is not converters._Converter
    ]
    assert len(built_in) >= 10


def test_a_json_provider_missing_a_half_is_refused():
    from veloce.json_provider import JSONProvider

    with pytest.raises(TypeError, match="loads"):

        class Bad(JSONProvider):
            def dumps(self, obj, **kwargs):
                return b""


def test_a_complete_json_provider_is_accepted():
    from veloce.json_provider import JSONProvider

    class Good(JSONProvider):
        def dumps(self, obj, **kwargs):
            return b"{}"

        def loads(self, data):
            return {}

    assert Good is not None


def test_the_shipped_json_provider_satisfies_the_guard():
    """Class definition is what the guard checks; `DefaultJSONProvider` takes an
    `app`, so importing it is the assertion - a failing guard raises on import."""
    from veloce.json_provider import DefaultJSONProvider, JSONProvider

    assert issubclass(DefaultJSONProvider, JSONProvider)


def test_an_mcp_registry_missing_a_hook_is_refused():
    from veloce.contrib.mcp._registry_base import Registry

    with pytest.raises(TypeError, match="_store|_key|_duplicate_message"):

        class Bad(Registry):
            pass


def test_the_shipped_mcp_registries_satisfy_the_guard():
    from veloce.contrib.mcp.registry import ToolRegistry

    assert ToolRegistry is not None


def test_a_property_counts_as_implemented():
    """`Registry._store` is a property, not a method - the guard has to accept
    an override in whichever form the base declared."""
    from veloce._internal import _require_methods

    class Base:
        @property
        def thing(self):
            raise NotImplementedError

    class Sub(Base):
        @property
        def thing(self):
            return 1

    _require_methods(Sub, Base, ("thing",))

    class Missing(Base):
        pass

    with pytest.raises(TypeError):
        _require_methods(Missing, Base, ("thing",))
