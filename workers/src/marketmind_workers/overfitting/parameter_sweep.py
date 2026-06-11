"""Parameter robustness sweep.

For each spec, identify parameters with a clear numeric "neighborhood"
(stop-loss percent, take-profit percent, indicator periods, RSI
thresholds). For each parameter, define a set of nearby values. Build
the cartesian product of those values (the grid), capped at 50 cells.
Run a backtest for each cell. Look at how the baseline's return
compares to its immediate grid-neighbors.

If the baseline is a sharp lone peak — much higher return than the
cells one step away on every axis — that's a textbook overfitting
signature. If the baseline sits on a plateau (returns roughly similar
to neighbors), the strategy is parameter-robust.

v1 swept parameters:
  - `stop_loss_percent` on a percent stop-loss exit
  - `take_profit_percent` on a percent take-profit exit
  - `indicator_period` on any sma / ema / rsi indicator expression
  - `rsi_lower_threshold` / `rsi_upper_threshold` on comparison nodes
    where the right-hand side is a literal vs an RSI indicator

Not yet swept (will land in Phase 5+):
  - ATR multiplier on volatility-scaled stops
  - Bollinger band std-dev multiplier
  - MACD fast/slow/signal periods (interdependent — harder to sweep)
  - Risk-based sizing percent

Impact ordering (descending) — used when pruning axes to stay under
the 50-cell cap:
  1. stop_loss_percent
  2. take_profit_percent
  3. indicator_period (the largest period in the spec changes character
     of the strategy more than the smallest)
  4. rsi thresholds
"""

from __future__ import annotations

import copy
import json
import math
from collections.abc import Iterator
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Any, Final

import structlog
from marketmind_shared.schemas import (
    ParameterSweepResult,
    StrategySpec,
    SweepAxis,
    SweepAxisKind,
    SweepCell,
)

from marketmind_workers.backtest.engine import run_backtest
from marketmind_workers.backtest.metrics import compute_metrics

log = structlog.get_logger(__name__)


# Hard cap on cells. 50 keeps the worst-case sweep at ~50 backtests,
# which fits inside the 2.5 min budget for the full overfitting
# pipeline on cached data.
_MAX_CELLS: Final[int] = 50


# ---- Neighborhood generation ---------------------------------------------


def _neighborhood_stop_loss(current: float) -> list[float]:
    """Stop-loss percent neighbors. Centered on `current`, spans
    typical retail range [0.02, 0.20]. 5 values keeps grids manageable.
    """
    # Keep one of the points exactly at `current` so the baseline
    # cell lives in the grid.
    candidates = [
        max(0.02, current * 0.5),
        max(0.02, current * 0.75),
        current,
        min(0.20, current * 1.25),
        min(0.20, current * 1.75),
    ]
    return _unique_sorted(candidates)


def _neighborhood_take_profit(current: float) -> list[float]:
    candidates = [
        max(0.02, current * 0.5),
        max(0.02, current * 0.75),
        current,
        min(0.50, current * 1.25),
        min(0.50, current * 2.0),
    ]
    return _unique_sorted(candidates)


def _neighborhood_indicator_period(current: float) -> list[float]:
    """Indicator period neighbors. Multiplicative so the spacing
    scales with the magnitude (50 → 70 vs 200 → 280).
    """
    c = float(current)
    candidates = [
        max(5.0, round(c * 0.5)),
        max(5.0, round(c * 0.75)),
        c,
        round(c * 1.25),
        round(c * 1.75),
    ]
    return _unique_sorted([float(v) for v in candidates])


def _neighborhood_percentile_window(current: float) -> list[float]:
    """PercentileExpr.window neighbors. Multiplicative spacing scales
    with magnitude (20 → 30 vs 168 → 252). Lower bound matches the
    schema floor of 10.
    """
    c = float(current)
    candidates = [
        max(10.0, round(c * 0.5)),
        max(10.0, round(c * 0.75)),
        c,
        round(c * 1.25),
        round(c * 1.75),
    ]
    return _unique_sorted([float(v) for v in candidates])


