"""Harness round-trip for the voice loop: one turn -> steps + streamed answer.

Consolidates the ``/v1/chat/completions`` SSE read + ``/events`` step-fetch that
``overlay/bridge.py`` proved for the typed path, so the voice loop drives the
exact same harness turn — the only difference is what sits in front (mic->STT)
and behind (answer->TTS) it.

The parse helpers are pure so they unit-test without httpx; ``stream_turn`` does
the IO and is covered by the live smoke.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

log = logging.getLogger("sonar.voice.harness")

_SSE_DATA_PREFIX = "data: "
_SSE_DONE = "[DONE]"

# Event kinds yielded by stream_turn.
STEP = "step"
DELTA = "delta"


def sse_delta(line: str) -> str | None:
    """Extract ``choices[0].delta.content`` from one SSE line, or None.

    Mirrors voice/osvoice/providers/llm_openai.py so the voice loop reads the
    harness stream exactly as osvoice's OpenAI LM slot would.
    """
    if not line.startswith(_SSE_DATA_PREFIX):
        return None
    data = line[len(_SSE_DATA_PREFIX):].strip()
    if not data or data == _SSE_DONE:
        return None
    try:
        obj = json.loads(data)
        choices = obj.get("choices") or []
        if not choices:
            return None
        return choices[0].get("delta", {}).get("content")
    except (json.JSONDecodeError, AttributeError, IndexError, TypeError):
        return None


def steps_from_payload(payload: Any) -> list[dict]:
    """Pull the step-event list out of a ``/events`` JSON body, defensively."""
    if not isinstance(payload, dict):
        return []
    events = payload.get("events")
    return [e for e in events if isinstance(e, dict)] if isinstance(events, list) else []


async def stream_turn(
    client: "Any", messages: list[dict[str, str]]
) -> AsyncIterator[tuple[str, Any]]:
    """Run one harness turn; yield ``(STEP, event)`` then ``(DELTA, text)`` items.

    The harness runs its whole (blocking) tool loop before it streams the first
    answer byte, so by the time the response headers arrive every step-event is
    already recorded — we fetch them once up front, then stream the answer.

    Args:
        client:   an ``httpx.AsyncClient`` bound to the harness base URL.
        messages: the OpenAI-style conversation for this turn — prior
                  user/assistant turns followed by the new user utterance, so
                  follow-ups resolve against the session (the harness threads the
                  whole array into the model; see agent.run_turn).
    """
    payload = {"stream": True, "messages": messages}
    async with client.stream(
        "POST", "/v1/chat/completions", json=payload, timeout=180.0
    ) as resp:
        resp.raise_for_status()
        turn_id = resp.headers.get("X-Sonar-Turn-Id")
        for event in await _fetch_steps(client, turn_id):
            yield STEP, event
        async for line in resp.aiter_lines():
            delta = sse_delta(line)
            if delta:
                yield DELTA, delta


async def _fetch_steps(client: "Any", turn_id: str | None) -> list[dict]:
    """Fetch this turn's recorded step-events from the harness (empty on failure)."""
    if not turn_id:
        return []
    try:
        r = await client.get("/events", params={"turn_id": turn_id}, timeout=10.0)
        r.raise_for_status()
        return steps_from_payload(r.json())
    except Exception:  # noqa: BLE001 — steps are cosmetic; never break the turn
        log.warning("could not fetch /events for turn %s", turn_id)
        return []
