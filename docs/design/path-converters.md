# Design: Path converters (R12)

## Contract

URL rules can declare a converter on each path parameter:
`{name:int}`, `{name:float}`, `{name:str}`, `{name:uuid}`, `{name:path}`,
`{name:any(a,b,c)}`. The converter is applied **at match time**, not in the
dependency-injection layer. A segment the converter rejects causes the
router to fall through to the next candidate child — which means a typed
mismatch is a **route miss** (404), not a 422.

Coerced values land directly in `request.path_params`:
- `int` → Python `int`
- `float` → Python `float`
- `uuid` → `uuid.UUID`
- `path` → `str` (greedy — consumes the rest of the URL)
- `str` / `string` → `str` (the default; behaviour unchanged)
- `any(a,b,c)` → `str` constrained to the listed values

Two segment syntaxes are accepted — the angle-bracket form (`<int:id>`)
and the brace form (`{id:int}`). UUID validation follows RFC 4122.

## Observable behavior (new)

| URL rule              | Request          | path_params           |
|-----------------------|------------------|------------------------|
| `/u/{id:int}`         | `GET /u/42`      | `{"id": 42}` (int)     |
| `/u/{id:int}`         | `GET /u/abc`     | **404** (route miss)   |
| `/files/{p:path}`     | `GET /files/a/b` | `{"p": "a/b"}`         |
| `/x/{u:uuid}`         | valid UUID       | `{"u": UUID(...)}`     |
| `/c/{c:any(red,blue)}`| `GET /c/red`     | `{"c": "red"}`         |
| `/c/{c:any(red,blue)}`| `GET /c/green`   | **404**                |
| `/u/{name}` (default) | `GET /u/alice`   | `{"name": "alice"}` (str — unchanged) |

## Observable behavior (unchanged)

Routes without converters are unaffected. `/u/{name}` still binds `name` to
a raw string; `dependency.py` continues to coerce by handler annotation
where one is declared (e.g. `name: int` triggers DI coercion).

## Data model

```
class _Converter:
    greedy: bool = False
    def match(value: str) -> tuple[bool, Any]: ...

class StringConverter(_Converter):  pass  # default
class IntConverter(_Converter):     pass
class FloatConverter(_Converter):   pass
class UUIDConverter(_Converter):    pass  # rejects non-canonical UUIDs
class PathConverter(_Converter):    greedy = True
class AnyConverter(_Converter):     ...   # holds the allowed-values tuple
```

`RadixNode.converter` is `None` on static and wildcard nodes; on param
nodes it is always set (defaulting to `StringConverter()`).

## Hot-path budget

- Static segments: untouched (one dict lookup per child).
- Param segments: one method call per converter candidate. `int.match` is
  one `int(value)` try/except; `uuid.match` is one regex `match`; `any.match`
  is one tuple `in` check. All O(1) per attempt.
- Greedy (`path`) match short-circuits — one join of remaining segments
  and a return.

The router still tries static-first, then param children in registration
order. The added per-param cost is comparable to a Python attribute read
plus the converter's own check. No reflection, no allocation per request
beyond what already happened.

## Threading model

Converters are stateless and shared across all matches. No locks.
`AnyConverter._choices` is a frozen tuple set at registration. Free-threaded
safe.

## Public API

No new public symbols on the `veloce` namespace. Users opt in via the
existing `{name:type}` syntax in route declarations. Converters live in
`veloce.routing.converters` and are accessible for users who want to
extend (a future feature: `Router.url_map.converters[]` registration —
not landed in R12).

## Trade-offs

- **Multiple param children with the same name and different converters
  are allowed** (`/u/{id:int}` and `/u/{name}` coexist on `/u/`). The
  router tries them in registration order; the int converter wins for
  digits, the string converter catches the rest — the intuitive outcome.
- **`Router.match` no longer typed as `dict[str, str]`** — it returns
  `dict[str, Any]` because coerced values can be `int`, `float`, `UUID`.
  Existing callers that re-coerced in DI still work (idempotent).
- **`float` converter rejects scientific notation and `nan`/`inf`** to
  keep matched values predictable. To accept those, register the bare
  string converter and coerce in the handler.

## References

- RFC 4122 §3 (UUID canonical form)
- PEP 3333 (URL path semantics)
