"""Phase E.6 — pure carry (funding-harvest) probe on BTC, full gauntlet.

Runs the funding-carry strategy (perp_carry.py) through the full gauntlet at
UNCHANGED rigor (compute_overfitting_score / deflated_sharpe reused verbatim;
WF/sweep over entry_z x funding_window / MC signals from the carry engine).
Headline-specific: the STEAMROLLER DECOMPOSITION — funding-collected vs
price-PnL, worst-drawdown cause, and an MC + drop-worst fat-tail check.

USAGE: uv run python workers/scripts/phase_e6_carry_hunt.py --asset BTC
"""
from __future__ import annotations

import argparse
import math
from datetime import datetime

import numpy as np
import pandas as pd
from marketmind_shared.schemas.overfitting import (
    MonteCarloHistogramBin,
    MonteCarloResult,
    ParameterSweepResult,
    SweepAxis,
    SweepAxisKind,
    SweepCell,
    WalkForwardResult,
    WindowResult,
)
from marketmind_shared.schemas.strategy_spec import StrategySpec
from marketmind_workers.backtest.perp_carry import (
    CarryResult,
    load_perp_carry_data,
    run_perp_carry_backtest,
)
from marketmind_workers.overfitting.composite import compute_overfitting_score
from marketmind_workers.overfitting.deflated_sharpe import deflated_sharpe

_BPY = 6.0 * 365.0
_LEG_RT_BPS = 30.0
_SPEC = StrategySpec.model_validate({
    "schema_version": "1.0", "name": "perp carry", "instrument": {
        "symbol": "BTC/USDT:USDT", "exchange": "binance_usdm",
        "quote_currency": "USDT", "asset_class": "crypto_perp"},
    "primary_timeframe": "4h", "direction": "long",
    "entry": {"condition": {"type": "compare", "left": {"kind": "price", "field": "close"},
              "op": ">=", "right": {"kind": "constant", "value": 0.0}}, "order_type": "market"},
    "exit": {"exits": [{"type": "time", "max_bars_held": 1}]}})


def _pyd(x: object) -> datetime:
    return pd.Timestamp(x).to_pydatetime()  # type: ignore[arg-type]


def _metrics(res: CarryResult) -> tuple[float, float, int, float]:
    eq = pd.Series([v for _t, v in res.equity_curve])
    if len(eq) < 2 or eq.iloc[0] == 0:
        return 0.0, 0.0, len(res.trades), 2.0
    rets = eq.pct_change().dropna()
    mr, sr = float(rets.mean()), float(rets.std(ddof=1))
    sharpe = (mr * _BPY) / (sr * math.sqrt(_BPY)) if sr > 0 else 0.0
    return float(eq.iloc[-1] / eq.iloc[0] - 1.0), sharpe, len(res.trades), max((len(eq) - 1) / _BPY, 2.0)


def _max_dd(res: CarryResult) -> float:
    eq = pd.Series([v for _t, v in res.equity_curve])
    return float((eq / eq.cummax() - 1.0).min())


def _run(ohlcv: pd.DataFrame, funding: pd.Series, *, entry_z: float = 2.0,
         funding_window: int = 90) -> CarryResult:
    return run_perp_carry_backtest(ohlcv, funding, entry_z=entry_z, funding_window=funding_window)


def _walk_forward(ohlcv: pd.DataFrame, funding: pd.Series, n_windows: int = 6,
                  train_ratio: float = 0.7) -> WalkForwardResult:
    idx = pd.DatetimeIndex(ohlcv.index)
    edges = [int(i * (len(idx) - 1) / n_windows) for i in range(n_windows + 1)]
    windows: list[WindowResult] = []
    isr: list[float] = []
    oosr: list[float] = []
    for w in range(n_windows):
        lo, hi = edges[w], edges[w + 1]
        sp = lo + int(train_ratio * (hi - lo))
        ri, si, ni, _ = _metrics(_run(ohlcv.iloc[lo:sp], funding.reindex(ohlcv.iloc[lo:sp].index)))
        ro, so, no, _ = _metrics(_run(ohlcv.iloc[sp:hi], funding.reindex(ohlcv.iloc[sp:hi].index)))
        isr.append(ri)
        oosr.append(ro)
        windows.append(WindowResult(
            window_index=w, in_sample_start=_pyd(idx[lo]), in_sample_end=_pyd(idx[sp]),
            out_of_sample_start=_pyd(idx[sp]), out_of_sample_end=_pyd(idx[hi]),
            in_sample_return_pct=ri, in_sample_sharpe=si, in_sample_num_trades=ni,
            out_of_sample_return_pct=ro, out_of_sample_sharpe=so, out_of_sample_num_trades=no))
    isa, oosa = float(np.mean(isr)), float(np.mean(oosr))
    valid = isa > 0
    return WalkForwardResult(
        windows=windows, in_sample_avg_return=isa, out_of_sample_avg_return=oosa,
        degradation_ratio=(oosa / isa) if valid else 0.0, degradation_ratio_valid=valid,
        out_of_sample_positive_rate=float(np.mean([r > 0 for r in oosr])),
        consistency_score=1.0 / (1.0 + 2.0 * float(np.std(oosr))),
        train_ratio=train_ratio, n_windows_requested=n_windows, n_windows_actual=n_windows)


