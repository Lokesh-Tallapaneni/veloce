"""MCP tasks — run a long tool call in the background and poll it for a result.

The Model Context Protocol's task augmentation lets a client start a `tools/call`
it does not want to block on: the client sends the call with a ``task`` field, the
server answers immediately with a `CreateTaskResult` carrying a task id, runs the
handler detached, and the client retrieves the outcome later with ``tasks/result``
(or watches progress with ``tasks/get`` and ``notifications/tasks/status``).

The handler invocation is the *same* one a synchronous call uses - the background
runner calls the server's shared `tools/call` result builder - so a tool exposed
from a route stays one handler behind both doors: the HTTP request, the
synchronous tool call, and the task all run the identical dispatch path. Task
support is opt-in per tool (`@app.mcp_tool(task_support=True)` or
``mcp_task_support=True`` on a route); a tool that does not opt in advertises
``execution.taskSupport: "forbidden"`` and rejects a task-augmented call.

A task moves through the spec's lifecycle - ``working`` while the handler runs,
then ``completed`` / ``failed`` on its outcome, or ``cancelled`` after
``tasks/cancel``. ``input_required`` is part of the modelled status set for a tool
that needs follow-up, though the framework never enters it on its own. Each task
carries the ``io.modelcontextprotocol/related-task`` ``_meta`` key on its status
notifications so a client can correlate them, and the `CreateTaskResult` carries
the optional ``io.modelcontextprotocol/model-immediate-response`` hint.

Tasks are kept in memory for a bounded time-to-live so a never-polled task does
not leak; an expired task is evicted lazily on the next task method.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from veloce.contrib.mcp._registry_base import Registry
from veloce.contrib.mcp.capabilities.base import Capability
from veloce.contrib.mcp.descriptors import MCPDescriptor
from veloce.contrib.mcp.errors import InvalidParamsError, ResourceNotFoundError

if TYPE_CHECKING:  # pragma: no cover
    from veloce.contrib.mcp.server import MCPServer, MethodHandler

# The task status values the spec defines. `working` is the only non-terminal
# state the framework drives a task into on its own; `input_required` is modelled
# for a tool that needs client follow-up but is never entered automatically.
STATUS_WORKING = "working"
STATUS_INPUT_REQUIRED = "input_required"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# The two non-terminal states a task may legitimately sit in; everything else is
# terminal (its result final, `tasks/cancel` a no-op). Defining terminal as the
# complement keeps the lifecycle set in one place: a new non-terminal state need
# only be added here.
_NON_TERMINAL_STATUSES = frozenset({STATUS_WORKING, STATUS_INPUT_REQUIRED})
TASK_STATUSES = frozenset(
    {
        STATUS_WORKING,
        STATUS_INPUT_REQUIRED,
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELLED,
    }
)
_TERMINAL_STATUSES = TASK_STATUSES - _NON_TERMINAL_STATUSES

# Default time-to-live (seconds) a created task is retained before eviction, used
# when the client requests none. A client may shorten or extend it per call via
# the task `ttl`; the server keeps the requested value verbatim.
_DEFAULT_TASK_TTL_SECONDS = 300

# Suggested client poll interval (milliseconds) reported on a working task so a
# client paces its `tasks/get` polling instead of busy-looping.
_POLL_INTERVAL_MS = 500

# MCP `_meta` key correlating a message with the task it belongs to. Set on every
# `notifications/tasks/status` so a client can route the notification to the task.
META_RELATED_TASK = "io.modelcontextprotocol/related-task"

# MCP `_meta` hint on a `CreateTaskResult` telling the client an immediate model
# turn is not expected - the task runs in the background. Advisory, non-binding.
_META_MODEL_IMMEDIATE_RESPONSE = "io.modelcontextprotocol/model-immediate-response"


def _now_iso() -> str:
    """Return the current UTC time as an RFC 3339 timestamp."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(slots=True)
