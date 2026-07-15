"""Notes-mode intent detection (pure, regex only).

Distinguishes the note-TAKER ("take notes", a live diarized session) from the
harness's note.capture ("note that milk is out", a one-line vault append). To
avoid mid-meeting false triggers, start/stop phrases must be COMMANDS: anchored
at the start of the utterance, with only a short lead-in ("hey", "sonar",
"can you", …) allowed before the verb.
"""
from __future__ import annotations

import re

# Optional polite/wake lead-in before the actual command.
_LEAD = r"(?:(?:hey|hi|ok|okay|so|sonar|please|now|can you|could you|would you|let'?s|go ahead and)[,\s]+){0,3}"

# "take notes", "start taking notes", "take some meeting notes", "start the
# note taker", "take notes on <topic>" … but NOT "note that …" (note.capture)
# and not a mid-sentence mention ("we should take notes next time").
_START = re.compile(
    rf"^\s*{_LEAD}"
    r"(?:(?:start|begin)\s+(?:taking\s+|the\s+)?|take\s+(?:some\s+)?)"
    r"(?:meeting\s+)?note[- ]?(?:s|taker|taking)\b"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)

_STOP = re.compile(
    rf"^\s*{_LEAD}"
    r"(?:stop|end|finish|wrap\s+up|close|done(?:\s+with)?)\s+"
    r"(?:taking\s+|the\s+)?(?:meeting\s+)?note[- ]?(?:s|taker|taking)\b",
    re.IGNORECASE,
)

# "take notes on/for/about <topic>" -> a title hint for the session.
_TITLE = re.compile(r"^\s*(?:on|for|about|of)\s+(?P<title>.+?)[.!?]?\s*$", re.IGNORECASE)


def wants_notes_start(text: str) -> bool:
    """True iff the utterance is a command to start a diarized notes session."""
    return bool(_START.match(text or ""))


def wants_notes_stop(text: str) -> bool:
    """True iff the utterance is a command to end the running notes session."""
    return bool(_STOP.match(text or ""))


def notes_title_hint(text: str) -> str | None:
    """Extract a session title from 'take notes on <topic>' phrasing, if any."""
    m = _START.match(text or "")
    if not m:
        return None
    t = _TITLE.match(m.group("rest") or "")
    if not t:
        return None
    title = t.group("title").strip()
    return title or None
