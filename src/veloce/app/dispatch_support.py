"""Dispatch support — module state and hook-binding helpers shared by the dispatch mixins."""

from __future__ import annotations

import contextlib
import inspect
import weakref
from collections.abc import Callable, MutableMapping
from typing import Any, get_args, get_origin

from veloce._constants import (
    HEADER_ACCEPT,
    MIME_TEXT_HTML,
    MIME_TEXT_PLAIN,
)
from veloce._model_backend import (
    is_msgspec_struct,
)
from veloce._protocol_constants import (
    TRACE_HEADER_TRACEPARENT,
    TRACE_HEADER_TRACESTATE,
    build_trace_carrier,
)
from veloce.http.request import Request
from veloce.http.response import (
    Response,
)

# ── Dispatch-scoped module state ───────────────────────────


# Cache of the `(request_key, exc_key)` binding per exception handler.
_HookBinding = tuple[str | None, str | None]

_exc_handler_sig_cache: weakref.WeakKeyDictionary[Callable[..., Any], _HookBinding] = (
    weakref.WeakKeyDictionary()
)

# Cache of the `(request_key, response_key)` binding per after-request hook.
#: How long a request waits between checks while another runs the
#: `before_first_request` hooks. Once per app, so the cost is one wake-up.
_FIRST_REQUEST_POLL_SECONDS = 0.001

_after_hook_sig_cache: weakref.WeakKeyDictionary[Callable[..., Any], _HookBinding] = (
    weakref.WeakKeyDictionary()
)

_MW_RESPONSE_CHAIN_KEY = "_mw_response_chain"


# ── Module helpers ─────────────────────────────────────────


def _prefers_html(request: Request) -> bool:
    """Whether the client prefers an HTML response over plain text."""
    accept = request.headers.get(HEADER_ACCEPT)
    if not accept:
        return False
    return request.accept_mimetypes.best_match([MIME_TEXT_PLAIN, MIME_TEXT_HTML]) == MIME_TEXT_HTML


def _trace_carrier(request: Request) -> dict[str, str] | None:
    """Inbound W3C trace headers as a carrier dict, or `None` if absent.

    Only `traceparent` / `tracestate` are copied, keeping the core free of any
    OpenTelemetry dependency.
    """
    traceparent = request.headers.get(TRACE_HEADER_TRACEPARENT)
    if traceparent is None:
        return None
    return build_trace_carrier(traceparent, request.headers.get(TRACE_HEADER_TRACESTATE))


def _is_msgspec_payload(result: Any) -> bool:
    """Whether a handler return value should be encoded by msgspec.

    A `tuple` is deliberately excluded - it is the `(body, status[, headers])`
    response idiom, not a JSON array.
    """
    if is_msgspec_struct(type(result)):
        return True
    if isinstance(result, list) and result:
        return is_msgspec_struct(type(result[0]))
    return False


def _is_struct_list_model(model: Any) -> bool:
    """Whether `model` is `list[SomeStruct]` (a msgspec list response_model)."""
    if get_origin(model) is list:
        args = get_args(model)
        return bool(args) and is_msgspec_struct(args[0])
    return False


# `BaseException` rather than the raised class, so any base of it matches alike.
_SECOND_VALUE_TYPES: dict[str, type[Any]] = {"response": Response, "exc": BaseException}


def _resolve_hook_binding(fn: Callable[..., Any], second_name: str) -> _HookBinding:
    """Decide which parameters of `fn` receive the request and the second value.

    Three rules per slot, in order: the declared name (`request` and `response` /
    `exc`), then the annotation, then position. A lone unmatched parameter is the
    exception - it receives the second value. Binding is by keyword, so a
    positional-only parameter cannot be filled and a third required one raises.
    """
    # PEP 563 strings, evaluated once per callable; `eval_str` is all-or-none.
    evaluated = True
    try:
        params = inspect.signature(fn, eval_str=True).parameters
    except Exception:  # noqa: BLE001 - any unevaluatable annotation, not just NameError
        params = inspect.signature(fn).parameters
        evaluated = False

    if any(p.kind is p.VAR_KEYWORD for p in params.values()):
        return "request", second_name

    request_key = "request" if "request" in params else None
    second_key = second_name if second_name in params else None
    # No `VAR_KEYWORD` can reach here - the `any(...)` above returned.
    bindable = [p for p in params.values() if p.kind is not p.VAR_POSITIONAL]

    if evaluated and (request_key is None or second_key is None):
        second_type = _SECOND_VALUE_TYPES[second_name]
        for p in bindable:
            # Skip what cannot answer `issubclass` (`Request | None`, a string).
            if p.name in (request_key, second_key) or not isinstance(p.annotation, type):
                continue
            if request_key is None and issubclass(p.annotation, Request):
                request_key = p.name
            elif second_key is None and issubclass(p.annotation, second_type):
                second_key = p.name

    # A default is the author stating a value, which closure capture relies on.
    fillable = [
        p.name
        for p in bindable
        if p.name != request_key
        and p.name != second_key
        and p.default is p.empty
        and p.name not in ("self", "cls")
    ]
    if fillable and (request_key is None or second_key is None):
        if request_key is None and second_key is None and len(fillable) == 1:
            # Binding the request instead would fail silently.
            second_key = fillable[0]
        elif request_key is not None:
            second_key = fillable[0]
        elif second_key is None:
            for name in fillable:
                if request_key is None:
                    request_key = name
                elif second_key is None:
                    second_key = name
                else:
                    break
        # Left unbound, to reach the `unfillable` refusal on the first request.

    unfillable = [n for n in fillable if n != request_key and n != second_key]
    if unfillable:
        raise TypeError(
            f"{getattr(fn, '__qualname__', None) or fn!r} declares required "
            f"parameter(s) {', '.join(unfillable)} that cannot be filled: only "
            f"the request and the {second_name} are passed, so give them a "
            "default or remove them"
        )
    return request_key, second_key


def _adapt_hook_kwargs(
    fn: Callable[..., Any],
    cache: MutableMapping[Any, _HookBinding],
    second_name: str,
    request: Request,
    second_value: Any,
) -> dict[str, Any]:
    """Select the kwargs `fn` accepts, from `request` and one other value.

    The binding is cached per callable, so the per-request cost is one lookup
    and the dict build. Not a coroutine: it is called from inside an existing
    `await`, and wrapping it would add a coroutine per hook per request.
    """
    # A callable that cannot be weakly referenced raises on lookup, not just insert.
    try:
        binding = cache.get(fn)
    except TypeError:
        binding = None
    if binding is None:
        binding = _resolve_hook_binding(fn, second_name)
        with contextlib.suppress(TypeError):
            cache[fn] = binding
    request_key, second_key = binding
    kwargs: dict[str, Any] = {}
    if request_key is not None:
        kwargs[request_key] = request
    if second_key is not None:
        kwargs[second_key] = second_value
    return kwargs
