"""RFC 6570 variables in a resource URI template.

A template binds its variables to the route's path parameters. The spec's two
expansion forms mean different things, and both now do:

- `{name}` is simple expansion - one URI segment, with reserved characters
  arriving percent-encoded, so the value is decoded before the handler sees it.
- `{+name}` is reserved expansion - the value carries `/` literally, so a whole
  path binds to one variable. A file tree could not be addressed without it.
"""

from __future__ import annotations

import orjson
import pytest

from tests._mcp import RESOURCE_NOT_FOUND
from veloce import Veloce
from veloce.contrib.mcp.resources import _template_specificity, build_resource_registry
from veloce.contrib.mcp.server import MCPServer
from veloce.contrib.mcp.session import MCPSession


def _app(uri: str, path: str = "/files/{path}") -> Veloce:
    app = Veloce(title="Templates", version="1.0.0", openapi_url=None)

    @app.get(path, expose_as_mcp_resource=True, mcp_resource_uri=uri, mcp_description="A file")
    async def read_file(path: str) -> dict:
        return {"received": path}

    return app


async def _read(app: Veloce, uri: str) -> dict:
    return await MCPServer(app).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {"uri": uri}},
        MCPSession(),
    )


async def _value(app: Veloce, uri: str) -> str:
    response = await _read(app, uri)
    assert "error" not in response, response["error"]
    return orjson.loads(response["result"]["contents"][0]["text"])["received"]


# ── Reserved expansion spans segments ────────────────────────────────


async def test_a_reserved_variable_matches_a_whole_path():
    assert await _value(_app("file://{+path}"), "file://a/b/c.py") == "a/b/c.py"


async def test_a_reserved_variable_also_matches_one_segment():
    assert await _value(_app("file://{+path}"), "file://note.txt") == "note.txt"


async def test_a_simple_variable_still_matches_only_one_segment():
    """Simple expansion is one segment; a path is what `{+name}` is for."""
    response = await _read(_app("file://{path}"), "file://a/b/c.py")
    assert response["error"]["code"] == RESOURCE_NOT_FOUND


async def test_a_simple_variable_matches_a_single_segment():
    assert await _value(_app("file://{path}"), "file://note.txt") == "note.txt"


# ── Percent-decoding ─────────────────────────────────────────────────


async def test_an_encoded_reserved_character_is_decoded():
    """The client encoded `/` to carry it inside one segment."""
    assert await _value(_app("file://{path}"), "file://a%2Fb.py") == "a/b.py"


async def test_an_encoded_space_is_decoded():
    assert await _value(_app("file://{path}"), "file://my%20notes.txt") == "my notes.txt"


async def test_a_reserved_variable_decodes_too():
    assert await _value(_app("file://{+path}"), "file://docs/my%20notes.txt") == (
        "docs/my notes.txt"
    )


async def test_a_value_needing_no_decoding_is_passed_through():
    assert await _value(_app("file://{path}"), "file://plain.txt") == "plain.txt"


async def test_a_percent_that_is_not_an_escape_survives():
    """`unquote` leaves a stray `%` alone rather than failing the read."""
    assert await _value(_app("file://{path}"), "file://100%.txt") == "100%.txt"


# ── Which template wins ──────────────────────────────────────────────


def _two_template_app(greedy_first: bool) -> Veloce:
    app = Veloce(title="Both", openapi_url=None)

    def add_greedy() -> None:
        @app.get(
            "/any/{path}",
            expose_as_mcp_resource=True,
            mcp_resource_uri="file://{+path}",
            mcp_description="Any file",
        )
        async def any_file(path: str) -> dict:
            return {"matched": "greedy", "received": path}

    def add_specific() -> None:
        @app.get(
            "/meta/{name}",
            expose_as_mcp_resource=True,
            mcp_resource_uri="file://{name}/meta",
            mcp_description="Metadata",
        )
        async def meta(name: str) -> dict:
            return {"matched": "specific", "received": name}

    if greedy_first:
        add_greedy()
        add_specific()
    else:
        add_specific()
        add_greedy()
    return app


@pytest.mark.parametrize("greedy_first", [True, False])
async def test_a_specific_template_wins_over_a_greedy_one(greedy_first: bool):
    """A greedy template matches everything, so it must not win on order."""
    response = await _read(_two_template_app(greedy_first), "file://report/meta")
    payload = orjson.loads(response["result"]["contents"][0]["text"])
    assert payload["matched"] == "specific"
    assert payload["received"] == "report"


