from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from fastapi import HTTPException, Query

import app as base_app
from providers import load_spot, provider_meta
from scanner import opportunity_scan, theme_strength
from strategy import DEFAULT_HOLDINGS, market_context, rank_codes
from themes import THEMES

# Patch the base route module so existing /quote, /portfolio, /market endpoints use
# the same hybrid provider as the monitor/scanners.
base_app._load_spot = load_spot
base_app._meta = provider_meta
app = base_app.app
_load_spot = load_spot
_meta = provider_meta

log = logging.getLogger("market_monitor")
_MONITOR_STARTED = False


def _snapshot_payload(df) -> dict[str, Any]:
    return {
        "market": market_context(df),
        "themes": theme_strength(df, THEMES),
        "holdings": rank_codes(df, DEFAULT_HOLDINGS),
        "opportunities": opportunity_scan(df, limit=10),
    }


@app.get("/themes/default")
def themes_default() -> dict[str, Any]:
    try:
        df, fetched_at = _load_spot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"market provider unavailable: {exc}")
    return {"meta": _meta(fetched_at), "data": theme_strength(df, THEMES)}


@app.get("/scan/opportunities")
def scan_opportunities(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    try:
        df, fetched_at = _load_spot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"market provider unavailable: {exc}")
    return {
        "meta": _meta(fetched_at),
        "market": market_context(df),
        "data": opportunity_scan(df, limit=limit),
        "note": "Heuristic candidate scan; candidates require sector/news/risk confirmation before any trade decision.",
    }


@app.get("/snapshot")
def snapshot() -> dict[str, Any]:
    try:
        df, fetched_at = _load_spot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"market provider unavailable: {exc}")
    return {"meta": _meta(fetched_at), **_snapshot_payload(df)}


def _is_active_session(session: str) -> bool:
    return session in {"auction", "pre_open", "continuous_am", "continuous_pm", "lunch_break", "post_close"}


def _monitor_loop() -> None:
    interval = max(15, int(os.getenv("MONITOR_INTERVAL_SECONDS", "30")))
    closed_interval = max(300, int(os.getenv("MONITOR_CLOSED_INTERVAL_SECONDS", "900")))
    first = True
    while True:
        try:
            df, fetched_at = _load_spot()
            meta = _meta(fetched_at)
            payload = {"meta": meta, **_snapshot_payload(df)}
            # Compact machine-readable line: ChatGPT can retrieve the newest snapshot
            # via Railway logs before a custom MCP/app connection is available.
            log.warning("MARKET_SNAPSHOT %s", json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))
            sleep_for = interval if _is_active_session(meta["market_session"]) else closed_interval
        except Exception as exc:
            log.exception("MARKET_PROVIDER_ERROR %r", exc)
            sleep_for = 60 if first else min(closed_interval, 300)
        first = False
        time.sleep(sleep_for)


@app.on_event("startup")
def start_monitor() -> None:
    global _MONITOR_STARTED
    if _MONITOR_STARTED or os.getenv("MONITOR_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return
    _MONITOR_STARTED = True
    thread = threading.Thread(target=_monitor_loop, name="market-monitor", daemon=True)
    thread.start()
    log.warning("MARKET_MONITOR_STARTED")
