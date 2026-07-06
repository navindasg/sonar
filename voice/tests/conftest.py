"""Shared test fixtures and guards for the osvoice pure-logic test suite.

These tests must run on ANY machine — without mlx / torch / ollama / parakeet_mlx
installed — because every provider adapter keeps its heavy, Apple-Silicon-only
backends behind lazy imports. This module fails fast with a clear message if any
of those heavy backends leak into the import graph, so a regression in the lazy
import discipline surfaces here instead of as a confusing CI failure elsewhere.
"""
from __future__ import annotations

import sys

# Backends that MUST stay out of the import graph for resolver/aggregator/vad
# unit tests. If one is importable in CI that is fine; what matters is that
# importing osvoice.registry never *triggers* importing them (verified below).
_HEAVY_BACKENDS = ("mlx", "mlx_lm", "mlx_audio", "parakeet_mlx", "torch", "silero_vad")


def pytest_configure(config) -> None:
    """Import the registry and assert no heavy backend was pulled in transitively."""
    import osvoice.registry  # noqa: F401  (import side effect is the thing under test)

    leaked = [name for name in _HEAVY_BACKENDS if name in sys.modules]
    assert not leaked, (
        f"heavy backend(s) leaked into the import graph via lazy-import violation: "
        f"{leaked}; adapters must import these inside load()/stream()"
    )
