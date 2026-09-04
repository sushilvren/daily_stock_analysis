from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

APP_NAME = "A-Stock Market Data Gateway"
DEFAULT_CACHE_SECONDS = float(os.getenv("CACHE_SECONDS", "4"))
SH_TZ = ZoneInfo("Asia/Shanghai")

app = FastAPI(
    title=APP_NAME,
    version="0.1.0",
    description=(
        "Read-only A-share market-data gateway. Every response includes source and fetch timestamp. "
        "The fallback provider is AkShare and MUST NOT be treated as exchange Level-2 or guaranteed real-time data."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["*"],
)

_lock = threading.Lock()
_cache: dict[str, Any] = {"ts": 0.0, "df": None, "fetched_at": None, "error": None}


def _now_iso() -> str:
    return datetime.now(SH_TZ).isoformat(timespec="seconds")


def _market_session() -> str:
    now = datetime.now(SH_TZ)
    if now.weekday() >= 5:
        return "closed_weekend"
    hhmm = now.hour * 100 + now.minute
    if 915 <= hhmm < 925:
        return "auction"
    if 925 <= hhmm < 930:
        return "pre_open"
    if 930 <= hhmm < 1130:
        return "continuous_am"
    if 1130 <= hhmm < 1300:
        return "lunch_break"
    if 1300 <= hhmm < 1500:
        return "continuous_pm"
    if 1500 <= hhmm < 1530:
        return "post_close"
    return "closed"


def _safe_value(v: Any) -> Any:
    if pd.isna(v):
        return None
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            pass
    return v


def _normalise_code(code: str) -> str:
    c = code.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if c.startswith(prefix):
            c = c[2:]
    if "." in c:
        c = c.split(".")[0]
    return c.zfill(6)


def _load_spot() -> tuple[pd.DataFrame, str]:
    now = time.monotonic()
    with _lock:
        df = _cache.get("df")
        if df is not None and now - float(_cache["ts"]) < DEFAULT_CACHE_SECONDS:
            return df, str(_cache["fetched_at"])

        try:
            fresh = ak.stock_zh_a_spot_em()
            fetched_at = _now_iso()
            if fresh is None or fresh.empty:
                raise RuntimeError("AkShare returned an empty A-share spot table")
            fresh["代码"] = fresh["代码"].astype(str).str.zfill(6)
            _cache.update(ts=now, df=fresh, fetched_at=fetched_at, error=None)
            return fresh, fetched_at
        except Exception as exc:
            _cache["error"] = repr(exc)
            # If the upstream fails, return a recent cached snapshot for up to 120 seconds.
            if df is not None and now - float(_cache["ts"]) < 120:
                return df, str(_cache["fetched_at"])
            raise


def _meta(fetched_at: str) -> dict[str, Any]:
    return {
        "source": "AkShare.stock_zh_a_spot_em",
        "fetched_at": fetched_at,
        "timezone": "Asia/Shanghai",
        "market_session": _market_session(),
        "provider_timestamp_available": False,
        "real_time_guarantee": False,
        "level2": False,
        "freshness_note": (
            "fetched_at is the gateway retrieval time, not an exchange timestamp. "
            "Do not describe this feed as guaranteed real-time or Level-2."
        ),
    }


FIELD_MAP = {
    "代码": "code",
    "名称": "name",
    "最新价": "last",
    "涨跌幅": "pct_change",
    "涨跌额": "change",
    "成交量": "volume",
    "成交额": "turnover",
    "振幅": "amplitude",
    "最高": "high",
    "最低": "low",
    "今开": "open",
    "昨收": "prev_close",
    "量比": "volume_ratio",
    "换手率": "turnover_rate",
    "涨速": "speed",
    "5分钟涨跌": "change_5m",
    "60日涨跌幅": "change_60d",
    "年初至今涨跌幅": "change_ytd",
    "总市值": "market_cap",
    "流通市值": "float_market_cap",
}


def _row_to_quote(row: pd.Series) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for cn, en in FIELD_MAP.items():
        if cn in row.index:
            out[en] = _safe_value(row[cn])
    return out


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": APP_NAME,
        "status": "ok",
        "docs": "/docs",
        "health": "/health",
        "warning": "Fallback data is not guaranteed exchange-real-time or Level-2.",
    }


