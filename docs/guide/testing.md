# Testing

Veloce ships an in-memory `TestClient`. It constructs ASGI scopes
directly and runs your handlers through the **real** `app.__call__`
surface — middleware, response encoding, cookies, and the lifespan
handshake all execute. No socket, no separate server.

## Creating a client

`app.test_client()` returns a client bound to your application:

```python
from veloce import Veloce

app = Veloce()


@app.get("/ping")
async def ping():
    return {"pong": True}


def test_ping():
    client = app.test_client()
    response = client.get("/ping")
    assert response.status_code == 200
    assert response.json() == {"pong": True}
```

You can also construct it explicitly:

```python
from veloce.testclient import TestClient

client = TestClient(app)
```

## Making requests

The client mirrors the HTTP verbs and returns a response object with
`status_code`, `json()`, `text`, `headers`, and `content_type`:

```python
client.get("/items", params={"page": 2})
client.post("/users", json={"name": "ada", "email": "ada@example.com"})
client.post("/upload", files={"f": ("data.csv", b"a,b,c")})
client.delete("/items/1")
```

Cookies set by the application persist across calls on the same client.

## Lifespan startup and shutdown

Use the client as a context manager to run `startup` / `shutdown`
events around the block:

```python
def test_with_lifespan():
    with app.test_client() as client:
        # startup handlers have run here
        assert client.get("/ping").status_code == 200
    # shutdown handlers have run here
```

## Testing WebSockets

`websocket_connect` opens a WebSocket against a `@app.websocket` route:

```python
def test_ws_echo():
    client = app.test_client()
    with client.websocket_connect("/ws") as ws:
        ws.send_text("hello")
        assert ws.receive_text() == "echo: hello"
```

## Overriding dependencies

Combine the client with `app.dependency_overrides` to swap real
dependencies (databases, auth, external APIs) for fakes — see
[Dependency Injection](dependency-injection.md#overriding-dependencies-in-tests).
