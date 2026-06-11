"""Turtle integration test — the iterative engine backtests a
Donchian-breakout strategy end-to-end on the frozen BTC/USDT 4h dataset.

Two specs derived from fixture 11:

  * System 2 — a bare 20-bar Donchian breakout, no gate. Run directly on
    the iterative engine it produces a full multi-year trade ledger
    (~200 trades): proof the custom engine handles a realistic strategy
    end to end — breakout entries, 2-ATR stops, 10-bar-low exits, equity.
  * System 1 — the same breakout gated by
    `not(prior_signal last_would_have_won)`, routed to the iterative
    engine via run_backtest (the router sends Tier-3 specs there).

RESOLVED (prior_signal extension). An earlier A.3b finding was that a
`prior_trade`-based skip-after-winner gate LATCHES SHUT after System 1's
first winning trade: a skipped breakout opens no trade, so "the most
recent completed trade" stays that winner forever and the gate never
re-opens — System 1 produced just ONE trade. The fix is `prior_signal`
plus phantom outcomes (design doc §4.7): a skipped breakout is scored
by the trade it would have produced, so the gate keeps tracking each
new breakout. System 1 now produces a full multi-year ledger — far more
than one trade — yet strictly fewer than ungated System 2, since the
gate still skips the breakouts that follow a would-have-won breakout.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
from marketmind_shared.schemas.strategy_spec import StrategySpec, Timeframe
from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.iterative import run_iterative_backtest
from marketmind_workers.backtest.metrics import compute_metrics

_FIXTURES = Path(__file__).parent / "fixtures"
_SPEC = _FIXTURES / "strategies" / "valid" / "11_turtle_system1.json"
_PARQUET = _FIXTURES / "market" / "btc_usdt_4h.parquet"
_START = datetime(2020, 1, 1, tzinfo=UTC)
_END = datetime(2026, 5, 20, tzinfo=UTC)


def test_turtle_prior_signal_skip_rule_no_longer_latches() -> None:
    system1 = StrategySpec.model_validate(json.loads(_SPEC.read_text()))
    assert system1.schema_version == "2.0"

    # System 2: the same breakout WITHOUT the skip gate — strip the
    # entry `and(...)` down to its first child (the Donchian compare).
    s2_dict = json.loads(_SPEC.read_text())
    s2_dict["entry"]["condition"] = s2_dict["entry"]["condition"]["conditions"][0]
    s2_dict["schema_version"] = "1.0"
    s2_dict["name"] = "Turtle System 2 — breakout, no skip gate"
    system2 = StrategySpec.model_validate(s2_dict)

    df = pd.read_parquet(_PARQUET)
    data = {Timeframe.H4: df}

    # System 1 — the prior_signal gate routes it to the iterative engine.
    s1_run = run_backtest(system1, _START, _END, 10_000.0, data_override=data)
    s1 = compute_metrics(s1_run, Timeframe.H4)
    # System 2 — run directly on the iterative engine for an apples-to-
    # apples comparison on the same simulator.
    s2_run = run_iterative_backtest(system2, data, _START, _END, 10_000.0)
    s2 = compute_metrics(s2_run, Timeframe.H4)

    # System 2 proves the custom engine backtests a realistic breakout
    # strategy end to end: a full multi-year ledger of real trades.
    assert s2.num_trades > 50
    assert len(s2_run.equity_curve) == len(df)

    # System 1's prior_signal gate does NOT latch — the prior_trade bug
    # this test was written to catch. It produces a full multi-year
    # ledger (>50 trades, vs the single trade prior_trade produced)...
    assert s1.num_trades > 50
    # ...yet strictly fewer than ungated System 2: the gate still skips
    # the breakouts that follow a would-have-won breakout.
    assert s1.num_trades < s2.num_trades
    assert len(s1_run.equity_curve) == len(df)
    assert s1_run.entry_diagnostics is not None