class MCPTask(MCPDescriptor):
    """One in-flight or settled task created from a task-augmented tool call.

    `name` (inherited) is the task id the client polls by. The task records its
    lifecycle status, the tool it runs, its created / last-updated timestamps and
    time-to-live, and - once it settles - the `tools/call` result the client
    retrieves with ``tasks/result``.
    """

    tool_name: str = ""
    status: str = STATUS_WORKING
    status_message: str | None = None
    created_at: str = field(default_factory=_now_iso)
    last_updated_at: str = field(default_factory=_now_iso)
    ttl_ms: int = _DEFAULT_TASK_TTL_SECONDS * 1000
    expires_at: float = 0.0
    # Identity of the connection that created the task (the dispatching session's
    # `connection_id`, or `None` off a connection). The id is process-unique and
    # never recycled, so a task that outlives its evicted owner cannot be matched
    # by a later session. A task method from a different connection cannot see or
    # act on this task, so one HTTP client cannot read another's result, cancel
    # another's work, or enumerate another's tasks.
    owner_key: int | None = None
    # The settled `tools/call` result, set when the task reaches a terminal
    # status; `None` while the task is still working.
    result: dict[str, Any] | None = None
    # The running asyncio task, kept so `tasks/cancel` can unwind it; cleared once
    # the task settles. Typed loosely to avoid importing asyncio at module load.
    runner: Any = None

    def describe(self) -> dict[str, Any]:
        """Shape this task into the MCP Task object the task methods return."""
        task: dict[str, Any] = {
            "taskId": self.name,
            "status": self.status,
            "createdAt": self.created_at,
            "lastUpdatedAt": self.last_updated_at,
            "ttl": self.ttl_ms,
        }
        if self.status_message is not None:
            task["statusMessage"] = self.status_message
        # A working task suggests a poll cadence so the client does not busy-loop;
        # a settled task needs no further polling, so the hint is omitted.
        if self.status == STATUS_WORKING:
            task["pollInterval"] = _POLL_INTERVAL_MS
        return task

    def is_terminal(self) -> bool:
        """Return whether the task has settled (no further transition happens)."""
        return self.status in _TERMINAL_STATUSES

    def settle(self, status: str, result: dict[str, Any], message: str | None = None) -> None:
        """Move the task to a terminal status carrying its final result."""
        self.status = status
        self.status_message = message
        self.result = result
        self.last_updated_at = _now_iso()
        self.runner = None


@dataclass(slots=True)
class TaskRegistry(Registry[MCPTask]):
    """Task id -> `MCPTask`, the in-memory store of created tasks."""

    tasks: dict[str, MCPTask] = field(default_factory=dict)

    @property
    def _store(self) -> dict[str, MCPTask]:
        return self.tasks

    def _key(self, item: MCPTask) -> str:
        return item.name

    def _duplicate_message(self, key: str) -> str:
        return f"Duplicate MCP task id {key!r}."

    def evict_expired(self) -> None:
        """Drop tasks whose time-to-live has elapsed so a stale task does not leak.

        Only a settled task is evicted on expiry; a still-working task is left in
        place even past its ttl so its eventual result is never discarded out
        from under a client that is still polling.
        """
        if not self.tasks:
            return
        now = time.monotonic()
        expired = [
            key
            for key, task in self.tasks.items()
            if task.expires_at and now >= task.expires_at and task.is_terminal()
        ]
        for key in expired:
            del self.tasks[key]

    def owned_by(self, owner_key: int | None) -> list[MCPTask]:
        """Return the tasks created by the given connection.

        Used to reclaim a session's tasks when it is evicted so a never-settling
        task does not pin memory for the process lifetime after its owner is gone.
        """
        return [task for task in self.tasks.values() if task.owner_key == owner_key]

    def drop(self, task: MCPTask) -> None:
        """Remove a task from the store, settled or not (used on owner eviction)."""
        self.tasks.pop(task.name, None)


def task_ttl_ms(params: dict[str, Any]) -> int:
    """Return the requested task time-to-live in milliseconds, or the default.

    The client may attach a ``ttl`` (milliseconds) to the ``task`` field; an
    absent or non-positive value falls back to the server default so a task is
    always retained for a bounded, sensible window.
    """
    task_field = params.get("task")
    if isinstance(task_field, dict):
        ttl = task_field.get("ttl")
        if isinstance(ttl, int) and not isinstance(ttl, bool) and ttl > 0:
            return ttl
    return _DEFAULT_TASK_TTL_SECONDS * 1000