def _neighborhood_rsi_threshold(current: float, *, lower: bool) -> list[float]:
    if lower:
        candidates = [
            max(10.0, current - 10.0),
            max(10.0, current - 5.0),
            current,
            min(45.0, current + 5.0),
            min(45.0, current + 10.0),
        ]
    else:
        candidates = [
            max(55.0, current - 10.0),
            max(55.0, current - 5.0),
            current,
            min(90.0, current + 5.0),
            min(90.0, current + 10.0),
        ]
    return _unique_sorted(candidates)


def _unique_sorted(values: list[float]) -> list[float]:
    """De-duplicate to 4 decimal places and return ascending."""
    rounded = {round(v, 4): v for v in values}
    return sorted(rounded.values())


# ---- Axis detection -------------------------------------------------------


# Impact ranking — lower = more impactful. Used by the prune step.
_AXIS_IMPACT_RANK: Final[dict[SweepAxisKind, int]] = {
    SweepAxisKind.STOP_LOSS_PCT: 0,
    SweepAxisKind.TAKE_PROFIT_PCT: 1,
    SweepAxisKind.INDICATOR_PERIOD: 2,
    # PERCENTILE_WINDOW: same magnitude-of-effect tier as INDICATOR_PERIOD —
    # both pick the lookback for a rolling computation. Slot just after
    # indicator_period so a spec with both prefers period sweeps first.
    SweepAxisKind.PERCENTILE_WINDOW: 2,
    SweepAxisKind.RSI_UPPER_THRESHOLD: 3,
    SweepAxisKind.RSI_LOWER_THRESHOLD: 4,
}


def _detect_axes(spec_dict: dict[str, Any]) -> list[SweepAxis]:
    """Walk the spec dict and produce one SweepAxis per swept-eligible
    parameter group.

    Grouping: paths sharing the same `(kind, current_value)` collapse
    into a single axis. The Golden Cross spec has SMA(50) appearing
    twice (entry + exit conditions) and SMA(200) appearing twice — each
    pair becomes one axis whose `target_paths` lists both positions.
    """
    found: dict[tuple[SweepAxisKind, float], list[str]] = {}

    def visit(node: Any, path: list[str | int]) -> Iterator[tuple[SweepAxisKind, float, str]]:
        if isinstance(node, dict):
            # stop_loss / take_profit exits live under exit.exits[*].method
            type_val = node.get("type")
            if type_val == "stop_loss":
                method = node.get("method", {})
                if isinstance(method, dict) and method.get("kind") == "percent":
                    val = float(method["value"])
                    yield (
                        SweepAxisKind.STOP_LOSS_PCT,
                        val,
                        _to_path([*path, "method", "value"]),
                    )
            elif type_val == "take_profit":
                method = node.get("method", {})
                if isinstance(method, dict) and method.get("kind") == "percent":
                    val = float(method["value"])
                    yield (
                        SweepAxisKind.TAKE_PROFIT_PCT,
                        val,
                        _to_path([*path, "method", "value"]),
                    )
            # Indicator expression: detect period inside .params
            if node.get("kind") == "indicator" and isinstance(node.get("params"), dict):
                params = node["params"]
                period = params.get("period")
                name = node.get("name")
                if isinstance(period, int | float) and name in {"sma", "ema", "rsi", "wma", "adx", "keltner"}:
                    yield (
                        SweepAxisKind.INDICATOR_PERIOD,
                        float(period),
                        _to_path([*path, "params", "period"]),
                    )
            # PercentileExpr: detect window field (v1.2.A). Distinct from
            # INDICATOR_PERIOD — percentile is a wrapper expression, not
            # an indicator. The neighborhood function is similar (discrete
            # int, multiplicative spacing) but the schema floor differs
            # (10 vs 2).
            if node.get("kind") == "percentile" and isinstance(node.get("window"), int | float):
                yield (
                    SweepAxisKind.PERCENTILE_WINDOW,
                    float(node["window"]),
                    _to_path([*path, "window"]),
                )
            # RSI thresholds: comparison nodes where one side is an
            # rsi indicator and the other is a literal scalar.
            if node.get("type") == "compare":
                left = node.get("left", {})
                right = node.get("right", {})
                if (
                    isinstance(left, dict)
                    and isinstance(right, dict)
                    and left.get("kind") == "indicator"
                    and left.get("name") == "rsi"
                    and right.get("kind") == "scalar"
                ):
                    op = node.get("op", "")
                    val = float(right["value"])
                    kind = (
                        SweepAxisKind.RSI_LOWER_THRESHOLD
                        if op in {"<", "<=", "below"}
                        else SweepAxisKind.RSI_UPPER_THRESHOLD
                    )
                    yield (kind, val, _to_path([*path, "right", "value"]))

            for k, v in node.items():
                yield from visit(v, [*path, k])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from visit(v, [*path, i])

    for kind, val, target_path in visit(spec_dict, []):
        key = (kind, round(val, 6))
        found.setdefault(key, []).append(target_path)

    axes: list[SweepAxis] = []
    for (kind, val), paths in found.items():
        axes.append(_build_axis(kind, val, paths))
    # Stable, impact-ordered ordering so prune is deterministic.
    axes.sort(key=lambda a: (_AXIS_IMPACT_RANK[a.kind], a.baseline_value))
    return axes


