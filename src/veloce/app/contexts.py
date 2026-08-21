"""Outside-dispatch context managers — app/request binding and lifespan.

`_AppContext` and `_TestRequestContext` bind `current_app` / `g` / `request` for
code running outside a real request (tests, scripts, CLI). `_LifespanManager`
drives the app's startup/shutdown cycle for `async with app.lifespan_context()`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from veloce._internal import _current_app_var, _current_request_var
from veloce._protocol_constants import LIFECYCLE_SHUTDOWN, LIFECYCLE_STARTUP
from veloce.helpers import _RequestGlobals
from veloce.http.request import Request
from veloce.sessions import Session
from veloce.signals import appcontext_popped, appcontext_pushed, appcontext_tearing_down

if TYPE_CHECKING:  # pragma: no cover
    from veloce.app import Veloce


class _LifespanManager:
    """Async context manager driving the app's lifespan cycle.

    `async with app.lifespan_context(): ...` runs startup on entry and shutdown
    on exit. Re-entrant guard: a second `__aenter__` without an intervening
    `__aexit__` raises, since lifespan is once-per-app.
    """

    __slots__ = ("_app", "_entered")

    def __init__(self, app: Veloce) -> None:
        self._app = app
        self._entered = False

    async def __aenter__(self) -> Veloce:
        if self._entered:
            raise RuntimeError("lifespan_context already entered")
        self._entered = True
        await self._app._run_lifecycle(LIFECYCLE_STARTUP)
        return self._app

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self._app._run_lifecycle(LIFECYCLE_SHUTDOWN)
        self._entered = False


class _AppContext:
    """Outside-request binding for `current_app` and `g`.

    Implemented as a re-entrant context manager: nested
    `with app.app_context(): ...` blocks restore the previous binding on exit
    (via the `ContextVar` token returned by `set()`), so two apps in one process
    don't bleed into each other.
    """

    __slots__ = ("_app", "_app_token", "_g_token")

    def __init__(self, app: Veloce) -> None:
        self._app = app
        self._app_token: Any = None
        self._g_token: Any = None

    def __enter__(self) -> Veloce:
        self._app_token = _current_app_var.set(self._app)
        # Fresh `g` store - each app_context block gets its own.
        self._g_token = _RequestGlobals._ctx_var.set({})
        appcontext_pushed.send(self._app)
        return self._app

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        appcontext_tearing_down.send(self._app, exc=exc)
        if self._app_token is not None:
            _current_app_var.reset(self._app_token)
        if self._g_token is not None:
            _RequestGlobals._ctx_var.reset(self._g_token)
        appcontext_popped.send(self._app)


class _TestRequestContext:
    """Synthesises a request for tests/scripts without running dispatch.

    Inside the block: `current_app`, `g`, and `request._state` resolve. Outside:
    the bindings are unwound. No middleware, no DI, no handler - that's what
    `TestClient` is for. This is for unit tests that just need
    `current_app.config[...]` or `g.foo = ...` to work in isolation.
    """

    __slots__ = ("_app_ctx", "_request", "_request_token")

    def __init__(
        self,
        app: Veloce,
        path: str,
        method: str,
        headers: dict[str, str],
        query_string: str,
        body: bytes,
    ) -> None:
        self._app_ctx = _AppContext(app)
        self._request = Request(
            method=method,
            path=path,
            query_string=query_string,
            headers=headers,
            body=body,
        )
        self._request.app = app
        self._request_token: Any = None

    def __enter__(self) -> Request:
        self._app_ctx.__enter__()
        # Stash the synthetic request on a contextvar so user code can read it
        # via the same `current_request`-style helpers used at dispatch time.
        # Provide an in-memory `Session` so helpers that read the request's
        # session (`flash`, `get_flashed_messages`, `session` proxy) work inside
        # the block without requiring the caller to also install
        # `SessionMiddleware`. Production dispatch installs one via the
        # middleware; the context just mirrors that surface.
        if "session" not in self._request._state:
            self._request._state["session"] = Session()
        self._request_token = _current_request_var.set(self._request)
        return self._request

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._request_token is not None:
            _current_request_var.reset(self._request_token)
        self._app_ctx.__exit__(exc_type, exc, tb)
