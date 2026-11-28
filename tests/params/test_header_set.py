"""HeaderSet ordered case-insensitive set."""

from __future__ import annotations

import pytest

from veloce.http.header_set import HeaderSet


def test_empty_constructor():
    hs = HeaderSet()
    assert len(hs) == 0
    assert not hs
    assert hs.to_header() == ""


def test_from_comma_string():
    hs = HeaderSet("GET, POST, PUT")
    assert len(hs) == 3
    assert "GET" in hs
    assert "post" in hs  # case-insensitive
    assert "delete" not in hs


def test_from_iterable():
    hs = HeaderSet(["gzip", "deflate"])
    assert "GZIP" in hs
    assert list(hs) == ["gzip", "deflate"]


def test_add_dedupes_case_insensitively():
    hs = HeaderSet()
    hs.add("GET")
    hs.add("get")
    hs.add("Get")
    assert len(hs) == 1
    assert list(hs) == ["GET"]


def test_add_preserves_insertion_order():
    hs = HeaderSet()
    hs.add("a")
    hs.add("b")
    hs.add("c")
    assert list(hs) == ["a", "b", "c"]


def test_discard_is_silent_when_missing():
    hs = HeaderSet("a")
    hs.discard("xxx")  # no exception


def test_discard_removes_entry():
    hs = HeaderSet("a, b, c")
    hs.discard("B")
    assert list(hs) == ["a", "c"]
    assert "b" not in hs


def test_remove_raises_when_missing():
    with pytest.raises(KeyError):
        HeaderSet().remove("x")


def test_to_header_preserves_order():
    hs = HeaderSet("GET, POST")
    hs.add("DELETE")
    assert hs.to_header() == "GET, POST, DELETE"


def test_update_adds_many():
    hs = HeaderSet("a")
    hs.update(["b", "c", "A"])  # "A" is a dup of "a"
    assert len(hs) == 3
    assert list(hs) == ["a", "b", "c"]


def test_clear_resets():
    hs = HeaderSet("a, b")
    hs.clear()
    assert not hs


def test_eq_with_set():
    hs = HeaderSet("a, b")
    assert hs == {"A", "B"}
    assert hs == ["a", "b"]
    assert hs != {"a", "c"}


def test_eq_with_another_headerset():
    a = HeaderSet("GET, POST")
    b = HeaderSet("post, get")
    assert a == b


def test_str_roundtrips():
    hs = HeaderSet("gzip, deflate")
    assert str(hs) == "gzip, deflate"


def test_skips_blank_tokens_from_string():
    hs = HeaderSet("a, , b,, c")
    assert list(hs) == ["a", "b", "c"]
