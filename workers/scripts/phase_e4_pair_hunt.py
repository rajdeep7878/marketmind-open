"""Phase E.4 — the BTC/ETH market-neutral log-spread MR hunt.

The first trustworthy verdict on whether a market-neutral PAIR delivers the
fast-trades-with-edge that every directional shape (fast trend, fast MR,
equity ORB) failed to. Runs the E.3 perp-pair engine (funding-on-mark,
verified) through the FULL gauntlet at UNCHANGED rigor — the scoring
(`compute_overfitting_score`, `deflated_sharpe`, the 30/60 verdict bands, the
0.35/0.25/0.25/0.15 v1 weights) is reused VERBATIM; only the four input
signals are recomputed from the multi-leg engine (which has no vbt parity
partner — drift-parity is N/A for the multi-leg path by construction, per E.3).

USAGE: uv run python workers/scripts/phase_e4_pair_hunt.py
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import UTC, datetime

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
from marketmind_shared.schemas.strategy_spec.validator import validate_spec
from marketmind_workers.backtest.perp_pairs import (
    LegData,
    build_spread,
    load_perp_pair_data,
    run_perp_pair_backtest,
    spread_zscore,
)
from marketmind_workers.overfitting.composite import compute_overfitting_score
from marketmind_workers.overfitting.deflated_sharpe import deflated_sharpe

_BPY = 24.0 * 365.0  # 1h bars per year = 8760 (matches metrics._BARS_PER_YEAR[H1])
_PAIR_RT_BPS = 60.0  # E.3: crypto_perp 30bps/leg round-trip x 2 legs


# --------------------------------------------------------------------------- #
#  Spec
# --------------------------------------------------------------------------- #
def build_pair_spec(*, zscore_period: int = 168, entry_z: float = 2.5,
                    exit_z: float = 0.5, stop_z: float = 4.0,
                    corr_period: int = 168, corr_min: float = 0.3) -> StrategySpec:
    spec_dict = {
        "schema_version": "1.0",
        "name": "BTC/ETH perp log-spread mean-reversion (market-neutral)",
        "description": (
            "Long ETH-perp / short BTC-perp (and inverse) on the log-spread "
            "z-score; unlevered, funding-on-mark, divergence stop + correlation "
            "regime gate. Market-neutral relative-value. BACKTEST-ONLY."
        ),
        "instrument": {"symbol": "ETH/USDT:USDT", "exchange": "binance_usdm",
                       "quote_currency": "USDT", "asset_class": "crypto_perp"},
        "primary_timeframe": "1h", "direction": "long",
        # entry/exit are required-but-placeholder for a multi-leg spec — the
        # spread config drives the signal (documented E.3 design choice).
        "entry": {"condition": {"type": "compare", "left": {"kind": "price", "field": "close"},
                  "op": ">=", "right": {"kind": "constant", "value": 0.0}}, "order_type": "market"},
        "exit": {"exits": [{"type": "time", "max_bars_held": 1}]},
        "position_sizing": {"mode": "fixed_percent_equity", "percent": 1.0},
        "costs": {"funding_model": "binance_8h"},
        "legs": [{"instrument": {"symbol": "BTC/USDT:USDT", "exchange": "binance_usdm",
                  "quote_currency": "USDT", "asset_class": "crypto_perp"},
                  "direction": "short", "weight": 1.0}],
        "spread": {"method": "log", "zscore_period": zscore_period, "entry_z": entry_z,
                   "exit_z": exit_z, "stop_z": stop_z,
                   "corr_period": corr_period, "corr_min": corr_min},
    }
    spec, _w = validate_spec(spec_dict)
    return spec


# --------------------------------------------------------------------------- #
#  Metrics from the perp equity curve (replicates metrics.py Sharpe at bpy)
# --------------------------------------------------------------------------- #
def _metrics(res: object) -> tuple[float, float, int, int, float]:
    """(total_return, annualized_sharpe, n_trades, bars, t_years)."""
    eq = pd.Series([v for _t, v in res.equity_curve])  # type: ignore[attr-defined]
    if len(eq) < 2 or eq.iloc[0] == 0:
        return 0.0, 0.0, len(res.trades), len(eq), 2.0  # type: ignore[attr-defined]
    total_return = float(eq.iloc[-1] / eq.iloc[0] - 1.0)
    rets = eq.pct_change().dropna()
    mean_r, std_r = float(rets.mean()), float(rets.std(ddof=1))
    sharpe = (mean_r * _BPY) / (std_r * math.sqrt(_BPY)) if std_r > 0 else 0.0
    bars = len(eq) - 1
    return total_return, sharpe, len(res.trades), bars, max(bars / _BPY, 2.0)  # type: ignore[attr-defined]


def _run(spec: StrategySpec, legs: list[LegData], start: datetime | None = None,
         end: datetime | None = None) -> object:
    return run_perp_pair_backtest(spec, legs, start=start, end=end, initial_capital=10_000.0)


def _pyd(x: object) -> datetime:
    """DatetimeIndex element -> python datetime (pandas-stubs type the element
    as DatetimeIndex, so go through pd.Timestamp)."""
    return pd.Timestamp(x).to_pydatetime()  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
#  Walk-forward (6 windows, 70/30 IS/OOS, cold backtest per segment — v1 shape)
# --------------------------------------------------------------------------- #
def _walk_forward(spec: StrategySpec, legs: list[LegData], idx: pd.DatetimeIndex,
                  n_windows: int = 6, train_ratio: float = 0.7) -> WalkForwardResult:
    edges = [idx[int(i * (len(idx) - 1) / n_windows)] for i in range(n_windows + 1)]
    windows: list[WindowResult] = []
    is_rets: list[float] = []
    oos_rets: list[float] = []
    for w in range(n_windows):
        w_lo, w_hi = edges[w], edges[w + 1]
        split = idx[int((idx.get_indexer([w_lo])[0]) + train_ratio *
                        (idx.get_indexer([w_hi])[0] - idx.get_indexer([w_lo])[0]))]
        is_res = _run(spec, legs, _pyd(w_lo), _pyd(split))
        oos_res = _run(spec, legs, _pyd(split), _pyd(w_hi))
        is_r, is_s, is_n, *_ = _metrics(is_res)
        oos_r, oos_s, oos_n, *_ = _metrics(oos_res)
        is_rets.append(is_r)
        oos_rets.append(oos_r)
        windows.append(WindowResult(
            window_index=w,
            in_sample_start=_pyd(w_lo), in_sample_end=_pyd(split),
            out_of_sample_start=_pyd(split), out_of_sample_end=_pyd(w_hi),
            in_sample_return_pct=is_r, in_sample_sharpe=is_s, in_sample_num_trades=is_n,
            out_of_sample_return_pct=oos_r, out_of_sample_sharpe=oos_s,
            out_of_sample_num_trades=oos_n))
    is_avg = float(np.mean(is_rets))
    oos_avg = float(np.mean(oos_rets))
    valid = is_avg > 0
    degradation = (oos_avg / is_avg) if valid else 0.0
    return WalkForwardResult(
        windows=windows, in_sample_avg_return=is_avg, out_of_sample_avg_return=oos_avg,
        degradation_ratio=degradation, degradation_ratio_valid=valid,
        out_of_sample_positive_rate=float(np.mean([r > 0 for r in oos_rets])),
        consistency_score=1.0 / (1.0 + 2.0 * float(np.std(oos_rets))),
        train_ratio=train_ratio, n_windows_requested=n_windows, n_windows_actual=n_windows)


# --------------------------------------------------------------------------- #
#  Parameter sweep over zscore_period x entry_z x exit_z (peakiness vs neighbors)
# --------------------------------------------------------------------------- #
def _sweep(legs: list[LegData], base: dict[str, float]) -> ParameterSweepResult:
    grid = {
        "zscore_period": [84, 168, 336],
        "entry_z": [2.0, 2.5, 3.0],
        "exit_z": [0.25, 0.5, 1.0],
    }
    cells: list[SweepCell] = []
    returns: dict[tuple[float, float, float], float] = {}
    for zp in grid["zscore_period"]:
        for ez in grid["entry_z"]:
            for xz in grid["exit_z"]:
                spec = build_pair_spec(zscore_period=int(zp), entry_z=ez, exit_z=xz)
                r, s, n, *_ = _metrics(_run(spec, legs))
                is_base = (zp == base["zscore_period"] and ez == base["entry_z"]
                           and xz == base["exit_z"])
                returns[(zp, ez, xz)] = r
                cells.append(SweepCell(
                    axis_values={"zscore_period": zp, "entry_z": ez, "exit_z": xz},
                    total_return_pct=r, sharpe_ratio=s, num_trades=n, is_baseline=is_base))
    base_key = (base["zscore_period"], base["entry_z"], base["exit_z"])
    base_ret = returns[base_key]
    # immediate neighbors: differ from baseline by one grid step on exactly one axis
    neigh: list[float] = []
    for (zp, ez, xz), r in returns.items():
        diffs = sum(1 for a, b in zip((zp, ez, xz), base_key, strict=True) if a != b)
        if diffs == 1:
            neigh.append(r)
    neigh_avg = float(np.mean(neigh)) if neigh else base_ret
    peak = max(0.0, min(1.0, (base_ret - neigh_avg) / max(abs(base_ret), 0.01)))
    all_rets = list(returns.values())
    rank = float(np.mean([r <= base_ret for r in all_rets]))
    axes = [
        SweepAxis(kind=SweepAxisKind.INDICATOR_PERIOD, label="zscore_period",
                  values=[float(x) for x in grid["zscore_period"]],
                  baseline_value=base["zscore_period"], target_paths=["spread.zscore_period"]),
        SweepAxis(kind=SweepAxisKind.RSI_UPPER_THRESHOLD, label="entry_z",
                  values=[float(x) for x in grid["entry_z"]], baseline_value=base["entry_z"],
                  target_paths=["spread.entry_z"]),
        SweepAxis(kind=SweepAxisKind.RSI_LOWER_THRESHOLD, label="exit_z",
                  values=[float(x) for x in grid["exit_z"]], baseline_value=base["exit_z"],
                  target_paths=["spread.exit_z"]),
    ]
    return ParameterSweepResult(
        axes=axes, cells=cells, baseline_return_pct=base_ret, baseline_rank_percentile=rank,
        best_in_grid_return=max(all_rets), worst_in_grid_return=min(all_rets),
        neighborhood_avg_return=neigh_avg, peakiness_score=peak,
        n_combinations=len(cells), skipped_reason=None)


# --------------------------------------------------------------------------- #
#  Monte Carlo: joint-permute both legs' log-returns (preserve contemporaneous
#  coupling, DESTROY the temporal mean-reversion the edge depends on).
# --------------------------------------------------------------------------- #
def _permute_legs(legs: list[LegData], rng: np.random.Generator) -> list[LegData]:
    a, b = legs[0], legs[1]
    idx = a.last.index
    a_c, b_c = a.last["close"].to_numpy(), b.last["close"].to_numpy()
    a_ret = np.diff(np.log(a_c))
    b_ret = np.diff(np.log(b_c))
    perm = rng.permutation(len(a_ret))  # JOINT — same shuffle for both legs
    a_new = np.empty_like(a_c)
    b_new = np.empty_like(b_c)
    a_new[0], b_new[0] = a_c[0], b_c[0]
    a_new[1:] = a_c[0] * np.exp(np.cumsum(a_ret[perm]))
    b_new[1:] = b_c[0] * np.exp(np.cumsum(b_ret[perm]))
    out: list[LegData] = []
    for leg, new in ((a, a_new), (b, b_new)):
        last = pd.DataFrame({"open": new, "high": new, "low": new, "close": new,
                             "volume": leg.last["volume"].to_numpy()}, index=idx)
        out.append(replace(leg, last=last, mark_close=pd.Series(new, index=idx)))
    return out


def _monte_carlo(spec: StrategySpec, legs: list[LegData], real_return: float,
                 real_sharpe: float, n: int = 100, seed: int = 42) -> MonteCarloResult:
    rng = np.random.default_rng(seed)
    synth: list[float] = []
    for _ in range(n):
        r, *_ = _metrics(_run(spec, _permute_legs(legs, rng)))
        synth.append(r)
    arr = np.array(synth)
    p_value = float(np.mean(arr >= real_return))
    counts, edges = np.histogram(arr, bins=24)
    hist = [MonteCarloHistogramBin(lo=float(edges[i]), hi=float(edges[i + 1]),
                                   count=int(counts[i])) for i in range(len(counts))]
    return MonteCarloResult(
        real_return_pct=real_return, real_sharpe=real_sharpe, n_permutations=n,
        synthetic_mean_return=float(arr.mean()), synthetic_std_return=float(arr.std(ddof=1)),
        synthetic_min=float(arr.min()), synthetic_max=float(arr.max()), histogram=hist,
        p_value=p_value, percentile_rank=float(np.mean(arr < real_return)), seed=seed)


# --------------------------------------------------------------------------- #
#  Empirical inspection (mandatory) — one representative market-neutral trade
# --------------------------------------------------------------------------- #
def _inspect(spec: StrategySpec, legs: list[LegData], res: object) -> None:
    a, b = legs[0], legs[1]
    from typing import cast
    spread = build_spread(cast("pd.Series", a.last["close"]),
                          cast("pd.Series", b.last["close"]), "log")
    z = spread_zscore(spread, spec.spread.zscore_period)  # type: ignore[union-attr]
    trades = res.trades  # type: ignore[attr-defined]
    # pick a representative reversion round-trip (not the forced end-of-data one)
    t = next((x for x in trades if x.exit_reason in ("reversion", "stop")), trades[0] if trades else None)
    if t is None:
        print("  (no trades)")
        return
    et, xt = pd.Timestamp(t.entry_time), pd.Timestamp(t.exit_time)
    z_entry = float(z.loc[et]) if et in z.index else float("nan")
    z_exit = float(z.loc[xt]) if xt in z.index else float("nan")
    fl = [r for r in res.funding_ledger if et <= pd.Timestamp(r.timestamp) <= xt]  # type: ignore[attr-defined]
    print("  EMPIRICAL ROUND-TRIP (market-neutral cross-check):")
    print(f"    ENTRY {et}  side={'LONG-spread (long ETH/short BTC)' if t.side>0 else 'SHORT-spread (short ETH/long BTC)'}")
    print(f"      z@entry={z_entry:+.2f} (|z|>=entry_z={spec.spread.entry_z})  "  # type: ignore[union-attr]
          f"ETH={float(a.last['close'].loc[et]):.2f} BTC={float(b.last['close'].loc[et]):.2f}")
    print(f"    FUNDING while held: {len(fl)} accruals, net={sum(r.cashflow for r in fl):+.2f} "
          f"(on MARK, sign-correct per leg)")
    if fl:
        r0 = fl[0]
        print(f"      e.g. {str(r0.timestamp)[:16]} {r0.leg_symbol} qty={r0.signed_qty:+.3f} "
              f"mark={r0.mark_price:.2f} rate={r0.funding_rate*1e4:+.2f}bps -> {r0.cashflow:+.3f}")
    print(f"    EXIT  {xt}  reason={t.exit_reason}  z@exit={z_exit:+.2f}")
    print(f"    PnL: price={t.price_pnl:+.2f} funding={t.funding_pnl:+.2f} cost={t.cost:.2f} "
          f"-> NET {t.net_pnl:+.2f}")
    # market-neutrality: ETH and BTC both moved; check directional component ~cancels
    eth_chg = float(a.last['close'].loc[xt] / a.last['close'].loc[et] - 1)
    btc_chg = float(b.last['close'].loc[xt] / b.last['close'].loc[et] - 1)
    print(f"    over the hold: ETH {eth_chg:+.2%}, BTC {btc_chg:+.2%}  -> spread move "
          f"{(eth_chg-btc_chg):+.2%} (PnL tracks the SPREAD, not market direction)")


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 72)
    print("PHASE E.4 — BTC/ETH market-neutral log-spread MR hunt")
    print("=" * 72)
    base = {"zscore_period": 168.0, "entry_z": 2.5, "exit_z": 0.5}
    spec = build_pair_spec(zscore_period=168, entry_z=2.5, exit_z=0.5)
    legs = load_perp_pair_data(spec)
    idx = pd.DatetimeIndex(legs[0].last.index)
    print(f"legs: A={legs[0].symbol}(long) B={legs[1].symbol}(short)  "
          f"bars={len(idx)}  {_pyd(idx[0]).date()}->{_pyd(idx[-1]).date()}")

    # ---- COST-SANITY (pre-hunt, the load-bearing 60bps doubled-pair number) --
    res = _run(spec, legs)
    tot, sharpe, ntr, _bars, t_years = _metrics(res)
    freq = ntr / t_years
    drag = freq * _PAIR_RT_BPS * 1e-4
    print("\n--- COST-SANITY (pair RT = 60 bps = 2 legs x 30 bps) ---")
    for f in (10, 25, 50, 100):
        print(f"  {f:>4} trades/yr -> {f*_PAIR_RT_BPS*1e-4*100:5.1f}%/yr drag")
    print(f"  BASELINE realized: {freq:.0f} trades/yr -> {drag*100:.1f}%/yr cost drag at 60bps")
    print("  (a continuously-rebalanced hedge would add more; we DON'T rebalance "
          "intra-trade, so this is the floor)")

    print(f"\n--- BACKTEST (full {t_years:.1f}y) ---")
    print(f"  trades={ntr} ({freq:.0f}/yr)  total_return={tot:+.2%}  sharpe={sharpe:.2f}  "
          f"net_funding={res.total_funding:+.0f}  total_cost={res.total_cost:.0f}  "  # type: ignore[attr-defined]
          f"final_equity={res.final_equity:.0f}")  # type: ignore[attr-defined]
    exit_mix: dict[str, int] = {}
    for x in res.trades:  # type: ignore[attr-defined]
        exit_mix[x.exit_reason] = exit_mix.get(x.exit_reason, 0) + 1
    print(f"  exit mix: {exit_mix}")
    _inspect(spec, legs, res)

    # ---- GAUNTLET (unchanged scoring) ----------------------------------------
    print("\n--- GAUNTLET (compute_overfitting_score reused VERBATIM) ---")
    wf = _walk_forward(spec, legs, idx)
    print(f"  walk-forward: degradation={wf.degradation_ratio:.3f} valid={wf.degradation_ratio_valid} "
          f"oos_pos={wf.out_of_sample_positive_rate:.2f} ({sum(w.out_of_sample_return_pct>0 for w in wf.windows)}/6)")
    sweep = _sweep(legs, base)
    print(f"  param-sweep: peakiness={sweep.peakiness_score:.3f} cells={sweep.n_combinations} "
          f"baseline_rank={sweep.baseline_rank_percentile:.2f} best={sweep.best_in_grid_return:+.2%}")
    mc = _monte_carlo(spec, legs, tot, sharpe)
    print(f"  monte-carlo: p_value={mc.p_value:.3f} synth_mean={mc.synthetic_mean_return:+.2%} "
          f"real={mc.real_return_pct:+.2%} n={mc.n_permutations}")
    ds = deflated_sharpe(sharpe, n_trials_estimate=100, n_observations=round(t_years),
                         returns_skewness=0.0, returns_kurtosis=3.0)
    print(f"  deflated-shp: prob_real={ds.probability_strategy_is_real:.3f}")
    score = compute_overfitting_score(spec, walk_forward=wf, sweep=sweep, monte_carlo=mc, deflated=ds)
    print(f"\n  COMPOSITE: score={score.score:.2f}/100  verdict={score.verdict.value}")
    for c in sorted(score.contributions, key=lambda c: -c.contribution_pts):
        print(f"    - {c.name:18s} raw={c.raw_value:+.3f} w={c.weight:.2f} pts={c.contribution_pts:+.1f}")
    print(f"  explanation: {score.explanation}")

    # ---- VERDICT -------------------------------------------------------------
    cost_sanity = "PASS" if tot > drag else ("MARGINAL" if tot > 0 else "FAIL")
    seed = score.verdict.value == "likely_robust" and score.score < 30.0 and tot > drag
    print("\n--- SEED/REJECT ---")
    print(f"  cost-sanity: gross_return {tot:+.2%} vs drag {drag*100:.1f}% -> {cost_sanity}")
    print(f"  {'SEED' if seed else 'REJECT'} — composite {score.score:.2f} ({score.verdict.value}); "
          f"seed only on likely_robust + cost-sanity PASS. Do NOT seed marginal.")
    return 0


if __name__ == "__main__":
    _ = UTC
    raise SystemExit(main())
