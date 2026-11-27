"""An overlapping mount prefix is refused, whichever door registered it.

`mount()` rejects a prefix that is a path-segment ancestor of, or equal to, one
already mounted — mounts are matched in registration order, so an overlap means
one silently shadows the other. The check scanned two of the three registries:
`_mounted_apps` and `_asgi_mounts`, but not `_static_handlers`. And
`mount_static()` appended to that third registry directly, so it neither
consulted the check nor was visible to it.

The result was worse than order-dependent. Dispatch tries `_mounted_apps` before
`_static_handlers`, so a Veloce mount always won:

    app.mount_static("/assets", d)   # registered first
    app.mount("/assets", sub)        # accepted, no error
    GET /assets/f.txt                # -> the sub-app, always

and the reverse order gave the same answer. Every asset under a static mount that
shares a prefix with an app mount was unreachable, with nothing said about it at
wiring time or at request time.

The check now covers all three registries and both entry points. It is a
registration-time scan over a handful of mounts, so it costs nothing per request.
"""

from __future__ import annotations

import pytest

from veloce import StaticFiles, Veloce
from veloce.testclient import TestClient


@pytest.fixture
def assets(tmp_path):
    (tmp_path / "f.txt").write_text("STATIC")
    return str(tmp_path)


def _sub() -> Veloce:
    sub = Veloce(openapi_url=None)

    @sub.get("/f.txt")
    async def f():
        return {"from": "mount"}

    return sub


async def _asgi(scope, receive, send):  # pragma: no cover - never dispatched
    raise AssertionError


# ── static against a sub-app mount, both orders ──────────────────────


def test_a_mount_over_an_existing_static_prefix_is_refused(assets):
    """The defect: accepted, then the static mount served nothing."""
    app = Veloce(openapi_url=None)
    app.mount_static("/assets", assets, must_exist=False)
    with pytest.raises(ValueError, match="overlap"):
        app.mount("/assets", _sub())


def test_a_static_mount_over_an_existing_mount_prefix_is_refused(assets):
    app = Veloce(openapi_url=None)
    app.mount("/assets", _sub())
    with pytest.raises(ValueError, match="overlap"):
        app.mount_static("/assets", assets, must_exist=False)


def test_a_static_mount_under_an_existing_mount_prefix_is_refused(assets):
    """A descendant prefix shadows just as completely as an equal one."""
    app = Veloce(openapi_url=None)
    app.mount("/assets", _sub())
    with pytest.raises(ValueError, match="overlap"):
        app.mount_static("/assets/img", assets, must_exist=False)


def test_a_mount_under_an_existing_static_prefix_is_refused(assets):
    app = Veloce(openapi_url=None)
    app.mount_static("/assets", assets, must_exist=False)
    with pytest.raises(ValueError, match="overlap"):
        app.mount("/assets/api", _sub())


def test_two_static_mounts_on_the_same_prefix_are_refused(assets):
    """The third registry against itself, which nothing checked at all."""
    app = Veloce(openapi_url=None)
    app.mount_static("/assets", assets, must_exist=False)
    with pytest.raises(ValueError, match="overlap"):
        app.mount_static("/assets", assets, must_exist=False)


def test_a_static_mount_over_an_asgi_mount_is_refused(assets):
    app = Veloce(openapi_url=None)
    app.mount("/ext", _asgi)
    with pytest.raises(ValueError, match="overlap"):
        app.mount_static("/ext", assets, must_exist=False)


def test_a_static_handler_mounted_through_mount_is_also_visible(assets):
    """`mount()` routes a `.prefix`/`.handle` object into the static registry;
    it must be visible to the next check, not just appended."""

    app = Veloce(openapi_url=None)
    app.mount("/assets", StaticFiles(directory=assets, must_exist=False))
    with pytest.raises(ValueError, match="overlap"):
        app.mount("/assets", _sub())


# ── the error names both sides ───────────────────────────────────────


def test_the_error_names_the_conflicting_prefix(assets):
    app = Veloce(openapi_url=None)
    app.mount_static("/assets", assets, must_exist=False)
    with pytest.raises(ValueError) as err:
        app.mount("/assets", _sub())
    assert "/assets" in str(err.value)


# ── non-overlapping prefixes are still accepted ──────────────────────
#
# The negatives, and the half that matters most: an overlap check that refused
# ordinary configurations would break more apps than the shadowing ever did.


def test_two_disjoint_static_mounts_are_accepted(assets):
    app = Veloce(openapi_url=None)
    app.mount_static("/assets", assets, must_exist=False)
    app.mount_static("/media", assets, must_exist=False)
    assert len(app._static_handlers) == 2


def test_a_static_mount_beside_a_sub_app_mount_is_accepted(assets):
    app = Veloce(openapi_url=None)
    app.mount("/api", _sub())
    app.mount_static("/assets", assets, must_exist=False)
    assert len(app._static_handlers) == 1
    assert len(app._mounted_apps) == 1


def test_a_shared_name_prefix_that_is_not_a_path_ancestor_is_accepted(assets):
    """`/assets` and `/assets-v2` share a string prefix but not a path segment."""
    app = Veloce(openapi_url=None)
    app.mount_static("/assets", assets, must_exist=False)
    app.mount_static("/assets-v2", assets, must_exist=False)
    assert len(app._static_handlers) == 2


def test_the_default_static_prefix_still_works(assets):
    app = Veloce(openapi_url=None)
    app.mount_static("/static", assets, must_exist=False)
    assert app._static_handlers[0].prefix == "/static"


def test_a_static_mount_still_serves_its_files(assets):
    """The check must not damage what it guards."""

    app = Veloce(openapi_url=None)
    app.mount_static("/assets", assets, must_exist=False)
    assert TestClient(app).get("/assets/f.txt").body == b"STATIC"


def test_a_sub_app_mount_still_serves_its_routes(assets):

    app = Veloce(openapi_url=None)
    app.mount("/api", _sub())
    app.mount_static("/assets", assets, must_exist=False)
    assert TestClient(app).get("/api/f.txt").json() == {"from": "mount"}
    assert TestClient(app).get("/assets/f.txt").body == b"STATIC"


def test_a_prefix_without_a_leading_slash_is_normalised_before_comparison(assets):
    """`assets` and `/assets` are the same mount; the check must see that."""
    app = Veloce(openapi_url=None)
    app.mount_static("/assets", assets, must_exist=False)
    with pytest.raises(ValueError, match="overlap"):
        app.mount("assets", _sub())
