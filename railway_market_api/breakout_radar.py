from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from strategy import market_context

SH_TZ = ZoneInfo("Asia/Shanghai")
_MEMORY_PATH = Path(os.getenv("BREAKOUT_MEMORY_PATH", "/tmp/a_stock_breakout_memory.json"))


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def _board(code: str) -> str:
    code = str(code).zfill(6)
    if code.startswith(("4", "8", "92")):
        return "bse"
    if code.startswith(("300", "301")):
        return "chinext"
    if code.startswith(("688", "689")):
        return "star"
    return "main"


def _limit_threshold(board: str, name: str) -> float:
    if "ST" in name.upper() or name.startswith("*ST"):
        return 4.5
    if board == "bse":
        return 28.0
    if board in {"chinext", "star"}:
        return 18.5
    return 9.2


class CapitalMemory:
    """Small rolling memory for pre-breakout activity.

    Intraday observations are folded into one record per trading date, preserving the
    day's maximum turnover/activity. The default file is best-effort persistence across
    process restarts on the same Railway container; callers should not treat it as a
    durable database.
    """

    def __init__(self, path: Path = _MEMORY_PATH, keep_days: int = 10, observe_interval: int = 300) -> None:
        self.path = path
        self.keep_days = max(5, keep_days)
        self.observe_interval = max(60, observe_interval)
        self.lock = threading.RLock()
        self.data: dict[str, dict[str, dict[str, Any]]] = {}
        self.last_observe_ts = 0.0
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data = raw
        except Exception:
            self.data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.path)
        except Exception:
            pass

    def observe(self, df: pd.DataFrame, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_observe_ts < self.observe_interval:
            return
        today = datetime.now(SH_TZ).date().isoformat()
        with self.lock:
            if not force and now - self.last_observe_ts < self.observe_interval:
                return
            for _, r in df.iterrows():
                code = str(r.get("代码", "")).zfill(6)
                if not code or code == "000000":
                    continue
                name = str(r.get("名称", ""))
                rec = {
                    "name": name,
                    "turnover_rate": _f(r.get("换手率")),
                    "turnover": _f(r.get("成交额")),
                    "volume_ratio": _f(r.get("量比"), 1.0),
                    "pct_change": _f(r.get("涨跌幅")),
                    "amplitude": _f(r.get("振幅")),
                    "float_cap": _f(r.get("流通市值")),
                }
                days = self.data.setdefault(code, {})
                old = days.get(today)
                if old:
                    rec["turnover_rate"] = max(_f(old.get("turnover_rate")), rec["turnover_rate"])
                    rec["turnover"] = max(_f(old.get("turnover")), rec["turnover"])
                    rec["volume_ratio"] = max(_f(old.get("volume_ratio"), 1.0), rec["volume_ratio"])
                    rec["amplitude"] = max(_f(old.get("amplitude")), rec["amplitude"])
                    rec["max_pct_change"] = max(_f(old.get("max_pct_change"), _f(old.get("pct_change"))), rec["pct_change"])
                    rec["min_pct_change"] = min(_f(old.get("min_pct_change"), _f(old.get("pct_change"))), rec["pct_change"])
                else:
                    rec["max_pct_change"] = rec["pct_change"]
                    rec["min_pct_change"] = rec["pct_change"]
                days[today] = rec
                for d in sorted(days.keys())[:-self.keep_days]:
                    days.pop(d, None)
            self.last_observe_ts = now
            self._save()

    def summary(self, code: str, exclude_today: bool = True) -> dict[str, Any]:
        today = datetime.now(SH_TZ).date().isoformat()
        with self.lock:
            days = dict(self.data.get(code, {}))
        items = [(d, v) for d, v in sorted(days.items()) if not (exclude_today and d == today)]
        if not items:
            return {"days": 0, "hot_days": 0, "max_turnover_rate": 0.0, "max_turnover": 0.0, "max_volume_ratio": 0.0, "max_pct_change": 0.0}
        values = [v for _, v in items[-self.keep_days :]]
        hot_days = sum(
            1
            for v in values
            if _f(v.get("turnover_rate")) >= 15
            or _f(v.get("volume_ratio"), 1.0) >= 1.8
            or _f(v.get("max_pct_change"), _f(v.get("pct_change"))) >= 7
        )
        return {
            "days": len(values),
            "hot_days": hot_days,
            "max_turnover_rate": round(max(_f(v.get("turnover_rate")) for v in values), 3),
            "max_turnover": max(_f(v.get("turnover")) for v in values),
            "max_volume_ratio": round(max(_f(v.get("volume_ratio"), 1.0) for v in values), 3),
            "max_pct_change": round(max(_f(v.get("max_pct_change"), _f(v.get("pct_change"))) for v in values), 3),
        }


MEMORY = CapitalMemory()


def breakout_radar(df: pd.DataFrame, limit: int = 20, mode: str = "close") -> list[dict[str, Any]]:
    """Rank pre-breakout candidates before the obvious limit-up stage.

    This is a quantitative pre-filter, not a buy signal. Catalyst/news purity still needs
    external confirmation. `close` emphasizes preheat and capital memory; `auction`
    gives extra weight to constructive gaps and relative strength.
    """
    mode = mode.lower().strip()
    if mode not in {"close", "auction", "intraday"}:
        mode = "close"

    MEMORY.observe(df)
    market = market_context(df)
    market_median = _f(market.get("median_pct_change"))
    out: list[dict[str, Any]] = []

    for _, r in df.iterrows():
        code = str(r.get("代码", "")).zfill(6)
        name = str(r.get("名称", ""))
        if not code or not name or name.startswith("退"):
            continue

        board = _board(code)
        last = _f(r.get("最新价"))
        prev = _f(r.get("昨收"))
        open_ = _f(r.get("今开"))
        high = _f(r.get("最高"))
        low = _f(r.get("最低"))
        pct = _f(r.get("涨跌幅"))
        turnover = _f(r.get("成交额"))
        turnover_rate = _f(r.get("换手率"))
        ratio = _f(r.get("量比"), 1.0)
        amp = _f(r.get("振幅"))
        float_cap = _f(r.get("流通市值"))
        total_cap = _f(r.get("总市值"))
        cap = float_cap or total_cap
        if last <= 0 or prev <= 0 or turnover < 15_000_000:
            continue
        if pct >= _limit_threshold(board, name) or pct <= -12:
            continue
        if cap and cap > 80_000_000_000:
            continue

        pos = (last - low) / (high - low) if high > low > 0 else 0.5
        gap = (open_ / prev - 1) * 100 if open_ > 0 else 0.0
        rel = pct - market_median
        memory = MEMORY.summary(code)

        score = 35.0
        tags: list[str] = []
        risks: list[str] = []

        if board == "bse":
            score += 10
            tags.append("北交所高弹性")
        elif board in {"chinext", "star"}:
            score += 5
            tags.append("20CM弹性")

        if cap:
            cap_yi = cap / 100_000_000
            if cap_yi <= 30:
                score += 10
                tags.append("小流通盘")
            elif cap_yi <= 80:
                score += 8
            elif cap_yi <= 150:
                score += 5
            elif cap_yi <= 300:
                score += 2
        else:
            cap_yi = None

        if 5 <= turnover_rate <= 22:
            score += 8
            tags.append("换手预热")
        elif 22 < turnover_rate <= 35:
            score += 5
            tags.append("高换手")
        elif turnover_rate > 45:
            score -= 8
            risks.append("换手过热")

        if 1.2 <= ratio < 2.5:
            score += 5
            tags.append("量比温和放大")
        elif 2.5 <= ratio <= 5:
            score += 7
            tags.append("明显放量")
        elif ratio > 7:
            score -= 3
            risks.append("量比过热")

        if turnover >= 50_000_000:
            score += 3
        if turnover >= 150_000_000:
            score += 2

        if -1.5 <= pct <= 5.5 and rel >= 0.8:
            score += 7
            tags.append("强于市场")
        elif rel >= 2.0:
            score += 4
            tags.append("相对强势")
        if 0.55 <= pos <= 0.92:
            score += 4
        elif pos > 0.92 and pct > 6:
            score -= 2
            risks.append("接近日内高潮")
        if amp >= 4:
            score += 2

        if memory["hot_days"] >= 1:
            score += 8
            tags.append("近10日资金记忆")
        if memory["max_turnover_rate"] >= 20:
            score += 5
            tags.append("历史高换手")
        if memory["max_pct_change"] >= 8:
            score += 4
            tags.append("历史强势记忆")
        if memory["hot_days"] >= 2:
            score += 3

        if memory["hot_days"] >= 1 and pct <= 4.5 and turnover_rate <= 28:
            score += 4
            tags.append("异动后未高潮")

        if mode == "auction":
            if 0.5 <= gap <= 5 and rel >= 1:
                score += 8
                tags.append("竞价超预期")
            if gap < -1 and pct > 0:
                score += 5
                tags.append("低开转强")
            if gap >= 6:
                score -= 5
                risks.append("竞价过度一致")
            if gap >= 2 and pct < gap * 0.25:
                score -= 6
                risks.append("高开承接弱")

        score = max(0.0, min(100.0, score))
        grade = "A" if score >= 78 else "B" if score >= 68 else "C"
        threshold = 64 if mode == "auction" else 66
        if score < threshold:
            continue

        out.append({
            "code": code,
            "name": name,
            "board": board,
            "last": last,
            "pct_change": round(pct, 3),
            "relative_strength": round(rel, 3),
            "turnover": turnover,
            "turnover_rate": round(turnover_rate, 3),
            "volume_ratio": round(ratio, 3),
            "amplitude": round(amp, 3),
            "float_cap_yi": round(cap_yi, 2) if cap_yi is not None else None,
            "intraday_position": round(pos, 3),
            "gap_pct": round(gap, 3),
            "score": round(score, 1),
            "grade": grade,
            "tags": tags,
            "risks": risks,
            "capital_memory": memory,
            "requires_catalyst_confirmation": True,
        })

    out.sort(key=lambda x: (x["score"], x["relative_strength"], x["turnover"]), reverse=True)
    return out[:limit]
