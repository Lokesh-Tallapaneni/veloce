---
description: Build WebSocket endpoints in Veloce with origin validation (CSWSH defense), dependency injection on connect, subprotocol negotiation, and an in-memory test client.
tags: [websocket, asgi, real-time]
---

# WebSockets

Veloce handles WebSocket connections natively over the ASGI WebSocket
scope — no separate server or add-on required.

## Declaring a WebSocket route

Use the `@app.websocket(...)` decorator. The handler receives a
`WebSocket` object:

```python title="websocket_app.py"
from veloce import Veloce

app = Veloce()


@app.websocket("/ws")
async def echo(ws):
    await ws.accept()
    message = await ws.receive_text()
    await ws.send_text(f"echo: {message}")
    await ws.close()
```

The connection lifecycle is explicit:

1. `await ws.accept()` — complete the handshake.
2. `receive_*` / `send_*` — exchange messages.
3. `await ws.close()` — end the connection (an optional `code` and
   `reason` may be passed).

## Sending and receiving

| Method                  | Direction | Payload                       |
|-------------------------|-----------|-------------------------------|
| `receive_text()`        | in        | text frame → `str`            |
| `receive_bytes()`       | in        | binary frame → `bytes`        |
| `receive_json()`        | in        | text frame parsed as JSON     |
| `send_text(...)`        | out       | text frame                    |
| `send_bytes(...)`       | out       | binary frame                  |
| `send_json(...)`        | out       | JSON, text or binary frame    |

For a long-lived connection, loop over the async iterators
(`iter_text()`, `iter_bytes()`, `iter_json()`):

```python
@app.websocket("/chat")
async def chat(ws):
    await ws.accept()
    async for message in ws.iter_text():
        await ws.send_text(f"you said: {message}")
```

## Declarative listener — `@app.websocket_listener`

When a handler is just "accept, then handle each message, then close on
disconnect", `@app.websocket_listener(path)` removes the boilerplate. The
decorated callback handles one message at a time; the framework owns the
handshake, the receive loop, and the clean close.

```python
@app.websocket_listener("/echo")
async def echo(data):
    return {"echo": data}
```

The callback is called as `cb(data)`, or `cb(ws, data)` when its first
parameter is named `ws`/`socket` (or it declares two positional parameters).
Returning a non-`None` value sends it back; returning `None` sends nothing, so
a pure consumer needs no special casing.

`receive` and `send` select the codec — `"json"` (default), `"text"`, or
`"bytes"`. `on_connect(ws)` runs after accept, and `on_disconnect(ws)` always
runs when the loop ends, including on peer disconnect. Sync callbacks and hooks
are offloaded to a thread, matching sync HTTP handlers.

```python
async def joined(ws): ...
async def left(ws): ...

@app.websocket_listener(
    "/room", receive="text", send="text", on_connect=joined, on_disconnect=left
)
async def room(data):
    return data.upper()
```

For full control over the handshake and loop, reach for the imperative
`@app.websocket` decorator above.

## Typing the messages

Annotate the callback's message parameter and each frame is validated before
your code runs:

```python
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from veloce import Veloce

app = Veloce()


class Join(BaseModel):
    type: Literal["join"]
    room: str


class Say(BaseModel):
    type: Literal["say"]
    text: str


Inbound = Annotated[Join | Say, Field(discriminator="type")]


@app.websocket_listener("/chat")
async def chat(message: Inbound) -> dict:
    if isinstance(message, Join):
        return {"joined": message.room}
    return {"said": message.text}
```

