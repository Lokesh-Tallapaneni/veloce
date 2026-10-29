"""`advertised_length` accepts the body-permitted decision its callers already made.

Three send paths compute `status_permits_body(...)` for a response and then
call `advertised_length(...)`, which computed it again - two calls per
response for one answer. The function now takes the flag, and these pin that
passing it can never disagree with computing it.
"""

from __future__ import annotations

import pytest

from veloce.http.response import advertised_length
from veloce.status import status_permits_body

#: One per class the function branches on: body-permitting, 304, bodiless.
STATUSES = [100, 101, 200, 201, 204, 205, 206, 301, 304, 400, 404, 500, 503]
BODIES = [b"", b"x", b"x" * 4096]


@pytest.mark.parametrize("status_code", STATUSES)
@pytest.mark.parametrize("body", BODIES)
def test_passing_the_flag_matches_computing_it(status_code: int, body: bytes):
    computed = advertised_length(status_code, body)
    passed = advertised_length(status_code, body, status_permits_body(status_code))

    assert passed == computed, (
        f"status {status_code} with {len(body)} bytes: passing the flag gave "
        f"{passed!r}, computing it gave {computed!r}"
    )


@pytest.mark.parametrize("status_code", STATUSES)
def test_omitting_the_flag_still_decides_for_itself(status_code: int):
    """The parameter is an optimisation, not a required argument."""
    assert advertised_length(status_code, b"abc") == advertised_length(status_code, b"abc", None)


def test_a_body_permitting_status_advertises_what_it_carries():
    assert advertised_length(200, b"hello", True) == 5


def test_a_304_may_advertise_the_length_a_200_would_have():
    # RFC 9110 Sec. 8.6 / 15.4.5; an unknown length is omitted, not zeroed.
    assert advertised_length(304, b"hello", False) == 5
    assert advertised_length(304, b"", False) is None


def test_every_other_bodiless_status_has_length_zero():
    assert advertised_length(204, b"", False) == 0
    assert advertised_length(100, b"", False) == 0
    assert advertised_length(205, b"", False) == 0
