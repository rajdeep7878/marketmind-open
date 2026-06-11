#!/usr/bin/env python
"""Seed a trader v1 strategy version from a MarketMind extraction.

Operational script (Phase 12 Step 14). Reads a MarketMind
`extracted_strategies` row + its latest `backtest_results` +
`overfitting_analyses`, translates them into the trader's
`backtest_metrics` JSONB contract, and INSERTs a new
`trader_strategy_versions` row with `approved_for_paper=FALSE`.

The operator approves the version manually via
`POST /trader/strategies/{id}/approve_paper` after review. This
script NEVER auto-approves — see "Critical safety" below.

Usage:
    DATABASE_URL=postgresql://marketmind:marketmind_dev@localhost:5432/marketmind \\
    uv run python scripts/trader_seed_strategy.py \\
        --extraction-id 00000000-0000-0000-0000-000000000000 \\
        --name "MA Trend BTC 4h" \\
        --template ma_trend \\
        --parameters-json '{"fast_ema_period": 12, "slow_ema_period": 26, ...}' \\
        --symbols BTC/USDT \\
        --timeframes 4h \\
        --risk-pct 0.005 \\
        [--fee-bps 10] \\
        [--slippage-bps 10] \\
        [--notes "Adapted from ..."] \\
        [--dry-run]

Template routing. With `--template` omitted, the script auto-routes the
spec to the generic `spec` template (TemplateName.SPEC, A.5a's
SpecTemplate) — for ANY spec the SpecTemplate can run, stateful or not.
This covers v2 stateful specs and v1-style specs that use an indicator no
hand-coded template models (e.g. Supertrend). To instead map a
non-stateful spec onto a hand-coded v1 template, supply `--template` and
`--parameters-json` together (a stateful spec rejects them — it must use
the SpecTemplate). `--dry-run` prints the routing decision.

By default the script picks the most recent `backtest_results` row
for the extraction (ORDER BY created_at DESC LIMIT 1) and the
unique overfitting analysis linked to it. When the extraction has
multiple backtests (e.g., the same spec re-run at a different
timeframe), pin the exact pair explicitly:

    --backtest-id <UUID>     (must reference an existing row whose
                              strategy_id matches the extraction)
    --overfitting-id <UUID>  (must reference an existing row whose
                              backtest_id matches the chosen
                              backtest; defaults to the unique
                              overfitting row for that backtest)

Both IDs are validated before any write; --dry-run prints which
backtest_id / overfitting_id the run resolved to so the operator
can confirm before the real seed.

Backtest-metrics translation (the load-bearing contract):

The trader's drift analyzer + the admin `approve_paper` validator
both require the version's `backtest_metrics` JSONB to have
exactly this shape:

    walk_forward.out_of_sample_trade_freq_per_week  (number)
    single_pass.win_rate                             (number)
    single_pass.avg_return_per_trade                 (number)
    single_pass.max_drawdown_pct                     (number)

We derive each from MarketMind's own result shapes:

  - `out_of_sample_trade_freq_per_week`: sum of
    `WindowResult.out_of_sample_num_trades` across all walk-forward
    windows, divided by total OOS days, times 7.

  - `win_rate`: `BacktestMetrics.win_rate` from the latest
    backtest_results row's `metrics` subtree.

  - `avg_return_per_trade`: `BacktestMetrics.expectancy` — the
    expected value of a single trade's return. Standard textbook
    expectancy IS average-return-per-trade.

  - `max_drawdown_pct`: `BacktestMetrics.max_drawdown_pct`.

The single-pass values are whole-period (in-sample) figures — the
v1 trader uses them as a proxy for "approving" performance. The
truly-OOS metric is the walk-forward trade frequency, which is
what the drift analyzer compares the live bot against. See
`workers/trader/drift.py`'s STEP 14 SEED-SCRIPT TRANSLATION block
for the matching contract on the read side.

Critical safety:

  - `approved_for_paper` is ALWAYS inserted as FALSE. The seed
    script CANNOT enable paper trading; the operator must
    explicitly POST to /trader/strategies/{id}/approve_paper after
    reviewing the version's overfitting summary.
  - `approved_for_live` is `FALSE` (and pinned there by the
    immutability trigger on `trader_strategy_versions`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from marketmind_shared.schemas.strategy_spec import StrategySpec, spec_uses_stateful_v2
from marketmind_workers.trader.templates.spec_template import spec_template_rejection_reason
from psycopg.types.json import Jsonb
from pydantic import ValidationError

# ---- Constants -------------------------------------------------------------


# The five v1 templates. Matches the CHECK constraint on
# `trader_strategy_versions.template` (migration 0006).
_VALID_TEMPLATES: frozenset[str] = frozenset(
    {"ma_trend", "breakout", "rsi_mean_reversion", "bb_mean_reversion", "vcb"},
)


# ---- Errors ----------------------------------------------------------------


class SeedError(Exception):
    """Raised on any pre-write validation failure. Caught by main()
    which prints the message and exits 1.
    """


# ---- Pure-function payload builder ----------------------------------------


def build_backtest_metrics_payload(
    backtest_result_json: dict[str, Any],
    walk_forward_json: dict[str, Any],
) -> dict[str, Any]:
    """Translate MarketMind's BacktestResult + WalkForwardResult
    into the trader's two-subtree `backtest_metrics` contract.

    Inputs are the raw JSONB blobs as read from Postgres (already
    parsed to dicts by psycopg). No Pydantic validation here —
    that would tie this script to specific MarketMind schema
    versions; we just read the fields we need.

    Raises SeedError on missing required fields. Returns a plain
    dict ready to wrap in `Jsonb(...)`.
    """
    metrics = backtest_result_json.get("metrics")
    # `dict` check is load-bearing for tests that pass synthetic
    # blobs missing the `metrics` subtree — pyright is happy with
    # `dict[str, Any]` here because the input dict may have arbitrary
    # values at "metrics", not necessarily dict.
    if not isinstance(metrics, dict):  # type: ignore[unreachable]
        raise SeedError(
            "backtest_results.result_json is missing the `metrics` subtree "
            "(expected BacktestMetrics fields: win_rate, expectancy, "
            "max_drawdown_pct).",
        )

    try:
        win_rate = float(metrics["win_rate"])
        avg_return_per_trade = float(metrics["expectancy"])
        max_drawdown_pct = float(metrics["max_drawdown_pct"])
    except KeyError as exc:
        raise SeedError(
            f"backtest_results.result_json.metrics missing required field {exc}",
        ) from exc

    trade_freq = _compute_oos_trade_freq_per_week(walk_forward_json)

    return {
        "walk_forward": {
            "out_of_sample_trade_freq_per_week": trade_freq,
        },
        "single_pass": {
            "win_rate": win_rate,
            "avg_return_per_trade": avg_return_per_trade,
            "max_drawdown_pct": max_drawdown_pct,
        },
    }


def _compute_oos_trade_freq_per_week(walk_forward_json: dict[str, Any]) -> float:
    """Total OOS trades / total OOS days * 7.

    Sums across every window in `walk_forward_json["windows"]`. If
    the walk-forward couldn't run (n_windows_actual=0 or no
    `windows` array), raises SeedError — the trader's drift
    analyzer needs this value, so a strategy without OOS data
    can't be seeded.
    """
    windows = walk_forward_json.get("windows")
    if not isinstance(windows, list) or not windows:  # type: ignore[unreachable]
        raise SeedError(
            "overfitting_analyses.walk_forward_json has no `windows` array. "
            "Walk-forward analysis must have produced at least one OOS window "
            "before the strategy can be paper-traded.",
        )

    total_trades = 0
    total_oos_days = 0.0
    for window in windows:
        if not isinstance(window, dict):
            continue
        total_trades += int(window.get("out_of_sample_num_trades", 0))
        oos_start = window.get("out_of_sample_start")
        oos_end = window.get("out_of_sample_end")
        if oos_start is None or oos_end is None:
            continue
        start_dt = datetime.fromisoformat(str(oos_start).replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(str(oos_end).replace("Z", "+00:00"))
        delta_days = (end_dt - start_dt).total_seconds() / 86_400.0
        if delta_days > 0:
            total_oos_days += delta_days

    if total_oos_days <= 0:
        raise SeedError(
            "Walk-forward windows have zero total OOS duration. Cannot compute trade frequency.",
        )
    return (total_trades / total_oos_days) * 7.0


def build_overfitting_metrics_payload(
    walk_forward_json: dict[str, Any],
    parameter_sweep_json: dict[str, Any],
    monte_carlo_json: dict[str, Any],
    deflated_sharpe_json: dict[str, Any],
    composite_score_json: dict[str, Any],
) -> dict[str, Any]:
    """Compact summary of the overfitting analyses for the trader's
    `overfitting_metrics` JSONB.

    The trader doesn't currently READ this column — it's stored for
    operator inspection via /trader/strategies. We extract a few
    headline numbers from each analysis rather than dumping the
    full structures (which can be hundreds of KB for parameter
    sweeps + monte-carlo histograms).

    Source JSONBs are all NOT NULL per migration 0005's schema, so
    no defensive None checks are necessary here.
    """
    return {
        "walk_forward": {
            "degradation_ratio": walk_forward_json.get("degradation_ratio"),
            "out_of_sample_positive_rate": walk_forward_json.get(
                "out_of_sample_positive_rate",
            ),
            "consistency_score": walk_forward_json.get("consistency_score"),
            "n_windows": walk_forward_json.get("n_windows_actual"),
        },
        "parameter_sweep": {
            "peakiness_score": parameter_sweep_json.get("peakiness_score"),
            "baseline_rank_percentile": parameter_sweep_json.get(
                "baseline_rank_percentile",
            ),
        },
        "monte_carlo": {
            "p_value": monte_carlo_json.get("p_value"),
            "percentile_rank": monte_carlo_json.get("percentile_rank"),
        },
        "deflated_sharpe": {
            "deflated_sharpe": deflated_sharpe_json.get("deflated_sharpe"),
            "probabilistic_sharpe": deflated_sharpe_json.get("probabilistic_sharpe"),
        },
        "composite_score": {
            "score": composite_score_json.get("score"),
            "verdict": composite_score_json.get("verdict"),
        },
    }


# ---- DB fetchers ----------------------------------------------------------


@dataclass(frozen=True)
class _ExtractionRow:
    id: UUID
    spec_json: dict[str, Any]


@dataclass(frozen=True)
class _BacktestRow:
    id: UUID
    result_json: dict[str, Any]


@dataclass(frozen=True)
class _OverfittingRow:
    id: UUID
    walk_forward_json: dict[str, Any]
    parameter_sweep_json: dict[str, Any]
    monte_carlo_json: dict[str, Any]
    deflated_sharpe_json: dict[str, Any]
    composite_score_json: dict[str, Any]


def _fetch_extraction(
    conn: psycopg.Connection[Any],
    extraction_id: UUID,
) -> _ExtractionRow:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, spec_json FROM extracted_strategies WHERE id = %s",
            (str(extraction_id),),
        )
        row = cur.fetchone()
    if row is None:
        raise SeedError(f"extracted_strategies row not found: {extraction_id}")
    if row[1] is None:
        raise SeedError(
            f"extracted_strategies {extraction_id} has spec_json=NULL "
            "(this happens on refusal extractions). Cannot seed.",
        )
    return _ExtractionRow(id=UUID(str(row[0])), spec_json=dict(row[1]))


def _fetch_latest_backtest(
    conn: psycopg.Connection[Any],
    extraction_id: UUID,
) -> _BacktestRow:
    """The most recent backtest for the given extraction. The FK is
    `backtest_results.strategy_id` → `extracted_strategies.id`.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, result_json
            FROM backtest_results
            WHERE strategy_id = %s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (str(extraction_id),),
        )
        row = cur.fetchone()
    if row is None:
        raise SeedError(
            f"No backtest_results found for extraction {extraction_id}. "
            "Run Phase 3 backtest before seeding.",
        )
    return _BacktestRow(id=UUID(str(row[0])), result_json=dict(row[1]))


