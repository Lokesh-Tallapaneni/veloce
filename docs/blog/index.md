---
description: >-
  Writing from the Veloce maintainer on the framework's design — the one-IR
  architecture, reflection-free dispatch, and why it was built from scratch.
---

# Blog

Notes from building Veloce — design decisions, trade-offs, and the architecture
behind the framework.

- [Why I wrote an ASGI framework from scratch instead of wrapping Starlette](why-from-scratch.md)
  — the one property that's hard to get from a wrapper: a single definition the
  runtime, OpenAPI, and an MCP tool surface all derive from, so they can't drift.
- [Reflection-free request dispatch: how Veloce compiles the dependency graph](reflection-free-dispatch.md)
  — what it means to compile each handler once at registration, and an honest
  account of what that does and doesn't buy you.