def _sweep(ohlcv: pd.DataFrame, funding: pd.Series) -> ParameterSweepResult:
    zs = [1.5, 2.0, 2.5]
    wins = [60, 90, 180]
    base = (2.0, 90)
    cells: list[SweepCell] = []
    rets: dict[tuple[float, int], float] = {}
    for ez in zs:
        for win in wins:
            r, s, n, _ = _metrics(_run(ohlcv, funding, entry_z=ez, funding_window=win))
            rets[(ez, win)] = r
            cells.append(SweepCell(axis_values={"entry_z": ez, "funding_window": float(win)},
                                   total_return_pct=r, sharpe_ratio=s, num_trades=n,
                                   is_baseline=(ez, win) == base))
    br = rets[base]
    neigh = [r for (ez, win), r in rets.items()
             if (ez != base[0]) ^ (win != base[1]) and (ez == base[0] or win == base[1])]
    na = float(np.mean(neigh)) if neigh else br
    allr = list(rets.values())
    return ParameterSweepResult(
        axes=[SweepAxis(kind=SweepAxisKind.RSI_UPPER_THRESHOLD, label="entry_z", values=zs,
                        baseline_value=2.0, target_paths=["carry.entry_z"]),
              SweepAxis(kind=SweepAxisKind.INDICATOR_PERIOD, label="funding_window",
                        values=[float(w) for w in wins], baseline_value=90.0,
                        target_paths=["carry.funding_window"])],
        cells=cells, baseline_return_pct=br,
        baseline_rank_percentile=float(np.mean([r <= br for r in allr])),
        best_in_grid_return=max(allr), worst_in_grid_return=min(allr), neighborhood_avg_return=na,
        peakiness_score=max(0.0, min(1.0, (br - na) / max(abs(br), 0.01))),
        n_combinations=len(cells), skipped_reason=None)


