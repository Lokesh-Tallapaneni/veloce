"""Public dispatch aliases for the dispatch pipeline."""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from typing import Any

from veloce._internal import (
    MIME_HTML,
    _is_async_callable,
    _unpack_response_tuple,
)
from veloce.app._host import AppHost
from veloce.blueprints import _endpoint_blueprint
from veloce.helpers import jsonify
from veloce.http.request import Request
from veloce.http.response import (
    Response,
)


class AliasDispatchMixin(AppHost):
    """Public dispatch entry points and response coercion helpers."""

    # Both alias `_dispatch_request`, which runs the full chain inline.
    async def dispatch_request(self, request: Request) -> Any:
        """Alias for `_dispatch_request`."""
        return await self._dispatch_request(request, self._ensure_pipeline())

    async def full_dispatch_request(self, request: Request) -> Any:
        """Dispatch `request` through the full before/after-request hook chain."""
        return await self._dispatch_request(request, self._ensure_pipeline())

    async def preprocess_request(self, request: Request) -> Any:
        """Run all `before_request` hooks for `request`.

        A non-None return short-circuits the chain and becomes the response.
        App-level hooks fire first, then the matched-blueprint bucket.
        """
        for hook in self._before_request_hooks:
            result = await self._call_handler(hook, {"request": request})
            if result is not None:
                return result
        # `endpoint` is a slot assigned in `__init__`, so no default can apply.
        bp = _endpoint_blueprint(request.endpoint)
        if bp is not None and self._bp_before_hooks:
            for hook in self._bp_before_hooks.get(bp, ()):
                result = await self._call_handler(hook, {"request": request})
                if result is not None:
                    return result
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        """Run all `after_request` hooks for `(request, response)`.

        Hooks fire in **reverse** registration order; each may return a
        replacement `Response`, and any other return keeps the existing one.
        This is the dispatch path itself, not a re-implementation.

        Args:
            request: The request the response answers.
            response: The response to pass through the hooks.

        Returns:
            The response after every hook has run.

        """
        return await self._run_after_hooks(request, response, _endpoint_blueprint(request.endpoint))

    @staticmethod
    def ensure_sync(func: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap `func` so it is callable from synchronous code.

        A regular function is returned unchanged; a coroutine function comes
        back wrapped to run on a dedicated event loop. Use it to bridge async
        handlers into CLI commands, background workers or test scaffolding.
        """
        if not _is_async_callable(func):
            return func

        @functools.wraps(func)
        def _sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return asyncio.run(func(*args, **kwargs))

        return _sync_wrapper

    def make_response(self, value: Any) -> Response:
        """Coerce a handler-return value into a `Response`.

        Accepts (with this coercion table):
        - `Response` -> returned as-is
        - `str` / `bytes` -> wrapped as a text/HTML response
        - `dict` / `list` -> wrapped as a JSON response via `jsonify`
        - `tuple` of `(body, status)`, `(body, status, headers)` or
          `(body, headers)` -> unpacked and re-coerced
        - anything else -> JSON, matching what dispatch does with the same
          value returned from a handler

        A tuple of any other length is not a response tuple and is answered as
        a plain value.
        """
        if isinstance(value, Response):
            return value
        if isinstance(value, tuple):
            unpacked = _unpack_response_tuple(value)
            if unpacked is not None:
                body, code, headers = unpacked
                resp = self.make_response(body)
                if code is not None:
                    resp.status_code = code
                if headers:
                    items = headers.items() if isinstance(headers, dict) else headers
                    for k, v in items:
                        resp.headers[k] = v
                # The status line and headers changed, so a cached encoding is stale.
                resp._encoded = None
                return resp
        if isinstance(value, (dict, list)):
            return jsonify(value)
        if isinstance(value, bytes):
            return Response(body=value, content_type=MIME_HTML)
        if isinstance(value, str):
            return Response(
                body=value.encode("utf-8"),
                content_type=MIME_HTML,
            )
        return jsonify(value)
