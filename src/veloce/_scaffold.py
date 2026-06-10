"""Project and file scaffolding for the `veloce new` / `veloce generate` commands.

Templates are plain in-module strings (no Jinja dependency, matching the CLI's
stdlib-only footprint) carrying `__PROJECT__` / `__SNAKE__` / `__PASCAL__` tokens
that `_render` substitutes. Generated projects import the public `veloce` surface
and are written to pass `ruff` and `mypy` out of the box.
"""

from __future__ import annotations

import re
from pathlib import Path

# ── Naming helpers ───────────────────────────────────────────────────


def _snake(name: str) -> str:
    """Return a snake_case identifier derived from `name`."""
    cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", name).strip("_")
    cleaned = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", cleaned)
    snake = cleaned.lower() or "app"
    if snake[0].isdigit():
        snake = f"_{snake}"
    return snake


def _pascal(name: str) -> str:
    """Return a PascalCase identifier derived from `name`."""
    parts = [p for p in re.split(r"[^0-9a-zA-Z]+", name) if p]
    pascal = "".join(p[:1].upper() + p[1:] for p in parts) or "App"
    if pascal[0].isdigit():
        pascal = f"_{pascal}"
    return pascal


def _render(text: str, *, project: str, snake: str, pascal: str) -> str:
    """Substitute the scaffold tokens and normalise to a trailing newline."""
    body = (
        text.replace("__PROJECT__", project)
        .replace("__SNAKE__", snake)
        .replace("__PASCAL__", pascal)
    )
    return body.strip("\n") + "\n"


# ── Shared files ─────────────────────────────────────────────────────

_GITIGNORE = """
__pycache__/
*.py[cod]
.venv/
.env
.mypy_cache/
.ruff_cache/
.pytest_cache/
dist/
build/
*.egg-info/
"""

# `[tool.uv] package = false` marks this an application (not a library), so
# `uv run`/`uv sync` manage the environment without trying to build a wheel from
# the flat `app.py` layout - the project runs with `uv run` or plain `pip`.
_PYPROJECT_TEMPLATE = """
[project]
name = "__SNAKE__"
version = "0.1.0"
description = "A Veloce application."
requires-python = ">=3.10"
dependencies = [
__DEPS__
]

[dependency-groups]
dev = ["pytest"]

[tool.uv]
package = false

[tool.ruff]
line-length = 100
target-version = "py310"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]

[tool.mypy]
python_version = "3.10"
warn_unused_ignores = true

[tool.pytest.ini_options]
testpaths = ["tests"]
"""


def _pyproject(deps: list[str]) -> str:
    """Return the pyproject template with `deps` rendered into the array."""
    dep_lines = "\n".join(f'    "{dep}",' for dep in deps)
    return _PYPROJECT_TEMPLATE.replace("__DEPS__", dep_lines)


_TESTS_INIT = ""

# ── minimal ──────────────────────────────────────────────────────────

_MINIMAL_APP = """
from __future__ import annotations

from veloce import Veloce

app = Veloce(title="__PROJECT__")


@app.get("/")
async def index() -> dict[str, str]:
    return {"message": "Welcome to __PROJECT__"}


if __name__ == "__main__":
    app.run()
"""

_MINIMAL_TEST = """
from __future__ import annotations

from veloce import TestClient

from app import app


def test_index() -> None:
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert response.json() == {"message": "Welcome to __PROJECT__"}
"""

_MINIMAL_README = """
# __PROJECT__

A [Veloce](https://github.com/Lokesh-Tallapaneni/veloce) application.

## Run

With [uv](https://docs.astral.sh/uv/) (recommended):

    uv run veloce run app:app --reload
    uv run pytest

Or with pip:

    pip install veloceframework pytest
    veloce run app:app --reload
    pytest

Then open http://127.0.0.1:8000.
"""

# ── api ──────────────────────────────────────────────────────────────

