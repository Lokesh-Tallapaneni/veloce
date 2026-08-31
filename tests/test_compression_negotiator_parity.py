"""The compression negotiator answers like `AcceptHeader`, or deliberately not.

`CompressionMiddleware` carries its own RFC 9110 Sec. 12.4.2 q-value parser and
negotiator (`_quality`, `_negotiate`) although
`http/datastructures.AcceptHeader` implements the same rules and
`Request.accept_encodings` already exposes a cached parsed instance.

The duplication is real. What is *not* real is a divergence: the two were
differential-tested across every header shape that matters - bare tokens, mixed
weights, `q=0` refusals, the `Q=` spelling, a malformed weight, wildcards, and a
wildcard combined with a refusal - and they agree on all of them.

They differ in exactly one case, and it is deliberate:

    Accept-Encoding absent   _negotiate -> None (send identity)
                             best_match -> the first offered coding

RFC 9110 Sec. 12.5.3 does say an absent header means any coding is acceptable,
so `best_match`'s reading is the letter of the spec. Compressing for a client
that never asked is the kind of thing old intermediaries mishandle, so the
middleware's `if not accept: return None` is the conservative reading and is
kept.

This module exists because two implementations of one rule set that nobody
compares are how the *next* divergence gets in. Consolidating them would put an
`AcceptHeader` parse on the per-response path in place of a hand-rolled fast
path, which is a change that needs its own measurement; pinning them against
each other costs nothing and closes the drift.
"""

from __future__ import annotations

import pytest

from veloce.http.datastructures import AcceptHeader
from veloce.middleware.compression import _negotiate, _quality

OFFERED = ("br", "gzip", "deflate")

HEADERS = [
    "gzip",
    "gzip, deflate",
    "br;q=1.0, gzip;q=0.8",
    "gzip;q=0",
    "*",
    "*;q=0",
    "gzip;q=0, *",
    "identity",
    "gzip;q=0.5, br;q=0.5",
    "GZIP",
    "gzip;Q=0",
    "gzip;q=bad",
    "deflate, gzip;q=0",
    "*, gzip;q=0",
    "br;q=0.9, gzip;q=0.9, deflate;q=1.0",
    "  gzip  ,  br  ",
    "br;q=0.001",
    "gzip;q=1.0, gzip;q=0",
]


# ── the two implementations agree ────────────────────────────────────


@pytest.mark.parametrize("header", HEADERS)
def test_the_negotiator_agrees_with_accept_header(header):
    """A non-empty header must resolve the same way through either parser."""
    mine = _negotiate(header, OFFERED)
    theirs = AcceptHeader.parse(header).best_match(list(OFFERED), default=None)
    assert mine == theirs, header


@pytest.mark.parametrize("header", HEADERS)
@pytest.mark.parametrize("offered", [("gzip",), ("gzip", "br"), ("deflate", "gzip", "br")])
def test_they_agree_whatever_the_server_offers(header, offered):
    """Tie-breaking is by the server's order in both, so the offer list must not
    be what separates them."""
    mine = _negotiate(header, offered)
    theirs = AcceptHeader.parse(header).best_match(list(offered), default=None)
    assert mine == theirs, (header, offered)


# ── the one deliberate difference ────────────────────────────────────


def test_an_absent_header_sends_identity():
    """The documented divergence, pinned so it is a decision and not a bug.

    Changing this to follow `best_match` would start compressing for clients
    that never advertised support.
    """
    assert _negotiate("", OFFERED) is None


def test_accept_header_would_have_compressed():
    """The other half of that statement, so the difference is visible here
    rather than inferred."""
    assert AcceptHeader.parse("").best_match(list(OFFERED), default=None) == "br"


# ── the two helpers implement the rules they claim ───────────────────


