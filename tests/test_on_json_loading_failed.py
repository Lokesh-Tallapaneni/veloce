"""Request.on_json_loading_failed hook (Q10)."""

from __future__ import annotations

import pytest

from veloce import Request


def _json_req(body: bytes) -> Request:
    return Request(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/json"},
        body=body,
    )


def test_default_hook_reraises_decode_error():
    req = _json_req(b"{not valid json")
    with pytest.raises(Exception):
        req.get_json()


def test_silent_skips_the_hook():
    req = _json_req(b"{still bad")
    # silent=True returns None without invoking the hook.
    assert req.get_json(silent=True) is None


def test_subclass_can_override_hook():
    class TolerantRequest(Request):
        def on_json_loading_failed(self, error):
            return {"fallback": True}

    req = TolerantRequest(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/json"},
        body=b"garbage{",
    )
    assert req.get_json() == {"fallback": True}


def test_hook_receives_the_error():
    captured: list = []

    class CapturingRequest(Request):
        def on_json_loading_failed(self, error):
            captured.append(error)
            return None

    req = CapturingRequest(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/json"},
        body=b"]]bad",
    )
    req.get_json()
    assert len(captured) == 1
    assert isinstance(captured[0], Exception)


def test_valid_json_does_not_invoke_hook():
    invoked: list = []

    class HookRequest(Request):
        def on_json_loading_failed(self, error):
            invoked.append(error)
            return None

    req = HookRequest(
        method="POST",
        path="/",
        query_string="",
        headers={"content-type": "application/json"},
        body=b'{"ok": true}',
    )
    assert req.get_json() == {"ok": True}
    assert invoked == []
