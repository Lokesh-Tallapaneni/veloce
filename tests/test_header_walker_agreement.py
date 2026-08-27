r"""The two quoted-string walkers implement one grammar, checked not claimed.

`split_outside_quotes` and `parse_header_params` both walk a header value
tracking whether they are inside a quoted string. `split_outside_quotes` used to
carry a docstring saying it "mirrors parse_header_params' inner escape handling
so the two walkers stay consistent" - and they did not: it honoured a
``\<char>`` escape everywhere, where `parse_header_params` honours one only
inside a quoted string, which is the whole of what RFC 9110 Sec. 5.6.4 defines
`quoted-pair` for.

That is not a tidiness problem. `Forwarded:` reaches `split_outside_quotes`
attacker-supplied, and `ProxyFix` selects the trusted hop by counting elements
from the right - so a sender who could suppress a delimiter could merge the
trusted proxy's element into their own. This module holds the two walkers to
the same grammar by running both, rather than asserting a comment.
"""

from __future__ import annotations

import pytest

from veloce._header_parsing import parse_header_params, split_outside_quotes

BS = chr(92)

AGREEMENT_CASES = [
    "a=1;b=2",
    f"a=1{BS};b=2",
    f"a=1{BS}{BS};b=2",
    'a="x,y";b=2',
    f'a="x{BS}"y";b=2',
    'a="";b=2',
    f"a={BS};b=2",
    "a=1;b=2;c=3",
    f"a=1;b={BS}",
]


@pytest.mark.parametrize("value", AGREEMENT_CASES, ids=repr)
def test_both_walkers_find_the_same_delimiters(value: str) -> None:
    """The split's token count must equal the params walker's parameter count."""
    tokens = [t for t in split_outside_quotes(value, ";") if t.strip()]
    _positional, params = parse_header_params(value, delimiter=";", unescape=False)
    assert len(tokens) == len(params), (
        f"split_outside_quotes found {len(tokens)} token(s) and parse_header_params "
        f"{len(params)} parameter(s) in {value!r} - the two disagree about where "
        "a delimiter is"
    )


def test_a_backslash_outside_quotes_does_not_hide_a_delimiter() -> None:
    """RFC 9110 Sec. 5.6.4: `quoted-pair` exists only inside a quoted-string."""
    assert split_outside_quotes(f"a=1{BS};b=2", ";") == [f"a=1{BS}", "b=2"]


def test_a_backslash_inside_quotes_still_escapes() -> None:
    """The escape is real where the grammar defines it."""
    assert split_outside_quotes(f'a="x{BS}"y";b=2', ";") == [f'a="x{BS}"y"', "b=2"]


def test_a_quoted_delimiter_is_still_protected() -> None:
    assert split_outside_quotes('host="a,b",for=1.2.3.4', ",") == ['host="a,b"', "for=1.2.3.4"]


class TestForwardedHopSelectionIsNotEvadable:
    """The security property, driven through `ProxyFix` rather than the walker."""

    @staticmethod
    def _parse(header: str) -> dict[str, str]:
        from veloce.middleware.proxy_fix import ProxyFix

        return ProxyFix.__new__(ProxyFix)._parse_forwarded(header, 1, 0, 1, 0)

    def test_a_trailing_backslash_cannot_absorb_the_trusted_hop(self) -> None:
        spoofed = self._parse(f"for=6.6.6.6{BS}, for=10.0.0.1")
        assert spoofed == {"for": "10.0.0.1"}

    def test_the_unescaped_header_is_unchanged(self) -> None:
        assert self._parse("for=6.6.6.6, for=10.0.0.1") == {"for": "10.0.0.1"}

    def test_a_quoted_comma_still_does_not_fake_a_hop(self) -> None:
        parsed = self._parse('host="a,b", for=10.0.0.1')
        assert parsed["for"] == "10.0.0.1"


def test_remote_addr_is_not_attacker_controlled_through_a_backslash() -> None:
    """End to end: the value a handler reads, through the real middleware."""
    from veloce import Veloce
    from veloce.middleware.proxy_fix import ProxyFix
    from veloce.testclient import TestClient

    app = Veloce(openapi_url=None)
    app.add_middleware(ProxyFix(x_for=1, x_proto=0, x_host=0, x_prefix=0))

    @app.get("/who")
    async def who(request):
        return {"remote_addr": request.remote_addr}

    with TestClient(app) as client:
        clean = client.get("/who", headers={"Forwarded": "for=6.6.6.6, for=10.0.0.1"})
        spoof = client.get("/who", headers={"Forwarded": f"for=6.6.6.6{BS}, for=10.0.0.1"})

    assert clean.json()["remote_addr"] == "10.0.0.1"
    assert spoof.json()["remote_addr"] == "10.0.0.1", (
        "a single attacker-supplied backslash put attacker text into remote_addr"
    )
