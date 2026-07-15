"""Notes intent: command-anchored start/stop phrases, title hints, no false fires."""

from __future__ import annotations

import pytest

from notes.intent import notes_title_hint, wants_notes_start, wants_notes_stop


@pytest.mark.parametrize("text", [
    "take notes",
    "Take notes.",
    "hey take notes",
    "Hey, take some notes",
    "okay sonar take notes",
    "start taking notes",
    "let's take notes",
    "can you take meeting notes",
    "start the note taker",
    "begin note taking",
    "take notes on the budget review",
    "please take notes for project sonar",
])
def test_start_commands_fire(text: str) -> None:
    assert wants_notes_start(text)


@pytest.mark.parametrize("text", [
    "note that milk is out",                 # note.capture's job, not the note taker
    "take a note",                           # singular -> note.capture
    "I should take notes more often",        # mid-sentence mention, not a command
    "we should take notes next time",
    "did you take notes yesterday",
    "what notes do I have on sonar",
    "stop taking notes",                     # a STOP command is not a start
    "",
])
def test_start_does_not_false_fire(text: str) -> None:
    assert not wants_notes_start(text)


@pytest.mark.parametrize("text", [
    "stop taking notes",
    "Stop taking notes.",
    "okay stop the notes",
    "end notes",
    "end the note taker",
    "finish the notes",
    "wrap up the notes",
    "done taking notes",
])
def test_stop_commands_fire(text: str) -> None:
    assert wants_notes_stop(text)


@pytest.mark.parametrize("text", [
    "stop",
    "end the meeting",
    "stop talking",
    "he told me to stop taking notes once",  # mid-sentence mention, not a command
    "take notes",
    "",
])
def test_stop_does_not_false_fire(text: str) -> None:
    assert not wants_notes_stop(text)


def test_title_hint_extracted() -> None:
    assert notes_title_hint("take notes on the budget review.") == "the budget review"
    assert notes_title_hint("take notes for Project Sonar") == "Project Sonar"
    assert notes_title_hint("hey take notes about hiring") == "hiring"


def test_title_hint_absent() -> None:
    assert notes_title_hint("take notes") is None
    assert notes_title_hint("this is not a command") is None
