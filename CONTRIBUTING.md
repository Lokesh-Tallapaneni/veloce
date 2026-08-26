# Contributing to Veloce

Thanks for your interest in improving Veloce. This guide covers how to set up a
development environment, the checks your change must pass, and how to propose it.

## Where to start

New here? Browse the open
[`good first issue`](https://github.com/Lokesh-Tallapaneni/veloce/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22)
and
[`help wanted`](https://github.com/Lokesh-Tallapaneni/veloce/issues?q=is%3Aissue+is%3Aopen+label%3A%22help+wanted%22)
issues - each is scoped with a suggested approach so you can get going quickly.
Documentation improvements are always welcome and make an excellent first
contribution.

## Development setup

Veloce targets Python 3.10+.

The project uses [uv](https://docs.astral.sh/uv/) for environment management.
With it installed, `uv sync --all-extras --dev` sets up a locked environment
with all dev tooling, and you can run any command below through
`uv run <command>`:

```bash
git clone https://github.com/Lokesh-Tallapaneni/veloce.git
cd veloce
uv sync --all-extras --dev
```

The dev tooling lives in the PEP 735 `[dependency-groups]` table (not a `dev`
extra), so a plain `pip install -e ".[dev]"` will not pull it in. Without uv,
install the package plus the dev group with pip 25.1 or newer:

```bash
pip install -e . --group dev
```

Then run the commands below directly (without the `uv run` prefix).

The repository ships a `.pre-commit-config.yaml` that runs `ruff check --fix`,
`ruff format`, and basic hygiene hooks. Install it once so lint and formatting
are fixed before each commit instead of failing in CI:

```bash
pre-commit install
```

## Quality gates

Every change must pass the full gate set before it is submitted:

```bash
ruff check .                    # lint
ruff format --check .           # formatting
mypy src/veloce                 # static types
pytest                          # full test suite
```

CI runs two more gates on each pull request; reproduce them locally before you
submit:

```bash
# Coverage floor: CI fails the build below 90%.
pytest --cov=veloce --cov-fail-under=90

# Parser fuzzing: CI runs the hypothesis leg with a larger example budget.
HYPOTHESIS_PROFILE=ci pytest -m fuzz

# Benchmarks: CI runs them on CodSpeed and reports the delta on the PR.
# Two instruments - see benchmarks/README.md for which suite a benchmark
# belongs in and why.
pytest benchmarks --ignore=benchmarks/walltime --ignore=benchmarks/memory --codspeed
pytest benchmarks/walltime --codspeed   # thread-crossing paths
pytest benchmarks/memory --codspeed     # allocation of held structures
```

- Tests use `pytest-asyncio` in auto mode: write async tests as plain
  `async def test_*` functions (do **not** add `@pytest.mark.asyncio`).
- Add a regression test for every bug fix, placed in the test module for the
  feature it exercises - not in a catch-all file.
- Benchmarks live in `benchmarks/` (outside `testpaths`, so a plain `pytest`
  never collects them). See `benchmarks/README.md` before adding one.
- `mkdocs build --strict` must succeed for any docs change.

## Making a change

1. Open an issue describing the bug or proposal first for anything non-trivial.
2. Create a branch off `main`.
3. Keep the change focused; unrelated cleanups belong in their own pull request.
4. Update the public surface in the same change when behavior changes: tests,
   the relevant docs page, and `CHANGELOG.md` under `## [Unreleased]`.
5. New public symbols must be exported from the appropriate `__init__.py`
   gateway and documented.
6. Open a pull request against `main`. All changes merge through pull requests;
   direct pushes to `main` are not used.

## How this codebase keeps itself honest

Two conventions do most of the work here. Both exist because prose does not
enforce anything — a rule nobody executes drifts, and the drift is silent.

### 1. A claim about the code is executed, not asserted

If a docstring, a guide page or the README states a behaviour, a test runs that
exact statement. This is why `tests/` contains files named `*_claims.py`,
`*_contract*.py`, `*_parity*.py` and `*_invariants.py` — they are not testing
features, they are testing that what we *say* is still true.

Concretely, when you write documentation:

- **A code block in a guide should be runnable, and something should run it.**
  Several test modules execute every Python block on a page in order. A worked
  example that nobody executes is the most common way docs go wrong — and it
  fails quietly, because the reader assumes the mistake is theirs.
- **A stated guarantee gets a test named after the guarantee**, not after the
  function. `test_a_timeout_consumes_no_message` beats `test_receive_timeout_2`.
- **Prefer the direction that catches an omission.** A test that every symbol in
  the README's feature table exists is worth more than one that a particular
  symbol is listed: the first catches a table naming something that was removed.

### 2. A structural rule is enforced where it is broken, not reviewed

Where a mistake can be caught at import or at class definition, it should be —
not left to fail on a live request.

- **A base class meant for subclassing checks its subclasses.** `Cache`,
  `SessionStore`, `View`, `JSONProvider`, the path-converter base and the MCP
  registry base all raise `TypeError` at class-definition time naming the method
  you forgot, rather than a `NotImplementedError` on the first request that
  needs it. If you add a base class with abstract methods, add the same guard —
  `veloce._internal._require_methods` does it in three lines.
- **A structural invariant gets a test that walks the structure.** When a field
  was added to `RouteInfo` recently, `test_route_field_parity.py` failed
  immediately because two route-copy paths had not been updated. No reviewer
  would reliably have caught that.
- **Validate configuration with `ValueError`, not `assert`, on any security
  surface.** `python -O` strips assert statements; a middleware that validated
  its arguments with one constructed happily under `-O` and then emitted no
  header at all. `AssertionError` remains fine for API misuse that fails loudly
  anyway.

### What this means for a pull request

Adding a feature: a test for the behaviour, and a test for anything the docs
claim about it. Fixing a bug: a test that fails on the commit before yours.
Changing something structural: ask what would catch the next person forgetting,
and add that.

## Commit messages

Write commit messages and pull request descriptions that state **what** changed
in the code or docs, and the result of the change. Use plain, imperative
summaries, for example:

```
fix cookie validation duplication
refactor middleware config normalization
```

Do not include attribution, tooling notes, or origin/process narration - the
diff speaks for the change.

## Reporting security issues

Do not open a public issue for a security vulnerability. Follow the process in
[SECURITY.md](SECURITY.md) instead.

## Becoming a maintainer

Veloce is actively looking for contributors and co-maintainers. The path is
straightforward: land a few quality pull requests, help triage issues and review
other people's PRs, and you'll be offered triage and then commit access. If you'd
like to own an area - the docs, a specific module, the MCP layer - say so in an
issue; maintenance is shared with the people who show up.

## Code of conduct

Participation in this project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).
