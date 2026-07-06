"""Process-wide MLX execution gate.

MLX releases the GIL during eval but is NOT thread-safe for concurrent evals
(ml-explore/mlx #3078, #1448), and its GPU stream state is *thread-affine*: an
array or generator created on one thread cannot be evaluated or resumed on
another (the failure is ``std::runtime_error: There is no Stream(gpu, 0) in
current thread``). So every blocking MLX call in osvoice — STT, LLM and TTS alike
— runs on a SINGLE dedicated worker thread.

One thread gives both guarantees at once: at most one eval is ever in flight, and
every step of a streamed generator stays pinned to the thread that created it.
The asyncio event loop stays responsive (audio I/O never blocks) because the work
runs off-loop — we simply never run two evals in parallel, and wouldn't want to:
they share one GPU.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import AsyncIterator, Callable, Iterator, TypeVar

T = TypeVar("T")

# The one and only thread that touches MLX. max_workers=1 serializes every eval
# AND keeps generator steps on a single thread (MLX stream state is thread-local).
_MLX_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="osvoice-mlx")

_DONE = object()


async def offload(fn: Callable[..., T], *args: object) -> T:
    """Run a blocking (MLX) callable on the dedicated MLX worker thread."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_MLX_EXECUTOR, fn, *args)


async def stream_sync(make_iter: Callable[[], Iterator[T]]) -> AsyncIterator[T]:
    """Stream a blocking MLX generator to async code without losing its thread.

    The generator is built AND fully driven inside a single MLX-thread submission
    (a producer that pushes each item onto a thread-safe queue), so its MLX
    arrays/stream are never resumed on a different submission — the failure mode
    that ``offload``-per-``next()`` hits. The async consumer pulls items off the
    queue as they arrive, so tokens/frames still stream out incrementally while the
    event loop stays responsive. Used by the mlx_lm and mlx-audio streaming paths.

    A generator error is captured and re-raised on the consumer side after any
    already-produced items are delivered.

    IMPORTANT: the producer must yield fully-materialized values (str / bytes /
    numpy). Never pass a lazy ``mx.array`` across the queue — it would be evaluated
    on the consumer (event-loop) thread, which raises ``There is no Stream(gpu, 0)
    in current thread``. Force evaluation (np.asarray / .item()) inside make_iter.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[object] = asyncio.Queue()
    error: list[BaseException] = []

    def produce() -> None:
        try:
            for item in make_iter():
                loop.call_soon_threadsafe(queue.put_nowait, item)
        except BaseException as exc:  # noqa: BLE001 - surfaced on the consumer side
            error.append(exc)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, _DONE)

    loop.run_in_executor(_MLX_EXECUTOR, produce)  # whole generator, one submission
    while True:
        item = await queue.get()
        if item is _DONE:
            break
        yield item  # type: ignore[misc]
    if error:
        raise error[0]