def _monte_carlo(ohlcv: pd.DataFrame, funding: pd.Series, real_ret: float, real_sharpe: float,
                 n: int = 100, seed: int = 42) -> MonteCarloResult:
    """Permute the price log-returns (keep funding fixed) — the carry's entry/
    exit TIMING is funding-driven and unchanged, so this isolates whether the
    REAL price ordering at funding-extreme times hurts (the steamroller)."""
    rng = np.random.default_rng(seed)
    close = ohlcv["close"].to_numpy()
    lr = np.diff(np.log(close))
    synth: list[float] = []
    for _ in range(n):
        shuf = close[0] * np.exp(np.cumsum(np.concatenate([[0.0], rng.permutation(lr)])))
        syn = ohlcv.copy()
        syn["open"] = syn["high"] = syn["low"] = syn["close"] = syn["mark_close"] = shuf
        r, *_ = _metrics(_run(syn, funding))
        synth.append(r)
    arr = np.array(synth)
    counts, edges = np.histogram(arr, bins=24)
    return MonteCarloResult(
        real_return_pct=real_ret, real_sharpe=real_sharpe, n_permutations=n,
        synthetic_mean_return=float(arr.mean()), synthetic_std_return=float(arr.std(ddof=1)),
        synthetic_min=float(arr.min()), synthetic_max=float(arr.max()),
        histogram=[MonteCarloHistogramBin(lo=float(edges[i]), hi=float(edges[i + 1]),
                                          count=int(counts[i])) for i in range(len(counts))],
        p_value=float(np.mean(arr >= real_ret)), percentile_rank=float(np.mean(arr < real_ret)), seed=seed)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", default="BTC")
    args = ap.parse_args()
    ohlcv, funding = load_perp_carry_data(args.asset)
    idx = pd.DatetimeIndex(ohlcv.index)
    print(f"=== E.6 PURE CARRY probe — {args.asset} perp 4h, {len(ohlcv)} bars "
          f"{_pyd(idx[0]).date()}->{_pyd(idx[-1]).date()} ===")

    res = _run(ohlcv, funding)
    tot, sharpe, ntr, t_years = _metrics(res)
    freq = ntr / t_years
    drag = freq * _LEG_RT_BPS * 1e-4
    # per-trade funding vs round-trip cost (the cost-sanity heart of carry)
    fund_per_trade = res.total_funding / max(ntr, 1)
    cost_per_trade = res.total_cost / max(ntr, 1)
    print("\n--- COST-SANITY (carry: funding receipts must clear the round-trip) ---")
    print(f"  {freq:.0f} trades/yr x {_LEG_RT_BPS}bps RT = {drag*100:.1f}%/yr cost drag")
    print(f"  funding collected/trade = {fund_per_trade:+.1f}  vs  cost/trade = {cost_per_trade:.1f}  "
          f"-> funding {'CLEARS' if fund_per_trade > cost_per_trade else 'does NOT clear'} cost")

    print(f"\n--- BACKTEST (full {t_years:.1f}y) ---")
    print(f"  trades={ntr} ({freq:.0f}/yr) return={tot:+.1%} sharpe={sharpe:.2f} max_dd={_max_dd(res):.1%}")
    print(f"  STEAMROLLER DECOMPOSITION: funding_collected={res.total_funding:+.0f}  "
          f"price_pnl={res.total_price_pnl:+.0f}  cost={-res.total_cost:+.0f}  "
          f"net={res.final_equity-res.initial_capital:+.0f}")
    exits: dict[str, int] = {}
    for t in res.trades:
        exits[t.exit_reason] = exits.get(t.exit_reason, 0) + 1
    print(f"  exits: {exits}  collected_funding_on {sum(t.funding_pnl>0 for t in res.trades)}/{ntr} trades")
    # empirical trade: a representative stop (steamroller candidate) + a normalize
    for reason in ("stop", "normalize"):
        t = next((x for x in res.trades if x.exit_reason == reason), None)
        if t:
            side = "SHORT(collect+funding)" if t.direction < 0 else "LONG(collect-funding)"
            print(f"  EXAMPLE [{reason}]: {side} {str(t.entry_time)[:13]}->{str(t.exit_time)[:13]} "
                  f"funding={t.funding_pnl:+.1f} price={t.price_pnl:+.1f} cost={t.cost:.1f} NET={t.net_pnl:+.1f}")
    # steamroller fat-tail: drop the 5 worst price trades, recompute net
    worst = sorted(res.trades, key=lambda x: x.price_pnl)[:5]
    print(f"  worst-5 price moves: {[round(w.price_pnl) for w in worst]} "
          f"(net of those 5 = {sum(w.net_pnl for w in worst):+.0f}; rest = {res.final_equity-res.initial_capital-sum(w.net_pnl for w in worst):+.0f})")

    print("\n--- GAUNTLET (unchanged scoring) ---")
    wf = _walk_forward(ohlcv, funding)
    sweep = _sweep(ohlcv, funding)
    mc = _monte_carlo(ohlcv, funding, tot, sharpe)
    ds = deflated_sharpe(sharpe, n_trials_estimate=100, n_observations=round(t_years),
                         returns_skewness=0.0, returns_kurtosis=3.0)
    score = compute_overfitting_score(_SPEC, walk_forward=wf, sweep=sweep, monte_carlo=mc, deflated=ds)
    print(f"  walk-forward: degradation={wf.degradation_ratio:.3f} valid={wf.degradation_ratio_valid} "
          f"oos_pos={sum(w.out_of_sample_return_pct>0 for w in wf.windows)}/6")
    print(f"  sweep: peakiness={sweep.peakiness_score:.3f} best={sweep.best_in_grid_return:+.1%}")
    print(f"  monte-carlo: p={mc.p_value:.3f} (price-permute; real={mc.real_return_pct:+.1%} synth_mean={mc.synthetic_mean_return:+.1%})")
    print(f"  deflated-shp: prob_real={ds.probability_strategy_is_real:.3f}")
    print(f"  COMPOSITE: {score.score:.2f}/100 verdict={score.verdict.value}")

    cost_sanity = "PASS" if (tot > drag and fund_per_trade > cost_per_trade) else (
        "MARGINAL" if tot > 0 else "FAIL")
    seed = score.verdict.value == "likely_robust" and score.score < 30.0 and cost_sanity == "PASS" and sharpe > 0
    print("\n--- SEED/REJECT ---")
    print(f"  cost-sanity={cost_sanity}  {'SEED' if seed else 'REJECT'} — composite {score.score:.2f} "
          f"({score.verdict.value}); seed only likely_robust + cost-sanity PASS + steamroller-survivable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
