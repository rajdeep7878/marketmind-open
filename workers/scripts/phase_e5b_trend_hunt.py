"""Phase E.5b — single-asset perp slow-trend hunt (one asset, both modes).

Runs the triple-EMA cascade perp trend strategy (perp_trend.py) through the
FULL gauntlet at UNCHANGED rigor for ONE asset, in BOTH directional modes
(long+short and long-only), and prints a structured verdict + dumps the
baseline return series (for cross-asset correlation in the synthesis). The
gauntlet scoring (compute_overfitting_score / deflated_sharpe, 30/60 bands,
v1 weights) is reused VERBATIM; only the four input signals come from the
trend engine. Multi-asset single-instrument perp trend has no vbt parity
partner (funding) — drift-parity N/A by construction (per E.3).

USAGE: uv run python workers/scripts/phase_e5b_trend_hunt.py --asset SOL
"""
from __future__ import annotations

import argparse
import json
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
from marketmind_workers.backtest.perp_trend import (
    TrendResult,
    load_perp_trend_data,
    run_perp_trend_backtest,
)
from marketmind_workers.overfitting.composite import compute_overfitting_score
from marketmind_workers.overfitting.deflated_sharpe import deflated_sharpe

_BPY = 6.0 * 365.0  # 4h bars/year = 2190 (matches metrics._BARS_PER_YEAR[H4])
_LEG_RT_BPS = 30.0  # crypto_perp single-leg round-trip (conservative)
# A throwaway single-leg spec so compute_overfitting_score (which reads
# spec_uses_stateful_v2 for the v1/v2 weight split) gets the v1 weights.
_SPEC = StrategySpec.model_validate({
    "schema_version": "1.0", "name": "perp trend", "instrument": {
        "symbol": "BTC/USDT:USDT", "exchange": "binance_usdm",
        "quote_currency": "USDT", "asset_class": "crypto_perp"},
    "primary_timeframe": "4h", "direction": "long",
    "entry": {"condition": {"type": "compare", "left": {"kind": "price", "field": "close"},
              "op": ">=", "right": {"kind": "constant", "value": 0.0}}, "order_type": "market"},
    "exit": {"exits": [{"type": "time", "max_bars_held": 1}]},
})


def _cascade(mid: int) -> tuple[int, int, int]:
    return max(2, round(mid / 3)), mid, mid * 2


def _pyd(x: object) -> datetime:
    return pd.Timestamp(x).to_pydatetime()  # type: ignore[arg-type]


def _metrics(res: TrendResult) -> tuple[float, float, int, float]:
    """(total_return, annualized_sharpe, n_trades, t_years)."""
    eq = pd.Series([v for _t, v in res.equity_curve])
    if len(eq) < 2 or eq.iloc[0] == 0:
        return 0.0, 0.0, len(res.trades), 2.0
    rets = eq.pct_change().dropna()
    mean_r, std_r = float(rets.mean()), float(rets.std(ddof=1))
    sharpe = (mean_r * _BPY) / (std_r * math.sqrt(_BPY)) if std_r > 0 else 0.0
    return float(eq.iloc[-1] / eq.iloc[0] - 1.0), sharpe, len(res.trades), max((len(eq) - 1) / _BPY, 2.0)


def _run(ohlcv: pd.DataFrame, funding: pd.Series, *, mid: int = 30, atr_mult: float = 3.0,
         allow_short: bool = True) -> TrendResult:
    f, m, s = _cascade(mid)
    return run_perp_trend_backtest(ohlcv, funding, ema_fast=f, ema_mid=m, ema_slow=s,
                                   atr_mult=atr_mult, allow_short=allow_short)


