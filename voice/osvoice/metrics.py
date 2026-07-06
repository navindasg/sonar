"""Per-turn latency instrumentation (pure stdlib).

A single conversational turn flows through several stages: speech ends (VAD),
the STT final transcript lands, the LLM emits its first token, TTS produces its
first frame, and the first audio reaches the speaker. `TurnTimer` records a raw
`time.perf_counter()` mark per stage during the turn, then freezes them into an
immutable `TurnMetrics` whose properties expose the deltas we actually care
about (TTFT, time-to-first-audio, total). Enabled only when `--metrics` is on;
output goes through `logging`, never `print`.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger("osvoice.metrics")

# Canonical stage names. Marks are raw perf_counter() seconds; `turn_start` is
# the reference origin for the headline deltas (everything else is relative).
TURN_START = "turn_start"
VAD_END = "vad_end"
STT_FINAL = "stt_final"
LLM_FIRST_TOKEN = "llm_first_token"
TTS_FIRST_FRAME = "tts_first_frame"
FIRST_AUDIO_OUT = "first_audio_out"


def _delta_ms(marks: dict[str, float], start: str, end: str) -> float | None:
    """Milliseconds between two recorded marks, or None if either is missing."""
    a = marks.get(start)
    b = marks.get(end)
    if a is None or b is None:
        return None
    return (b - a) * 1_000.0


@dataclass(frozen=True)
class TurnMetrics:
    """Immutable snapshot of one turn's stage marks (raw perf_counter seconds).

    Properties derive the deltas of interest in milliseconds, returning None
    when a contributing mark was never recorded (e.g. a turn cut short).
    """

    marks: dict[str, float]

    @property
    def _speech_end(self) -> str:
        """The end-of-user-speech origin mark.

        Prefers an explicit ``vad_end`` (set once server-side VAD endpointing is
        wired in); falls back to ``turn_start``, which today coincides with the
        end of user speech because a turn begins when the client signals it.
        """
        return VAD_END if VAD_END in self.marks else TURN_START

    @property
    def ttft(self) -> float | None:
        """Time to first LLM token, measured from the end of user speech."""
        return _delta_ms(self.marks, self._speech_end, LLM_FIRST_TOKEN)

    @property
    def time_to_first_audio(self) -> float | None:
        """Voice-to-voice latency: end of user speech -> first audio out."""
        return _delta_ms(self.marks, self._speech_end, FIRST_AUDIO_OUT)

    @property
    def stt_latency(self) -> float | None:
        """End of user speech -> final STT transcript."""
        return _delta_ms(self.marks, self._speech_end, STT_FINAL)

    @property
    def tts_first_frame(self) -> float | None:
        """First LLM token -> first TTS frame produced."""
        return _delta_ms(self.marks, LLM_FIRST_TOKEN, TTS_FIRST_FRAME)

    @property
    def total(self) -> float | None:
        """Whole turn: start of capture -> first audio out."""
        return _delta_ms(self.marks, TURN_START, FIRST_AUDIO_OUT)


class TurnTimer:
    """Accumulates stage marks for one turn, then freezes them into TurnMetrics.

    The first `mark()` (typically `turn_start`) establishes the turn origin;
    repeated marks for the same stage keep the earliest (first-seen) timestamp,
    matching the "first token"/"first frame" semantics of the deltas.
    """

    def __init__(self) -> None:
        self._marks: dict[str, float] = {}

    def mark(self, stage: str) -> None:
        """Record perf_counter() for `stage` once (first occurrence wins)."""
        if stage not in self._marks:
            self._marks[stage] = time.perf_counter()

    def summary(self) -> TurnMetrics:
        """Freeze the accumulated marks into an immutable TurnMetrics snapshot."""
        return TurnMetrics(marks=dict(self._marks))

    def log_summary(self, log: logging.Logger | None = None) -> TurnMetrics:
        """Emit one structured line with the key deltas (ms) and return the snapshot."""
        metrics = self.summary()
        (log or logger).info(
            "turn metrics",
            extra={
                "stt_ms": metrics.stt_latency,
                "ttft_ms": metrics.ttft,
                "tts_first_frame_ms": metrics.tts_first_frame,
                "time_to_first_audio_ms": metrics.time_to_first_audio,
                "total_ms": metrics.total,
            },
        )
        return metrics
