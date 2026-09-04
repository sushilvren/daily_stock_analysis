from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd

SH_TZ = ZoneInfo("Asia/Shanghai")


def _now_iso() -> str:
    return datetime.now(SH_TZ).isoformat(timespec="seconds")


def _session() -> str:
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


class HybridProvider:
    """Prefer AkShare's full table; fall back to direct TongdaXin quotes if HTTP is blocked."""

    TDX_HOSTS = [
        ("119.147.212.81", 7709),
        ("180.153.39.51", 7709),
        ("115.238.90.165", 7709),
        ("114.80.149.19", 7709),
        ("61.152.249.56", 7709),
        ("123.125.108.23", 7709),
    ]

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cache_seconds = max(4.0, float(os.getenv("CACHE_SECONDS", "12")))
        self.cache_ts = 0.0
        self.df: pd.DataFrame | None = None
        self.fetched_at: str | None = None
        self.source = "none"
        self.last_error: str | None = None
        self._tdx_api = None
        self._tdx_host: tuple[str, int] | None = None
        self._universe: list[tuple[int, str, str]] | None = None
        self._universe_ts = 0.0

    def meta(self, fetched_at: str | None = None) -> dict[str, Any]:
        source = self.source
        return {
            "source": source,
            "fetched_at": fetched_at or self.fetched_at or _now_iso(),
            "timezone": "Asia/Shanghai",
            "market_session": _session(),
            "provider_timestamp_available": False,
            "real_time_guarantee": False,
            "level2": False,
            "quote_depth": 5 if source.startswith("TongdaXin") else None,
            "freshness_note": (
                "Gateway retrieval time is not an exchange timestamp. Current fallback feeds are "
                "public/unlicensed for this project and must not be described as guaranteed real-time or Level-2."
            ),
            "last_provider_error": self.last_error,
        }

    def load(self) -> tuple[pd.DataFrame, str]:
        now = time.monotonic()
        with self.lock:
            if self.df is not None and now - self.cache_ts < self.cache_seconds:
                return self.df, str(self.fetched_at)

            errors: list[str] = []
            try:
                df = ak.stock_zh_a_spot_em()
                if df is not None and not df.empty:
                    df["代码"] = df["代码"].astype(str).str.zfill(6)
                    self._save(df, "AkShare/Eastmoney")
                    self.last_error = None
                    return self.df, str(self.fetched_at)
            except Exception as exc:
                errors.append(f"AkShare:{type(exc).__name__}:{exc}")

            try:
                df = self._load_tdx()
                if df is not None and not df.empty:
                    self._save(df, "TongdaXin/pytdx")
                    self.last_error = "; ".join(errors) if errors else None
                    return self.df, str(self.fetched_at)
            except Exception as exc:
                errors.append(f"TDX:{type(exc).__name__}:{exc}")
                self._reset_tdx()

            self.last_error = "; ".join(errors)
            # Graceful stale fallback for up to 3 minutes; timestamp makes staleness explicit.
            if self.df is not None and now - self.cache_ts < 180:
                return self.df, str(self.fetched_at)
            raise RuntimeError(self.last_error or "all market providers unavailable")

    def _save(self, df: pd.DataFrame, source: str) -> None:
        self.df = df
        self.source = source
        self.fetched_at = _now_iso()
        self.cache_ts = time.monotonic()

    @staticmethod
    def _is_a_stock(market: int, code: str) -> bool:
        if market == 1:
            return code.startswith(("600", "601", "603", "605", "688", "689"))
        return code.startswith(("000", "001", "002", "003", "300", "301"))

    def _connect_tdx(self):
        try:
            from pytdx.hq import TdxHq_API
        except ImportError as exc:
            raise RuntimeError("pytdx is not installed") from exc

        if self._tdx_api is not None:
            return self._tdx_api
        api = TdxHq_API(heartbeat=True)
        for host, port in self.TDX_HOSTS:
            try:
                if api.connect(host, port, time_out=2.5):
                    self._tdx_api = api
                    self._tdx_host = (host, port)
                    return api
            except Exception:
                continue
        raise RuntimeError("unable to connect to configured TongdaXin quote servers")

    def _reset_tdx(self) -> None:
        try:
            if self._tdx_api is not None:
                self._tdx_api.disconnect()
        except Exception:
            pass
        self._tdx_api = None
        self._tdx_host = None

    def _load_universe(self, api) -> list[tuple[int, str, str]]:
        # Refresh at most once per day; security list pagination max is 1000.
        if self._universe and time.monotonic() - self._universe_ts < 12 * 3600:
            return self._universe
        rows: list[tuple[int, str, str]] = []
        for market in (0, 1):
            total = int(api.get_security_count(market) or 0)
            start = 0
            while start < total:
                page = api.get_security_list(market, start) or []
                if not page:
                    break
                for item in page:
                    code = str(item.get("code", ""))
                    name = str(item.get("name", ""))
                    if len(code) == 6 and self._is_a_stock(market, code):
                        rows.append((market, code, name))
                start += 1000
        # Deduplicate while preserving order.
        seen = set()
        clean = []
        for item in rows:
            key = item[:2]
            if key not in seen:
                seen.add(key)
                clean.append(item)
        if not clean:
            raise RuntimeError("TongdaXin returned an empty A-share universe")
        self._universe = clean
        self._universe_ts = time.monotonic()
        return clean

    def _load_tdx(self) -> pd.DataFrame:
        api = self._connect_tdx()
        universe = self._load_universe(api)
        names = {(m, c): n for m, c, n in universe}
        pairs = [(m, c) for m, c, _ in universe]
        quotes: list[dict[str, Any]] = []
        for i in range(0, len(pairs), 80):
            batch = api.get_security_quotes(pairs[i:i + 80]) or []
            quotes.extend(batch)

        rows: list[dict[str, Any]] = []
        for q in quotes:
            market = int(q.get("market", 0))
            code = str(q.get("code", "")).zfill(6)
            price = float(q.get("price") or 0)
            prev = float(q.get("last_close") or 0)
            open_ = float(q.get("open") or 0)
            high = float(q.get("high") or 0)
            low = float(q.get("low") or 0)
            if price <= 0 or prev <= 0 or not self._is_a_stock(market, code):
                continue
            change = price - prev
            pct = change / prev * 100 if prev else 0.0
            amp = (high - low) / prev * 100 if prev and high and low else 0.0
            rows.append({
                "代码": code,
                "名称": names.get((market, code), ""),
                "最新价": price,
                "涨跌幅": pct,
                "涨跌额": change,
                "成交量": float(q.get("vol") or 0),
                "成交额": float(q.get("amount") or 0),
                "振幅": amp,
                "最高": high,
                "最低": low,
                "今开": open_,
                "昨收": prev,
                "量比": None,
                "换手率": None,
                "涨速": None,
                "5分钟涨跌": None,
                "总市值": None,
                "流通市值": None,
            })
        if not rows:
            raise RuntimeError("TongdaXin returned no usable A-share quotes")
        return pd.DataFrame(rows)


PROVIDER = HybridProvider()


def load_spot() -> tuple[pd.DataFrame, str]:
    return PROVIDER.load()


def provider_meta(fetched_at: str | None = None) -> dict[str, Any]:
    return PROVIDER.meta(fetched_at)
