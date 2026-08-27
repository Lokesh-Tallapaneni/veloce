"""make_response() helper — body/status/headers coercion."""

from __future__ import annotations

import orjson

from veloce import make_response


class TestMakeResponse:
    def test_make_response_string(self):
        resp = make_response("Hello", 201)
        assert resp.status_code == 201
        assert resp.body == b"Hello"

    def test_make_response_dict(self):
        resp = make_response({"ok": True}, 200)
        assert resp.status_code == 200
        assert orjson.loads(resp.body)["ok"] is True

    def test_make_response_bytes(self):
        resp = make_response(b"\x00\x01", 200)
        assert resp.body == b"\x00\x01"