def _walk_forward(ohlcv: pd.DataFrame, funding: pd.Series, *, allow_short: bool,
                  n_windows: int = 6, train_ratio: float = 0.7) -> WalkForwardResult:
    idx = pd.DatetimeIndex(ohlcv.index)
    edges = [int(i * (len(idx) - 1) / n_windows) for i in range(n_windows + 1)]
    windows: list[WindowResult] = []
    is_r: list[float] = []
    oos_r: list[float] = []
    for w in range(n_windows):
        lo, hi = edges[w], edges[w + 1]
        split = lo + int(train_ratio * (hi - lo))
        seg_is = ohlcv.iloc[lo:split]
        seg_oos = ohlcv.iloc[split:hi]
        fis = funding.reindex(seg_is.index)
        foos = funding.reindex(seg_oos.index)
        ri, si, ni, _ = _metrics(_run(seg_is, fis, allow_short=allow_short))
        ro, so, no, _ = _metrics(_run(seg_oos, foos, allow_short=allow_short))
        is_r.append(ri)
        oos_r.append(ro)
        windows.append(WindowResult(
            window_index=w,
            in_sample_start=_pyd(idx[lo]), in_sample_end=_pyd(idx[split]),
            out_of_sample_start=_pyd(idx[split]), out_of_sample_end=_pyd(idx[hi]),
            in_sample_return_pct=ri, in_sample_sharpe=si, in_sample_num_trades=ni,
            out_of_sample_return_pct=ro, out_of_sample_sharpe=so, out_of_sample_num_trades=no))
    is_avg, oos_avg = float(np.mean(is_r)), float(np.mean(oos_r))
    valid = is_avg > 0
    return WalkForwardResult(
        windows=windows, in_sample_avg_return=is_avg, out_of_sample_avg_return=oos_avg,
        degradation_ratio=(oos_avg / is_avg) if valid else 0.0, degradation_ratio_valid=valid,
        out_of_sample_positive_rate=float(np.mean([r > 0 for r in oos_r])),
        consistency_score=1.0 / (1.0 + 2.0 * float(np.std(oos_r))),
        train_ratio=train_ratio, n_windows_requested=n_windows, n_windows_actual=n_windows)


def _sweep(ohlcv: pd.DataFrame, funding: pd.Series, *, allow_short: bool) -> ParameterSweepResult:
    mids = [15, 30, 60]
    mults = [2.0, 3.0, 4.0]
    base = (30, 3.0)
    cells: list[SweepCell] = []
    rets: dict[tuple[int, float], float] = {}
    for mid in mids:
        for mult in mults:
            r, s, n, _ = _metrics(_run(ohlcv, funding, mid=mid, atr_mult=mult, allow_short=allow_short))
            rets[(mid, mult)] = r
            cells.append(SweepCell(axis_values={"ema_mid": float(mid), "atr_mult": mult},
                                   total_return_pct=r, sharpe_ratio=s, num_trades=n,
                                   is_baseline=(mid, mult) == base))
    base_ret = rets[base]
    neigh = [r for (mid, mult), r in rets.items()
             if (mid != base[0]) ^ (mult != base[1]) and (mid == base[0] or mult == base[1])]
    neigh_avg = float(np.mean(neigh)) if neigh else base_ret
    peak = max(0.0, min(1.0, (base_ret - neigh_avg) / max(abs(base_ret), 0.01)))
    allr = list(rets.values())
    return ParameterSweepResult(
        axes=[SweepAxis(kind=SweepAxisKind.INDICATOR_PERIOD, label="ema_mid",
                        values=[float(x) for x in mids], baseline_value=30.0,
                        target_paths=["spread.zscore_period"]),
              SweepAxis(kind=SweepAxisKind.STOP_LOSS_PCT, label="atr_mult", values=mults,
                        baseline_value=3.0, target_paths=["exit.atr_mult"])],
        cells=cells, baseline_return_pct=base_ret,
        baseline_rank_percentile=float(np.mean([r <= base_ret for r in allr])),
        best_in_grid_return=max(allr), worst_in_grid_return=min(allr),
        neighborhood_avg_return=neigh_avg, peakiness_score=peak,
        n_combinations=len(cells), skipped_reason=None)


