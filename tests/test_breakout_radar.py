from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "railway_market_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from breakout_radar import MEMORY, breakout_radar  # noqa: E402


def _row(code: str, name: str, pct: float, cap_yi: float, turnover_rate: float, ratio: float, turnover_yi: float):
    prev = 10.0
    last = prev * (1 + pct / 100)
    return {
        "代码": code,
        "名称": name,
        "最新价": last,
        "涨跌幅": pct,
        "成交额": turnover_yi * 100_000_000,
        "换手率": turnover_rate,
        "量比": ratio,
        "振幅": 5.0,
        "流通市值": cap_yi * 100_000_000,
        "总市值": cap_yi * 1.3 * 100_000_000,
        "今开": prev * 1.01,
        "昨收": prev,
        "最高": last * 1.02,
        "最低": prev * 0.985,
    }


def test_bse_small_cap_preheat_ranks_above_large_cap(tmp_path):
    old_path = MEMORY.path
    old_data = MEMORY.data
    old_ts = MEMORY.last_observe_ts
    try:
        MEMORY.path = tmp_path / "memory.json"
        MEMORY.data = {}
        MEMORY.last_observe_ts = 0.0
        df = pd.DataFrame([
            _row("920176", "维琪样本", 3.2, 18, 14, 2.0, 1.2),
            _row("600000", "大盘样本", 3.2, 500, 3, 1.1, 8.0),
            _row("000001", "市场样本", -1.0, 300, 2, 0.9, 4.0),
        ])
        rows = breakout_radar(df, limit=10, mode="close")
        assert rows
        assert rows[0]["code"] == "920176"
        assert rows[0]["board"] == "bse"
        assert "北交所高弹性" in rows[0]["tags"]
    finally:
        MEMORY.path = old_path
        MEMORY.data = old_data
        MEMORY.last_observe_ts = old_ts
