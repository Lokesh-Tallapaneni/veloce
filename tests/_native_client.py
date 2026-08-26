"""An in-memory client for the native `HttpProtocol`, shaped like `TestClient`.

`TestClient` drives the ASGI entry point. `HttpProtocol` — the transport
`app.run()` and `VeloceWorker` use — has ~1400 lines of its own framing (HEAD
stripping, keep-alive, pipelining, `Expect: 100-continue`, chunked bodies, the
413/408/400 refusals) that no test reached unless it stood up a socket, or wrote
its own fake transport. Three files in this suite had written their own, and the
recorded pattern is that the serious defects cluster in exactly this file.

This is the shared door. It feeds real bytes to `data_received` and parses the
real bytes written back, so nothing about the framing is mocked out — the parts
that diverged from ASGI are precisely the parts it exercises.

Deliberately test-only rather than a public `NativeTestClient`. It closes the
coverage gap without committing the project to a second supported client;
promote it if users ask for one.

Usage::

    from tests._native_client import NativeClient

    resp = NativeClient(app).get("/items")
    assert resp.status_code == 200
"""

from __future__ import annotations

import asyncio
from typing import Any

import orjson

from veloce.serving.protocol import HttpProtocol

CRLF = b"\r\n"


class NativeTransport(asyncio.Transport):
    """Captures written bytes and records the flow-control calls made on it."""

    def __init__(self) -> None:
        super().__init__()
        self.writes: list[bytes] = []
        self.paused = 0
        self.resumed = 0
        self.reading = True
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def writelines(self, chunks) -> None:
        for chunk in chunks:
            self.write(chunk)

    def close(self) -> None:
        self.closed = True

    def is_closing(self) -> bool:
        return self.closed

    def pause_reading(self) -> None:
        self.paused += 1
        self.reading = False

    def resume_reading(self) -> None:
        self.resumed += 1
        self.reading = True

    def get_extra_info(self, name, default=None):
        return default

    @property
    def data(self) -> bytes:
        return b"".join(self.writes)


class NativeResponse:
    """One parsed HTTP/1.1 response, read off the wire the client actually saw."""

    __slots__ = ("status_code", "reason", "headers", "body", "raw")

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        head, _, body = raw.partition(CRLF + CRLF)
        lines = head.split(CRLF)
        parts = lines[0].split(b" ", 2)
        self.status_code = int(parts[1]) if len(parts) > 1 else 0
        self.reason = parts[2].decode("latin-1") if len(parts) > 2 else ""
        headers: dict[str, str] = {}
        for line in lines[1:]:
            name, _, value = line.partition(b":")
            key = name.decode("latin-1").strip().lower()
            text = value.decode("latin-1").strip()
            # A repeated header keeps every value, joined - `Set-Cookie` is the
            # one that matters and the last-wins dict would lose all but one.
            headers[key] = f"{headers[key]}, {text}" if key in headers else text
        self.headers = headers
        self.body = _dechunk(body) if headers.get("transfer-encoding") == "chunked" else body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self) -> Any:
        return orjson.loads(self.body)

    def __repr__(self) -> str:
        return f"<NativeResponse [{self.status_code}]>"


def _dechunk(body: bytes) -> bytes:
    """Reassemble a chunked body, so a caller compares payloads not framing."""
    out = bytearray()
    while True:
        line, _, rest = body.partition(CRLF)
        if not line:
            break
        size = int(line.split(b";", 1)[0], 16)
        if size == 0:
            break
        out += rest[:size]
        body = rest[size + len(CRLF) :]
    return bytes(out)


class NativeClient:
    """Drive one app through `HttpProtocol` over an in-memory transport.

    Each call opens a fresh connection unless `keep_alive=True` was passed, in
    which case the connection is reused so keep-alive and pipelining behaviour
    can be exercised.
    """

    def __init__(self, app: Any, *, keep_alive: bool = False) -> None:
        self.app = app
        self.keep_alive = keep_alive
        self.loop = asyncio.new_event_loop()
        self._protocol: HttpProtocol | None = None
        self._transport: NativeTransport | None = None
        # The ASGI client runs startup on construction; match it, so a lifespan
        # that seeds app state is present for the native door too.
        self.loop.run_until_complete(app._run_lifecycle("startup"))

    # ── connection ───────────────────────────────────────────────────

    def connect(self) -> tuple[HttpProtocol, NativeTransport]:
        """Open a connection, or return the live one under `keep_alive`."""
        if self.keep_alive and self._protocol is not None:
            assert self._transport is not None
            return self._protocol, self._transport
        protocol = HttpProtocol(self.app, self.loop)
        transport = NativeTransport()
        protocol.connection_made(transport)
        self._protocol, self._transport = protocol, transport
        return protocol, transport

    def close(self) -> None:
        for task in asyncio.all_tasks(self.loop):
            task.cancel()
        self.settle(2)
        self.loop.close()

    def __enter__(self) -> NativeClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── driving ──────────────────────────────────────────────────────

    def settle(self, turns: int = 40) -> None:
        """Run the loop until the protocol's tasks have made progress."""
        for _ in range(turns):
            self.loop.run_until_complete(asyncio.sleep(0))

    def send_raw(self, raw: bytes, *, turns: int = 40) -> bytes:
        """Feed exact bytes and return everything written back."""
        protocol, transport = self.connect()
        before = len(transport.writes)
        protocol.data_received(raw)
        self.settle(turns)
        return b"".join(transport.writes[before:])

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
        json: Any = None,
        version: str = "HTTP/1.1",
        turns: int = 40,
    ) -> NativeResponse:
        """Send one request and parse the response off the wire.

        `content` and `json` are named as `TestClient` names them, so one test
        can drive either door with the same call.
        """
        sent = headers.copy() if headers else {}
        sent.setdefault("Host", "testserver")
        if json is not None:
            content = orjson.dumps(json)
            sent.setdefault("Content-Type", "application/json")
        if content is not None:
            sent.setdefault("Content-Length", str(len(content)))
        head = f"{method.upper()} {path} {version}".encode("latin-1") + CRLF
        for name, value in sent.items():
            head += f"{name}: {value}".encode("latin-1") + CRLF
        return NativeResponse(self.send_raw(head + CRLF + (content or b""), turns=turns))

    def get(self, path: str, **kwargs) -> NativeResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> NativeResponse:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> NativeResponse:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs) -> NativeResponse:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs) -> NativeResponse:
        return self.request("DELETE", path, **kwargs)

    def head(self, path: str, **kwargs) -> NativeResponse:
        return self.request("HEAD", path, **kwargs)

    def options(self, path: str, **kwargs) -> NativeResponse:
        return self.request("OPTIONS", path, **kwargs)

    def pipeline(self, *requests: bytes, turns: int = 200) -> list[NativeResponse]:
        """Send several requests in one write and split the responses back out."""
        raw = self.send_raw(b"".join(requests), turns=turns)
        return [NativeResponse(part) for part in _split_responses(raw)]


def _split_responses(raw: bytes) -> list[bytes]:
    """Split a pipelined byte stream on each `HTTP/1.` status line."""
    out: list[bytes] = []
    start = raw.find(b"HTTP/1.")
    while start != -1:
        nxt = raw.find(b"HTTP/1.", start + 1)
        out.append(raw[start:] if nxt == -1 else raw[start:nxt])
        start = nxt
    return out
