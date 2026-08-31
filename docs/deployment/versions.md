---
description: Pin veloceframework safely — read veloce.__version__, what SemVer means while Veloce is pre-1.0, and which version constraint to put in your requirements.
tags: [deployment, versioning, pinning, semver]
---

# Versioning and pinning

Veloce is published to PyPI as `veloceframework` and follows
[Semantic Versioning](https://semver.org/). The installed version is available
at runtime as `veloce.__version__` and from the CLI as `veloce --version`. This
page covers how to pin the dependency; the
full guarantees live in the [versioning and stability policy](../policies.md).

```bash
pip install veloceframework
```

## Check the installed version

`veloce.__version__` is the version string of the installed distribution.

```python
import veloce

print(veloce.__version__)  # e.g. "0.3.0"
```

From a shell, the CLI prints the same value:

```bash
veloce --version
```

!!! note
    Pass the version to the app only when you want it in your OpenAPI schema:
    `app = Veloce(version="1.4.2")`. That is *your application's* version, not
    Veloce's — it is unrelated to `veloce.__version__`.

## What the version number means

A release is `MAJOR.MINOR.PATCH`. Under SemVer:

| Bump | Example | What it may contain |
| --- | --- | --- |
| Patch | `0.3.0` → `0.3.1` | Bug fixes only, always backward compatible. |
| Minor | `0.3.0` → `0.4.0` | New features; while pre-1.0, may also contain breaking changes. |
| Major | `0.x` → `1.0.0` | The first stable API contract. |

!!! warning "Veloce is pre-1.0"
    While the major version is `0`, a **minor** bump (`0.3` → `0.4`) is allowed
    to break the public API under SemVer. Breaking changes are deliberate and
    recorded in `CHANGELOG.md`, but you should not assume a `0.x` minor upgrade
    is drop-in. Pin a range you have tested.

## Pinning the dependency

Pin a minor range you have tested and bump it intentionally. While Veloce is
pre-1.0, pin the current minor and allow patches:

```bash
pip install "veloceframework>=0.3,<0.4"
```

In a `requirements.txt`:

```text
veloceframework>=0.3,<0.4
```

In `pyproject.toml`:

```toml
dependencies = [
    "veloceframework>=0.3,<0.4",
]
```

After Veloce reaches `1.0`, breaking changes land only in a major release, so a
caret-style range that allows minors and patches becomes safe:

```bash
pip install "veloceframework>=1.0,<2.0"
```

!!! tip
    Commit a lockfile (`uv.lock`, `pip freeze > requirements.lock`, or your
    Poetry/PDM lock) so deploys install the exact versions you tested, not
    whatever is newest at build time. The range in `pyproject.toml` says what is
    *allowed*; the lockfile says what actually ships.

## How Veloce pins its own dependencies

Veloce declares a **floor and no ceiling** for each dependency:

```toml
dependencies = [
    "orjson>=3.11.5",
    "pydantic>=2.4.0",
    "jinja2>=3.1.6",
]
```

An upper bound is added only for a *known* incompatibility, never pre-emptively.
A speculative `<4` propagates into every downstream resolution and can block an
application from taking a fix, which costs more than it protects.

The floor is the oldest release Veloce is willing to be installed with, and it
moves for two reasons:

- **A security fix.** The floor is raised to the first release carrying it, so
  an install resolving at the minimum is not a vulnerable one. This is why the
  floors are higher than the oldest version that would technically work.
- **A feature Veloce depends on.** Raising the floor is the alternative to
  version-sniffing at runtime.

A floor is raised in a **minor** release and noted in the changelog, since it can
change what resolves for an application that pins loosely.

!!! note
    The floors are what a fresh `pip install veloceframework` may resolve to, not
    what Veloce is tested against. CI runs the locked versions in `uv.lock`,
    which track current releases.

## Surfacing deprecations early

Before a public symbol is removed it keeps working for at least one minor
release and raises a `VeloceDeprecationWarning` that names its replacement.

That category is rooted at `UserWarning`, not at `DeprecationWarning`, and
deliberately so: Python's default filter shows a `DeprecationWarning` only when
it is raised from `__main__`. Every application served by uvicorn or gunicorn
reaches Veloce from an application module instead, so a deprecation would have
been silent there and the removal would have arrived unannounced.

Turn the warnings into test failures so an upgrade surfaces them before
production:

```bash
python -W error::UserWarning -m pytest
```

To be precise about the category rather than promoting every `UserWarning`:

```python
import warnings

from veloce import VeloceDeprecationWarning

warnings.filterwarnings("error", category=VeloceDeprecationWarning)
```

The same call with `"ignore"` silences them once you have read them.

!!! warning
    `python -W error::DeprecationWarning` does **not** catch these. Veloce's
    deprecations are not `DeprecationWarning` subclasses, for the reason above.

## Next steps

- Read the guarantees in full — see the [versioning and stability policy](../policies.md).
- Serve the pinned app — see [Run a server manually](manually.md).
- Full signatures are in the [API reference](../reference/index.md).
