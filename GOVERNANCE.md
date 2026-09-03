# Governance

This document says who decides what in Veloce, and how that would change.

It is deliberately short. Describing an elaborate process for a project with one
maintainer would be a description of something that does not exist.

## Current model

Veloce has one **maintainer** — Lokesh Tallapaneni
([@Lokesh-Tallapaneni](https://github.com/Lokesh-Tallapaneni)) — meaning one
person holds commit and release rights and is accountable for what ships.

It has **contributors** — Revanth Ravella
([@revanth-ravella](https://github.com/revanth-ravella)) among them. Those are
different roles: a contributor proposes changes through pull requests, a
maintainer decides what lands and cuts releases. Copyright in the project is
held jointly by everyone whose work has been merged, which is why the licence
reads "and contributors".

In practice this means:

- Every change lands through a pull request, including the maintainer's own; see
  [CONTRIBUTING.md](CONTRIBUTING.md). With one maintainer there is nobody to
  review those, so they are self-merged once CI is green — the pull request buys
  a diff, a description and a gate, not a second reader. An outside
  contributor's pull request is reviewed by the maintainer before it lands.
- Design decisions are made by the maintainer, in public, on the issue or pull
  request where they come up. Where a decision rejects an obvious alternative,
  the reasoning is written down at the point of rejection rather than
  reconstructed later.
- There is no vote, no committee and no tie-break procedure, because with one
  maintainer there is nothing to tie.

Anyone may open an issue or a pull request. Contributions do not require prior
discussion for small, self-contained changes, though an issue first will save
you work on anything larger.

## How decisions get made

Decisions are argued from evidence, in this order of weight:

1. **A reproduction.** A claim about behaviour is settled by a script that
   demonstrates it, run against the real dispatch path rather than a helper in
   isolation. This applies equally to bug reports and to security findings.
2. **A measurement.** A claim about performance is settled by a benchmark on
   the path that actually changed, on Linux. "This should be faster" is not an
   argument; a number is. An optimisation is not accepted on theory or
   inspection alone.
3. **The project's stated constraints.** The rules in
   [CONTRIBUTING.md](CONTRIBUTING.md) about hot-path cost, public API surface,
   dependency direction and framework independence are binding, and a change
   that violates one needs to argue against the rule rather than around it.

Where a decision comes out against a proposal, the expectation is that the
reasoning is recorded — in the issue, or in the code where the next reader will
need it. Issue [#307](https://github.com/Lokesh-Tallapaneni/veloce/issues/307)
is the pattern: a confirmed finding, deliberately not fixed, with the argument
for that written down.

## Becoming a maintainer

[CONTRIBUTING.md](CONTRIBUTING.md) describes the path. This section says only
what governance adds to it: nobody holds commit rights but the maintainer today,
and the judgement being looked for is the one described in "How decisions get
made" above — a contributor who has argued the project out of one bad change has
shown more than one who has merged ten easy ones.

When a second maintainer is added, this document gets rewritten to describe a
shared model, including how a disagreement between maintainers is resolved —
the question that only becomes real at that point.

## Releases

The maintainer cuts releases. A tagged `vX.Y.Z` triggers the release workflow,
which publishes to PyPI through Trusted Publishing; there is no separate release
manager and no manual upload step.

Versioning and what counts as a breaking change are covered by the
[versioning policy](https://veloceframework.com/policies/), not here.

## Security

Vulnerability reports go through GitHub's private advisory flow, described in
[SECURITY.md](SECURITY.md). The maintainer triages and fixes them; there is no
embargo list and no security team.

The security-relevant surface has not had an external review — that is tracked
openly as
[#309](https://github.com/Lokesh-Tallapaneni/veloce/issues/309).

## Code of conduct

Behaviour in the project's spaces is governed by
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Reports go to the maintainer, who is
currently also the only person who can act on them, including on a report about
himself — a limitation worth naming rather than hiding, and one that resolves
only when there is a second maintainer.

## Changing this document

By pull request, like everything else.
