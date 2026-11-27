"""`mount_mcp` documents every argument it takes, one paragraph each.

Twelve parameters used to be described inside a single unbroken 49-line
paragraph, out of signature order - so a reader looking up `page_size` had to
scan the whole block for the sentence that happens to start with it, and a
parameter added later could be appended anywhere or forgotten entirely.
"""

from __future__ import annotations

import inspect

import pytest

from veloce import Veloce

DOC = inspect.getdoc(Veloce.mount_mcp) or ""
PARAMS = [p for p in inspect.signature(Veloce.mount_mcp).parameters if p != "self"]


def test_the_docstring_is_there_at_all() -> None:
    assert len(DOC) > 500, "the scan below passes trivially on a short docstring"
    assert len(PARAMS) >= 13


@pytest.mark.parametrize("name", PARAMS)
def test_every_parameter_is_named_in_the_docstring(name: str) -> None:
    assert f"`{name}`" in DOC or f"{name}=" in DOC, (
        f"mount_mcp takes {name} and the docstring never mentions it"
    )


def test_no_paragraph_is_a_wall() -> None:
    """One paragraph per parameter; the original was 49 lines in one block."""
    longest = max(len(p.split("\n")) for p in DOC.split("\n\n"))
    assert longest <= 12, f"a {longest}-line paragraph is back"


def test_the_argument_paragraphs_follow_signature_order() -> None:
    """Out-of-order prose is what made the original hard to search."""
    body = DOC[DOC.index("**Arguments**") :]
    positions = [(body.index(f"`{p}`"), p) for p in PARAMS if f"`{p}`" in body]
    assert [p for _, p in positions] == [p for _, p in sorted(positions)], (
        f"documented out of signature order: {[p for _, p in sorted(positions)]}"
    )
