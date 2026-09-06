from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "railway_market_api"
if str(API_DIR) not in sys.path:
    sys.path.insert(0, str(API_DIR))

from hithink_client import HiThinkClient  # noqa: E402


def test_thscode_mapping_supports_bse_92_prefix(monkeypatch):
    monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
    client = HiThinkClient()
    assert client.to_thscode("920176") == "920176.BJ"
    assert client.to_thscode("832000") == "832000.BJ"
    assert client.to_thscode("688001") == "688001.SH"
    assert client.to_thscode("300001") == "300001.SZ"


def test_missing_key_disables_provider(monkeypatch):
    monkeypatch.delenv("HITHINK_FINANCE_API_KEY", raising=False)
    client = HiThinkClient()
    assert client.enabled is False
    assert client.safe_snapshot(["600519"]) == []
    assert client.safe_auction_snapshot(["920176"]) == []
