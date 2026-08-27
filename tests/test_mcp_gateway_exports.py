"""Every name the MCP gateway publishes is importable from it, and is the real one.

`veloce.contrib.mcp.__all__` is the package's published surface, and eight of its
names occurred nowhere in the suite - so a leaf rename, a dropped re-export or a
gateway typo would have shipped. Enumerating `__all__` rather than listing names
by hand means the check covers the next addition too.
"""

from __future__ import annotations

import importlib

import pytest

from veloce.contrib import mcp

ALL = sorted(mcp.__all__)

#: The published surface, frozen. The enumeration tests below catch a name that
#: no longer resolves; only a snapshot catches one silently dropped, which is
#: the direction that breaks a user's import. Update deliberately, with a
#: CHANGELOG entry - this is a public gateway.
MCP_ALL = {
    "AccessToken",
    "ArgTransform",
    "AudioContent",
    "AuthorizationCode",
    "AuthorizationError",
    "AuthorizationStore",
    "BidirectionalTransport",
    "Capability",
    "CompletionResult",
    "CompletionsCapability",
    "ContentBlock",
    "EmbeddedResource",
    "HeaderMismatchError",
    "Icon",
    "ImageContent",
    "InMemoryAuthorizationStore",
    "InternalError",
    "InvalidParamsError",
    "InvalidRequestError",
    "JSON_SCHEMA_DIALECT",
    "LoggingCapability",
    "MCPAuth",
    "MCPAuthorizationServer",
    "MCPCapabilityError",
    "MCPContext",
    "MCPError",
    "MCPPrompt",
    "MCPRequestError",
    "MCPResource",
    "MCPServer",
    "MCPSession",
    "MCPTask",
    "MCPTool",
    "MethodHandler",
    "MethodNotFoundError",
    "OAuthClient",
    "OriginNotAllowedError",
    "PromptRegistry",
    "PromptsCapability",
    "ProtocolVersionError",
    "ResourceLink",
    "ResourceNotFoundError",
    "ResourceRegistry",
    "ResourcesCapability",
    "SampledToolCall",
    "SamplingRun",
    "SessionBackend",
    "SessionNotFoundError",
    "SessionRecord",
    "SessionRequiredError",
    "StdioTransport",
    "SubscriptionsCapability",
    "TaskRegistry",
    "TasksCapability",
    "TextContent",
    "ToolFilter",
    "ToolRegistry",
    "ToolsCapability",
    "Transport",
    "add_mcp_proxy",
    "build_prompt_registry",
    "build_registry",
    "build_resource_registry",
    "derive_tool",
    "register_authorization_server",
    "register_http_transport",
    "register_sse_transport",
    "serve_stdio",
}


def test_the_published_surface_is_unchanged() -> None:
    published = set(mcp.__all__)
    assert published == MCP_ALL, (
        "veloce.contrib.mcp.__all__ changed. Added: "
        f"{sorted(published - MCP_ALL)}; removed: {sorted(MCP_ALL - published)}. "
        "Removing a name breaks an import that worked - update this snapshot "
        "and CHANGELOG.md together."
    )


def test_the_gateway_publishes_something() -> None:
    """A guard against the enumeration silently going empty."""
    assert len(ALL) > 50


@pytest.mark.parametrize("name", ALL)
def test_the_name_resolves_on_the_gateway(name: str) -> None:
    assert hasattr(mcp, name), f"{name} is in __all__ but not bound on the gateway"


@pytest.mark.parametrize("name", ALL)
def test_the_gateway_object_is_the_leaf_object(name: str) -> None:
    """A re-export must be the same object, not a same-named copy."""
    obj = getattr(mcp, name)
    module_name = getattr(obj, "__module__", None)
    if module_name is None or not module_name.startswith("veloce.contrib.mcp"):
        return  # a constant or a typing alias, which carries no home module
    leaf = importlib.import_module(module_name)
    assert getattr(leaf, obj.__name__, None) is obj


def test_every_shipped_capability_is_on_one_gateway() -> None:
    """Neither gateway used to show the whole set; the parent now does."""
    from veloce.contrib.mcp import capabilities

    for name in capabilities.__all__:
        assert name in mcp.__all__, (
            f"{name} is published by veloce.contrib.mcp.capabilities but not by "
            "veloce.contrib.mcp, so a reader of either sees a partial set"
        )


def test_import_star_binds_exactly_all() -> None:
    namespace: dict[str, object] = {}
    exec("from veloce.contrib.mcp import *", namespace)  # noqa: S102
    bound = {k for k in namespace if not k.startswith("__")}
    assert bound == set(ALL)


def test_every_published_name_appears_in_the_docs() -> None:
    """A published name with no reference entry is one a reader cannot find.

    Six of them appeared nowhere in `docs/` - the only undocumented exports in
    the package - which is how `Capability` came to be exported as a
    subclassable base with nothing telling a reader what to subclass.
    """
    import pathlib

    docs_root = pathlib.Path(__file__).resolve().parents[1] / "docs"
    if not docs_root.is_dir():
        pytest.skip("docs/ is not present in this checkout")
    text = " ".join(p.read_text(encoding="utf-8") for p in docs_root.rglob("*.md"))
    missing = [name for name in ALL if name not in text]
    assert not missing, f"published by veloce.contrib.mcp but absent from docs/: {missing}"