def _fetch_overfitting(
    conn: psycopg.Connection[Any],
    backtest_id: UUID,
) -> _OverfittingRow:
    """The overfitting row for the backtest. A UNIQUE index on
    `backtest_id` guarantees at most one row.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, walk_forward_json, parameter_sweep_json,
                   monte_carlo_json, deflated_sharpe_json, composite_score_json
            FROM overfitting_analyses
            WHERE backtest_id = %s
            LIMIT 1
            """,
            (str(backtest_id),),
        )
        row = cur.fetchone()
    if row is None:
        raise SeedError(
            f"No overfitting_analyses found for backtest {backtest_id}. "
            "Run Phase 4 walk-forward + monte-carlo + parameter-sweep "
            "before seeding (the admin approve_paper endpoint will "
            "reject the version without walk-forward data).",
        )
    return _OverfittingRow(
        id=UUID(str(row[0])),
        walk_forward_json=dict(row[1]),
        parameter_sweep_json=dict(row[2]),
        monte_carlo_json=dict(row[3]),
        deflated_sharpe_json=dict(row[4]),
        composite_score_json=dict(row[5]),
    )


def _fetch_backtest_by_id(
    conn: psycopg.Connection[Any],
    backtest_id: UUID,
    extraction_id: UUID,
) -> _BacktestRow:
    """Fetch the named backtest, asserting it belongs to the
    extraction. Raises SeedError on miss or mismatch.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, result_json, strategy_id
            FROM backtest_results
            WHERE id = %s
            """,
            (str(backtest_id),),
        )
        row = cur.fetchone()
    if row is None:
        raise SeedError(f"backtest_results row not found: {backtest_id}")
    actual_strategy_id = UUID(str(row[2]))
    if actual_strategy_id != extraction_id:
        raise SeedError(
            f"backtest {backtest_id} belongs to strategy_id={actual_strategy_id}, "
            f"not the requested extraction {extraction_id}",
        )
    return _BacktestRow(id=UUID(str(row[0])), result_json=dict(row[1]))


