"""
step_events.py
==============
Shared SSE step queue — imported by BOTH server.py and tools.py.
server.py  → imports emit_step, _step_queues from here
tools.py   → imports emit_step from here
"""
import asyncio
import json
import logging

_step_queues: dict[str, asyncio.Queue] = {}


def emit_step(
    event_id: str,
    step:     str,
    status:   str,       # "running" | "done" | "error"
    meta:     str = "",
    payload:  dict = None,
) -> None:
    """
    Push one step event to the SSE queue for this eventId.
    Called from tools.py — completely silent if no GUI is connected.
    Never raises, never blocks.
    """
    q = _step_queues.get(event_id)
    if not q:
        return
    try:
        q.put_nowait({
            "step":    step,
            "status":  status,
            "meta":    meta,
            "payload": payload or {},
        })
    except Exception:
        pass  # QueueFull or no event loop — always silent
