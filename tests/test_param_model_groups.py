"""`Query(group=True)` and friends spread a model across one request source.

A bare model annotation under a marker already means "one key holding a JSON
document" (see test_openapi_param_models.py). Grouping is therefore opt-in:
`group=True` reads the model's *fields* from that source instead, so a shared
filter/pagination model can be declared once and reused across handlers.
"""

from __future__ import annotations

import urllib.parse

import msgspec
from pydantic import BaseModel, Field

from veloce import Cookie, Header, Query, Veloce
from veloce._handler_plan import build_plan
from veloce.contrib.mcp.plan_bridge import build_input_schema
from veloce.testclient import TestClient


class Filters(BaseModel):
    token: str
    skip: int = 0
    limit: int = 100


class Aliased(BaseModel):
    page_size: int = Field(default=10, alias="pageSize")


class Headers(BaseModel):
    x_token: str
    x_trace: str | None = None


class Prefs(BaseModel):
    theme: str = "light"
    lang: str = "en"


class Tagged(BaseModel):
    tag: list[str] = Field(default_factory=list)


def test_group_reads_each_field_from_the_query_string():
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def items(f: Filters = Query(group=True)):
        return {"token": f.token, "skip": f.skip, "limit": f.limit}

    body = TestClient(app).get("/items?token=abc&skip=5&limit=10").json()
    assert body == {"token": "abc", "skip": 5, "limit": 10}


def test_group_applies_model_defaults_for_absent_fields():
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def items(f: Filters = Query(group=True)):
        return {"skip": f.skip, "limit": f.limit}

    assert TestClient(app).get("/items?token=abc").json() == {"skip": 0, "limit": 100}


def test_group_blames_the_offending_field_not_the_group():
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def items(f: Filters = Query(group=True)):
        return {}

    resp = TestClient(app).get("/items?token=abc&limit=nope")
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["query", "limit"]


def test_group_reports_a_missing_required_field_by_name():
    app = Veloce(openapi_url=None)

    @app.get("/items")
    async def items(f: Filters = Query(group=True)):
        return {}

    resp = TestClient(app).get("/items")
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["query", "token"]


def test_group_honours_field_aliases():
    app = Veloce(openapi_url=None)

    @app.get("/a")
    async def a(f: Aliased = Query(group=True)):
        return {"page_size": f.page_size}

    assert TestClient(app).get("/a?pageSize=42").json() == {"page_size": 42}


def test_header_group_converts_underscores_to_hyphens():
    app = Veloce(openapi_url=None)

    @app.get("/h")
    async def h(hdr: Headers = Header(group=True)):
        return {"token": hdr.x_token, "trace": hdr.x_trace}

    body = TestClient(app).get("/h", headers={"x-token": "t1", "x-trace": "tr"}).json()
    assert body == {"token": "t1", "trace": "tr"}


def test_cookie_group_reads_each_field_from_cookies():
    app = Veloce(openapi_url=None)

    @app.get("/p")
    async def p(prefs: Prefs = Cookie(group=True)):
        return {"theme": prefs.theme, "lang": prefs.lang}

    client = TestClient(app)
    body = client.get("/p", headers={"Cookie": "theme=dark; lang=fr"}).json()
    assert body == {"theme": "dark", "lang": "fr"}


def test_group_collects_repeated_values_into_a_list_field():
    app = Veloce(openapi_url=None)

    @app.get("/t")
    async def t(f: Tagged = Query(group=True)):
        return {"tag": f.tag}

    assert TestClient(app).get("/t?tag=a&tag=b").json() == {"tag": ["a", "b"]}


def test_a_bare_model_marker_still_parses_one_json_string_key():
    """Grouping is opt-in precisely so this existing behaviour is untouched."""
    app = Veloce(openapi_url=None)

    @app.get("/q")
    async def q(f: Filters = Query()):
        return {"token": f.token, "limit": f.limit}

    qs = "f=" + urllib.parse.quote('{"token":"abc","limit":7}')
    assert TestClient(app).get(f"/q?{qs}").json() == {"token": "abc", "limit": 7}


# ── a group model's requiredness reaches both doors ──────────────────
#
# `slot.has_default` was set from `has_default or marker.has_default`, where the
# left operand is already `True` by then: the marker-in-default-position
# spelling fills the signature slot, and the `Annotated` spelling sets it
# explicitly. So every group model read as defaulted. Pydantic groups flatten
# into their fields and escape it; a msgspec group does not.


class _MsgspecFilters(msgspec.Struct):
    q: str


def test_a_required_group_model_is_required_in_the_tool_schema():
    async def handler(f: _MsgspecFilters = Query(group=True)) -> dict:
        return {}

    schema = build_input_schema(build_plan(handler), {})
    assert schema["required"] == ["f"]


def test_an_optional_group_model_is_not_required_in_the_tool_schema():
    """The negative: a declared default still reads as optional."""

    async def handler(f: _MsgspecFilters | None = Query(group=True, default=None)) -> dict:
        return {}

    schema = build_input_schema(build_plan(handler), {})
    assert "required" not in schema or "f" not in schema["required"]


def test_a_required_group_model_is_still_required_over_http():
    """The HTTP door flattens a group, so its field carries the requirement."""
    app = Veloce()

    @app.get("/f")
    async def read(f: _MsgspecFilters = Query(group=True)) -> dict:
        return {"q": f.q}

    with TestClient(app) as client:
        assert client.get("/f").status_code == 422
        assert client.get("/f?q=hi").json() == {"q": "hi"}
