"""Trader v1 signal engine.

`evaluate_one_cycle(database_url, settings, ...)` is the public
entry point. Each cycle:

  1. Load every enabled + paper-approved row from
     `trader_strategy_versions`.
  2. For each version, build its typed template (the registry +
     factory in `trader.templates.__init__`).
  3. For each (version × symbol × timeframe) covered by the
     intersection of the version's own spec AND the trader's
     env-configured `TRADER_SYMBOLS` × `TRADER_TIMEFRAMES`:
      a. Acquire a per-`(SIGNAL_EXECUTION, version_id)` Postgres
         advisory transaction lock. Skip if another worker holds
         it.
      b. Find the latest CLOSED candle in `trader_candles`. If
         none, skip.
      c. Dedupe: if a `trader_signals` row already exists for
         `(version, symbol, timeframe, candle_close_ts)`, skip —
         this candle has been evaluated already.
      d. Load enough candle history. We request
         `min_bars_needed() + _SAFETY_MARGIN_BARS`. The +5 buffer
         is intentional defence against off-by-one in indicator
         warmups (the template's own check is sufficient at
         steady state but the safety margin makes the boundary
         non-load-bearing).
      e. Load the current open `PaperPosition` for
         `(version_id, symbol)`, if any.
      f. Call `template.evaluate(candles, position)`.
      g. On HOLD: write a `trader_audit_logs` row, no signal row
         (HOLDs would dominate the signals table at zero
         informational value).
      h. On BUY / SELL / EXIT: INSERT into `trader_signals` with
         ON CONFLICT DO NOTHING (the unique-constraint dedupe
         safety net even though step (c) already pre-checked).

The risk manager (Step 6) reads unprocessed signal rows separately
in its own pass; the signal engine is responsible for SIGNAL
PRODUCTION only. Determinism: same DB state + same params + same
candles ⇒ identical signal output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

import pandas as pd
import psycopg
import structlog
from marketmind_shared.schemas.strategy_spec.common import Timeframe, _StrictModel
from marketmind_shared.schemas.trader import (
    LoopName,
    PaperPosition,
    PositionSide,
    PositionStatus,
    SignalEvaluation,
    SignalKind,
    StrategyState,
    TemplateName,
)
from marketmind_shared.trader.time import now_utc
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from marketmind_workers.trader.config import TraderSettings
from marketmind_workers.trader.heartbeat import touch_heartbeat
from marketmind_workers.trader.locks import try_advisory_xact_lock
from marketmind_workers.trader.templates import (
    SpecTemplate,
    StrategyTemplate,
    build_template,
)

log = structlog.get_logger(__name__)


# Defence against off-by-one in indicator warmup math. The
# template's own min_bars_needed() already accounts for warmup; this
# margin makes the boundary non-load-bearing — the cost is loading a
# handful more rows from trader_candles per evaluation.
_SAFETY_MARGIN_BARS: Final[int] = 5

# trader_strategy_state.state_schema_version (migration 0013). Version 1
# is a Tier-1/Tier-2 StrategyState; version 2 carries a Tier-3 `tier3`
# block (A.6). A row is stamped per its content (see _schema_version);
# this engine reads either.
_STATE_SCHEMA_VERSION_T2: Final[int] = 1
_STATE_SCHEMA_VERSION_T3: Final[int] = 2
_ACCEPTED_STATE_SCHEMA_VERSIONS: Final[frozenset[int]] = frozenset({1, 2})

# Candle fetch size for a Tier-3 spec — its shadow simulation needs the
# full history (§6C). 200k 4h candles is ~90 years: effectively the lot.
_TIER3_FETCH_BARS: Final[int] = 200_000


class _CorruptStateError(Exception):
    """Raised when a `trader_strategy_state` row exists but cannot be
    trusted — unparseable JSONB, a missing field, or a `state_schema_version`
    this engine does not understand. `_evaluate_pair` turns it into the
    §6A.3 disable-and-alert fallback: never trade on unknown state.
    """

    def __init__(self, reason: str, as_of_candle: datetime | None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.as_of_candle = as_of_candle


class SignalEngineResult(_StrictModel):
    """Aggregate stats from one signal-engine cycle. Returned by
    `evaluate_one_cycle`; the RQ wrapper in Step 12 logs this and
    re-enqueues itself for the next boundary.
    """

    versions_loaded: int = 0
    versions_misconfigured: int = 0  # symbols/timeframes outside env's set
    pair_attempts: int = 0  # one per (version, symbol, timeframe)
    pair_locked_out: int = 0  # advisory lock held elsewhere
    pair_no_data: int = 0  # no closed candle for this pair
    pair_insufficient_history: int = 0  # < min_bars_needed bars
    pair_duplicate_signal: int = 0  # already signaled this candle
    pair_state_guarded: int = 0  # idempotency guard: candle already advanced state
    pair_state_disabled: int = 0  # corrupt state -> version disabled + alerted
    evaluations: int = 0  # template.evaluate() calls
    holds: int = 0  # HOLD evaluations (audited, not persisted)
    signals_persisted: int = 0  # BUY / SELL / EXIT inserted


@dataclass
class _CycleState:
    versions_loaded: int = 0
    versions_misconfigured: int = 0
    pair_attempts: int = 0
    pair_locked_out: int = 0
    pair_no_data: int = 0
    pair_insufficient_history: int = 0
    pair_duplicate_signal: int = 0
    pair_state_guarded: int = 0
    pair_state_disabled: int = 0
    evaluations: int = 0
    holds: int = 0
    signals_persisted: int = 0


@dataclass(frozen=True)
class _ActiveVersion:
    """Slim internal view of a `trader_strategy_versions` row.

    The DTO `TraderStrategyVersion` carries 18 fields; only six are
    load-bearing for the signal engine. The internal dataclass keeps
    the hot path small.
    """

    id: UUID
    template: TemplateName
    parameters: dict[str, Any]
    symbols: list[str]
    timeframes: list[Timeframe]


# ---- DB-touching helpers ---------------------------------------------------


def _load_active_versions(conn: psycopg.Connection[Any]) -> list[_ActiveVersion]:
    """Load every enabled + paper-approved strategy version.

    Order: deterministic by `(strategy_id, version DESC, id)` so the
    cycle's iteration order is reproducible.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, template, parameters, symbols, timeframes
            FROM trader_strategy_versions
            WHERE approved_for_paper = TRUE AND enabled = TRUE
            ORDER BY strategy_id, version DESC, id
            """,
        )
        rows = cur.fetchall()
    out: list[_ActiveVersion] = []
    for row in rows:
        id_, template_str, parameters, symbols, timeframes = row
        out.append(
            _ActiveVersion(
                id=UUID(str(id_)),
                template=TemplateName(template_str),
                parameters=dict(parameters),
                symbols=list(symbols),
                timeframes=[Timeframe(tf) for tf in timeframes],
            ),
        )
    return out


def _latest_closed_candle(
    conn: psycopg.Connection[Any],
    symbol: str,
    timeframe: str,
) -> tuple[datetime, datetime] | None:
    """Return ``(open_ts, close_ts)`` of the most recent closed candle.

    Reads only `is_closed = TRUE` rows. None if the ingestion loop
    hasn't covered this pair yet (or the symbol/timeframe is wrong).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT open_ts, close_ts FROM trader_candles
            WHERE symbol = %s AND timeframe = %s AND is_closed = TRUE
            ORDER BY close_ts DESC
            LIMIT 1
            """,
            (symbol, timeframe),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], row[1]


def _signal_exists(
    conn: psycopg.Connection[Any],
    version_id: UUID,
    symbol: str,
    timeframe: str,
    candle_close_ts: datetime,
) -> bool:
    """Dedupe pre-check: have we already produced a signal for this
    `(version, symbol, timeframe, candle_close_ts)`?

    The unique constraint on `trader_signals` enforces this at the
    DB level via ON CONFLICT — this pre-check is a fast-path that
    avoids redundant template.evaluate() work.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM trader_signals
            WHERE strategy_version_id = %s AND symbol = %s
              AND timeframe = %s AND candle_close_ts = %s
            LIMIT 1
            """,
            (str(version_id), symbol, timeframe, candle_close_ts),
        )
        return cur.fetchone() is not None


def _load_candles_df(
    conn: psycopg.Connection[Any],
    symbol: str,
    timeframe: str,
    n_bars: int,
) -> pd.DataFrame:
    """Load the most recent N closed candles into the DataFrame shape
    documented in
    `marketmind_workers.trader.templates.base.StrategyTemplate.evaluate`:

      - tz-aware UTC DatetimeIndex of bar OPEN times, ascending;
      - float64 OHLCV columns.

    The query reads DESC then sorts ASC in Python; for the typical
    n_bars (200-ish) this is faster than sorting in the DB across
    a window scan.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT open_ts, open, high, low, close, volume
            FROM trader_candles
            WHERE symbol = %s AND timeframe = %s AND is_closed = TRUE
            ORDER BY close_ts DESC
            LIMIT %s
            """,
            (symbol, timeframe, n_bars),
        )
        rows = cur.fetchall()
    if not rows:
        # Empty DataFrame: orchestrator's `len(df) < min_bars` check
        # short-circuits before any indicator call, so the column
        # set doesn't need to be set up here.
        return pd.DataFrame()
    # NUMERIC columns decode to Decimal; cast to float for the indicator
    # pipeline (pandas / `ta` operate in float64). The Decimal-only path
    # is at the trader's outer edges (size/PnL math), not inside
    # indicators.
    rows.sort(key=lambda r: r[0])
    return pd.DataFrame(
        {
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[5]) for r in rows],
        },
        index=pd.DatetimeIndex([r[0] for r in rows], name="open_ts"),
    )


def _open_position(
    conn: psycopg.Connection[Any],
    version_id: UUID,
    symbol: str,
) -> PaperPosition | None:
    """Return the currently-OPEN paper position for the pair, or None.

    The DB partial unique index on `trader_paper_positions` guarantees
    at most one OPEN row per `(strategy_version_id, symbol)`, so this
    query has at most one match.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, strategy_version_id, symbol, side,
                   entry_order_id, exit_order_id,
                   entry_price, entry_ts, exit_price, exit_ts,
                   size, stop_price, take_profit_price,
                   status, realised_pnl, realised_pnl_pct, close_reason
            FROM trader_paper_positions
            WHERE strategy_version_id = %s AND symbol = %s AND status = 'OPEN'
            """,
            (str(version_id), symbol),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return PaperPosition(
        id=UUID(str(row[0])),
        strategy_version_id=UUID(str(row[1])),
        symbol=row[2],
        side=PositionSide(row[3]),
        entry_order_id=UUID(str(row[4])),
        exit_order_id=UUID(str(row[5])) if row[5] is not None else None,
        entry_price=row[6],
        entry_ts=row[7],
        exit_price=row[8],
        exit_ts=row[9],
        size=row[10],
        stop_price=row[11],
        take_profit_price=row[12],
        status=PositionStatus(row[13]),
        realised_pnl=row[14],
        realised_pnl_pct=row[15],
        close_reason=row[16],
    )


def _persist_signal(
    conn: psycopg.Connection[Any],
    version_id: UUID,
    symbol: str,
    timeframe: str,
    candle_close_ts: datetime,
    evaluation: SignalEvaluation,
) -> bool:
    """INSERT trader_signals with ON CONFLICT DO NOTHING.

    Returns True if a new row was actually inserted; False if the
    unique constraint deduped the write. The dedupe pre-check in
    the orchestrator should already have caught this; the unique
    constraint is the safety net against race conditions across
    workers (or against a buggy caller that skips the pre-check).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_signals
                (strategy_version_id, symbol, timeframe, candle_close_ts,
                 signal, reason, indicators,
                 proposed_entry_price, proposed_stop_price, proposed_take_profit_price)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (strategy_version_id, symbol, timeframe, candle_close_ts)
            DO NOTHING
            """,
            (
                str(version_id),
                symbol,
                timeframe,
                candle_close_ts,
                evaluation.kind.value,
                evaluation.reason,
                Jsonb(evaluation.indicators),
                evaluation.proposed_entry_price,
                evaluation.proposed_stop_price,
                evaluation.proposed_take_profit_price,
            ),
        )
        return (cur.rowcount or 0) > 0


def _audit_hold(
    conn: psycopg.Connection[Any],
    version_id: UUID,
    symbol: str,
    timeframe: str,
    candle_close_ts: datetime,
    evaluation: SignalEvaluation,
) -> None:
    """Write a HOLD decision to trader_audit_logs.

    HOLDs are intentionally not persisted as trader_signals rows —
    they would dominate the table. The audit log captures the
    decision for forensic queries ("what did we see for BTC/USDT
    around 2026-05-18T14:00Z?") without the table-domination
    problem.
    """
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "candle_close_ts": candle_close_ts.isoformat(),
        "reason": evaluation.reason,
        "indicators": evaluation.indicators,
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_audit_logs (actor, event, entity_type, entity_id, payload)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                "signal_engine",
                "hold_decision",
                "trader_strategy_versions",
                str(version_id),
                Jsonb(payload),
            ),
        )


def _load_strategy_state(
    conn: psycopg.Connection[Any],
    version_id: UUID,
    symbol: str,
    timeframe: str,
) -> tuple[StrategyState | None, datetime | None]:
    """Load the most-recent `trader_strategy_state` row for the pair — the
    seed for this cycle's stateful evaluation (A.5b, design doc §6B).

    Returns `(state, candle_close_ts)`, or `(None, None)` when no row
    exists (a clean cold start). Raises `_CorruptStateError` when a row
    exists but cannot be trusted — unparseable JSONB or a
    `state_schema_version` this engine does not understand. `_evaluate_pair`
    turns that into disable-and-alert (design doc §6A.3): a corrupt row is
    NEVER silently treated as a cold start, which would trade on wrong
    state.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT state, candle_close_ts, state_schema_version
            FROM trader_strategy_state
            WHERE strategy_version_id = %s AND symbol = %s AND timeframe = %s
            ORDER BY candle_close_ts DESC
            LIMIT 1
            """,
            (str(version_id), symbol, timeframe),
        )
        row = cur.fetchone()
    if row is None:
        return None, None
    state_json, candle_close_ts, schema_version = row
    if schema_version not in _ACCEPTED_STATE_SCHEMA_VERSIONS:
        raise _CorruptStateError(
            f"state_schema_version {schema_version} is not understood by this "
            f"engine (accepts {sorted(_ACCEPTED_STATE_SCHEMA_VERSIONS)})",
            candle_close_ts,
        )
    try:
        return StrategyState.model_validate(state_json), candle_close_ts
    except ValidationError as exc:
        raise _CorruptStateError(
            f"trader_strategy_state JSONB failed to deserialize: {exc}",
            candle_close_ts,
        ) from exc


def _handle_corrupt_state(
    conn: psycopg.Connection[Any],
    version_id: UUID,
    symbol: str,
    timeframe: str,
    *,
    reason: str,
    as_of_candle: datetime | None,
) -> None:
    """Disable a strategy version and raise an operator alert because its
    persisted state cannot be trusted (design doc §6A.3).

    The version is set `enabled = FALSE` and stays disabled until an
    operator investigates and re-enables it — there is no auto-recovery,
    because a recoverable stop is always safer than trading on unknown
    state. A WARNING `trader_alerts` row carries the forensic payload.
    Both writes commit inside the caller's per-pair transaction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE trader_strategy_versions SET enabled = FALSE WHERE id = %s",
            (str(version_id),),
        )
        body = (
            f"strategy_version_id={version_id} symbol={symbol} "
            f"timeframe={timeframe} as_of_candle={as_of_candle} — {reason}. "
            "The version is disabled and will not trade; investigate the "
            "trader_strategy_state row, then re-enable it manually."
        )
        cur.execute(
            """
            INSERT INTO trader_alerts (channel, severity, subject, body)
            VALUES ('telegram', 'warning', %s, %s)
            """,
            ("Strategy auto-disabled — corrupt stateful state", body),
        )
    log.error(
        "strategy_disabled_corrupt_state",
        version_id=str(version_id),
        symbol=symbol,
        timeframe=timeframe,
        as_of_candle=as_of_candle.isoformat() if as_of_candle is not None else None,
        reason=reason,
    )


