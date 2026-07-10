"""OutputPlayer buffer controls — the flush that makes an F5 cutoff actually
silence the reply, plus gain clamping and RMS. No hardware: only start() touches
sounddevice, and these never call it."""

from __future__ import annotations

from audio_io import OutputPlayer, rms_pcm16


def test_write_then_flush_empties_the_buffer() -> None:
    p = OutputPlayer()
    p.write(b"\x01\x02" * 100)
    assert p.pending_bytes() == 200
    p.flush()                       # cutoff: queued audio dropped immediately
    assert p.pending_bytes() == 0


def test_write_ignores_empty_bytes() -> None:
    p = OutputPlayer()
    p.write(b"")
    assert p.pending_bytes() == 0


def test_set_gain_clamps_to_unit_range() -> None:
    p = OutputPlayer()
    p.set_gain(2.0)
    assert p.gain == 1.0
    p.set_gain(-1.0)
    assert p.gain == 0.0
    p.set_gain(0.35)
    assert abs(p.gain - 0.35) < 1e-9


def test_rms_pcm16_zero_for_silence_positive_for_signal() -> None:
    assert rms_pcm16(b"") == 0.0
    assert rms_pcm16(b"\x00\x00" * 50) == 0.0
    assert rms_pcm16(b"\xff\x7f" * 50) > 0.0   # near full-scale int16
