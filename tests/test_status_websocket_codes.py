"""WebSocket close-code constants in veloce.status — RFC 6455 §7.4.1.

One table, parametrized, asserting the literal numbers. Five one-line tests
covering five of the fifteen codes sat above a table covering all fifteen; the
table subsumed every one of them, and looping inside it meant the first
mismatch stopped the check and the report said only that the table failed.

An HTTP-status test lived here too - three codes that have nothing to do with
WebSockets - and is in `test_status_http_iana_parity.py` with the other
HTTP-status work.
"""

from __future__ import annotations

import pytest

from veloce import status

# RFC 6455 Sec. 7.4.1, in full. The numbers are literal on purpose: they are the
# wire contract, and reading them back off the constants they check would make
# the table agree with itself.
RFC6455_CODES = {
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


@pytest.mark.parametrize(("name", "code"), RFC6455_CODES.items(), ids=RFC6455_CODES)
def test_a_close_code_has_the_value_the_rfc_gives_it(name: str, code: int):
    assert getattr(status, name) == code


@pytest.mark.parametrize("name", RFC6455_CODES)
def test_a_close_code_is_a_plain_int(name: str):
    """Usable directly wherever a close code is expected, without conversion."""
    value = getattr(status, name)
    assert isinstance(value, int)
    assert value + 0 == RFC6455_CODES[name]


def test_the_table_covers_every_shipped_close_code():
    """A code added to `status` without a row here would go unchecked."""
    shipped = {name for name in dir(status) if name.startswith("WS_")}
    assert shipped == set(RFC6455_CODES)


def test_the_name_states_the_close_code_it_carries():
    """`WS_1008_POLICY_VIOLATION` promising 1008 is the point of the spelling."""
    for name, code in RFC6455_CODES.items():
        assert name.split("_")[1] == str(code), name
