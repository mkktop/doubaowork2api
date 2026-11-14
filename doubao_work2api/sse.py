"""Keep downstream SSE alive without changing token timing or generating text."""
from __future__ import annotations
import asyncio
from contextlib import suppress


async def keepalive(source, interval=15):
    iterator = source.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.create_task(anext(iterator))
            ready, _ = await asyncio.wait({pending}, timeout=interval)
            if not ready:
                yield ': ping\n\n'
                continue
            completed, pending = pending, None
            try:
                item = completed.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        if pending is not None:
            pending.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        if hasattr(iterator, 'aclose'):
            await iterator.aclose()
