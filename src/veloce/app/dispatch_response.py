"""Dependency resolution and response shaping for the dispatch pipeline."""

from __future__ import annotations

import asyncio
from typing import Any, get_args

from pydantic import BaseModel as _PydanticBaseModel

import veloce._json as _json
import veloce.status as status
from veloce._constants import (
    STATE_INJECTED_RESPONSE,
)
from veloce._internal import (
    _UNRESOLVED_JSON_DUMPS,
    MIME_HTML,
    MIME_JSON,
    _close_form_uploads,
    _unpack_response_tuple,
)
from veloce._model_backend import (
    _HAS_MSGSPEC,
    ModelBackend,
    _msgspec,
    backend_of,
    is_pydantic_model,
    shape_through_model,
)
from veloce.app._host import AppHost
from veloce.app.dispatch_support import _is_msgspec_payload
from veloce.dependency import DependencyResolver, Depends
from veloce.encoders import orjson_default
from veloce.http.request import Request
from veloce.http.response import (
    JSONResponse,
    Response,
    StreamingResponse,
)
from veloce.sse import EventSourceResponse


class ResponseDispatchMixin(AppHost):
    """Dependency resolution, response-model shaping and result coercion."""

    async def _resolve_dependencies(
        self, request: Request, match: Any
    ) -> tuple[dict[str, Any], DependencyResolver | None]:
        """Build the handler kwargs and the resolver that backs them.

        `None` for trivial / request-only plans. The caller must hold the
        resolver so the dispatch `finally` can run its yield-dependency
        teardowns even when the handler raises.
        """
        route_info = match.route_info
        if route_info.handler_plan is not None:
            if route_info.is_trivial_plan:
                return {}, None
            if route_info.is_request_only_plan:
                return {route_info.request_param_name: request}, None
            resolver = DependencyResolver()
            resolver._overrides = self._dependency_overrides
            resolver._override_subplans = self._override_subplans
            kwargs = await resolver.resolve_plan(
                route_info.handler_plan,
                request,
                match.path_params,
                route_info.route_dep_plans,
            )
            return kwargs, resolver
        resolver = DependencyResolver()
        resolver._overrides = self._dependency_overrides
        resolver._override_subplans = self._override_subplans
        kwargs = await resolver.resolve(
            route_info.handler,
            request,
            match.path_params,
            route_dependencies=[d for d in route_info.dependencies if isinstance(d, Depends)],
        )
        return kwargs, resolver

    def _build_response(self, request: Request, match: Any, result: Any) -> Response:
        """Turn a handler return value into the final Response."""
        route_info = match.route_info
        # Every backend shapes, or a subclass would put its extra fields on the wire.
        if route_info.response_model is not None and not isinstance(result, Response):
            result = self._apply_response_model(result, route_info)

        response = self._coerce_response(result, route_info.response_class)

        # Only while the response still carries the default 200, so a handler wins.
        if (
            route_info.status_code != status.HTTP_200_OK
            and response.status_code == status.HTTP_200_OK
        ):
            response.status_code = route_info.status_code
            response._encoded = None

        # A returned Response keeps its own; `0` means the handler set none.
        injected = request._state.get(STATE_INJECTED_RESPONSE) if request._state else None
        if injected is not None and not isinstance(result, Response):
            if injected.status_code:
                response.status_code = injected.status_code
            for hk, hv in injected.headers.items():
                if hk.lower() == "set-cookie":
                    response._append_set_cookie_header(hv)
                else:
                    response.headers[hk] = hv
            response._encoded = None

        return response

    def _schedule_background_tasks(self, request: Request, response: Response) -> bool:
        """Schedule the DI-injected queue and response-attached background task.

        Both run through `spawn()`, the single tracked-task path. Returns whether
        anything was scheduled: a background task may still be reading an upload,
        so the caller uses this to decide when to release the spool files.
        """
        injected = request._background_tasks
        attached_bg = response.background
        # Checked before anything is allocated: most responses have neither.
        if injected is None and attached_bg is None:
            return False

        coros = []
        if injected is not None:
            coros.append(injected.run_all())

        if attached_bg is not None:
            # Anything with a `run_all()` or `run()` coroutine method works.
            if hasattr(attached_bg, "run_all"):
                coro = attached_bg.run_all()
            elif hasattr(attached_bg, "run"):
                coro = attached_bg.run()
            else:
                coro = None
            if coro is not None:
                coros.append(coro)

        if not coros:
            return False
        tasks = [self.spawn(coro) for coro in coros]

        # A done-callback, not a task: a failed task still counts as done.
        form = request._form
        if form is not None:
            remaining = len(tasks)

            # Not over `request`, whose body and scope would stay alive with it.
            def _release(_task: asyncio.Task[Any]) -> None:
                nonlocal remaining
                remaining -= 1
                if remaining == 0:
                    _close_form_uploads(form)

            for task in tasks:
                task.add_done_callback(_release)
        return True

    def _apply_response_model(self, result: Any, route_info: Any) -> Any:
        """Route the handler return through `response_model` + dump flags.

        A `list[Model]` declaration validates each element individually. An
        async generator/iterable has no shape to validate against - left
        untouched, for the streaming fast path in `_coerce_response`.
        """
        if hasattr(result, "__aiter__"):
            return result
        model = route_info.response_model
        # Resolved at registration; read, never mutated - shared across requests.
        dump_kwargs = route_info.response_dump_kwargs

        origin = route_info.response_model_origin
        if origin is list:
            args = get_args(model)
            if args:
                inner = args[0]
                if not isinstance(result, (list, tuple)):
                    return result  # let downstream coercion handle the mismatch
                if is_pydantic_model(inner):
                    dumped: list[Any] = []
                    for item in result:
                        # Dumped directly, preserving what `exclude_unset` reads.
                        if type(item) is inner:
                            dumped.append(item.model_dump(**dump_kwargs))
                        else:
                            # Pydantic does not revalidate a satisfying instance.
                            payload = (
                                item.model_dump() if isinstance(item, _PydanticBaseModel) else item
                            )
                            dumped.append(inner.model_validate(payload).model_dump(**dump_kwargs))
                    return dumped
                if backend_of(inner) is not ModelBackend.NONE:
                    # `dump_kwargs` is Pydantic's vocabulary and cannot apply here.
                    return [
                        item if type(item) is inner else shape_through_model(item, inner)
                        for item in result
                    ]
            return result

        if is_pydantic_model(model):
            # A roundtrip erases the `__pydantic_fields_set__` info that drives
            # `exclude_unset`, so a SUBCLASS must not take this path.
            if type(result) is model:
                return result.model_dump(**dump_kwargs)
            # `exclude_unset` resets: the fields-set markers do not transfer.
            payload = result.model_dump() if isinstance(result, _PydanticBaseModel) else result
            validated = model.model_validate(payload)
            return validated.model_dump(**dump_kwargs)

        # The exact type is tested first - `backend_of` walks an isinstance ladder.
        if type(result) is model:
            return result
        if route_info.response_model_backend is not ModelBackend.NONE:
            # A full builtins round-trip, kept off the common path above.
            return shape_through_model(result, model)

        return result

    @staticmethod
    def _response_class_mismatch(response_class: Any, result: Any) -> str:
        """Build the message for a return the declared response class cannot render.

        Without it a `dict` reaches `.encode()` and produces an `AttributeError`
        naming neither the class asked for nor the value returned.
        """
        name = getattr(response_class, "__name__", response_class)
        return (
            f"{name} cannot render a {type(result).__name__}; it encodes str or bytes. "
            f"Return a str, or declare response_class=JSONResponse on this route "
            f"to send the value as JSON."
        )

    def _json_from_handler(self, data: Any) -> Response:
        """Build the JSON response for a handler's `dict` / `list` / model return.

        Takes the direct path unless the application configured a provider or a
        JSON option, in which case that dialect applies here too.
        """
        dumps = self._json_dumps_override()
        # Encoding here rather than in `JSONResponse(data)` resolves the dialect once.
        body = _json.dumps(data, default=orjson_default) if dumps is None else dumps(data)
        return JSONResponse._from_encoded(body)

    def _json_dumps_override(self) -> Any:
        """Return the configured serialiser, or `None` to take the direct path.

        Resolved once and cached. `None` is the stock case, where the direct
        path already emits exactly what the provider would.
        """
        dumps = self._handler_json_dumps
        if dumps is _UNRESOLVED_JSON_DUMPS:
            dumps = self._handler_json_dumps = self._resolve_handler_json_dumps()
        return dumps

    def _coerce_response(self, result: Any, response_class: Any = None) -> Response:
        """Convert handler return value to a Response object."""
        if isinstance(result, Response):
            return result
        # Gated so the JSONResponse-subclass branch below still owns dicts.
        if type(result) is dict and response_class is None:
            return self._json_from_handler(result)
        # A struct encodes in C; with a response_class it normalizes to builtins.
        if _HAS_MSGSPEC and _is_msgspec_payload(result):
            if response_class is None:
                dumps = self._json_dumps_override()
                if dumps is None:
                    return Response(body=_msgspec.json.encode(result), content_type=MIME_JSON)
                # A configured dialect outranks the struct fast path.
                return JSONResponse.from_bytes(dumps(_msgspec.to_builtins(result)))
            result = _msgspec.to_builtins(result)
        # Through the one table `make_response` uses; any other length is a value.
        if isinstance(result, tuple):
            unpacked = _unpack_response_tuple(result)
            if unpacked is not None:
                body, code, headers = unpacked
                resp = self._coerce_response(body, response_class)
                if code is not None:
                    resp.status_code = code
                if headers:
                    resp.headers.update(headers)
                # The status line and headers changed, so a cached encoding is stale.
                resp._encoded = None
                return resp
        # Checked here, not ahead of dict/msgspec/tuple above: neither can be
        # an async generator/iterable, so this only costs a `hasattr` call on
        # a shape none of those three already claimed. A `response_class`
        # that already knows how to accept one (StreamingResponse,
        # EventSourceResponse) is left to its own constructor, further down,
        # since building a plain StreamingResponse here would lose its media
        # type and its own item shape (e.g. a `ServerSentEvent`).
        if hasattr(result, "__aiter__") and not (
            isinstance(response_class, type)
            and issubclass(response_class, (StreamingResponse, EventSourceResponse))
        ):
            return StreamingResponse(result)
        if response_class is not None:
            if isinstance(response_class, type) and issubclass(response_class, JSONResponse):
                if isinstance(result, _PydanticBaseModel):
                    result = result.model_dump()
                dumps = self._json_dumps_override()
                if dumps is None:
                    return response_class(result)
                # On the requested class, so a subclass keeps its media type.
                return response_class.from_bytes(dumps(result))
            if isinstance(result, (str, bytes)):
                return response_class(result)
            # Try, then translate: refusing first would turn away a serialiser
            # that renders the value perfectly well.
            try:
                return response_class(result)
            except (AttributeError, TypeError) as exc:
                raise TypeError(self._response_class_mismatch(response_class, result)) from exc
        if isinstance(result, (dict, list)):
            return self._json_from_handler(result)
        if isinstance(result, str):
            # text/html is the same default `make_response()` applies.
            return Response(body=result.encode(), content_type=MIME_HTML)
        if isinstance(result, bytes):
            return Response(body=result, content_type=MIME_HTML)
        if isinstance(result, _PydanticBaseModel):
            return self._json_from_handler(result.model_dump())
        return self._json_from_handler(result)
