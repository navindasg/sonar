"""Sonar brief-builder worker.

A deterministic (plain-code control flow) scheduled worker that:
  1. gathers bounded inputs from the Obsidian vault (recent notes),
  2. makes ONE bounded LLM leaf call to summarize them,
  3. composes a markdown brief,
  4. writes it to BOTH the SQLite live-state DB and a vault note.

The LLM is only ever a leaf step — never the control loop.
"""

__all__ = ["__version__"]
__version__ = "0.1.0"
