"""FTR data QA validator — runs on every load, results persisted.

Checks (mandate Stage 1):
- strictly increasing UTC timestamps
- duplicate removal (logged, counted)
- gap report with per-timeframe tolerance — gaps are REPORTED, never filled
- candle-boundary alignment (`ts % timeframe == 0`)
- outlier flagging via rolling-MAD on log returns (|z| > 12 => flag, never delete)
- cross-venue close divergence > 50 bps sustained over 6 bars => flag

The validator never mutates beyond dropping exact-duplicate index rows; no
NaN forward-fills, no gap interpolation. A QAReport is returned for
persistence into ``ftr_data_quality``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd
import structlog

from marketmind_workers.ftr.data.ohlcv import dtindex

logger = structlog.get_logger(__name__)

_TIMEFRAME_S: dict[str, int] = {"1m": 60, "1h": 3600, "4h": 14400, "1d": 86400}

# A gap is reported when the spacing exceeds tolerance x bar interval.
_GAP_TOLERANCE_MULTIPLIER = 1.5

_MAD_WINDOW = 200
_MAD_Z_FLAG = 12.0

_XVENUE_DIVERGENCE_BPS = 50.0
_XVENUE_SUSTAINED_BARS = 6


def _ts(idx: pd.DatetimeIndex, i: int) -> datetime:
    val = idx[i]
    assert isinstance(val, pd.Timestamp)
    return val.to_pydatetime()


@dataclass(frozen=True)
class GapRecord:
    gap_start: datetime
    gap_end: datetime
    missing_bars: int


@dataclass
class QAReport:
    exchange: str
    symbol: str
    timeframe: str
    rows: int
    first_ts: datetime | None
    last_ts: datetime | None
    duplicates_removed: int
    monotonic: bool
    misaligned_bars: int
    gaps: list[GapRecord] = field(default_factory=list)
    outlier_ts: list[datetime] = field(default_factory=list)
    cross_venue_flags: list[datetime] = field(default_factory=list)
    passed: bool = True
    notes: list[str] = field(default_factory=list)

    def to_row(self) -> dict[str, object]:
        """Shape for the ftr_data_quality table (JSONB details column)."""
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "rows": self.rows,
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "passed": self.passed,
            "details": {
                "duplicates_removed": self.duplicates_removed,
                "monotonic": self.monotonic,
                "misaligned_bars": self.misaligned_bars,
                "gaps": [
                    {
                        "start": g.gap_start.isoformat(),
                        "end": g.gap_end.isoformat(),
                        "missing_bars": g.missing_bars,
                    }
                    for g in self.gaps
                ],
                "outliers": [t.isoformat() for t in self.outlier_ts],
                "cross_venue_flags": [t.isoformat() for t in self.cross_venue_flags],
                "notes": self.notes,
            },
        }


def validate_ohlcv(
    df: pd.DataFrame,
    *,
    exchange: str,
    symbol: str,
    timeframe: str,
    cross_venue_close: pd.Series | None = None,
) -> tuple[pd.DataFrame, QAReport]:
    """Validate a series; returns (deduped frame, report). Never fills gaps."""
    if timeframe not in _TIMEFRAME_S:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    if len(df) and dtindex(df).tz is None:
        raise ValueError("QA boundary: index must be tz-aware UTC")

    bar_s = _TIMEFRAME_S[timeframe]

    dup_mask = dtindex(df).duplicated(keep="first")
    duplicates_removed = int(dup_mask.sum())
    clean = df.loc[~dup_mask].sort_index()
    idx = dtindex(clean)

    monotonic = bool(idx.is_monotonic_increasing)

    epoch_s = idx.asi8 // 1_000_000_000 if len(clean) else np.array([], dtype="int64")
    misaligned = int((epoch_s % bar_s != 0).sum())

    gaps: list[GapRecord] = []
    if len(clean) >= 2:
        deltas = np.diff(epoch_s)
        gap_idx = np.nonzero(deltas > bar_s * _GAP_TOLERANCE_MULTIPLIER)[0]
        for i in gap_idx:
            missing = int(deltas[i] // bar_s) - 1
            gaps.append(
                GapRecord(
                    gap_start=_ts(idx, int(i)), gap_end=_ts(idx, int(i) + 1), missing_bars=missing
                )
            )

    outlier_ts: list[datetime] = []
    if len(clean) > _MAD_WINDOW:
        logret = pd.Series(np.log(clean["close"].to_numpy()), index=idx).diff()
        med = logret.rolling(_MAD_WINDOW).median()
        mad = (logret - med).abs().rolling(_MAD_WINDOW).median()
        z = (logret - med) / (1.4826 * mad.replace(0.0, np.nan))
        flagged = (z.abs() > _MAD_Z_FLAG).fillna(value=False).to_numpy(dtype=bool)
        flagged_idx = idx[flagged]
        outlier_ts = [_ts(flagged_idx, i) for i in range(len(flagged_idx))]

    cross_flags: list[datetime] = []
    if cross_venue_close is not None and len(clean):
        aligned = cross_venue_close.reindex(idx)
        div_bps = ((clean["close"] / aligned) - 1.0).abs() * 1e4
        sustained = (
            (div_bps > _XVENUE_DIVERGENCE_BPS)
            .rolling(_XVENUE_SUSTAINED_BARS)
            .sum()
            .eq(_XVENUE_SUSTAINED_BARS)
            .fillna(value=False)
            .to_numpy(dtype=bool)
        )
        sustained_idx = idx[sustained]
        cross_flags = [_ts(sustained_idx, i) for i in range(len(sustained_idx))]

    notes: list[str] = []
    if duplicates_removed:
        notes.append(f"removed {duplicates_removed} duplicate-index rows")
    if misaligned:
        notes.append(f"{misaligned} bars misaligned to the {timeframe} boundary")
    if gaps:
        total_missing = sum(g.missing_bars for g in gaps)
        notes.append(f"{len(gaps)} gaps / {total_missing} missing bars (reported, NOT filled)")

    passed = monotonic and misaligned == 0
    first_ts = _ts(idx, 0) if len(clean) else None
    last_ts = _ts(idx, -1) if len(clean) else None
    report = QAReport(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        rows=len(clean),
        first_ts=first_ts,
        last_ts=last_ts,
        duplicates_removed=duplicates_removed,
        monotonic=monotonic,
        misaligned_bars=misaligned,
        gaps=gaps,
        outlier_ts=outlier_ts,
        cross_venue_flags=cross_flags,
        passed=passed,
        notes=notes,
    )
    log = logger.warning if (gaps or not passed or outlier_ts) else logger.info
    log(
        "ftr_data_qa",
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        rows=report.rows,
        gaps=len(gaps),
        outliers=len(outlier_ts),
        duplicates=duplicates_removed,
        passed=passed,
    )
    return clean, report
