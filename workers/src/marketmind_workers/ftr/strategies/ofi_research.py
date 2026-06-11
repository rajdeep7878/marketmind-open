"""3.3 ofi_microstructure_research — measurement module, HARD research-only.

EXPECTED OUTCOME (written here so future readers do not mistake this module
for a profit claim): in taker mode the verdict is expected to be REJECTED.
The literature this module implements (Cont, Kukanov & Stoikov 2014;
Silantyev 2019) itself notes that OFI explains *contemporaneous* mid-price
variation, that predictive correlation decays quickly with horizon, and
that predicted moves are typically smaller than the bid-ask spread — so a
spread-crossing taker at retail fees pays more to enter than the predicted
move is worth. The module's real outputs are MEASUREMENTS: R²/IC by
horizon, OFI betas, decay curves, realized spread and adverse-selection
markouts — these calibrate the liquidity overlay's cost model and seed
future maker-side research.

``research_simulation_only=True`` is a frozen field on OFIResearchSpec; the
paper trader and any execution router refuse this strategy BY TYPE
(test_ftr_uk_compliance_guard + test_ftr_paper_refuses_research_only).

Sample gate (Stage 4): no verdict of any kind until >= 28 recorded days
with >= 95% recorder uptime; below that the verdict is INSUFFICIENT_DATA.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import structlog
from sklearn.linear_model import Ridge

from marketmind_workers.ftr.features.micro import l1_grid, markouts, micro_features
from marketmind_workers.ftr.strategies.specs import OFIResearchSpec

logger = structlog.get_logger(__name__)

MIN_RECORDED_DAYS = 28
MIN_UPTIME_PCT = 95.0

_HORIZON_BARS_1M = {"1m": 1, "5m": 5, "15m": 15}


@dataclass(frozen=True)
class RecorderSample:
    """Recorded-data inventory derived from hourly integrity manifests."""

    recorded_days: float
    mean_uptime_pct: float
    hours: int

    @property
    def sufficient(self) -> bool:
        return self.recorded_days >= MIN_RECORDED_DAYS and self.mean_uptime_pct >= MIN_UPTIME_PCT


def inventory_recordings(recordings_dir: Path, symbol: str) -> RecorderSample:
    """Scan hourly manifests for one symbol; returns the sample inventory."""
    safe = symbol.replace("/", "_")
    base = recordings_dir / safe
    manifests = sorted(base.glob("*/manifest.json")) if base.exists() else []
    if not manifests:
        return RecorderSample(recorded_days=0.0, mean_uptime_pct=0.0, hours=0)
    uptimes = [float(json.loads(p.read_text()).get("uptime_pct", 0.0)) for p in manifests]
    return RecorderSample(
        recorded_days=len(manifests) / 24.0,
        mean_uptime_pct=float(np.mean(uptimes)),
        hours=len(manifests),
    )


@dataclass(frozen=True)
class MeasurementReport:
    """Predictability + cost measurements by horizon (the real deliverable)."""

    horizon: str
    r2_in_sample: float
    r2_out_of_sample: float
    ic_out_of_sample: float
    ofi_beta: float
    mean_spread_bps: float
    mean_predicted_move_bps: float
    adverse_selection_bps: dict[str, float]
    n_obs: int

    def predicted_move_vs_spread(self) -> float:
        """< 1.0 means predicted moves are sub-spread: no taker stat-arb."""
        if self.mean_spread_bps == 0:
            return float("nan")
        return self.mean_predicted_move_bps / self.mean_spread_bps


def measure_predictability(
    book_ticker: pd.DataFrame,
    trades: pd.DataFrame,
    *,
    spec: OFIResearchSpec,
) -> MeasurementReport:
    """Fit ridge on 1m micro features to predict mid returns at the spec
    horizon; 70/30 chronological split (no shuffle — time series)."""
    feats = micro_features(book_ticker, trades, window="1min")
    h = _HORIZON_BARS_1M[spec.horizon]
    # Forward h-bar mid return at t = sum of the NEXT h 1m log returns:
    # rolling(h).sum() at t+h covers (t, t+h]; shift(-h) aligns it to t.
    mid_logret = feats["mid_logret"]
    assert isinstance(mid_logret, pd.Series)
    fwd_sum = mid_logret.rolling(h).sum()
    assert isinstance(fwd_sum, pd.Series)
    target = fwd_sum.shift(-h)
    assert isinstance(target, pd.Series)

    cols = ["spread_bps", "l1_imbalance", "ofi", "tfi", "rvol_short"]
    data = pd.concat([feats[cols], target.rename("y")], axis=1).dropna()
    assert isinstance(data, pd.DataFrame)
    if len(data) < 200:
        raise ValueError(f"only {len(data)} clean observations; need >= 200")

    split = int(len(data) * 0.7)
    train, test = data.iloc[:split], data.iloc[split:]

    model = Ridge(alpha=1.0, random_state=spec.seed)
    x_tr = train[cols].to_numpy()
    x_te = test[cols].to_numpy()
    # Standardize on TRAIN stats only.
    mu, sd = x_tr.mean(axis=0), x_tr.std(axis=0)
    sd[sd == 0] = 1.0
    model.fit((x_tr - mu) / sd, train["y"].to_numpy())

    pred_tr = model.predict((x_tr - mu) / sd)
    pred_te = model.predict((x_te - mu) / sd)

    def _r2(y: np.ndarray, p: np.ndarray) -> float:
        ss_res = float(np.sum((y - p) ** 2))
        ss_tot = float(np.sum((y - y.mean()) ** 2))
        return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")

    y_te = test["y"].to_numpy()
    ic = float(pd.Series(pred_te).corr(pd.Series(y_te), method="spearman"))

    marks = markouts(book_ticker)
    adverse = {
        c: float(marks[c].mean())
        for c in marks.columns
        if c.startswith("adverse_selection_")
    }

    ofi_idx = cols.index("ofi")
    report = MeasurementReport(
        horizon=spec.horizon,
        r2_in_sample=_r2(train["y"].to_numpy(), pred_tr),
        r2_out_of_sample=_r2(y_te, pred_te),
        ic_out_of_sample=ic,
        ofi_beta=float(model.coef_[ofi_idx]),
        mean_spread_bps=float(feats["spread_bps"].mean()),
        mean_predicted_move_bps=float(np.mean(np.abs(pred_te)) * 1e4),
        adverse_selection_bps=adverse,
        n_obs=len(data),
    )
    logger.info(
        "ftr_ofi_measurement",
        horizon=spec.horizon,
        r2_oos=round(report.r2_out_of_sample, 5),
        ic_oos=round(report.ic_out_of_sample, 4),
        pred_vs_spread=round(report.predicted_move_vs_spread(), 3),
    )
    return report


@dataclass(frozen=True)
class SimTrade:
    ts: pd.Timestamp
    side: str
    entry_px: float
    exit_px: float
    net_bps: float
    mode: str


def simulate_taker(
    book_ticker: pd.DataFrame,
    predictions: pd.Series,
    *,
    spec: OFIResearchSpec,
    taker_fee_bps: float,
    slippage_bps: float,
) -> list[SimTrade]:
    """Research-only taker simulation: top-decile conviction, cooldown,
    daily cap. Cross the spread at decision+latency, exit at horizon end
    the same way. No queue optimism, fills at opposite touch + slippage."""
    l1 = l1_grid(book_ticker, freq="1s")
    h_s = _HORIZON_BARS_1M[spec.horizon] * 60
    threshold = predictions.abs().quantile(spec.conviction_decile)

    trades: list[SimTrade] = []
    last_trade_ts: pd.Timestamp | None = None
    daily_count: dict[str, int] = {}
    lat = pd.Timedelta(milliseconds=spec.decision_latency_ms)

    for ts, score in predictions.items():
        assert isinstance(ts, pd.Timestamp)
        if abs(score) < threshold or score == 0:
            continue
        if last_trade_ts is not None and ts - last_trade_ts < pd.Timedelta(
            minutes=spec.cooldown_minutes
        ):
            continue
        day = str(ts.date())
        if daily_count.get(day, 0) >= spec.daily_signal_cap:
            continue
        t_in = ts + lat
        t_out = t_in + pd.Timedelta(seconds=h_s)
        try:
            row_in = l1.loc[:t_in].iloc[-1]
            row_out = l1.loc[:t_out].iloc[-1]
        except IndexError:
            continue
        if bool(row_in.isna().any()) or bool(row_out.isna().any()):
            continue
        slip = slippage_bps * 1e-4
        if score > 0:  # buy: pay the ask + slippage, exit at bid - slippage
            entry = float(row_in["ask"]) * (1 + slip)
            exit_ = float(row_out["bid"]) * (1 - slip)
            gross_bps = (exit_ / entry - 1.0) * 1e4
        else:  # sell-side signal — long/flat only, so we SKIP shorts
            continue
        net_bps = gross_bps - 2.0 * taker_fee_bps
        trades.append(
            SimTrade(
                ts=ts, side="long", entry_px=entry, exit_px=exit_, net_bps=net_bps, mode="taker"
            )
        )
        last_trade_ts = ts
        daily_count[day] = daily_count.get(day, 0) + 1
    return trades


def simulate_pessimistic_maker(
    book_ticker: pd.DataFrame,
    predictions: pd.Series,
    *,
    spec: OFIResearchSpec,
    maker_fee_bps: float,
    timeout_s: int = 60,
) -> list[SimTrade]:
    """Pessimistic maker mode: post at the touch; filled ONLY if price trades
    strictly through the level before timeout; cancel otherwise. Adverse-
    selection markout is the exit price drift. No queue-position optimism —
    'touched' is not 'filled' here, the market must trade THROUGH."""
    l1 = l1_grid(book_ticker, freq="1s")
    h_s = _HORIZON_BARS_1M[spec.horizon] * 60
    threshold = predictions.abs().quantile(spec.conviction_decile)

    trades: list[SimTrade] = []
    last_trade_ts: pd.Timestamp | None = None
    daily_count: dict[str, int] = {}

    for ts, score in predictions.items():
        assert isinstance(ts, pd.Timestamp)
        if abs(score) < threshold or score <= 0:  # long/flat only
            continue
        if last_trade_ts is not None and ts - last_trade_ts < pd.Timedelta(
            minutes=spec.cooldown_minutes
        ):
            continue
        day = str(ts.date())
        if daily_count.get(day, 0) >= spec.daily_signal_cap:
            continue
        try:
            row0 = l1.loc[:ts].iloc[-1]
        except IndexError:
            continue
        if bool(row0.isna().any()):
            continue
        bid0 = float(row0["bid"])
        fill_window = l1.loc[ts : ts + pd.Timedelta(seconds=timeout_s)]
        # strictly through: someone must SELL below our bid level
        traded_through = fill_window["ask"] < bid0
        if not bool(traded_through.any()):
            continue  # cancel after timeout — never filled
        t_fill = fill_window.index[traded_through.to_numpy(dtype=bool)][0]
        t_out = t_fill + pd.Timedelta(seconds=h_s)
        try:
            row_out = l1.loc[:t_out].iloc[-1]
        except IndexError:
            continue
        if bool(row_out.isna().any()):
            continue
        entry = bid0
        exit_ = float(row_out["bid"])
        gross_bps = (exit_ / entry - 1.0) * 1e4
        net_bps = gross_bps - 2.0 * maker_fee_bps
        trades.append(
            SimTrade(
                ts=ts, side="long", entry_px=entry, exit_px=exit_, net_bps=net_bps, mode="maker"
            )
        )
        last_trade_ts = ts
        daily_count[day] = daily_count.get(day, 0) + 1
    return trades
