# WebSockets

Veloce handles WebSocket connections natively over the ASGI WebSocket
scope — no separate server or add-on required.

## Declaring a WebSocket route

Use the `@app.websocket(...)` decorator. The handler receives a
`WebSocket` object:

```python
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
`"https://app.example.com"` matches `"https://APP.example.com/"`. The
literal `"*"` is the explicit "accept any origin" escape hatch
(use only when you have another check). `Origin: null` (sandboxed
iframes, `file://` pages) is rejected, as is a missing header — branch
on `ws.origin is None` explicitly if you need to allow non-browser
clients.

### Registered-once — `WebSocketOriginMiddleware`

When every WebSocket route in your app shares the same allow-list,
register the middleware so the check runs before any handler:

```python
from veloce import Veloce
from veloce.middleware.security import WebSocketOriginMiddleware

app = Veloce()
app.add_middleware(
    WebSocketOriginMiddleware(
        allowed_origins=["https://app.example.com"],
        allow_missing=True,  # accept handshakes with no Origin
    )
)
```

The middleware closes the handshake with `1008` on a mismatch — same
contract as the per-handler helper. Plain HTTP requests pass straight
through; `Origin` enforcement for HTTP is `CORSMiddleware`'s job.

The two APIs use the same normalisation, so allow-lists are
interchangeable. Pick the per-handler form when only a few routes need
the check or when each route needs a different allow-list; pick the
middleware when one policy covers everything.

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