@pytest.mark.parametrize("greedy_first", [True, False])
async def test_the_greedy_template_still_serves_what_nothing_else_matches(greedy_first: bool):
    response = await _read(_two_template_app(greedy_first), "file://a/b/c.py")
    payload = orjson.loads(response["result"]["contents"][0]["text"])
    assert payload["matched"] == "greedy"
    assert payload["received"] == "a/b/c.py"


async def test_a_static_resource_still_wins_over_a_greedy_template():
    app = _two_template_app(greedy_first=True)

    @app.get(
        "/config",
        expose_as_mcp_resource=True,
        mcp_resource_uri="file://config",
        mcp_description="Config",
    )
    async def config() -> dict:
        return {"matched": "static"}

    response = await _read(app, "file://config")
    payload = orjson.loads(response["result"]["contents"][0]["text"])
    assert payload["matched"] == "static"


# ── What the listing advertises ──────────────────────────────────────


async def test_the_listing_advertises_the_template_verbatim():
    """A client expands the template itself, so it must see the operator."""
    response = await MCPServer(_app("file://{+path}")).handle_message(
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list", "params": {}},
        MCPSession(),
    )
    assert response["result"]["resourceTemplates"][0]["uriTemplate"] == "file://{+path}"


# ── Declaration is still checked ─────────────────────────────────────


def test_a_reserved_variable_binds_its_path_parameter():
    """`{+path}` names `path`, so the operator is not part of the name."""

    registry = build_resource_registry(_app("file://{+path}"))
    assert registry.templates()[0].uri_param_names == ("path",)


def test_a_template_variable_that_names_no_path_parameter_is_refused():

    with pytest.raises(ValueError, match="must match its path parameters"):
        build_resource_registry(_app("file://{+wrong}"))


def test_an_unrecognised_placeholder_is_not_taken_as_a_variable():
    """`{-x}` is not an RFC 6570 form this server serves; it is not a binding."""

    with pytest.raises(ValueError, match="must match its path parameters"):
        build_resource_registry(_app("file://{-x}"))


# ── More than one variable ───────────────────────────────────────────


async def test_a_template_may_mix_both_forms():
    app = Veloce(title="Mixed", openapi_url=None)

    @app.get(
        "/repo/{repo}/blob/{path}",
        expose_as_mcp_resource=True,
        mcp_resource_uri="repo://{repo}/blob/{+path}",
        mcp_description="A file in a repository",
    )
    async def blob(repo: str, path: str) -> dict:
        return {"repo": repo, "path": path}

    response = await _read(app, "repo://veloce/blob/src/veloce/app/core.py")
    payload = orjson.loads(response["result"]["contents"][0]["text"])
    assert payload == {"repo": "veloce", "path": "src/veloce/app/core.py"}


# ── Two catch-alls ───────────────────────────────────────────────────


def _two_greedy_app(catch_all_first: bool) -> Veloce:
    """A catch-all and a longer template that also spans segments."""
    app = Veloce(title="TwoGreedy", openapi_url=None)

    def add_catch_all() -> None:
        @app.get(
            "/any/{path}",
            expose_as_mcp_resource=True,
            mcp_resource_uri="docs://{+path}",
            mcp_description="Any document",
        )
        async def any_doc(path: str) -> dict:
            return {"matched": "catch-all", "received": path}

    def add_meta() -> None:
        @app.get(
            "/meta/{path}",
            expose_as_mcp_resource=True,
            mcp_resource_uri="docs://{+path}/meta",
            mcp_description="Metadata",
        )
        async def meta(path: str) -> dict:
            return {"matched": "meta", "received": path}

    if catch_all_first:
        add_catch_all()
        add_meta()
    else:
        add_meta()
        add_catch_all()
    return app


@pytest.mark.parametrize("catch_all_first", [True, False])
async def test_the_longer_template_wins_between_two_catch_alls(catch_all_first: bool):
    """Both span segments, so only specificity separates them - not order."""
    response = await _read(_two_greedy_app(catch_all_first), "docs://a/b/c.md/meta")
    payload = orjson.loads(response["result"]["contents"][0]["text"])
    assert payload["matched"] == "meta"
    assert payload["received"] == "a/b/c.md"


@pytest.mark.parametrize("catch_all_first", [True, False])
async def test_the_catch_all_still_serves_the_rest(catch_all_first: bool):
    response = await _read(_two_greedy_app(catch_all_first), "docs://a/b/c.md")
    payload = orjson.loads(response["result"]["contents"][0]["text"])
    assert payload["matched"] == "catch-all"


def test_specificity_counts_the_literal_characters():

    assert _template_specificity("docs://{+path}/meta") > _template_specificity("docs://{+path}")
    assert _template_specificity("docs://index") == len("docs://index")
    assert _template_specificity("{+path}") == 0
