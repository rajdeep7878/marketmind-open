"""3.2 trend_4h_portfolio — adaptive 4h/6h spot long/flat trend portfolio.

Confirmation-layered entry (consistent with the repo's seedable-zone
finding): ``close > EMA_fast > EMA_slow`` AND close makes a new Donchian(N)
high. Exit: Chandelier trail (highest close since entry − m·ATR14) or
EMA fast/slow cross-down, whichever first.

Universe: point-in-time top-K by rolling 30d median dollar volume, minimum
listed history at selection time, re-selected monthly using only data
available then — no survivorship. Stablecoins / wrapped / leveraged tokens
are excluded upstream (the Stage-1 superset contains none).

Sizing: per-asset vol targeting (target 20% annualized per sleeve), 25%
per-asset cap, 100% gross cap, no leverage. Decisions only at bar closes;
per-asset re-entry cooldown.

Honesty diagnostics (mandatory): average pairwise correlation of held
assets, effective breadth N_eff = N / (1 + (N-1)·rho_bar), per-regime split
(BTC above/below its 200d MA).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from marketmind_workers.ftr.data.ohlcv import dtindex
from marketmind_workers.ftr.features.hourly import col
from marketmind_workers.ftr.strategies.specs import TrendPortfolioSpec

_BARS_PER_DAY = {"4h": 6, "6h": 4}
_BARS_PER_YEAR = {"4h": 2190.0, "6h": 1460.0}


def _ema(s: pd.Series, span: int) -> pd.Series:
    out = s.ewm(span=span, adjust=False).mean()
    assert isinstance(out, pd.Series)
    return out


def _atr(df: pd.DataFrame, window: int = 14) -> pd.Series:
    high, low, close = col(df, "high"), col(df, "low"), col(df, "close")
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    assert isinstance(tr, pd.Series)
    out = tr.ewm(alpha=1.0 / window, adjust=False).mean()
    assert isinstance(out, pd.Series)
    return out


def resample_6h(df_1h: pd.DataFrame) -> pd.DataFrame:
    """Build the 6h variant from 1h bars (Binance has no native 6h cache)."""
    agg = df_1h.resample("6h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    out = agg.dropna()
    assert isinstance(out, pd.DataFrame)
    return out


# ---------------------------------------------------------------------------
# Point-in-time universe
# ---------------------------------------------------------------------------


def select_universe(
    daily_dollar_volume: dict[str, pd.Series],
    listed_since: dict[str, pd.Timestamp],
    *,
    spec: TrendPortfolioSpec,
    month_starts: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Monthly point-in-time membership matrix (index=month, cols=symbol).

    At each month start t: eligible = listed >= min_listed_days before t;
    rank by median dollar volume over the 30d ENDING at t; keep top-K.
    Only data with timestamp < t is used.
    """
    symbols = sorted(daily_dollar_volume)
    rows: list[dict[str, bool]] = []
    for t in month_starts:
        scores: dict[str, float] = {}
        for sym in symbols:
            if (t - listed_since[sym]).days < spec.min_listed_days:
                continue
            dv = daily_dollar_volume[sym]
            window = dv.loc[(dv.index >= t - pd.Timedelta(days=30)) & (dv.index < t)]
            if len(window) < 20:
                continue
            scores[sym] = float(window.median())
        top = sorted(scores, key=lambda s: scores[s], reverse=True)[: spec.universe_size]
        rows.append({sym: sym in top for sym in symbols})
    return pd.DataFrame(rows, index=month_starts, columns=pd.Index(symbols))


def membership_at(universe: pd.DataFrame, ts: pd.Timestamp, symbol: str) -> bool:
    """Membership of `symbol` at bar `ts` = last monthly selection <= ts."""
    eligible = universe.loc[universe.index <= ts]
    if eligible.empty:
        return False
    return bool(eligible.iloc[-1][symbol])


# ---------------------------------------------------------------------------
# Per-asset signal state machine
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetSignals:
    """Per-bar entry/exit raw conditions + state-machine position series."""

    position: pd.Series  # 0/1 after entry/exit state machine
    entries: pd.Series  # bool: state machine entered at this bar close
    exits: pd.Series  # bool: state machine exited at this bar close
    atr: pd.Series
    realized_vol_ann: pd.Series


