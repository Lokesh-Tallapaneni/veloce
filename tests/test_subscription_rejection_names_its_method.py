"""A sessionless subscribe / unsubscribe rejection names the method it rejected.

`_require_session` builds the message from a `method` parameter that **neither
caller supplied**, so both took its default - `"resources/subscribe"`. A client
that called `resources/unsubscribe` on a stateless transport was told
`resources/subscribe requires a stateful connection`, naming a method it had not
called.

The parameter has no default now, so a third caller cannot silently inherit the
wrong one either.
"""

from __future__ import annotations

import inspect

import pytest

from tests._mcp import call_error
from veloce import Veloce
from veloce.contrib.mcp import MCPServer
from veloce.contrib.mcp.subscriptions import SubscriptionsCapability


def _server() -> MCPServer:
    app = Veloce(title="S", version="1.0.0", openapi_url=None)

    @app.get(
        "/doc",
        expose_as_mcp_resource=True,
        mcp_resource_uri="res://doc",
        mcp_description="A document",
    )
    async def doc() -> dict:
        return {"v": 1}

    app.config["MCP_RESOURCE_SUBSCRIPTIONS"] = True
    return MCPServer(app)


@pytest.mark.parametrize(
    "method", ["resources/subscribe", "resources/unsubscribe"], ids=["subscribe", "unsubscribe"]
)
async def test_the_rejection_names_the_method_that_was_called(method):
    error = await call_error(_server(), method, {"uri": "res://doc"})
    assert method in error["message"], error["message"]


async def test_unsubscribe_is_not_told_it_called_subscribe():
    """The defect, stated as the thing that was wrong."""
    error = await call_error(_server(), "resources/unsubscribe", {"uri": "res://doc"})
    assert "resources/subscribe" not in error["message"], error["message"]


async def test_the_rejection_still_explains_itself():
    """The negative: naming the method must not lose the reason."""
    error = await call_error(_server(), "resources/unsubscribe", {"uri": "res://doc"})
    assert "stateful connection" in error["message"]


def test_the_parameter_has_no_default():
    """What let both callers take the wrong value: a default nobody overrode."""
    parameter = inspect.signature(SubscriptionsCapability._require_session).parameters["method"]
    assert parameter.default is inspect.Parameter.empty
