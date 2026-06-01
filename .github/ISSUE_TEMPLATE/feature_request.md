---
name: Feature request
about: A missing capability or API surface Veloce should add — not a defect.
title: "<area>: <one-line capability the framework should add>"
labels: ""
assignees: ""
---

<!--
Title style: prefix with the area / module / surface, then describe the
capability as a noun phrase. Examples:

  - `cli: discover plugin commands via importlib.metadata entry points`
  - `otel: extract incoming traceparent and link server span to upstream`
  - `staticfiles: serve multi-range requests as 206 multipart/byteranges`

Open with one paragraph naming the gap and what other frameworks ship
for the same need (Flask / FastAPI / Starlette / Django, where
applicable). Avoid marketing adjectives — describe what's missing.
-->

<!-- 1-2 paragraphs naming the gap; compare against peer frameworks where relevant -->

### What's missing

<!--
The specific user pattern that doesn't work today. Show the code the
user would naturally write and the failure mode (raises, no-ops,
ignores the input, etc.).
-->

```python
# what a user expects to be able to write today
```

### Why it matters

<!--
Who hits this — a class of app, a deployment shape, a migration path?
Be specific about the audience; "everyone" is rarely true.
-->

### Proposed shape

<!--
The smallest concrete API that would close the gap. Prefer one design
over none — even a sketch is more useful than a vague "Veloce should
support X". If you have measured constraints (perf budget, error
contract, breaking-change cost), name them.
-->

```python
# proposed API
```

### Alternatives considered

<!--
Why not just extend an existing surface? Why not punt to a userland
extension? If you discarded options, name them and why — saves the
reviewer time.
-->
