"""Sonar Notes — diarized meeting note taker.

F5 -> "take notes" -> a browser UI opens with a live, speaker-diarized
transcript; "stop taking notes" (or the End button) turns the session into an
AI overview (summary + per-person action items) plus the full transcript, all
editable, saved into the vault under Sonar/Notes/.

Module split mirrors the rest of voice/: pure logic (intent, diarize, session,
store, summarize rendering) unit-tests anywhere; the heavy speaker-embedding
backend (speechbrain ECAPA) stays behind a lazy adapter in embed.py; server.py
and controller.py are the IO glue driven by voice_loop.py.
"""
