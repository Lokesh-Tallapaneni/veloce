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

## Handshake data and dependencies

The `WebSocket` exposes `query_params`, `headers`, `cookies`, `client`,
and `url` from the handshake request. `Depends()` works on WebSocket
handlers too, so authentication and shared setup are resolved the same
way as for HTTP routes.

## Testing WebSockets

The in-memory `TestClient` can drive a WebSocket without a network — see
[Testing](testing.md):

```python
client = app.test_client()
with client.websocket_connect("/ws") as ws:
    ws.send_text("hello")
    assert ws.receive_text() == "echo: hello"
```
