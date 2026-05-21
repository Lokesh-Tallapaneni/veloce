"""WebSocket close-code constants in veloce.status — RFC 6455 §7.4.1."""

from __future__ import annotations

from veloce import status


def test_normal_closure():
    assert status.WS_1000_NORMAL_CLOSURE == 1000


def test_going_away():
    assert status.WS_1001_GOING_AWAY == 1001


def test_policy_violation():
    assert status.WS_1008_POLICY_VIOLATION == 1008


def test_internal_error():
    assert status.WS_1011_INTERNAL_ERROR == 1011


def test_tls_handshake():
    assert status.WS_1015_TLS_HANDSHAKE == 1015


def test_full_rfc6455_set_present():
    expected = {
        "WS_1000_NORMAL_CLOSURE": 1000,
        "WS_1001_GOING_AWAY": 1001,
        "WS_1002_PROTOCOL_ERROR": 1002,
        "WS_1003_UNSUPPORTED_DATA": 1003,
        "WS_1005_NO_STATUS_RCVD": 1005,
        "WS_1006_ABNORMAL_CLOSURE": 1006,
        "WS_1007_INVALID_FRAME_PAYLOAD_DATA": 1007,
        "WS_1008_POLICY_VIOLATION": 1008,
        "WS_1009_MESSAGE_TOO_BIG": 1009,
        "WS_1010_MANDATORY_EXT": 1010,
        "WS_1011_INTERNAL_ERROR": 1011,
        "WS_1012_SERVICE_RESTART": 1012,
        "WS_1013_TRY_AGAIN_LATER": 1013,
        "WS_1014_BAD_GATEWAY": 1014,
        "WS_1015_TLS_HANDSHAKE": 1015,
    }
    for name, value in expected.items():
        assert getattr(status, name) == value


def test_codes_usable_as_close_code():
    # The constants are plain ints — usable directly where a close code
    # is expected.
    assert isinstance(status.WS_1000_NORMAL_CLOSURE, int)
    assert status.WS_1001_GOING_AWAY + 0 == 1001


def test_http_status_iana_parity_codes():
    """The three standard HTTP codes added for full IANA parity."""
    assert status.HTTP_208_ALREADY_REPORTED == 208
    assert status.HTTP_226_IM_USED == 226
    assert status.HTTP_421_MISDIRECTED_REQUEST == 421
