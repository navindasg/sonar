# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "httpx>=0.27",
#   "websockets>=13",
# ]
# ///
"""Sonar morning brief — the proactive delivery of daily.brief.

Runs on a schedule (launchd, via `sonar.sh brief install`) or on demand
(`sonar.sh brief`). Three steps:

  1. Ask the harness for the brief (a /v1 turn that triggers the daily.brief tool),
  2. Save it as a dated vault note (durable, reviewable, RAG-able next reindex),
  3. If the voice loop is up on :8770, speak it aloud through Kokoro.

Speaking reuses the voice loop's new `say` command (proactive text -> TTS, no
harness turn). We stay connected until the loop signals the turn ended, so the
disconnect doesn't cut the brief off mid-sentence. If the voice loop is down the
note is still written — the brief is never lost.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
import websockets

HARNESS = os.environ.get("SONAR_HARNESS_URL", "http://127.0.0.1:8787").rstrip("/")
GLOW_PORT = int(os.environ.get("SONAR_GLOW_PORT", "8770"))
VAULT = Path(
    os.environ.get("SONAR_VAULT_PATH", str(Path.home() / "Documents" / "Obsidian Vault"))
)
PROMPT = os.environ.get(
    "SONAR_BRIEF_PROMPT",
    "Give me my morning brief for today, including any important unread email. "
    "Keep it warm, natural, and concise — it will be read aloud.",
)
_SPEAK_TIMEOUT_S = 180.0


def fetch_brief() -> str:
    """Run one non-streaming harness turn and return the narrated brief text."""
    body = {"stream": False, "messages": [{"role": "user", "content": PROMPT}]}
    resp = httpx.post(f"{HARNESS}/v1/chat/completions", json=body, timeout=180.0)
    resp.raise_for_status()
    data = resp.json()
    return str(data["choices"][0]["message"]["content"]).strip()


def write_note(text: str, *, now: datetime) -> Path:
    """Save the brief as Sonar/Brief/<date>.md (atomic write, append-safe dir)."""
    day = now.strftime("%Y-%m-%d")
    out_dir = VAULT / "Sonar" / "Brief"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{day}.md"
    content = (
        f"# Morning Brief — {day}\n\n_generated {now.strftime('%H:%M')} by Sonar_\n\n"
        f"{text}\n"
    )
    tmp = out_dir / f"{day}.md.tmp"
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return path


async def speak(text: str) -> bool:
    """Ask the voice loop to speak the brief; wait for it to finish. False if the
    voice loop isn't reachable (the note is still saved)."""
    url = f"ws://127.0.0.1:{GLOW_PORT}"
    try:
        async with websockets.connect(url, open_timeout=5) as ws:
            await ws.send(json.dumps({"cmd": "say", "text": text}))
            # Stay connected until the loop reports the turn ended — disconnecting
            # early would trip its cleanup and cut the speech off.
            async with asyncio.timeout(_SPEAK_TIMEOUT_S):
                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if msg.get("turn") == "end":
                        return True
        return True
    except Exception as exc:  # noqa: BLE001 — voice down must not lose the brief
        print(f"[brief] not spoken (voice loop unreachable: {exc})", file=sys.stderr)
        return False


async def main() -> None:
    try:
        text = fetch_brief()
    except Exception as exc:  # noqa: BLE001 — surface a clear failure to the log
        print(f"[brief] harness request failed: {exc}", file=sys.stderr)
        sys.exit(1)
    if not text:
        print("[brief] harness returned an empty brief", file=sys.stderr)
        sys.exit(1)

    path = write_note(text, now=datetime.now())
    print(f"[brief] saved {path}")
    spoken = await speak(text)
    print(f"[brief] spoken={spoken}")


if __name__ == "__main__":
    asyncio.run(main())
