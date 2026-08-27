"""Both body sources answer the interface `Request` consumes them through.

`ASGIBodySource`'s docstring said it "presents the SAME consumer interface" as
`RequestBodySource` and nothing declared what that interface is - so
`Request.is_disconnected()` probed with `getattr(source, "_disconnected", False)`,
a private attribute only one of the two defined.

Declaring it as `BodySource` surfaced why the probe needed a default: the native
source never set the flag. `HttpProtocol.connection_lost` signals a vanished
client with `feed_eof()`, indistinguishable from a completed body, so a native
streaming route was told the client was still there.
"""

from __future__ import annotations

import pytest

from veloce import Request, Veloce
from veloce.http._body import ASGIBodySource, BodySource, RequestBodySource
from veloce.serving.protocol import HttpProtocol

SOURCES = [RequestBodySource, ASGIBodySource]


@pytest.mark.parametrize("cls", SOURCES, ids=lambda c: c.__name__)
def test_the_source_declares_the_interface(cls: type) -> None:
    assert issubclass(cls, BodySource)


@pytest.mark.parametrize("cls", SOURCES, ids=lambda c: c.__name__)
@pytest.mark.parametrize("member", ["__aiter__", "__anext__", "read", "disconnected"])
def test_every_member_is_implemented_not_inherited(cls: type, member: str) -> None:
    """Inheriting the base's `raise NotImplementedError` is not implementing it."""
    assert getattr(cls, member) is not getattr(BodySource, member), (
        f"{cls.__name__}.{member} is the base's stub"
    )


@pytest.mark.parametrize("cls", SOURCES, ids=lambda c: c.__name__)
def test_the_subclass_keeps_slots(cls: type) -> None:
    """A slotted base's subclass regains a `__dict__` if it forgets."""
    assert "__slots__" in cls.__dict__


async def test_a_completed_native_body_is_not_disconnected() -> None:
    source = RequestBodySource()
    source.feed(b"hello")
    source.feed_eof()
    assert await source.read() == b"hello"
    assert source.disconnected is False


async def test_a_native_body_cut_short_reports_disconnected() -> None:
    """The gap declaring the interface found: this used to answer False."""
    source = RequestBodySource()
    source.feed(b"partial")
    source.feed_eof(disconnected=True)
    assert await source.read() == b"partial"
    assert source.disconnected is True


async def test_a_completed_asgi_body_is_not_disconnected() -> None:
    messages = iter(
        [
            {"type": "http.request", "body": b"a", "more_body": True},
            {"type": "http.request", "body": b"b", "more_body": False},
        ]
    )

    async def receive() -> dict:
        return next(messages)

    source = ASGIBodySource(receive)
    assert await source.read() == b"ab"
    assert source.disconnected is False


async def test_an_asgi_disconnect_reports_disconnected() -> None:
    messages = iter(
        [
            {"type": "http.request", "body": b"a", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive() -> dict:
        return next(messages)

    source = ASGIBodySource(receive)
    assert await source.read() == b"a"
    assert source.disconnected is True


def _request(source: object = None) -> Request:
    return Request(
        method="GET", path="/", query_string="", headers={}, body=b"", body_source=source
    )


@pytest.mark.parametrize("cls", SOURCES, ids=lambda c: c.__name__)
async def test_the_request_consumer_reads_the_declared_attribute(cls: type) -> None:
    """`Request.is_disconnected()` through both sources, not through a getattr."""
    source = cls() if cls is RequestBodySource else cls(lambda: None)
    assert await _request(source).is_disconnected() is False


async def test_a_cut_short_native_body_reaches_the_consumer() -> None:
    source = RequestBodySource()
    source.feed_eof(disconnected=True)
    assert await _request(source).is_disconnected() is True


async def test_a_request_with_no_source_is_not_disconnected() -> None:
    assert await _request().is_disconnected() is False


class TestTheNativeProtocolReportsAVanishedClient:
    """The gap end to end: `connection_lost` mid-body, through the real protocol.

    The unit tests above drive the source directly, so they pass whether or not
    the protocol passes `disconnected=True`. This drives `HttpProtocol`, which
    is where the flag was never being set.
    """

    @staticmethod
    async def _protocol_with_streaming_route():
        import asyncio

        from ._protocol import _FakeTransport

        seen: dict[str, object] = {}
        app = Veloce(openapi_url=None)

        async def upload(request):
            seen["request"] = request
            chunks = []
            async for chunk in request.stream():
                chunks.append(chunk)
            seen["body"] = b"".join(chunks)
            return {"received": len(seen["body"])}

        app.add_route("/upload", upload, methods=["POST"], stream=True)

        protocol = HttpProtocol(app, asyncio.get_running_loop())
        transport = _FakeTransport()
        protocol.connection_made(transport)
        return protocol, seen

    async def test_a_client_that_vanishes_mid_body_is_reported(self) -> None:
        import asyncio

        protocol, seen = await self._protocol_with_streaming_route()
        protocol.data_received(
            b"POST /upload HTTP/1.1\r\nHost: t\r\nContent-Length: 100\r\n\r\nhalf"
        )
        for _ in range(6):
            await asyncio.sleep(0)

        request = seen.get("request")
        assert request is not None, "the streaming handler never started"
        assert await request.is_disconnected() is False, "not disconnected yet"

        protocol.connection_lost(None)
        for _ in range(6):
            await asyncio.sleep(0)

        assert await request.is_disconnected() is True, (
            "the client went away mid-body and the request still reports connected"
        )

    async def test_a_body_that_completes_is_not_reported_as_disconnected(self) -> None:
        import asyncio

        protocol, seen = await self._protocol_with_streaming_route()
        protocol.data_received(b"POST /upload HTTP/1.1\r\nHost: t\r\nContent-Length: 4\r\n\r\nhalf")
        for _ in range(8):
            await asyncio.sleep(0)

        request = seen.get("request")
        assert request is not None
        assert seen.get("body") == b"half"
        assert await request.is_disconnected() is False
