"""Phase C C.6 — trader cycle weekend-skip dispatch for non-24/7 markets.

The trader's `ingest_one_cycle` iterates `(symbol, timeframe)` pairs from
TRADER_SYMBOLS and fetches OHLCV from the corresponding adapter. Until
C.6 the loop fetched unconditionally — fine for crypto (24/7), but for
FX / metals / equity symbols on Saturday or Sunday the Oanda / Alpaca
adapters either return empty responses or 4xx errors. After three
consecutive errors per-pair the ingestion's `_update_error_state` fires
a `data_feed_failure` alert (severity=critical). C.6 prevents this
alert spam by skipping the fetch entirely on weekend days for
non-24/7 asset classes.

Companion to C.5's `session_filter.drop_weekends_if_session_closed`:
  - C.5 (backtest): drops weekend rows from historical DataFrames so
    the engine produces honest verdicts on FX strategies
  - C.6 (live trader): skips weekend cycles for FX symbols so live
    ingestion doesn't generate false-positive alerts

Minimum-path scope per the previous session's analysis: structural
weekday >= 5 filter, no calendar library, no per-venue holiday
tables. Holiday handling (e.g. equity-market closures for Christmas
Day on a Friday) lands in C.4-full alongside equities in C.9.

Crypto bit-identity: crypto_spot returns False unconditionally; the
3 production strategies' cycle behaviour is byte-identical to pre-C.6.
"""

from __future__ import annotations

from datetime import datetime

from marketmind_shared.schemas.strategy_spec import AssetClass


def should_skip_weekend(
    asset_class: AssetClass,
    now_utc: datetime,
) -> bool:
    """True iff the trader should SKIP this cycle because the symbol's
    venue is weekend-closed and today is Sat/Sun.

    Behaviour by AssetClass:
      crypto_spot   → False unconditionally (24/7)
      fx_spot       → True on Sat (weekday=5) + Sun (weekday=6)
      metals_spot   → True on Sat + Sun (Oanda XAU/USD weekend close)
      equity_etf    → True on Sat + Sun (Alpaca; markets closed)
      equity_single → True on Sat + Sun

    Pandas / Python convention: Monday=0..Sunday=6. Weekend = weekday >= 5.

    `now_utc` must be a tz-aware UTC datetime. The caller (ingestion
    loop) passes `now_utc()` from `marketmind_shared.trader.time` which
    is always tz-aware UTC.

    Holiday handling (Christmas Day on a Friday, NFP on first Friday,
    etc.) is OUT OF SCOPE for C.6 — landed in C.4-full alongside
    equities in C.9 via pandas_market_calendars. The minimum-path
    weekend skip is sufficient to suppress the FX 3-strikes alert
    storm; equity-market holiday-handling generates noise but no
    spurious alerts because Alpaca returns empty bars cleanly.
    """
    if asset_class == "crypto_spot":
        return False
    return now_utc.weekday() >= 5


__all__ = ["should_skip_weekend"]