def _build_axis(kind: SweepAxisKind, baseline: float, paths: list[str]) -> SweepAxis:
    if kind is SweepAxisKind.STOP_LOSS_PCT:
        values = _neighborhood_stop_loss(baseline)
        label = "Stop-loss %"
    elif kind is SweepAxisKind.TAKE_PROFIT_PCT:
        values = _neighborhood_take_profit(baseline)
        label = "Take-profit %"
    elif kind is SweepAxisKind.INDICATOR_PERIOD:
        values = _neighborhood_indicator_period(baseline)
        label = f"Indicator period ({int(baseline)})"
    elif kind is SweepAxisKind.PERCENTILE_WINDOW:
        values = _neighborhood_percentile_window(baseline)
        label = f"Percentile window ({int(baseline)})"
    elif kind is SweepAxisKind.RSI_LOWER_THRESHOLD:
        values = _neighborhood_rsi_threshold(baseline, lower=True)
        label = f"RSI lower threshold ({baseline:.0f})"
    else:
        values = _neighborhood_rsi_threshold(baseline, lower=False)
        label = f"RSI upper threshold ({baseline:.0f})"
    return SweepAxis(
        kind=kind,
        label=label,
        values=values,
        baseline_value=baseline,
        target_paths=paths,
    )


def _to_path(parts: list[str | int]) -> str:
    """Build a JSON-pointer-ish path. We don't bother escaping `/` or
    `~` because our schema field names never contain them.
    """
    return "/".join(str(p) for p in parts)


# ---- Pruning to stay under the cell cap ----------------------------------


def _prune_to_budget(axes: list[SweepAxis], max_cells: int) -> tuple[list[SweepAxis], str | None]:
    """Drop the lowest-impact axes until the cartesian product fits.

    Returns (pruned_axes, skipped_reason_or_None). When we drop axes
    we record a human-readable note so the UI can explain why some
    parameters weren't swept.
    """
    pruned = list(axes)
    # Sort by impact (ascending); ascending = most impactful first.
    pruned.sort(key=lambda a: _AXIS_IMPACT_RANK[a.kind])
    while pruned and _grid_size(pruned) > max_cells:
        dropped = pruned.pop()
        log.info("parameter_sweep_axis_dropped", kind=dropped.kind, n_values=len(dropped.values))
    if not pruned:
        return [], "no swept-eligible parameters found in spec"
    if len(pruned) < len(axes):
        missing = ", ".join(a.label for a in axes if a not in pruned)
        return pruned, (f"grid would exceed {max_cells} cells; dropped: {missing}")
    return pruned, None


