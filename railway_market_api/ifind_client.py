from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests


class IFindClient:
    BASE = "https://quantapi.51ifind.com/api/v1"

    def __init__(self) -> None:
        self.refresh_token = os.getenv("IFIND_REFRESH_TOKEN", "").strip()
        self._access_token = ""
        self._token_ts = 0.0
        self._lock = threading.Lock()
        self.last_error: str | None = None
        self.last_provider_time: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.refresh_token)

    def _get_access_token(self) -> str:
        if not self.enabled:
            raise RuntimeError("IFIND_REFRESH_TOKEN is not configured")
        with self._lock:
            # iFinD access_token is valid for 7 days; refresh locally after 12h for resilience.
            if self._access_token and time.monotonic() - self._token_ts < 12 * 3600:
                return self._access_token
            r = requests.post(
                f"{self.BASE}/get_access_token",
                headers={"Content-Type": "application/json", "refresh_token": self.refresh_token},
                timeout=10,
            )
            r.raise_for_status()
            payload = r.json()
            token = ((payload.get("data") or {}).get("access_token") if isinstance(payload, dict) else None)
            if not token:
                raise RuntimeError(f"iFinD token response missing access_token: {payload}")
            self._access_token = str(token)
            self._token_ts = time.monotonic()
            return self._access_token

    @staticmethod
    def to_thscode(code: str) -> str:
        c = str(code).strip().upper()
        if c.endswith((".SH", ".SZ", ".BJ")):
            return c
        c = c.split(".")[0].replace("SH", "").replace("SZ", "").replace("BJ", "").zfill(6)
        if c.startswith(("6", "5", "9")):
            return f"{c}.SH"
        if c.startswith(("4", "8")):
            return f"{c}.BJ"
        return f"{c}.SZ"

    @staticmethod
    def plain_code(thscode: str) -> str:
        return str(thscode).split(".")[0].zfill(6)

    def realtime(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        if not codes:
            return {}
        token = self._get_access_token()
        thscodes = [self.to_thscode(c) for c in codes]
        body = {
            "codes": ",".join(thscodes),
            "indicators": "open,high,low,latest,latestVolume,latestAmount",
        }
        r = requests.post(
            f"{self.BASE}/real_time_quotation",
            json=body,
            headers={"Content-Type": "application/json", "access_token": token, "ifindlang": "cn"},
            timeout=12,
        )
        r.raise_for_status()
        payload = r.json()
        if isinstance(payload, dict) and payload.get("errorcode") not in (None, 0):
            raise RuntimeError(f"iFinD error {payload.get('errorcode')}: {payload.get('errmsg')}")

        result: dict[str, dict[str, Any]] = {}
        tables = payload.get("tables", []) if isinstance(payload, dict) else []
        for item in tables if isinstance(tables, list) else []:
            if not isinstance(item, dict):
                continue
            thscode = str(item.get("thscode") or item.get("code") or "")
            code = self.plain_code(thscode)
            table = item.get("table") if isinstance(item.get("table"), dict) else item
            row: dict[str, Any] = {"code": code, "thscode": thscode}
            for field in ("open", "high", "low", "latest", "latestVolume", "latestAmount"):
                value = table.get(field) if isinstance(table, dict) else None
                if isinstance(value, list):
                    value = value[-1] if value else None
                row[field] = value
            times = item.get("time")
            if isinstance(times, list) and times:
                row["provider_time"] = times[-1]
                self.last_provider_time = str(times[-1])
            result[code] = row

        self.last_error = None
        return result

    def safe_realtime(self, codes: list[str]) -> dict[str, dict[str, Any]]:
        if not self.enabled:
            return {}
        try:
            return self.realtime(codes)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return {}


IFIND = IFindClient()
