# Veloce examples

Small, complete, single-file applications. Each one runs on its own with no
external services — the data stores are in-memory dicts and the HTML clients
are served inline.

## Setup

```bash
pip install veloceframework
```

## Running an example

Each file starts its own development server under `if __name__ == "__main__"`,
so run any of them directly:

```bash
python examples/crud_api.py
```

The app listens on `http://localhost:8000`. Stop it with `Ctrl+C`.

## The examples

| File | What it shows |
|------|---------------|
| [`crud_api.py`](crud_api.py) | A REST CRUD API: Pydantic request bodies, typed path parameters, dependency injection for shared state, and `HTTPException` for errors. |
| [`auth_jwt.py`](auth_jwt.py) | Token auth end to end: password hashing with `hash_password`/`verify_password`, signing a JWT with `encode_jwt`, and a protected route that verifies the Bearer token with `HTTPBearer` + `decode_jwt`. |
| [`websocket_chat.py`](websocket_chat.py) | A broadcast WebSocket chat room with a browser client, using the imperative `@app.websocket` API and `iter_text()`. |
| [`sse_feed.py`](sse_feed.py) | A Server-Sent Events live feed with `EventSourceResponse` and `ServerSentEvent`, including named events and a keep-alive `ping`. |
| [`file_upload.py`](file_upload.py) | A multipart upload saved to disk: `Form`/`File`/`UploadFile` parameters, `secure_filename`, and `UploadFile.save`. |

## Learning path

If you are new to Veloce, start with the
[tutorial](../docs/tutorial/index.md) — it builds one app step by step. Come
back to these examples once you want to see complete, focused apps for a
specific feature.
