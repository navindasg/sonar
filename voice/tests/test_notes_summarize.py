"""AI overview: prompt build, JSON parse, deterministic render, graceful failure."""

from __future__ import annotations

import json

import httpx
import pytest

from notes import session as sess
from notes.summarize import (
    build_messages,
    parse_overview,
    render_overview,
    summarize,
    transcript_text,
)


def _state() -> sess.SessionState:
    s = sess.SessionState(title="Standup", started_at="2026-07-15T10:00:00")
    s = sess.add_segment(s, "S1", "I'll ship the PR today", 0.0, 2.0)
    s = sess.add_segment(s, "S2", "and I'll review it", 3.0, 4.5)
    s = sess.rename_speaker(s, "S1", "Navin")
    return s


def test_transcript_uses_display_names() -> None:
    text = transcript_text(_state())
    assert text.splitlines() == [
        "Navin: I'll ship the PR today",
        "Speaker 2: and I'll review it",
    ]


def test_messages_carry_title_and_transcript() -> None:
    msgs = build_messages(_state())
    assert msgs[0]["role"] == "system"
    assert "Meeting: Standup" in msgs[1]["content"]
    assert "Navin: I'll ship the PR today" in msgs[1]["content"]


def test_parse_rejects_non_schema_replies() -> None:
    assert parse_overview("not json") is None
    assert parse_overview(json.dumps(["a", "list"])) is None
    assert parse_overview(json.dumps({"summary": "not a list"})) is None
    ok = parse_overview(json.dumps({"summary": ["x"]}))
    assert ok == {"summary": ["x"]}


def test_parse_unwraps_a_fenced_json_reply() -> None:
    # gemma often ignores the constrained-decoding format and wraps its JSON in a
    # ```json fence (+ a line of prose), which used to leak verbatim into the note.
    raw = 'Here is the summary:\n```json\n{"summary": ["did things"]}\n```'
    assert parse_overview(raw) == {"summary": ["did things"]}


def test_render_tolerates_task_person_key_variants() -> None:
    # The model isn't consistent about the action-item key: "task"/"action" are
    # accepted alongside our schema's "item" so the item isn't silently dropped.
    md = render_overview({
        "summary": ["x"],
        "action_items": [
            {"task": "harden diarization", "person": "Navin"},
            {"action": "take a subscription", "owner": "Dad"},
        ],
    })
    assert "- **Navin**" in md and "  - [ ] harden diarization" in md
    assert "- **Dad**" in md and "  - [ ] take a subscription" in md


def test_render_groups_action_items_by_person() -> None:
    md = render_overview({
        "summary": ["shipped things"],
        "action_items": [
            {"person": "Navin", "item": "ship the PR"},
            {"person": "Dana", "item": "review it"},
            {"person": "Navin", "item": "update the docs"},
        ],
        "decisions": ["merge tomorrow"],
        "open_questions": [],
    })
    navin = md.index("- **Navin**")
    assert md.index("  - [ ] ship the PR") < md.index("  - [ ] update the docs")
    assert navin < md.index("- **Dana**")
    assert "### Decisions" in md and "- merge tomorrow" in md
    assert "### Open Questions" not in md          # empty sections are omitted


def test_render_handles_empty_overview() -> None:
    md = render_overview({"summary": [], "action_items": []})
    assert "- (empty)" in md and "- (none)" in md


async def test_summarize_happy_path() -> None:
    payload = {
        "summary": ["PR ships today"],
        "action_items": [{"person": "Navin", "item": "ship the PR"}],
        "decisions": [],
        "open_questions": ["when is the release?"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "test-model"
        assert body["format"]["type"] == "object"   # constrained decoding is on
        return httpx.Response(200, json={"message": {"content": json.dumps(payload)}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ollama"
    ) as client:
        md = await summarize(client, _state(), model="test-model")
    assert "- PR ships today" in md
    assert "- **Navin**" in md
    assert "- when is the release?" in md


async def test_summarize_falls_back_to_raw_text_on_bad_json() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"message": {"content": "plain prose summary"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ollama"
    ) as client:
        md = await summarize(client, _state())
    assert md == "plain prose summary"


async def test_summarize_never_raises_when_ollama_is_down() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ollama"
    ) as client:
        md = await summarize(client, _state())
    assert md.startswith("_(AI overview unavailable")


async def test_summarize_empty_transcript_short_circuits() -> None:
    state = sess.SessionState(title="x", started_at="t")
    md = await summarize(None, state)              # no client call at all
    assert md == "_(nothing was said)_"