def _grid_size(axes: list[SweepAxis]) -> int:
    n = 1
    for a in axes:
        n *= len(a.values)
    return n


# ---- Spec mutation --------------------------------------------------------


def _mutate_spec(spec_dict: dict[str, Any], axis: SweepAxis, value: float) -> None:
    """In-place assignment of `value` at every `target_path` for the axis.

    The `axis.values` come from the neighborhood functions which round
    indicator periods to integers; we cast to int when the path points
    at a `period` field so Pydantic doesn't reject 50.0 for an int slot.
    """
    # PercentileExpr.window is also a strict int; cast for the same
    # reason as INDICATOR_PERIOD (Pydantic rejects 20.0 for an int slot).
    cast_to_int = axis.kind in {
        SweepAxisKind.INDICATOR_PERIOD,
        SweepAxisKind.PERCENTILE_WINDOW,
    }
    casted: int | float = round(value) if cast_to_int else value
    for path in axis.target_paths:
        _set_by_path(spec_dict, path, casted)


def _set_by_path(d: Any, path: str, value: Any) -> None:
    parts = path.split("/")
    cur = d
    for p in parts[:-1]:
        cur = cur[int(p)] if p.isdigit() else cur[p]
    last = parts[-1]
    if last.isdigit():
        cur[int(last)] = value
    else:
        cur[last] = value


# ---- Public entry --------------------------------------------------------


def run_parameter_sweep(
    spec: StrategySpec,
    start: datetime,
    end: datetime,
    *,
    initial_capital: float = 10_000.0,
    data_dir: str | Path = "/data",
    max_cells: int = _MAX_CELLS,
) -> ParameterSweepResult:
    """Detect swept-able parameters, build the grid, run backtests, score peakiness."""
    if max_cells < 1:
        raise ValueError(f"max_cells must be >= 1; got {max_cells}")

    baseline_dict = json.loads(spec.model_dump_json())
    axes = _detect_axes(baseline_dict)
    axes, skipped_reason = _prune_to_budget(axes, max_cells)

    if not axes:
        return _empty_result(spec, start, end, initial_capital, data_dir, skipped_reason)

    cells: list[SweepCell] = []
    for combo in product(*[a.values for a in axes]):
        cell_spec_dict = copy.deepcopy(baseline_dict)
        axis_values: dict[str, float] = {}
        for axis, value in zip(axes, combo, strict=True):
            _mutate_spec(cell_spec_dict, axis, value)
            axis_values[axis.label] = float(value)
        # Pydantic validation acts as a constraint check: e.g., if a
        # crossover spec gets fast == slow after mutation, it raises.
        try:
            cell_spec = StrategySpec.model_validate(cell_spec_dict)
        except Exception as exc:
            log.info("parameter_sweep_cell_invalid", axis_values=axis_values, error=str(exc))
            continue
        is_baseline = all(
            math.isclose(v, axis.baseline_value, rel_tol=1e-6, abs_tol=1e-9)
            for axis, v in zip(axes, combo, strict=True)
        )
        ret, sharpe, n_trades = _run_one(
            cell_spec,
            start,
            end,
            initial_capital,
            data_dir,
        )
        cells.append(
            SweepCell(
                axis_values=axis_values,
                total_return_pct=ret,
                sharpe_ratio=sharpe,
                num_trades=n_trades,
                is_baseline=is_baseline,
            ),
        )

    return _aggregate(axes, cells, spec, start, end, initial_capital, data_dir, skipped_reason)


def _run_one(
    spec: StrategySpec,
    start: datetime,
    end: datetime,
    initial_capital: float,
    data_dir: str | Path,
) -> tuple[float, float, int]:
    try:
        run = run_backtest(spec, start, end, initial_capital, data_dir=data_dir)
    except Exception as exc:
        log.warning("parameter_sweep_cell_failed", error=str(exc))
        return 0.0, 0.0, 0
    m = compute_metrics(run, spec.primary_timeframe)
    return m.total_return_pct, m.sharpe_ratio, m.num_trades


