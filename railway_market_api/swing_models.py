from __future__ import annotations

from typing import Any

import pandas as pd


def score_daily_bars(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records or len(records) < 30:
        return {"score": None, "signals": [], "risk": ["日线样本不足，至少需要30根K线"]}

    df = pd.DataFrame(records).copy()
    for c in ["open", "high", "low", "close", "volume", "amount"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close"]).reset_index(drop=True)
    if len(df) < 30:
        return {"score": None, "signals": [], "risk": ["有效日线样本不足"]}

    close = df["close"]
    high = df["high"]
    low = df["low"]
    volume = df["volume"].fillna(0)
    amount = df["amount"].fillna(0)

    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()
    vma5 = volume.rolling(5).mean()
    vma20 = volume.rolling(20).mean()

    last = len(df) - 1
    px = float(close.iloc[last])
    score = 50.0
    signals: list[str] = []
    risks: list[str] = []

    # 20-day breakout / trend continuation.
    prev20_high = float(high.iloc[max(0, last - 20):last].max())
    if px >= prev20_high and prev20_high > 0:
        score += 14
        signals.append("20日新高突破")
    elif px >= prev20_high * 0.98:
        score += 6
        signals.append("接近20日突破位")

    # MA alignment and reclaim.
    m5 = float(ma5.iloc[last]) if pd.notna(ma5.iloc[last]) else px
    m10 = float(ma10.iloc[last]) if pd.notna(ma10.iloc[last]) else px
    m20 = float(ma20.iloc[last]) if pd.notna(ma20.iloc[last]) else px
    if px > m5 > m10 > m20:
        score += 12
        signals.append("5/10/20日均线多头排列")
    elif px > m20:
        score += 4
    else:
        score -= 8
        risks.append("价格位于20日均线下方")

    # Volume confirmation.
    vv5 = float(vma5.iloc[last]) if pd.notna(vma5.iloc[last]) else 0
    vv20 = float(vma20.iloc[last]) if pd.notna(vma20.iloc[last]) else 0
    if vv20 > 0 and vv5 / vv20 >= 1.35:
        score += 8
        signals.append("近期成交量显著放大")
    elif vv20 > 0 and vv5 / vv20 < 0.7:
        score -= 4
        risks.append("近期量能偏弱")

    # High-tight consolidation proxy: strong 30d advance + shallow 10d range.
    if len(df) >= 40:
        base = float(close.iloc[last - 30])
        ret30 = (px / base - 1) * 100 if base > 0 else 0
        range10 = (float(high.iloc[last - 9:last + 1].max()) / float(low.iloc[last - 9:last + 1].min()) - 1) * 100
        if ret30 >= 20 and range10 <= 12:
            score += 9
            signals.append("强趋势后的窄幅整理结构")

    # Limit-up shakeout proxy: recent large up day, then controlled pullback and reclaim.
    pct = close.pct_change() * 100
    recent = pct.iloc[max(0, last - 10):last]
    if (recent >= 9.3).any():
        peak_idx = recent[recent >= 9.3].index[-1]
        post = close.iloc[peak_idx:last + 1]
        if len(post) >= 2:
            peak = float(post.max())
            drawdown = (float(post.min()) / peak - 1) * 100 if peak > 0 else -99
            if drawdown >= -10 and px >= float(post.iloc[0]) * 0.98:
                score += 8
                signals.append("近期涨停后回踩幅度受控并重新走强")

    # RPS-like own-trend proxy. True cross-sectional RPS is added later in nightly universe batch.
    for days, weight in [(20, 5), (60, 6), (120, 5)]:
        if len(df) > days:
            base = float(close.iloc[last - days])
            ret = (px / base - 1) * 100 if base > 0 else 0
            if ret > 15:
                score += weight
            elif ret < -10:
                score -= weight

    # Gap/distribution risk on the latest bar.
    o = float(df["open"].iloc[last]) if pd.notna(df["open"].iloc[last]) else px
    h = float(high.iloc[last]) if pd.notna(high.iloc[last]) else px
    l = float(low.iloc[last]) if pd.notna(low.iloc[last]) else px
    if h > l:
        pos = (px - l) / (h - l)
        if pos < 0.25:
            score -= 7
            risks.append("收盘位于当日区间下沿，存在兑现压力")
        elif pos > 0.75:
            score += 4
    if o > 0 and px < o * 0.97:
        score -= 5
        risks.append("当日高开低走/弱收盘特征")

    score = max(0.0, min(100.0, score))
    if score >= 82:
        grade = "A"
    elif score >= 70:
        grade = "B"
    elif score >= 58:
        grade = "C"
    elif score >= 45:
        grade = "D"
    else:
        grade = "E"

    return {
        "score": round(score, 1),
        "grade": grade,
        "signals": signals[:8],
        "risk": risks[:6],
        "metrics": {
            "last": round(px, 4),
            "ma5": round(m5, 4),
            "ma10": round(m10, 4),
            "ma20": round(m20, 4),
            "prev20_high": round(prev20_high, 4),
            "last_amount": float(amount.iloc[last]) if pd.notna(amount.iloc[last]) else None,
        },
        "note": "Sequoia-X inspired, independently implemented heuristic; not a guaranteed prediction model.",
    }