@app.get("/health")
def health() -> dict[str, Any]:
    age = None
    if _cache.get("df") is not None:
        age = round(time.monotonic() - float(_cache.get("ts", 0)), 2)
    return {
        "status": "ok",
        "server_time": _now_iso(),
        "market_session": _market_session(),
        "cache_age_seconds": age,
        "last_provider_error": _cache.get("error"),
    }


@app.get("/quote/{code}")
def quote(code: str) -> dict[str, Any]:
    c = _normalise_code(code)
    try:
        df, fetched_at = _load_spot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"market provider unavailable: {exc}")
    hit = df[df["代码"] == c]
    if hit.empty:
        raise HTTPException(status_code=404, detail=f"code not found: {c}")
    return {"meta": _meta(fetched_at), "data": _row_to_quote(hit.iloc[0])}


@app.get("/quotes")
def quotes(codes: str = Query(..., description="Comma-separated A-share codes")) -> dict[str, Any]:
    requested = [_normalise_code(x) for x in codes.split(",") if x.strip()]
    if not requested:
        raise HTTPException(status_code=400, detail="no codes supplied")
    if len(requested) > 100:
        raise HTTPException(status_code=400, detail="max 100 codes per request")
    try:
        df, fetched_at = _load_spot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"market provider unavailable: {exc}")
    indexed = df.set_index("代码", drop=False)
    rows = []
    missing = []
    for c in requested:
        if c not in indexed.index:
            missing.append(c)
            continue
        row = indexed.loc[c]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        rows.append(_row_to_quote(row))
    return {"meta": _meta(fetched_at), "data": rows, "missing": missing}


@app.get("/market/breadth")
def breadth() -> dict[str, Any]:
    try:
        df, fetched_at = _load_spot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"market provider unavailable: {exc}")
    pct = pd.to_numeric(df.get("涨跌幅"), errors="coerce")
    turnover = pd.to_numeric(df.get("成交额"), errors="coerce")
    data = {
        "total": int(pct.notna().sum()),
        "up": int((pct > 0).sum()),
        "down": int((pct < 0).sum()),
        "flat": int((pct == 0).sum()),
        "limit_like_up_9_5": int((pct >= 9.5).sum()),
        "limit_like_down_9_5": int((pct <= -9.5).sum()),
        "up_5pct": int((pct >= 5).sum()),
        "down_5pct": int((pct <= -5).sum()),
        "median_pct_change": _safe_value(pct.median()),
        "total_turnover": _safe_value(turnover.sum()),
    }
    return {"meta": _meta(fetched_at), "data": data}


@app.get("/scan/movers")
def movers(limit: int = Query(30, ge=1, le=200), side: str = Query("up")) -> dict[str, Any]:
    try:
        df, fetched_at = _load_spot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"market provider unavailable: {exc}")
    work = df.copy()
    work["__pct"] = pd.to_numeric(work.get("涨跌幅"), errors="coerce")
    work["__turnover"] = pd.to_numeric(work.get("成交额"), errors="coerce")
    ascending = side.lower() in {"down", "losers", "bottom"}
    work = work.sort_values(["__pct", "__turnover"], ascending=[ascending, False]).head(limit)
    return {"meta": _meta(fetched_at), "data": [_row_to_quote(r) for _, r in work.iterrows()]}


@app.get("/scan/active")
def active(limit: int = Query(50, ge=1, le=300)) -> dict[str, Any]:
    try:
        df, fetched_at = _load_spot()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"market provider unavailable: {exc}")
    work = df.copy()
    work["__turnover"] = pd.to_numeric(work.get("成交额"), errors="coerce")
    work = work.sort_values("__turnover", ascending=False).head(limit)
    return {"meta": _meta(fetched_at), "data": [_row_to_quote(r) for _, r in work.iterrows()]}
