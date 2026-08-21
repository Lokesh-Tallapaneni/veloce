"""TasksMixin — background-task execution and result shaping for `MCPServer`.

Holds the task lifecycle (`_create_task` / `_run_task` / `_cancel_task` and the
status notification), the streamed-result drain, and the response-to-MCP-result
shaping shared between the synchronous and task-augmented `tools/call` paths.
Mixed into `MCPServer`, so `self` resolves the dispatch core and `InvocationMixin`
at runtime; the `TYPE_CHECKING` block declares the cross-member surface for mypy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from veloce._model_backend import shape_through_model
from veloce.contrib.mcp._helpers import (
    _binary_result,
    _notifier_var,
    _progress_token,
    _resource_result_from_response,
    _response_body_value,
    _session_var,
    _stringify,
    _text_result,
    _to_structured,
)
from veloce.contrib.mcp.context import _in_task_var
from veloce.contrib.mcp.errors import (
    InvalidParamsError,
    _StreamTimeoutError,
    _StreamTooLargeError,
)
from veloce.contrib.mcp.tasks import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    TASKS_EXTENSION,
    create_task_result,
    new_task,
    status_notification,
    task_ttl_ms,
)
from veloce.http.response import Response

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from veloce.contrib.mcp.registry import MCPTool
    from veloce.contrib.mcp.tasks import MCPTask, TaskRegistry

_logger = logging.getLogger(__name__)

# Cap on the bytes buffered from a streamed tool result. A streamed response has
# no single body, so the MCP path drains it into one (`_drain_stream`); this
# bound stops a runaway or unbounded stream from exhausting memory - crossing it
# yields an in-band tool error instead.
_STREAM_BUFFER_LIMIT = 5 * 1024 * 1024

# Wall-clock budget for draining a streamed tool result. The size cap alone does
# not defend against a slow or never-completing stream that stays small (a
# heartbeat SSE feed, a handler awaiting forever): without a deadline such a
# stream wedges the serial stdio serve loop, blocking every later request.
# Crossing it closes the stream and yields an in-band tool error.
_STREAM_DRAIN_TIMEOUT = 30.0


# The `_meta` key a modern request states its revision in. Duplicated here rather
# than imported from `server`, which imports this module.
_META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"


def _modern_request(params: dict[str, Any]) -> bool:
    """Whether this request declared a modern protocol version in its `_meta`."""
    meta = params.get("_meta")
    return isinstance(meta, dict) and isinstance(meta.get(_META_PROTOCOL_VERSION), str)


def _client_declared_tasks() -> bool:
    """Whether the calling client advertised the tasks extension."""
    session = _session_var.get()
    if session is None:
        return False
    extensions = session.client_capabilities.get("extensions")
    return isinstance(extensions, dict) and TASKS_EXTENSION in extensions


class TasksMixin:
    """Background-task lifecycle and tool-result shaping, mixed into `MCPServer`."""

    # `MCPServer` is slotted; a mixin must declare `__slots__` too or its
    # instances regain a `__dict__` (see the __slots__ discipline rule).
    __slots__ = ()

    if TYPE_CHECKING:  # pragma: no cover
        # Attributes / methods the host server (and `InvocationMixin`) provide.
        app: Any
        _tasks: TaskRegistry
        _produce_tool_result: Callable[..., Any]
        _error_text: Callable[..., Any]

    # ── Tasks ─────────────────────────────────────────────

    def _create_task(
        self, tool: MCPTool, arguments: dict[str, Any], params: dict[str, Any]
    ) -> dict[str, Any]:
        """Start a task-augmented tool call and return its `CreateTaskResult`.

        A tool that did not opt into task support rejects the task-augmented call
        (invalid-params), matching the ``execution.taskSupport: "forbidden"`` it
        advertises. An opted-in tool's call is started detached - the handler runs
        on a background asyncio task through the same `tools/call` result builder
        the synchronous path uses - and the new task object is returned at once.
        """
        if not tool.task_support:
            raise InvalidParamsError(
                f"Tool {tool.name!r} does not support task execution; call it without a 'task' field."
            )
        # A task is never handed to a client that did not declare the extension:
        # it would be given a handle it has no `tasks/*` methods to resolve.
        modern = _modern_request(params)
        if modern and not _client_declared_tasks():
            raise InvalidParamsError(
                "This client did not declare the "
                f"{TASKS_EXTENSION!r} extension, so a task cannot be returned; "
                "call the tool without a 'task' field."
            )
        self._tasks.evict_expired()
        # The creating connection owns the task: a task method from a different
        # connection cannot see or act on it (multi-client isolation on HTTP).
        session = _session_var.get()
        owner_key = session.connection_id if session is not None else None
        task = new_task(tool.name, task_ttl_ms(params), owner_key)
        self._tasks.register(task)
        # `create_task` copies the current context, so the background runner sees
        # the same notifier / log level / principal the request established here.
        progress_token = _progress_token(params)
        task.runner = asyncio.ensure_future(self._run_task(task, tool, arguments, progress_token))
        return create_task_result(task, modern=modern)

    async def _run_task(
        self,
        task: MCPTask,
        tool: MCPTool,
        arguments: dict[str, Any],
        progress_token: str | int | None,
    ) -> None:
        """Run a task's tool call to completion, settling and notifying the client.

        Produces the result through the shared `tools/call` builder (one handler,
        two doors), then moves the task to `completed` / `failed` and emits a
        ``notifications/tasks/status`` so a watching client learns the outcome.
        A cancellation mid-run leaves the already-recorded `cancelled` status
        untouched.
        """
        # Mark this as a detached task so a server->client request issued from the
        # runner is refused on the serial stdio transport (its reply has no reader
        # while the serve loop has already resumed reading stdin).
        _in_task_var.set(True)
        started = time.perf_counter()
        try:
            result = await self._produce_tool_result(tool, arguments, started, progress_token)
        except asyncio.CancelledError:
            # `_cancel_task` has already settled the task to `cancelled` and sent
            # its notification; let the cancellation unwind without overwriting it.
            raise
        except InvalidParamsError as exc:
            # A malformed argument surfaces synchronously on the non-task path; on
            # the task path the call already started, so it settles the task as
            # failed rather than propagating to a dead request.
            task.settle(STATUS_FAILED, _text_result(str(exc), is_error=True), str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            _logger.exception("MCP task %s raised", task.name)
            text = self._error_text(exc, "the task raised an internal error")
            task.settle(STATUS_FAILED, _text_result(text, is_error=True), text)
        else:
            # An in-band tool error is still a settled task: the call ran and
            # produced a result the client retrieves; `isError` lives inside it.
            is_error = bool(result.get("isError"))
            task.settle(STATUS_FAILED if is_error else STATUS_COMPLETED, result)
        await self._notify_task_status(task)

    async def _cancel_task(self, task: MCPTask) -> None:
        """Cancel a running task, settling it to `cancelled` and notifying.

        A task already terminal is left untouched (a cancel racing completion is a
        no-op per the spec); a working task is moved to `cancelled` and its runner
        unwound. The status notification is awaited inline (the `tasks/cancel`
        handler is async) so it cannot be dropped by garbage collection.
        """
        if task.is_terminal():
            return
        runner = task.runner
        task.settle(STATUS_CANCELLED, _text_result("task cancelled", is_error=True), "cancelled")
        if runner is not None and not runner.done():
            runner.cancel()
        await self._notify_task_status(task)

    async def _notify_task_status(self, task: MCPTask) -> None:
        """Emit ``notifications/tasks/status`` for a task transition, if a sink exists.

        The notifier captured when the task was created carries the message; off a
        transport (a bare construction) or once the originating request's stream
        has closed there is no sink and the transition is silent - the client
        still learns the outcome by polling ``tasks/get`` / ``tasks/result``.
        """
        notifier = _notifier_var.get()
        if notifier is None:
            return
        try:
            await notifier(status_notification(task))
        except Exception:  # pragma: no cover - a dead sink must not fail the task
            _logger.exception("MCP task status notification failed")

    async def _drain_stream(self, response: Response) -> None:
        """Buffer a streamed response into its body so it can be a tool result.

        A `StreamingResponse` / `EventSourceResponse` has no single body, but an
        MCP `tools/call` returns one result, so the stream is consumed and joined
        into the response body; afterwards it shapes like any buffered response.
        A non-streaming response is left untouched. Draining is bounded in both
        size and time: a stream exceeding `_STREAM_BUFFER_LIMIT` raises
        `_StreamTooLargeError`, and one that has not completed within
        `_STREAM_DRAIN_TIMEOUT` raises `_StreamTimeoutError` - both surfaced as an
        in-band error. The size cap stops a fast runaway; the deadline stops a
        slow or never-completing stream (a heartbeat SSE feed, a handler that
        awaits forever) from wedging the serial stdio serve loop. The size cap
        bounds the *accumulated* bytes - a single chunk is materialised by the
        producer before the check, so peak memory is the cap plus the largest
        single chunk. On either bounded failure - size cap or timeout - the
        underlying async generator is closed so the producing task does not leak,
        and that close is itself bounded by the same deadline.
        """
        if not response.is_streamed:
            return
        chunks: list[bytes] = []
        stream = response.iter_encoded()

        async def _collect() -> None:
            total = 0
            async for chunk in stream:
                piece = chunk.encode("utf-8") if isinstance(chunk, str) else chunk
                total += len(piece)
                if total > _STREAM_BUFFER_LIMIT:
                    raise _StreamTooLargeError(
                        f"streamed result exceeded the {_STREAM_BUFFER_LIMIT}-byte MCP buffer limit"
                    )
                chunks.append(piece)

        # The collect runs as its own task so the deadline can be enforced
        # independently of the producer's teardown. Cancelling an in-flight
        # `async for` step runs the generator's `finally` as part of that
        # cancellation; if that teardown awaits (a malicious / buggy
        # `finally: await ...`), awaiting the cancellation to completion would
        # re-wedge the serial stdio serve loop past the deadline. So the
        # cancellation is itself bounded and the producer is abandoned if its
        # teardown overruns the budget - the timeout's whole purpose.
        task = asyncio.ensure_future(_collect())
        try:
            # `wait_for` (not `asyncio.timeout`, which is 3.11+) is fed a shield
            # so a timeout does not synchronously await the cancellation here.
            await asyncio.wait_for(asyncio.shield(task), _STREAM_DRAIN_TIMEOUT)
        except asyncio.TimeoutError as exc:
            await self._abandon_drain(task, stream)
            raise _StreamTimeoutError(
                f"streamed result did not complete within the "
                f"{_STREAM_DRAIN_TIMEOUT}-second MCP drain timeout"
            ) from exc
        except _StreamTooLargeError:
            # The size cap raises from inside the task, leaving the producer
            # suspended at its `async for`; close it under the same bound so it
            # does not leak until GC.
            await self._abandon_drain(task, stream)
            raise
        response.body = b"".join(chunks)
        response._stream = None

    @staticmethod
    async def _abandon_drain(task: asyncio.Future[None], stream: Any) -> None:
        """Tear down an aborted drain's collect task and producer, bounded.

        Both the task cancellation and the generator `aclose()` are bounded by
        `_STREAM_DRAIN_TIMEOUT`; a teardown that itself awaits past the budget is
        abandoned rather than allowed to re-wedge the serial stdio serve loop.
        """
        # On the timeout path the task is still running its in-flight step and is
        # cancelled here; on the size-cap path it has already raised and only the
        # suspended producer needs closing.
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), _STREAM_DRAIN_TIMEOUT)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception:
                _logger.exception("MCP stream drain teardown failed")
        aclose = getattr(stream, "aclose", None)
        if aclose is None:
            return
        try:
            await asyncio.wait_for(aclose(), _STREAM_DRAIN_TIMEOUT)
        except Exception:
            _logger.exception("MCP stream cleanup failed")

    def _result_from_response(
        self, tool: MCPTool, response: Response, model_filtered: bool
    ) -> dict[str, Any]:
        """Shape a route-backed tool's `Response` into the MCP call result.

        The response body is decoded back to a value (so the agent sees the same
        JSON the HTTP client would) and a 4xx/5xx status is flagged as an in-band
        `isError`. A streamed response has already been buffered into its body by
        `_drain_stream` before this runs.

        `model_filtered` is `True` when the route built this response from a
        non-`Response` handler return (so `_build_response` ran the
        `response_model` filter over it) and `False` when the handler returned
        its own `Response` (whose body bypassed the filter). When `False` and the
        route declares a `response_model`, the decoded body is re-run through that
        filter here before being emitted as `structuredContent`, so the value
        conforms to the advertised `outputSchema` and a field the model would
        exclude cannot leak under a schema that says it is absent.
        """
        # An image/audio body has no text form; emit it as the matching typed
        # MCP content block (base64) rather than a decoded-text block.
        binary = _binary_result(response)
        if binary is not None:
            return binary
        # A successful response may ask, via an opt-in header, that its result be
        # delivered as a resource-link / embedded-resource block rather than inline
        # text; a 4xx/5xx skips this and surfaces the error body as text below.
        if response.status_code < 400:
            resource = _resource_result_from_response(tool, response)
            if resource is not None:
                return resource
        shaped = self._shape_result(tool, response)
        # A 4xx/5xx is an in-band error: surface the body text and flag it,
        # without structured content (the error body is not the tool's output
        # shape). A success goes through the shared success shaping so a declared
        # `outputSchema` yields `structuredContent` alongside the text block.
        if response.status_code >= 400:
            return _text_result(_stringify(shaped), is_error=True)
        # A handler that returned its own `Response` bypassed the route
        # `response_model` filter, so its decoded body is not yet trusted to
        # conform to the advertised `outputSchema`. Re-run it through the filter
        # here so the emitted `structuredContent` both honours the MCP MUST (a
        # declared `outputSchema` is matched by conforming structured output) and
        # keeps the field-leak protection - a field the model excludes is dropped
        # before it reaches the client.
        #
        # The body is one the handler explicitly built, so it may not be an
        # object the `response_model` can validate at all: a `PlainTextResponse`,
        # an SSE / streaming body decoded to text, or a JSON shape that fails
        # `model_validate`. The HTTP path serves such a body as-is (a handler's
        # own `Response` bypasses `response_model` there), so the re-filter must
        # never harden a call HTTP would serve into a JSON-RPC transport error.
        # On any re-filter failure the value is emitted as the text content block
        # only, with no `structuredContent` - a non-object body has no object
        # form to advertise anyway.
        route_info = tool.route_info
        if tool.output_schema is not None:
            if route_info is not None and route_info.response_model is not None:
                # `_build_response` already ran the `response_model` filter for a
                # non-`Response` return (`model_filtered`); only a handler-built
                # `Response` body still needs it, through the route's dump
                # settings so `structuredContent` conforms to the advertised
                # schema and an excluded field cannot leak.
                if not model_filtered:
                    try:
                        shaped = self.app._apply_response_model(shaped, route_info)
                    except Exception:
                        return _text_result(_stringify(shaped))
            elif tool.output_model is not None:
                # The output schema came from the handler's return annotation, not
                # a `response_model`, so `_build_response` never filtered to it -
                # validate every return (raw value or handler-built body) through
                # the model so a field outside it cannot leak.
                try:
                    shaped = shape_through_model(shaped, tool.output_model)
                except Exception:
                    return _text_result(_stringify(shaped))
        return self._success_result(tool, shaped)

    def _success_result(self, tool: MCPTool, shaped: Any) -> dict[str, Any]:
        """Build a successful tool-call result from a shaped return value.

        The text content block is always present (back-compatible with clients
        that read only `content`). `structuredContent` is added when the tool
        declares an `outputSchema` and the value carries an object form. A body
        that bypassed the route `response_model` has already been re-filtered by
        the caller, so any value reaching here is trusted to conform to the
        advertised schema - honouring the MCP requirement that a declared
        `outputSchema` is matched by conforming structured output.
        """
        result = _text_result(_stringify(shaped))
        if tool.output_schema is not None:
            structured = _to_structured(shaped)
            if structured is not None:
                result["structuredContent"] = structured
        return result

    def _shape_result(self, tool: MCPTool, result: Any) -> Any:
        """Run a tool's return through the HTTP response shaping.

        For a tool exposed from an HTTP route the handler return is shaped
        exactly as the HTTP path shapes it: the route `response_model` filtering
        runs first for a non-`Response` return (so fields hidden on the HTTP
        response cannot leak over MCP). A returned `Response`/`JSONResponse` -
        from either a route-backed or a pure `@app.mcp_tool` - is unwrapped to
        its actual body (a JSON body decoded back to a value, any other body to
        its text) rather than serialised as an object repr; a streamed response
        has been buffered into that body by `_drain_stream` first. A pure tool's
        non-`Response` return is passed back unchanged.

        This shapes the value only. A route-backed handler that returned its own
        `Response` bypassed the `response_model` filter, so its decoded body is
        re-run through that filter by the caller (`_result_from_response`) before
        being advertised as schema-conformant `structuredContent`.
        """
        route_info = tool.route_info

        # `response_model` reshapes only a non-`Response` return, mirroring
        # `app._build_response`: a handler that built its own Response already
        # chose its body.
        if (
            route_info is not None
            and route_info.response_model is not None
            and not isinstance(result, Response)
        ):
            result = self.app._apply_response_model(result, route_info)

        if isinstance(result, Response):
            return _response_body_value(result)
        return result
