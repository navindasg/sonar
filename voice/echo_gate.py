"""Barge-in decision logic for the full-duplex voice loop (pure, testable).

While Sonar is *speaking* its reply, the mic stays hot so you can cut it off
mid-sentence. The catch: on a laptop the mic also hears Kokoro's own voice
through the speakers, so naive VAD would treat that echo as "the user talking"
and the assistant would interrupt itself. This module decides — per audio frame,
from three cheap scalars — whether the mic energy is *really* the user talking
over the reply, or just the reply leaking back in.

The transport owns the audio; this owns only the decision, so it is pure and
unit-testable with no mlx/sounddevice/torch. The heuristic (no acoustic echo
cancellation needed):

  * A frame is *suspect* when voiced (Silero prob high) AND its mic RMS clears a
    threshold set **relative to what we are currently playing** — echo scales
    with the TTS output level, real barge-in speech clears it.
  * ``DUCK_AFTER`` suspect frames in a row → tell the caller to *duck* (lower the
    TTS gain). Ducking drops the echo floor, so if it really was the user the
    next frames read even cleaner — self-reinforcing confirmation.
  * ``TRIGGER_FRAMES`` suspect frames in a row → confirmed barge-in.

Any non-suspect frame resets the run, so a single echo transient never trips it.
Native macOS Voice-Processing AEC is the robustness follow-up if this proves
leaky on specific hardware; the gate's inputs stay the same either way.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# Defaults tuned for MacBook built-in mic+speakers at conversational distance.
# All overridable at construction so they can be calibrated per machine.
_DEFAULT_FLOOR: Final = 0.06        # absolute mic-RMS gate even in silence (0..1)
_DEFAULT_MARGIN: Final = 2.2        # barge-in must exceed this * current TTS RMS
_DEFAULT_VAD_ON: Final = 0.6        # Silero speech-probability threshold
_DEFAULT_DUCK_AFTER: Final = 2      # suspect frames before ducking TTS (~64 ms)
_DEFAULT_TRIGGER_FRAMES: Final = 5  # suspect frames before confirming (~160 ms)


@dataclass(frozen=True)
class GateDecision:
    """Per-frame verdict. ``duck`` lowers TTS gain; ``barge_in`` cancels the reply."""

    duck: bool = False
    barge_in: bool = False


class EchoGate:
    """Stateful per-frame barge-in detector, active only while TTS is playing.

    Feed one :meth:`observe` per audio frame during playback; :meth:`reset` at
    the start/end of every spoken reply. Frame cadence is the mic frame size
    (512 samples @16 kHz ≈ 32 ms), which sets the meaning of the frame-count
    thresholds.
    """

    def __init__(
        self,
        floor: float = _DEFAULT_FLOOR,
        margin: float = _DEFAULT_MARGIN,
        vad_on: float = _DEFAULT_VAD_ON,
        duck_after: int = _DEFAULT_DUCK_AFTER,
        trigger_frames: int = _DEFAULT_TRIGGER_FRAMES,
    ) -> None:
        if trigger_frames < 1:
            raise ValueError(f"trigger_frames must be >= 1, got {trigger_frames}")
        if duck_after < 1 or duck_after > trigger_frames:
            raise ValueError(
                f"duck_after must be in [1, trigger_frames]={trigger_frames}, "
                f"got {duck_after}"
            )
        self._floor = floor
        self._margin = margin
        self._vad_on = vad_on
        self._duck_after = duck_after
        self._trigger_frames = trigger_frames
        self._suspect = 0

    def reset(self) -> None:
        """Clear the suspect-run counter (call when playback starts and ends)."""
        self._suspect = 0

    def observe(self, vad_prob: float, mic_rms: float, tts_rms: float) -> GateDecision:
        """Fold one frame into the detector and return the current verdict.

        Args:
            vad_prob: Silero speech probability for this mic frame (0..1).
            mic_rms:  RMS energy of this mic frame (0..1).
            tts_rms:  RMS energy of the TTS frame currently on the speaker (0..1).
        """
        threshold = max(self._floor, tts_rms * self._margin)
        if vad_prob >= self._vad_on and mic_rms >= threshold:
            self._suspect += 1
        else:
            self._suspect = 0
        return GateDecision(
            duck=self._suspect >= self._duck_after,
            barge_in=self._suspect >= self._trigger_frames,
        )
