---
description: Validate JSON request bodies with Pydantic models — nested models, field constraints, extra value types, multiple body parameters, and partial updates.
tags: [pydantic, body, validation, models]
---

# Request models

Annotate a handler parameter with a [Pydantic](https://docs.pydantic.dev/) `BaseModel` and Veloce reads the JSON body, validates it against the model, and hands the handler a fully-typed object. This page covers nested models, field-level constraints, extra value types, multiple body parameters, and partial updates.

## Declaring a body model

A parameter annotated with a `BaseModel` subclass is bound from the JSON request body. Validation failures become a `422` response before the handler runs.

```python title="app.py"
from pydantic import BaseModel

from veloce import Veloce

app = Veloce()


class Item(BaseModel):
    name: str
    price: float
    in_stock: bool = True


@app.post("/items")
async def create_item(item: Item):
    return item.model_dump()
```

- The `item` parameter is bound from the whole JSON body.
- `in_stock: bool = True` has a default, so the client may omit it.
- `item.model_dump()` turns the validated model back into a plain dict.

A bad body is rejected with a structured `422` — see [Error handling](error-handling.md) for the response shape.

```python
from pydantic import BaseModel

from veloce import TestClient, Veloce

app = Veloce()


class Item(BaseModel):
    name: str
    price: float
    in_stock: bool = True


@app.post("/items")
async def create_item(item: Item):
    return item.model_dump()


client = TestClient(app)

ok = client.post("/items", json={"name": "Widget", "price": 9.99})
assert ok.status_code == 200
assert ok.json() == {"name": "Widget", "price": 9.99, "in_stock": True}

bad = client.post("/items", json={"name": "Widget"})
assert bad.status_code == 422
```

## Nested models

A model field can itself be a model, a `list` of models, or a `dict`. Pydantic validates the whole tree, so deeply structured bodies need no extra wiring.

```python title="app.py"
from pydantic import BaseModel

from veloce import Veloce

app = Veloce()


class Image(BaseModel):
    url: str
    alt: str


class Item(BaseModel):
    name: str
    price: float
    tags: list[str] = []
    images: list[Image] = []


@app.post("/items")
async def create_item(item: Item):
    return item.model_dump()
```

A body like `{"name": "Widget", "price": 9.99, "images": [{"url": "/a.png", "alt": "a"}]}` is validated end to end; a wrong type anywhere in the tree produces a `422` whose `loc` points at the offending field path.

!!! warning "A `list[Model]` body parameter is not supported"
    Annotating a parameter with `list[Item]` does **not** read a JSON array body — Veloce treats a list-typed parameter as a repeated query parameter. To accept a top-level JSON array, wrap it in a model field (`items: list[Item]`) and post an object, or read the array yourself with `await request.json()`.

## Validating model fields

Use Pydantic's `Field` to attach constraints (bounds, lengths, patterns) and schema metadata directly on a model field. Constraints are enforced during body validation.

```python title="app.py"
from pydantic import BaseModel, Field

from veloce import Veloce

app = Veloce()


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    price: float = Field(gt=0, description="Price in USD, must be positive.")
    quantity: int = Field(default=1, ge=0)


@app.post("/items")
async def create_item(item: Item):
    return item.model_dump()
```

A value that violates a constraint (for example `price` of `-1`) is rejected with a `422` whose `loc` names the field.

```python
from pydantic import BaseModel, Field

from veloce import TestClient, Veloce

app = Veloce()


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=50)
    price: float = Field(gt=0)


@app.post("/items")
async def create_item(item: Item):
    return item.model_dump()


client = TestClient(app)

bad = client.post("/items", json={"name": "Widget", "price": -1})
assert bad.status_code == 422
assert bad.json()["detail"][0]["loc"] == ["price"]
```

!!! note
    `Field` is a Pydantic symbol (`from pydantic import Field`), not a Veloce one. Veloce's own [`Body`](../reference/parameters.md#veloce.Body), [`Query`](../reference/parameters.md#veloce.Query), and [`Path`](../reference/parameters.md#veloce.Path) markers carry the same constraint keywords for scalar parameters that are not model fields — see [Parameters](parameters.md).

## Extra value types

A model field is not limited to `str`/`int`/`float`/`bool`. Pydantic parses and serialises richer types from JSON strings, including the standard-library types below.

| Type | Accepted JSON | Parsed Python value |
| --- | --- | --- |
| `datetime.datetime` | ISO 8601 string (`"2026-01-01T12:00:00"`) | `datetime` |
| `datetime.date` | ISO date string (`"2026-01-01"`) | `date` |
| `datetime.timedelta` | seconds number or ISO 8601 duration | `timedelta` |
| `uuid.UUID` | UUID string | `UUID` |
| `decimal.Decimal` | number or numeric string | `Decimal` |

```python title="app.py"
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from veloce import Veloce

app = Veloce()


class Order(BaseModel):
    id: UUID
    placed_at: datetime
    total: Decimal


@app.post("/orders")
async def create_order(order: Order):
    return {
        "id": str(order.id),
        "placed_at": order.placed_at.isoformat(),
        "total": str(order.total),
    }
```

The same types are accepted on scalar `Query`/`Path` parameters — Veloce coerces the raw string through Pydantic there too. See [Parameters](parameters.md) for the parameter-level form.

## Multiple body parameters

You can annotate more than one parameter with a model. Veloce binds each one differently from FastAPI — see the warning below.

```python title="app.py"
from pydantic import BaseModel

from veloce import Veloce

app = Veloce()


class User(BaseModel):
    name: str


class Item(BaseModel):
    title: str


@app.post("/orders")
async def create_order(user: User, item: Item):
    return {"user": user.model_dump(), "item": item.model_dump()}
```

Each model parameter is validated against the **entire** JSON body, not a key named after the parameter. The body above must be one flat object carrying every field: `{"name": "Ada", "title": "Widget"}`.

!!! warning "Multiple body models share the whole body — no auto-keying"
    FastAPI nests each model under its parameter name (`{"user": {...}, "item": {...}}`) when a path declares two or more body parameters. Veloce does **not**: every model parameter receives the same flat top-level body. Sending the FastAPI-style nested shape fails validation because the model fields are missing at the top level. When two payloads must travel together, define a single wrapper model with both as nested fields instead.

```python
from pydantic import BaseModel

from veloce import TestClient, Veloce

app = Veloce()


class User(BaseModel):
    name: str


class Item(BaseModel):
    title: str


@app.post("/orders")
async def create_order(user: User, item: Item):
    return {"user": user.model_dump(), "item": item.model_dump()}


client = TestClient(app)

flat = client.post("/orders", json={"name": "Ada", "title": "Widget"})
assert flat.status_code == 200
assert flat.json() == {"user": {"name": "Ada"}, "item": {"title": "Widget"}}

nested = client.post("/orders", json={"user": {"name": "Ada"}, "item": {"title": "Widget"}})
assert nested.status_code == 422
```

The robust pattern is a single wrapper model:

```python title="app.py"
from pydantic import BaseModel

from veloce import Veloce

app = Veloce()


class User(BaseModel):
    name: str


class Item(BaseModel):
    title: str


class Order(BaseModel):
    user: User
    item: Item


@app.post("/orders")
async def create_order(order: Order):
    return order.model_dump()
```

## Partial updates

For a `PATCH` route, declare an update model where every field is optional, then apply only the fields the client actually sent with `model_dump(exclude_unset=True)`.

```python title="app.py"
from pydantic import BaseModel

from veloce import Veloce

app = Veloce()

_item = {"name": "Widget", "price": 9.99, "tags": []}


class ItemUpdate(BaseModel):
    name: str | None = None
    price: float | None = None
    tags: list[str] | None = None


@app.patch("/item")
async def update_item(update: ItemUpdate):
    changes = update.model_dump(exclude_unset=True)
    _item.update(changes)
    return _item
```

- Every field defaults to `None`, so any subset of fields is a valid body.
- `exclude_unset=True` drops fields the client omitted, so a `PATCH` of `{"price": 12.5}` changes only `price` and leaves `name` and `tags` untouched.
- Use `model_dump()` (without `exclude_unset`) for a full `PUT` replacement instead.

```python
from pydantic import BaseModel

from veloce import TestClient, Veloce

app = Veloce()

_item = {"name": "Widget", "price": 9.99, "tags": []}


class ItemUpdate(BaseModel):
    name: str | None = None
    price: float | None = None
    tags: list[str] | None = None


@app.patch("/item")
async def update_item(update: ItemUpdate):
    changes = update.model_dump(exclude_unset=True)
    _item.update(changes)
    return _item


client = TestClient(app)

resp = client.patch("/item", json={"price": 12.5})
assert resp.status_code == 200
assert resp.json() == {"name": "Widget", "price": 12.5, "tags": []}
```

!!! warning "Pydantic dataclasses are not detected as body models"
    Only `pydantic.BaseModel` subclasses (and, when installed, `msgspec.Struct` types) are bound from the request body. A class decorated with `@pydantic.dataclasses.dataclass` is **not** a `BaseModel` subclass, so Veloce treats such a parameter as a query/path scalar rather than a body. Use a `BaseModel` for request bodies, or read the body yourself with `await request.json()`. For the high-performance `msgspec.Struct` backend, see [msgspec models](msgspec.md).

## Next steps

- [Parameters](parameters.md) — declare and validate query, path, header, and cookie values, plus scalar `Body` fields.
- [Requests and responses](requests-responses.md) — read the raw body, shape responses, and use `response_model=`.
- [Error handling](error-handling.md) — the `422` validation-error body and custom handlers.
- [msgspec models](msgspec.md) — validate bodies with `msgspec.Struct` instead of Pydantic.
- Full signatures are in the [API reference](../reference/index.md).
