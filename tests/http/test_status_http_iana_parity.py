"""The HTTP status codes added for full IANA parity.

These three were the gap between `veloce.status` and the IANA registry. They
lived in `test_status_websocket_codes.py`, a module named for RFC 6455 close
codes, which is not where anyone looking for an HTTP code would find them.
"""

from __future__ import annotations

import pytest

from veloce import status

# Literal on purpose: these are the registered values, and reading them back
# off the constants they check would make the table agree with itself.
IANA_PARITY_CODES = {
    "HTTP_208_ALREADY_REPORTED": 208,
    "HTTP_226_IM_USED": 226,
    "HTTP_421_MISDIRECTED_REQUEST": 421,
}


@pytest.mark.parametrize(("name", "code"), IANA_PARITY_CODES.items(), ids=IANA_PARITY_CODES)
def test_an_iana_parity_code_has_its_registered_value(name: str, code: int):
    assert getattr(status, name) == code


def test_the_name_states_the_status_code_it_carries():
    for name, code in IANA_PARITY_CODES.items():
        assert name.split("_")[1] == str(code), name
