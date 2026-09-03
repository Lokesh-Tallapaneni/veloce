# Roadmap to 1.0

Veloce is at `0.20.0`. This document says what `1.0` means, what has to be true
before it, and what is deliberately not in scope.

**There are no dates here.** This is a single-maintainer project, and a date
would be a guess presented as a commitment. The list below is ordered by what
blocks what, and progress against it is visible in the issue tracker.

## What 1.0 means

One thing, and it is the only thing: **the public API stops changing without a
major version.**

Today, under the [versioning policy](https://veloceframework.com/policies/), a
minor release may contain breaking changes — `0.20.0` contained six. After
`1.0`, a breaking change to the public API requires a major release. That is the
whole promise, and it is why the items below are almost entirely about deciding
things rather than adding them.

`1.0` is explicitly **not** a claim of feature completeness, and not a
performance claim.

## Before 1.0

### 1. Remove what is already deprecated

Both entries in the changelog's only `### Deprecated` block. They are the
cheapest items and they gate the API freeze, because a `1.0` that still ships
them commits to them for the whole `1.x` line.

- `Veloce.on_event()` and `add_event_handler()` — superseded by
  `@app.on_startup` / `@app.on_shutdown`. The changelog records "Removal in
  v1.0.0", so this one is already decided.
- `FileResponse(path)`'s sync constructor on a running loop. It warns today, and
  `http/response.py` says "the next major bump will tighten this to a hard
  error" — so `1.0` is that bump unless the sync helpers it protects
  (`send_file`, `Veloce.send_static_file`) are given an async path first. That
  is the decision to make, and it is about those helpers rather than about the
  warning.

Nothing else is deprecated. The `### Deprecated` block lists exactly these two,
and the tree carries exactly three `VeloceDeprecationWarning` call sites
(`app/lifecycle.py` twice, `http/response.py` once) — worth stating because a
`1.0` that removes something never formally deprecated would breach the
project's own three-step deprecation process.

### 2. Settle the public surface

The API freeze is only meaningful if the surface is known. Concretely:

- Audit `__all__` at the top level and at every subpackage gateway against what
  is actually documented. A symbol that is importable but absent from the docs
  is either public and undocumented, or private and leaking; neither should
  survive the freeze.
- Confirm the consequences of `veloce.contrib.*` being public API. The
  [versioning policy](https://veloceframework.com/policies/) already names
  `veloce.contrib` and `veloce.contrib.mcp` in the public surface, so `1.0`
  freezes them on the same terms as the core. That is a real commitment for a
  package whose wire format is defined externally — see item 5.
- Resolve [#302](https://github.com/Lokesh-Tallapaneni/veloce/issues/302)
  (`functools.partial` handlers) — a small bug, but one where the route
  registrar and the plan builder currently disagree about what a valid handler
  is. Freezing that disagreement is worse than fixing it.

### 3. External security review

[#309](https://github.com/Lokesh-Tallapaneni/veloce/issues/309).

`0.20.0` shipped eighteen security fixes, from two separate pieces of work: a
sweep of the shipped framework, and a review of the fix for an auth bypass
released in `0.19.0`.

The sweep examined 27 candidates and confirmed 16, every one reproduced against
the real dispatch path rather than against a helper in isolation. One of those
16 — the pipelining bound, now [#307](https://github.com/Lokesh-Tallapaneni/veloce/issues/307)
— was deliberately left unfixed, so 15 shipped. The remaining 3 came from the
branch review. The eleven refuted candidates are the number worth publishing: an
audit that confirms everything it looks at is a list of suspicions.

It was still the maintainer auditing his own work, which structurally misses the
bug whose absence you assumed.

The surface that most needs an outside reader, in order:

1. The two multi-hop proxy header parsers (`middleware/proxy_fix.py`)
2. The native HTTP/1.1 protocol (`serving/protocol.py`), which does not share
   uvicorn's parser and so does not inherit its scrutiny
3. The signer (`signing.py`) — key rotation and the timestamped construction
4. The MCP authorization path (`contrib/mcp/`)

This is the one item that cannot be completed alone, and the one most likely to
change the shape of the others.

### 4. Registration-time correctness

Two known issues in the path that compiles a handler once at registration. Both
are registration-time only — they cost startup, not per-request latency — but
both are in the machinery that the whole design rests on.

- [#305](https://github.com/Lokesh-Tallapaneni/veloce/issues/305) — type hints
  resolved twice per `add_route`, which also constructs a
  `Depends(build_client())` argument twice. The double construction is the part
  that is arguably a correctness bug rather than waste.
- [#306](https://github.com/Lokesh-Tallapaneni/veloce/issues/306) — the request
  body path's sensitivity to feed splits. Scoped as measure-first; no change
  should be attempted before there is a number.

### 5. A stated MCP revision policy

This is the item most specific to Veloce, and the one with no precedent to copy.

A route exposed with `expose_as_mcp_tool=True` is a public API surface, but it
is one whose *wire format* is defined by someone else. Veloce currently serves
three protocol revisions — `2025-06-18`, `2025-11-25` and `2026-07-28` — with
real negotiation between them, including the newest revision's per-request
version carrying and its partitioned JSON-RPC error range.

The policy already answers the ownership question: `veloce.contrib.mcp` is
public API, so `1.0` freezes it. What the policy does not yet distinguish is the
*Python surface* from the *protocol surface* underneath it.

The Model Context Protocol revises faster than Veloce will cut major versions,
which makes one question load-bearing:

- When a revision is superseded and Veloce stops serving it, is that a breaking
  change to public API — requiring a major version — or a change to an external
  protocol Veloce merely tracks?

Under a literal reading of the policy today it is the former, because the
documented behaviour of a public symbol is part of the promise. That may be the
right answer. It is also a heavy one: it means each spec deprecation cycle can
force a Veloce major.

If the intent is the lighter reading — Python API frozen, protocol support
tracking the specification on its own cadence — then
[`docs/policies.md`](https://veloceframework.com/policies/) has to say so
**before** `1.0`, not after. Deciding it under pressure, when the first
post-`1.0` revision lands, is how a stability promise gets quietly broken.

### 6. Documentation completes the contract

Every public symbol reachable from `__all__` should appear in a guide with a
usage example, or in the API reference with a docstring that carries one. At the
freeze, the documentation is part of what is being frozen.

## Not blocking 1.0

Stated explicitly so these are not read as commitments:

- **Free-threaded builds** ([#308](https://github.com/Lokesh-Tallapaneni/veloce/issues/308)).
  Blocked externally: `orjson` is a required dependency and ships no
  free-threaded wheels, so a `3.14t` interpreter cannot install Veloce at all.
  Nothing in Veloce's design assumes the GIL, but this cannot be attempted, let
  alone promised.
- **The native transport's pipelining depth bound**
  ([#307](https://github.com/Lokesh-Tallapaneni/veloce/issues/307)). Confirmed,
  deliberately unfixed, with the reasoning recorded on the issue. The expected
  outcome is closing it in favour of the status quo.
- **Performance work in general.** Veloce is fast and the benchmarks are
  published, but no throughput target gates `1.0`. An optimisation lands when it
  is measured on the path it changes, whenever that happens.
- **A feature freeze.** Minor releases add features — the policy says so, and
  `[Unreleased]` already carries three. Features keep landing on the way to
  `1.0`; what changes at `1.0` is the compatibility promise, not the pace. A
  feature is only deferred when it would widen the surface being frozen faster
  than it can be settled.

## After 1.0: what the agent surface is for

Not planned in detail, deliberately — but the direction is not a wishlist, it
follows from one property the architecture already has.

A route is compiled once at registration into a contract: parameters, types,
dependencies, authorization, response shape. HTTP, OpenAPI and MCP each emit
from that one contract, so an agent calling a tool reaches the same handler and
the same `Security()` dependency as a browser calling the endpoint.

Be precise about what that does and does not buy. There is one handler and one
compiled plan, but there are still two dispatch paths — `contrib/mcp` builds its
own resolver and its own request context, and its comments say it binds them
"exactly as `handle_request` does". That phrase is the honest description: the
paths are kept in agreement deliberately. What the shared contract buys is that
a divergence is a **bug with one fix site**, rather than two features drifting
apart in two places. Two of `0.20.0`'s security fixes were exactly that kind of
divergence — the MCP door copied a raw JSON value where the HTTP path converted
it, and a completion ran without checking the owning prompt's scopes.

That is a real advantage and it does not need overstating. A roadmap that claims
drift is impossible is refuted by its own changelog.

### The other AI workload: serving the model itself

Before the speculative part, the unglamorous half — because it is already true
and belongs in the frozen surface rather than in a plan.

Two different things get called "AI workloads". One is *serving agents*, which
is what MCP is for. The other is *serving inference*: a request arrives, a model
runs, a result comes back. That second one is mostly a concurrency problem, and
it is the reason a `def` handler is a first-class citizen here rather than a
compatibility shim.

A model call is blocking and CPU-bound. `model.predict()`, a tokenizer, a
`numpy` reduction — none of these are awaitable, and none of them release the
loop on their own. Veloce runs a sync handler on a worker thread, so the loop
keeps serving while it works:

```python
@app.get("/classify")
def classify(request, text: str):     # def, not async def
    return {"label": model.predict(text)}   # blocking is fine here
```

Measured on the real dispatch path: four concurrent requests to a handler that
blocks for 0.30s complete in 0.30s, not 1.20s, and the handler body runs on a
worker thread rather than the loop thread.

This applies to **handlers**. A sync *dependency* is called inline on the event
loop by default, so the same four concurrent requests take 1.20s and the loop
is blocked while they run — trivial dependencies should not pay for a thread
hop they do not need. Put the blocking call in the handler, or opt the
dependency in:

```python
@app.get("/classify")
async def classify(request, m: Annotated[Model, Depends(load_model, offload=True)]):
    ...
```

The distinction matters most for exactly this workload, so it is documented in
the [dependency injection guide](https://veloceframework.com/guide/dependency-injection/)
rather than left to be discovered.

The streaming half is the modern shape — a token at a time from a language
model — and is served by `EventSourceResponse` for SSE, `StreamingResponse` for
raw chunks, and WebSockets on both transports for bidirectional sessions.
Request bodies stream too (`stream=True`), which is what a large audio or image
upload needs to avoid being buffered whole.

None of this is planned work. It is listed here because `1.0` freezes it: the
sync-offload guarantee and the streaming response types are part of the public
contract, and an inference service written against them should not have to
change at a major version.

### `veloce gen-client` — a fourth emit target

The same contract that produces an OpenAPI operation and an MCP tool can produce
a typed client. Nothing in the IR is HTTP-specific enough to prevent it.

Not built. There is no `gen-client` subcommand today, and this is the honest
statement of a direction rather than a half-finished feature.

### Static verification over the contract

This is the more interesting one, and it is specific to serving agents.

When a human calls your API, a missing authorization check is a bug someone
might find. When an *agent* calls it, the agent will enumerate every tool it can
see and call each one, because that is what agents do. The class of bug where a
route reads an owner-scoped resource without checking who is asking — an IDOR,
or BOLA in the OWASP API list — stops being a latent flaw and becomes something
that gets exercised systematically on the first day.

Because the contract is known at registration and before any request is served,
that property is checkable rather than testable: every route that takes an
owner-scoped identifier must have an authorizer attached to it. A route that
does not is a registration-time failure, not a pentest finding.

`veloce check` already runs a pre-deploy audit. The step beyond it is proving a
property over the whole route table rather than looking for known-bad patterns.

Not built, and harder than it sounds — the difficulty is not the proof, it is
defining "owner-scoped" in a way that is precise enough to check and loose
enough to be useful without annotating every route by hand.

### Where the agent surface goes next

This is the direction the project is actually pointed. Serving agents is what
Veloce is for, and the surface is expected to keep growing — the MCP
specification is moving quickly, and a framework whose thesis is "one route, two
doors" has to move with it rather than freeze at whatever revision `1.0` ships.

Concretely, the work that follows from here:

- **Tracking the specification.** New MCP revisions land as they stabilise. This
  is ongoing maintenance, not a milestone — the three currently served revisions
  arrived that way.
- **The two directions above** — client generation from the same contract, and
  static authorization proofs over the route table. Both matter more as agents
  multiply, because both are about catching a mistake before an agent finds it.
- **Whatever the ecosystem converges on.** If a second protocol reaches the
  adoption MCP has, the route-contract architecture is what makes adding it a
  new emit target rather than a second framework. That is the whole point of the
  design.

One bar, though, and it is the reason this section is not a wishlist: an agent
surface is an *authorization* surface. Every capability added is another way for
a caller to reach a handler, and `0.20.0` shipped two fixes for exactly that —
the MCP door and the HTTP door disagreeing about a value and about a scope
check. Breadth that outruns the guarantees is how a framework ends up with an
agent door that is easier to walk through than the front one.

So: more agent capability, on the condition that each addition emits from the
same contract the HTTP door does rather than growing a parallel path beside it.

The two post-`1.0` directions above are speculative in their timing. The
direction is not.

## Following along

Progress is the issue tracker, not this file — this document is revised when the
shape of the work changes, not when an item moves. If something here matters to
you, the issue is the place to say so.
