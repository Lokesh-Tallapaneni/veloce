"""SSE event store — the opt-in replay buffer behind resumable streams.

When resumability is enabled the server attaches an id to each SSE event carrying
a JSON-RPC payload and keeps a bounded history, so a client that reconnects with
a ``Last-Event-ID`` is replayed only what it missed.

Per the MCP 2025-06-18 / 2025-11-25 Streamable HTTP transport an event id SHOULD
encode its originating stream and a replay MUST be scoped to that one stream: an
id is ``"{stream}.{seq}"`` and `replay_after` walks only that stream's ring, so a
resumed GET cannot leak another POST's events.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from typing import Any

# Events retained per stream, capping the replay window so a never-reconnecting
# client cannot grow the buffer without bound.
_MAX_EVENTS_PER_STREAM = 256

# Streams retained at once. Each resumable POST mints a fresh stream id, so
# without this cap the store would grow by one stream per request forever.
_MAX_STREAMS = 1024

# Separator in an event id. URL-safe base64 stream ids never contain it, so a
# split on the last occurrence recovers the two parts unambiguously.
_EVENT_ID_SEP = "."


class SSEEventStore:
    """Record SSE events per stream and replay a stream's missed tail on resume."""

    __slots__ = ("_streams", "_max_streams")

    def __init__(self, max_streams: int = _MAX_STREAMS) -> None:
        # One bounded ring per stream id, LRU-ordered so the least-recently-active
        # stream is evicted first when the stream cap is exceeded.
        self._streams: OrderedDict[str, deque[tuple[int, str, dict[str, Any]]]] = OrderedDict()
        self._max_streams = max_streams

    def record(self, stream_id: str, seq: int, payload: dict[str, Any]) -> str:
        """Store one outbound `payload` for `stream_id` and return its event id.

        The id is `"{stream_id}.{seq}"`, so a later `Last-Event-ID` resolves to
        the right ring.
        """
        event_id = f"{stream_id}{_EVENT_ID_SEP}{seq}"
        ring = self._streams.get(stream_id)
        if ring is None:
            ring = deque(maxlen=_MAX_EVENTS_PER_STREAM)
            self._streams[stream_id] = ring
            # A stream that never reconnects cannot keep its history alive forever.
            while len(self._streams) > self._max_streams:
                self._streams.popitem(last=False)
        else:
            # Keep an in-use stream off the eviction front.
            self._streams.move_to_end(stream_id)
        ring.append((seq, event_id, payload))
        return event_id

    def replay_after(self, last_event_id: str) -> list[tuple[str, dict[str, Any]]]:
        """Return the `(event_id, payload)` events after `last_event_id`, in order.

        Replay is scoped to the stream the id names; an unknown or malformed id
        yields an empty list rather than crossing into another stream's history.
        """
        stream_id, sep, raw_seq = last_event_id.rpartition(_EVENT_ID_SEP)
        if not sep:
            return []
        ring = self._streams.get(stream_id)
        if ring is None:
            return []
        try:
            after = int(raw_seq)
        except ValueError:
            return []
        # By sequence rather than identity, so a resume from the priming id
        # (sequence 0) replays the whole tail and an evicted id replays the rest.
        return [(event_id, payload) for seq, event_id, payload in ring if seq > after]
