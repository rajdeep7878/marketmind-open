"""3.4 liquidity_overlay — execution-timing filter. Cost reducer, not alpha.

The overlay may only BLOCK or DEFER an entry; it never inflates an edge
estimate and never initiates a trade. Inputs in priority order:

1. live/recorded bookTicker spread when available;
2. otherwise the Abdi & Ranaldo (2017) high-low spread estimator on 1m
   OHLC ("A Simple Estimation of Bid-Ask Spreads from Daily Close, High,
   and Low Prices", RFS 30(12) — applied here on 1m bars):

       beta-free closed form per bar pair:
       s_t^2 = max(4 * (c_t - eta_t) * (c_t - eta_{t+1}), 0)
       with c = log close, eta = (log high + log low) / 2
       spread_t = sqrt(mean of s^2 over the estimation window)

3. hour-of-week liquidity score built from recorded/1m volume;
4. realized-vol regime (wide-spread storms get deferred).

Rule: ALLOW iff estimated spread <= the configured percentile of the
trailing-30d hour-of-week-MATCHED distribution AND liquidity score >=
threshold; otherwise DEFER up to max_defer_bars, then SKIP. Every
DEFER/SKIP carries SKIP_LIQUIDITY_FILTER with the measured values.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

import numpy as np
import pandas as pd

from marketmind_workers.ftr.data.ohlcv import dtindex
from marketmind_workers.ftr.features.hourly import col
from marketmind_workers.ftr.strategies.specs import LiquidityOverlaySpec


def _how_array(idx: pd.DatetimeIndex) -> np.ndarray[Any, np.dtype[np.int64]]:
    """Hour-of-week codes (weekday*24 + hour; pandas Mon=0 convention).

    cast: the bundled pandas stubs miss .weekday/.hour on DatetimeIndex.
    """
    idx_any = cast("Any", idx)
    return np.asarray(idx_any.weekday, dtype="int64") * 24 + np.asarray(
        idx_any.hour, dtype="int64"
    )


class OverlayDecision(StrEnum):
    ALLOW = "ALLOW"
    DEFER = "DEFER"
    SKIP = "SKIP"


@dataclass(frozen=True)
class OverlayVerdict:
    decision: OverlayDecision
    spread_bps: float
    spread_percentile: float
    liquidity_score: float
    detail: str


def abdi_ranaldo_spread_bps(ohlc_1m: pd.DataFrame, *, window: int = 390) -> pd.Series:
    """Abdi-Ranaldo (2017) spread estimate in bps on a rolling window.

    eta_t = midpoint of log high/low; c_t = log close. The per-bar squared
    spread is 4*(c_t - eta_t)*(c_t - eta_{t+1}) floored at zero; the rolling
    root-mean gives the spread level in relative terms (x1e4 => bps).
    """
    c = pd.Series(np.log(col(ohlc_1m, "close").to_numpy()), index=ohlc_1m.index)
    eta = pd.Series(
        (np.log(col(ohlc_1m, "high").to_numpy()) + np.log(col(ohlc_1m, "low").to_numpy())) / 2.0,
        index=ohlc_1m.index,
    )
    prod = 4.0 * (c - eta) * (c - eta.shift(-1))
    assert isinstance(prod, pd.Series)
    s2 = prod.clip(lower=0.0)
    spread = np.sqrt(s2.rolling(window, min_periods=max(window // 4, 30)).mean())
    return pd.Series(spread, index=ohlc_1m.index) * 1e4


def hour_of_week_liquidity_score(volume_1m: pd.Series, *, trailing_days: int = 30) -> pd.Series:
    """Score in [0,1]: each bar's hour-of-week median volume vs the global
    distribution over the trailing window. pandas weekday Mon=0 everywhere."""
    idx = volume_1m.index
    assert isinstance(idx, pd.DatetimeIndex)
    last = idx.max()
    assert isinstance(last, pd.Timestamp)
    recent = volume_1m.loc[idx >= last - pd.Timedelta(days=trailing_days)]
    ridx = recent.index
    assert isinstance(ridx, pd.DatetimeIndex)
    how = pd.Series(_how_array(ridx), index=ridx)
    med_by_how = recent.groupby(how).median()
    ranks = med_by_how.rank(pct=True)
    full_how = pd.Series(_how_array(idx), index=idx)
    return full_how.map(ranks).fillna(0.0)


class LiquidityOverlay:
    """Stateful per-(strategy, symbol) overlay: ALLOW / DEFER / SKIP.

    Wire as a decorator on strategy entries: the Stage-4 ablation is a
    config flip (spec.use_liquidity_overlay).
    """

    def __init__(
        self,
        config: LiquidityOverlaySpec,
        *,
        spread_bps: pd.Series,
        liquidity_score: pd.Series,
    ) -> None:
        self.config = config
        self.spread_bps = spread_bps
        self.liquidity_score = liquidity_score
        self._defer_count: int = 0

    def evaluate(self, ts: pd.Timestamp) -> OverlayVerdict:
        cfg = self.config
        if len(self.spread_bps) == 0:
            spread_hist = self.spread_bps
        else:
            spread_hist = self.spread_bps.loc[
                (self.spread_bps.index >= ts - pd.Timedelta(days=cfg.trailing_days))
                & (self.spread_bps.index <= ts)
            ].dropna()
        if spread_hist.empty:
            # no measurement: ALLOW (the overlay must never invent a block
            # from missing data; missing input = no information, not a veto)
            return OverlayVerdict(
                decision=OverlayDecision.ALLOW,
                spread_bps=float("nan"),
                spread_percentile=float("nan"),
                liquidity_score=float("nan"),
                detail="no spread measurement available",
            )
        # hour-of-week matched distribution
        hidx = spread_hist.index
        assert isinstance(hidx, pd.DatetimeIndex)
        how = _how_array(hidx)
        target_how = int(ts.weekday()) * 24 + int(ts.hour)
        matched = spread_hist.to_numpy()[how == target_how]
        if len(matched) < 8:
            matched = spread_hist.to_numpy()  # fall back to unmatched dist

        current = float(spread_hist.iloc[-1])
        # Midrank percentile: ties count half, so a CONSTANT spread reads as
        # the 50th percentile (typical conditions), not the 100th (worst).
        pct = float((matched < current).mean() + 0.5 * (matched == current).mean())
        score_series = self.liquidity_score.loc[self.liquidity_score.index <= ts]
        score = float(score_series.iloc[-1]) if len(score_series) else 0.0

        ok = pct <= cfg.spread_percentile_max and score >= cfg.liquidity_score_min
        if ok:
            self._defer_count = 0
            return OverlayVerdict(
                decision=OverlayDecision.ALLOW,
                spread_bps=current,
                spread_percentile=pct,
                liquidity_score=score,
                detail="spread+liquidity within bounds",
            )
        self._defer_count += 1
        if self._defer_count <= cfg.max_defer_bars:
            decision = OverlayDecision.DEFER
            detail = f"defer {self._defer_count}/{cfg.max_defer_bars}"
        else:
            decision = OverlayDecision.SKIP
            detail = "max defers exhausted"
            self._defer_count = 0
        return OverlayVerdict(
            decision=decision,
            spread_bps=current,
            spread_percentile=pct,
            liquidity_score=score,
            detail=detail,
        )


def apply_overlay_to_positions(
    position: pd.Series,
    overlay: LiquidityOverlay,
) -> tuple[pd.Series, list[tuple[pd.Timestamp, OverlayVerdict]]]:
    """Vectorized-run helper: re-time entries through the overlay.

    Walks the 0/1 position series; each flat->long transition consults the
    overlay. DEFER pushes the entry to the next bar (re-checked); SKIP
    cancels that entry episode entirely (until the strategy re-signals).
    Exits are NEVER touched — the overlay only delays getting in, never
    getting out.
    """
    idx = dtindex(position.to_frame())
    pos = position.to_numpy(dtype="int64").copy()
    out = np.zeros_like(pos)
    log: list[tuple[pd.Timestamp, OverlayVerdict]] = []

    i = 0
    n = len(pos)
    while i < n:
        if pos[i] == 1 and (i == 0 or pos[i - 1] == 0) and (i == 0 or out[i - 1] == 0):
            # entry episode starting at i: find its end in the raw series
            j = i
            while j < n and pos[j] == 1:
                j += 1
            # walk the episode looking for an ALLOW
            entered_at: int | None = None
            for t in range(i, j):
                ts = idx[t]
                assert isinstance(ts, pd.Timestamp)
                verdict = overlay.evaluate(ts)
                log.append((ts, verdict))
                if verdict.decision == OverlayDecision.ALLOW:
                    entered_at = t
                    break
                if verdict.decision == OverlayDecision.SKIP:
                    break
            if entered_at is not None:
                out[entered_at:j] = 1
            i = j
        else:
            i += 1
    return pd.Series(out, index=position.index), log
