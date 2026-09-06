from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests


class HiThinkClient:
    """Lightweight adapter for HiThink's official Financial API.

    The API key is read only from HITHINK_FINANCE_API_KEY. Missing credentials disable
    the provider cleanly so AkShare/TongdaXin remain available as fallbacks.
    """

    def __init__(self) -> None:
        self.base_url = os.getenv("HITHINK_FINANCE_BASE_URL", "https://fuyao.aicubes.cn").rstrip("/")
        self.api_key = os.getenv("HITHINK_FINANCE_API_KEY", "").strip()
        self.timeout = max(3.0, float(os.getenv("HITHINK_FINANCE_TIMEOUT_SECONDS", "10")))
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self.last_request_id: str | None = None
        self.last_success_ts: float | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    @staticmethod
    def to_thscode(code: str) -> str:
        c = str(code).strip().upper()
        if c.endswith((".SH", ".SZ", ".BJ")):
            return c
        c = c.split(".")[0].replace("SH", "").replace("SZ", "").replace("BJ", "").zfill(6)
        if c.startswith(("4", "8", "92")):
            return f"{c}.BJ"
        if c.startswith(("6", "5", "9")):
            return f"{c}.SH"
        return f"{c}.SZ"

    @staticmethod
    def plain_code(thscode: str) -> str:
        return str(thscode).split(".")[0].zfill(6)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.enabled:
            raise RuntimeError("HITHINK_FINANCE_API_KEY is not configured")
        headers = {"X-api-key": self.api_key, "Accept": "application/json"}
        attempts = 3
        delay = 0.6
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                r = requests.get(
                    f"{self.base_url}{path}",
                    params={k: v for k, v in (params or {}).items() if v is not None},
                    headers=headers,
                    timeout=self.timeout,
                )
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt + 1 < attempts:
                        time.sleep(delay)
                        delay *= 2
                        continue
                r.raise_for_status()
                payload = r.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("HiThink response is not a JSON object")
                self.last_request_id = str(payload.get("request_id") or "") or None
                code = payload.get("code")
                if code != 0:
                    if code in {4001, 5001, 5002, 5003} and attempt + 1 < attempts:
                        time.sleep(delay)
                        delay *= 2
                        continue
                    raise RuntimeError(f"HiThink business error {code}: {payload.get('message')}")
                with self._lock:
                    self.last_error = None
                    self.last_success_ts = time.time()
                return payload.get("data")
            except Exception as exc:
                last_exc = exc
                if attempt + 1 < attempts and isinstance(exc, requests.RequestException):
                    time.sleep(delay)
                    delay *= 2
                    continue
                break
        with self._lock:
            self.last_error = f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown error"
        raise RuntimeError(self.last_error)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "base_url": self.base_url,
            "last_request_id": self.last_request_id,
            "last_success_ts": self.last_success_ts,
            "last_error": self.last_error,
        }

    def snapshot(self, codes: list[str]) -> list[dict[str, Any]]:
        if not codes:
            return []
        thscodes = [self.to_thscode(c) for c in codes[:100]]
        data = self._get("/api/a-share/prices/snapshot", {"thscodes": ",".join(thscodes)}) or {}
        return list(data.get("item") or []) if isinstance(data, dict) else []

    def auction_snapshot(self, codes: list[str], stage: str = "final") -> list[dict[str, Any]]:
        if not codes:
            return []
        stage = "live" if str(stage).lower() == "live" else "final"
        thscodes = [self.to_thscode(c) for c in codes[:100]]
        data = self._get(
            "/api/a-share/auction/snapshot",
            {"thscodes": ",".join(thscodes), "stage": stage},
        ) or {}
        return list(data.get("item") or []) if isinstance(data, dict) else []

    def valuations(self, codes: list[str]) -> list[dict[str, Any]]:
        if not codes:
            return []
        thscodes = [self.to_thscode(c) for c in codes[:100]]
        data = self._get("/api/a-share/valuations/snapshot", {"thscodes": ",".join(thscodes)}) or {}
        return list(data.get("item") or []) if isinstance(data, dict) else []

    def limit_up_pool(self, date_ms: int | None = None, size: int = 100) -> Any:
        return self._get(
            "/api/a-share/special-data/limit-up-pool",
            {"date_ms": date_ms, "page": 1, "size": max(1, min(int(size), 200)), "sort_field": "limit_up_time", "sort_dir": "asc"},
        )

    def limit_up_ladder(self) -> Any:
        return self._get("/api/a-share/special-data/limit-up-ladder")

    def anomaly_list(self, tag_codes: str | None = None) -> Any:
        return self._get("/api/a-share/special-data/anomaly-analysis-list", {"tag_codes": tag_codes})

    def hot_stock_list(self, period: str = "hour") -> Any:
        normalized = "day" if str(period).lower() == "day" else "hour"
        return self._get("/api/a-share/special-data/hot-stock-list", {"period": normalized})

    def dragon_tiger(self, board_type: str = "all", date: str | None = None) -> Any:
        normalized = str(board_type).lower()
        if normalized not in {"all", "org", "hot_money"}:
            normalized = "all"
        return self._get(
            "/api/a-share/special-data/dragon-tiger-list",
            {"board_type": normalized, "date": date},
        )

    def safe_auction_snapshot(self, codes: list[str], stage: str = "final") -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        try:
            return self.auction_snapshot(codes, stage=stage)
        except Exception:
            return []

    def safe_snapshot(self, codes: list[str]) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        try:
            return self.snapshot(codes)
        except Exception:
            return []


HITHINK = HiThinkClient()
