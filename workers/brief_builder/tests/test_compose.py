"""Tests for markdown composition and the LLM prompt builder (no live model)."""

from __future__ import annotations

from pathlib import Path

from brief_builder.brief import compose_markdown
from brief_builder.gather import NoteInput
from brief_builder.llm import build_prompt


def _note(title: str, excerpt: str) -> NoteInput:
    return NoteInput(path=Path(f"/x/{title}.md"), title=title, excerpt=excerpt, mtime=1.0)


def test_compose_includes_frontmatter_body_and_sources() -> None:
    notes = (_note("Project Alpha", "kickoff"), _note("Groceries", "milk"))
    md = compose_markdown(
        title="Any Brief — 2026-07-06",
        created_at="2026-07-06T12:00:00+00:00",
        window="any",
        body_md="- ship the spike\n- buy milk",
        notes=notes,
    )
    assert md.startswith("---\n")
    assert "title: Any Brief — 2026-07-06" in md
    assert "window: any" in md
    assert "generator: sonar/brief-builder" in md
    assert "# Any Brief — 2026-07-06" in md
    assert "- ship the spike" in md
    assert "## Sources" in md
    assert "- Project Alpha" in md
    assert "- Groceries" in md


def test_compose_omits_sources_when_no_notes() -> None:
    md = compose_markdown(
        title="Morning Brief — 2026-07-06",
        created_at="2026-07-06T07:00:00+00:00",
        window="morning",
        body_md="Quiet day.",
        notes=(),
    )
    assert "## Sources" not in md
    assert "Quiet day." in md


def test_build_prompt_lists_notes() -> None:
    notes = (_note("Alpha", "kickoff notes"),)
    prompt = build_prompt("morning", notes)
    assert "morning brief" in prompt
    assert "1. Alpha — kickoff notes" in prompt


def test_build_prompt_handles_empty_notes() -> None:
    prompt = build_prompt("any", ())
    assert "no recent notes" in prompt.lower()
