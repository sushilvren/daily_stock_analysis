from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import akshare as ak
import pandas as pd
import requests

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
    """Resilient public A-share quote gateway.

    Priority:
      AkShare/Eastmoney -> TDX -> Sina sh_a+sz_a -> Eastmoney direct -> stale LKG.
    Public feeds are best-effort and are not licensed Level-2/exchange real-time data.
    """

    TDX_HOSTS = [
        ("119.147.212.81", 7709),
        ("180.153.39.51", 7709),
        ("115.238.90.165", 7709),
        ("114.80.149.19", 7709),
        ("61.152.249.56", 7709),
        ("123.125.108.23", 7709),
    ]
    SINA_URL = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeData"
    )
    EASTMONEY_URL = "https://82.push2.eastmoney.com/api/qt/clist/get"

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
        self._http = requests.Session()
        self._http.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/152.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json,text/plain,*/*",
            }
        )

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value in (None, "", "--", "-", "null"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _is_a_stock(market: int, code: str) -> bool:
        if market == 1:
            return code.startswith(("600", "601", "603", "605", "688", "689"))
        return code.startswith(("000", "001", "002", "003", "300", "301"))

    def _age_seconds(self) -> float | None:
        if self.df is None or not self.cache_ts:
            return None
        return max(0.0, time.monotonic() - self.cache_ts)

    def _stale_ttl_seconds(self) -> int:
        session = _session()
        if session in {"continuous_am", "continuous_pm", "auction", "pre_open"}:
            return max(300, int(os.getenv("STALE_ACTIVE_SECONDS", "900")))
        if session in {"lunch_break", "post_close"}:
            return max(900, int(os.getenv("STALE_TRANSITION_SECONDS", "7200")))
        return max(3600, int(os.getenv("STALE_CLOSED_SECONDS", "43200")))

    def meta(self, fetched_at: str | None = None) -> dict[str, Any]:
        age = self._age_seconds()
        stale = bool(age is not None and age > self.cache_seconds)
        return {
            "source": self.source,
            "fetched_at": fetched_at or self.fetched_at or _now_iso(),
            "timezone": "Asia/Shanghai",
            "market_session": _session(),
            "provider_timestamp_available": False,
            "real_time_guarantee": False,
            "level2": False,
            "quote_depth": 5 if self.source.startswith("TongdaXin") else None,
            "is_stale": stale,
            "data_age_seconds": round(age, 1) if age is not None else None,
            "stale_ttl_seconds": self._stale_ttl_seconds(),
            "freshness_note": (
                "Public fallback feeds are best-effort. Retrieval time is not an exchange "
                "timestamp. If is_stale=true, the last-known-good snapshot is being served."
            ),
            "last_provider_error": self.last_error,
        }

    def _save(self, df: pd.DataFrame, source: str, errors: list[str] | None = None) -> None:
        df = df.copy()
        df["代码"] = df["代码"].astype(str).str.zfill(6)
        self.df = df
        self.source = source
        self.fetched_at = _now_iso()
        self.cache_ts = time.monotonic()
        self.last_error = "; ".join(errors or []) or None

    def load(self) -> tuple[pd.DataFrame, str]:
        now = time.monotonic()
        with self.lock:
            if self.df is not None and now - self.cache_ts < self.cache_seconds:
                return self.df, str(self.fetched_at)

            errors: list[str] = []
            providers = [
                ("AkShare/Eastmoney", self._load_akshare),
                ("TongdaXin/pytdx", self._load_tdx),
                ("Sina/MarketCenter", self._load_sina),
                ("Eastmoney/direct", self._load_eastmoney_direct),
            ]

            for name, loader in providers:
                try:
                    df = loader()
                    if df is not None and not df.empty:
                        self._save(df, name, errors)
                        return self.df, str(self.fetched_at)
                    errors.append(f"{name}:empty")
                except Exception as exc:
                    errors.append(f"{name}:{type(exc).__name__}:{exc}")
                    if name.startswith("TongdaXin"):
                        self._reset_tdx()

            self.last_error = "; ".join(errors)
            if self.df is not None and now - self.cache_ts < self._stale_ttl_seconds():
                base = self.source.split(" (stale)")[0]
                self.source = f"{base} (stale)"
                return self.df, str(self.fetched_at)
            raise RuntimeError(self.last_error or "all market providers unavailable")

    def _load_akshare(self) -> pd.DataFrame:
        df = ak.stock_zh_a_spot_em()
        if df is None or df.empty:
            raise RuntimeError("empty AkShare table")
        return df

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
        seen: set[tuple[int, str]] = set()
        clean: list[tuple[int, str, str]] = []
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
            quotes.extend(api.get_security_quotes(pairs[i : i + 80]) or [])

        rows: list[dict[str, Any]] = []
        for q in quotes:
            market = int(q.get("market", 0))
            code = str(q.get("code", "")).zfill(6)
            price = float(q.get("price") or 0)
            prev = float(q.get("last_close") or 0)
            high = float(q.get("high") or 0)
            low = float(q.get("low") or 0)
            if price <= 0 or prev <= 0 or not self._is_a_stock(market, code):
                continue
            rows.append(
                {
                    "代码": code,
                    "名称": names.get((market, code), ""),
                    "最新价": price,
                    "涨跌幅": (price - prev) / prev * 100,
                    "涨跌额": price - prev,
                    "成交量": float(q.get("vol") or 0),
                    "成交额": float(q.get("amount") or 0),
                    "振幅": (high - low) / prev * 100 if high and low else 0.0,
                    "最高": high,
                    "最低": low,
                    "今开": float(q.get("open") or 0),
                    "昨收": prev,
                    "量比": None,
                    "换手率": None,
                    "涨速": None,
                    "5分钟涨跌": None,
                    "总市值": None,
                    "流通市值": None,
                }
            )
        if not rows:
            raise RuntimeError("TongdaXin returned no usable A-share quotes")
        return pd.DataFrame(rows)

    def _load_sina_node(self, node: str, market: int) -> list[dict[str, Any]]:
        page_size = max(20, min(80, int(os.getenv("SINA_PAGE_SIZE", "80"))))
        max_pages = max(1, min(80, int(os.getenv("SINA_MAX_PAGES", "50"))))
        timeout = max(2.0, min(15.0, float(os.getenv("SINA_TIMEOUT_SECONDS", "6"))))
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()

        for page in range(1, max_pages + 1):
            response = self._http.get(
                self.SINA_URL,
                params={
                    "page": page,
                    "num": page_size,
                    "sort": "symbol",
                    "asc": 1,
                    "node": node,
                    "symbol": "",
                    "_s_r_a": "init" if page == 1 else "page",
                },
                headers={"Referer": "https://vip.stock.finance.sina.com.cn/"},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or not payload:
                break

            added = 0
            for item in payload:
                code = str(item.get("code", "")).zfill(6)
                if code in seen or not self._is_a_stock(market, code):
                    continue
                price = self._to_float(item.get("trade"))
                prev = self._to_float(item.get("settlement"))
                if not price or price <= 0 or not prev or prev <= 0:
                    continue
                high = self._to_float(item.get("high"))
                low = self._to_float(item.get("low"))
                change = self._to_float(item.get("pricechange"))
                pct = self._to_float(item.get("changepercent"))
                if change is None:
                    change = price - prev
                if pct is None:
                    pct = change / prev * 100
                rows.append(
                    {
                        "代码": code,
                        "名称": str(item.get("name", "")),
                        "最新价": price,
                        "涨跌幅": pct,
                        "涨跌额": change,
                        "成交量": self._to_float(item.get("volume")) or 0.0,
                        "成交额": self._to_float(item.get("amount")) or 0.0,
                        "振幅": ((high - low) / prev * 100)
                        if high is not None and low is not None
                        else None,
                        "最高": high,
                        "最低": low,
                        "今开": self._to_float(item.get("open")),
                        "昨收": prev,
                        "量比": None,
                        "换手率": self._to_float(item.get("turnoverratio")),
                        "涨速": None,
                        "5分钟涨跌": None,
                        "总市值": self._to_float(item.get("mktcap")),
                        "流通市值": self._to_float(item.get("nmc")),
                    }
                )
                seen.add(code)
                added += 1
            if added == 0 or len(payload) < page_size:
                break
        return rows

    def _load_sina(self) -> pd.DataFrame:
        rows = self._load_sina_node("sh_a", 1)
        rows.extend(self._load_sina_node("sz_a", 0))
        df = pd.DataFrame(rows)
        min_rows = max(1000, int(os.getenv("SINA_MIN_ROWS", "3500")))
        if df.empty or len(df) < min_rows:
            raise RuntimeError(
                f"Sina returned only {len(df)} usable Shanghai/Shenzhen A-share rows; "
                f"expected >= {min_rows}"
            )
        return df

    def _load_eastmoney_direct(self) -> pd.DataFrame:
        timeout = max(2.0, min(15.0, float(os.getenv("EASTMONEY_TIMEOUT_SECONDS", "8"))))
        response = self._http.get(
            self.EASTMONEY_URL,
            params={
                "pn": 1,
                "pz": 6000,
                "po": 1,
                "np": 1,
                "ut": "bd1d9ddb04089700cf9c27f6f7426281",
                "fltt": 2,
                "invt": 2,
                "fid": "f3",
                "fs": "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23",
                "fields": "f12,f14,f2,f3,f4,f5,f6,f7,f15,f16,f17,f18,f10,f8,f20,f21",
            },
            headers={"Referer": "https://quote.eastmoney.com/"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        diff = ((payload or {}).get("data") or {}).get("diff") or []
        rows: list[dict[str, Any]] = []
        for item in diff:
            code = str(item.get("f12", "")).zfill(6)
            market = 1 if code.startswith(("6", "68")) else 0
            if not self._is_a_stock(market, code):
                continue
            price = self._to_float(item.get("f2"))
            prev = self._to_float(item.get("f18"))
            if not price or price <= 0 or not prev or prev <= 0:
                continue
            rows.append(
                {
                    "代码": code,
                    "名称": str(item.get("f14", "")),
                    "最新价": price,
                    "涨跌幅": self._to_float(item.get("f3")),
                    "涨跌额": self._to_float(item.get("f4")),
                    "成交量": self._to_float(item.get("f5")) or 0.0,
                    "成交额": self._to_float(item.get("f6")) or 0.0,
                    "振幅": self._to_float(item.get("f7")),
                    "最高": self._to_float(item.get("f15")),
                    "最低": self._to_float(item.get("f16")),
                    "今开": self._to_float(item.get("f17")),
                    "昨收": prev,
                    "量比": self._to_float(item.get("f10")),
                    "换手率": self._to_float(item.get("f8")),
                    "涨速": None,
                    "5分钟涨跌": None,
                    "总市值": self._to_float(item.get("f20")),
                    "流通市值": self._to_float(item.get("f21")),
                }
            )
        if len(rows) < 1500:
            raise RuntimeError(f"Eastmoney direct returned only {len(rows)} usable A-share rows")
        return pd.DataFrame(rows)


PROVIDER = HybridProvider()


def load_spot() -> tuple[pd.DataFrame, str]:
    return PROVIDER.load()


def provider_meta(fetched_at: str | None = None) -> dict[str, Any]:
    return PROVIDER.meta(fetched_at)