def _persist_strategy_state(
    conn: psycopg.Connection[Any],
    version_id: UUID,
    symbol: str,
    timeframe: str,
    candle_close_ts: datetime,
    next_state: StrategyState,
) -> None:
    """INSERT a `trader_strategy_state` row, ON CONFLICT DO NOTHING.

    Append-only: one row per evaluated candle (design doc §6B.4). The
    UNIQUE (version, symbol, timeframe, candle_close_ts) makes a duplicate
    advance — a cross-worker race past the advisory lock — a silent no-op
    (design doc §6A.2). The row is stamped schema version 2 when it
    carries a Tier-3 `tier3` block, else 1 (design doc §6C-Q3).
    """
    schema_version = (
        _STATE_SCHEMA_VERSION_T3
        if next_state.tier3 is not None
        else _STATE_SCHEMA_VERSION_T2
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_strategy_state
                (strategy_version_id, symbol, timeframe, candle_close_ts,
                 state, state_schema_version)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (strategy_version_id, symbol, timeframe, candle_close_ts)
            DO NOTHING
            """,
            (
                str(version_id),
                symbol,
                timeframe,
                candle_close_ts,
                Jsonb(next_state.model_dump(mode="json")),
                schema_version,
            ),
        )


# ---- Orchestrator ----------------------------------------------------------


def evaluate_one_cycle(
    database_url: str,
    settings: TraderSettings,
    *,
    run_id: UUID | None = None,
    now: datetime | None = None,
) -> SignalEngineResult:
    """Run one signal-engine pass.

    Parameters
    ----------
    database_url
        psycopg connection string.
    settings
        TraderSettings. Defines the `TRADER_SYMBOLS` × `TRADER_TIMEFRAMES`
        configured set. A strategy version is only evaluated for the
        intersection of its own symbols/timeframes AND this set.
    run_id
        Optional `trader_bot_runs` row to heartbeat.
    now
        Optional clock override for tests; defaults to `now_utc()`.

    Returns
    -------
    SignalEngineResult
        Aggregate stats. Each per-pair operation runs in its own
        transaction; a failure for one pair does NOT abort the cycle.

    Notes
    -----
    Determinism: same DB state + same params + same candles ⇒
    identical signal output. The `_now` parameter exists only for
    deterministic logging timestamps; the strategy evaluation does
    not consult the clock.
    """
    state = _CycleState()
    _ = now if now is not None else now_utc()  # logged via structlog contextvars in caller

    config_symbols = set(settings.symbols_list())
    config_timeframes = set(settings.timeframes_list())

    with psycopg.connect(database_url) as conn:
        if run_id is not None:
            with conn.transaction():
                touch_heartbeat(conn, run_id, phase="signal")

        versions = _load_active_versions(conn)
        state.versions_loaded = len(versions)

        log.info(
            "signal_cycle_starting",
            versions=len(versions),
            config_symbols=sorted(config_symbols),
            config_timeframes=sorted(config_timeframes),
        )

        for version in versions:
            # Intersection of the version's spec and the trader's
            # configured set. If empty, the version is misconfigured
            # for this trader environment — skip with a stat bump.
            version_symbols = sorted(set(version.symbols) & config_symbols)
            version_timeframes = sorted(
                {tf.value for tf in version.timeframes} & config_timeframes,
            )
            if not version_symbols or not version_timeframes:
                state.versions_misconfigured += 1
                log.warning(
                    "version_misconfigured_for_trader_env",
                    version_id=str(version.id),
                    version_symbols=version.symbols,
                    version_timeframes=[tf.value for tf in version.timeframes],
                )
                continue

            try:
                template: StrategyTemplate = build_template(version.template, version.parameters)
            except Exception:
                # Bad parameters land here; the version is unusable
                # until the operator fixes it. Log + skip.
                log.exception(
                    "version_build_template_failed",
                    version_id=str(version.id),
                    template=version.template.value,
                )
                state.versions_misconfigured += 1
                continue

            min_bars = template.min_bars_needed()
            if isinstance(template, SpecTemplate) and template.is_tier3:
                # A Tier-3 spec's shadow simulation indexes bars absolutely
                # and persists a SignalHistory keyed on those indices — it
                # must see the full candle history, not a sliding window
                # (design doc §6C). _TIER3_FETCH_BARS is effectively
                # unbounded for any realistic strategy lifetime.
                fetch_bars = _TIER3_FETCH_BARS
            else:
                fetch_bars = min_bars + _SAFETY_MARGIN_BARS

            for symbol in version_symbols:
                for timeframe in version_timeframes:
                    state.pair_attempts += 1
                    _evaluate_pair(
                        conn,
                        version=version,
                        template=template,
                        symbol=symbol,
                        timeframe=timeframe,
                        min_bars=min_bars,
                        fetch_bars=fetch_bars,
                        state=state,
                    )

    result = SignalEngineResult(
        versions_loaded=state.versions_loaded,
        versions_misconfigured=state.versions_misconfigured,
        pair_attempts=state.pair_attempts,
        pair_locked_out=state.pair_locked_out,
        pair_no_data=state.pair_no_data,
        pair_insufficient_history=state.pair_insufficient_history,
        pair_duplicate_signal=state.pair_duplicate_signal,
        pair_state_guarded=state.pair_state_guarded,
        pair_state_disabled=state.pair_state_disabled,
        evaluations=state.evaluations,
        holds=state.holds,
        signals_persisted=state.signals_persisted,
    )
    log.info("signal_cycle_complete", **result.model_dump())
    return result


def _evaluate_pair(
    conn: psycopg.Connection[Any],
    *,
    version: _ActiveVersion,
    template: StrategyTemplate,
    symbol: str,
    timeframe: str,
    min_bars: int,
    fetch_bars: int,
    state: _CycleState,
) -> None:
    """Single-pair evaluation inside its own transaction.

    Pulled out of the orchestrator to keep the transaction-scope
    boundary obvious: every advisory-lock + read + write for one
    pair happens inside this `with conn.transaction():` block, so a
    failure rolls back the lock + any partial writes cleanly.
    """
    with conn.transaction():
        if not try_advisory_xact_lock(conn, LoopName.SIGNAL_EXECUTION, version.id):
            state.pair_locked_out += 1
            return

        latest = _latest_closed_candle(conn, symbol, timeframe)
        if latest is None:
            state.pair_no_data += 1
            return
        _, latest_close_ts = latest

        if _signal_exists(conn, version.id, symbol, timeframe, latest_close_ts):
            state.pair_duplicate_signal += 1
            return

        df = _load_candles_df(conn, symbol, timeframe, fetch_bars)
        if len(df) < min_bars:
            state.pair_insufficient_history += 1
            log.info(
                "signal_pair_insufficient_history",
                version_id=str(version.id),
                symbol=symbol,
                timeframe=timeframe,
                have_bars=len(df),
                need_bars=min_bars,
            )
            return

        position = _open_position(conn, version.id, symbol)

        if isinstance(template, SpecTemplate) and template.is_stateful:
            # Stateful (Tier-2) spec — seed from persisted state, guard
            # against re-advancing an already-evaluated candle, and write
            # the advanced state row atomically with the signal below.
            try:
                prior_state, prev_state_ts = _load_strategy_state(
                    conn,
                    version.id,
                    symbol,
                    timeframe,
                )
            except _CorruptStateError as exc:
                # Never trade on unknown state — disable the version and
                # alert the operator (design doc §6A.3).
                _handle_corrupt_state(
                    conn,
                    version.id,
                    symbol,
                    timeframe,
                    reason=exc.reason,
                    as_of_candle=exc.as_of_candle,
                )
                state.pair_state_disabled += 1
                return
            if prev_state_ts is not None and latest_close_ts <= prev_state_ts:
                # Idempotency guard (design doc §6A.2): this candle's state
                # already advanced on an earlier cycle. The 1-minute tick
                # re-evaluates a 4h HOLD candle ~240x — re-advancing the
                # regime latch / ratchet extremum would corrupt it.
                state.pair_state_guarded += 1
                return
            try:
                evaluation, next_state = template.evaluate_stateful(
                    df,
                    position,
                    prior_state,
                )
            except Exception as exc:
                # Any failure evaluating the stateful spec means the result
                # cannot be trusted — treat it exactly like corrupt state:
                # disable + alert, never trade on it (design doc §6A.3).
                # The broad except is deliberate — one version's evaluation
                # bug must not crash the cycle for every other version.
                log.exception(
                    "stateful_evaluation_failed",
                    version_id=str(version.id),
                    symbol=symbol,
                    timeframe=timeframe,
                )
                _handle_corrupt_state(
                    conn,
                    version.id,
                    symbol,
                    timeframe,
                    reason=f"stateful evaluation raised {type(exc).__name__}: {exc}",
                    as_of_candle=latest_close_ts,
                )
                state.pair_state_disabled += 1
                return
            # One state row per evaluated candle (§6B.4), committed inside
            # this same transaction as the signal below — atomic (§6A.2).
            _persist_strategy_state(
                conn,
                version.id,
                symbol,
                timeframe,
                latest_close_ts,
                next_state,
            )
        else:
            evaluation = template.evaluate(df, position)
        state.evaluations += 1

        if evaluation.kind is SignalKind.HOLD:
            _audit_hold(conn, version.id, symbol, timeframe, latest_close_ts, evaluation)
            state.holds += 1
            return

        # Defensive: every non-HOLD signal MUST carry a positive
        # stop. The template's docstring + the DTO already enforce
        # this for BUY signals; for EXIT the template carries the
        # position's stop forward. The check is a runtime guard
        # against a future template bug.
        if evaluation.proposed_stop_price <= Decimal(0):
            log.error(
                "signal_missing_stop_price",
                version_id=str(version.id),
                symbol=symbol,
                kind=evaluation.kind.value,
                reason=evaluation.reason,
            )
            return

        inserted = _persist_signal(
            conn,
            version.id,
            symbol,
            timeframe,
            latest_close_ts,
            evaluation,
        )
        if inserted:
            state.signals_persisted += 1
        else:
            # Dedupe lost to ON CONFLICT — another worker beat us.
            state.pair_duplicate_signal += 1


__all__ = ["SignalEngineResult", "evaluate_one_cycle"]
