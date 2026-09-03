# Versioning & stability policy

Veloce follows [Semantic Versioning](https://semver.org/). This page defines what
counts as the public API, what stability you can rely on, and how breaking
changes and deprecations are handled.

## Current status

Veloce is pre-1.0 (`0.x`). Under SemVer, while the major version is `0`, a
**minor** bump (`0.3` → `0.4`) may contain breaking changes. The project keeps
breaking changes deliberate and documented, but a stable-forever API contract
begins at `1.0`. Pin a version range you have tested (for example
`veloceframework>=0.4,<0.5`) until `1.0` is released.

## Supported Python versions

Veloce supports **3.10 through 3.14**. Every one of them runs the full test
suite on each commit, and the PyPI classifiers list exactly the versions CI
exercises - a test enforces that the two agree, so a classifier is never an
untested promise.

A version is dropped only in a minor release, and only once it has reached
[end of life](https://devguide.python.org/versions/).

!!! note "Free-threaded builds"

    Free-threaded (`3.14t`, PEP 703) builds are **not yet supported**. The
    blocker is external rather than architectural: `orjson` is a required
    dependency and publishes no free-threaded wheels, so the interpreter
    cannot install Veloce at all. This is tracked and will be revisited when
    that changes; nothing in Veloce's own design assumes the GIL for
    correctness.

## What is the public API

The public API is:

- every symbol exported from the top-level package (`from veloce import X` —
  i.e. names in `veloce.__all__`), and the same names re-exported from each
  subpackage gateway (`veloce.app`, `veloce.http`, `veloce.routing`,
  `veloce.middleware`, `veloce.security`, `veloce.contrib`,
  `veloce.contrib.mcp`, `veloce.serving`);
- the documented behaviour of those symbols described in this documentation;
- the `veloce` command-line interface.

Anything **not** in that list is private, including any name beginning with an
underscore and any module not re-exported from a gateway. Private names may
change or be removed at any time without notice.

## Stability guarantees

- **Patch releases** (`0.4.0` → `0.4.1`) contain only bug fixes and are always
  backward compatible.
- **Minor releases** add features and may, while pre-1.0, contain breaking
  changes — always called out in `CHANGELOG.md` under `### Changed` or
  `### Removed` with a migration note.
- After `1.0`, breaking changes to the public API will only land in a **major**
  release.

## Deprecation process

When a public symbol or behaviour is to change or be removed:

1. It keeps working and raises a `VeloceDeprecationWarning` that names the replacement.
2. The deprecation is recorded in `CHANGELOG.md` under `### Deprecated` with a
   migration note.
3. The symbol is removed no earlier than the **next minor release** after the one
   that introduced the warning (and, after `1.0`, only in a major release).

To surface deprecations in your own test suite, turn them into errors:

```bash
python -W error::veloce.VeloceDeprecationWarning -m pytest
```

`VeloceDeprecationWarning` subclasses **`UserWarning`**, not
`DeprecationWarning` — deliberately, so it is visible by default rather than
silenced the way Python hides `DeprecationWarning` outside `__main__`. That also
means `-W error::DeprecationWarning` does **not** catch it.

## Reporting

- Bugs and feature requests: the project's GitHub issue tracker.
- Security vulnerabilities: do not open a public issue — follow
  [SECURITY.md](https://github.com/Lokesh-Tallapaneni/veloce/blob/main/SECURITY.md).

## What's next

- [Configuration](guide/configuration.md)
- [Deployment](guide/deployment.md)
