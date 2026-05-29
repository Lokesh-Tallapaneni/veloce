"""Accept-* header parsing + best_match (Q25)."""

from __future__ import annotations

from veloce import AcceptHeader, Request


def _req(headers: dict[str, str]) -> Request:
    return Request(method="GET", path="/", query_string="", headers=headers, body=b"")


# ── AcceptHeader.parse ────────────────────────────────────────────────


def test_parse_plain_comma_separated():
    h = AcceptHeader.parse("en, fr, de")
    assert h.values == ["en", "fr", "de"]
    assert h.quality("en") == 1.0
    assert h.quality("fr") == 1.0


def test_parse_with_q_values():
    h = AcceptHeader.parse("en;q=0.9, fr;q=0.7, de;q=0.5")
    assert h.quality("en") == 0.9
    assert h.quality("fr") == 0.7
    assert h.quality("de") == 0.5


def test_parse_missing_q_defaults_to_1():
    h = AcceptHeader.parse("en, fr;q=0.5")
    assert h.quality("en") == 1.0
    assert h.quality("fr") == 0.5


def test_parse_malformed_q_falls_back_to_1():
    """`q=invalid` shouldn't crash the parser; treat as default."""
    h = AcceptHeader.parse("en;q=notanumber")
    assert h.quality("en") == 1.0


def test_parse_empty_string_yields_empty_header():
    h = AcceptHeader.parse("")
    assert h.values == []
    assert bool(h) is False


def test_q_zero_marks_explicit_rejection():
    h = AcceptHeader.parse("en, de;q=0")
    assert h.quality("en") == 1.0
    assert h.quality("de") == 0.0
    assert "de" not in h


# ── best_match ────────────────────────────────────────────────────────


def test_best_match_highest_q():
    h = AcceptHeader.parse("en;q=0.5, fr;q=0.9, de;q=0.7")
    assert h.best_match(["en", "fr", "de"]) == "fr"


def test_best_match_returns_first_when_no_header():
    """Empty Accept header = client accepts anything → first option wins."""
    h = AcceptHeader.parse("")
    assert h.best_match(["en", "fr"]) == "en"


def test_best_match_default_when_nothing_acceptable():
    h = AcceptHeader.parse("en, fr")
    assert h.best_match(["de", "it"]) is None
    assert h.best_match(["de", "it"], default="fallback") == "fallback"


def test_best_match_respects_caller_order_on_tie():
    h = AcceptHeader.parse("en, fr")  # both q=1.0
    # Tie → first in caller's option list wins.
    assert h.best_match(["fr", "en"]) == "fr"
    assert h.best_match(["en", "fr"]) == "en"


# ── MIME wildcards ────────────────────────────────────────────────────


def test_mime_star_star_matches_anything():
    h = AcceptHeader.parse("*/*", mime=True)
    assert h.quality("text/html") == 1.0
    assert h.quality("application/json") == 1.0


def test_mime_type_star_matches_subtypes():
    h = AcceptHeader.parse("text/*;q=0.8, application/json", mime=True)
    assert h.quality("text/html") == 0.8
    assert h.quality("text/plain") == 0.8
    assert h.quality("application/json") == 1.0
    assert h.quality("image/png") == 0.0


def test_mime_best_match_typical_browser_accept():
    """Real-world: a browser sends a long Accept list — pick HTML."""
    raw = "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,*/*;q=0.8"
    h = AcceptHeader.parse(raw, mime=True)
    assert h.best_match(["text/html", "application/json"]) == "text/html"


def test_non_mime_does_not_match_wildcards():
    """`*/*` style isn't a valid Accept-Language token; it shouldn't match."""
    h = AcceptHeader.parse("*/*")  # mime=False
    assert h.quality("en") == 0.0


# ── Request integration ──────────────────────────────────────────────


def test_request_accept_mimetypes():
    req = _req({"accept": "application/json;q=0.9, text/html;q=0.8"})
    assert req.accept_mimetypes.best_match(["text/html", "application/json"]) == (
        "application/json"
    )


def test_request_accept_languages():
    req = _req({"accept-language": "fr-FR;q=0.9, en;q=0.5"})
    assert req.accept_languages.best_match(["en", "fr-FR"]) == "fr-FR"


def test_request_accept_encodings():
    req = _req({"accept-encoding": "br, gzip;q=0.5"})
    assert req.accept_encodings.best_match(["gzip", "br"]) == "br"


def test_request_accept_charsets():
    req = _req({"accept-charset": "utf-8;q=0.9, iso-8859-1;q=0.5"})
    assert req.accept_charsets.best_match(["iso-8859-1", "utf-8"]) == "utf-8"


def test_missing_accept_header_returns_first_option():
    """No header sent → client has no preference → first option wins."""
    req = _req({})
    assert req.accept_mimetypes.best_match(["text/html", "application/json"]) == ("text/html")


def test_iter_yields_values_in_order():
    h = AcceptHeader.parse("en;q=0.5, fr, de;q=0.7")
    assert list(h) == ["en", "fr", "de"]


def test_contains_checks_quality():
    h = AcceptHeader.parse("en, fr;q=0")
    assert "en" in h
    assert "fr" not in h


# ── RFC 9110 §12.5.4 wildcard for non-MIME Accept-* headers ──────────


def test_non_mime_bare_wildcard_matches_any_value():
    h = AcceptHeader.parse("*")
    assert h.quality("en-US") == 1.0
    assert h.quality("fr") == 1.0
    assert h.quality("gzip") == 1.0


def test_non_mime_wildcard_respects_q_value():
    h = AcceptHeader.parse("en, *;q=0.1")
    assert h.quality("en") == 1.0
    assert h.quality("fr") == 0.1


def test_non_mime_explicit_beats_wildcard():
    h = AcceptHeader.parse("*;q=0.3, en;q=0.9")
    assert h.quality("en") == 0.9
    assert h.quality("fr") == 0.3
