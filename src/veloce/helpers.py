"""Helper functions — abort, jsonify, make_response, flash, g,
current_app, send_from_directory."""

from __future__ import annotations

import contextvars
from typing import Any, NoReturn

from veloce.http.response import FileResponse, JSONResponse, Response

# ── current_app proxy ────────────────────────────────────────────────

# The active app is stashed on this ContextVar by `Veloce.handle_request`.
# `current_app` is a proxy that resolves to the active app on every
# attribute access — so `current_app.config["DEBUG"]` works from any
# coroutine/task spawned during request handling.
_current_app_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "veloce_current_app", default=None
)

# Outside-request stash for `app.test_request_context()` — the dispatch
# pipeline passes the live request through arguments, so this is only
# consulted by code that explicitly enters a test_request_context block.
_current_request_var: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "veloce_current_request", default=None
)


class _CurrentAppProxy:
    """Context-local proxy to the currently-handling Veloce app.

    Resolves to the app set by `Veloce.handle_request` for the duration
    of the request. Accessing any attribute outside that scope raises
    `RuntimeError`.
    """

    __slots__ = ()

    def _resolve(self) -> Any:
        app = _current_app_var.get()
        if app is None:
            raise RuntimeError(
                "Working outside of application context. "
                "`current_app` is only available while a request is being handled."
            )
        return app

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            # ContextVar / dunder lookups never resolve through the proxy.
            return super().__getattribute__(name)
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:
        app = _current_app_var.get()
        if app is None:
            return "<current_app: unbound>"
        return f"<current_app: {app!r}>"

    def __bool__(self) -> bool:
        return _current_app_var.get() is not None


# Singleton — `from veloce import current_app`.
current_app = _CurrentAppProxy()


class _CurrentRequestProxy:
    """Context-local proxy to the request being handled.

    Resolves to the `Request` bound by the dispatcher (or by
    `app.test_request_context()`) for the duration of the request.
    Accessing any attribute outside that scope raises `RuntimeError`.
    """

    __slots__ = ()

    def _resolve(self) -> Any:
        req = _current_request_var.get()
        if req is None:
            raise RuntimeError(
                "Working outside of request context. `request` is only "
                "available while a request is being handled."
            )
        return req

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return super().__getattribute__(name)
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:
        req = _current_request_var.get()
        return "<request: unbound>" if req is None else f"<request: {req!r}>"

    def __bool__(self) -> bool:
        return _current_request_var.get() is not None


# Singleton — `from veloce import request`.
request = _CurrentRequestProxy()


class _SessionProxy:
    """Context-local proxy to the current request's session.

    Resolves to `request.session` (the dict `SessionMiddleware`
    maintains). Accessing it outside a request context, or without
    `SessionMiddleware` installed, raises `RuntimeError` — the same
    failure modes as `request` and `Request.session`.
    """

    __slots__ = ()

    def _resolve(self) -> Any:
        req = _current_request_var.get()
        if req is None:
            raise RuntimeError(
                "Working outside of request context. `session` is only "
                "available while a request is being handled."
            )
        return req.session

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            return super().__getattribute__(name)
        return getattr(self._resolve(), name)

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
        req = _current_request_var.get()
        return req is not None and "session" in req._state


# Singleton — `from veloce import session`.
session = _SessionProxy()


def has_app_context() -> bool:
    """True iff `current_app` resolves to a real app.

    Use this to gate code that reads `current_app`/`app.config` so it
    can also run outside a request (e.g. helper modules imported at
    module-import time, before any app is bound to the contextvar).
    """
    return _current_app_var.get() is not None


def has_request_context() -> bool:
    """True iff a request is bound to this task/context.

    Veloce passes the live request through arguments during dispatch,
    so this only flips True inside `app.test_request_context()` blocks
    or when application code explicitly sets the contextvar.
    """
    return _current_request_var.get() is not None


# ── abort() ──────────────────────────────────────────────────────


def abort(status_code: int, detail: str = "", headers: dict[str, str] | None = None) -> NoReturn:
    """Raise an HTTPException — a concise shorthand.

    Raises the typed subclass for known status codes (e.g. `NotFound` for 404,
    `Forbidden` for 403) so error handlers registered against a specific
    subclass match. Unknown codes fall back to the bare `HTTPException`.

    Usage:
        abort(404)              # → raises NotFound
        abort(403, "Forbidden") # → raises Forbidden
    """
    from veloce.exceptions import exception_for_status

    if not detail:
        from http import HTTPStatus

        try:
            detail = HTTPStatus(status_code).phrase
        except ValueError:
            detail = "Error"
    cls = exception_for_status(status_code)
    raise cls(status_code=status_code, detail=detail, headers=headers)


# ── after_this_request() ────────────────────────────────────────


