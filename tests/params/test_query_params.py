"""QueryParams.from_query_string parsing and DoS bound."""

from __future__ import annotations

import pytest

from veloce.exceptions import RequestURITooLong
from veloce.http.datastructures import _MAX_QUERY_FIELDS, QueryParams


def test_from_query_string_basic():
    qp = QueryParams.from_query_string("a=1&b=2&a=3")
    assert qp.getlist("a") == ["1", "3"]
    assert qp.getlist("b") == ["2"]


def test_from_query_string_empty():
    assert list(QueryParams.from_query_string("")) == []


def test_from_query_string_keeps_blank_values():
    qp = QueryParams.from_query_string("a=&b=2")
    assert qp.getlist("a") == [""]


def test_from_query_string_rejects_overflow():
    overflow = "&".join(f"k{i}=v" for i in range(_MAX_QUERY_FIELDS + 1))
    with pytest.raises(RequestURITooLong) as exc:
        QueryParams.from_query_string(overflow)
    assert exc.value.status_code == 414


def test_from_query_string_at_limit_passes():
    at_limit = "&".join(f"k{i}=v" for i in range(_MAX_QUERY_FIELDS))
    qp = QueryParams.from_query_string(at_limit)
    assert len(list(qp.items())) == _MAX_QUERY_FIELDS
