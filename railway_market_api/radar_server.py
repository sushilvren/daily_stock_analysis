from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from fastapi import HTTPException, Query

from breakout_radar import MEMORY, breakout_radar
from server import _load_spot, _meta, app
from strategy import market_context
from upstream_dsa_integration import market_phase

log = logging.getLogger("breakout_radar")
_RADAR_MEMORY_STARTED = False


@app.get("/scan/breakout-radar")
def scan_breakout_radar(
    limit: int = Query(20, ge=1, le=100),
    mode: str = Query("close", pattern="^(close|auction|intraday)$"),
) -> dict[str, Any]:
    try:
        df, fetched_at = _load_spot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"market provider unavailable: {exc}")
    context = market_context(df)
    data = breakout_radar(df, limit=limit, mode=mode)
    return {
        "meta": _meta(fetched_at),
        "market": context,
        "market_phase": market_phase(context),
        "mode": mode,
        "data": data,
        "grading": {"A": ">=78", "B": "68-77.9", "C": "below 68 but above mode threshold"},
        "model": {
            "goal": "pre-breakout candidate discovery before obvious limit-up acceleration",
            "factors": [
                "board elasticity (BSE/ChiNext/STAR)",
                "small float-cap preference",
                "turnover and volume-ratio preheat",
                "relative strength without climax",
                "5-10 day capital memory",
                "auction confirmation when mode=auction",
            ],
        },
        "note": "Quantitative pre-filter only. Grade A means priority research, not an automatic buy. Catalyst/news purity and sector breadth must be confirmed separately.",
    }


def _memory_loop() -> None:
    active_interval = max(180, int(os.getenv("BREAKOUT_MEMORY_INTERVAL_SECONDS", "300")))
    closed_interval = max(900, int(os.getenv("BREAKOUT_MEMORY_CLOSED_INTERVAL_SECONDS", "1800")))
    while True:
        try:
            df, fetched_at = _load_spot()
            meta = _meta(fetched_at)
            MEMORY.observe(df)
            session = str(meta.get("market_session", "closed"))
            active = session in {"auction", "pre_open", "continuous_am", "continuous_pm", "lunch_break", "post_close"}
            sleep_for = active_interval if active else closed_interval
        except Exception as exc:
            log.warning("BREAKOUT_MEMORY_ERROR %r", exc)
            sleep_for = min(closed_interval, 600)
        time.sleep(sleep_for)


@app.on_event("startup")
def start_breakout_memory() -> None:
    global _RADAR_MEMORY_STARTED
    if _RADAR_MEMORY_STARTED or os.getenv("BREAKOUT_RADAR_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return
    _RADAR_MEMORY_STARTED = True
    thread = threading.Thread(target=_memory_loop, name="breakout-memory", daemon=True)
    thread.start()
    log.warning("BREAKOUT_RADAR_MEMORY_STARTED")
