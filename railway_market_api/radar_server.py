from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

from fastapi import HTTPException, Query

from breakout_radar import MEMORY, breakout_radar
from hithink_client import HITHINK
from server import _load_spot, _meta, app
from strategy import market_context
from upstream_dsa_integration import market_phase

log = logging.getLogger("breakout_radar")
_RADAR_MEMORY_STARTED = False


def _grade(score: float) -> str:
    return "A" if score >= 78 else "B" if score >= 68 else "C"


def _enrich_with_hithink_auction(rows: list[dict[str, Any]], stage: str = "final") -> list[dict[str, Any]]:
    """Add official auction fields and a small confirmation delta to pre-filter scores."""
    if not rows or not HITHINK.enabled:
        return rows
    codes = [str(r.get("code", "")) for r in rows[:100] if r.get("code")]
    auction = HITHINK.safe_auction_snapshot(codes, stage=stage)
    by_code = {HITHINK.plain_code(x.get("thscode") or x.get("ticker") or ""): x for x in auction}

    for row in rows:
        item = by_code.get(str(row.get("code", "")).zfill(6))
        if not item:
            continue
        pct = item.get("auction_pct")
        vr = item.get("auction_volume_ratio")
        turn = item.get("auction_turnover_pct")
        amount = item.get("auction_amount")
        float_cap = item.get("float_market_cap")
        unmatched = item.get("auction_unmatched")

        def num(v: Any, default: float = 0.0) -> float:
            try:
                return float(v) if v is not None else default
            except Exception:
                return default

        apct = num(pct)
        avr = num(vr)
        aturn = num(turn)
        delta = 0.0
        tags = list(row.get("tags") or [])
        risks = list(row.get("risks") or [])

        if 0.5 <= apct <= 5:
            delta += 4
            tags.append("官方竞价偏强")
        elif apct > 7:
            delta -= 3
            risks.append("竞价过度一致")
        elif apct < -3:
            delta -= 4
            risks.append("竞价明显偏弱")

        if 1.5 <= avr <= 6:
            delta += 5
            tags.append("竞价量比放大")
        elif avr > 10:
            delta -= 1
            risks.append("竞价量比过热")

        if 0.2 <= aturn <= 3:
            delta += 2
            tags.append("竞价换手有效")
        elif aturn > 6:
            delta -= 2
            risks.append("竞价换手过热")

        new_score = max(0.0, min(100.0, float(row.get("score", 0.0)) + delta))
        row["score"] = round(new_score, 1)
        row["grade"] = _grade(new_score)
        row["tags"] = list(dict.fromkeys(tags))
        row["risks"] = list(dict.fromkeys(risks))
        row["hithink_auction"] = {
            "auction_price": item.get("auction_price"),
            "auction_pct": pct,
            "auction_volume": item.get("auction_volume"),
            "auction_amount": amount,
            "auction_unmatched": unmatched,
            "auction_turnover_pct": turn,
            "auction_yesterday_ratio_pct": item.get("auction_yesterday_ratio_pct"),
            "auction_volume_ratio": vr,
            "float_market_cap": float_cap,
            "data_status": item.get("data_status"),
            "score_delta": round(delta, 1),
        }

    rows.sort(key=lambda x: (x.get("score", 0), x.get("relative_strength", 0), x.get("turnover", 0)), reverse=True)
    return rows


def _require_hithink() -> None:
    if not HITHINK.enabled:
        raise HTTPException(
            status_code=503,
            detail="HiThink provider is installed but HITHINK_FINANCE_API_KEY is not configured",
        )


@app.get("/providers/hithink/status")
def hithink_status() -> dict[str, Any]:
    return HITHINK.status()