def _monte_carlo(ohlcv: pd.DataFrame, funding: pd.Series, *, allow_short: bool,
                 real_ret: float, real_sharpe: float, n: int = 100, seed: int = 42) -> MonteCarloResult:
    rng = np.random.default_rng(seed)
    close = ohlcv["close"].to_numpy()
    logret = np.diff(np.log(close))
    idx = ohlcv.index
    synth: list[float] = []
    for _ in range(n):
        shuf = close[0] * np.exp(np.cumsum(np.concatenate([[0.0], rng.permutation(logret)])))
        syn = pd.DataFrame({"open": shuf, "high": shuf, "low": shuf, "close": shuf,
                            "volume": ohlcv["volume"].to_numpy(), "mark_close": shuf}, index=idx)
        r, *_ = _metrics(_run(syn, funding, allow_short=allow_short))
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


def _hunt_mode(ohlcv: pd.DataFrame, funding: pd.Series, *, allow_short: bool) -> dict[str, object]:
    res = _run(ohlcv, funding, allow_short=allow_short)
    tot, sharpe, ntr, t_years = _metrics(res)
    freq = ntr / t_years
    drag = freq * _LEG_RT_BPS * 1e-4
    longs = [t for t in res.trades if t.direction > 0]
    shorts = [t for t in res.trades if t.direction < 0]
    wf = _walk_forward(ohlcv, funding, allow_short=allow_short)
    sweep = _sweep(ohlcv, funding, allow_short=allow_short)
    mc = _monte_carlo(ohlcv, funding, allow_short=allow_short, real_ret=tot, real_sharpe=sharpe)
    ds = deflated_sharpe(sharpe, n_trials_estimate=100, n_observations=round(t_years),
                         returns_skewness=0.0, returns_kurtosis=3.0)
    score = compute_overfitting_score(_SPEC, walk_forward=wf, sweep=sweep, monte_carlo=mc, deflated=ds)
    seed = score.verdict.value == "likely_robust" and score.score < 30.0 and tot > drag and sharpe > 0
    eq = pd.Series([v for _t, v in res.equity_curve], index=[t for t, _v in res.equity_curve])
    return {
        "mode": "long+short" if allow_short else "long-only",
        "verdict": score.verdict.value, "composite": round(score.score, 2),
        "seed": bool(seed), "sharpe": round(sharpe, 3), "total_return_pct": round(tot, 4),
        "trades": ntr, "trades_per_yr": round(freq, 1), "cost_drag_pct_yr": round(drag * 100, 2),
        "cost_sanity": "PASS" if tot > drag else ("MARGINAL" if tot > 0 else "FAIL"),
        "wf_degradation": round(wf.degradation_ratio, 3), "wf_valid": wf.degradation_ratio_valid,
        "wf_oos_pos": f"{sum(w.out_of_sample_return_pct > 0 for w in wf.windows)}/6",
        "sweep_peakiness": round(sweep.peakiness_score, 3), "sweep_best": round(sweep.best_in_grid_return, 3),
        "mc_pvalue": round(mc.p_value, 3), "dsr_prob_real": round(ds.probability_strategy_is_real, 3),
        "net_funding": round(res.total_funding, 0), "total_cost": round(res.total_cost, 0),
        "long_n": len(longs), "long_net": round(sum(t.net_pnl for t in longs), 0),
        "short_n": len(shorts), "short_net": round(sum(t.net_pnl for t in shorts), 0),
        "_equity": eq,  # for correlation (stripped from JSON)
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asset", required=True)
    ap.add_argument("--dump-returns", default="")  # path to dump baseline weekly returns
    args = ap.parse_args()
    ohlcv, funding = load_perp_trend_data(args.asset)
    idx = pd.DatetimeIndex(ohlcv.index)
    out: dict[str, object] = {"asset": args.asset, "bars": len(ohlcv),
                              "start": str(_pyd(idx[0]).date()), "end": str(_pyd(idx[-1]).date()),
                              "years": round((idx[-1] - idx[0]).days / 365.25, 1)}
    for allow_short in (True, False):
        m = _hunt_mode(ohlcv, funding, allow_short=allow_short)
        eq = m.pop("_equity")
        out["long+short" if allow_short else "long-only"] = m
        if args.dump_returns and not allow_short:  # dump long-only weekly returns
            wk = pd.Series(eq).resample("1W").last().pct_change().dropna()
            wk.to_frame("ret").to_parquet(args.dump_returns)
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    _ = datetime
    raise SystemExit(main())
