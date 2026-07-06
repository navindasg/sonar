"""Transport-layer regression tests for barge-in audio flushing.

The merged outbound queue carries both JSON control events and binary PCM frames.
On barge-in, `pipeline.barge_in()` drains the pipeline's own queue, but frames may
already have been pumped onto the connection's merged queue ahead of the
``interrupted`` event. `_flush_merged_audio()` must drop those stale audio frames
while preserving control events in order, so the interrupt reaches the client
before any residual speech. Pure queue logic — no real WebSocket needed.
"""
from __future__ import annotations

from osvoice.transport_ws import _Connection


def _make_connection() -> _Connection:
    """Build a _Connection without a live socket or real pipeline.

    Only the merged queue is exercised here; the ws and pipeline factory are
    stubs (the factory just needs to be callable and return any object).
    """
    return _Connection(ws=object(), make_pipeline=lambda emit: object())


def test_flush_merged_audio_drops_bytes_keeps_control_in_order() -> None:
    # Arrange: interleave audio frames with control events, ending with interrupt.
    conn = _make_connection()
    conn._merged.put_nowait(b"stale-audio-1")
    conn._merged.put_nowait({"type": "speaking_start", "text": ""})
    conn._merged.put_nowait(b"stale-audio-2")
    conn._merged.put_nowait({"type": "interrupted", "text": ""})

    # Act
    dropped = conn._flush_merged_audio()

    # Assert: both audio frames dropped; control events retained in FIFO order.
    assert dropped == 2
    remaining = []
    while not conn._merged.empty():
        remaining.append(conn._merged.get_nowait())
    assert remaining == [
        {"type": "speaking_start", "text": ""},
        {"type": "interrupted", "text": ""},
    ]


def test_flush_merged_audio_empty_queue_is_noop() -> None:
    # Arrange
    conn = _make_connection()

    # Act / Assert: nothing to drop, queue stays empty.
    assert conn._flush_merged_audio() == 0
    assert conn._merged.empty()


def test_flush_merged_audio_only_control_events_untouched() -> None:
    # Arrange: control-only queue must be preserved exactly.
    conn = _make_connection()
    events = [{"type": "partial", "text": "he"}, {"type": "final", "text": "hello"}]
    for event in events:
        conn._merged.put_nowait(event)

    # Act
    dropped = conn._flush_merged_audio()

    # Assert
    assert dropped == 0
    remaining = [conn._merged.get_nowait() for _ in range(len(events))]
    assert remaining == events
