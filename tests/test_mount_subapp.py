"""app.mount() — mounting a sub-application under a path prefix."""

from __future__ import annotations

import pytest

from tests.conftest import make_request
from veloce import Request, Response, Veloce
from veloce.contrib.staticfiles import StaticFiles
from veloce.testclient import TestClient


async def test_mount():
    main = Veloce(openapi_url=None)
    sub = Veloce(openapi_url=None)

    @sub.get("/items")
    async def items(request: Request):
        return [{"id": 1}]

    main.mount("/api", sub)

    resp = await main.handle_request(make_request(path="/api/items"))
    assert resp.status_code == 200
    import orjson

    assert orjson.loads(resp.body) == [{"id": 1}]


class TestMountedRequestCarriesTheConnection:
    """A sub-app answers the same connection, so it must see the same one.

    The sub-request was built with a scope holding only `root_path`, so every
    scope-derived property read its default inside a mount: `request.is_secure`
    was `False` over TLS - inverting secure-cookie and HTTPS-guard decisions -
    and `request.client_host` was `None`, silently defeating IP allowlisting,
    rate limiting and audit logging.
    """

    @staticmethod
    def _probe_app(name: str) -> Veloce:
        app = Veloce(openapi_url=None)

        @app.get("/p")
        async def probe(request: Request):
            return {
                "where": name,
                "client": request.client_host,
                "scheme": request.scheme,
                "is_secure": request.is_secure,
                "url": str(request.url),
                "root_path": request.root_path,
                "path": request.path,
            }

        return app

    async def _call(self, app: Veloce, path: str) -> dict:
        import orjson

        scope = {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [(b"host", b"example.com")],
            "root_path": "",
            "scheme": "https",
            "http_version": "1.1",
            "client": ("203.0.113.9", 1234),
            "server": ("example.com", 443),
        }
        chunks: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            chunks.append(message)

        await app(scope, receive, send)
        body = b"".join(m.get("body", b"") for m in chunks[1:])
        return orjson.loads(body)

    async def test_the_connection_properties_match_the_top_level_route(self):
        app = self._probe_app("top")
        app.mount("/sub", self._probe_app("mount"))

        top = await self._call(app, "/p")
        mounted = await self._call(app, "/sub/p")

        assert mounted["client"] == top["client"] == "203.0.113.9"
        assert mounted["scheme"] == top["scheme"] == "https"
        assert mounted["is_secure"] is top["is_secure"] is True
        assert mounted["url"] == top["url"]

    async def test_the_mount_still_rewrites_what_it_owns(self):
        """Carrying the scope forward must not carry the parent's path with it."""
        app = self._probe_app("top")
        app.mount("/sub", self._probe_app("mount"))

        mounted = await self._call(app, "/sub/p")
        assert mounted["where"] == "mount"
        assert mounted["root_path"] == "/sub"
        assert mounted["path"] == "/p"

    async def test_a_sub_app_is_checked_against_its_own_body_limit(self):
        """The derived request must not inherit the parent's enforcement flag."""
        main = Veloce(openapi_url=None)
        sub = Veloce(openapi_url=None)
        sub.config["MAX_CONTENT_LENGTH"] = 8

        @sub.post("/echo")
        async def echo(request: Request):
            return {"n": len(await request.body())}

        main.mount("/api", sub)

        resp = await main.handle_request(
            make_request(path="/api/echo", method="POST", body=b"x" * 64)
        )
        assert resp.status_code == 413


# ── mount() asks for the protocol, not the class ─────────────────────
#
# Moved here from `test_extensibility_gaps.py`, a module named for a review
# batch rather than a subject.


class MemoryAssets:
    """Exactly what the dispatcher needs: a prefix and an async handle."""

    def __init__(self, files: dict[str, str]) -> None:
        self.prefix = ""
        self.files = files

    async def handle(self, request):
        name = request.path[len(self.prefix) :].lstrip("/")
        if name in self.files:
            return Response(body=self.files[name].encode(), content_type="text/plain")
        return None


def test_a_handler_implementing_the_protocol_can_be_mounted():
    """The defect: rejected for not being a `StaticFiles`."""
    app = Veloce(openapi_url=None)
    app.mount("/assets", MemoryAssets({"a.txt": "hello"}))
    client = TestClient(app)
    assert client.get("/assets/a.txt").text == "hello"


def test_a_mounted_handler_that_returns_none_falls_through():
    app = Veloce(openapi_url=None)
    app.mount("/assets", MemoryAssets({}))

    @app.get("/assets/live")
    async def live():
        return {"route": True}

    client = TestClient(app)
    assert client.get("/assets/live").json() == {"route": True}
    assert client.get("/assets/missing").status_code == 404


def test_the_mount_prefix_is_written_onto_the_handler():
    handler = MemoryAssets({})
    Veloce(openapi_url=None).mount("/assets/", handler)
    assert handler.prefix == "/assets"


def test_a_real_staticfiles_still_mounts(tmp_path):
    (tmp_path / "a.txt").write_text("disk", encoding="utf-8")
    app = Veloce(openapi_url=None)
    app.mount("/s", StaticFiles(directory=str(tmp_path)))
    assert TestClient(app).get("/s/a.txt").text == "disk"


def test_something_that_is_neither_is_still_rejected():
    with pytest.raises(TypeError):
        Veloce(openapi_url=None).mount("/x", object())
