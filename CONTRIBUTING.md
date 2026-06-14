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
```

- Tests use `pytest-asyncio` in auto mode: write async tests as plain
  `async def test_*` functions (do **not** add `@pytest.mark.asyncio`).
- Add a regression test for every bug fix, placed in the test module for the
  feature it exercises - not in a catch-all file.
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
