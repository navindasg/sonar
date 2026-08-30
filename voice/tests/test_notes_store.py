"""Vault save: rendered markdown, slug safety, collisions, atomic re-save."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from notes import session as sess
from notes.store import pick_path, render_note, save_note, slug_for

NOW = datetime(2026, 7, 15, 14, 30)


def _state(title: str = "Budget Review") -> sess.SessionState:
    s = sess.SessionState(title=title, started_at="2026-07-15T14:00:00")
    s = sess.add_segment(s, "S1", "let's start with the numbers", 0.0, 2.5)
    s = sess.add_segment(s, "S2", "revenue is up eight percent", 65.0, 68.0)
    s = sess.rename_speaker(s, "S1", "Navin")
    s = sess.set_summary(s, "### Summary\n\n- numbers reviewed")
    return s


# Exactly what render_overview produces: plain bullets, no checkboxes — the only
# scannable "- [ ]" in a saved note comes from the "## My Action Items" section.
_OVERVIEW_MD = (
    "### Summary\n\n- numbers reviewed\n\n"
    "### Action Items\n\n"
    "- **Navin**\n  - ship the PR\n  - update the docs\n"
    "- **Dana**\n  - review it\n"
)

# todo_list's open-checkbox regex, mirrored so these tests measure what the
# harness's vault scan will actually pick up out of a saved note.
_OPEN_CHECKBOX = re.compile(r"^\s*[-*+]\s+\[ \]\s+(?P<text>\S.*?)\s*$")


def _open_checkboxes(md: str) -> list[str]:
    """Every open to-do checkbox in a note, as todo_list would see it."""
    return [m.group("text") for m in map(_OPEN_CHECKBOX.match, md.splitlines()) if m]


def _state_with_actions() -> sess.SessionState:
    return sess.set_summary(_state(), _OVERVIEW_MD)


def _section(md: str, heading: str) -> str:
    """The text under ``heading`` up to the next same-level heading."""
    body = md.split(heading, 1)[1] if heading in md else ""
    return body.split("\n## ", 1)[0]


def test_render_has_overview_then_transcript() -> None:
    md = render_note(_state(), NOW)
    assert md.index("## AI Overview") < md.index("- numbers reviewed") < md.index("## Transcript")
    assert "**Navin** (00:00): let's start with the numbers" in md
    assert "**Speaker 2** (01:05): revenue is up eight percent" in md
    # Names are emitted as YAML-safe (JSON) scalars now — see the injection test.
    assert 'speakers: ["Navin", "Speaker 2"]' in md
    assert "# Budget Review" in md


def test_render_without_summary_marks_it() -> None:
    s = sess.set_summary(_state(), "")
    assert "_(no AI overview)_" in render_note(s, NOW)


@pytest.mark.parametrize("title,expect", [
    ("../../etc/passwd", "etc passwd"),          # separators and dots stripped
    ("Budget: Q3 review!", "Budget Q3 review"),
    ("   ", "FALLBACK"),
    ("", "FALLBACK"),
])
def test_slug_never_escapes(title: str, expect: str) -> None:
    assert slug_for(title, fallback="FALLBACK") == expect


def test_save_writes_under_sonar_notes(tmp_path: Path) -> None:
    target = save_note(_state(), tmp_path, NOW)
    assert target == tmp_path / "Sonar" / "Notes" / "Budget Review.md"
    text = target.read_text(encoding="utf-8")
    assert text.startswith("---\ncreated: 2026-07-15 14:30")
    assert not list(target.parent.glob("*.tmp"))  # atomic swap left no temp file


def test_collision_gets_a_numbered_suffix(tmp_path: Path) -> None:
    save_note(_state(), tmp_path, NOW)
    second = pick_path(tmp_path, _state(), NOW)
    assert second.name == "Budget Review-2.md"


def test_resave_overwrites_the_same_file(tmp_path: Path) -> None:
    first = save_note(_state(), tmp_path, NOW)
    edited = sess.set_summary(_state(), "### Summary\n\n- EDITED")
    again = save_note(edited, tmp_path, NOW, path=first)
    assert again == first
    assert "- EDITED" in first.read_text(encoding="utf-8")
    assert len(list(first.parent.iterdir())) == 1   # no duplicate note


def test_missing_vault_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        save_note(_state(), tmp_path / "nope", NOW)


# --- the user's own action items become real to-do checkboxes ----------------


def test_my_action_items_are_dated_checkboxes_for_the_user_only() -> None:
    md = render_note(_state_with_actions(), NOW, user_names=["navin"])
    mine = _section(md, "## My Action Items")
    # Dated, because todo_list's today/overdue filters (the ones daily.brief uses)
    # skip undated tasks — an unstamped item could never reach the brief.
    assert "- [ ] ship the PR 📅 2026-07-15" in mine
    assert "- [ ] update the docs 📅 2026-07-15" in mine
    assert "review it" not in mine          # Dana's commitment is not the user's


def test_my_action_items_sit_between_the_overview_and_the_transcript() -> None:
    md = render_note(_state_with_actions(), NOW, user_names=["Navin"])
    assert md.index("## AI Overview") < md.index("## My Action Items") < md.index("## Transcript")


def test_nothing_in_the_note_is_lost_when_the_section_is_added() -> None:
    plain = render_note(_state_with_actions(), NOW, user_names=[])
    md = render_note(_state_with_actions(), NOW, user_names=["Navin"])
    for kept in (
        'speakers: ["Navin", "Speaker 2"]',
        "# Budget Review",
        "- **Dana**",
        "  - review it",
        "**Navin** (00:00): let's start with the numbers",
    ):
        assert kept in md, kept
    # ...and the only difference is the new section.
    assert "## My Action Items" not in plain


def test_no_section_when_the_user_is_not_identified() -> None:
    # Guessing which attendee is the user would put other people's commitments on
    # the user's to-do list, so an unconfigured Sonar writes the note as before.
    assert "## My Action Items" not in render_note(_state_with_actions(), NOW, user_names=[])


def test_user_name_comes_from_the_environment_by_default(monkeypatch) -> None:
    monkeypatch.setenv("SONAR_USER_NAME", "Navin")
    assert "- [ ] ship the PR 📅 2026-07-15" in render_note(_state_with_actions(), NOW)
    monkeypatch.delenv("SONAR_USER_NAME")
    assert "## My Action Items" not in render_note(_state_with_actions(), NOW)


def test_a_task_that_already_has_a_due_date_is_not_stamped_twice() -> None:
    state = sess.set_summary(
        _state(), "### Action Items\n\n- **Navin**\n  - [ ] file taxes 📅 2026-04-15\n"
    )
    mine = _section(render_note(state, NOW, user_names=["Navin"]), "## My Action Items")
    assert mine.strip() == "- [ ] file taxes 📅 2026-04-15"


def test_a_multiline_task_cannot_break_out_of_its_checkbox() -> None:
    # The task text is model output; a stray newline must not spawn a second
    # checkbox or a heading in the saved note.
    state = sess.set_summary(
        _state(), "### Action Items\n\n- **Navin**\n  - [ ] do the thing\n## Fake Heading\n"
    )
    mine = _section(render_note(state, NOW, user_names=["Navin"]), "## My Action Items")
    assert mine.strip() == "- [ ] do the thing 📅 2026-07-15"


def test_the_note_yields_exactly_one_checkbox_per_commitment_of_the_users() -> None:
    # This is the property the whole feature rests on, measured with todo_list's
    # own regex: a vault scan must see each of the user's commitments ONCE, dated,
    # and must not see anyone else's. It used to see "ship the PR" twice (dated
    # here, undated in the overview) plus Dana's "review it".
    md = render_note(_state_with_actions(), NOW, user_names=["Navin"])
    assert _open_checkboxes(md) == [
        "ship the PR 📅 2026-07-15",
        "update the docs 📅 2026-07-15",
    ]


def test_an_unidentified_user_leaves_no_checkboxes_at_all_in_the_note() -> None:
    # Without a configured identity we must not fall back to enrolling every
    # attendee's commitment in the user's vault-wide to-do list.
    assert _open_checkboxes(render_note(_state_with_actions(), NOW, user_names=[])) == []


def test_resaving_a_session_never_duplicates_checkboxes(tmp_path: Path) -> None:
    first = save_note(_state_with_actions(), tmp_path, NOW, user_names=["Navin"])
    save_note(_state_with_actions(), tmp_path, NOW, path=first, user_names=["Navin"])
    text = first.read_text(encoding="utf-8")
    assert text.count("- [ ] ship the PR 📅 2026-07-15") == 1
    assert text.count("## My Action Items") == 1


# --- regression: #11 hostile speaker names must not inject YAML frontmatter ---

def _frontmatter(md: str) -> str:
    """The text between the first two '---' fences (the YAML frontmatter block)."""
    parts = md.split("---")
    assert len(parts) >= 3, "note is missing its frontmatter fences"
    return parts[1]


@pytest.mark.parametrize("evil_name", [
    "Navin\ninjected: true",          # newline would break out to a top-level key
    "Navin: boss",                    # ':' would turn the entry into a mapping
    "[malformed] {flow}",             # YAML flow indicators
    'quote " and # hash',             # quote + comment indicators
])
def test_speaker_name_cannot_inject_frontmatter(evil_name: str) -> None:
    yaml = pytest.importorskip("yaml")
    s = sess.SessionState(title="T", started_at="2026-07-15T14:00:00")
    s = sess.add_segment(s, "S1", "hello", 0.0, 1.0)
    s = sess.add_segment(s, "S2", "hi", 1.0, 2.0)
    s = sess.rename_speaker(s, "S1", evil_name)

    fm = yaml.safe_load(_frontmatter(render_note(s, NOW)))

    # Parses as valid YAML with exactly the expected top-level keys — the hostile
    # name did NOT create an "injected" (or any other) top-level key.
    assert set(fm) == {"created", "type", "speakers", "source"}
    # ...and it round-trips as a plain string list, the evil name preserved.
    assert fm["speakers"] == [evil_name, "Speaker 2"]
