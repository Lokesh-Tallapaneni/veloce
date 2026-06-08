---
description: Turn models, dataclasses, datetime, UUID, and Decimal into JSON-safe values with jsonable_encoder, extend it with register_encoder, and swap the serialiser through the JSONProvider / DefaultJSONProvider interface and config_orjson_options.
tags: [json, encoding, serialisation, orjson]
---

# JSON and encoding

Veloce serialises responses with [orjson](https://github.com/ijl/orjson) and a
small conversion layer that turns rich Python objects — Pydantic models,
dataclasses, `datetime`, `UUID`, `Decimal`, `set`, `Path` — into JSON-safe
values. [`jsonable_encoder`](../reference.md#veloce.jsonable_encoder) exposes
that conversion directly, [`register_encoder`](../reference.md#veloce.register_encoder)
extends it for your own types, and the
[`JSONProvider`](../reference.md#veloce.JSONProvider) interface lets you replace
the serialiser entirely. orjson ships as a Veloce dependency, so there is
nothing extra to install.

## How a return value becomes JSON

When a handler returns a `dict`, `list`, or Pydantic model, Veloce wraps it in a
[`JSONResponse`](../reference.md#veloce.JSONResponse) and encodes it with orjson.
orjson handles the common leaf types (`str`, `int`, `float`, `bool`, `None`,
`datetime`, `UUID`, `Enum`, dataclass) at C speed; anything it cannot encode
natively — `set`, `Path`, `Decimal`, `bytes`, or a registered custom type — is
routed through Veloce's fallback so the encode succeeds instead of raising.

```python title="app.py"
from datetime import datetime
from uuid import UUID

from veloce import TestClient, Veloce

app = Veloce()


@app.get("/event")
async def event():
    return {
        "id": UUID("12345678-1234-5678-1234-567812345678"),
        "at": datetime(2026, 1, 1, 12, 0, 0),
        "tags": {"a", "b"},
    }


client = TestClient(app)

resp = client.get("/event")
assert resp.status_code == 200
assert resp.json() == {
    "id": "12345678-1234-5678-1234-567812345678",
    "at": "2026-01-01T12:00:00",
    "tags": ["a", "b"],  # sets serialise as a sorted list
}
```

A bare `str` return is sent as `text/html`, not JSON; return a `dict`/`list` or
a [`JSONResponse`](../reference.md#veloce.JSONResponse) when you want a JSON body.
See [Requests and responses](requests-responses.md) for the full return-value
rules.

## jsonable_encoder

Use [`jsonable_encoder`](../reference.md#veloce.jsonable_encoder) when you need
the JSON-safe form of an object *before* it reaches a response — for example to
store it, log it, or pass it to a non-orjson serialiser. Unlike the response
path, it walks the whole object graph and returns plain `dict`/`list`/scalar
values.

```python
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from veloce import jsonable_encoder


class Item(BaseModel):
    name: str
    price: Decimal
    created: datetime


data = jsonable_encoder(Item(name="book", price=Decimal("9.99"), created=datetime(2026, 1, 1)))
# {"name": "book", "price": 9.99, "created": "2026-01-01T00:00:00"}
```

The built-in conversions:

| Input type | JSON output |
| --- | --- |
| `datetime` / `date` / `time` | ISO 8601 string (`.isoformat()`). |
| `timedelta` | Total seconds as a `float`. |
| `UUID`, `Path`, `IPv4Address`, `re.Pattern` | Their `str()` form. |
| `Decimal` | `int` when integer-valued, else `float` (lossless). |
| `Enum` | The member's `.value`. |
| `bytes` / `bytearray` | Base64 string (lossless, JSON Schema `format: byte`). |
| `set` / `frozenset` | A list, sorted for deterministic output. |
| `BaseModel` / dataclass | Its fields as a `dict`. |

!!! note
    `bytes` encode as **base64**, not a UTF-8 decode. A UTF-8 decode would
    substitute `U+FFFD` for any non-text byte and corrupt image headers, hash
    digests, or gzip blobs; base64 round-trips every byte exactly.

### Filtering fields

`include` and `exclude` are sets of key names applied at **every depth**, not
just the top level. `exclude_unset`, `exclude_defaults`, and `exclude_none`
forward to Pydantic's `model_dump` for model inputs; `exclude_none` also drops
`None`-valued keys from plain dicts.

```python
from pydantic import BaseModel

from veloce import jsonable_encoder


class User(BaseModel):
    name: str
    password: str
    bio: str | None = None


user = User(name="ada", password="secret")
jsonable_encoder(user, exclude={"password"}, exclude_none=True)
# {"name": "ada"}
```

### Per-call custom encoders

Pass `custom_encoder={type: fn}` to override a conversion for a single call. It
is consulted before every built-in rule, at every depth: the exact `type(obj)`
wins, otherwise entries are scanned in insertion order for the first
`isinstance` match.

```python
from datetime import datetime

from veloce import jsonable_encoder

jsonable_encoder(
    {"at": datetime(2026, 1, 1, 12, 0, 0)},
    custom_encoder={datetime: lambda dt: int(dt.timestamp())},
)
# {"at": 1767268800}
```

!!! warning "Circular references raise"
    `jsonable_encoder` raises `ValueError` on a self-referential object graph
    (a container that transitively contains itself) rather than recursing until
    the stack overflows.

!!! warning "Secrets are refused"
    A [`Secret`](../reference.md#veloce.Secret) reaching either JSON path raises
    `TypeError`. Call `.reveal()` explicitly at the point you intend to expose
    the value, so a secret never leaks into a response by accident.

## Registering an encoder for a custom type

[`register_encoder`](../reference.md#veloce.register_encoder) installs a
process-wide encoder for a type and its subclasses. It applies to both
`jsonable_encoder` and the response path, so a registered type serialises the
same way everywhere. Registering a type that already has a built-in handler
overrides it.

```python
from veloce import TestClient, Veloce, jsonable_encoder, register_encoder


class Color:
    def __init__(self, r: int, g: int, b: int) -> None:
        self.r, self.g, self.b = r, g, b


register_encoder(Color, lambda c: f"#{c.r:02x}{c.g:02x}{c.b:02x}")

app = Veloce()


@app.get("/brand")
async def brand():
    return {"primary": Color(255, 0, 0)}


assert jsonable_encoder(Color(0, 128, 255)) == "#0080ff"

client = TestClient(app)
assert client.get("/brand").json() == {"primary": "#ff0000"}
```

The encoder receives one instance and must return a JSON-able value (a scalar,
or a list/dict of such). Resolution walks the type's MRO, so subclasses of a
registered type are covered too. Remove a registration with
[`unregister_encoder`](../reference.md#veloce.unregister_encoder); it is a no-op
if the type was never registered.

```python
from veloce import unregister_encoder

unregister_encoder(Color)
```

!!! note
    Registration is global to the process and persists across requests. Register
    your encoders once at startup, not per request.

## Choosing the serialiser with JSONProvider

`app.json` is the active [`JSONProvider`](../reference.md#veloce.JSONProvider).
Veloce's default is [`DefaultJSONProvider`](../reference.md#veloce.DefaultJSONProvider),
an orjson-backed provider, instantiated lazily on first access. A provider
exposes three methods:

| Method | Purpose |
| --- | --- |
| `dumps(obj, **kwargs)` | Serialise `obj` to JSON `bytes`. |
| `loads(data)` | Parse JSON `bytes`/`str` into Python objects. |
| `response(value, **kwargs)` | Build a `Response` carrying `value` as JSON. |

Subclass `JSONProvider` to plug in a different serialiser, then point the app at
it. Set `app.json_provider_class` to a class (instantiated lazily) or assign
`app.json` an instance directly.

```python title="app.py"
import json
from typing import Any

from veloce import JSONProvider, TestClient, Veloce


class StdlibJSONProvider(JSONProvider):
    def dumps(self, obj: Any, **kwargs: Any) -> bytes:
        return json.dumps(obj).encode()

    def loads(self, data: bytes | str) -> Any:
        return json.loads(data)


app = Veloce()
app.json_provider_class = StdlibJSONProvider


@app.get("/ping")
async def ping():
    return app.json.response({"pong": True})


client = TestClient(app)
assert client.get("/ping").json() == {"pong": True}
```

!!! note
    `dumps` returns `bytes` (not `str`) so callers can write straight to a
    response body without re-encoding. The base `response()` calls `dumps` and
    wraps the result in a `JSONResponse` via `from_bytes`, so your dump options
    survive without a re-encode.

## Tuning the default provider

[`DefaultJSONProvider`](../reference.md#veloce.DefaultJSONProvider) reads two
[config](configuration.md) flags into an orjson option bitmask when it is first
instantiated. [`config_orjson_options`](../reference.md#veloce.config_orjson_options)
builds that bitmask and is shared with the `jsonify` helper so the two paths
cannot drift.

| Config key | orjson option | Effect |
| --- | --- | --- |
| `JSON_SORT_KEYS` | `OPT_SORT_KEYS` | Sort object keys for deterministic output. |
| `JSONIFY_PRETTYPRINT_REGULAR` | `OPT_INDENT_2` | Indent output by two spaces. |

```python title="app.py"
from veloce import TestClient, Veloce

app = Veloce()
app.config["JSON_SORT_KEYS"] = True


@app.get("/")
async def index():
    return {"b": 2, "a": 1}


client = TestClient(app)
assert client.get("/").body == b'{"a":1,"b":2}'
```

!!! warning "Set the flags before the first `app.json` access"
    The default provider caches its option bitmask the first time `app.json` is
    read. Set these flags during application setup; mutating them after the
    first response does not retroactively change the cached provider. Per-call
    `sort_keys` / `indent` keyword arguments to `dumps` are still applied on top.

## Next steps

- Return JSON, HTML, and streaming bodies from handlers — see [Requests and responses](requests-responses.md).
- Validate and shape request/response bodies with models — see [Request models](request-models.md).
- Swap in a Struct-based encoder for hot endpoints — see [msgspec](msgspec.md).
- Full signatures are in the [API reference](../reference.md).