def compute_asset_signals(
    df: pd.DataFrame,
    *,
    spec: TrendPortfolioSpec,
    member_mask: pd.Series,
    btc_regime_ok: pd.Series | None = None,
) -> AssetSignals:
    """Run the confirmation-layered entry / chandelier-exit state machine.

    All conditions evaluate on bar-close data only. The position series
    marks the bars where the strategy WANTS exposure; fills happen next bar
    open in the engines.
    """
    close = col(df, "close")
    high = col(df, "high")
    ema_f = _ema(close, spec.ema_fast)
    ema_s = _ema(close, spec.ema_slow)
    atr = _atr(df, 14)

    # New Donchian(N) high: close exceeds the max HIGH of the prior N bars
    # (shifted: the current bar must BREAK the channel, not define it).
    donchian_hi = high.rolling(spec.donchian_n).max().shift(1)
    entry_raw = (close > ema_f) & (ema_f > ema_s) & (close > donchian_hi)
    cross_down = ema_f < ema_s

    if btc_regime_ok is not None:
        entry_raw = entry_raw & btc_regime_ok.reindex(entry_raw.index).fillna(value=False)
    entry_raw = entry_raw & member_mask

    bars_per_day = _BARS_PER_DAY[spec.timeframe]
    cooldown_bars = max(int(spec.reentry_cooldown_hours / (24 / bars_per_day) / 1), 0)
    # reentry_cooldown_hours is wall-clock; bars = hours / bar_hours
    bar_hours = 24 / bars_per_day
    cooldown_bars = int(np.ceil(spec.reentry_cooldown_hours / bar_hours))

    n = len(df)
    close_arr = close.to_numpy()
    atr_arr = atr.to_numpy()
    entry_arr = entry_raw.fillna(value=False).to_numpy(dtype=bool)
    crossdn_arr = cross_down.fillna(value=False).to_numpy(dtype=bool)
    member_arr = member_mask.reindex(df.index).fillna(value=False).to_numpy(dtype=bool)

    position = np.zeros(n, dtype="int64")
    entries = np.zeros(n, dtype=bool)
    exits = np.zeros(n, dtype=bool)

    in_pos = False
    highest_close = 0.0
    cooldown_until = -1

    for i in range(n):
        if in_pos:
            highest_close = max(highest_close, close_arr[i])
            trail = highest_close - spec.chandelier_atr_multiple * atr_arr[i]
            # Exit on trail breach, cross-down, or losing universe membership.
            if close_arr[i] <= trail or crossdn_arr[i] or not member_arr[i]:
                in_pos = False
                exits[i] = True
                cooldown_until = i + cooldown_bars
        elif entry_arr[i] and i > cooldown_until and not np.isnan(atr_arr[i]):
            in_pos = True
            highest_close = close_arr[i]
            entries[i] = True
        position[i] = 1 if in_pos else 0

    logret = pd.Series(np.log(close_arr), index=df.index).diff()
    bpy = _BARS_PER_YEAR[spec.timeframe]
    rvol_ann = logret.rolling(30 * bars_per_day).std() * np.sqrt(bpy)

    idx = dtindex(df)
    return AssetSignals(
        position=pd.Series(position, index=idx),
        entries=pd.Series(entries, index=idx),
        exits=pd.Series(exits, index=idx),
        atr=atr,
        realized_vol_ann=rvol_ann,
    )


def target_weights(
    signals: dict[str, AssetSignals],
    *,
    spec: TrendPortfolioSpec,
) -> pd.DataFrame:
    """Per-bar target weights: vol-targeted sleeves, per-asset + gross caps.

    w_i = min(target_sleeve_vol / realized_vol_i, per_asset_cap) while in
    position; scaled down pro-rata if the gross sum exceeds the gross cap.
    No leverage by construction.
    """
    weights = {}
    for sym, sig in signals.items():
        raw = (spec.target_sleeve_vol_annual / sig.realized_vol_ann).clip(
            upper=spec.per_asset_cap_pct
        )
        weights[sym] = (raw * sig.position).fillna(0.0)
    w = pd.DataFrame(weights).fillna(0.0)
    gross = w.sum(axis=1)
    scale = (spec.gross_cap_pct / gross).clip(upper=1.0).fillna(1.0)
    return w.mul(scale, axis=0)


# ---------------------------------------------------------------------------
# Honesty diagnostics
# ---------------------------------------------------------------------------


def effective_breadth(weights: pd.DataFrame, returns: pd.DataFrame) -> dict[str, float]:
    """Average pairwise correlation of HELD assets and N_eff.

    N_eff = N / (1 + (N-1)·rho_bar). Crypto pairwise correlations are high,
    so N_eff is expected to be well below the nominal count — measured and
    reported, never assumed away.
    """
    held_mask = weights > 0
    held_counts = held_mask.sum(axis=1)
    avg_held = float(held_counts[held_counts > 0].mean()) if (held_counts > 0).any() else 0.0

    held_cols = [c for c in weights.columns if bool(held_mask[c].any())]
    if len(held_cols) < 2:
        return {"avg_held": avg_held, "avg_pairwise_corr": float("nan"), "n_eff": avg_held}
    held_returns = returns[held_cols]
    assert isinstance(held_returns, pd.DataFrame)
    corr = held_returns.corr()
    vals = corr.to_numpy()
    iu = np.triu_indices_from(vals, k=1)
    rho = float(np.nanmean(vals[iu]))
    n = avg_held if avg_held > 0 else float(len(held_cols))
    n_eff = n / (1.0 + (n - 1.0) * rho) if not np.isnan(rho) else n
    return {"avg_held": avg_held, "avg_pairwise_corr": rho, "n_eff": n_eff}


def btc_regime_mask(btc_daily_close: pd.Series, target_index: pd.DatetimeIndex) -> pd.Series:
    """BTC > its 200d MA, forward-aligned onto the strategy's bar index.

    Uses only daily closes already final at each target bar (shift(1) on the
    daily series before reindex => no lookahead through the current day).
    """
    ma200 = btc_daily_close.rolling(200).mean()
    above = (btc_daily_close > ma200).shift(1)
    aligned = above.reindex(target_index, method="ffill")
    return aligned.fillna(value=False).astype(bool)
