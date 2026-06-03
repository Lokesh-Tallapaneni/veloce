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


# ── Production masking + verbose opt-in (info-disclosure hardening) ───


def _app_stub(**config):
    class _A:
        pass

    a = _A()
    a.config = config
    return a


def test_malformed_json_masked_by_default():
    from veloce.exceptions import BadRequest

    req = _json_req(b"{not valid json")
    with pytest.raises(BadRequest) as exc:
        req.get_json()
    assert exc.value.detail == "Invalid JSON body"
    # No decoder position internals leak.
    for leak in ("line", "column", "char"):
        assert leak not in exc.value.detail.lower()


def test_debug_detail_attached_regardless_of_verbose():
    from veloce.exceptions import BadRequest

    req = _json_req(b"{bad")
    with pytest.raises(BadRequest) as exc:
        req.get_json()
    assert exc.value.debug_detail  # underlying decoder message preserved for ops


def test_verbose_flag_surfaces_reason():
    from veloce.exceptions import BadRequest

    req = _json_req(b"{bad")
    req.app = _app_stub(JSON_ERRORS_VERBOSE=True)
    with pytest.raises(BadRequest) as exc:
        req.get_json()
    assert exc.value.detail.startswith("Invalid JSON body:")


def test_debug_config_also_surfaces_reason():
    from veloce.exceptions import BadRequest

    # A real Config always carries JSON_ERRORS_VERBOSE (seeded False), so DEBUG
    # must still surface the reason via an explicit OR - not a dict-default
    # fallback that the present-but-False key would kill.
    req = _json_req(b"{bad")
    req.app = _app_stub(DEBUG=True, JSON_ERRORS_VERBOSE=False)
    with pytest.raises(BadRequest) as exc:
        req.get_json()
    assert exc.value.detail.startswith("Invalid JSON body:")


def test_debug_app_constructor_surfaces_reason_end_to_end():
    from veloce import Veloce
    from veloce.exceptions import BadRequest

    # Through a real Veloce(debug=True) app (real Config) - guards the dead
    # DEBUG-fallback regression a bare-dict stub could hide.
    app = Veloce(debug=True, openapi_url=None)
    req = _json_req(b"{bad")
    req.app = app
    with pytest.raises(BadRequest) as exc:
        req.get_json()
    assert exc.value.detail.startswith("Invalid JSON body:")


def test_async_and_sync_paths_agree_on_masked_detail():
    import asyncio

    from veloce.exceptions import BadRequest

    sync_req = _json_req(b"{bad")
    with pytest.raises(BadRequest) as sync_exc:
        sync_req.get_json()

    async_req = _json_req(b"{bad")
    with pytest.raises(BadRequest) as async_exc:
        asyncio.new_event_loop().run_until_complete(async_req.json())

    assert sync_exc.value.detail == async_exc.value.detail == "Invalid JSON body"


def test_string_false_flags_do_not_surface_reason():
    from veloce.exceptions import BadRequest

    # Dotenv string "false" for both flags must stay masked, not treated truthy.
    req = _json_req(b"{bad")
    req.app = _app_stub(JSON_ERRORS_VERBOSE="false", DEBUG="false")
    with pytest.raises(BadRequest) as exc:
        req.get_json()
    assert exc.value.detail == "Invalid JSON body"
