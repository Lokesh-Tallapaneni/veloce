"""Sharing HTTP sessions between workers.

Without a backend a session lives in the process that minted it, so behind more
than one worker a client's second request can reach a worker that never saw its
id, which answers 404 and makes the client start over. A `SessionBackend` gives
the workers one place to agree on what a session id means.

Only the portable part travels: the lifecycle flag and the client's declared
identity. Subscriptions, listen streams, tasks and the in-flight registry belong
to the worker holding the connection - a task cannot be cancelled from a process
that is not running it - so each worker rebuilds those for itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from veloce import AsyncTestClient, Veloce
from veloce.contrib.mcp.transports.session_store import (
    HttpSessionStore,
    SessionBackend,
    SessionRecord,
)


class MemoryBackend:
    """A backend two stores can share, standing in for Redis in a test."""

    def __init__(self) -> None:
        self.records: dict[str, SessionRecord] = {}
        self.reads = 0
        self.writes = 0
        self.deletes = 0

    async def read(self, session_id: str) -> SessionRecord | None:
        self.reads += 1
        return self.records.get(session_id)

    async def write(self, session_id: str, record: SessionRecord, ttl: float) -> None:
        self.writes += 1
        self.records[session_id] = record

    async def delete(self, session_id: str) -> None:
        self.deletes += 1
        self.records.pop(session_id, None)


def _workers(backend: MemoryBackend) -> tuple[HttpSessionStore, HttpSessionStore]:
    """Two stores sharing one backend - two workers behind a load balancer."""
    return HttpSessionStore(backend=backend), HttpSessionStore(backend=backend)


# ── The contract ─────────────────────────────────────────────────────


def test_the_backend_protocol_is_satisfied_structurally():
    """Any object with the three methods is a backend; no base class to inherit."""
    assert isinstance(MemoryBackend(), SessionBackend)


def test_a_record_defaults_to_an_uninitialized_session():
    record = SessionRecord()
    assert record.initialized is False
    assert record.client_capabilities == {}
    assert record.client_info is None


# ── One worker mints, another serves ─────────────────────────────────


async def test_a_session_minted_on_one_worker_resolves_on_another():
    backend = MemoryBackend()
    first, second = _workers(backend)

    session_id, _session = await first.create()
    assert await second.resolve(session_id) is not None


async def test_an_unknown_id_is_still_unknown_everywhere():
    backend = MemoryBackend()
    _first, second = _workers(backend)
    assert await second.resolve("never-minted") is None


async def test_the_handshake_travels_to_the_other_worker():
    backend = MemoryBackend()
    first, second = _workers(backend)

    session_id, session = await first.create()
    session.record_initialize({"capabilities": {"sampling": {}}, "clientInfo": {"name": "probe"}})
    session.initialized = True
    await first.persist(session_id, session)

    adopted = await second.resolve(session_id)
    assert adopted is not None
    assert adopted.initialized is True
    assert adopted.supports("sampling") is True
    assert adopted.client_info == {"name": "probe"}


async def test_an_adopted_session_starts_with_its_own_connection_state():
    """Subscriptions and streams belong to the worker holding the connection."""
    backend = MemoryBackend()
    first, second = _workers(backend)

    session_id, session = await first.create()
    session.subscriptions.add("res://watched")
    session.listen_streams[1] = {}
    await first.persist(session_id, session)

    adopted = await second.resolve(session_id)
    assert adopted is not None
    assert adopted.subscriptions == set()
    assert adopted.listen_streams == {}


async def test_an_adopted_session_gets_its_own_connection_id():
    """Task and in-flight ownership must not alias across workers."""
    backend = MemoryBackend()
    first, second = _workers(backend)

    session_id, session = await first.create()
    adopted = await second.resolve(session_id)
    assert adopted is not None
    assert adopted.connection_id != session.connection_id


async def test_the_record_is_read_on_every_resolution():
    """Not cached: reading it is how a worker learns the session ended elsewhere."""
    backend = MemoryBackend()
    first, second = _workers(backend)

    session_id, _session = await first.create()
    await second.resolve(session_id)
    reads_after_adoption = backend.reads
    await second.resolve(session_id)
    await second.resolve(session_id)
    assert backend.reads == reads_after_adoption + 2


# ── Ending a session ends it everywhere ──────────────────────────────


async def test_terminating_on_one_worker_terminates_on_the_other():
    backend = MemoryBackend()
    first, second = _workers(backend)

    session_id, _session = await first.create()
    await second.resolve(session_id)
    assert await first.terminate(session_id) is True

    assert backend.records == {}
    # The other worker still holds a local copy, so it must consult the record
    # before serving again - which is why termination deletes it.
    assert await HttpSessionStore(backend=backend).resolve(session_id) is None


async def test_terminating_an_id_this_worker_never_saw_still_reports_success():
    """The DELETE may reach any worker; the client must not see a spurious 404."""
    backend = MemoryBackend()
    first, second = _workers(backend)

    session_id, _session = await first.create()
    assert await second.terminate(session_id) is True
    assert backend.records == {}


async def test_terminating_an_unknown_id_is_still_a_no_op():
    backend = MemoryBackend()
    store = HttpSessionStore(backend=backend)
    assert await store.terminate("never-minted") is False


# ── Idle eviction is local ───────────────────────────────────────────


async def test_local_eviction_does_not_end_a_session_other_workers_serve():
    """This worker going quiet is not the conversation ending."""
    backend = MemoryBackend()
    quiet = HttpSessionStore(idle_ttl=0.0, backend=backend)
    busy = HttpSessionStore(backend=backend)

    session_id, _session = await quiet.create()
    # A zero idle window drops the local entry on the next access...
    assert await busy.resolve(session_id) is not None
    await quiet.resolve(session_id)
    # ...but the shared record survives, so the session is still live.
    assert backend.records
    assert backend.deletes == 0


# ── Without a backend, nothing changes ───────────────────────────────


async def test_a_store_without_a_backend_keeps_its_sessions_to_itself():
    first, second = HttpSessionStore(), HttpSessionStore()
    session_id, _session = await first.create()
    assert await first.resolve(session_id) is not None
    assert await second.resolve(session_id) is None


async def test_persist_without_a_backend_does_nothing():
    store = HttpSessionStore()
    session_id, session = await store.create()
    await store.persist(session_id, session)
    assert await store.resolve(session_id) is session


# ── Through the transport ────────────────────────────────────────────


def _app() -> Veloce:
    app = Veloce(title="Shared", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="Add two numbers")
    async def add(a: int, b: int) -> int:
        return a + b

    return app


async def _post(client: Any, body: dict, session_id: str | None = None) -> Any:
    headers = {"accept": "application/json", "content-type": "application/json"}
    if session_id is not None:
        headers["mcp-session-id"] = session_id
    return await client.post("/mcp", json=body, headers=headers)


_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"capabilities": {"sampling": {}}},
}
_CALL = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
}


async def test_a_client_initialized_on_one_worker_is_served_by_another():
    """The end-to-end shape: two apps, one backend, one client conversation."""
    import orjson

    backend = MemoryBackend()
    worker_a, worker_b = _app(), _app()
    worker_a.mount_mcp(transport="http", sessions=True, session_backend=backend)
    worker_b.mount_mcp(transport="http", sessions=True, session_backend=backend)

    async with AsyncTestClient(worker_a) as client_a, AsyncTestClient(worker_b) as client_b:
        opened = await _post(client_a, _INIT)
        session_id = opened.headers["mcp-session-id"]

        served = await _post(client_b, _CALL, session_id)
        assert served.status_code == 200
        payload = orjson.loads(served.body)
        assert payload["result"]["content"][0]["text"] == "5"


async def test_without_a_backend_the_other_worker_answers_not_found():
    """The behaviour a backend exists to fix, pinned so it stays visible."""

    worker_a, worker_b = _app(), _app()
    worker_a.mount_mcp(transport="http", sessions=True)
    worker_b.mount_mcp(transport="http", sessions=True)

    async with AsyncTestClient(worker_a) as client_a, AsyncTestClient(worker_b) as client_b:
        opened = await _post(client_a, _INIT)
        session_id = opened.headers["mcp-session-id"]
        served = await _post(client_b, _CALL, session_id)
        assert served.status_code == 404


async def test_a_delete_on_one_worker_ends_the_session_on_the_other():

    backend = MemoryBackend()
    worker_a, worker_b = _app(), _app()
    worker_a.mount_mcp(transport="http", sessions=True, session_backend=backend)
    worker_b.mount_mcp(transport="http", sessions=True, session_backend=backend)

    async with AsyncTestClient(worker_a) as client_a, AsyncTestClient(worker_b) as client_b:
        opened = await _post(client_a, _INIT)
        session_id = opened.headers["mcp-session-id"]
        assert (await _post(client_b, _CALL, session_id)).status_code == 200

        ended = await client_b.delete("/mcp", headers={"mcp-session-id": session_id})
        assert ended.status_code == 204

        after = await _post(client_a, _CALL, session_id)
        assert after.status_code == 404


@pytest.mark.parametrize("method", ["read", "write", "delete"])
def test_an_incomplete_backend_is_not_a_backend(method: str):
    """The protocol is what the store calls; a partial object fails the check."""
    namespace = {
        name: (lambda self, *a, **k: None) for name in ("read", "write", "delete") if name != method
    }
    partial = type("Partial", (), namespace)()
    assert not isinstance(partial, SessionBackend)