_API_APP = """
from __future__ import annotations

from veloce import Veloce

from api import api

app = Veloce(title="__PROJECT__")
app.register_blueprint(api)


@app.get("/")
async def index() -> dict[str, str]:
    return {"service": "__PROJECT__", "docs": "/docs"}


if __name__ == "__main__":
    app.run()
"""

_API_MODELS = """
from __future__ import annotations

from pydantic import BaseModel


class ItemIn(BaseModel):
    name: str
    price: float


class Item(ItemIn):
    id: int
"""

_API_ROUTER = """
from __future__ import annotations

from typing import Annotated

from veloce import Blueprint, Depends, HTTPException

from models import Item, ItemIn

api = Blueprint("api", url_prefix="/api")

# A trivial in-memory store stands in for a database so the starter runs with
# no external services. Swap `get_store` for a real session dependency.
_ITEMS: dict[int, Item] = {}
_NEXT_ID = {"value": 1}


def get_store() -> dict[int, Item]:
    return _ITEMS


@api.get("/items")
async def list_items(store: Annotated[dict[int, Item], Depends(get_store)]) -> list[Item]:
    return list(store.values())


@api.post("/items", status_code=201)
async def create_item(
    payload: ItemIn,
    store: Annotated[dict[int, Item], Depends(get_store)],
) -> Item:
    item = Item(id=_NEXT_ID["value"], **payload.model_dump())
    _NEXT_ID["value"] += 1
    store[item.id] = item
    return item


@api.get("/items/{item_id}")
async def get_item(
    item_id: int,
    store: Annotated[dict[int, Item], Depends(get_store)],
) -> Item:
    item = store.get(item_id)
    if item is None:
        raise HTTPException(404, "item not found")
    return item
"""

_API_TEST = """
from __future__ import annotations

from veloce import TestClient

from app import app


def test_create_and_fetch_item() -> None:
    client = TestClient(app)

    created = client.post("/api/items", json={"name": "widget", "price": 9.5})
    assert created.status_code == 201
    item = created.json()
    assert item["id"] >= 1
    assert item["name"] == "widget"

    fetched = client.get(f"/api/items/{item['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "widget"


def test_missing_item_is_404() -> None:
    assert TestClient(app).get("/api/items/999999").status_code == 404
"""

_API_README = """
# __PROJECT__

A [Veloce](https://github.com/Lokesh-Tallapaneni/veloce) JSON API with Pydantic
models, a router blueprint, and dependency injection.

## Run

With [uv](https://docs.astral.sh/uv/) (recommended):

    uv run veloce run app:app --reload
    uv run pytest

Or with pip:

    pip install veloceframework pydantic pytest
    veloce run app:app --reload
    pytest

- API: http://127.0.0.1:8000/api/items
- Interactive docs: http://127.0.0.1:8000/docs
"""

# ── web ──────────────────────────────────────────────────────────────

_WEB_APP = """
from __future__ import annotations

from veloce import HTMLResponse, Veloce, render_template

app = Veloce(title="__PROJECT__", template_folder="templates")
app.mount_static(prefix="/static", directory="static", must_exist=False)


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(render_template("index.html", title="__PROJECT__"))


if __name__ == "__main__":
    app.run()
"""

_WEB_BASE_HTML = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{% block title %}__PROJECT__{% endblock %}</title>
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
  <main>{% block content %}{% endblock %}</main>
</body>
</html>
"""

_WEB_INDEX_HTML = """
{% extends "base.html" %}
{% block content %}
  <h1>{{ title }}</h1>
  <p>Your Veloce app is running. Edit <code>templates/index.html</code> to begin.</p>
{% endblock %}
"""

_WEB_CSS = """
:root { color-scheme: light dark; }
body { font-family: system-ui, sans-serif; margin: 0; }
main { max-width: 40rem; margin: 4rem auto; padding: 0 1rem; }
h1 { margin-bottom: 0.5rem; }
code { background: rgba(127, 127, 127, 0.2); padding: 0.1rem 0.3rem; border-radius: 0.2rem; }
"""

_WEB_TEST = """
from __future__ import annotations

