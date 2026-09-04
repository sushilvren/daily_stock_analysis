from __future__ import annotations

from typing import Any

import pandas as pd

from strategy import market_context, score_row


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def theme_strength(df: pd.DataFrame, themes: dict[str, dict]) -> list[dict[str, Any]]:
    market = market_context(df)
    work = df.copy()
    work["代码"] = work["代码"].astype(str).str.zfill(6)
    indexed = work.set_index("代码", drop=False)
    rows: list[dict[str, Any]] = []

    for key, cfg in themes.items():
        members = []
        for c in cfg.get("codes", []):
            if c not in indexed.index:
                continue
            r = indexed.loc[c]
            if isinstance(r, pd.DataFrame):
                r = r.iloc[0]
            members.append(r)
        if not members:
            continue

        pct = pd.Series([_f(r.get("涨跌幅"), float("nan")) for r in members]).dropna()
        turnover = sum(_f(r.get("成交额")) for r in members)
        up_ratio = float((pct > 0).mean()) if len(pct) else 0.0
        median_pct = float(pct.median()) if len(pct) else 0.0
        avg_pct = float(pct.mean()) if len(pct) else 0.0
        market_median = _f(market.get("median_pct_change"))
        rel = median_pct - market_median
        core_scores = [score_row(r, market).score for r in members]
        leader_score = max(core_scores) if core_scores else 50.0

        score = 50 + max(-20, min(20, rel * 5)) + (up_ratio - 0.5) * 24 + (leader_score - 50) * 0.22
        score = max(0.0, min(100.0, score))
        leaders = sorted(
            [
                {
                    "code": str(r.get("代码")),
                    "name": r.get("名称"),
                    "pct_change": _f(r.get("涨跌幅")),
                    "turnover": _f(r.get("成交额")),
                }
                for r in members
            ],
            key=lambda x: (x["pct_change"], x["turnover"]),
            reverse=True,
        )[:3]

        rows.append({
            "theme": key,
            "label": cfg.get("label", key),
            "score": round(score, 1),
            "median_pct_change": round(median_pct, 3),
            "avg_pct_change": round(avg_pct, 3),
            "relative_to_market_median": round(rel, 3),
            "up_ratio": round(up_ratio, 3),
            "turnover": turnover,
            "members_available": len(members),
            "leaders": leaders,
        })

    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows


def opportunity_scan(df: pd.DataFrame, limit: int = 20) -> list[dict[str, Any]]:
    """Explainable intraday scanner, designed to avoid blindly chasing limit-up names."""
    market = market_context(df)
    out: list[dict[str, Any]] = []

    for _, r in df.iterrows():
        code = str(r.get("代码", "")).zfill(6)
        name = str(r.get("名称", ""))
        if not code or name.startswith("退"):
            continue
        last = _f(r.get("最新价"))
        pct = _f(r.get("涨跌幅"))
        turnover = _f(r.get("成交额"))
        ratio = _f(r.get("量比"), 1.0)
        speed = _f(r.get("涨速"))
        c5 = _f(r.get("5分钟涨跌"))
        high = _f(r.get("最高"))
        low = _f(r.get("最低"))
        open_ = _f(r.get("今开"))
        prev = _f(r.get("昨收"))
        if last <= 0 or turnover < 100_000_000:
            continue
        # Exclude most already-extreme names from the default opportunity list.
        if pct >= 9.3 or pct <= -8:
            continue

        pos = (last - low) / (high - low) if high > low else 0.5
        gap = (open_ / prev - 1) * 100 if prev > 0 and open_ > 0 else 0.0
        sig = score_row(r, market)

        tags: list[str] = []
        bonus = 0.0
        if gap < -0.5 and pct > 0.5:
            tags.append("低开转强")
            bonus += 7
        if 1 <= pct <= 7 and pos >= 0.82 and ratio >= 1.1:
            tags.append("日内强势/接近高点")
            bonus += 5
        if speed >= 0.5 or c5 >= 0.8:
            tags.append("短线加速")
            bonus += 4
        if ratio >= 1.5:
            tags.append("放量")
            bonus += 3
        if gap >= 1.5 and pct < gap * 0.35:
            tags.append("高开回落风险")
            bonus -= 8
        if pos <= 0.25:
            tags.append("靠近日内低位")
            bonus -= 5

        final = max(0.0, min(100.0, sig.score + bonus))
        if final < 62:
            continue
        out.append({
            "code": code,
            "name": name,
            "last": last,
            "pct_change": pct,
            "turnover": turnover,
            "volume_ratio": ratio,
            "speed": speed,
            "change_5m": c5,
            "intraday_position": round(pos, 3),
            "score": round(final, 1),
            "tags": tags,
            "base_signal": sig.as_dict(),
        })

    out.sort(key=lambda x: (x["score"], x["turnover"]), reverse=True)
    return out[:limit]