def after_this_request(func: Any) -> Any:
    """Register a one-shot after-request callback.

    Fires after the global `@app.after_request` hooks have run for the
    current request only — future requests are unaffected. Useful for
    work that depends on data computed inside the handler (e.g. setting
    a cookie whose value the handler decided).

    Returns the callback unchanged so it can be used as a decorator.
    Raises `RuntimeError` when called outside an active request.
    """
    request = _current_request_var.get()
    if request is None:
        raise RuntimeError("after_this_request() requires an active request context.")
    # List, not set: order matters — dispatcher drains in registration order.
    cbs = request._state.setdefault("_after_this_request", [])
    cbs.append(func)
    return func


# ── send_file() ─────────────────────────────────────────────────


def send_file(
    path_or_file: Any,
    mimetype: str | None = None,
    as_attachment: bool = False,
    download_name: str | None = None,
    last_modified: Any = None,
    etag: bool | str = True,
    max_age: int | None = None,
) -> Response:
    """Serve a file top-level helper.

    Accepts a filesystem path (str / PathLike) and returns a `FileResponse`
    with conditional-GET headers already set (Last-Modified, ETag — both
    were added by Q40/Q42). Optional knobs:

    - `mimetype=` overrides the auto-guessed content type.
    - `as_attachment=True` sets `Content-Disposition: attachment;
      filename=<download_name or basename>`.
    - `download_name=` overrides the filename in `Content-Disposition`.
    - `last_modified=` overrides the file's mtime (datetime, unix ts,
      or pre-formatted IMF-fixdate string).
    - `etag=False` suppresses the auto-generated ETag; `etag="<value>"`
      uses the caller-provided one verbatim (already-quoted).
    - `max_age=` adds `Cache-Control: public, max-age=<n>`.
    """
    from veloce.http.response import FileResponse

    headers: dict[str, str] = {}
    if last_modified is not None:
        import datetime as _dt
        from email.utils import formatdate

        if isinstance(last_modified, _dt.datetime):
            if last_modified.tzinfo is None:
                last_modified = last_modified.replace(tzinfo=_dt.timezone.utc)
            headers["Last-Modified"] = formatdate(last_modified.timestamp(), usegmt=True)
        elif isinstance(last_modified, (int, float)):
            headers["Last-Modified"] = formatdate(float(last_modified), usegmt=True)
        else:
            headers["Last-Modified"] = str(last_modified)

    if etag is False:
        # Sentinel — FileResponse won't override an already-set ETag, so
        # write a placeholder we then pop.
        headers["ETag"] = "_strip"
    elif isinstance(etag, str):
        headers["ETag"] = etag

    if max_age is not None:
        headers["Cache-Control"] = f"public, max-age={max_age}"

    path = str(path_or_file)
    attachment_name = download_name if as_attachment else None
    resp = FileResponse(
        path=path,
        filename=attachment_name or (download_name if as_attachment else None),
        content_type=mimetype,
        headers=headers,
    )
    if headers.get("ETag") == "_strip":
        resp.headers.pop("ETag", None)
        resp._encoded = None
    return resp


# ── redirect() ──────────────────────────────────────────────────


def redirect(
    location: str,
    code: int = 302,
    headers: dict[str, str] | None = None,
) -> Response:
    """Build a redirect response helper.

    Default `code=302` matches the long-standing convention. RFC 9110 §15.4
    catalogue: 301 (permanent, method may change), 302 (found, method
    may change), 303 (see other, method becomes GET), 307 (temporary,
    method preserved), 308 (permanent, method preserved). Pick the one
    that matches your semantics — the helper is a thin wrapper, not a
    policy. Accepts extra headers (e.g. `Vary`).
    """
    from veloce.http.response import RedirectResponse

    return RedirectResponse(location, status_code=code, headers=headers)


# ── jsonify() ────────────────────────────────────────────────────


def jsonify(*args: Any, **kwargs: Any) -> JSONResponse:
    """Create a JSON response — a concise shorthand.

    Honours two app-config flags when called inside a request:
    - `JSON_SORT_KEYS` (default False) — sort dict keys alphabetically.
    - `JSONIFY_PRETTYPRINT_REGULAR` (default False) — indent the output
      with 2 spaces for readability. Often enabled under DEBUG.

    Usage:
        return jsonify(name="alice", age=30)
        return jsonify({"name": "alice"})
        return jsonify([1, 2, 3])
    """
    if args and kwargs:
        raise TypeError("jsonify() takes either positional or keyword args, not both")
    data = (args[0] if len(args) == 1 else list(args)) if args else kwargs

    # Try to read flags from the current app's config; fall back to the
    # plain defaults when called outside a request context.
    import orjson

    options = 0
    app = _current_app_var.get()
    if app is not None:
        cfg = app.config
        if cfg.get("JSON_SORT_KEYS"):
            options |= orjson.OPT_SORT_KEYS
        if cfg.get("JSONIFY_PRETTYPRINT_REGULAR"):
            options |= orjson.OPT_INDENT_2

    if options:
        # Use pre-encoded bytes so the orjson options apply. JSONResponse's
        # default ctor re-serialises through orjson; we sidestep that.
        body = orjson.dumps(data, option=options)
        resp = JSONResponse.__new__(JSONResponse)
        Response.__init__(
            resp,
            status_code=200,
            body=body,
            content_type="application/json",
            headers=None,
        )
        return resp
    return JSONResponse(data)