def _aggregate(
    axes: list[SweepAxis],
    cells: list[SweepCell],
    spec: StrategySpec,
    start: datetime,
    end: datetime,
    initial_capital: float,
    data_dir: str | Path,
    skipped_reason: str | None,
) -> ParameterSweepResult:
    if not cells:
        return _empty_result(spec, start, end, initial_capital, data_dir, skipped_reason)

    returns = [c.total_return_pct for c in cells]
    baseline_cell = next((c for c in cells if c.is_baseline), None)
    if baseline_cell is None:
        # Should never happen — we always include the baseline values
        # in each axis's neighborhood. Fall back to the median cell
        # so the result still validates.
        sorted_returns = sorted(returns)
        baseline_return = sorted_returns[len(sorted_returns) // 2]
    else:
        baseline_return = baseline_cell.total_return_pct

    best = max(returns)
    worst = min(returns)
    rank = sum(1 for r in returns if r <= baseline_return) / len(returns)

    neighbor_returns = (
        _immediate_neighbor_returns(axes, cells, baseline_cell) if baseline_cell else []
    )
    neighbor_avg = (
        sum(neighbor_returns) / len(neighbor_returns) if neighbor_returns else baseline_return
    )
    peakiness = _peakiness_score(baseline_return, neighbor_avg)

    return ParameterSweepResult(
        axes=axes,
        cells=cells,
        baseline_return_pct=baseline_return,
        baseline_rank_percentile=rank,
        best_in_grid_return=best,
        worst_in_grid_return=worst,
        neighborhood_avg_return=neighbor_avg,
        peakiness_score=peakiness,
        n_combinations=len(cells),
        skipped_reason=skipped_reason,
    )


def _immediate_neighbor_returns(
    axes: list[SweepAxis],
    cells: list[SweepCell],
    baseline_cell: SweepCell,
) -> list[float]:
    """Cells one step away on a single axis (others equal to baseline)."""
    by_values = {tuple(sorted(c.axis_values.items())): c for c in cells}
    baseline_key = sorted(baseline_cell.axis_values.items())
    neighbors: list[float] = []
    for axis in axes:
        idx = axis.values.index(axis.baseline_value)
        candidate_idxs = [idx - 1, idx + 1]
        for ci in candidate_idxs:
            if 0 <= ci < len(axis.values):
                neighbor_value = axis.values[ci]
                # Build the neighbor key by replacing this axis only.
                neighbor_items = [
                    (k, neighbor_value if k == axis.label else v) for k, v in baseline_key
                ]
                cell = by_values.get(tuple(sorted(neighbor_items)))
                if cell is not None:
                    neighbors.append(cell.total_return_pct)
    return neighbors


def _peakiness_score(baseline: float, neighbor_avg: float) -> float:
    """0 = baseline sits at or below neighbor average (no peak).
    1 = baseline is far above neighbor average (sharp peak).

    Uses a relative gap: (baseline - neighbors) / (|baseline| + small).
    Negative gaps clamp to 0.
    """
    denom = max(abs(baseline), 0.01)
    raw = (baseline - neighbor_avg) / denom
    return max(0.0, min(1.0, raw))


def _empty_result(
    _spec: StrategySpec,
    _start: datetime,
    _end: datetime,
    _initial_capital: float,
    _data_dir: str | Path,
    skipped_reason: str | None,
) -> ParameterSweepResult:
    return ParameterSweepResult(
        axes=[],
        cells=[],
        baseline_return_pct=0.0,
        baseline_rank_percentile=0.5,
        best_in_grid_return=0.0,
        worst_in_grid_return=0.0,
        neighborhood_avg_return=0.0,
        peakiness_score=0.0,
        n_combinations=0,
        skipped_reason=skipped_reason or "no swept-eligible parameters found in spec",
    )


__all__ = ["run_parameter_sweep"]