def _fetch_overfitting_by_id(
    conn: psycopg.Connection[Any],
    overfitting_id: UUID,
    backtest_id: UUID,
) -> _OverfittingRow:
    """Fetch the named overfitting analysis, asserting its
    backtest_id matches the chosen backtest. Raises SeedError on
    miss or mismatch.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, walk_forward_json, parameter_sweep_json,
                   monte_carlo_json, deflated_sharpe_json,
                   composite_score_json, backtest_id
            FROM overfitting_analyses
            WHERE id = %s
            """,
            (str(overfitting_id),),
        )
        row = cur.fetchone()
    if row is None:
        raise SeedError(f"overfitting_analyses row not found: {overfitting_id}")
    actual_backtest_id = UUID(str(row[6]))
    if actual_backtest_id != backtest_id:
        raise SeedError(
            f"overfitting {overfitting_id} belongs to backtest_id={actual_backtest_id}, "
            f"not the chosen backtest {backtest_id}",
        )
    return _OverfittingRow(
        id=UUID(str(row[0])),
        walk_forward_json=dict(row[1]),
        parameter_sweep_json=dict(row[2]),
        monte_carlo_json=dict(row[3]),
        deflated_sharpe_json=dict(row[4]),
        composite_score_json=dict(row[5]),
    )