# ── make_response() ─────────────────────────────────────────────


def make_response(
    body: Any = b"",
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    content_type: str | None = None,
) -> Response:
    """Create a Response — a convenience wrapper.

    Usage:
        resp = make_response("Hello", 200)
        resp = make_response({"data": True}, 201)
    """
    if isinstance(body, (dict, list)):
        return JSONResponse(body, status_code=status_code, headers=headers)
    if isinstance(body, str):
        ct = content_type or "text/html; charset=utf-8"
        return Response(
            status_code=status_code,
            body=body.encode("utf-8"),
            content_type=ct,
            headers=headers,
        )
    if isinstance(body, bytes):
        ct = content_type or "application/octet-stream"
        return Response(
            status_code=status_code,
            body=body,
            content_type=ct,
            headers=headers,
        )
    # Pydantic model
    if hasattr(body, "model_dump"):
        return JSONResponse(body.model_dump(), status_code=status_code, headers=headers)
    return JSONResponse(body, status_code=status_code, headers=headers)


# ── send_from_directory() ────────────────────────────────────────


def send_from_directory(
    directory: str,
    filename: str,
    mimetype: str | None = None,
    as_attachment: bool = False,
    download_name: str | None = None,
) -> FileResponse:
    """Send a file from a directory (sync version).

    Traversal-safe via `safe_join`. Returns 403 on any escape attempt.

    For async, use send_from_directory_async() instead.
    """
    from veloce.safe import safe_join

    resolved = safe_join(directory, filename)
    if resolved is None:
        abort(403, "Access denied")

    attachment_name = download_name if as_attachment else None
    return FileResponse(
        path=resolved,
        filename=attachment_name or (filename if as_attachment else None),
        content_type=mimetype,
    )


async def send_from_directory_async(
    directory: str,
    filename: str,
    mimetype: str | None = None,
    as_attachment: bool = False,
    download_name: str | None = None,
) -> FileResponse:
    """Send a file from a directory — async version, reads file in executor.

    Traversal-safe via `safe_join`.
    """
    from veloce.safe import safe_join

    # `safe_join` is pure string arithmetic; the file read happens below.
    resolved = safe_join(directory, filename)  # noqa: ASYNC240
    if resolved is None:
        abort(403, "Access denied")

    attachment_name = download_name if as_attachment else None
    return await FileResponse.from_path(
        path=resolved,
        filename=attachment_name or (filename if as_attachment else None),
        content_type=mimetype,
    )


# ── g (request-scoped globals) ───────────────────────────────────


class _RequestGlobals:
    """Request-scoped global object — the `g` namespace.

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
        """Reset g for a new request."""
        self._ctx_var.set({})


# Singleton
g = _RequestGlobals()


# ── flash() / get_flashed_messages() ─────────────────────────────


def flash(message: str, category: str = "message") -> None:
    """Flash a message for the next request — requires SessionMiddleware.

    Usage:
        flash("Item created successfully")
        flash("Invalid input", "error")
    """
    store = g._get_store()
    flashes = store.setdefault("_flashes", [])
    flashes.append((category, message))
    # `message_flashed` signal — fires for every flash() call.
    from veloce.signals import message_flashed

    message_flashed.send(_current_app_var.get(), message=message, category=category)


def get_flashed_messages(
    with_categories: bool = False, category_filter: list[str] | None = None
) -> list:
    """Get flashed messages — call in templates.

    Usage:
        messages = get_flashed_messages()
        messages = get_flashed_messages(with_categories=True)
    """
    store = g._get_store()
    flashes = store.pop("_flashes", [])
    if category_filter:
        flashes = [(cat, msg) for cat, msg in flashes if cat in category_filter]
    if with_categories:
        return flashes
    return [msg for _, msg in flashes]


# ── stream_with_context() ────────────────────────────────────────


def stream_with_context(generator: Any) -> Any:
    """Keep the request context alive while a streaming generator runs.

    A streaming response body is consumed by the ASGI
    emit layer *after* the handler has returned, by which point the
    request context has been torn down — so a generator that touches
    `request`, `g`, or `current_app` would fail. Wrap it::

        return StreamingResponse(stream_with_context(generate()))

    The current request / app / `g` snapshot is captured now and
    re-established for the lifetime of the wrapped iteration. Accepts
    either an async or a synchronous generator/iterable.
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
