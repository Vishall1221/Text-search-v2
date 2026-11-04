"""Async entry points shared by the Windows API, scripts and test runner."""
from __future__ import annotations

import asyncio
import sys
from typing import Coroutine, Any


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    # psycopg async does not support Windows' default Proactor loop. Uvicorn's
    # explicit factory takes precedence over the process-wide asyncio policy.
    return asyncio.SelectorEventLoop()


def configure_event_loop() -> None:
    if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def run_async(coro: Coroutine[Any, Any, Any]) -> Any:
    with asyncio.Runner(loop_factory=selector_loop_factory if sys.platform == "win32" else None) as runner:
        return runner.run(coro)