@pytest.mark.parametrize(
    ("pieces", "refused"),
    [
        (["gzip", "q=0"], True),
        (["gzip", "Q=0"], True),
        (["gzip", "q=0.0"], True),
        (["gzip", "q=0.1"], False),
        (["gzip"], False),
        (["gzip", "q=bad"], False),
    ],
)
def test_a_refusal_reads_both_spellings(pieces, refused):
    """RFC 5234 Sec. 2.3 makes the ABNF literal case-insensitive.

    Asserted through `_quality`, because that is what `_negotiate` relies on: a
    refused coding gets weight 0.0, and the selection loop's `weight >
    best_weight` (starting at 0.0) can never pick it. There used to be a
    separate `_refuses` predicate saying the same thing, with no caller in
    `src/` - only this test - so it was dead code kept alive by its own test.
    """
    assert (_quality(pieces) == 0) is refused


@pytest.mark.parametrize(
    ("pieces", "weight"),
    [
        (["gzip"], 1.0),
        (["gzip", "q=0.5"], 0.5),
        (["gzip", "Q=0.5"], 0.5),
        (["gzip", "q=bad"], 1.0),
        (["gzip", "q=0"], 0.0),
    ],
)
def test_quality_defaults_to_one(pieces, weight):
    """A malformed weight reads as the default the client would have got by
    omitting it, rather than being fatal."""
    assert _quality(pieces) == weight


def test_a_refusal_beats_a_wildcard():
    """`gzip;q=0, *` must not select gzip, in either implementation."""
    assert _negotiate("gzip;q=0, *", ("gzip", "br")) == "br"
    assert AcceptHeader.parse("gzip;q=0, *").best_match(["gzip", "br"]) == "br"


def test_nothing_offered_is_none():
    assert _negotiate("gzip", ()) is None


# ── the defect the parity check exposed ──────────────────────────────
#
# Pinning the two against each other found a real bug, and not in the
# middleware: `AcceptHeader.best_match` ranked non-MIME headers through
# `quality()`, which reports the MAX across an exact match and a `*` match. So a
# client that explicitly refused a coding and accepted anything else was
# recommended the refused one.
#
# `quality_explicit` already existed for exactly this - its docstring names the
# bug - but only the precompressed-static path used it. `best_match` now ranks
# non-MIME headers through it too. The MIME path was already correct, resolving
# by most-specific range.


def test_an_explicit_refusal_is_not_overridden_by_a_wildcard():
    """`gzip;q=0, *` selected `gzip` before this."""
    assert AcceptHeader.parse("gzip;q=0, *").best_match(["gzip"]) is None


def test_a_refused_coding_falls_through_to_an_accepted_one():
    assert AcceptHeader.parse("gzip;q=0, *").best_match(["gzip", "br"]) == "br"


def test_the_same_holds_for_a_language_header():
    """Non-MIME semantics, so `Accept-Language` had the same defect."""
    assert AcceptHeader.parse("en;q=0, *").best_match(["en", "fr"]) == "fr"


def test_the_refusal_reaches_the_public_request_property():
    """Through `request.accept_encodings`, which is the documented surface."""
    from tests.conftest import make_request

    request = make_request(path="/", headers={"Accept-Encoding": "gzip;q=0, *"})
    assert request.accept_encodings.best_match(["gzip"]) is None


def test_a_repeated_token_honours_the_refusal():
    """`gzip;q=1.0, gzip;q=0` is undefined by RFC 9110; read as fail-closed."""
    assert AcceptHeader.parse("gzip;q=1.0, gzip;q=0").best_match(["gzip"]) is None
    assert AcceptHeader.parse("gzip;q=0, gzip;q=1.0").best_match(["gzip"]) is None


def test_a_wildcard_still_supplies_a_weight_for_an_unlisted_coding():
    """The negative: the fix must not make `*` stop working."""
    assert AcceptHeader.parse("*").best_match(["br", "gzip"]) == "br"
    assert AcceptHeader.parse("gzip;q=0.5, *").best_match(["br"]) == "br"


def test_a_mime_header_is_unaffected():
    """The MIME path resolves by most-specific range and was already correct."""
    header = AcceptHeader.parse("application/json;q=0, */*", mime=True)
    assert header.best_match(["application/json", "text/html"]) == "text/html"
