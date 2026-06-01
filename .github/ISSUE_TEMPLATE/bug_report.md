---
name: Bug report
about: A correctness, security, or behavioural defect — anything where the framework does the wrong thing.
title: "<symbol-or-area> <one-line symptom> — <one-line consequence>"
labels: ""
assignees: ""
---

<!--
Title style: a concrete one-liner naming the symbol / area, the symptom,
and the user-visible consequence, separated by an em-dash. Examples:

  - `Response.body assignment bypasses _encoded cache invalidation — stale body and wrong Content-Length on re-encode`
  - `verify_password accepts attacker-controlled scrypt N without bounds check — tampered hash verifies instantly`
  - `Router._merge_node loses trailing_slash / tolerant_slash / subdomain / host on merge — sub-routers silently lose constraints`

Open with one or two short paragraphs of prose describing what is broken
and the assumption that silently held until now. No lists yet. Cite the
relevant RFC section inline when applicable.
-->

<!-- describe the defect in 1-2 paragraphs -->

### Where

<!--
Cite file:line and paste the minimum buggy snippet. Use the form
`src/veloce/<path>:<line>` so a reader can jump straight to it.
If a user-visible code pattern triggers the bug, show it too.
-->

```python
# src/veloce/<path>:<line>
```

### Why it matters

<!--
What does this break in real use? Be concrete. Examples worth calling
out: hot-path involvement, data corruption, security exposure, silent
mis-emission, cache mismatch, RFC violation, breaks-on-second-call.
Keep the framing honest — defensive items get tagged "defensive", real
exploits get the corresponding language.
-->

### Suggested fix

<!--
Either one concrete code patch, or two options labelled A / B with the
trade-off named. Mention tests that should be added to lock the fix in.
-->

```python
# Option A: minimal change
```
