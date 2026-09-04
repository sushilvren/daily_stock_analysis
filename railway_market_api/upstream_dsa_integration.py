from __future__ import annotations

"""Selected ideas adapted from ZhuLinsen/daily_stock_analysis.

This module intentionally re-implements a small, transparent subset of concepts
rather than vendoring the whole upstream project. It adds:
- market-phase context
- prediction confidence calibration hooks
- strategy auto-weighting from historical outcomes
- portfolio concentration/correlation risk helpers

Upstream: https://github.com/ZhuLinsen/daily_stock_analysis (MIT)
"""

from dataclasses import dataclass
from statistics import mean
from typing import Any, Iterable


@dataclass
class StrategyStat:
    name: str
    samples: int
    wins: int
    avg_return: float

    @property
    def win_rate(self) -> float:
        return self.wins / self.samples if self.samples else 0.0

    def weight(self, min_samples: int = 20) -> float:
        # Conservative shrinkage to 50% until enough observations accumulate.
        shrink = min(1.0, self.samples / max(1, min_samples))
        calibrated_wr = 0.5 + (self.win_rate - 0.5) * shrink
        edge = max(-0.25, min(0.25, self.avg_return / 10.0))
        return max(0.2, min(1.8, 1.0 + (calibrated_wr - 0.5) * 1.6 + edge))


def market_phase(context: dict[str, Any]) -> dict[str, Any]:
    score = float(context.get("score") or 50)
    up_ratio = float(context.get("up_ratio") or 0)
    median = float(context.get("median_pct_change") or 0)
    strong = int(context.get("strong_count") or 0)
    weak = int(context.get("weak_count") or 0)

    if score >= 75 and up_ratio >= 0.62:
        phase = "主升/发酵"
        risk = "medium"
    elif score >= 62:
        phase = "修复/转强"
        risk = "medium"
    elif score >= 48:
        phase = "震荡/分歧"
        risk = "medium-high"
    elif score >= 35:
        phase = "退潮"
        risk = "high"
    else:
        phase = "冰点/高风险"
        risk = "very-high"

    if strong > 0 and weak > strong * 2:
        risk = "high" if risk != "very-high" else risk
    if median < -1.5:
        risk = "very-high"

    return {
        "phase": phase,
        "risk": risk,
        "score": score,
        "up_ratio": up_ratio,
        "median_pct_change": median,
        "strong_count": strong,
        "weak_count": weak,
    }


def auto_weights(stats: Iterable[dict[str, Any]]) -> dict[str, float]:
    parsed = []
    for item in stats:
        try:
            parsed.append(
                StrategyStat(
                    name=str(item["name"]),
                    samples=int(item.get("samples") or 0),
                    wins=int(item.get("wins") or 0),
                    avg_return=float(item.get("avg_return") or 0),
                )
            )
        except Exception:
            continue
    if not parsed:
        return {}
    raw = {s.name: s.weight() for s in parsed}
    total = sum(raw.values()) or 1.0
    return {k: round(v / total, 4) for k, v in raw.items()}


def calibrate_confidence(raw_confidence: float, samples: int, hit_rate: float | None = None) -> float:
    raw = max(0.0, min(1.0, float(raw_confidence)))
    if samples <= 0 or hit_rate is None:
        return round(0.5 + (raw - 0.5) * 0.35, 4)
    empirical = max(0.0, min(1.0, float(hit_rate)))
    trust = min(0.8, samples / 100.0)
    return round(raw * (1 - trust) + empirical * trust, 4)


def portfolio_risk(positions: list[dict[str, Any]]) -> dict[str, Any]:
    """Simple concentration helper. Position rows accept symbol, weight, sector/theme."""
    clean = []
    for p in positions:
        try:
            w = float(p.get("weight") or 0)
        except Exception:
            continue
        if w <= 0:
            continue
        clean.append({"symbol": str(p.get("symbol") or ""), "weight": w, "sector": str(p.get("sector") or "未知")})
    total = sum(p["weight"] for p in clean)
    if total <= 0:
        return {"status": "empty", "concentration": 0, "top_weight": 0, "sector_weights": {}}
    for p in clean:
        p["weight"] /= total
    sector_weights: dict[str, float] = {}
    for p in clean:
        sector_weights[p["sector"]] = sector_weights.get(p["sector"], 0.0) + p["weight"]
    hhi = sum(p["weight"] ** 2 for p in clean)
    top_weight = max((p["weight"] for p in clean), default=0)
    top_sector = max(sector_weights.values(), default=0)
    warnings = []
    if top_weight >= 0.35:
        warnings.append("单一持仓权重偏高")
    if top_sector >= 0.60:
        warnings.append("板块/主题集中度偏高")
    if hhi >= 0.30:
        warnings.append("组合集中度偏高")
    return {
        "status": "ok",
        "concentration": round(hhi, 4),
        "top_weight": round(top_weight, 4),
        "top_sector_weight": round(top_sector, 4),
        "sector_weights": {k: round(v, 4) for k, v in sorted(sector_weights.items(), key=lambda kv: kv[1], reverse=True)},
        "warnings": warnings,
    }
