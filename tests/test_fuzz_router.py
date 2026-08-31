"""Property-based fuzz tests for the radix router — `routing/router.py`.

Registers many randomised static paths and asserts every one matches straight
back, then throws arbitrary junk paths at a router with parameter and `path:`
routes, asserting matching returns a route-or-None and never raises.
"""

from __future__ import annotations

import string

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from veloce import Veloce

pytestmark = pytest.mark.fuzz

_SEGMENT_CHARS = string.ascii_lowercase + string.digits + "-_"
_segment = st.text(alphabet=_SEGMENT_CHARS, min_size=1, max_size=8)
_static_path = st.lists(_segment, min_size=1, max_size=5).map(lambda segs: "/" + "/".join(segs))


@settings(deadline=None)
@given(paths=st.lists(_static_path, max_size=40, unique=True))
def test_registered_static_paths_always_match(paths: list[str]) -> None:
    """Every registered static path matches straight back to its route."""
    app = Veloce(openapi_url=None)

    async def handler():
        return {}

    for path in paths:
        app.add_route(path, handler, ["GET"])
    for path in paths:
        assert app.match("GET", path) is not None, f"registered path lost: {path}"


# Junk paths may carry separators, dots, and percent-escapes — the characters a
# traversal / matcher edge case would exploit.
_junk_segment = st.text(alphabet=_SEGMENT_CHARS + "/.%", max_size=6)
_junk_path = st.lists(_junk_segment, max_size=6).map(lambda segs: "/" + "/".join(segs))


@settings(deadline=None)
@given(path=_junk_path)
def test_arbitrary_paths_match_or_none_never_raise(path: str) -> None:
    """Matching arbitrary junk returns a route or `None` — never raises."""
    app = Veloce(openapi_url=None)

    async def h():
        return {}

    app.add_route("/users/{uid}/items/{iid}", h, ["GET"])
    app.add_route("/static/{path:path}", h, ["GET"])
    app.match("GET", path)  # must not raise
