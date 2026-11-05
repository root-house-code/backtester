"""Golden runs: reference strategies on fixed synthetic data must keep their stored results.

If a change to the engine intentionally changes results, review the diff, then
regenerate with:  UPDATE_GOLDEN=1 pytest tests/test_golden.py
"""

import json
import os
from pathlib import Path

import pytest
from helpers import STRATEGIES, rotation_frames, run

from backtester.run import metrics_json

GOLDEN = Path(__file__).parent / "golden"

CASES = {
    "buy_and_hold": dict(universe=["SPY", "QQQ", "TLT"]),
    "momentum_rotation": dict(),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_run(name):
    res = run(STRATEGIES / f"{name}.py", rotation_frames(), "2015-01-01", "2019-12-31",
              benchmark="SPY", **CASES[name])
    actual = json.loads(metrics_json(res.metrics))
    actual["trades_digest"] = res.trades[
        ["ticker", "entry_date", "exit_date", "shares", "exit_reason"]
    ].astype(str).fillna("open").values.tolist()

    path = GOLDEN / f"{name}.json"
    if os.environ.get("UPDATE_GOLDEN") or not path.exists():
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
        pytest.skip(f"wrote golden snapshot {path.name}")

    expected = json.loads(path.read_text())
    assert actual == expected
