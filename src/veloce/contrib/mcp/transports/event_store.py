"""SSE event store — the opt-in replay buffer behind resumable streams.

The Streamable HTTP transport drops every event after it is written. When
resumability is enabled the server instead attaches an id to each SSE event that
carries a JSON-RPC payload and keeps a bounded history of those events, so a
client whose connection drops can reconnect with a ``Last-Event-ID`` and have the
server replay only what it missed.

Per the MCP 2025-06-18 / 2025-11-25 Streamable HTTP transport an event id SHOULD
encode enough to identify its originating stream, and a replay MUST be scoped to
that one stream — never events from a different stream. This store satisfies both:
an id is ``"{stream}.{seq}"`` and `replay_after` walks only the named stream's
ring, so a resumed GET cannot leak another POST's events.

The store is created only when the feature is enabled, so the stateless default
allocates nothing and the per-event path stays a single append.
"""

from __future__ import annotations

from collections import OrderedDict, deque
from typing import Any

# Events retained per stream. A resumable POST stream produces a handful of
# notifications plus its final response; this caps the replay window so a
# never-reconnecting client cannot grow the buffer without bound.
_MAX_EVENTS_PER_STREAM = 256

# Streams retained at once. Each resumable POST mints a fresh stream id, so the
# count of streams would otherwise grow by one per request for the process
# lifetime. The store keeps the most-recently-active streams up to this cap and
# evicts the oldest, bounding total memory to roughly cap * per-stream events.
_MAX_STREAMS = 1024

# Separator between the stream id and the per-stream sequence number in an event
# id. URL-safe base64 stream ids never contain it, so a split on the last
# occurrence recovers the two parts unambiguously.
_EVENT_ID_SEP = "."


class SSEEventStore:
    """Record SSE events per stream and replay a stream's missed tail on resume."""

    __slots__ = ("_streams", "_max_streams")

    def __init__(self, max_streams: int = _MAX_STREAMS) -> None:
        # One bounded ring per stream id: each entry is (seq, event_id, payload).
        # An `OrderedDict` so the oldest stream is evicted first when the stream
        # cap is exceeded (LRU on most-recent activity).
        self._streams: OrderedDict[str, deque[tuple[int, str, dict[str, Any]]]] = OrderedDict()
        self._max_streams = max_streams

    def record(self, stream_id: str, seq: int, payload: dict[str, Any]) -> str:
        """Store one outbound `payload` for `stream_id` and return its event id.

        The id is `"{stream_id}.{seq}"`, encoding the originating stream so a
        later `Last-Event-ID` resolves to the right ring. When a new stream pushes
        the count past the cap, the least-recently-active stream's history is
        evicted so the store cannot grow without bound.
        """
        event_id = f"{stream_id}{_EVENT_ID_SEP}{seq}"
        ring = self._streams.get(stream_id)
        if ring is None:
            ring = deque(maxlen=_MAX_EVENTS_PER_STREAM)
            self._streams[stream_id] = ring
            # Reclaim the oldest streams once the cap is crossed; a stream that
            # never reconnects cannot keep its history alive forever.
            while len(self._streams) > self._max_streams:
                self._streams.popitem(last=False)
        else:
            # Mark this stream most-recently active so an in-use stream is not the
            # next eviction target.
            self._streams.move_to_end(stream_id)
        ring.append((seq, event_id, payload))
        return event_id

    def replay_after(self, last_event_id: str) -> list[tuple[str, dict[str, Any]]]:
        """Return the `(event_id, payload)` events after `last_event_id`, in order.

        Replay is scoped to the stream the id names: the id encodes a
        `(stream, sequence)` pair, and every recorded event with a higher sequence
        on that stream is replayed. An id for an unknown stream (already evicted,
        or never recorded) yields an empty list rather than crossing into another
        stream's history, and a malformed id replays nothing.
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
        # Compare by sequence rather than identity so a resume from the priming id
        # (sequence 0) replays the whole tail, and an evicted middle id still
        # replays whatever remains rather than nothing.
        return [(event_id, payload) for seq, event_id, payload in ring if seq > after]
