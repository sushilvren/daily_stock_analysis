from __future__ import annotations

import threading
from typing import Any

import pandas as pd

TDX_HOSTS = [
    ("119.147.212.81", 7709),
    ("180.153.39.51", 7709),
    ("115.238.90.165", 7709),
    ("114.80.149.19", 7709),
    ("61.152.249.56", 7709),
    ("123.125.108.23", 7709),
]

_lock = threading.RLock()


def normalize_code(code: str) -> str:
    c = str(code).strip().upper()
    if c.startswith(("SH", "SZ", "BJ")):
        c = c[2:]
    if "." in c:
        c = c.split(".", 1)[0]
    return c.zfill(6)


def market_code(code: str) -> tuple[int, str]:
    c = normalize_code(code)
    if c.startswith(("6", "5", "9")):
        return 1, c
    return 0, c


def _connect():
    from pytdx.hq import TdxHq_API

    api = TdxHq_API(heartbeat=True)
    last_error = None
    for host, port in TDX_HOSTS:
        try:
            if api.connect(host, port, time_out=3):
                return api, f"{host}:{port}"
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"unable to connect to TongdaXin servers: {last_error}")


def quote_depth(code: str) -> dict[str, Any]:
    market, c = market_code(code)
    with _lock:
        api, host = _connect()
        try:
            data = api.get_security_quotes([(market, c)]) or []
            if not data:
                raise RuntimeError(f"no quote for {c}")
            q = data[0]
            bids = []
            asks = []
            for i in range(1, 6):
                bids.append({"level": i, "price": q.get(f"bid{i}"), "volume": q.get(f"bid_vol{i}")})
                asks.append({"level": i, "price": q.get(f"ask{i}"), "volume": q.get(f"ask_vol{i}")})
            prev = float(q.get("last_close") or 0)
            price = float(q.get("price") or 0)
            pct = (price / prev - 1) * 100 if prev else None
            return {
                "source": "TongdaXin/pytdx",
                "server": host,
                "code": c,
                "price": price,
                "prev_close": prev,
                "pct_change": pct,
                "open": q.get("open"),
                "high": q.get("high"),
                "low": q.get("low"),
                "volume": q.get("vol"),
                "amount": q.get("amount"),
                "bids": bids,
                "asks": asks,
            }
        finally:
            try:
                api.disconnect()
            except Exception:
                pass


def transactions(code: str, start: int = 0, count: int = 100) -> dict[str, Any]:
    market, c = market_code(code)
    count = max(1, min(int(count), 2000))
    start = max(0, int(start))
    with _lock:
        api, host = _connect()
        try:
            rows = api.get_transaction_data(market, c, start, count) or []
            data = []
            for r in rows:
                data.append({
                    "time": r.get("time"),
                    "price": r.get("price"),
                    "volume": r.get("vol"),
                    "num": r.get("num"),
                    "buy_or_sell": r.get("buyorsell"),
                })
            return {"source": "TongdaXin/pytdx", "server": host, "code": c, "data": data}
        finally:
            try:
                api.disconnect()
            except Exception:
                pass


def bars(code: str, category: int = 0, count: int = 120) -> dict[str, Any]:
    """category: 0=5m, 1=15m, 2=30m, 3=1h, 9=daily."""
    market, c = market_code(code)
    count = max(10, min(int(count), 800))
    if category not in {0, 1, 2, 3, 9}:
        raise ValueError("category must be one of 0,1,2,3,9")
    with _lock:
        api, host = _connect()
        try:
            rows = api.get_security_bars(category, market, c, 0, count) or []
            df = api.to_df(rows) if rows else pd.DataFrame()
            records = []
            if not df.empty:
                for _, r in df.iterrows():
                    records.append({
                        "datetime": str(r.get("datetime")),
                        "open": _safe(r.get("open")),
                        "high": _safe(r.get("high")),
                        "low": _safe(r.get("low")),
                        "close": _safe(r.get("close")),
                        "volume": _safe(r.get("vol")),
                        "amount": _safe(r.get("amount")),
                    })
            return {"source": "TongdaXin/pytdx", "server": host, "code": c, "category": category, "data": records}
        finally:
            try:
                api.disconnect()
            except Exception:
                pass


def _safe(v: Any) -> Any:
    try:
        if pd.isna(v):
            return None
    except Exception:
        pass
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            pass
    return v
