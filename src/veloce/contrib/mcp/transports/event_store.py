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

from collections import deque
from typing import Any

# Events retained per stream. A resumable POST stream produces a handful of
# notifications plus its final response; this caps the replay window so a
# never-reconnecting client cannot grow the buffer without bound.
_MAX_EVENTS_PER_STREAM = 256

# Separator between the stream id and the per-stream sequence number in an event
# id. URL-safe base64 stream ids never contain it, so a split on the last
# occurrence recovers the two parts unambiguously.
_EVENT_ID_SEP = "."


class SSEEventStore:
    """Record SSE events per stream and replay a stream's missed tail on resume."""

    __slots__ = ("_streams",)

    def __init__(self) -> None:
        # One bounded ring per stream id: each entry is (seq, event_id, payload).
        # The sequence is kept alongside the id so a resume compares by ordinal.
        self._streams: dict[str, deque[tuple[int, str, dict[str, Any]]]] = {}

    def record(self, stream_id: str, seq: int, payload: dict[str, Any]) -> str:
        """Store one outbound `payload` for `stream_id` and return its event id.

        The id is `"{stream_id}.{seq}"`, encoding the originating stream so a
        later `Last-Event-ID` resolves to the right ring.
        """
        event_id = f"{stream_id}{_EVENT_ID_SEP}{seq}"
        ring = self._streams.get(stream_id)
        if ring is None:
            ring = self._streams[stream_id] = deque(maxlen=_MAX_EVENTS_PER_STREAM)
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

    def discard(self, stream_id: str) -> None:
        """Drop a stream's history once it can no longer be resumed."""
        self._streams.pop(stream_id, None)
