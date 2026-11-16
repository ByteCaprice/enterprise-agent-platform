"""Read-only async lookup over supplied candidates only; no network or storage."""

import asyncio

from domain.platform_tool import ToolContext

from .steps import rank_candidates


async def lookup_candidates(_context: ToolContext, values: dict) -> dict:
    return await asyncio.to_thread(rank_candidates, values)
