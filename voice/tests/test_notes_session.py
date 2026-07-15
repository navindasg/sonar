"""Session state transitions: pure, validated, rev-bumped, JSON-serializable."""

from __future__ import annotations

from notes import session as sess


def _base() -> sess.SessionState:
    return sess.SessionState(title="Standup", started_at="2026-07-15T10:00:00")


def _with_segments() -> sess.SessionState:
    s = sess.add_segment(_base(), "S1", "morning everyone", 0.0, 1.4)
    s = sess.add_segment(s, "S2", "hi, quick update from me", 2.0, 4.1)
    return s


def test_add_segment_registers_speaker_and_bumps_rev() -> None:
    s = _base()
    s2 = sess.add_segment(s, "S1", "  hello  ", 0.0, 1.0)
    assert s.segments == ()                      # original untouched (immutable)
    assert [x.text for x in s2.segments] == ["hello"]
    assert dict(s2.names) == {"S1": "Speaker 1"}
    assert s2.rev == s.rev + 1


def test_add_segment_rejects_garbage() -> None:
    s = _base()
    assert sess.add_segment(s, "bogus", "hi", 0.0, 1.0) is s
    assert sess.add_segment(s, "S1", "   ", 0.0, 1.0) is s


def test_edit_and_delete_segment() -> None:
    s = _with_segments()
    edited = sess.edit_segment_text(s, 0, "good morning everyone")
    assert edited.segments[0].text == "good morning everyone"
    assert edited.segments[1] == s.segments[1]

    gone = sess.delete_segment(edited, 0)
    assert [x.id for x in gone.segments] == [1]

    # unknown ids / wrong types are no-ops that return the SAME state object
    assert sess.edit_segment_text(s, 99, "x") is s
    assert sess.edit_segment_text(s, "0", "x") is s
    assert sess.delete_segment(s, None) is s


def test_reassign_and_rename_speaker() -> None:
    s = _with_segments()
    moved = sess.reassign_segment(s, 1, "S1")
    assert moved.segments[1].speaker == "S1"

    named = sess.rename_speaker(moved, "S1", "  Navin ")
    assert sess.display_name(named, "S1") == "Navin"
    assert sess.display_name(named, "S2") == "Speaker 2"

    # renaming an unknown speaker or with a non-string is a no-op
    assert sess.rename_speaker(s, "S9", "x") is s
    assert sess.rename_speaker(s, "S1", 42) is s


def test_rename_to_blank_restores_default() -> None:
    s = sess.rename_speaker(_with_segments(), "S2", "   ")
    assert sess.display_name(s, "S2") == "Speaker 2"


def test_add_speaker_fills_first_free_slot() -> None:
    s = _with_segments()                        # has S1, S2
    s3 = sess.add_speaker(s)
    assert dict(s3.names).keys() == {"S1", "S2", "S3"}


def test_title_and_summary_and_status() -> None:
    s = _base()
    assert sess.set_title(s, "  Planning sync ").title == "Planning sync"
    assert sess.set_title(s, "") is s
    assert sess.set_summary(s, "### Summary").summary_md == "### Summary"
    assert sess.set_status(s, sess.REVIEW).status == sess.REVIEW
    assert sess.set_status(s, "nonsense") is s


def test_mark_saved() -> None:
    s = sess.mark_saved(_with_segments(), "Sonar/Notes/Standup.md")
    assert s.status == sess.SAVED
    assert s.saved_path == "Sonar/Notes/Standup.md"


def test_to_json_shape() -> None:
    s = sess.rename_speaker(_with_segments(), "S1", "Navin")
    j = sess.to_json(s, elapsed_s=12.34)
    assert j["type"] == "state"
    assert j["title"] == "Standup"
    assert j["elapsed_s"] == 12.3
    assert j["speakers"] == [{"id": "S1", "name": "Navin"},
                             {"id": "S2", "name": "Speaker 2"}]
    assert j["segments"][0] == {"id": 0, "speaker": "S1",
                                "text": "morning everyone", "t0": 0.0, "t1": 1.4}
