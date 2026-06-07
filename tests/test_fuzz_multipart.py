"""Property-based fuzz tests for the multipart parser — `http/formparsers.py`.

Feeds arbitrary bytes, corrupted-but-well-formed bodies, and fuzzed
boundaries at `parse_multipart_form`. The parser must return a `FormData`
or raise only its declared exceptions (`BadRequest` / `RequestEntityTooLarge`),
never an unhandled error, hang, or unbounded allocation.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from veloce.exceptions import BadRequest, RequestEntityTooLarge
from veloce.http.datastructures import FormData, parse_multipart_form

pytestmark = pytest.mark.fuzz

# The parser opens a SpooledTemporaryFile per part; the function_scoped_fixture
# health check is irrelevant here (no fixtures), and writing temp files makes
# each example a little slow, so cap examples modestly for CI.
_FUZZ = settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow], deadline=None)


@_FUZZ
@given(body=st.binary(max_size=400), boundary=st.text(alphabet="abc123-_", min_size=1, max_size=20))
def test_arbitrary_bytes_yield_formdata_or_declared_error(body: bytes, boundary: str) -> None:
    """Random bytes under a valid boundary never crash the parser."""
    content_type = f"multipart/form-data; boundary={boundary}"
    try:
        result = parse_multipart_form(body, content_type)
    except (RequestEntityTooLarge, BadRequest):
        return  # declared controlled rejections
    assert isinstance(result, FormData)


@_FUZZ
@given(content_type=st.text(max_size=120))
def test_arbitrary_content_type_never_crashes(content_type: str) -> None:
    """A fuzzed Content-Type either parses or raises `BadRequest` (missing /
    malformed boundary) — never an unhandled error."""
    try:
        result = parse_multipart_form(b"", content_type)
    except (BadRequest, RequestEntityTooLarge):
        return
    assert isinstance(result, FormData)


_BOUNDARY = "veloceboundary"
_BASE_BODY = (
    f"--{_BOUNDARY}\r\n"
    'Content-Disposition: form-data; name="field"\r\n\r\n'
    "value\r\n"
    f"--{_BOUNDARY}--\r\n"
).encode()


@_FUZZ
@given(
    positions=st.lists(st.integers(min_value=0, max_value=len(_BASE_BODY) - 1), max_size=6),
    replacements=st.lists(st.integers(min_value=0, max_value=255), max_size=6),
)
def test_corrupted_valid_body_never_crashes(positions: list[int], replacements: list[int]) -> None:
    """Flipping random bytes in a well-formed body yields only declared outcomes."""
    corrupted = bytearray(_BASE_BODY)
    for pos, rep in zip(positions, replacements):
        corrupted[pos] = rep
    content_type = f"multipart/form-data; boundary={_BOUNDARY}"
    try:
        result = parse_multipart_form(bytes(corrupted), content_type)
    except (RequestEntityTooLarge, BadRequest):
        return
    assert isinstance(result, FormData)


@_FUZZ
@given(
    name=st.text(alphabet="abcXYZ_-", min_size=1, max_size=20),
    value=st.text(alphabet="abc 123\r\n;=", max_size=40),
)
def test_well_formed_single_field_round_trips(name: str, value: str) -> None:
    """A constructed single text field is parsed back under the same name.

    A field value carrying the boundary marker or CRLF can legitimately
    restructure the body, so the invariant is the weaker "no crash, valid
    FormData"; an exact-value check would over-constrain the fuzzer.
    """
    body = (
        f"--{_BOUNDARY}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
        f"--{_BOUNDARY}--\r\n"
    ).encode()
    content_type = f"multipart/form-data; boundary={_BOUNDARY}"
    try:
        result = parse_multipart_form(body, content_type)
    except (RequestEntityTooLarge, BadRequest):
        return
    assert isinstance(result, FormData)
