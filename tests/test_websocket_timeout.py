"""WebSocket receive with a timeout."""

from __future__ import annotations

import asyncio

import pytest


class TestWebSocketTimeout:
    """Test WebSocket receive with timeout."""

    @pytest.mark.asyncio
    async def test_receive_timeout(self):
        from veloce.websocket import WebSocket

        class FakeTransport:
            def write(self, data):
                pass

            def close(self):
                pass

            def get_extra_info(self, key):
                return None

        ws = WebSocket(FakeTransport(), {"sec-websocket-key": "test"})
        # Skip the full handshake — the test only exercises the
        # `wait_for(_receive_queue.get())` timeout, but `receive_text`
        # now refuses to run before `accept()` (a real handshake state
        # check). Flipping the flag mirrors the post-accept state.
        ws._accepted = True

        with pytest.raises(asyncio.TimeoutError):
            await ws.receive_text(timeout=0.01)
