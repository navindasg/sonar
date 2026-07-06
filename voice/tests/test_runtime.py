"""Tests for the MLX execution gate's async bridging (no MLX needed).

`offload` and `stream_sync` are exercised with plain Python callables/generators:
we only verify the async<->worker-thread plumbing (return values, item ordering,
completion, and exception propagation), which is backend-agnostic.
"""
from __future__ import annotations

import pytest

from osvoice.runtime import offload, stream_sync


async def test_offload_returns_callable_result() -> None:
    assert await offload(lambda: 6 * 7) == 42


async def test_offload_passes_args() -> None:
    assert await offload(lambda a, b: a + b, 2, 3) == 5


async def test_stream_sync_yields_in_order() -> None:
    def gen():
        yield from range(5)

    out = [item async for item in stream_sync(gen)]
    assert out == [0, 1, 2, 3, 4]


async def test_stream_sync_empty_generator() -> None:
    def gen():
        return iter(())

    out = [item async for item in stream_sync(gen)]
    assert out == []


async def test_stream_sync_propagates_error_after_items() -> None:
    def gen():
        yield "a"
        yield "b"
        raise RuntimeError("boom in generator")

    seen: list[str] = []
    with pytest.raises(RuntimeError, match="boom in generator"):
        async for item in stream_sync(gen):
            seen.append(item)

    # Items produced before the error are still delivered, then the error raises.
    assert seen == ["a", "b"]
