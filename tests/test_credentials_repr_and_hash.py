"""Credential objects do not render their secret, and stay hashable.

`HTTPBasicCredentials` and `HTTPDigestCredentials` became `@dataclass(slots=True)`
during a style pass. A dataclass generates a field-rendering `__repr__` and sets
`__hash__ = None`, so two things changed that the pass did not intend:

    repr(HTTPBasicCredentials("u", "hunter2"))
    -> HTTPBasicCredentials(username='u', password='hunter2')

Anything downstream of `credentials: HTTPBasicCredentials = Depends(HTTPBasic())`
that raises now carries the plaintext password in that frame's locals. An error
tracker capturing frame locals writes it to persistent storage; so does
`logger.exception("auth failed for %r", credentials)`, or any repr-based
structured log.

The repository already set the opposite convention next door: `secret.py`
renders `Secret('***')` so the plaintext escapes only through an explicit
accessor.

The hashability is the same edit's other half. Before the conversion both were
hand-written slotted classes with identity equality, so they could be put in a
set or used as a dict key.
"""

from __future__ import annotations

import logging

import pytest

from veloce.security.http import HTTPBasicCredentials, HTTPDigestCredentials

PASSWORD = "hunter2"


def test_a_basic_credentials_repr_hides_the_password():
    rendered = repr(HTTPBasicCredentials("u", PASSWORD))

    assert PASSWORD not in rendered
    assert "***" in rendered


def test_a_basic_credentials_repr_still_names_the_user():
    """Masking must not make the object useless in a log."""
    rendered = repr(HTTPBasicCredentials("alice", PASSWORD))

    assert "alice" in rendered
    assert "HTTPBasicCredentials" in rendered


def test_the_password_is_still_readable_through_the_attribute():
    """The mask is on the rendering, not on the value."""
    assert HTTPBasicCredentials("u", PASSWORD).password == PASSWORD


def test_a_basic_credentials_object_is_hashable():
    creds = HTTPBasicCredentials("u", PASSWORD)

    assert {creds}
    assert {creds: 1}[creds] == 1


def test_a_digest_credentials_repr_hides_the_response():
    """`response` is the value that authenticates the request - RFC 7616 Sec. 3.4."""
    rendered = repr(HTTPDigestCredentials(username="u", response="deadbeefcafe"))

    assert "deadbeefcafe" not in rendered
    assert "***" in rendered


def test_a_digest_credentials_repr_keeps_the_protocol_fields():
    """The nonce and realm are what make the render worth having."""
    rendered = repr(
        HTTPDigestCredentials(username="u", realm="api", nonce="n1", response="deadbeef")
    )

    assert "api" in rendered
    assert "n1" in rendered
    assert "u" in rendered


def test_a_digest_credentials_object_is_hashable():
    assert {HTTPDigestCredentials(username="u", response="x")}


@pytest.mark.parametrize(
    "creds",
    [
        HTTPBasicCredentials("u", PASSWORD),
        HTTPDigestCredentials(username="u", response=PASSWORD),
    ],
    ids=["basic", "digest"],
)
def test_a_formatted_credential_leaks_nothing(creds):
    """Through `logging` itself, which is the leak the masking exists to stop.

    `logger.exception("auth failed for %r", credentials)` defers the formatting
    to the logging module, so the render happens at emit time inside a handler.
    Building the record and formatting it is what a deployment actually does.
    """
    record = logging.LogRecord(
        "veloce.test", logging.ERROR, __file__, 0, "auth failed for %r", (creds,), None
    )

    assert PASSWORD not in logging.Formatter().format(record)
    assert PASSWORD not in f"{creds!r}"
    assert PASSWORD not in str(creds)
