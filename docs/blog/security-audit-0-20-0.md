---
description: >-
  Veloce 0.20.0 shipped 18 security fixes in one release. What the internal
  audit covered, the one root cause behind the worst of them, what was
  deliberately left alone, and what still needs an outside pair of eyes.
---

# Eighteen security fixes in one release

Veloce [0.20.0](../changelog.md) has an eighteen-item `### Security` section.
That is an unusual thing to publish, and read cold it invites the obvious
question: was this framework riddled with holes, or did someone finally go
looking?

It was the second one. This post is the context the changelog cannot carry —
what the audit covered, what it found, what it *refuted*, and what is still
open.

## Why the number is large

A changelog entry is written per fix, not per root cause. Group the eighteen by
cause and the shape changes:

Six of them are one bug. Veloce resolves each handler's signature **once at
registration** and runs a compiled plan per request — that is the core design
decision, and it means the framework has to evaluate your type annotations at
import time. Under [PEP 563](https://peps.python.org/pep-0563/), which every
Veloce module and a great many application modules enable, annotations are
strings. So a name that cannot be resolved at registration is not a rare
pathology; it is a normal consequence of a `TYPE_CHECKING` import or a
deferred model.

The framework's response to an unresolvable annotation was to drop it and carry
on treating the parameter as unannotated. For an annotation that carried a
`Security()` marker, that meant the authorization dependency silently did not
run. Same for `Header()` and `Cookie()` — the parameter fell back to being read
from the query string, moving a credential from a header into the URL.

That is one decision — *fail open when you cannot understand a declaration* —
and it produced a family of bypasses reachable through an aliased import, an
`Optional[Annotated[T, Security]]`, a subscript whose base does not resolve,
and a few other spellings. Each spelling is its own changelog line because each
is its own regression test, but there is one fix: a route whose unresolved
annotation carries any parameter marker is now **refused at registration**. It
raises. You get a `TypeError` naming the parameter and the unresolved name,
which is the correct outcome for a declaration the framework cannot honour.

The remaining twelve are genuinely independent, and they cluster where you
would expect: the multi-hop proxy header parsers, the raw HTTP protocol's
header accounting, WebSocket backpressure, the session signer's key rotation,
and the MCP transport's scope checks.

## What the audit covered

Twelve areas, each swept independently rather than as a single read-through:
annotation and dependency resolution; proxy and forwarded-header trust;
sessions, signing and CSRF; the native HTTP/1.1 protocol implementation
(`app.run()` and the gunicorn worker, which do not share uvicorn's parser);
WebSocket framing and lifecycle; body-size limits across both transports; the
MCP tool, resource and prompt surface plus both its transports; header
encoding; and the routing and URL-building paths.

Every candidate had to be **reproduced against the real dispatch path** — not
against a helper called in isolation — before it counted. That bar matters more
than the count. Of 27 candidates, **16 were confirmed and 11 were refuted.**
A separate focused review of the fix branch itself found 8 more, including four
regressions the first round of fixes had introduced.

Publishing the refutation rate is the point. An audit that confirms everything
it looks at is not an audit; it is a list of suspicions. Several of the eleven
were plausible, well-argued, and simply wrong once the probe ran.

## What was deliberately not fixed

One confirmed finding shipped unfixed: the native transport can over-admit
pipelined requests. `on_headers_complete` appends to the request queue before
`pause_reading()` takes effect, and because a single TCP segment is fed to the
parser in one call, every request already in that segment is materialised —
the depth bound is exceeded by whatever the client packed into one write.

Two working fixes were written and both were reverted. The reason is
proportion: aiohttp, which serves a great deal of production traffic, applies
**no** pipelining depth bound at all. A bound that is approximate is not a
security boundary, and paying per-request cost on the hot path to tighten an
approximation nobody else makes was the wrong trade. It is recorded, it is
bounded by the existing body and header limits, and it will be revisited if the
transport's read path changes.

Three lower-severity findings — a duplicated helper, a redundant `str()` at
registration time, and test duplication — are open and tracked. None affects
behaviour.

## About the exposure

The fail-open behaviour existed in released versions, so the honest question is
what it meant for anyone running them.

0.19.0 — the last release carrying it — was the current version for **12 hours
and 55 minutes**. More importantly, the affected path was not silent: dropping
a parameter marker emitted a `UserWarning` that named the parameter and the
metadata it had discarded. An application whose `Security()` dependency had
stopped running was saying so on startup, to anyone whose warnings were not
suppressed.

I am not going to translate that into a count of affected deployments, because
I cannot measure it and a number I cannot defend is worse than none. What I can
say is that this was a latent fail-open default with a visible warning on a
recent release, not a quietly exploited hole.

## What is still open

**No external review has happened.** Everything above is the maintainer
auditing his own framework, which is exactly the arrangement that misses things
— you cannot easily find the bug whose absence you assumed. The refutation rate
and the reproduce-against-real-dispatch bar are there to make the process
harder to fool, and they are not a substitute for someone who did not write the
code.

Before 1.0 the intent is an outside review of the security-relevant surface:
the two header parsers, the signer, the native protocol, and the MCP
authorization path. Until that happens, treat this post as what it is — a
statement of what was checked and how, not a clean bill of health.

If you find something, the fastest route is a
[private advisory](https://github.com/Lokesh-Tallapaneni/veloce/security/advisories/new).
Reproductions against the real dispatch path are especially welcome; so are
refutations of anything claimed here.

## What changed for you

If you are upgrading, three of the eighteen can stop an application that
previously started:

- A route or websocket listener whose annotation carries a parameter marker and
  cannot be resolved now **raises at registration**. Import the name at runtime,
  or move it out of `TYPE_CHECKING`.
- `Security(scopes=...)` and the MCP `scopes=` / `mcp_scopes=` arguments reject
  a bare string. Pass `["scope"]`.
- An empty fallback signing secret is refused, which catches the
  `os.environ.get("OLD_SECRET_KEY", "")` shape in a rotation list.

Two more change behaviour without failing loudly: a path containing an empty
segment (`//admin/x`) now returns 404 rather than matching `/admin/x`, and
`url_for` percent-encodes substituted values. Both are documented with the
prior behaviour in the [routing guide](../guide/routing.md).

`ProxyFix(trust_forwarded=...)` also now defaults to `False`. If every proxy in
front of you sets `Forwarded`, pass `True` explicitly.
