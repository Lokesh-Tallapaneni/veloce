---
description: Stream Server-Sent Events from Veloce with EventSourceResponse and ServerSentEvent — push named events, ids, retry hints, and keep-alive heartbeats to the browser EventSource API.
tags: [sse, streaming, events, eventsource]
---

# Server-Sent Events

Server-Sent Events (SSE) push a one-way stream of updates from the server to
the client over a single long-lived HTTP connection. The browser consumes
them with the native [`EventSource`](https://developer.mozilla.org/en-US/docs/Web/API/EventSource)
API. Veloce models them with [`EventSourceResponse`](../reference.md#veloce.EventSourceResponse),
which streams [`ServerSentEvent`](../reference.md#veloce.ServerSentEvent) objects
as they are produced.

## A first example

Return an `EventSourceResponse` wrapping an async generator that yields
events:

```python
import asyncio

from veloce import EventSourceResponse, Request, ServerSentEvent, Veloce

app = Veloce()


@app.get("/events")
async def events(request: Request):
    async def generate():
        for i in range(10):
            yield ServerSentEvent(data=f"Event {i}")
            await asyncio.sleep(1)

    return EventSourceResponse(generate())
```

Veloce sets the `Content-Type` to `text/event-stream`, disables caching and
proxy buffering, and sends each event down the open connection the moment
the generator yields it — nothing is buffered until the end.

On the browser side:

```javascript
const source = new EventSource("/events");
source.onmessage = (e) => console.log(e.data);
```

## The `ServerSentEvent` fields

[`ServerSentEvent`](../reference.md#veloce.ServerSentEvent) maps directly onto
the SSE wire format defined by the
[WHATWG HTML specification](https://html.spec.whatwg.org/multipage/server-sent-events.html).
The only required field is `data`; the rest are optional:

```python
from veloce import ServerSentEvent

event = ServerSentEvent(
    data="hello",
    event="greeting",   # a named event type the client can listen for
    id="42",            # last-event id, echoed back on reconnect
    retry=5000,         # client reconnection delay, in milliseconds
    comment="trace-id 7f3a",  # a colon-prefixed comment line clients ignore
)
```

`data` is now optional: pass `comment=...` with no `data` to emit a
comment-only event (a stream the client ignores but proxies can see). A
multi-line comment is split into one `: ` line per segment, matching the way
`data` is split.

Named events let the client subscribe to specific types instead of the
default `message`:

```python
import asyncio

from veloce import EventSourceResponse, Request, ServerSentEvent, Veloce

app = Veloce()


@app.get("/stream")
async def stream(request: Request):
    async def generate():
        yield ServerSentEvent(data="connected", event="open")
        yield ServerSentEvent(data="75", event="progress")
        await asyncio.sleep(0)
        yield ServerSentEvent(data="done", event="close")

    return EventSourceResponse(generate())
```

```javascript
const source = new EventSource("/stream");
source.addEventListener("progress", (e) => console.log("progress", e.data));
source.addEventListener("close", () => source.close());
```

Multi-line `data` is handled for you — a string containing newlines is split
into one `data:` field per line, which the browser rejoins, so JSON or
multi-line text streams correctly.

For structured payloads, `ServerSentEvent.json` serializes any JSON-encodable
value into the `data` field for you, so you do not have to call `json.dumps`
yourself. The `event`/`id`/`retry` fields are forwarded unchanged:

```python
from veloce import ServerSentEvent

event = ServerSentEvent.json({"x": 1, "y": 2}, event="update")
```

The plain `data=` constructor remains the raw escape hatch — it sends the
string through verbatim (no JSON quoting), which is what you want for
pre-formatted text, HTML fragments, or CSV lines.

## What the generator may yield

The iterator passed to `EventSourceResponse` may yield
[`ServerSentEvent`](../reference.md#veloce.ServerSentEvent) objects, plain
`str`, or already-encoded `bytes`. A `ServerSentEvent` is encoded into the SSE
field format; a `str` is UTF-8 encoded and sent verbatim; `bytes` are passed
through unchanged. For anything beyond raw text you almost always want
`ServerSentEvent`, so the `data:`/`event:`/`id:` framing is built correctly.

Any other yielded value is coerced into a single-`data:` event so a quick
producer never crashes the stream: a `dict` (or other `Mapping`) becomes a
`data:` field carrying its JSON, and a scalar such as an `int` or `float`
becomes a `data:` field carrying its text. Reach for `ServerSentEvent` when you
need an `event:`/`id:`/`retry:` field.

```python
from veloce import EventSourceResponse, Request, ServerSentEvent, Veloce

app = Veloce()


@app.get("/ticks")
async def ticks(request: Request):
    async def generate():
        yield ServerSentEvent(data="structured event")
        yield "data: raw line\n\n"  # raw str, sent as-is
        yield {"x": 1}  # dict -> data: {"x":1}
        yield 42  # scalar -> data: 42

    return EventSourceResponse(generate())
```

## Keep-alive heartbeats

Idle connections can be dropped by proxies and load balancers that close
silent sockets. Pass `ping=<seconds>` to emit a keep-alive comment frame
whenever no event is produced within that interval. The frame is an SSE
comment (a colon-prefixed line) that clients ignore, so it holds the
connection open without surfacing a spurious event:

```python
import asyncio

from veloce import EventSourceResponse, Request, ServerSentEvent, Veloce

app = Veloce()


@app.get("/slow")
async def slow(request: Request):
    async def generate():
        while True:
            data = await wait_for_next_update()
            yield ServerSentEvent(data=data)

    # send a heartbeat every 15s of silence
    return EventSourceResponse(generate(), ping=15)
```

Pass `ping_comment="..."` to set the text of the keep-alive frame (it must be
a single line and is only meaningful alongside `ping`):

```python
    return EventSourceResponse(generate(), ping=15, ping_comment="keepalive")


async def wait_for_next_update() -> str:
    await asyncio.sleep(30)
    return "update"
```

!!! warning "`ping` must be a finite positive number of seconds"
    `EventSourceResponse` raises `ValueError` for a `ping` that is zero,
    negative, `NaN`, or infinite.

    A zero or negative interval would time out instantly and flood the
    connection with heartbeat frames; an infinite interval would never fire.

    Use a sensible positive value (a few seconds to a minute) or omit `ping`
    entirely to disable heartbeats.

## Status code and headers

`EventSourceResponse` accepts a `status_code` and a `headers` mapping like
any other response. The SSE-specific headers (`Content-Type`,
`Cache-Control`, `Connection`, `X-Accel-Buffering`) are always applied, but
you can add your own alongside them:

```python
from veloce import EventSourceResponse, Request, ServerSentEvent, Veloce

app = Veloce()


@app.get("/events")
async def events(request: Request):
    async def generate():
        yield ServerSentEvent(data="hi")

    return EventSourceResponse(
        generate(),
        status_code=200,
        headers={"X-Stream": "demo"},
    )
```

## Testing an SSE endpoint

The in-memory [`TestClient`](testing.md) drains the stream and exposes the
full body, so you can assert on the encoded events:

```python
from veloce import EventSourceResponse, Request, ServerSentEvent, Veloce

app = Veloce()


@app.get("/sse")
async def sse(request: Request):
    async def generate():
        yield ServerSentEvent(data="hello", event="greeting")

    return EventSourceResponse(generate())


def test_sse():
    response = app.test_client().get("/sse")
    assert response.status_code == 200
    assert "text/event-stream" in response.content_type
    assert b"data: hello" in response.body
    assert b"event: greeting" in response.body
```

!!! note
    SSE is one-directional: server to client only. For bidirectional,
    real-time messaging — chat, collaborative editing, games — use
    [WebSockets](websockets.md) instead.

## Next steps

- [WebSockets](websockets.md) — full-duplex communication when one-way
  streaming is not enough.
- [Requests & Responses](requests-responses.md) — including
  `StreamingResponse` for streaming non-SSE payloads.
- [Background tasks](background-tasks.md) — run work after the response is
  sent.
- API reference: [`EventSourceResponse`](../reference.md#veloce.EventSourceResponse),
  [`ServerSentEvent`](../reference.md#veloce.ServerSentEvent).
