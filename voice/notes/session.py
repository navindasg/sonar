"""Notes session state — frozen dataclasses + pure transition functions.

The controller holds ONE `SessionState` and replaces it wholesale through these
functions (never mutates), so every transition is unit-testable and the server
can serialize any snapshot for the UI. `rev` increments on every change; the
browser uses it to skip stale re-renders while the user is mid-edit.

Client edit ops arrive as untrusted JSON: every transition validates its inputs
and returns the state UNCHANGED on garbage (unknown segment id, non-string
text, malformed speaker id) — a hostile/buggy page can never corrupt a session.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace

# Session lifecycle: recording -> summarizing -> review -> saved. `discarded`
# is terminal from any post-recording state; `review` re-entered after edits.
RECORDING = "recording"
SUMMARIZING = "summarizing"
REVIEW = "review"
SAVED = "saved"
DISCARDED = "discarded"

_SPEAKER_ID = re.compile(r"^S[1-9]\d*$")
_MAX_TEXT = 20_000
_MAX_TITLE = 200
_MAX_NAME = 80


@dataclass(frozen=True)
class Segment:
    """One diarized, endpointed utterance."""

    id: int
    speaker: str        # "S1", "S2", …
    text: str
    t0: float           # seconds from session start
    t1: float


@dataclass(frozen=True)
class SessionState:
    """Everything the UI shows, in one immutable snapshot."""

    title: str
    started_at: str                       # local ISO, display only
    status: str = RECORDING
    segments: tuple[Segment, ...] = ()
    names: tuple[tuple[str, str], ...] = ()   # (speaker id, display name)
    summary_md: str = ""
    saved_path: str = ""                  # vault-relative once saved
    rev: int = 0
    next_seg_id: int = 0                   # monotonic; segment ids never reused
    diarization_degraded: bool = False    # embedder unavailable -> one speaker


def default_name(speaker: str) -> str:
    """"S3" -> "Speaker 3" (used until the user labels the person)."""
    return f"Speaker {speaker[1:]}" if _SPEAKER_ID.match(speaker) else speaker


def display_name(state: SessionState, speaker: str) -> str:
    """The user's label for a speaker, or the default."""
    return dict(state.names).get(speaker) or default_name(speaker)


def _bump(state: SessionState, **changes) -> SessionState:
    return replace(state, rev=state.rev + 1, **changes)


def _ensure_speaker(state: SessionState, speaker: str) -> SessionState:
    if any(sid == speaker for sid, _ in state.names):
        return state
    return replace(state, names=state.names + ((speaker, default_name(speaker)),))


def add_segment(
    state: SessionState, speaker: str, text: str, t0: float, t1: float
) -> SessionState:
    """Append one finalized utterance (registers the speaker on first sight)."""
    if not _SPEAKER_ID.match(speaker) or not text.strip():
        return state
    state = _ensure_speaker(state, speaker)
    # Ids come from a monotonic counter, not len(segments): after a delete the
    # latter would hand a live id to the next segment, so every id-keyed op and
    # the UI's row keys would then hit two rows at once.
    seg = Segment(id=state.next_seg_id, speaker=speaker,
                  text=text.strip()[:_MAX_TEXT], t0=round(t0, 2), t1=round(t1, 2))
    return _bump(state, segments=state.segments + (seg,),
                 next_seg_id=state.next_seg_id + 1)


def edit_segment_text(state: SessionState, seg_id: object, text: object) -> SessionState:
    """User edited a line. Emptied text keeps the segment (delete is explicit)."""
    if not isinstance(seg_id, int) or not isinstance(text, str):
        return state
    if not any(s.id == seg_id for s in state.segments):
        return state
    segs = tuple(
        replace(s, text=text.strip()[:_MAX_TEXT]) if s.id == seg_id else s
        for s in state.segments
    )
    return _bump(state, segments=segs)


def delete_segment(state: SessionState, seg_id: object) -> SessionState:
    """Drop a line entirely (mis-fire, throat-clear, crosstalk)."""
    if not isinstance(seg_id, int) or not any(s.id == seg_id for s in state.segments):
        return state
    return _bump(state, segments=tuple(s for s in state.segments if s.id != seg_id))


def reassign_segment(state: SessionState, seg_id: object, speaker: object) -> SessionState:
    """Move a line to another speaker (must already exist or be added first)."""
    if not isinstance(seg_id, int) or not isinstance(speaker, str):
        return state
    if not _SPEAKER_ID.match(speaker) or not any(s.id == seg_id for s in state.segments):
        return state
    state = _ensure_speaker(state, speaker)
    segs = tuple(
        replace(s, speaker=speaker) if s.id == seg_id else s for s in state.segments
    )
    return _bump(state, segments=segs)


def add_speaker(state: SessionState) -> SessionState:
    """Manually add the next free speaker slot (for reassigning missed voices)."""
    taken = {sid for sid, _ in state.names}
    n = 1
    while f"S{n}" in taken:
        n += 1
    return _bump(_ensure_speaker(state, f"S{n}"))


def rename_speaker(state: SessionState, speaker: object, name: object) -> SessionState:
    """Label a speaker ("S1" -> "Navin"); applies everywhere they appear."""
    if not isinstance(speaker, str) or not isinstance(name, str):
        return state
    if not any(sid == speaker for sid, _ in state.names):
        return state
    clean = name.strip()[:_MAX_NAME] or default_name(speaker)
    names = tuple((sid, clean if sid == speaker else n) for sid, n in state.names)
    return _bump(state, names=names)


def set_title(state: SessionState, title: object) -> SessionState:
    if not isinstance(title, str) or not title.strip():
        return state
    return _bump(state, title=title.strip()[:_MAX_TITLE])


def set_status(state: SessionState, status: str) -> SessionState:
    if status not in (RECORDING, SUMMARIZING, REVIEW, SAVED, DISCARDED):
        return state
    return _bump(state, status=status)


def set_summary(state: SessionState, markdown: object) -> SessionState:
    if not isinstance(markdown, str):
        return state
    return _bump(state, summary_md=markdown)


def mark_saved(state: SessionState, rel_path: str) -> SessionState:
    return _bump(state, status=SAVED, saved_path=rel_path)


def set_diarization_degraded(state: SessionState, degraded: bool) -> SessionState:
    """Flag that speaker diarization is unavailable (the embedder failed to load),
    so the UI can warn that everyone lands under one speaker. Idempotent."""
    if not isinstance(degraded, bool) or state.diarization_degraded == degraded:
        return state
    return _bump(state, diarization_degraded=degraded)


def to_json(state: SessionState, elapsed_s: float = 0.0) -> dict:
    """One full-state message for the UI (server stamps live elapsed time)."""
    return {
        "type": "state",
        "rev": state.rev,
        "title": state.title,
        "started_at": state.started_at,
        "status": state.status,
        "elapsed_s": round(elapsed_s, 1),
        "speakers": [{"id": sid, "name": n} for sid, n in state.names],
        "segments": [
            {"id": s.id, "speaker": s.speaker, "text": s.text, "t0": s.t0, "t1": s.t1}
            for s in state.segments
        ],
        "summary": state.summary_md,
        "saved_path": state.saved_path,
        "diarization_degraded": state.diarization_degraded,
    }