from veloce import TestClient

from app import app


def test_index_renders_html() -> None:
    response = TestClient(app).get("/")
    assert response.status_code == 200
    assert "__PROJECT__" in response.text
    assert response.headers["content-type"].startswith("text/html")
"""

_WEB_README = """
# __PROJECT__

A server-rendered [Veloce](https://github.com/Lokesh-Tallapaneni/veloce) web app
with Jinja templates and static files.

## Run

With [uv](https://docs.astral.sh/uv/) (recommended):

    uv run veloce run app:app --reload
    uv run pytest

Or with pip:

    pip install veloceframework jinja2 pytest
    veloce run app:app --reload
    pytest

Then open http://127.0.0.1:8000.
"""

# ── Project template registry ────────────────────────────────────────

# Each template maps a destination relative path to its source content. The
# common files (.gitignore, tests package) are shared; the pyproject carries
# per-template dependencies, and the app code, tests, and README differ.
_COMMON = {
    ".gitignore": _GITIGNORE,
    "tests/__init__.py": _TESTS_INIT,
}

PROJECT_TEMPLATES: dict[str, dict[str, str]] = {
    "minimal": {
        **_COMMON,
        "pyproject.toml": _pyproject(["veloceframework"]),
        "app.py": _MINIMAL_APP,
        "tests/test_app.py": _MINIMAL_TEST,
        "README.md": _MINIMAL_README,
    },
    "api": {
        **_COMMON,
        "pyproject.toml": _pyproject(["veloceframework", "pydantic"]),
        "app.py": _API_APP,
        "models.py": _API_MODELS,
        "api.py": _API_ROUTER,
        "tests/test_app.py": _API_TEST,
        "README.md": _API_README,
    },
    "web": {
        **_COMMON,
        "pyproject.toml": _pyproject(["veloceframework", "jinja2"]),
        "app.py": _WEB_APP,
        "templates/base.html": _WEB_BASE_HTML,
        "templates/index.html": _WEB_INDEX_HTML,
        "static/style.css": _WEB_CSS,
        "tests/test_app.py": _WEB_TEST,
        "README.md": _WEB_README,
    },
}

PROJECT_TEMPLATE_NAMES = tuple(PROJECT_TEMPLATES)

# ── File generators (`veloce generate`) ──────────────────────────────

_GEN_ROUTE = """
from __future__ import annotations

from veloce import Blueprint

# Register on an app with: app.register_blueprint(__SNAKE___router)
__SNAKE___router = Blueprint("__SNAKE__")


@__SNAKE___router.get("/__SNAKE__")
async def __SNAKE__() -> dict[str, str]:
    return {"resource": "__SNAKE__"}
"""

_GEN_BLUEPRINT = """
from __future__ import annotations

from veloce import Blueprint, HTTPException

# Register on an app with: app.register_blueprint(__SNAKE__)
__SNAKE__ = Blueprint("__SNAKE__", url_prefix="/__SNAKE__")

_STORE: dict[int, dict[str, object]] = {}


@__SNAKE__.get("/")
async def list_all() -> list[dict[str, object]]:
    return list(_STORE.values())


@__SNAKE__.get("/{item_id}")
async def get_one(item_id: int) -> dict[str, object]:
    item = _STORE.get(item_id)
    if item is None:
        raise HTTPException(404, "not found")
    return item
"""

_GEN_MIDDLEWARE = """
from __future__ import annotations

from veloce import Middleware, Request, Response


class __PASCAL__Middleware(Middleware):
    \"\"\"Add an `X-__PASCAL__` header to every response.\"\"\"

    async def process_request(self, request: Request) -> Response | None:
        # Return a Response here to short-circuit, or None to continue.
        return None

    async def process_response(self, request: Request, response: Response) -> Response:
        response.headers["X-__PASCAL__"] = "1"
        return response
"""

_GEN_MODEL = """
from __future__ import annotations

from pydantic import BaseModel


class __PASCAL__(BaseModel):
    id: int
    name: str
"""

_GEN_SECURITY = """
from __future__ import annotations

from typing import Annotated

from veloce import APIKeyHeader, HTTPException, Security

__SNAKE___scheme = APIKeyHeader(name="X-API-Key")

# Replace with a real lookup (database, secret store, ...).
_VALID_KEYS = {"change-me"}


async def require___SNAKE__(api_key: Annotated[str, Security(__SNAKE___scheme)]) -> str:
    if api_key not in _VALID_KEYS:
        raise HTTPException(401, "invalid API key")
    return api_key
"""

GENERATORS: dict[str, str] = {
    "route": _GEN_ROUTE,
    "blueprint": _GEN_BLUEPRINT,
    "middleware": _GEN_MIDDLEWARE,
    "model": _GEN_MODEL,
    "security": _GEN_SECURITY,
}

GENERATOR_KINDS = tuple(GENERATORS)


# ── Public operations ────────────────────────────────────────────────


class ScaffoldError(Exception):
    """A scaffolding request could not be fulfilled (bad target, collision)."""


def _ensure_parent_dir(target: Path) -> None:
    """Create `target.parent`, mapping any path-chain error to `ScaffoldError`.

    `mkdir(parents=True)` leaks a raw `FileExistsError` / `NotADirectoryError`
    when an ancestor component is a file (e.g. `--dir` points through a file);
    surface a clean `ScaffoldError` instead.
    """
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        raise ScaffoldError(
            f"cannot create {target.parent}: a path component is not a directory"
        ) from err


def scaffold_project(
    name: str, template: str, dest_root: Path, *, force: bool = False
) -> list[Path]:
    """Write a new project named `name` from `template` under `dest_root`.

    Returns the list of files written. Raises `ScaffoldError` if the target
    directory already contains files and `force` is not set.
    """
    if template not in PROJECT_TEMPLATES:
        raise ScaffoldError(
            f"unknown template {template!r}; choose one of {', '.join(PROJECT_TEMPLATE_NAMES)}"
        )
    project_dir = dest_root / name
    if project_dir.exists():
        if not project_dir.is_dir():
            raise ScaffoldError(f"{project_dir} exists and is not a directory")
        if any(project_dir.iterdir()) and not force:
            raise ScaffoldError(
                f"{project_dir} already exists and is not empty; pass --force to overwrite"
            )

    snake, pascal = _snake(name), _pascal(name)
    written: list[Path] = []
    for rel_path, raw in PROJECT_TEMPLATES[template].items():
        target = project_dir / rel_path
        _ensure_parent_dir(target)
        content = "" if raw == "" else _render(raw, project=name, snake=snake, pascal=pascal)
        target.write_text(content, encoding="utf-8")
        written.append(target)
    return written


def generate_file(
    kind: str, name: str, dest_dir: Path, *, force: bool = False, to_stdout: bool = False
) -> tuple[Path | None, str]:
    """Render a `kind` file for `name`. Returns `(path_or_None, content)`.

    With `to_stdout=True` the content is returned without writing (path is None).
    Raises `ScaffoldError` on an unknown kind or an existing file without `force`.
    """
    if kind not in GENERATORS:
        raise ScaffoldError(f"unknown kind {kind!r}; choose one of {', '.join(GENERATOR_KINDS)}")
    snake, pascal = _snake(name), _pascal(name)
    content = _render(GENERATORS[kind], project=name, snake=snake, pascal=pascal)
    if to_stdout:
        return None, content
    if dest_dir.exists() and not dest_dir.is_dir():
        raise ScaffoldError(f"{dest_dir} exists and is not a directory")
    target = dest_dir / f"{snake}.py"
    if target.exists() and not force:
        raise ScaffoldError(f"{target} already exists; pass --force to overwrite")
    _ensure_parent_dir(target)
    target.write_text(content, encoding="utf-8")
    return target, content
