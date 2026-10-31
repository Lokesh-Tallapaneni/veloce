"""The library-named JSON response classes warn once and keep working.

`ORJSONResponse` and `UJSONResponse` are deprecated because Veloce had adopted
two mechanisms that were never meant to coexist: library-named response classes
and a `JSONProvider`. A class that owns its library has to bypass the provider,
because a provider's `dumps` transforms and encodes in one call - so
`UJSONResponse` silently drops an application's envelope, which this module
pins so the reason for the deprecation does not get lost.

The warning is raised from the package `__getattr__`, which caches into
`globals()`. It therefore fires once per process on first import and costs a
response nothing.
"""

from __future__ import annotations

import importlib
import warnings
from typing import Any

import pytest

import veloce
import veloce.http
from veloce import (
    DefaultJSONProvider,
    TestClient,
    Veloce,
    VeloceDeprecationWarning,
)

DEPRECATED = ("ORJSONResponse", "UJSONResponse")


@pytest.fixture
def fresh_veloce():
    """A package whose `__getattr__` cache has not yet resolved the aliases.

    The names are cached into module globals on first access, so a test that
    asserts the warning has to clear them first or the second test sees nothing.
    """
    saved = {m: {n: m.__dict__.pop(n, None) for n in DEPRECATED} for m in (veloce, veloce.http)}
    yield veloce
    for module, names in saved.items():
        for name, value in names.items():
            if value is not None:
                module.__dict__[name] = value


@pytest.mark.parametrize("name", DEPRECATED)
def test_importing_the_alias_warns(name, fresh_veloce):
    with pytest.warns(VeloceDeprecationWarning, match=f"`{name}` is deprecated"):
        getattr(fresh_veloce, name)


@pytest.mark.parametrize("name", DEPRECATED)
def test_the_warning_names_the_replacement(name, fresh_veloce):
    with pytest.warns(VeloceDeprecationWarning, match="use_json_backend"):
        getattr(fresh_veloce, name)


@pytest.mark.parametrize("name", DEPRECATED)
def test_the_warning_fires_once_per_process(name, fresh_veloce):
    with pytest.warns(VeloceDeprecationWarning):
        getattr(fresh_veloce, name)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        getattr(fresh_veloce, name)
    assert caught == []


@pytest.mark.parametrize("name", DEPRECATED)
def test_the_subpackage_gateway_warns_too(name, fresh_veloce):
    """`from veloce.http import ORJSONResponse` must not slip past the notice."""
    module = importlib.import_module("veloce.http")
    with pytest.warns(VeloceDeprecationWarning):
        getattr(module, name)


@pytest.mark.parametrize("name", DEPRECATED)
def test_the_alias_stays_exported(name):
    assert name in veloce.__all__
    assert name in veloce.http.__all__


def test_a_non_deprecated_lazy_export_does_not_warn(fresh_veloce):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert fresh_veloce.LiveServer is not None
    assert caught == []


def test_an_unknown_attribute_still_raises_attribute_error():
    with pytest.raises(AttributeError):
        veloce.NoSuchName  # noqa: B018
    with pytest.raises(AttributeError):
        veloce.http.NoSuchName  # noqa: B018


# ── the behaviour the deprecation exists to retire ───────────────────


class _Envelope(DefaultJSONProvider):
    def dumps(self, obj: Any, **kwargs: Any) -> bytes:
        return super().dumps({"data": obj, "ok": True}, **kwargs)


def test_the_deprecated_aliases_still_serve_responses():
    """Deprecated is not removed; existing applications keep working."""
    # Deferred: importing at module top resolves the alias at collection,
    # so the warning would already have fired before any test runs.
    from veloce import ORJSONResponse, UJSONResponse

    app = Veloce()

    @app.get("/orjson", response_class=ORJSONResponse)
    async def orjson_route(request):  # noqa: ANN001, ANN202
        return {"v": 1}

    @app.get("/ujson", response_class=UJSONResponse)
    async def ujson_route(request):  # noqa: ANN001, ANN202
        return {"v": 1}

    with TestClient(app) as client:
        assert client.get("/orjson").json() == {"v": 1}
        assert client.get("/ujson").json() == {"v": 1}


def test_ujson_response_drops_the_provider_and_orjson_response_does_not():
    """Pins the asymmetry the deprecation retires, rather than leaving it folklore.

    A class that owns its library cannot also honour a provider, so
    `UJSONResponse` loses the envelope while the alias keeps it.
    """
    # Deferred: importing at module top resolves the alias at collection,
    # so the warning would already have fired before any test runs.
    from veloce import ORJSONResponse, UJSONResponse

    app = Veloce()
    app.json_provider_class = _Envelope

    @app.get("/alias", response_class=ORJSONResponse)
    async def alias(request):  # noqa: ANN001, ANN202
        return {"v": 1}

    @app.get("/bound", response_class=UJSONResponse)
    async def bound(request):  # noqa: ANN001, ANN202
        return {"v": 1}

    with TestClient(app) as client:
        assert client.get("/alias").json() == {"data": {"v": 1}, "ok": True}
        assert client.get("/bound").json() == {"v": 1}
