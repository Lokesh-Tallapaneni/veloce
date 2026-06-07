# Contributing to Veloce

Thanks for your interest in improving Veloce. This guide covers how to set up a
development environment, the checks your change must pass, and how to propose it.

## Development setup

Veloce targets Python 3.10+.

```bash
git clone https://github.com/Lokesh-Tallapaneni/veloce.git
cd veloce
pip install -e ".[dev]"
```

The project uses [uv](https://docs.astral.sh/uv/) for environment management;
if you have it installed, `uv sync --all-extras --dev` sets up a locked
environment and you can run any command below through `uv run <command>`.

## Quality gates

Every change must pass the full gate set before it is submitted. These are the
same checks CI runs on each pull request:

```bash
pytest                          # full test suite
ruff check .                    # lint
ruff format --check .           # formatting
mypy src/veloce                 # static types
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

## Code of conduct

Participation in this project is governed by the
[Code of Conduct](CODE_OF_CONDUCT.md).
