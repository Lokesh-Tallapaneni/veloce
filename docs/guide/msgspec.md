---
description: Validate request bodies and serialise responses with msgspec.Struct instead of Pydantic on hot paths, chosen per endpoint by type.
tags: [msgspec, performance, validation, serialisation]
---

# msgspec (fast backend)

Veloce validates request bodies and serializes responses with Pydantic by
default. For hot paths it can also use [msgspec](https://jcristharif.com/msgspec/),
which decodes and encodes in C with no intermediate dict. The backend is chosen
**per endpoint, by type** — there is no global switch — so Pydantic and msgspec
endpoints coexist in one app.

## Install

msgspec is an optional extra:

```bash
pip install veloceframework[fast]
```

Without it, every msgspec code path is inert and Veloce behaves exactly as
before. Pydantic stays the default and is always required.

## Request bodies

Type a body parameter as a `msgspec.Struct` instead of a Pydantic `BaseModel`:

```python
import msgspec

from veloce import Veloce

app = Veloce()


class Address(msgspec.Struct):
    city: str
    zip: str


class User(msgspec.Struct):
    id: int
    name: str
    address: Address


@app.post("/users")
async def create(user: User):
    return {"id": user.id, "city": user.address.city}
```

The body is decoded straight into the `User` struct. Type errors (`"id": "x"`
where an `int` is required), malformed JSON, and a missing required field each
produce a `422`. A struct body typed `User | None` with an empty request body
resolves to `None`.

## Responses

Return a struct (or a list of structs) and it is encoded directly:

```python
@app.get("/users")
async def listing() -> list[User]:
    return [User(id=1, name="ada", address=Address(city="London", zip="SW1"))]
```

The `(body, status)` and `(body, status, headers)` tuple shapes work with a
struct body, and a custom `response_class` renders the struct through the
requested class. A `response_model=User` (or `response_model=list[User]`)
declaration documents the response in `/docs`.

## OpenAPI

Struct request bodies and declared `response_model` structs — including
`list[Struct]` — emit component schemas under `components.schemas`, with nested
structs resolved by `$ref`, at parity with Pydantic models. A return annotation
supplies the response model here as it does for Pydantic, so a handler annotated
`-> list[User]` documents an array of `$ref` without declaring
`response_model=`.

## Parity and the one difference

Choosing msgspec is a performance choice, not a feature trade-off: validation
behavior, response shaping, and OpenAPI schemas match the Pydantic path. The one
difference is the validation-error shape — msgspec reports the offending field
path inside the error **message** (`"Expected int, got str - at $.id"`) rather
than the structured `loc` list Pydantic produces. The error `loc` is always
`["body"]`; the full path is preserved in `msg`.

## Not yet supported

- `list[Struct]` **request bodies** (a bare `list[T]` body still routes to the
  query string, unchanged). `list[Struct]` **responses** are supported.
- attrs / dataclass models.

## When to use which

Reach for `msgspec.Struct` on validation- or serialization-heavy endpoints where
the body/response is the bottleneck. Keep Pydantic where you rely on its
ecosystem — validators, computed fields, settings, `model_config`, or
FastAPI-compatible model reuse. Both can be used in the same application.

## Next steps

- [Request models](request-models.md) — declare and validate bodies with Pydantic, the default backend.
- [Requests and responses](requests-responses.md) — response shaping, tuple shorthands, and custom response classes.
- Full signatures are in the [API reference](../reference/index.md).
