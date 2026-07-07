"""Run the Sonar harness: ``python -m sonar_harness`` (or via uvicorn).

Binds localhost only — the harness is the STT<->TTS seam on this device, never
exposed off-box. Port is configurable via SONAR_PORT (default 8787).
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    uvicorn.run(
        "sonar_harness.server:app",
        host=os.environ.get("SONAR_HOST", "127.0.0.1"),
        port=int(os.environ.get("SONAR_PORT", "8787")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
