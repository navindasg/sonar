"""AI overview: prompt build, JSON parse, deterministic render, graceful failure."""

from __future__ import annotations

import json

import httpx
import pytest

from notes import session as sess
from notes.summarize import (
    build_messages,
    configured_user_names,
    parse_action_items,
    parse_overview,
    render_overview,
    summarize,
    transcript_text,
    user_action_items,
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
    assert "- **Navin**" in md and "  - harden diarization" in md
    assert "- **Dad**" in md and "  - take a subscription" in md


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
    assert md.index("  - ship the PR") < md.index("  - update the docs")
    assert navin < md.index("- **Dana**")
    assert "### Decisions" in md and "- merge tomorrow" in md
    assert "### Open Questions" not in md          # empty sections are omitted


def test_render_leaves_the_scannable_checkbox_to_one_section() -> None:
    # Regression: the overview used to render every attendee's item as "- [ ]".
    # todo_list scans the whole vault for open checkboxes at any indent, so that
    # enrolled Dana's commitments in the user's to-do list AND listed the user's
    # own item twice (here, and dated in the note's "## My Action Items").
    md = render_overview(_OVERVIEW)
    assert "- [ ]" not in md
    assert "  - ship the PR" in md and "  - review it" in md


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


# --- reading the rendered overview back (the user's own action items) --------

_OVERVIEW = {
    "summary": ["shipped things"],
    "action_items": [
        {"person": "Navin", "item": "ship the PR"},
        {"person": "Dana", "item": "review it"},
        {"person": "Navin", "item": "update the docs"},
    ],
    "decisions": ["**merge** tomorrow"],
    "open_questions": ["when?"],
}


def test_parse_action_items_round_trips_the_render() -> None:
    # The note stores the RENDERED markdown (the overview dict is thrown away and
    # the user may edit it), so the user's items have to come back out of the text.
    assert parse_action_items(render_overview(_OVERVIEW)) == (
        ("Navin", "ship the PR"),
        ("Navin", "update the docs"),
        ("Dana", "review it"),
    )


def test_parse_action_items_ignores_every_other_section() -> None:
    # Bold text in Decisions must not read as a person, and Summary bullets must
    # not read as tasks — only the Action Items section counts.
    md = render_overview(_OVERVIEW)
    people = {person for person, _ in parse_action_items(md)}
    assert people == {"Navin", "Dana"}
    tasks = {task for _, task in parse_action_items(md)}
    assert "shipped things" not in tasks and "when?" not in tasks


def test_parse_action_items_tolerates_a_hand_edited_section() -> None:
    # The UI lets the user rewrite the overview markdown by hand: accept the
    # bullet/checkbox variants they are likely to type, and squeeze the task text
    # so it can still become a single well-formed checkbox.
    md = (
        "### Action Items\n\n"
        "* **Navin**\n"
        "  - [ ] call   the   vendor\n"
        "- [ ] orphan task with no owner\n"
        "\n### Decisions\n\n- **Dana** ships it\n"
    )
    assert parse_action_items(md) == (("Navin", "call the vendor"),)


def test_parse_action_items_drops_items_already_ticked_off() -> None:
    # A task the user ticked must never come back as an open checkbox.
    md = "### Action Items\n\n- **Navin**\n  - [x] done\n  - [X] also done\n  - [ ] open\n"
    assert parse_action_items(md) == (("Navin", "open"),)


def test_parse_action_items_handles_an_overview_with_no_items() -> None:
    assert parse_action_items(render_overview({"summary": ["x"], "action_items": []})) == ()
    assert parse_action_items("") == ()
    assert parse_action_items("_(AI overview unavailable: boom)_") == ()


def test_user_action_items_keeps_only_the_user() -> None:
    items = user_action_items(render_overview(_OVERVIEW), ["navin"])   # case-insensitive
    assert items == ("ship the PR", "update the docs")


def test_user_action_items_accepts_several_aliases_and_dedupes() -> None:
    md = render_overview({
        "summary": [],
        "action_items": [
            {"person": "Navin", "item": "book the flight"},
            {"person": "Me", "item": "Book the Flight"},   # same task, other label
            {"person": "Dad", "item": "renew the passport"},
        ],
    })
    assert user_action_items(md, ["Navin", "Me"]) == ("book the flight",)


def test_user_action_items_is_empty_when_nobody_is_configured() -> None:
    # No configured identity -> we must NOT guess which attendee is the user.
    md = render_overview(_OVERVIEW)
    assert user_action_items(md, []) == ()
    assert user_action_items(md, ["  ", ""]) == ()
    assert user_action_items(md, ["Someone Else"]) == ()


def test_user_action_items_never_claims_a_similarly_named_colleague() -> None:
    # The whole point of exact matching: a wrong to-do is worse than a missing
    # one. Prefix/substring matching would file "Navin's manager"'s commitment
    # as the user's, so a configured "Navin" must claim NEITHER direction.
    md = render_overview({
        "summary": [],
        "action_items": [
            {"person": "Navin's manager", "item": "approve the budget"},
            {"person": "Navinder", "item": "book the room"},
        ],
    })
    assert user_action_items(md, ["Navin"]) == ()
    assert user_action_items(md, ["Nav"]) == ()


def test_parse_action_items_honours_a_hand_ticked_plain_bullet() -> None:
    # render_overview now emits plain bullets, so ticking an item off in the
    # review UI means typing the "[x]" yourself — that must still exclude it.
    md = "### Action Items\n\n- **Navin**\n  - [x] already done\n  - still open\n"
    assert parse_action_items(md) == (("Navin", "still open"),)


def test_configured_user_names_reads_the_env_alias_list(monkeypatch) -> None:
    monkeypatch.setenv("SONAR_USER_NAME", " Navin , Navin Dasgupta ,, ")
    assert configured_user_names() == ("Navin", "Navin Dasgupta")
    monkeypatch.delenv("SONAR_USER_NAME")
    assert configured_user_names() == ()
