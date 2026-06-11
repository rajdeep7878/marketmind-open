"""A.4 overfitting regression gate — the two production v1 strategies must
walk-forward bit-identically before and after the A.4 changes.

A.4 restructures `walk_forward.py`: the cold per-segment path is extracted
into `_run_windows_cold` and a new continuous-run path is added for
stateful (v2) specs. BB Breakout and Golden Cross are v1 (non-stateful)
specs — `spec_uses_stateful_v2` is false — so they take the *unchanged*
cold path and must produce identical walk-forward results. The reference
`WalkForwardResult`s in `fixtures/strategies/regression/
*_walkforward_reference.json` were captured from the pre-A.4 engine; a
drift here is a stop-and-report blocker, not a test to relax.

Scope. This gate covers `walk_forward.py` — the only overfitting module
A.4 restructures. `monte_carlo.py` and `parameter_sweep.py` are
byte-untouched by A.4, so their v1 outputs are bit-identical by
construction. `composite.py` is changed, but a v1 spec keeps the original
0.35/0.25/0.25/0.15 weights — regression-gated by `test_composite.py`'s
v1-spec tests (`test_v1_spec_keeps_original_composite_weights` and the
exact-score assertions).

Determinism: the frozen BTC/USDT 4h dataset is fed in by monkeypatching
`get_market_data` to date-slice it. `walk_forward.py` fetches per segment
through `run_backtest`, so the `data_override` hook used by
`test_backtest_regression.py` is not reachable here; date-slicing the
frozen frame is what the real cached fetch would have returned.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pandas as pd
import pytest
from marketmind_shared.schemas.strategy_spec import StrategySpec
from marketmind_workers.backtest import engine as engine_module
from marketmind_workers.overfitting.walk_forward import run_walk_forward

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
_REG = _FIXTURES / "strategies" / "regression"
_PARQUET = _FIXTURES / "market" / "btc_usdt_4h.parquet"
_START = datetime(2020, 1, 1, tzinfo=UTC)
_END = datetime(2026, 5, 20, tzinfo=UTC)


@pytest.mark.parametrize("strategy", ["bb_breakout", "golden_cross"])
def test_v1_walk_forward_is_bit_identical(
    strategy: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = StrategySpec.model_validate(json.loads((_REG / f"{strategy}.json").read_text()))
    reference = json.loads((_REG / f"{strategy}_walkforward_reference.json").read_text())

    frozen = pd.read_parquet(_PARQUET)

    def _get_market_data(
        _symbol: object, _tf: object, start: datetime, end: datetime, **_kw: object
    ) -> pd.DataFrame:
        return cast("pd.DataFrame", frozen.loc[start:end])

    monkeypatch.setattr(engine_module, "get_market_data", _get_market_data)

    wf = run_walk_forward(spec, _START, _END)
    actual = json.loads(json.dumps(wf.model_dump(mode="json"), sort_keys=True))

    assert actual == reference, (
        f"{strategy} walk-forward metrics drifted from the pre-A.4 baseline.\n"
        "v1 (non-stateful) specs must take the unchanged cold per-segment "
        "path — this is the A.4 regression gate, a non-negotiable "
        "stop-and-report blocker."
    )