@app.get("/scan/breakout-radar")
def scan_breakout_radar(
    limit: int = Query(20, ge=1, le=100),
    mode: str = Query("close", pattern="^(close|auction|intraday)$"),
    auction_stage: str = Query("final", pattern="^(live|final)$"),
) -> dict[str, Any]:
    try:
        df, fetched_at = _load_spot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"market provider unavailable: {exc}")
    context = market_context(df)
    # Pull a wider local shortlist first; HiThink only enriches the finalists.
    pre_limit = min(100, max(limit * 3, 30))
    data = breakout_radar(df, limit=pre_limit, mode=mode)
    if mode == "auction":
        data = _enrich_with_hithink_auction(data, stage=auction_stage)
    data = data[:limit]
    return {
        "meta": _meta(fetched_at),
        "market": context,
        "market_phase": market_phase(context),
        "mode": mode,
        "hithink": HITHINK.status(),
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
                "HiThink official auction confirmation when mode=auction",
            ],
        },
        "note": "Quantitative pre-filter only. Grade A means priority research, not an automatic buy. Catalyst/news purity and sector breadth still require confirmation.",
    }


@app.get("/hithink/auction")
def hithink_auction(
    codes: str = Query(..., description="Comma-separated A-share codes"),
    stage: str = Query("final", pattern="^(live|final)$"),
) -> dict[str, Any]:
    _require_hithink()
    items = [x.strip() for x in codes.split(",") if x.strip()]
    if not items or len(items) > 100:
        raise HTTPException(status_code=400, detail="codes must contain 1-100 A-share symbols")
    try:
        return {"provider": "HiThink Financial API", "data": HITHINK.auction_snapshot(items, stage=stage)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"HiThink auction unavailable: {exc}")


@app.get("/hithink/valuations")
def hithink_valuations(codes: str = Query(..., description="Comma-separated A-share codes")) -> dict[str, Any]:
    _require_hithink()
    items = [x.strip() for x in codes.split(",") if x.strip()]
    if not items or len(items) > 100:
        raise HTTPException(status_code=400, detail="codes must contain 1-100 A-share symbols")
    try:
        return {"provider": "HiThink Financial API", "data": HITHINK.valuations(items)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"HiThink valuations unavailable: {exc}")


@app.get("/hithink/limit-up-pool")
def hithink_limit_up_pool(
    date_ms: int | None = Query(None),
    size: int = Query(100, ge=1, le=200),
) -> dict[str, Any]:
    _require_hithink()
    try:
        return {"provider": "HiThink Financial API", "data": HITHINK.limit_up_pool(date_ms=date_ms, size=size)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"HiThink limit-up pool unavailable: {exc}")


@app.get("/hithink/limit-up-ladder")
def hithink_limit_up_ladder() -> dict[str, Any]:
    _require_hithink()
    try:
        return {"provider": "HiThink Financial API", "data": HITHINK.limit_up_ladder()}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"HiThink limit-up ladder unavailable: {exc}")


@app.get("/hithink/anomalies")
def hithink_anomalies(tag_codes: str | None = Query(None)) -> dict[str, Any]:
    _require_hithink()
    try:
        return {"provider": "HiThink Financial API", "data": HITHINK.anomaly_list(tag_codes=tag_codes)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"HiThink anomalies unavailable: {exc}")


@app.get("/hithink/hot-stocks")
def hithink_hot_stocks(period: str = Query("hour", pattern="^(hour|day)$")) -> dict[str, Any]:
    _require_hithink()
    try:
        return {"provider": "HiThink Financial API", "data": HITHINK.hot_stock_list(period=period)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"HiThink hot-stock list unavailable: {exc}")


@app.get("/hithink/dragon-tiger")
def hithink_dragon_tiger(
    board_type: str = Query("all", pattern="^(all|org|hot_money)$"),
    date: str | None = Query(None),
) -> dict[str, Any]:
    _require_hithink()
    try:
        return {"provider": "HiThink Financial API", "data": HITHINK.dragon_tiger(board_type=board_type, date=date)}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"HiThink dragon-tiger unavailable: {exc}")


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