def new_task(tool_name: str, ttl_ms: int, owner_key: int | None = None) -> MCPTask:
    """Create a fresh `working` task for `tool_name` with the given time-to-live.

    `owner_key` is the creating connection's identity; the task methods reject a
    caller whose connection does not match it, so a task is private to its creator.
    """
    task = MCPTask(name=uuid.uuid4().hex, description="", tool_name=tool_name)
    task.ttl_ms = ttl_ms
    task.expires_at = time.monotonic() + ttl_ms / 1000.0
    task.owner_key = owner_key
    return task


def create_task_result(task: MCPTask) -> dict[str, Any]:
    """Build the `CreateTaskResult` returned immediately for a task-augmented call.

    Carries the new task object plus the non-binding model-immediate-response
    hint so the client knows no immediate model turn is expected.
    """
    return {
        "task": task.describe(),
        "_meta": {_META_MODEL_IMMEDIATE_RESPONSE: True},
    }


def status_notification(task: MCPTask) -> dict[str, Any]:
    """Build the ``notifications/tasks/status`` message for a task transition.

    Carries the task object and the ``io.modelcontextprotocol/related-task``
    ``_meta`` key so the client can correlate the notification with its task.
    """
    return {
        "jsonrpc": "2.0",
        "method": "notifications/tasks/status",
        "params": {
            "task": task.describe(),
            "_meta": {META_RELATED_TASK: {"taskId": task.name}},
        },
    }


class TasksCapability(Capability):
    """The ``tasks/get|result|list|cancel`` methods and the tasks advertisement.

    Advertised only when at least one tool opts into task support, so a server
    whose tools all run synchronously stays inert and a client never probes an
    empty capability.
    """

    __slots__ = ("_server",)

    def __init__(self, server: MCPServer) -> None:
        self._server = server

    def advertise(self) -> dict[str, Any] | None:
        if not any(tool.task_support for tool in self._server.registry.tools.values()):
            return None
        # `list` / `cancel` are the optional sub-capabilities this server answers;
        # `requests.tools/call` declares that a `tools/call` may be task-augmented.
        return {"tasks": {"list": {}, "cancel": {}, "requests": {"tools/call": {}}}}

    def handlers(self) -> dict[str, MethodHandler]:
        return {
            "tasks/get": self._get,
            "tasks/result": self._result,
            "tasks/list": self._list,
            "tasks/cancel": self._cancel,
        }

    async def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return a task's current status object (``tasks/get`` polling)."""
        task = self._lookup(params)
        return task.describe()

    async def _result(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return a settled task's `tools/call` result (``tasks/result``).

        A task still working has no result yet, which is an invalid-params error
        so the client keeps polling ``tasks/get`` until the status is terminal.
        """
        task = self._lookup(params)
        if task.result is None:
            raise InvalidParamsError(
                f"Task {task.name!r} is still {task.status}; poll tasks/get until it settles."
            )
        return task.result

    async def _list(self, params: dict[str, Any]) -> dict[str, Any]:
        """List the calling connection's own tasks (``tasks/list``).

        Scoped to the caller: a task is returned only when its owning connection is
        the one dispatching, so one client never enumerates another's tasks.
        """
        store = self._server._tasks
        store.evict_expired()
        owner_key = self._caller_key()
        return {
            "tasks": [
                task.describe() for task in store.tasks.values() if task.owner_key == owner_key
            ]
        }

    async def _cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        """Cancel a running task and return its (now cancelled) status object.

        Cancelling a task already settled is a no-op that returns its terminal
        status, matching the spec's expectation that a client may race a cancel
        against natural completion.
        """
        task = self._lookup(params)
        await self._server._cancel_task(task)
        return task.describe()

    def _lookup(self, params: dict[str, Any]) -> MCPTask:
        """Resolve a ``taskId`` param to the caller's own task, evicting expired first.

        A task created by a different connection is treated as unknown - so a client
        cannot read, retrieve, or cancel a task it does not own even if it learns or
        guesses the id - rather than leaking that the id exists.
        """
        store = self._server._tasks
        store.evict_expired()
        task_id = params.get("taskId")
        if not isinstance(task_id, str):
            raise InvalidParamsError("task method requires a string 'taskId'")
        task = store.get(task_id)
        if task is None or task.owner_key != self._caller_key():
            raise ResourceNotFoundError(f"Unknown task: {task_id}")
        return task

    def _caller_key(self) -> int | None:
        """Return the dispatching connection's identity, scoping task ownership."""
        session = self._server.current_session()
        return session.connection_id if session is not None else None
