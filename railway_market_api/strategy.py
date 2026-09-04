from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import pandas as pd

DEFAULT_HOLDINGS = ["002384", "300666", "300408", "688008"]
DEFAULT_WATCH = [
    "002384", "300666", "300408", "688008",  # holdings
    "300308", "300502", "300394", "300476",  # CPO / AI hardware references
    "300570", "002463", "002837", "603283",  # software / AI app / liquid cooling refs
]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass
class Signal:
    score: float
    action: str
    risk: str
    reasons: list[str]
    triggers: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "action": self.action,
            "risk": self.risk,
            "reasons": self.reasons,
            "triggers": self.triggers,
        }


def market_context(df: pd.DataFrame) -> dict[str, Any]:
    pct = pd.to_numeric(df.get("涨跌幅"), errors="coerce")
    turnover = pd.to_numeric(df.get("成交额"), errors="coerce")
    valid = pct.dropna()
    total = int(valid.size)
    up = int((valid > 0).sum())
    down = int((valid < 0).sum())
    up_ratio = up / total if total else 0.5
    median = float(valid.median()) if total else 0.0
    strong = int((valid >= 5).sum())
    weak = int((valid <= -5).sum())

    # Cross-sectional sentiment score 0-100. Deliberately simple and explainable.
    score = 50.0
    score += (up_ratio - 0.5) * 60
    score += _clip(median, -3, 3) * 5
    score += _clip((strong - weak) / max(total, 1) * 400, -10, 10)
    score = _clip(score, 0, 100)

    if score >= 72:
        regime = "risk_on"
    elif score >= 58:
        regime = "constructive"
    elif score >= 42:
        regime = "mixed"
    elif score >= 28:
        regime = "risk_off"
    else:
        regime = "panic"

    return {
        "score": round(score, 1),
        "regime": regime,
        "up": up,
        "down": down,
        "up_ratio": round(up_ratio, 4),
        "median_pct_change": round(median, 3),
        "up_5pct": strong,
        "down_5pct": weak,
        "total_turnover": float(turnover.fillna(0).sum()),
    }


def score_row(row: pd.Series, market: dict[str, Any]) -> Signal:
    pct = _num(row.get("涨跌幅"))
    speed = _num(row.get("涨速"))
    change_5m = _num(row.get("5分钟涨跌"))
    volume_ratio = _num(row.get("量比"), 1.0)
    turnover_rate = _num(row.get("换手率"))
    amplitude = _num(row.get("振幅"))
    high = _num(row.get("最高"))
    low = _num(row.get("最低"))
    last = _num(row.get("最新价"))
    open_ = _num(row.get("今开"))
    prev_close = _num(row.get("昨收"))
    market_median = _num(market.get("median_pct_change"))

    rel = pct - market_median
    score = 50.0
    reasons: list[str] = []
    triggers: list[str] = []

    # Relative strength is more useful than absolute rise/fall intraday.
    score += _clip(rel * 4.0, -20, 20)
    if rel >= 1.5:
        reasons.append(f"相对全市场中位数强 {rel:.2f}pct")
    elif rel <= -1.5:
        reasons.append(f"相对全市场中位数弱 {abs(rel):.2f}pct")

    score += _clip(speed * 5.0, -10, 10)
    score += _clip(change_5m * 3.0, -8, 8)
    if speed > 0.5 or change_5m > 1.0:
        reasons.append("短线涨速/5分钟动量转强")
    if speed < -0.5 or change_5m < -1.0:
        reasons.append("短线动量转弱")

    if volume_ratio >= 1.5:
        score += 6
        reasons.append(f"量比放大 {volume_ratio:.2f}")
    elif 0 < volume_ratio < 0.7:
        score -= 4
        reasons.append(f"量能偏弱 {volume_ratio:.2f}")

    # Intraday location: close to high is constructive; close to low is weak.
    if high > low and last > 0:
        pos = (last - low) / (high - low)
        score += (pos - 0.5) * 14
        if pos >= 0.75:
            reasons.append("价格位于日内区间上沿")
        elif pos <= 0.25:
            reasons.append("价格位于日内区间下沿")
    else:
        pos = None

    if prev_close > 0 and open_ > 0:
        gap = (open_ / prev_close - 1) * 100
        # Penalize high-open-low-go if current return loses most of the opening gap.
        if gap >= 1.5 and pct < gap * 0.35:
            score -= 8
            reasons.append("高开后明显回落，存在兑现")
        elif gap <= -1.5 and pct > 0:
            score += 8
            reasons.append("低开后翻红，属于超预期修复")

    if amplitude >= 8:
        score -= 3
        reasons.append("日内振幅较大，风险上升")
    if turnover_rate >= 15:
        reasons.append("高换手，需防情绪化波动")

    score = _clip(score, 0, 100)

    if score >= 80:
        action = "重点持有/候选，等待确认而非追高"
        risk = "medium"
    elif score >= 68:
        action = "持有观察，强于市场"
        risk = "medium"
    elif score >= 55:
        action = "观望，等待方向确认"
        risk = "medium"
    elif score >= 42:
        action = "偏弱，反抽时评估减仓"
        risk = "medium_high"
    else:
        action = "弱势，优先控制仓位"
        risk = "high"

    triggers.extend([
        "若重新放量突破日内高点：上调强度评级",
        "若跌破日内低点且所属板块同步走弱：触发减仓风险",
        "若个股弱于板块且反抽无法收复分时均价：优先去弱",
    ])
    return Signal(score=score, action=action, risk=risk, reasons=reasons[:6], triggers=triggers)


def rank_codes(df: pd.DataFrame, codes: Iterable[str]) -> list[dict[str, Any]]:
    market = market_context(df)
    indexed = df.copy()
    indexed["代码"] = indexed["代码"].astype(str).str.zfill(6)
    indexed = indexed.set_index("代码", drop=False)
    out: list[dict[str, Any]] = []
    for code in codes:
        c = str(code).zfill(6)
        if c not in indexed.index:
            out.append({"code": c, "missing": True})
            continue
        row = indexed.loc[c]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[0]
        sig = score_row(row, market)
        out.append({
            "code": c,
            "name": row.get("名称"),
            "last": _num(row.get("最新价"), None),
            "pct_change": _num(row.get("涨跌幅"), None),
            "signal": sig.as_dict(),
        })
    out.sort(key=lambda x: x.get("signal", {}).get("score", -1), reverse=True)
    return out