A frame that does not match closes the connection with
[`1007 Invalid Frame Payload Data`](https://www.rfc-editor.org/rfc/rfc6455#section-7.4.1),
and your callback never sees it. `on_disconnect` still runs.

!!! warning "A union of message types must be discriminated"

    A frame arrives as bytes and has to become exactly one of the declared
    types, so something must choose. An undiscriminated union - two message
    types with the same shape and no tag - is refused when the route is
    registered, rather than resolved by declaration order at runtime.

`msgspec.Struct` messages work the same way, tagged with `tag=` / `tag_field=`
instead of a `Literal` field. A union may not mix the two backends.

Dataclasses and `TypedDict`s are validated too — the same types the HTTP body
path accepts, through the same backend detection, so one annotation means the
same thing on both doors:

```python
from dataclasses import dataclass

from veloce import Veloce

app = Veloce()


@dataclass
class Move:
    x: int
    y: int


@app.websocket_listener("/move")
async def move(data: Move) -> dict:
    return {"x": data.x, "y": data.y}
```

!!! note "Use `typing_extensions.TypedDict` below Python 3.12"

    Pydantic refuses the `typing` spelling before 3.12 because only the
    backport records which keys are required. A `typing.TypedDict` message
    declares no contract there and its frames arrive unvalidated.

The return annotation documents what the channel sends. Unlike the receive
side it takes no discriminator and filters nothing: a union documents its
alternatives, and which member a value should be re-shaped through is
ambiguous. This matches how [`response_model`](openapi.md) unions already
behave on HTTP routes.

Typing is opt-in. A callback with no annotation receives the decoded payload
exactly as before, and `receive="text"` / `receive="bytes"` listeners are
unaffected.

!!! warning "An unresolvable message annotation is refused"

    A message type imported only under `TYPE_CHECKING`, or defined inside a
    function, cannot be resolved at registration — and a listener registered
    without it would accept every frame unvalidated. Veloce refuses the
    listener instead. Define the message type at module level, or import it at
    runtime.

!!! note "Added in version 0.19.0"

    Message annotations on `@app.websocket_listener` were previously ignored.

## Inbound validation and close codes

Incoming text frames are validated as UTF-8 at the parser boundary (RFC 6455
§8.1). A frame carrying invalid UTF-8 closes the connection with
`1007 Invalid Frame Payload Data` rather than surfacing a raw
`UnicodeDecodeError` from `receive_text()`. Binary frames are not validated.

When the peer closes, the close code and reason are exposed on the connection,
and the raised `WebSocketDisconnect` carries the peer's close code:

```python
import logging

from veloce import WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)


@app.websocket("/chat")
async def chat(ws: WebSocket):
    await ws.accept()
    try:
        async for message in ws.iter_text():
            await ws.send_text(message)
    except WebSocketDisconnect as exc:
        # exc.code, ws.close_code and ws.close_reason describe the peer close.
        log.info("closed %s: %s", ws.close_code, ws.close_reason)
```

`close_code` is `None` until the peer closes; an empty close payload records
`1005` ("no status received"). A malformed close code (below 1000, a reserved
code such as 1006, or an unassigned code below 3000) closes with
`1002 Protocol Error`, and a non-UTF-8 reason closes with `1007`.

## Idle-receive timeout

A peer that opens a connection and then goes silent ties up server resources
indefinitely. Pass `idle_timeout=<seconds>` when constructing the `WebSocket`
to bound how long any blocking receive (`receive`, `receive_text`,
`receive_bytes`, `receive_json`, and the `iter_*` loops) waits for the next
message. When no message arrives within the window the connection performs a
clean RFC 6455 close with `1001 Going Away` and the receive raises
`WebSocketDisconnect`, so the handler loop unwinds exactly as it would on a
peer-initiated close. The window bounds each complete message (under ASGI the
server delivers complete messages and handles ping/pong).

The handler receives a live `WebSocket`, so set the window with
`set_idle_timeout` (or tighten/relax it mid-connection):

```python
from veloce import Veloce, WebSocket, WebSocketDisconnect

app = Veloce()


@app.websocket("/chat")
async def chat(ws: WebSocket):
    ws.set_idle_timeout(30)  # close a peer silent for 30s
    await ws.accept()
    try:
        async for message in ws.iter_text():
            await ws.send_text(f"you said: {message}")
    except WebSocketDisconnect:
        pass  # idle close or peer close — both land here
```

A per-call `timeout` still applies; whichever deadline is smaller wins. A
smaller per-call `timeout` raises a plain `TimeoutError` and leaves the
connection open, while the idle window closing raises `WebSocketDisconnect`.

!!! note "Added in version 0.4"
    `idle_timeout` is opt-in. The default `None` preserves the previous
    unbounded behaviour. The value must be a finite positive number of
    seconds. It can also be supplied at construction via
    `WebSocket(..., idle_timeout=...)` and `WebSocket.from_asgi(...,
    idle_timeout=...)`.

## Proactive heartbeat

`idle_timeout` only fires while a receive is in flight, and a peer that
vanishes without sending a TCP FIN/RST (common behind NAT and load balancers)
can leave a connection half-open indefinitely. The fix is an active probe:
pass `heartbeat=<seconds>` when constructing a raw-transport `WebSocket`.

`heartbeat` is a construction-time option, so it applies to connections you
build by hand off the raw-transport path rather than to the live `WebSocket`
the framework hands an `@app.websocket` handler:

```python
from veloce import WebSocket

ws = WebSocket(transport, headers, heartbeat=20)
await ws.accept()  # arms the probe automatically
```

After `accept()` a timer sends an application PING carrying a token every
`heartbeat` seconds. The peer must answer with a PONG (or send any other
frame) before the next tick; any inbound byte defers the probe, so a busy
connection never pays for needless pings. Two consecutive idle windows with no
matching PONG drop the connection and record `1006` on `ws.close_code` (the
reserved abnormal-closure code is recorded but never sent on the wire). Call
`ws.start_heartbeat()` to arm the timer when you wire the transport yourself.

!!! note "Added in version 0.4"
    `heartbeat` is opt-in and raw-transport only. The default `None` preserves
    the previous behaviour, and the value is inert under ASGI, where the server
    owns ping/pong. The value must be a finite positive number of seconds.

## Subprotocol negotiation

Pick a subprotocol the client offered and confirm it during `accept`:

```python
@app.websocket("/ws")
async def negotiated(ws):
    chosen = ws.negotiate_subprotocol(["chat-v2", "chat-v1"])
    await ws.accept(subprotocol=chosen)
    await ws.send_text(chosen or "none")
    await ws.close()
```

!!! warning "ASGI server only"
    Confirming a subprotocol via `accept(subprotocol=...)` is supported only
    under an ASGI server (uvicorn / hypercorn). On the built-in `Veloce.run()`
    server it raises `RuntimeError`: that path writes the `101 Switching
    Protocols` response — including the `Sec-WebSocket-Protocol` header — before
    `accept()` runs, so the subprotocol cannot be chosen at `accept()` time.
    `negotiate_subprotocol(...)` (reading the client's offered list) works on
    both paths; only confirming one back is ASGI-only.

## Origin validation (CSWSH defence)

The WebSocket handshake is a plain HTTP/1.1 request, so neither
Same-Origin Policy nor CORS apply. A page on **any** origin can open a
socket to your app unless you check the handshake `Origin`. The attack
is Cross-Site WebSocket Hijacking (CSWSH); the defence is an allow-list.

Veloce ships two complementary APIs:

### Per-handler — `WebSocket.check_origin(allowed)`

Call before `accept()` and close on mismatch:

```python
@app.websocket("/ws")
async def chat(ws):
    if not ws.check_origin("https://app.example.com"):
        await ws.close(code=1008)  # policy violation
        return
    await ws.accept()
    async for msg in ws.iter_text():
        ...
```

`allowed` is a single origin string or an iterable of allowed origins.
Comparison is `.rstrip("/").lower()` on both sides, so
`"https://app.example.com"` matches `"https://APP.example.com/"`.

The literal `"*"` is the explicit "accept any origin" escape hatch — and it
**also accepts a missing or `null` `Origin`**, so reach for it only
when another check covers the same surface.

`Origin: null` (sandboxed
iframes, `file://` pages) is otherwise rejected, as is a missing
header — branch on `ws.origin is None` explicitly if you need to allow
non-browser clients.

### Registered-once — `WebSocketOriginMiddleware`

When every WebSocket route in your app shares the same allow-list,
register the middleware so the check runs before any handler:

```python
from veloce import Veloce
from veloce import WebSocketOriginMiddleware

app = Veloce()
app.add_middleware(
    WebSocketOriginMiddleware(
        allowed_origins=["https://app.example.com"],
        allow_missing=True,  # default; see note below
    )
)
```

The middleware closes the handshake with `1008` on a mismatch — same
contract as the per-handler helper. Plain HTTP requests pass straight
through; `Origin` enforcement for HTTP is `CORSMiddleware`'s job.

`allow_missing=True` (the default) still blocks every **browser-driven**
CSWSH attempt, because browsers always send `Origin` on the WebSocket
handshake (RFC 6455 §4.1) — what it lets through is non-browser clients
(mobile apps, service-to-service) that legitimately omit the header.
Set `allow_missing=False` only when the route should be browser-only;
otherwise the default is the safer choice.

### Picking between the two

The two APIs share normalisation (`.rstrip("/").lower()`, wildcard
`"*"`), so an allow-list written for one is reusable in the other.
They **differ on the default missing-`Origin` policy**: the per-handler
`check_origin` rejects missing origins (use `ws.origin is None` to opt
in), while the middleware accepts them unless you pass
`allow_missing=False`. A swap between the two is not policy-neutral —
read the previous paragraph before you switch.

Pick the per-handler form when only a few routes need the check, when
each route needs a different allow-list, or when you want
strict-by-default missing-`Origin` rejection. Pick the middleware when
one policy covers everything.

!!! warning "Heads-up"
    `SecurityHeadersMiddleware` is purely HTTP and does nothing for a
    WebSocket handshake. Add a `WebSocketOriginMiddleware` explicitly —
    there is no allow-list it could infer from the app.

## Handshake data and dependencies

The `WebSocket` exposes `query_params`, `headers`, `cookies`, `client`,
`origin`, and `url` from the handshake request. `Depends()` works on
WebSocket handlers too, so authentication and shared setup are resolved
the same way as for HTTP routes.

### Reaching the application

`ws.app` is the application serving the connection, mirroring
[`request.app`](../reference/requests.md#veloce.Request) — use it to read
`app.state`, config, or an installed extension:

```python
from veloce import Veloce, WebSocket

app = Veloce()
app.state.broker = object()


@app.websocket("/ws")
async def feed(ws: WebSocket) -> None:
    broker = ws.app.state.broker
    await ws.accept()
```

The [`current_app`](../reference/helpers.md#veloce.current_app) proxy resolves inside a
WebSocket handler too, so either works. Veloce does not populate an
`app` key in the ASGI `scope`, so `ws.scope["app"]` raises `KeyError`.

!!! note "Added in version 0.13"
    `ws.app`. Earlier versions reached the application only through
    `current_app`.

### Per-connection data

`ws.state` is the namespace for data that belongs to one connection — the
authenticated user, a subscription id, a per-socket counter. It accepts both
attribute and item syntax, and is discarded when the connection ends:

```python
from veloce import Veloce, WebSocket

app = Veloce()


@app.websocket("/ws")
async def chat(ws: WebSocket) -> None:
    await ws.accept()
    ws.state.user = ws.query_params.get("user", "anonymous")
    ws.state["room"] = ws.query_params.get("room", "lobby")

    async for message in ws.iter_text():
        await ws.send_text(f"[{ws.state['room']}] {ws.state.user}: {message}")
```

Assigning to the connection object itself (`ws.user = ...`) raises
`AttributeError`. `WebSocket` is slotted so that a server holding thousands of
open connections does not carry a per-connection `__dict__`, and `ws.state` is
the supported place for application data.

!!! note "Changed in version 0.13"
    `WebSocket` declares `__slots__`. Code that attached its own attributes
    directly to the connection object should move that data to `ws.state`.

!!! warning "Close before `accept()` loses the close code"
    A WebSocket close code travels in a close *frame*, which only exists once
    the handshake has completed. Closing before `accept()` therefore rejects the
    handshake at the HTTP layer, and the client sees an abnormal closure rather
    than the code you passed:

    ```python
    @app.websocket("/ws")
    async def guard(ws: WebSocket) -> None:
        if not authorised(ws):
            await ws.accept()                       # accept first,
            await ws.close(code=4001, reason="bad key")   # then close with the code
            return
        await ws.accept()
    ```

    Accept, then close, whenever the client needs to read a specific code such
    as `4001`. This is a property of the WebSocket handshake, not of Veloce.

## Testing WebSockets

The in-memory `TestClient` can drive a WebSocket without a network — see
[Testing](testing.md):

```python
client = app.test_client()
with client.websocket_connect("/ws") as ws:
    ws.send_text("hello")
    assert ws.receive_text() == "echo: hello"
```

## See also

- [Testing](testing.md#testing-websockets)
- [Middleware](middleware.md)
