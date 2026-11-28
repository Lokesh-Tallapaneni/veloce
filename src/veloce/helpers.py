"""Helpers — the request-scoped proxies and the response and file shortcuts.

`g`, `current_app`, `request` and `session` are the context-local proxies;
`jsonify`, `make_response`, `redirect` and `abort` build a response; `flash` and
`get_flashed_messages` carry one-shot messages in the session; `send_file` and
`send_from_directory` serve a path; and `url_for`, `stream_with_context` and
`after_this_request` are the remaining per-request conveniences.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Sequence
from http import HTTPStatus
from typing import Any, NoReturn

from veloce._helpers_files import (
    _attachment_name as _attachment_name,
)
from veloce._helpers_files import (
    _range_negotiable_request as _range_negotiable_request,
)
from veloce._helpers_files import (
    async_send_file as async_send_file,
)
from veloce._helpers_files import (
    send_file as send_file,
)
from veloce._helpers_files import (
    send_from_directory as send_from_directory,
)
from veloce._helpers_files import (
    send_from_directory_async as send_from_directory_async,
)
from veloce._internal import (
    MIME_HTML,
    _current_app_var,
    _current_request_var,
    _unpack_response_tuple,
)
from veloce.exceptions import exception_for_status
from veloce.http.response import JSONResponse, RedirectResponse, Response
from veloce.signals import message_flashed
from veloce.status import HTTP_200_OK, HTTP_302_FOUND

# ── Proxy classes ─────────────────────────────────────────


class _ContextProxy:
    """Shared base for ContextVar-backed proxies.

    Subclasses set `_var` (the ContextVar) and `_subject` (the name used in the
    unbound error message), and override `_resolve` when the bound value is not
    the final target.
    """

    __slots__ = ()

    _var: contextvars.ContextVar[Any]
    _subject: str
    _scope_label: str = "request"

    def _resolve(self) -> Any:
        value = self._var.get()
        if value is None:
            raise RuntimeError(
                f"Working outside of {self._scope_label} context. "
                f"`{self._subject}` is only available while a request is being handled."
            )
        return value

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return super().__getattribute__(name)
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:
        value = self._var.get()
        if value is None:
            return f"<{self._subject}: unbound>"
        return f"<{self._subject}: {value!r}>"

    def __bool__(self) -> bool:
        return self._var.get() is not None


class _CurrentAppProxy(_ContextProxy):
    """Context-local proxy to the currently-handling Veloce app.

    Accessing any attribute outside a request raises `RuntimeError`.
    """

    __slots__ = ()
    _var = _current_app_var
    _subject = "current_app"
    _scope_label = "application"


class _CurrentRequestProxy(_ContextProxy):
    """Context-local proxy to the request being handled.

    Resolves to the `Request` bound by the dispatcher, or by
    `app.test_request_context()`; outside one, raises `RuntimeError`.
    """

    __slots__ = ()
    _var = _current_request_var
    _subject = "request"


class _SessionProxy(_ContextProxy):
    """Context-local proxy to the current request's session.

    Resolves to `request.session`. Outside a request context, or without
    `SessionMiddleware` installed, raises `RuntimeError`.
    """

    __slots__ = ()
    _var = _current_request_var
    _subject = "session"

    def _resolve(self) -> Any:
        req = self._var.get()
        if req is None:
            raise RuntimeError(
                "Working outside of request context. `session` is only "
                "available while a request is being handled."
            )
        return req.session

    def __setattr__(self, name: str, value: Any) -> None:
        # Writes forward like reads, or a `Session` property such as `permanent`
        # raises `AttributeError` on this slotted proxy. Underscore names are ours.
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._resolve(), name, value)

    def __getitem__(self, key: str) -> Any:
        return self._resolve()[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._resolve()[key] = value

    def __delitem__(self, key: str) -> None:
        del self._resolve()[key]

    def __contains__(self, key: str) -> bool:
        return key in self._resolve()

    def __iter__(self) -> Any:
        return iter(self._resolve())

    def __len__(self) -> int:
        return len(self._resolve())

    def __bool__(self) -> bool:
        req = self._var.get()
        return req is not None and "session" in req._state


class _RequestGlobals:
    """Request-scoped global object - the `g` namespace.

    Uses contextvars so it works in async without thread-local hacks.
    """

    _ctx_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
        "veloce_g", default=None
    )

    def _get_store(self) -> dict[str, Any]:
        store = self._ctx_var.get()
        if store is None:
            store = {}
            self._ctx_var.set(store)
        return store

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return super().__getattribute__(name)
        store = self._get_store()
        try:
            return store[name]
        except KeyError as err:
            raise AttributeError(f"'g' has no attribute '{name}'") from err

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self._get_store()[name] = value

    def __delattr__(self, name: str) -> None:
        store = self._get_store()
        try:
            del store[name]
        except KeyError as err:
            raise AttributeError(f"'g' has no attribute '{name}'") from err

    def __contains__(self, name: str) -> bool:
        return name in self._get_store()

    def get(self, name: str, default: Any = None) -> Any:
        return self._get_store().get(name, default)

    def pop(self, name: str, *args: Any) -> Any:
        return self._get_store().pop(name, *args)

    def setdefault(self, name: str, default: Any = None) -> Any:
        return self._get_store().setdefault(name, default)

    def _reset(self) -> None:
        """Bind a fresh store for a new request.

        Eagerly, not on first access: a sync handler runs in a copied context,
        so a lazy `set()` inside it would land in the copy and be discarded.
        Binding here makes every write a mutation of a shared object.
        """
        self._ctx_var.set({})

    def _snapshot(self) -> dict[str, Any] | None:
        """Return the bound store so a nested binding can hand it back."""
        return self._ctx_var.get()

    def _restore(self, store: dict[str, Any] | None) -> None:
        """Rebind a store captured by `_snapshot`."""
        self._ctx_var.set(store)


# ── Aborter ───────────────────────────────────────────────


def _default_detail(code: int) -> str:
    """Resolve the reason-phrase fallback used when no detail is supplied."""
    try:
        return HTTPStatus(code).phrase
    except ValueError:
        return "Error"


class Aborter:
    """A callable that turns a status code into an HTTPException.

    Used as `app.aborter(404)` or `app.aborter(403, "Forbidden")`. Subclasses
    override `mapping` to register custom exception classes per status code.
    """

    mapping: dict[int, type[Any]] = {}

    def __init__(self, extra_mapping: dict[int, type[Any]] | None = None) -> None:
        self._mapping: dict[int, type[Any]] = {}
        if extra_mapping:
            self._mapping.update(extra_mapping)

    def __call__(
        self,
        code: int,
        detail: str = "",
        headers: dict[str, str] | None = None,
    ) -> NoReturn:
        """Raise the exception registered for `code`.

        Args:
            code: HTTP status code to abort with.
            detail: Message for the error body; defaults to the status phrase.
            headers: Extra headers to send with the error response.

        Raises:
            HTTPException: Always - the class registered for `code` on this
                aborter, then on `mapping`, then the stock class for the status.

        """  # noqa: DOC503 - the exception is raised or caught by a callee
        if not detail:
            detail = _default_detail(code)
        cls = self._mapping.get(code) or self.mapping.get(code) or exception_for_status(code)
        raise cls(status_code=code, detail=detail, headers=headers)


# ── Context introspection ─────────────────────────────────


def has_app_context() -> bool:
    """True iff `current_app` resolves to a real app.

    Use it to gate code that reads `current_app` / `app.config` so it can also
    run outside a request.
    """
    return _current_app_var.get() is not None


def has_request_context() -> bool:
    """True iff a request is bound to this task/context.

    The dispatcher binds it on every request, so this is True inside any handler,
    middleware, hook or template render, as well as inside an
    `app.test_request_context()` block. It is False outside a request - in
    startup and shutdown handlers, a background task, or a CLI command.
    """
    return _current_request_var.get() is not None


# ── abort() ───────────────────────────────────────────────


#: The aborter `abort()` uses when no application is bound. It registers no
#: custom mapping, so it resolves exactly what `exception_for_status` does.
_default_aborter = Aborter()


def abort(status_code: int, detail: str = "", headers: dict[str, str] | None = None) -> NoReturn:
    """Raise an HTTPException - a concise shorthand.

    Raises the typed subclass for known status codes so a handler registered
    against that subclass matches; unknown codes fall back to `HTTPException`.
    Inside an application context the call goes through `app.aborter`, so a
    custom code-to-exception mapping applies here too.

    Usage::

        abort(404)              # -> raises NotFound
        abort(403, "Forbidden") # -> raises Forbidden

    Args:
        status_code: HTTP status code to abort with.
        detail: Message for the error body; defaults to the status phrase.
        headers: Extra headers to send with the error response.

    Raises:
        HTTPException: Always - the typed subclass for `status_code`.

    """  # noqa: DOC502 - the exception is raised or caught by a callee
    app = _current_app_var.get()
    aborter: Aborter = _default_aborter if app is None else app.aborter
    aborter(status_code, detail, headers)


# ── after_this_request() ──────────────────────────────────


def after_this_request(func: Any) -> Any:
    """Register a one-shot after-request callback.

    Fires after the global `@app.after_request` hooks, for the current request
    only. Returns the callback unchanged so it can be used as a decorator, and
    raises `RuntimeError` outside an active request.
    """
    request = _current_request_var.get()
    if request is None:
        raise RuntimeError("after_this_request() requires an active request context.")
    # List, not set: the dispatcher drains in registration order.
    cbs = request._state.setdefault("_after_this_request", [])
    cbs.append(func)
    return func


# ── redirect() ────────────────────────────────────────────


def redirect(
    location: str,
    code: int = HTTP_302_FOUND,
    headers: dict[str, str] | None = None,
) -> Response:
    """Build a redirect response helper.

    RFC 9110 Sec. 15.4 catalogue: 301 (permanent, method may change), 302
    (found, method may change), 303 (see other, method becomes GET), 307
    (temporary, method preserved), 308 (permanent, method preserved).

    Args:
        location: Target URL, sent in the `Location` header.
        code: Redirect status code.
        headers: Extra headers to send with the response.

    Returns:
        A `RedirectResponse` to `location`.

    """
    return RedirectResponse(location, status_code=code, headers=headers)


# ── url_for() ─────────────────────────────────────────────


def url_for(endpoint: str, /, **path_params: Any) -> str:
    """Build the URL for a named route on the active app.

    The module-level form of `current_app.url_for`. The endpoint is
    positional-only, so a route may have an `{endpoint}` or `{name}` segment and
    still be reversed. Raises `RuntimeError` outside an application context.
    """
    app = _current_app_var.get()
    if app is None:
        raise RuntimeError(
            "url_for() requires an active application context - "
            "call it inside a request, or use `app.url_for(...)` directly."
        )
    return app.url_for(endpoint, **path_params)


# ── jsonify() ─────────────────────────────────────────────


def jsonify(*args: Any, **kwargs: Any) -> JSONResponse:
    """Create a JSON response - a concise shorthand.

    Honours two app-config flags, which reach every JSON response and not only
    this helper: `JSON_SORT_KEYS` sorts dict keys, and
    `JSONIFY_PRETTYPRINT_REGULAR` indents the output with 2 spaces.

    Usage::

        return jsonify(name="alice", age=30)
        return jsonify({"name": "alice"})
        return jsonify([1, 2, 3])
    """
    if args and kwargs:
        raise TypeError("jsonify() takes either positional or keyword args, not both")
    data = (args[0] if len(args) == 1 else list(args)) if args else kwargs

    # Inside a request, serialise through the app's JSON provider so this and
    # `app.json.dumps()` share one source of truth.
    app = _current_app_var.get()
    if app is not None:
        return app.json.response(data)
    return JSONResponse(data)


# ── make_response() ───────────────────────────────────────


def _apply_to_response(
    response: Response, status_code: int | None, headers: dict[str, str] | None
) -> Response:
    """Overlay an explicit status and headers onto an existing response.

    Shared by the two spellings that reach `make_response` carrying a
    `Response`: `make_response(resp, 403)` and `make_response((resp, 403))`.
    """
    if status_code is not None:
        response.status_code = status_code
    if headers:
        response.headers.update(headers)
    response._encoded = None
    return response


def make_response(
    body: Any = b"",
    status_code: int | None = None,
    headers: dict[str, str] | None = None,
    content_type: str | None = None,
) -> Response:
    """Create a Response - a convenience wrapper.

    A tuple body is unpacked as `(body, status)` / `(body, status, headers)` /
    `(body, headers)`, the same shapes a handler may return; any other length is
    answered as a plain value. An omitted `status_code` means 200 for a plain
    body and leaves an existing `Response`'s own status alone, which is why the
    default is `None` rather than `200`.

    Usage::

        resp = make_response("Hello", 200)
        resp = make_response({"data": True}, 201)
        resp = make_response((b"raw", 201))
        resp = make_response(jsonify({"error": "forbidden"}), 403)

    Args:
        body: The value to wrap: a `Response`, a `dict`/`list` (JSON), text or
            bytes, or a tuple in one of the handler return shapes.
        status_code: Status for the response; `None` keeps an existing
            `Response`'s own status and means 200 otherwise.
        headers: Headers to set on the response.
        content_type: Content type for text or bytes bodies; `text/html` when
            omitted.

    Returns:
        The response.

    """
    if isinstance(body, tuple):
        unpacked = _unpack_response_tuple(body)
        if unpacked is not None:
            inner, code, tuple_headers = unpacked
            if code is not None:
                status_code = code
            if tuple_headers is not None:
                headers = tuple_headers
            # As dispatch applies them to a handler's `return resp, 404`.
            if isinstance(inner, Response):
                return _apply_to_response(inner, status_code, headers)
            return make_response(inner, status_code, headers, content_type)
    # A `Response` is returned as itself, carrying any status and headers passed
    # beside it; without this branch it would be JSON-encoded via its `__str__`.
    if isinstance(body, Response):
        return _apply_to_response(body, status_code, headers)
    if status_code is None:
        status_code = HTTP_200_OK
    if isinstance(body, (dict, list)):
        return JSONResponse(body, status_code=status_code, headers=headers)
    if isinstance(body, (str, bytes)):
        # `text/html` for both, matching `Veloce.make_response` and dispatch.
        ct = content_type or MIME_HTML
        return Response(
            status_code=status_code,
            body=body.encode("utf-8") if isinstance(body, str) else body,
            content_type=ct,
            headers=headers,
        )
    # Duck-typed, so this module needs no pydantic import.
    model_dump = getattr(body, "model_dump", None)
    if model_dump is not None:
        return JSONResponse(model_dump(), status_code=status_code, headers=headers)
    return JSONResponse(body, status_code=status_code, headers=headers)


# ── flash() / get_flashed_messages() ──────────────────────


def _flash_store() -> Any:
    """Resolve the session dict the flash queue lives in.

    The session, not `g`: a flash has to survive the POST/redirect/GET
    round-trip, and the redirected GET sees a fresh `g`.
    """
    req = _current_request_var.get()
    if req is None or "session" not in req._state:
        raise RuntimeError(
            "flash() requires an active request with SessionMiddleware "
            "(or ServerSessionMiddleware) installed - the flashes are "
            "carried across requests via the session, not the per-request `g`."
        )
    return req._state["session"]


def flash(message: str, category: str = "message") -> None:
    """Flash a message for the next request - requires SessionMiddleware.

    Usage::

        flash("Item created successfully")
        flash("Invalid input", "error")

    Args:
        message: The text to show on the next request.
        category: A label such as `"error"` or `"info"` to filter on.

    Raises:
        RuntimeError: Outside a request, or when no session middleware is
            installed.

    """  # noqa: DOC502 - the exception is raised or caught by a callee
    store = _flash_store()
    flashes = store.setdefault("_flashes", [])
    flashes.append((category, message))
    # `flashes.append(...)` mutates an existing list, which the Session cannot
    # see, so the cookie would not be re-signed on a key already present.
    with contextlib.suppress(AttributeError):
        store.modified = True
    message_flashed.send(_current_app_var.get(), message=message, category=category)


def get_flashed_messages(
    *, with_categories: bool = False, category_filter: Sequence[str] | None = None
) -> list[str] | list[tuple[str, str]]:
    """Get flashed messages - call in templates.

    Usage::

        messages = get_flashed_messages()
        messages = get_flashed_messages(with_categories=True)

    Args:
        with_categories: Return `(category, message)` pairs instead of bare
            messages.
        category_filter: Keep only messages in these categories.

    Returns:
        The queued messages, which are consumed; empty outside a request.

    """
    req = _current_request_var.get()
    # Empty rather than raising, so a template render outside a request works.
    if req is None or "session" not in req._state:
        return []
    store = req._state["session"]
    # `Session.pop` already flips `.modified` when the key existed.
    flashes = store.pop("_flashes", [])
    if category_filter:
        allowed = set(category_filter)
        flashes = [(cat, msg) for cat, msg in flashes if cat in allowed]
    if with_categories:
        return flashes
    return [msg for _, msg in flashes]


# ── stream_with_context() ─────────────────────────────────


def stream_with_context(generator: Any) -> Any:
    """Keep the request context alive while a streaming generator runs.

    A streaming body is consumed *after* the handler returns, by which point the
    request context has been torn down. The current request / app / `g` snapshot
    is captured now and re-established for the wrapped iteration, which may be
    an async or a synchronous generator::

        return StreamingResponse(stream_with_context(generate()))
    """
    captured_app = _current_app_var.get()
    captured_req = _current_request_var.get()
    captured_g = _RequestGlobals._ctx_var.get()

    async def _ctx_keeping() -> Any:
        tok_app = _current_app_var.set(captured_app)
        tok_req = _current_request_var.set(captured_req)
        tok_g = _RequestGlobals._ctx_var.set(captured_g)
        try:
            if hasattr(generator, "__aiter__"):
                async for chunk in generator:
                    yield chunk
            else:
                for chunk in generator:
                    yield chunk
        finally:
            _RequestGlobals._ctx_var.reset(tok_g)
            _current_request_var.reset(tok_req)
            _current_app_var.reset(tok_app)

    return _ctx_keeping()


# ── Singletons ────────────────────────────────────────────

current_app = _CurrentAppProxy()

request = _CurrentRequestProxy()

session = _SessionProxy()

g = _RequestGlobals()
