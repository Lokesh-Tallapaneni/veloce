# Design: MultiDict semantics on headers and query params (Q7, Q12)

## Contract

`Request.headers` is a `Headers` (subclass of `multidict.CIMultiDict`) and
`Request.query_params` is a `QueryParams` (subclass of `multidict.MultiDict`).
Both preserve duplicate keys, expose an explicit `getlist(key)` method,
and behave like dicts for the common-case single-value access.

`Headers` is case-insensitive. `QueryParams` is case-sensitive (per
WHATWG URL spec).

## Observable behavior

```python
req.headers["Content-Type"]       # → "application/json"
req.headers["content-type"]       # → "application/json" (case-insensitive)
req.headers.get("X-Forwarded-For")# → first value, or None
req.headers.getlist("Set-Cookie") # → ["a=1", "b=2"]    (alias)
req.headers.getall("Set-Cookie")  # → ["a=1", "b=2"]    (multidict-native)

req.query_params["tag"]           # → "a"               (?tag=a&tag=b)
req.query_params.getlist("tag")   # → ["a", "b"]
req.query_params.get("missing")   # → None
```

For `Request(headers=...)`, the constructor accepts a plain `dict`, a list
of `(key, value)` tuples, or an existing `Headers`/`CIMultiDict` instance.
Plain dicts get wrapped; multidicts pass through.

## What changed

- `veloce.http.datastructures.Headers` now subclasses
  `multidict.CIMultiDict` rather than `dict`. Duplicates are preserved.
- `veloce.http.datastructures.QueryParams` is new — a `multidict.MultiDict`
  subclass for query string parsing.
- `Request.query_params` returns a `QueryParams` instance (no longer a
  plain `dict`). Lazy parsing unchanged.
- `Veloce.__call__` (the ASGI entry point) builds `Headers` directly from
  the raw `scope["headers"]` list of `(bytes, bytes)` tuples, preserving
  duplicate header lines that a dict would have collapsed.
- `DependencyResolver` `K_QUERY_LIST` slot now uses `.getall(name)` instead
  of `params[name] -> list-or-str` inspection. Cleaner, exact semantics.

## What did **not** change

- Existing handler code reading `request.headers["Content-Type"]` keeps
  working — multidict's `__getitem__` returns the first value.
- Existing handler code reading `request.query_params["q"]` keeps
  working — same single-value semantics.
- The `Headers` and `QueryParams` classes accept the same constructor
  shapes as before (`dict`, `Iterable[tuple]`, kwargs).

## Hot-path budget

`multidict` is a C-accelerated package — its read paths are faster than
Python dict for typical sizes (≤32 entries). Per-request overhead vs the
old plain-dict path is **net negative** in profiles of typical handlers.

A direct microbench against the prior implementation would show identical
or slightly faster header lookups; the meaningful change is **correctness**,
not speed.

## Threading model

`multidict.CIMultiDict` and `multidict.MultiDict` are not thread-safe for
concurrent writes. They are constructed once per request and read by one
task at a time, so this is fine. Free-threaded build: safe under the same
constraint as the GIL build.

## Trade-offs

- `Request.path_params` is **not** a MultiDict. Path parameters by spec
  can't repeat in one URL (a route can't have two `{id}` segments in the
  same position), so a plain dict suffices.
- `FormData` (the multipart parser output) is **not yet** converted to a
  multidict — it remains a `dict` subclass with `getlist` returning a
  list-or-single. Q8 (form MultiDict) is tracked as a follow-up; needs
  the multipart parser to switch to producing duplicates.
- `Cookie` headers are still parsed into a plain `dict` on `Request.cookies`.
  Q13 (cookies MultiDict) is a follow-up; cookies rarely duplicate in
  practice and the upgrade can land when needed.

## References

- WHATWG URL §5 (`application/x-www-form-urlencoded` decoding)
- RFC 9110 §5.2 (HTTP header field names case-insensitive)
- RFC 6265 (cookies)
- `multidict` package — `CIMultiDict`, `MultiDict`. Already a runtime
  dependency of veloce.
