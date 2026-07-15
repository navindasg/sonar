"""Notes-mode intent detection (pure, regex only).

Distinguishes the note-TAKER ("take notes", a live diarized session) from the
harness's note.capture ("note that milk is out", a one-line vault append). To
avoid mid-meeting false triggers, start/stop phrases must be COMMANDS: anchored
at the start of the utterance, with only a short lead-in ("hey", "sonar",
"can you", …) allowed before the verb — and, just as important, anchored at the
END so a sentence that merely mentions notes ("so let's wrap up the notes and
grab lunch") does not steer the session.
"""
from __future__ import annotations

import re

# Optional polite/wake lead-in before the actual command. Broad on purpose:
# acknowledgments ("alright", "cool", "great", …) routinely precede a real
# command, so a stingy lead-in just drops genuine ones. Safety comes from the
# whole-utterance anchoring below, not from keeping this list short.
_LEAD = (
    r"(?:(?:hey\s+there|hey|hi|ok|okay|so|sonar|please|now|"
    r"alright|alrighty|great|cool|nice|well|yeah|yep|yes|right|sure|"
    r"can\s+you|could\s+you|would\s+you|let'?s|go\s+ahead\s+and)[,\s]+){0,3}"
)

# Polite filler that may trail a spoken command without changing its meaning
# ("…please", "…everyone", "…for today") plus closing punctuation. Reused by the
# START and STOP tails to demand the command BE the whole utterance: content
# that isn't filler (e.g. "…and grab lunch", "…offline") fails the trailing $.
_FILLER = (
    r"(?:[,\s]+(?:please|now|everyone|everybody|folks|guys|team|today|thanks|"
    r"thank\s+you|for\s+today|for\s+now|for\s+the\s+day))*"
    r"[\s,.!?]*"
)

# "take notes", "start taking notes", "take some meeting notes", "start the
# note taker", "take notes on <topic>" … but NOT "note that …" (note.capture),
# not a mid-sentence mention ("we should take notes next time"), and not an
# aside that runs on into other content ("take notes offline" = discuss later;
# "take notes of that" = pay attention). The tail must be EITHER a title hint
# ("on/for/about <topic>") or a bare, politely-closed imperative.
_START = re.compile(
    rf"^\s*{_LEAD}"
    r"(?:(?:start|begin)\s+(?:taking\s+|the\s+)?|take\s+(?:some\s+)?)"
    r"(?:meeting\s+)?note[- ]?(?:s|taker|taking)\b"
    r"(?P<rest>"
    r"[\s,]+(?:on|for|about)\s+\S.*"   # title hint -> notes_title_hint
    rf"|{_FILLER}"                     # or a bare command + optional filler
    r")$",
    re.IGNORECASE,
)

# Symmetric to _START: a stop verb aimed at the session, anchored to the whole
# utterance so "stop the notes everyone" ends it but "so let's wrap up the notes
# and grab lunch" (runs on into other content) does not.
_STOP = re.compile(
    rf"^\s*{_LEAD}"
    r"(?:stop|end|finish|wrap\s+up|close|done(?:\s+with)?)\s+"
    r"(?:taking\s+|the\s+)?(?:meeting\s+)?note[- ]?(?:s|taker|taking)\b"
    rf"{_FILLER}$",
    re.IGNORECASE,
)

# "take notes on/for/about <topic>" -> a title hint for the session. ("of" is
# intentionally excluded, mirroring _START: "take notes of that" is idiomatic.)
_TITLE = re.compile(r"^\s*(?:on|for|about)\s+(?P<title>.+?)[.!?]?\s*$", re.IGNORECASE)


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
