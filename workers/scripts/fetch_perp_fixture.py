"""Phase E.2 — one-shot fetch of BTC + ETH USDT-margined PERPETUAL-swap
data (last-price OHLCV + mark price + 8h funding rate) for the eventual
market-neutral perp-pair spread research.

VENUE CHOICE — Binance USDM perpetuals (ccxt `binanceusdm`)
----------------------------------------------------------
Chosen over Bybit / OKX because it is the deepest-liquidity crypto-perp
venue with the LONGEST clean free history and the most mature ccxt
support: BTC/USDT:USDT trades from 2019-09-08, ETH/USDT:USDT shortly
after, funding every 8h. Crucially the historical OHLCV, MARK-price
klines, and funding-rate history are all PUBLIC endpoints — NO API KEY
is required (verified). That keeps Phase E key-free, unlike the FX
(Oanda) and equity (Alpaca) fixtures.

WHAT WE FETCH (both legs) and WHY each matters for honest perp backtest
----------------------------------------------------------------------
  - LAST-price 1h OHLCV       -> the tradeable series; fills happen here.
  - MARK-price close per bar  -> Binance funding + unrealized PnL +
                                 liquidation are computed on MARK, not
                                 last. Storing mark_close alongside the
                                 last-price OHLCV lets E.3 charge funding
                                 and mark-to-market HONESTLY rather than
                                 pretending last==mark. (Mark klines carry
                                 zero volume — they are a price index, not
                                 a tradeable book; we keep only the close.)
  - 8h FUNDING-rate history   -> the scheduled funding payments. Stored as
                                 its own series (8h cadence) keyed on the
                                 funding timestamp (00:00/08:00/16:00 UTC),
                                 which always lands on a 1h bar open, so it
                                 is time-alignable to the OHLCV by
                                 construction.

WORKING TIMEFRAME — 1h (justification)
--------------------------------------
1h is the default working timeframe for the perp-pair spread:
  - Funding is 8h, so 1h tiles a funding interval cleanly (8 bars).
  - A market-neutral spread pays cost on BOTH legs; the project log's cadence
    rule (lower TF => more cost drag + noise) bites twice, so 1h is the
    prudent balance vs 15m.
  - 1h over a multi-year window still yields ~50k bars per leg — ample
    for a meaningful walk-forward.
15m can be added later if E.3's spread half-life analysis wants finer
granularity; the fetch is parametric on `--timeframe`.

OUTPUT (tests/fixtures/market/, mirrors the btc_usdt / eurusd fixtures)
----------------------------------------------------------------------
  binance_btc_usdt_perp_1h.parquet      open/high/low/close/volume + mark_close
  binance_btc_usdt_perp_funding.parquet funding_rate (8h series)
  binance_eth_usdt_perp_1h.parquet      "
  binance_eth_usdt_perp_funding.parquet "
  Index: tz-aware UTC DatetimeIndex of bar/funding OPEN times, ascending,
         NAMELESS (the Stage-2b lesson: a named filter-frame index breaks
         the translator's multi-timeframe merge_asof; nameless is the safe
         default for any fixture that might later be a filter leg).

PERP UNIVERSE (Phase E.2 + E.5a): BTC, ETH (E.2) + SOL, BNB, XRP, DOGE, AVAX
(E.5a) — all USDT-margined Binance USDM perpetuals, deep liquidity + multi-year
history, for the multi-asset slow-trend portfolio (E.5b). The default below
fetches the whole universe; each symbol starts at its own venue inception (newer
assets have shorter history — the fetch reports the actual start).

USAGE
-----
  uv run python workers/scripts/fetch_perp_fixture.py   # whole universe
  uv run python workers/scripts/fetch_perp_fixture.py \
      --symbols SOL/USDT:USDT,AVAX/USDT:USDT --start 2020-01-01 --end 2026-06-01

RESEARCH-ONLY. No keys, no trading — this feeds the backtest gauntlet
only. No live perp path exists (and the live trader stays spot-long-only).
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import ccxt
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "market"

# ccxt caps Binance fetch_ohlcv / fetch_funding_rate_history at ~1000
# records per call. Page by advancing the cursor past the last record.
_PAGE = 1000
_INTERPAGE_SLEEP_S = 0.25  # on top of ccxt's enableRateLimit throttle
_TF_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


def _make_client() -> ccxt.binanceusdm:
    """Public binanceusdm client — no credentials. enableRateLimit lets
    ccxt self-throttle so we stay under Binance's weight limits.
    """
    return ccxt.binanceusdm({"enableRateLimit": True})


def _to_ms(d: datetime) -> int:
    return int(d.timestamp() * 1000.0)


def _fetch_ohlcv(
    client: ccxt.binanceusdm,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    *,
    price: str | None,
) -> list[list[float]]:
    """Paginated OHLCV in [start_ms, end_ms). `price='mark'` pulls the
    mark-price klines (else last-price). Cursor advances by one bar past
    the last returned timestamp to avoid re-fetching the boundary bar.
    """
    tf_ms = _TF_MS[timeframe]
    params: dict[str, str] = {"price": price} if price else {}
    rows: list[list[float]] = []
    cursor = start_ms
    label = f"{symbol} {timeframe}{' mark' if price else ''}"
    while cursor < end_ms:
        batch = cast(
            "list[list[float]]",
            client.fetch_ohlcv(symbol, timeframe, since=cursor, limit=_PAGE, params=params),
        )
        if not batch:
            break
        rows.extend(b for b in batch if b[0] < end_ms)
        last_ts = int(batch[-1][0])
        if last_ts < cursor:  # no forward progress — venue exhausted
            break
        cursor = last_ts + tf_ms
        print(f"  {label}: +{len(batch)} (total {len(rows)}, through "
              f"{datetime.fromtimestamp(last_ts / 1000, tz=UTC).date()})")
        time.sleep(_INTERPAGE_SLEEP_S)
    return rows


def _fetch_funding(
    client: ccxt.binanceusdm,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[dict[str, object]]:
    """Paginated 8h funding-rate history in [start_ms, end_ms)."""
    rows: list[dict[str, object]] = []
    cursor = start_ms
    while cursor < end_ms:
        batch = cast(
            "list[dict[str, object]]",
            client.fetch_funding_rate_history(symbol, since=cursor, limit=_PAGE),
        )
        if not batch:
            break
        fresh = [f for f in batch if int(cast("int", f["timestamp"])) < end_ms]
        rows.extend(fresh)
        last_ts = int(cast("int", batch[-1]["timestamp"]))
        if last_ts < cursor:
            break
        cursor = last_ts + 1  # funding ts are 8h apart; +1ms avoids the dup
        print(f"  {symbol} funding: +{len(batch)} (total {len(rows)}, through "
              f"{datetime.fromtimestamp(last_ts / 1000, tz=UTC).date()})")
        time.sleep(_INTERPAGE_SLEEP_S)
    return rows


def _ohlcv_frame(last_rows: list[list[float]], mark_rows: list[list[float]]) -> pd.DataFrame:
    """Build the last-price OHLCV frame and graft on the aligned
    mark-price close as `mark_close`.
    """
    cols = pd.Index(["ts_ms", "open", "high", "low", "close", "volume"])
    last = pd.DataFrame(last_rows, columns=cols)
    last["timestamp"] = pd.to_datetime(last["ts_ms"], unit="ms", utc=True)
    last = (
        last.drop(columns=["ts_ms"]).set_index("timestamp").sort_index()
    )
    last = last[~last.index.duplicated(keep="first")]

    mark = pd.DataFrame(mark_rows, columns=cols)
    mark["timestamp"] = pd.to_datetime(mark["ts_ms"], unit="ms", utc=True)
    mark_close = cast(
        "pd.Series",
        mark.drop(columns=["ts_ms"]).set_index("timestamp").sort_index()["close"],
    )
    mark_close = cast("pd.Series", mark_close[~mark_close.index.duplicated(keep="first")])

    last["mark_close"] = mark_close.reindex(last.index)
    return cast("pd.DataFrame", last.rename_axis(None))


def _funding_frame(rows: list[dict[str, object]]) -> pd.DataFrame:
    # Binance stamps each funding settlement a few ms off the exact
    # 00:00/08:00/16:00 UTC instant (observed jitter < 50ms, always
    # post-hour). Round to the hour so the 8h funding series aligns
    # EXACTLY onto the 1h bar grid — otherwise a downstream asof/exact
    # join silently drops ~45% of funding events on the ms mismatch.
    raw_ts = pd.to_datetime(
        [int(cast("int", r["timestamp"])) for r in rows], unit="ms", utc=True,
    )
    df = pd.DataFrame(
        {
            # .dt.round (not DatetimeIndex.round) — the index-level round is
            # mistyped as numeric round(decimals) in pandas-stubs.
            "timestamp": pd.Series(raw_ts).dt.round("h"),
            "funding_rate": [float(cast("float", r["fundingRate"])) for r in rows],
        },
    )
    df = df.set_index("timestamp").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return cast("pd.DataFrame", df.rename_axis(None))


def _confirm(tag: str, ohlcv: pd.DataFrame, funding: pd.DataFrame, timeframe: str) -> None:
    """Print the fixture confirmation the task requires: range, counts,
    gaps, mark coverage, and funding<->OHLCV time-alignability.
    """
    tf_ms = _TF_MS[timeframe]
    idx = pd.DatetimeIndex(ohlcv.index)
    deltas = idx.to_series().diff().dropna()
    gaps = cast("pd.Series", deltas[deltas > pd.Timedelta(milliseconds=tf_ms)])
    expected = int((idx[-1] - idx[0]) / pd.Timedelta(milliseconds=tf_ms)) + 1
    mark_missing = int(ohlcv["mark_close"].isna().sum())
    # alignability: every funding timestamp must fall on an OHLCV bar open
    fund_idx = pd.DatetimeIndex(funding.index)
    aligned = fund_idx.isin(idx)
    print(f"\n=== {tag} ===")
    print(f"  OHLCV ({timeframe}): {len(ohlcv)} bars  {idx[0]} -> {idx[-1]}")
    print(f"    expected contiguous bars: {expected}  actual: {len(ohlcv)}  "
          f"missing: {expected - len(ohlcv)}  gaps>1bar: {len(gaps)}")
    if len(gaps):
        worst = gaps.sort_values(ascending=False).head(3)
        for ts, d in worst.items():
            print(f"      gap {d} ending {ts}")
    print(f"    mark_close coverage: {len(ohlcv) - mark_missing}/{len(ohlcv)} "
          f"({'COMPLETE' if mark_missing == 0 else f'{mark_missing} NaN'})")
    print(f"  FUNDING: {len(funding)} records  {fund_idx[0]} -> {fund_idx[-1]}  "
          f"(8h cadence ⇒ ~{len(funding) * 8 / 24 / 365:.1f}yr)")
    print(f"    funding↔OHLCV time-alignable: {int(aligned.sum())}/{len(fund_idx)} "
          f"funding stamps land on a 1h bar "
          f"({'ALL ALIGNED' if bool(aligned.all()) else 'MISALIGNMENT — investigate'})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch BTC/ETH Binance perp fixtures.")
    parser.add_argument(
        "--symbols",
        default=(
            "BTC/USDT:USDT,ETH/USDT:USDT,SOL/USDT:USDT,BNB/USDT:USDT,"
            "XRP/USDT:USDT,DOGE/USDT:USDT,AVAX/USDT:USDT"
        ),
    )
    parser.add_argument("--timeframe", default="1h", choices=sorted(_TF_MS))
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2026-06-01")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)
    start_ms, end_ms = _to_ms(start), _to_ms(end)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)

    client = _make_client()
    client.load_markets()

    for sym in symbols:
        if sym not in client.symbols:
            print(f"ERROR: {sym} not a binanceusdm market", file=sys.stderr)
            return 1
        print(f"\n########## {sym} ({args.start} -> {args.end}, {args.timeframe}) ##########")
        last_rows = _fetch_ohlcv(client, sym, args.timeframe, start_ms, end_ms, price=None)
        mark_rows = _fetch_ohlcv(client, sym, args.timeframe, start_ms, end_ms, price="mark")
        fund_rows = _fetch_funding(client, sym, start_ms, end_ms)
        if not last_rows or not fund_rows:
            print(f"ERROR: empty fetch for {sym}", file=sys.stderr)
            return 1

        ohlcv = _ohlcv_frame(last_rows, mark_rows)
        funding = _funding_frame(fund_rows)
        # Clip funding to the OHLCV bar range. Newer assets (SOL/DOGE/AVAX)
        # publish a few funding stamps a few hours BEFORE their first kline;
        # those leading records have no bar to land on and are never tradeable
        # (you can't hold a position before the first bar). Dropping them makes
        # the funding series N/N alignable. No-op for BTC/ETH (funding and
        # OHLCV both start at the same instant).
        funding = funding.loc[
            (funding.index >= ohlcv.index[0]) & (funding.index <= ohlcv.index[-1])
        ]

        # tag e.g. "btc_usdt_perp" from "BTC/USDT:USDT"
        base = sym.split("/")[0].lower()
        tag = f"{base}_usdt_perp"
        p_ohlcv = FIXTURE_DIR / f"binance_{tag}_{args.timeframe}.parquet"
        p_fund = FIXTURE_DIR / f"binance_{tag}_funding.parquet"
        ohlcv.to_parquet(p_ohlcv, engine="pyarrow", compression="snappy")
        funding.to_parquet(p_fund, engine="pyarrow", compression="snappy")
        _confirm(tag, ohlcv, funding, args.timeframe)
        print(f"  wrote {p_ohlcv.name} ({p_ohlcv.stat().st_size / 1024:.0f} KB)")
        print(f"  wrote {p_fund.name} ({p_fund.stat().st_size / 1024:.0f} KB)")

    print("\nDone. Perp fixtures cached for Phase E.3 (multi-leg spread research).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
