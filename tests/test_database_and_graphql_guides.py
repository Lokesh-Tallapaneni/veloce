"""`docs/guide/databases.md` and `docs/how-to/graphql.md`, held to the code.

Both pages lean on libraries that were not installed here, so their
library-specific claims had never been executed. With SQLAlchemy, aiosqlite and
Strawberry installed, both were swept in full.

**The GraphQL page did not install what it imports.** Its install line named the
base distribution, and the very next block does `from strawberry.asgi import
GraphQL`. That integration lives behind an `[asgi]` extra, so a reader following
the page got a `ModuleNotFoundError` on the first import - the first thing they
ran. The install line now names the extra.

The extra pulls in a third-party ASGI toolkit that Strawberry's integration is
written against. The page says so, and says it belongs to the *mounted*
application rather than to Veloce, which does not use it - a distinction a reader
will want given this framework's premise. The test below derives that
dependency's name from the installed package's own metadata rather than naming
it here.

**And the page named a class that does not exist.** The prose used the name from
Strawberry's integration for a *different* framework; `strawberry.asgi` has no
such attribute. The code blocks below it correctly import `GraphQL`.

Everything else on both pages checked out, and the substantive behavioural claims
are pinned here rather than taken on trust: the `yield`-dependency session really
does roll back on error, a mounted ASGI app really does receive no `lifespan`
scope, and a `(body, status)` tuple return really is honoured.
"""

from __future__ import annotations

import pathlib

import pytest

from tests._markdown import blocks
from veloce import Veloce
from veloce.testclient import TestClient

sqlalchemy = pytest.importorskip("sqlalchemy", reason="SQLAlchemy is not installed")
pytest.importorskip("aiosqlite", reason="aiosqlite is not installed")

GUIDE = pathlib.Path(__file__).resolve().parents[1] / "docs/guide/databases.md"
HOWTO = pathlib.Path(__file__).resolve().parents[1] / "docs/how-to/graphql.md"

from sqlalchemy import Integer, String, select  # noqa: E402
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column  # noqa: E402


class Base(DeclarativeBase):
    """Module scope: SQLAlchemy resolves `Mapped[...]` by name, and this file
    uses PEP 563, so a locally-defined model cannot be resolved."""


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String)


# ── the GraphQL page installs what it imports ────────────────────────


def test_the_install_line_names_the_asgi_extra():
    """The defect: the page installed the base package and imported the extra."""
    text = HOWTO.read_text(encoding="utf-8")
    assert "strawberry-graphql[asgi]" in text
    assert "pip install veloceframework strawberry-graphql\n" not in text


def test_the_page_names_the_class_that_exists():
    """The old name belongs to Strawberry's integration for another framework."""
    text = HOWTO.read_text(encoding="utf-8")
    assert "GraphQLRouter" not in text
    assert "strawberry.asgi.GraphQL" in text


def test_the_named_class_is_importable():
    strawberry_asgi = pytest.importorskip(
        "strawberry.asgi", reason="strawberry-graphql[asgi] is not installed"
    )
    assert hasattr(strawberry_asgi, "GraphQL")
    assert not hasattr(strawberry_asgi, "GraphQLRouter")


def _asgi_extra_dependency() -> str:
    """The distribution Strawberry's `[asgi]` extra pulls in, from its metadata.

    Read at runtime rather than written here: this repository's source and tests
    do not name other web frameworks, and deriving it also makes the assertion
    stronger - the page has to name whatever the extra *actually* requires.
    """
    import importlib.metadata as metadata

    for requirement in metadata.distribution("strawberry-graphql").requires or []:
        if "extra ==" in requirement and "'asgi'" in requirement:
            return requirement.split(";")[0].split()[0].split("<")[0].split(">")[0].split("=")[0]
    pytest.skip("the asgi extra declares no requirement to check")


def test_the_page_names_the_dependency_the_extra_pulls_in():
    """A reader will ask where the new third-party package came from."""
    assert _asgi_extra_dependency().lower() in HOWTO.read_text(encoding="utf-8").lower()


def test_the_page_says_the_dependency_is_not_veloces():
    """It belongs to the mounted application, which is the point worth making."""
    text = HOWTO.read_text(encoding="utf-8")
    # Whitespace-normalised: the phrase wraps across lines in the source.
    flat = " ".join(text.split())
    assert "not of Veloce" in flat


def test_the_mounted_graphql_example_answers():
    """The page's own assertions, run."""
    strawberry = pytest.importorskip("strawberry")
    graphql_asgi = pytest.importorskip(
        "strawberry.asgi", reason="strawberry-graphql[asgi] is not installed"
    )

    @strawberry.type
    class Query:
        @strawberry.field
        def hello(self) -> str:
            return "Hello from GraphQL"

    app = Veloce(openapi_url=None)
    app.mount("/graphql", graphql_asgi.GraphQL(strawberry.Schema(Query)))

    @app.get("/")
    async def index(request) -> dict:
        return {"app": "main"}

    client = TestClient(app)
    assert client.get("/").json() == {"app": "main"}
    result = client.post("/graphql", json={"query": "{ hello }"})
    assert result.status_code == 200
    assert result.json() == {"data": {"hello": "Hello from GraphQL"}}


# ── a mounted ASGI app gets no lifespan ──────────────────────────────


