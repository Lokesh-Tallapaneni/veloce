"""TasksMixin — background-task execution and result shaping for `MCPServer`.

Holds the task lifecycle, the streamed-result drain, and the
response-to-MCP-result shaping shared between the synchronous and
task-augmented `tools/call` paths. Mixed into `MCPServer`, so `self` resolves
the dispatch core at runtime and the `TYPE_CHECKING` block declares it for mypy.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from veloce._model_backend import shape_through_model
from veloce.contrib.mcp._helpers import (
    _binary_result,
    _content_blocks,
    _era_modern_var,
    _notifier_var,
    _progress_token,
    _request_id_var,
    _resource_result_from_response,
    _response_body_value,
    _stringify,
    _text_result,
    _to_structured,
)
from veloce.contrib.mcp.context import (
    _in_task_var,
    _origin_request_id_var,
    _session_var,
    _task_id_var,
)
from veloce.contrib.mcp.errors import (
    InvalidParamsError,
    _InBandError,
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

# Bytes `_drain_stream` may buffer, so a runaway stream cannot exhaust memory.
_STREAM_BUFFER_LIMIT = 5 * 1024 * 1024

# Budget for the same drain, so a slow small stream cannot wedge the stdio loop.
_STREAM_DRAIN_TIMEOUT = 30.0


def _client_declared_tasks() -> bool:
    """Whether the calling client advertised the tasks extension."""
    session = _session_var.get()
    if session is None:
        return False
    extensions = session.client_capabilities.get("extensions")
    return isinstance(extensions, dict) and TASKS_EXTENSION in extensions


class TasksMixin:
    """Background-task lifecycle and tool-result shaping, mixed into `MCPServer`."""

    # `MCPServer` is slotted, so a mixin without `__slots__` regains a `__dict__`.
    __slots__ = ()

    if TYPE_CHECKING:  # pragma: no cover
        app: Any
        _tasks: TaskRegistry
        _result_dumps: Any
        _produce_tool_result: Callable[..., Any]
        _error_text: Callable[..., Any]

    # ── Tasks ─────────────────────────────────────────────

    def _create_task(
        self, tool: MCPTool, arguments: dict[str, Any], params: dict[str, Any]
    ) -> dict[str, Any]:
        """Start a task-augmented tool call and return its `CreateTaskResult`.

        A tool that did not opt in rejects the task-augmented call with
        invalid-params, matching the ``execution.taskSupport: "forbidden"`` it
        advertises. An opted-in call starts detached, through the same
        `tools/call` result builder the synchronous path uses.
        """
        if not tool.task_support:
            raise InvalidParamsError(
                f"Tool {tool.name!r} does not support task execution; call it without a 'task' field."
            )
        # Without the extension a client has no `tasks/*` method to resolve this.
        modern = _era_modern_var.get()
        if modern and not _client_declared_tasks():
            raise InvalidParamsError(
                "This client did not declare the "
                f"{TASKS_EXTENSION!r} extension, so a task cannot be returned; "
                "call the tool without a 'task' field."
            )
        self._tasks.evict_expired()
        # The creating connection owns the task, so no peer can see or act on it.
        session = _session_var.get()
        # A per-POST session could never poll its task back, nor evict it.
        if session is not None and not session.persistent:
            raise InvalidParamsError(
                "This connection has no session, so a task created here could "
                "never be retrieved; mount with sessions=True to use tasks over "
                "HTTP, or call the tool without a 'task' field."
            )
        owner_key = session.connection_id if session is not None else None
        task = new_task(tool.name, task_ttl_ms(params), owner_key)
        self._tasks.register(task)
        # `create_task` copies the context: same notifier, log level, principal.
        progress_token = _progress_token(params)
        task.runner = asyncio.ensure_future(
            self._run_task(task, tool, arguments, progress_token, _request_id_var.get())
        )
        return create_task_result(task)

    async def _run_task(
        self,
        task: MCPTask,
        tool: MCPTool,
        arguments: dict[str, Any],
        progress_token: str | int | None,
        origin_request_id: Any = None,
    ) -> None:
        """Run a task's tool call to completion, settling and notifying the client.

        Produces the result through the shared `tools/call` builder, then settles
        the task and emits ``notifications/tasks/status``. A cancellation mid-run
        leaves the already-recorded `cancelled` status untouched.
        """
        # Detached, so stdio refuses a server->client request: no reader.
        _in_task_var.set(True)
        # The polling handle, plus the request the client correlates against.
        _task_id_var.set(task.name)
        _origin_request_id_var.set(origin_request_id)
        started = time.perf_counter()
        try:
            result = await self._produce_tool_result(tool, arguments, started, progress_token)
        except asyncio.CancelledError:
            # `_cancel_task` already settled and notified; do not overwrite it.
            raise
        except InvalidParamsError as exc:
            # The call started, so a bad argument fails the task, not the request.
            task.settle(STATUS_FAILED, _text_result(str(exc), is_error=True), str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            _logger.exception("MCP task %s raised", task.name)
            text = self._error_text(exc, "the task raised an internal error")
            task.settle(STATUS_FAILED, _text_result(text, is_error=True), text)
        else:
            # An in-band tool error is still a settled task, `isError` and all.
            is_error = bool(result.get("isError"))
            task.settle(STATUS_FAILED if is_error else STATUS_COMPLETED, result)
        await self._notify_task_status(task)

    async def _cancel_task(self, task: MCPTask) -> None:
        """Cancel a running task, settling it to `cancelled` and notifying.

        A terminal task is left untouched - the spec makes a cancel racing
        completion a no-op. The status notification is awaited inline so it
        cannot be dropped by garbage collection.
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

        Off a transport, or once the originating stream has closed, there is no
        sink and the transition is silent; the client still learns the outcome by
        polling ``tasks/get`` / ``tasks/result``.
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

        A `tools/call` returns one result, so a streamed response is consumed and
        joined into its body and then shapes like any buffered one; a
        non-streaming response is left untouched.

        Bounded in size by `_STREAM_BUFFER_LIMIT` and in time by
        `_STREAM_DRAIN_TIMEOUT`, each raising its own in-band error. The size cap
        bounds the *accumulated* bytes, so peak memory is the cap plus the largest
        single chunk. On either failure the async generator is closed, bounded by
        the same deadline, so the producing task does not leak.
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

        # Its own task, so the deadline outlives a teardown that itself awaits.
        task = asyncio.ensure_future(_collect())
        try:
            # Shielded, so a timeout does not await the cancellation here.
            await asyncio.wait_for(asyncio.shield(task), _STREAM_DRAIN_TIMEOUT)
        except asyncio.TimeoutError as exc:
            await self._abandon_drain(task, stream)
            raise _StreamTimeoutError(
                f"streamed result did not complete within the "
                f"{_STREAM_DRAIN_TIMEOUT}-second MCP drain timeout"
            ) from exc
        except _StreamTooLargeError:
            # The cap raises inside the task, so close the suspended producer.
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
        # Mid-step on the timeout path, already raised on the size-cap one.
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

        The body is decoded back to a value, so the agent sees the same JSON the
        HTTP client would, and a 4xx/5xx is flagged as an in-band `isError`.

        `model_filtered` is `True` when `_build_response` already ran the route's
        `response_model` filter over a non-`Response` return. When `False` the
        decoded body is re-run through that filter here, so `structuredContent`
        conforms to the advertised `outputSchema` and an excluded field cannot
        leak under a schema that says it is absent.
        """
        # An image/audio body has no text form, so it goes out as a base64 block.
        binary = _binary_result(response)
        if binary is not None:
            return binary
        # An opt-in header can ask for a resource block instead of inline text.
        if response.status_code < 400:
            resource = _resource_result_from_response(tool, response)
            if resource is not None:
                return resource
        shaped = self._shape_result(tool, response)
        if response.status_code >= 400:
            return _text_result(_stringify(shaped, self._result_dumps), is_error=True)
        # Re-filtered for MCP's conformance rule; a failure falls back to text.
        route_info = tool.route_info
        if tool.output_schema is not None:
            if route_info is not None and route_info.response_model is not None:
                # Only a handler-built body still needs the route's own filter.
                if not model_filtered:
                    try:
                        shaped = self.app._apply_response_model(shaped, route_info)
                    except Exception:
                        return _text_result(_stringify(shaped, self._result_dumps))
            elif tool.output_model is not None:
                # From the annotation, which `_build_response` never filtered to.
                try:
                    shaped = shape_through_model(shaped, tool.output_model)
                except Exception:
                    return _text_result(_stringify(shaped, self._result_dumps))
        return self._success_result(tool, shaped)

    def _success_result(self, tool: MCPTool, shaped: Any) -> dict[str, Any]:
        """Build a successful tool-call result from a shaped return value.

        A handler that returned content blocks has said what the result is, so
        they are emitted in order and nothing is added. Otherwise the text block
        is always present, for clients that read only `content`, and
        `structuredContent` is added when the tool declares an `outputSchema` and
        the value carries an object form. The caller has already re-filtered a
        body that bypassed the route `response_model`.
        """
        try:
            blocks = _content_blocks(shaped)
        except _InBandError as exc:
            return _text_result(str(exc), is_error=True)
        if blocks is not None:
            return {"content": blocks}
        result = _text_result(_stringify(shaped, self._result_dumps))
        if tool.output_schema is not None:
            structured = _to_structured(shaped)
            if structured is not None:
                result["structuredContent"] = structured
        return result

    def _shape_result(self, tool: MCPTool, result: Any) -> Any:
        """Run a tool's return through the HTTP response shaping.

        A route-backed tool's return is shaped exactly as the HTTP path shapes
        it, so a field hidden on the HTTP response cannot leak over MCP. A
        returned `Response` is unwrapped to its actual body rather than an object
        repr; a pure tool's non-`Response` return is passed back unchanged.

        This shapes the value only - the caller re-runs a handler-built body
        through the `response_model` filter before advertising it as
        schema-conformant `structuredContent`.
        """
        route_info = tool.route_info

        # Mirroring `app._build_response`: a handler-built `Response` chose its body.
        if (
            route_info is not None
            and route_info.response_model is not None
            and not isinstance(result, Response)
        ):
            result = self.app._apply_response_model(result, route_info)

        if isinstance(result, Response):
            return _response_body_value(result)
        return result