# ---- DB writers -----------------------------------------------------------


def _ensure_strategy(
    conn: psycopg.Connection[Any],
    *,
    name: str,
) -> UUID:
    """Insert a `trader_strategies` row if `name` is unfamiliar;
    return its id either way. Idempotent on `name`.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM trader_strategies WHERE name = %s", (name,))
        row = cur.fetchone()
        if row is not None:
            return UUID(str(row[0]))
        cur.execute(
            "INSERT INTO trader_strategies (name) VALUES (%s) RETURNING id",
            (name,),
        )
        ins = cur.fetchone()
    assert ins is not None
    return UUID(str(ins[0]))


def _next_version(
    conn: psycopg.Connection[Any],
    strategy_id: UUID,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM trader_strategy_versions "
            "WHERE strategy_id = %s",
            (str(strategy_id),),
        )
        row = cur.fetchone()
    assert row is not None
    return int(row[0])


def _insert_version(
    conn: psycopg.Connection[Any],
    *,
    strategy_id: UUID,
    version: int,
    marketmind_spec_id: UUID,
    template: str,
    parameters: dict[str, Any],
    symbols: list[str],
    timeframes: list[str],
    risk_pct: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    backtest_metrics: dict[str, Any],
    overfitting_metrics: dict[str, Any],
    notes: str,
) -> UUID:
    """INSERT the version row.

    `approved_for_paper=FALSE` and `enabled=TRUE` are hard-coded
    here — the seed flow is "land it disabled-from-trading-but-
    enabled-for-evaluation, operator promotes via admin API". The
    immutability trigger pins `approved_for_live=FALSE` regardless
    of what we write.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trader_strategy_versions (
                strategy_id, version, marketmind_spec_id, template, parameters,
                symbols, timeframes, risk_pct, fee_bps, slippage_bps,
                backtest_metrics, overfitting_metrics, approved_for_paper,
                approved_for_live, enabled, notes
            ) VALUES (
                %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s,
                %s, %s, FALSE,
                FALSE, TRUE, %s
            )
            RETURNING id
            """,
            (
                str(strategy_id),
                version,
                str(marketmind_spec_id),
                template,
                Jsonb(parameters),
                symbols,
                timeframes,
                risk_pct,
                fee_bps,
                slippage_bps,
                Jsonb(backtest_metrics),
                Jsonb(overfitting_metrics),
                notes,
            ),
        )
        row = cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


# ---- template routing -----------------------------------------------------


def _resolve_template_routing(
    spec_json: dict[str, Any],
    cli_template: str | None,
    cli_parameters: dict[str, Any] | None,
) -> tuple[str, dict[str, Any], str]:
    """Decide ``(template, parameters, routing_description)`` for the version.

    With ``--template`` omitted, any SpecTemplate-compatible spec auto-routes
    to the generic `spec` template (A.5a's SpecTemplate) — stateful or not.
    A non-stateful spec that uses an indicator no hand-coded template models
    (e.g. Supertrend) is seedable this way. To map a non-stateful spec onto a
    hand-coded v1 template instead, the operator supplies ``--template`` +
    ``--parameters-json`` together; a stateful spec rejects them.
    """
    try:
        spec = StrategySpec.model_validate(spec_json)
    except ValidationError as exc:
        raise SeedError(
            f"the extraction's spec_json failed StrategySpec validation: {exc}",
        ) from None

    stateful = spec_uses_stateful_v2(spec)

    # Explicit --template: map onto a hand-coded v1 template. A stateful
    # spec cannot be forced onto one — it must use the SpecTemplate.
    if cli_template is not None or cli_parameters is not None:
        if stateful:
            raise SeedError(
                "this is a v2 stateful spec — it auto-routes to the generic "
                "'spec' template; do not pass --template / --parameters-json.",
            )
        if cli_template is None or cli_parameters is None:
            raise SeedError(
                "--template and --parameters-json must be supplied together.",
            )
        return (
            cli_template,
            cli_parameters,
            f"v1 spec -> operator-selected template='{cli_template}'",
        )

    # --template omitted: auto-route to the generic SpecTemplate, for any
    # spec it can run. This is how a non-stateful new-indicator spec
    # (e.g. Supertrend) gets seeded — not just stateful ones.
    reason = spec_template_rejection_reason(spec)
    if reason is not None:
        if stateful:
            raise SeedError(f"this v2 spec cannot run as a SpecTemplate — {reason}")
        raise SeedError(
            f"this spec cannot run as a SpecTemplate ({reason}); if it maps "
            "onto a hand-coded v1 template, pass --template / --parameters-json.",
        )
    kind = "v2 stateful" if stateful else "v1-style"
    return (
        "spec",
        {"spec": spec_json},
        f"{kind} spec -> template='spec' (the generic SpecTemplate)",
    )


# ---- argparse + main ------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=("Seed a trader v1 strategy version from a MarketMind extraction."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "After seeding, approve the version via "
            "`POST /trader/strategies/{id}/approve_paper`. The seed "
            "script never auto-approves."
        ),
    )
    parser.add_argument("--extraction-id", required=True, type=UUID)
    parser.add_argument(
        "--backtest-id",
        type=UUID,
        default=None,
        help=(
            "Explicit backtest_results.id. The row's strategy_id must match "
            "--extraction-id. Default: latest backtest for the extraction "
            "(ORDER BY created_at DESC)."
        ),
    )
    parser.add_argument(
        "--overfitting-id",
        type=UUID,
        default=None,
        help=(
            "Explicit overfitting_analyses.id. The row's backtest_id must "
            "match the chosen backtest. Default: the unique overfitting row "
            "linked to the chosen backtest."
        ),
    )
    parser.add_argument("--name", required=True)
    parser.add_argument(
        "--template",
        choices=sorted(_VALID_TEMPLATES),
        default=None,
        help=(
            "Hand-coded v1 template to map the spec onto. Required for a "
            "v1-style spec; omit for a v2 stateful spec (auto-routed to 'spec')."
        ),
    )
    parser.add_argument(
        "--parameters-json",
        default=None,
        help=(
            "JSON object of the v1 template's parameters dict. Required for a "
            "v1-style spec; omit for a v2 stateful spec."
        ),
    )
    parser.add_argument(
        "--symbols",
        required=True,
        help="Comma-separated trading symbols, e.g. BTC/USDT,ETH/USDT",
    )
    parser.add_argument(
        "--timeframes",
        required=True,
        help="Comma-separated timeframes, e.g. 4h or 1h,4h",
    )
    parser.add_argument("--risk-pct", required=True, type=Decimal)
    parser.add_argument("--fee-bps", type=Decimal, default=None)
    parser.add_argument("--slippage-bps", type=Decimal, default=None)
    parser.add_argument("--notes", default="")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the row that would be inserted; write nothing.",
    )
    return parser


def _parse_csv(raw: str) -> list[str]:
    return [s.strip() for s in raw.split(",") if s.strip()]


def _resolve_defaults(
    fee_bps: Decimal | None,
    slippage_bps: Decimal | None,
) -> tuple[Decimal, Decimal]:
    """Pull defaults from TraderSettings only if the user didn't
    provide them. We import lazily so the script doesn't need a
    live `TRADER_*` env unless it actually has to read defaults.
    """
    if fee_bps is not None and slippage_bps is not None:
        return fee_bps, slippage_bps
    from marketmind_workers.trader.config import get_trader_settings

    settings = get_trader_settings()
    return (
        fee_bps if fee_bps is not None else settings.trader_default_fee_bps,
        slippage_bps if slippage_bps is not None else settings.trader_default_slippage_bps,
    )


def _print_dry_run(
    *,
    name: str,
    template: str,
    parameters: dict[str, Any],
    routing: str,
    symbols: list[str],
    timeframes: list[str],
    risk_pct: Decimal,
    fee_bps: Decimal,
    slippage_bps: Decimal,
    backtest_metrics: dict[str, Any],
    overfitting_metrics: dict[str, Any],
    marketmind_spec_id: UUID,
    backtest_id: UUID,
    overfitting_id: UUID,
    notes: str,
) -> None:
    payload = {
        "strategy_name": name,
        "template": template,
        "parameters": parameters,
        "symbols": symbols,
        "timeframes": timeframes,
        "risk_pct": str(risk_pct),
        "fee_bps": str(fee_bps),
        "slippage_bps": str(slippage_bps),
        "marketmind_spec_id": str(marketmind_spec_id),
        "resolved_backtest_id": str(backtest_id),
        "resolved_overfitting_id": str(overfitting_id),
        "backtest_metrics": backtest_metrics,
        "overfitting_metrics": overfitting_metrics,
        "approved_for_paper": False,
        "approved_for_live": False,
        "enabled": True,
        "notes": notes,
    }
    print("DRY-RUN — would insert:")
    print(f"  routing                 = {routing}")
    print(f"  resolved backtest_id    = {backtest_id}")
    print(f"  resolved overfitting_id = {overfitting_id}")
    print(json.dumps(payload, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # --parameters-json is parsed only when supplied; a v2 stateful spec
    # auto-routes and needs neither --template nor --parameters-json.
    cli_parameters: dict[str, Any] | None = None
    if args.parameters_json is not None:
        try:
            loaded = json.loads(args.parameters_json)
        except json.JSONDecodeError as exc:
            print(f"ERROR: --parameters-json is not valid JSON: {exc}", file=sys.stderr)
            return 1
        if not isinstance(loaded, dict):
            print("ERROR: --parameters-json must be a JSON object (dict).", file=sys.stderr)
            return 1
        cli_parameters = loaded

    symbols = _parse_csv(args.symbols)
    timeframes = _parse_csv(args.timeframes)
    if not symbols:
        print("ERROR: --symbols is empty after parsing.", file=sys.stderr)
        return 1
    if not timeframes:
        print("ERROR: --timeframes is empty after parsing.", file=sys.stderr)
        return 1

    fee_bps, slippage_bps = _resolve_defaults(args.fee_bps, args.slippage_bps)

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        print(
            "ERROR: DATABASE_URL is not set. Point it at the trader Postgres before running.",
            file=sys.stderr,
        )
        return 1

    try:
        with psycopg.connect(database_url) as conn:
            extraction = _fetch_extraction(conn, args.extraction_id)
            template, parameters, routing = _resolve_template_routing(
                extraction.spec_json,
                args.template,
                cli_parameters,
            )
            if args.backtest_id is not None:
                backtest = _fetch_backtest_by_id(
                    conn,
                    args.backtest_id,
                    extraction.id,
                )
            else:
                backtest = _fetch_latest_backtest(conn, extraction.id)
            if args.overfitting_id is not None:
                overfitting = _fetch_overfitting_by_id(
                    conn,
                    args.overfitting_id,
                    backtest.id,
                )
            else:
                overfitting = _fetch_overfitting(conn, backtest.id)
            backtest_metrics = build_backtest_metrics_payload(
                backtest.result_json,
                overfitting.walk_forward_json,
            )
            overfitting_metrics = build_overfitting_metrics_payload(
                overfitting.walk_forward_json,
                overfitting.parameter_sweep_json,
                overfitting.monte_carlo_json,
                overfitting.deflated_sharpe_json,
                overfitting.composite_score_json,
            )

            if args.dry_run:
                _print_dry_run(
                    name=args.name,
                    template=template,
                    parameters=parameters,
                    routing=routing,
                    symbols=symbols,
                    timeframes=timeframes,
                    risk_pct=args.risk_pct,
                    fee_bps=fee_bps,
                    slippage_bps=slippage_bps,
                    backtest_metrics=backtest_metrics,
                    overfitting_metrics=overfitting_metrics,
                    marketmind_spec_id=extraction.id,
                    backtest_id=backtest.id,
                    overfitting_id=overfitting.id,
                    notes=args.notes,
                )
                return 0

            with conn.transaction():
                strategy_id = _ensure_strategy(conn, name=args.name)
                version = _next_version(conn, strategy_id)
                version_id = _insert_version(
                    conn,
                    strategy_id=strategy_id,
                    version=version,
                    marketmind_spec_id=extraction.id,
                    template=template,
                    parameters=parameters,
                    symbols=symbols,
                    timeframes=timeframes,
                    risk_pct=args.risk_pct,
                    fee_bps=fee_bps,
                    slippage_bps=slippage_bps,
                    backtest_metrics=backtest_metrics,
                    overfitting_metrics=overfitting_metrics,
                    notes=args.notes,
                )
    except SeedError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print("Seeded trader strategy version:")
    print(f"  strategy_id        = {strategy_id}")
    print(f"  strategy_version_id = {version_id}")
    print(f"  version             = {version}")
    print("  approved_for_paper  = FALSE  (operator must approve)")
    print()
    print("Next steps:")
    print(
        "  1. Review the version's backtest_metrics + overfitting_metrics via "
        "GET /trader/strategies",
    )
    print(
        f"  2. Approve for paper trading: POST /trader/strategies/{version_id}/approve_paper",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
