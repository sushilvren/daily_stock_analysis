from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

import pandas as pd
from fastapi import HTTPException, Query

import app as base_app
from ifind_client import IFIND
from microstructure import bars as tdx_bars, quote_depth as tdx_quote_depth, transactions as tdx_transactions
from providers import load_spot as base_load_spot, provider_meta
from scanner import opportunity_scan, theme_strength
from strategy import DEFAULT_HOLDINGS, market_context, rank_codes
from swing_models import score_daily_bars
from themes import THEMES

FOCUS_CODES = sorted(set(DEFAULT_HOLDINGS + [c for cfg in THEMES.values() for c in cfg.get("codes", [])]))
_IFIND_LAST_APPLIED = 0


def _overlay_ifind(df: pd.DataFrame) -> pd.DataFrame:
    global _IFIND_LAST_APPLIED
    quotes = IFIND.safe_realtime(FOCUS_CODES)
    if not quotes:
        _IFIND_LAST_APPLIED = 0
        return df
    work = df.copy()
    work["代码"] = work["代码"].astype(str).str.zfill(6)
    idx_map = {str(code): idx for idx, code in zip(work.index, work["代码"])}
    applied = 0
    for code, q in quotes.items():
        idx = idx_map.get(code)
        if idx is None:
            continue
        mapping = {"latest": "最新价", "open": "今开", "high": "最高", "low": "最低"}
        for src, dst in mapping.items():
            value = q.get(src)
            try:
                if value is not None:
                    work.at[idx, dst] = float(value)
            except Exception:
                pass
        try:
            last = float(work.at[idx, "最新价"])
            prev = float(work.at[idx, "昨收"])
            if prev > 0:
                work.at[idx, "涨跌额"] = last - prev
                work.at[idx, "涨跌幅"] = (last / prev - 1) * 100
        except Exception:
            pass
        applied += 1
    _IFIND_LAST_APPLIED = applied
    return work


def load_spot() -> tuple[pd.DataFrame, str]:
    df, fetched_at = base_load_spot()
    if IFIND.enabled:
        df = _overlay_ifind(df)
    return df, fetched_at


def gateway_meta(fetched_at: str | None = None) -> dict[str, Any]:
    meta = provider_meta(fetched_at)
    meta.update({
        "ifind_overlay_enabled": IFIND.enabled,
        "ifind_focus_quotes_applied": _IFIND_LAST_APPLIED,
        "ifind_provider_time": IFIND.last_provider_time,
        "ifind_last_error": IFIND.last_error,
    })
    if _IFIND_LAST_APPLIED:
        meta["source"] = f"{meta.get('source')} + iFinD focus overlay"
    return meta


# Patch base route module so all existing endpoints use the same provider layer.
base_app._load_spot = load_spot
base_app._meta = gateway_meta
app = base_app.app
_load_spot = load_spot
_meta = gateway_meta

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


@app.get("/micro/quote/{code}")
def micro_quote(code: str) -> dict[str, Any]:
    try:
        data = tdx_quote_depth(code)
        data["warning"] = "Public TongdaXin feed; useful for microstructure confirmation but not licensed Level-2."
        return data
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"TongdaXin quote unavailable: {exc}")


@app.get("/micro/ticks/{code}")
def micro_ticks(
    code: str,
    start: int = Query(0, ge=0),
    count: int = Query(100, ge=1, le=2000),
) -> dict[str, Any]:
    try:
        data = tdx_transactions(code, start=start, count=count)
        data["warning"] = "Public transaction feed; field semantics depend on upstream and are not exchange Level-2 attribution."
        return data
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"TongdaXin transaction feed unavailable: {exc}")


@app.get("/micro/bars/{code}")
def micro_bars(
    code: str,
    category: int = Query(0, description="0=5m,1=15m,2=30m,3=1h,9=daily"),
    count: int = Query(120, ge=10, le=800),
) -> dict[str, Any]:
    try:
        return tdx_bars(code, category=category, count=count)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"TongdaXin bars unavailable: {exc}")


@app.get("/strategy/swing/{code}")
def swing_strategy(code: str, count: int = Query(180, ge=60, le=500)) -> dict[str, Any]:
    try:
        raw = tdx_bars(code, category=9, count=count)
        model = score_daily_bars(raw.get("data", []))
        return {
            "source": raw.get("source"),
            "code": raw.get("code"),
            "model": model,
            "model_inputs": "daily OHLCV; breakout, MA alignment, volume confirmation, consolidation, shakeout and trend momentum",
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"swing strategy unavailable: {exc}")


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
