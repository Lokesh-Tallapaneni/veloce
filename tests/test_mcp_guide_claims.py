"""What `docs/guide/mcp.md` promises, held to the code.

The page is 2,400 lines and had only ever been sampled. Sweeping it in full found
one framework bug, one security-relevant example, and six blocks that could not
run.

**The framework bug** the sweep found - two `mount_mcp` calls colliding on route
names - is asserted in `test_mcp_mount_route_names.py`, which owns that
behaviour and records it in full. It is named here only because this sweep is
what found it.

**The security-relevant example.** The "issuing the tokens yourself" section built
its verifier with `authorization.verifier()` while passing
`resource_server_url=` to `MCPAuth` two lines below. The URI was right there and
not handed to the check that uses it, so every reader copying the example got
audience binding switched off — and, since the same review made that warn, a
startup warning they did not ask for.

**Version rejection was documented unqualified.** "A request declaring a version
the server does not serve is rejected with `-32022`" is true of a *modern*
request, which declares its revision in `_meta`. A handshake `initialize`
negotiates instead, answering with a version it does serve — which is what a
handshake is for.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

import veloce
from tests._markdown import blocks
from tests._mcp import UNSUPPORTED_PROTOCOL_VERSION, initialize
from veloce import Principal, Veloce
from veloce.config import Config
from veloce.contrib.mcp import MCPAuth, MCPAuthorizationServer
from veloce.contrib.mcp.context import MCPContext
from veloce.contrib.mcp.server import SERVED_PROTOCOL_VERSIONS
from veloce.testclient import TestClient

GUIDE = pathlib.Path(__file__).resolve().parents[1] / "docs/guide/mcp.md"
INITIALIZE = initialize()


def _tool_app() -> Veloce:
    app = Veloce(title="Guide", version="1.0.0", openapi_url=None)

    @app.mcp_tool(description="A tool")
    async def probe() -> str:
        return "ok"

    return app


# ── the guide's auth example enforces audience binding ───────────────


def test_the_auth_example_passes_resource_to_the_verifier():
    """The defect: the URI was two lines below and never handed to the check."""
    text = GUIDE.read_text(encoding="utf-8")
    assert "authorization.verifier(resource=" in text
    assert "verify=authorization.verifier()," not in text


def test_building_that_example_emits_no_warning(recwarn):
    """A reader copying it must not inherit a switched-off security check."""
    server = MCPAuthorizationServer(
        issuer="https://api.example.com",
        authenticate=lambda request: Principal(subject="u", scopes={"mcp:tools"}),
        scopes_supported=["mcp:tools"],
    )
    MCPAuth(
        verify=server.verifier(resource="https://api.example.com/mcp"),
        resource_server_url="https://api.example.com/mcp",
        authorization_servers=["https://api.example.com"],
    )
    assert [w for w in recwarn if "audience binding" in str(w.message)] == []


# ── version negotiation is described correctly ───────────────────────


def test_a_modern_request_with_a_bad_version_is_rejected():
    app = _tool_app()
    app.mount_mcp(transport="http", path="/mcp")
    client = TestClient(app)
    client.post("/mcp", json=INITIALIZE, headers={"Accept": "application/json"})
    body = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}},
        },
        headers={"Accept": "application/json"},
    ).json()
    assert body["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION


def test_a_handshake_with_a_bad_version_negotiates_instead():
    """The half the page stated unqualified: this does *not* reject."""
    app = _tool_app()
    app.mount_mcp(transport="http", path="/mcp")
    request = dict(INITIALIZE)
    request["params"] = {**INITIALIZE["params"], "protocolVersion": "1900-01-01"}
    body = TestClient(app).post("/mcp", json=request, headers={"Accept": "application/json"}).json()
    assert "error" not in body
    assert body["result"]["protocolVersion"] in ("2026-07-28", "2025-11-25", "2025-06-18")


def test_the_page_distinguishes_the_two_paths():
    text = GUIDE.read_text(encoding="utf-8")
    assert "negotiates rather than rejects" in text


def test_the_documented_version_list_matches_the_server():
    text = GUIDE.read_text(encoding="utf-8")
    documented = re.search(r'"supportedVersions": \[([^\]]+)\]', text)
    assert documented is not None
    listed = tuple(v.strip().strip('"') for v in documented.group(1).split(","))
    assert listed == SERVED_PROTOCOL_VERSIONS


# ── every block on the page stays runnable ───────────────────────────


def test_the_fence_scanner_finds_the_guide_blocks():
    """Every check below is a loop over `blocks(GUIDE)`, so an empty scan is silence."""
    found = blocks(GUIDE)
    assert len(found) > 10, f"the guide's fence scanner returned {len(found)} blocks"
    assert any(lang == "python" for _line, lang, _code in found)
    assert any(lang == "json" for _line, lang, _code in found)


# Compiling this page's python blocks lives in `test_docs_examples_parse.py`,
# which walks all of `docs/**` and therefore already covers every block here -
# verified: both compile the same 62 blocks from this page. The copy carried the
# same test name as that module's, so `pytest -k` selected two tests of which
# one was a strict subset of the other.


def test_every_json_block_is_json():
    """One was comment-annotated pseudo-JSON tagged `json`."""
    checked = 0
    for line_no, lang, code in blocks(GUIDE):
        if lang == "json":
            json.loads(code)
            checked += 1
    assert checked, "no json block was parsed"


def test_no_python_block_leaves_a_name_undefined():
    """The docs rule: a guide block must be runnable as-is.

    Every runnable block is executed. The version this replaces called
    `pytest.skip` from inside the loop, which ends the *test* - so one block
    needing an uninstalled optional dependency silently abandoned every block
    after it. A missing dependency is now recorded and the loop continues.
    """
    blocking = ("app.run(", "serve_stdio", "uvicorn.run", "while True", "asyncio.run(")
    checked = 0
    skipped: list[str] = []
    for line_no, lang, code in blocks(GUIDE):
        if lang != "python" or any(b in code for b in blocking):
            continue
        namespace = {n: getattr(veloce, n) for n in veloce.__all__}
        namespace["app"] = Veloce(title="Guide", version="1.0.0", openapi_url=None)
        namespace["__name__"] = "__main__"
        try:
            exec(compile(code, f"mcp.md:{line_no}", "exec"), namespace)
        except ModuleNotFoundError as exc:  # an optional dependency is not installed
            skipped.append(f"mcp.md:{line_no} needs {exc.name}")
            continue
        except NameError as exc:
            pytest.fail(f"mcp.md:{line_no} leaves a name undefined: {exc}")
        checked += 1
    if not checked:
        pytest.skip(f"every runnable block needs a missing dependency: {skipped}")


def test_every_named_config_key_exists():
    defaults = Config.default_config()
    for key in set(re.findall(r"\b(MCP_[A-Z_]+)\b", GUIDE.read_text(encoding="utf-8"))):
        assert key in defaults, key


def test_every_named_context_method_exists():
    for name in set(re.findall(r"`ctx\.([a-z_]+)", GUIDE.read_text(encoding="utf-8"))):
        assert hasattr(MCPContext, name), name


def test_every_internal_anchor_link_resolves():
    """A broken in-page anchor renders as a dead link and fails a strict build.

    Caught one introduced by this review's own edits: `#authenticating-an-agent`,
    where the heading is "Authentication and authorization". `mkdocs --strict`
    reports it, but the strict build cannot run on every machine (the social-card
    plugin needs libcairo), so the check belongs here too.
    """

    text = GUIDE.read_text(encoding="utf-8")
    anchors = {
        re.sub(r"[^a-z0-9\s-]", "", heading.strip().lower()).replace(" ", "-")
        for heading in re.findall(r"^#{1,6}\s+(.+)$", text, re.MULTILINE)
    }
    for link in set(re.findall(r"\]\(#([a-z0-9-]+)\)", text)):
        assert link in anchors, f"#{link} matches no heading in mcp.md"