def test_a_mounted_app_receives_no_lifespan_scope():
    """The page's note, measured."""
    seen: list[str] = []

    async def sub(scope, receive, send):
        seen.append(scope["type"])
        if scope["type"] == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"text/plain")],
                }
            )
            await send({"type": "http.response.body", "body": b"sub"})

    app = Veloce(openapi_url=None)
    app.mount("/sub", sub)

    @app.get("/")
    async def index() -> dict:
        return {"app": "main"}

    with TestClient(app) as client:
        client.get("/")
        client.get("/sub/thing")

    assert seen == ["http"]
    assert "lifespan" not in seen


def test_the_parent_lifecycle_still_runs():
    """Which is the page's point: the parent owns the resource."""
    app = Veloce(openapi_url=None)

    @app.on_startup
    async def open_pool() -> None:
        app.state.pool = "open"

    @app.get("/")
    async def index() -> dict:
        return {"pool": app.state.pool}

    with TestClient(app) as client:
        assert client.get("/").json() == {"pool": "open"}


# ── the database page's session contract ─────────────────────────────


def _db_app():
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    from veloce import Depends

    app = Veloce(openapi_url=None)

    @app.on_startup
    async def open_database() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        app.state.db_engine = engine
        app.state.db_sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

    @app.on_shutdown
    async def close_database() -> None:
        await app.state.db_engine.dispose()

    async def get_session() -> AsyncIterator[AsyncSession]:
        async with app.state.db_sessionmaker() as session:
            yield session

    @app.post("/users")
    async def create(name: str, session: AsyncSession = Depends(get_session)) -> dict:
        session.add(User(name=name))
        await session.commit()
        return {"ok": True}

    @app.post("/boom")
    async def boom(name: str, session: AsyncSession = Depends(get_session)) -> dict:
        session.add(User(name=name))
        await session.flush()
        raise RuntimeError("handler failed")

    @app.get("/users")
    async def listing(session: AsyncSession = Depends(get_session)) -> dict:
        rows = (await session.execute(select(User.name))).scalars().all()
        return {"names": list(rows)}

    @app.get("/missing")
    async def missing() -> dict:
        return {"detail": "not found"}, 404

    return app


def test_a_committed_write_is_visible():
    client = TestClient(_db_app())
    assert client.post("/users?name=ada").json() == {"ok": True}
    assert client.get("/users").json() == {"names": ["ada"]}


def test_a_failing_handler_rolls_its_session_back():
    """The page's promise: "always closed (and rolled back on error)"."""
    client = TestClient(_db_app())
    client.post("/users?name=ada")
    assert client.post("/boom?name=ghost").status_code == 500
    assert client.get("/users").json() == {"names": ["ada"]}


def test_each_request_gets_its_own_session():
    """A shared session would leak one request's transaction into the next."""
    client = TestClient(_db_app())
    client.post("/users?name=ada")
    client.post("/users?name=grace")
    assert sorted(client.get("/users").json()["names"]) == ["ada", "grace"]


def test_the_tuple_return_form_the_page_uses():
    """`return {...}, 404` - used in the page's read_user example."""
    response = TestClient(_db_app()).get("/missing")
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}


# ── both pages stay runnable ─────────────────────────────────────────


@pytest.mark.parametrize("page", [GUIDE, HOWTO], ids=["databases", "graphql"])
def test_every_python_block_parses(page):
    for line_no, lang, code in blocks(page):
        if lang == "python":
            compile(code, f"{page.name}:{line_no}", "exec")


@pytest.mark.parametrize("page", [GUIDE, HOWTO], ids=["databases", "graphql"])
def test_the_page_runs_cumulatively(page):
    """Blocks build one file, so the fair check is the whole page in order."""
    import veloce

    blocking = ("app.run(", "uvicorn.run", "while True", "asyncio.run(")
    namespace = {n: getattr(veloce, n) for n in veloce.__all__}
    namespace["app"] = Veloce(title="Guide", version="1.0.0", openapi_url=None)
    namespace["__name__"] = "__main__"
    for line_no, lang, code in blocks(page):
        if lang != "python" or any(b in code for b in blocking):
            continue
        try:
            exec(compile(code, f"{page.name}:{line_no}", "exec"), namespace)
        except ModuleNotFoundError as exc:
            pytest.skip(f"{page.name}:{line_no} needs {exc.name}")
        except Exception as exc:  # noqa: BLE001 - the point is to report it
            pytest.fail(f"{page.name}:{line_no} raised {type(exc).__name__}: {exc}")


@pytest.mark.parametrize("page", [GUIDE, HOWTO], ids=["databases", "graphql"])
def test_every_install_line_names_a_real_distribution(page):
    """The defect this file exists for: an install line that omits an extra.

    Every distribution an install line names must resolve, and every extra it
    asks for must be one that distribution declares - which is exactly what the
    GraphQL page got wrong.
    """
    import importlib.metadata as metadata

    open_bracket, close_bracket = chr(91), chr(93)
    for line in page.read_text(encoding="utf-8").splitlines():
        if "pip install " not in line:
            continue
        for token in line.split("pip install ", 1)[1].split():
            token = token.strip('"')
            name, _, extra = token.partition(open_bracket)
            extra = extra.rstrip(close_bracket)
            try:
                dist = metadata.distribution(name)
            except metadata.PackageNotFoundError:
                pytest.skip(f"{name} is not installed here")
            if extra:
                declared = dist.metadata.get_all("Provides-Extra") or []
                assert extra in declared, f"{name} declares no {extra!r} extra"
