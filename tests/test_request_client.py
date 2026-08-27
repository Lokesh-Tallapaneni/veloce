"""Request.client Address(host, port) accessor."""

from __future__ import annotations

from veloce import Request
from veloce.http.request import Address


def test_client_none_for_synthetic_request():
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    assert req.client is None


def test_client_from_asgi_scope():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={},
        body=b"",
        scope={"client": ("203.0.113.7", 54321)},
    )
    assert req.client == Address("203.0.113.7", 54321)


def test_client_host_and_port_attributes():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={},
        body=b"",
        scope={"client": ("10.0.0.1", 8080)},
    )
    assert req.client.host == "10.0.0.1"
    assert req.client.port == 8080


def test_client_tuple_unpacking():
    req = Request(
        method="GET",
        path="/",
        query_string="",
        headers={},
        body=b"",
        scope={"client": ("1.2.3.4", 9000)},
    )
    host, port = req.client
    assert host == "1.2.3.4"
    assert port == 9000


def test_client_honours_proxy_fix():
    req = Request(method="GET", path="/", query_string="", headers={}, body=b"")
    req.state["proxy_fix_client"] = "198.51.100.9"
    # client_host returns the trusted IP; port falls back to 0.
    assert req.client == Address("198.51.100.9", 0)


def test_address_is_namedtuple():
    a = Address("h", 1)
    assert isinstance(a, tuple)
    assert a._fields == ("host", "port")
