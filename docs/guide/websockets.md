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

## Inbound validation and close codes

Incoming text frames are validated as UTF-8 at the parser boundary (RFC 6455
§8.1). A frame carrying invalid UTF-8 closes the connection with
`1007 Invalid Frame Payload Data` rather than surfacing a raw
`UnicodeDecodeError` from `receive_text()`. Binary frames are not validated.

When the peer closes, the close code and reason are exposed on the connection,
and the raised `WebSocketDisconnect` carries the peer's close code:

```python
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
can leave a connection half-open indefinitely. On the raw-transport serving
path, pass `heartbeat=<seconds>` to actively probe the peer:

```python
@app.websocket("/chat")
async def chat(ws: WebSocket):
    ws = WebSocket(transport, headers, heartbeat=20)
    await ws.accept()
    ...
```

After `accept()` a timer sends an application PING carrying a token every
`heartbeat` seconds. The peer must answer with a PONG (or send any other
frame) before the next tick; any inbound byte defers the probe, so a busy
connection never pays for needless pings. Two consecutive idle windows with no
matching PONG drop the connection and record `1006` on `ws.close_code` (the
reserved abnormal-closure code is recorded but never sent on the wire). Call
`ws.start_heartbeat()` to arm the timer for a connection you build by hand.

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
    `app.use_secure_defaults()` does **not** register
    `WebSocketOriginMiddleware`. The helper sets cookie defaults and
    registers `SecurityHeadersMiddleware` (which is purely HTTP). Add a
    `WebSocketOriginMiddleware` explicitly — there is no allow-list it
    could infer from the app.

## Handshake data and dependencies

The `WebSocket` exposes `query_params`, `headers`, `cookies`, `client`,
`origin`, and `url` from the handshake request. `Depends()` works on
WebSocket handlers too, so authentication and shared setup are resolved
the same way as for HTTP routes.

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
