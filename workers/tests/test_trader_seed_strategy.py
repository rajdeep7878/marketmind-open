"""Tests for `scripts/trader_seed_strategy.py`.

Two layers:

  - Unit: `build_backtest_metrics_payload` + the helper
    `_compute_oos_trade_freq_per_week` are pure functions over
    dicts. Test them with synthetic fixtures.

  - Integration (testcontainers Postgres): seed a fake MarketMind
    extraction + backtest + overfitting chain, call `main()`
    directly (bypassing argparse via argv list), and verify the
    new trader_strategies / trader_strategy_versions rows landed
    with `approved_for_paper=FALSE`. Plus the round-trip property:
    the seeded `backtest_metrics` JSONB must pass the same
    validator the admin `approve_paper` endpoint uses.

The script lives at `scripts/trader_seed_strategy.py` and isn't
part of the installed package; we load it via importlib so the
test can call its public functions without sys.path mutation.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb


def _load_script_module() -> ModuleType:
    """Load `scripts/trader_seed_strategy.py` as a module."""
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "trader_seed_strategy.py"
    spec = importlib.util.spec_from_file_location(
        "trader_seed_strategy",
        script_path,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["trader_seed_strategy"] = mod
    spec.loader.exec_module(mod)
    return mod


_SEED_MOD: ModuleType = _load_script_module()


# ---- Unit: build_backtest_metrics_payload ---------------------------------


def _sample_metrics() -> dict[str, Any]:
    """A minimal BacktestMetrics dict — only the three fields the
    seed script reads.
    """
    return {
        "win_rate": 0.55,
        "expectancy": 0.012,
        "max_drawdown_pct": 0.08,
    }


def _sample_walk_forward(*, total_trades: int = 12, total_days: float = 28.0) -> dict[str, Any]:
    """A WalkForwardResult JSONB with two windows summing to the
    given OOS totals. Per-window dates are synthetic but cover the
    requested span.
    """
    half_days = total_days / 2.0
    base = datetime(2026, 1, 1, tzinfo=UTC)
    half_trades = total_trades // 2
    return {
        "windows": [
            {
                "window_index": 0,
                "in_sample_start": base.isoformat(),
                "in_sample_end": (base + timedelta(days=20)).isoformat(),
                "out_of_sample_start": (base + timedelta(days=20)).isoformat(),
                "out_of_sample_end": (base + timedelta(days=20 + half_days)).isoformat(),
                "in_sample_return_pct": 0.05,
                "in_sample_sharpe": 1.2,
                "in_sample_num_trades": 5,
                "out_of_sample_return_pct": 0.02,
                "out_of_sample_sharpe": 0.8,
                "out_of_sample_num_trades": half_trades,
            },
            {
                "window_index": 1,
                "in_sample_start": (base + timedelta(days=10)).isoformat(),
                "in_sample_end": (base + timedelta(days=30)).isoformat(),
                "out_of_sample_start": (base + timedelta(days=30)).isoformat(),
                "out_of_sample_end": (base + timedelta(days=30 + half_days)).isoformat(),
                "in_sample_return_pct": 0.06,
                "in_sample_sharpe": 1.3,
                "in_sample_num_trades": 6,
                "out_of_sample_return_pct": 0.025,
                "out_of_sample_sharpe": 0.9,
                "out_of_sample_num_trades": total_trades - half_trades,
            },
        ],
        "in_sample_avg_return": 0.055,
        "out_of_sample_avg_return": 0.0225,
        "degradation_ratio": 0.4,
        "out_of_sample_positive_rate": 0.7,
        "consistency_score": 0.6,
        "train_ratio": 0.7,
        "n_windows_requested": 2,
        "n_windows_actual": 2,
    }


def test_build_payload_returns_two_subtree_shape() -> None:
    """The output dict has exactly the four required keys the
    drift module + admin validator need.
    """
    payload = _SEED_MOD.build_backtest_metrics_payload(
        {"metrics": _sample_metrics()},
        _sample_walk_forward(),
    )
    assert set(payload.keys()) == {"walk_forward", "single_pass"}
    assert set(payload["walk_forward"].keys()) == {
        "out_of_sample_trade_freq_per_week",
    }
    assert set(payload["single_pass"].keys()) == {
        "win_rate",
        "avg_return_per_trade",
        "max_drawdown_pct",
    }


def test_build_payload_single_pass_values_match_source_metrics() -> None:
    payload = _SEED_MOD.build_backtest_metrics_payload(
        {"metrics": _sample_metrics()},
        _sample_walk_forward(),
    )
    assert payload["single_pass"]["win_rate"] == 0.55
    assert payload["single_pass"]["avg_return_per_trade"] == 0.012
    assert payload["single_pass"]["max_drawdown_pct"] == 0.08


def test_build_payload_trade_freq_per_week_math() -> None:
    """12 trades across 28 OOS days → 12/28 * 7 = 3 trades/week."""
    payload = _SEED_MOD.build_backtest_metrics_payload(
        {"metrics": _sample_metrics()},
        _sample_walk_forward(total_trades=12, total_days=28.0),
    )
    assert payload["walk_forward"]["out_of_sample_trade_freq_per_week"] == pytest.approx(3.0)


def test_build_payload_raises_when_metrics_subtree_missing() -> None:
    with pytest.raises(_SEED_MOD.SeedError, match="missing the `metrics` subtree"):
        _SEED_MOD.build_backtest_metrics_payload({}, _sample_walk_forward())


def test_build_payload_raises_when_required_metric_missing() -> None:
    """Drop `expectancy` from the metrics subtree; the script must
    fail loudly rather than silently inserting a NULL.
    """
    bad = _sample_metrics()
    del bad["expectancy"]
    with pytest.raises(_SEED_MOD.SeedError, match="missing required field"):
        _SEED_MOD.build_backtest_metrics_payload(
            {"metrics": bad},
            _sample_walk_forward(),
        )


def test_build_payload_raises_when_walk_forward_has_no_windows() -> None:
    with pytest.raises(_SEED_MOD.SeedError, match="no `windows` array"):
        _SEED_MOD.build_backtest_metrics_payload(
            {"metrics": _sample_metrics()},
            {"windows": []},
        )


def test_build_payload_passes_admin_validator() -> None:
    """Round-trip: the payload must satisfy the same shape check
    the admin `approve_paper` endpoint uses. If this fails, the
    seed → approve flow is broken end-to-end.
    """
    from marketmind_api.routes.trader_admin import _missing_backtest_metric_keys

    payload = _SEED_MOD.build_backtest_metrics_payload(
        {"metrics": _sample_metrics()},
        _sample_walk_forward(),
    )
    missing = _missing_backtest_metric_keys(payload)
    assert missing == [], f"seed payload failed admin validator with missing keys: {missing}"


# ---- Unit: _resolve_template_routing (A.5a) -------------------------------

_FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "strategies" / "valid"


def _fixture_spec(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text())  # type: ignore[no-any-return]


def test_routing_v2_stateful_spec_routes_to_spec_template() -> None:
    """A v2 spec (regime_state) auto-routes to template='spec', carrying
    the spec verbatim in `parameters`.
    """
    template, parameters, routing = _SEED_MOD._resolve_template_routing(
        _fixture_spec("09_regime_state_supertrend.json"),
        None,
        None,
    )
    assert template == "spec"
    assert parameters["spec"]["schema_version"] == "2.0"
    assert "spec" in routing.lower()


def test_routing_v2_spec_rejects_operator_template() -> None:
    """A v2 spec auto-routes — passing --template / --parameters-json is an
    operator error.
    """
    with pytest.raises(_SEED_MOD.SeedError, match="auto-routes"):
        _SEED_MOD._resolve_template_routing(
            _fixture_spec("09_regime_state_supertrend.json"),
            "breakout",
            {"breakout_period": 20},
        )


def test_routing_v2_tier3_spec_is_accepted() -> None:
    """Turtle System 1 uses prior_signal (Tier-3). Since A.6 the trader
    runs Tier-3 specs through the live shadow stepper, so the seed script
    routes a Tier-3 spec to template='spec' like any other v2 spec.
    """
    template, parameters, routing = _SEED_MOD._resolve_template_routing(
        _fixture_spec("11_turtle_system1.json"),
        None,
        None,
    )
    assert template == "spec"
    assert parameters["spec"]["schema_version"] == "2.0"
    assert "spec" in routing.lower()


def test_routing_v1_spec_uses_operator_template() -> None:
    """A v1-style (non-stateful) spec uses the operator-supplied template."""
    template, parameters, routing = _SEED_MOD._resolve_template_routing(
        _fixture_spec("01_golden_cross.json"),
        "ma_trend",
        {"fast_ema_period": 12, "slow_ema_period": 26},
    )
    assert template == "ma_trend"
    assert parameters == {"fast_ema_period": 12, "slow_ema_period": 26}
    assert "v1" in routing.lower()


def test_routing_v1_spec_auto_routes_to_spec() -> None:
    """A v1-style (non-stateful) spec with no --template auto-routes to
    template='spec' — provided it is SpecTemplate-compatible (here, it
    carries a stop_loss exit). This is how a non-stateful new-indicator
    strategy is seeded. (Was previously rejected; the rejection was
    removed for SpecTemplate-compatible specs.)
    """
    template, parameters, routing = _SEED_MOD._resolve_template_routing(
        _fixture_spec("02_rsi_mean_reversion.json"),
        None,
        None,
    )
    assert template == "spec"
    assert parameters["spec"]["schema_version"] == "1.0"
    assert "spec" in routing.lower()


def test_routing_partial_template_args_rejected() -> None:
    """--template without --parameters-json (or vice versa) is an error."""
    with pytest.raises(_SEED_MOD.SeedError, match="supplied together"):
        _SEED_MOD._resolve_template_routing(
            _fixture_spec("01_golden_cross.json"),
            "ma_trend",
            None,
        )


def test_routing_rejects_malformed_spec_json() -> None:
    with pytest.raises(_SEED_MOD.SeedError, match="StrategySpec validation"):
        _SEED_MOD._resolve_template_routing({"not": "a spec"}, "ma_trend", {})


# ---- Integration: testcontainers Postgres + main() ------------------------


pytestmark_integration = pytest.mark.integration


@pytest.fixture(scope="module")
def pg_container() -> Iterator[Any]:
    pytest.importorskip("testcontainers.postgres")
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer("postgres:16.6-alpine")
    container.start()
    try:
        yield container
    finally:
        container.stop()


@pytest.fixture(scope="module")
def database_url(pg_container: Any) -> str:
    url = pg_container.get_connection_url()  # type: ignore[attr-defined]
    return url.replace("+psycopg2", "")


@pytest.fixture(scope="module", autouse=True)
def _prepare_db(database_url: str) -> None:
    from marketmind_workers.db import apply_migrations

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute('CREATE EXTENSION IF NOT EXISTS "pgcrypto"')
    apply_migrations(database_url)


@pytest.fixture
def _clean(database_url: str) -> None:
    """Reset every table the seed touches between tests."""
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            TRUNCATE TABLE
                trader_strategy_versions,
                trader_strategies,
                overfitting_analyses,
                backtest_results,
                extracted_strategies,
                transcripts,
                ingested_content
            RESTART IDENTITY CASCADE
            """,
        )
        conn.commit()


def _seed_extraction_chain(
    database_url: str,
    *,
    metrics_overrides: dict[str, Any] | None = None,
    skip_overfitting: bool = False,
    spec_json: dict[str, Any] | None = None,
) -> UUID:
    """Insert a fake content → transcript → extraction → backtest
    → (optional) overfitting chain. Returns the extraction's id.

    `spec_json` is the extracted spec stored on the extraction row; it
    must be a real `StrategySpec` (the seed script parses it for template
    routing). Defaults to the v1 Golden Cross fixture — a non-stateful
    spec, so the chain takes the operator-supplied-template path.
    """
    metrics = _sample_metrics()
    if metrics_overrides:
        metrics.update(metrics_overrides)
    walk_forward = _sample_walk_forward()
    spec_payload = spec_json if spec_json is not None else _fixture_spec("01_golden_cross.json")

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO ingested_content (id, source_type, content_json)
            VALUES (%s, 'raw_text', %s)
            RETURNING id
            """,
            (str(uuid4()), Jsonb({"source_type": "raw_text", "text": "test"})),
        )
        c_row = cur.fetchone()
        assert c_row is not None
        content_id = c_row[0]
        cur.execute(
            """
            INSERT INTO transcripts (id, content_id, language, full_text,
                                     segments_json, duration_seconds, model_name)
            VALUES (%s, %s, 'en', 'test', %s, 1.0, 'test-stub')
            RETURNING id
            """,
            (str(uuid4()), str(content_id), Jsonb([])),
        )
        t_row = cur.fetchone()
        assert t_row is not None
        transcript_id = t_row[0]
        cur.execute(
            """
            INSERT INTO extracted_strategies (id, transcript_id, spec_json)
            VALUES (%s, %s, %s) RETURNING id
            """,
            (
                str(uuid4()),
                str(transcript_id),
                Jsonb(spec_payload),
            ),
        )
        e_row = cur.fetchone()
        assert e_row is not None
        extraction_id = UUID(str(e_row[0]))
        cur.execute(
            """
            INSERT INTO backtest_results (
                id, strategy_id, start_ts, end_ts, initial_capital,
                result_json, data_fetch_seconds, compute_seconds
            ) VALUES (%s, %s, %s, %s, %s, %s, 1.0, 1.0)
            RETURNING id
            """,
            (
                str(uuid4()),
                str(extraction_id),
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 6, 1, tzinfo=UTC),
                10000.0,
                Jsonb({"metrics": metrics}),
            ),
        )
        b_row = cur.fetchone()
        assert b_row is not None
        backtest_id = b_row[0]
        if not skip_overfitting:
            cur.execute(
                """
                INSERT INTO overfitting_analyses (
                    id, backtest_id, walk_forward_json, parameter_sweep_json,
                    monte_carlo_json, deflated_sharpe_json,
                    composite_score_json, compute_seconds
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 1.0)
                """,
                (
                    str(uuid4()),
                    str(backtest_id),
                    Jsonb(walk_forward),
                    Jsonb({"peakiness_score": 0.3, "baseline_rank_percentile": 0.7}),
                    Jsonb({"p_value": 0.04, "percentile_rank": 0.96}),
                    Jsonb({"deflated_sharpe": 0.8, "probabilistic_sharpe": 0.85}),
                    Jsonb({"score": 0.72, "verdict": "robust"}),
                ),
            )
        conn.commit()
    return extraction_id


def _argv(
    extraction_id: UUID,
    *,
    name: str = "Test Strategy",
    template: str | None = "ma_trend",
    parameters: str | None = '{"fast_ema_period": 12, "slow_ema_period": 26}',
    extra: list[str] | None = None,
) -> list[str]:
    args = ["--extraction-id", str(extraction_id), "--name", name]
    # A v2 stateful spec auto-routes — callers pass template=None /
    # parameters=None to omit --template / --parameters-json.
    if template is not None:
        args += ["--template", template]
    if parameters is not None:
        args += ["--parameters-json", parameters]
    args += [
        "--symbols",
        "BTC/USDT",
        "--timeframes",
        "4h",
        "--risk-pct",
        "0.005",
        "--fee-bps",
        "10",
        "--slippage-bps",
        "10",
    ]
    if extra:
        args.extend(extra)
    return args


@pytest.mark.integration
def test_main_inserts_strategy_and_version_with_approved_paper_false(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    extraction_id = _seed_extraction_chain(database_url)

    rc = _SEED_MOD.main(_argv(extraction_id))
    assert rc == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trader_strategies")
        s_row = cur.fetchone()
        cur.execute(
            "SELECT version, approved_for_paper, approved_for_live, enabled, "
            "template, symbols, timeframes, risk_pct "
            "FROM trader_strategy_versions",
        )
        v_rows = cur.fetchall()
    assert s_row is not None
    assert s_row[0] == 1
    assert len(v_rows) == 1
    v = v_rows[0]
    assert v[0] == 1  # version
    assert v[1] is False  # approved_for_paper — load-bearing
    assert v[2] is False  # approved_for_live
    assert v[3] is True  # enabled
    assert v[4] == "ma_trend"
    assert v[5] == ["BTC/USDT"]
    assert v[6] == ["4h"]
    from decimal import Decimal

    assert v[7] == Decimal("0.005")


@pytest.mark.integration
def test_main_seeds_v2_spec_as_spec_template(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A.5a acceptance — a v2 stateful spec (the Supertrend regime
    fixture) seeds end-to-end as template='spec' via auto-routing, with
    no --template / --parameters-json. The INSERT lands cleanly only
    because migration 0012 widened the `template` CHECK constraint.
    """
    monkeypatch.setenv("DATABASE_URL", database_url)
    extraction_id = _seed_extraction_chain(
        database_url,
        spec_json=_fixture_spec("09_regime_state_supertrend.json"),
    )

    rc = _SEED_MOD.main(_argv(extraction_id, template=None, parameters=None))
    assert rc == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT template, parameters FROM trader_strategy_versions")
        rows = cur.fetchall()
    assert len(rows) == 1
    template, parameters = rows[0]
    assert template == "spec"  # auto-routed; CHECK admits it post-0012
    assert parameters["spec"]["schema_version"] == "2.0"


@pytest.mark.integration
def test_main_dry_run_writes_nothing(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    extraction_id = _seed_extraction_chain(database_url)

    rc = _SEED_MOD.main(_argv(extraction_id, extra=["--dry-run"]))
    assert rc == 0

    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert '"approved_for_paper": false' in out

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trader_strategies")
        s_row = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM trader_strategy_versions")
        v_row = cur.fetchone()
    assert s_row is not None
    assert v_row is not None
    assert s_row[0] == 0
    assert v_row[0] == 0


@pytest.mark.integration
def test_main_reruns_same_name_reuses_strategy_and_bumps_version(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    extraction_id = _seed_extraction_chain(database_url)

    assert _SEED_MOD.main(_argv(extraction_id, name="Shared Name")) == 0
    assert _SEED_MOD.main(_argv(extraction_id, name="Shared Name")) == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM trader_strategies")
        s_row = cur.fetchone()
        cur.execute("SELECT version FROM trader_strategy_versions ORDER BY version")
        v_rows = cur.fetchall()
    assert s_row is not None
    assert s_row[0] == 1
    assert [r[0] for r in v_rows] == [1, 2]


@pytest.mark.integration
def test_main_errors_when_extraction_not_found(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    bogus = uuid4()
    rc = _SEED_MOD.main(_argv(bogus))
    assert rc == 1
    err = capsys.readouterr().err
    assert "not found" in err


@pytest.mark.integration
def test_main_errors_when_overfitting_missing(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    extraction_id = _seed_extraction_chain(database_url, skip_overfitting=True)
    rc = _SEED_MOD.main(_argv(extraction_id))
    assert rc == 1
    err = capsys.readouterr().err
    assert "No overfitting_analyses" in err


@pytest.mark.integration
def test_main_errors_when_spec_json_null(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The Phase 2.2 schema allows extraction rows with spec_json
    NULL (refusal verdicts). The seed script must reject those.
    """
    monkeypatch.setenv("DATABASE_URL", database_url)
    extraction_id = _seed_extraction_chain(database_url)
    # Patch the spec_json to NULL.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE extracted_strategies SET spec_json = NULL WHERE id = %s",
            (str(extraction_id),),
        )
        conn.commit()
    rc = _SEED_MOD.main(_argv(extraction_id))
    assert rc == 1
    err = capsys.readouterr().err
    assert "spec_json=NULL" in err


def _add_extra_backtest(
    database_url: str,
    extraction_id: UUID,
    *,
    win_rate: float,
    created_at: datetime,
) -> tuple[UUID, UUID]:
    """Insert a second backtest_results row (with a fresh
    overfitting_analyses) under the same extraction, distinguishable
    by `win_rate`. Returns (backtest_id, overfitting_id).
    """
    walk_forward = _sample_walk_forward()
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO backtest_results (
                id, strategy_id, start_ts, end_ts, initial_capital,
                result_json, data_fetch_seconds, compute_seconds, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, 1.0, 1.0, %s)
            RETURNING id
            """,
            (
                str(uuid4()),
                str(extraction_id),
                datetime(2026, 2, 1, tzinfo=UTC),
                datetime(2026, 7, 1, tzinfo=UTC),
                10000.0,
                Jsonb(
                    {
                        "metrics": {
                            "win_rate": win_rate,
                            "expectancy": 0.012,
                            "max_drawdown_pct": 0.08,
                        },
                    },
                ),
                created_at,
            ),
        )
        b_row = cur.fetchone()
        assert b_row is not None
        backtest_id = UUID(str(b_row[0]))
        cur.execute(
            """
            INSERT INTO overfitting_analyses (
                id, backtest_id, walk_forward_json, parameter_sweep_json,
                monte_carlo_json, deflated_sharpe_json,
                composite_score_json, compute_seconds
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 1.0)
            RETURNING id
            """,
            (
                str(uuid4()),
                str(backtest_id),
                Jsonb(walk_forward),
                Jsonb({"peakiness_score": 0.3, "baseline_rank_percentile": 0.7}),
                Jsonb({"p_value": 0.04, "percentile_rank": 0.96}),
                Jsonb({"deflated_sharpe": 0.8, "probabilistic_sharpe": 0.85}),
                Jsonb({"score": 0.72, "verdict": "robust"}),
            ),
        )
        of_row = cur.fetchone()
        assert of_row is not None
        overfitting_id = UUID(str(of_row[0]))
        conn.commit()
    return backtest_id, overfitting_id


@pytest.mark.integration
def test_main_backtest_id_flag_picks_explicit_older_backtest(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With two backtests under one extraction (old + new), the
    default selection picks the newer one. Passing --backtest-id
    pointed at the older row makes the seed read THAT row's metrics
    instead. Verifies the resulting backtest_metrics.single_pass.win_rate
    matches the older backtest's distinct value.
    """
    monkeypatch.setenv("DATABASE_URL", database_url)

    # _seed_extraction_chain inserts the FIRST backtest with win_rate=0.55
    # (the _sample_metrics default) and `created_at = NOW()` (DB default).
    # We then insert a second backtest 1 second later with win_rate=0.77.
    extraction_id = _seed_extraction_chain(database_url)
    older_win_rate = 0.55
    newer_win_rate = 0.77

    # The "older" row is whatever _seed_extraction_chain just inserted.
    # Find its backtest_id so we can pin to it.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM backtest_results WHERE strategy_id = %s",
            (str(extraction_id),),
        )
        b_row = cur.fetchone()
    assert b_row is not None
    older_backtest_id = UUID(str(b_row[0]))

    # Insert a second backtest dated 10 seconds later so the default
    # "ORDER BY created_at DESC LIMIT 1" picks it.
    newer_created = datetime.now(UTC) + timedelta(seconds=10)
    _add_extra_backtest(
        database_url,
        extraction_id,
        win_rate=newer_win_rate,
        created_at=newer_created,
    )

    # Pin --backtest-id to the OLDER row.
    rc = _SEED_MOD.main(
        _argv(
            extraction_id,
            extra=["--backtest-id", str(older_backtest_id)],
        ),
    )
    assert rc == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT backtest_metrics FROM trader_strategy_versions LIMIT 1",
        )
        v_row = cur.fetchone()
    assert v_row is not None
    backtest_metrics = dict(v_row[0])
    # Reading the OLDER backtest's win_rate proves the flag pinned correctly;
    # without it, the default-latest path would have read newer_win_rate.
    assert backtest_metrics["single_pass"]["win_rate"] == older_win_rate, (
        f"--backtest-id pinned to older row but got win_rate="
        f"{backtest_metrics['single_pass']['win_rate']}, expected {older_win_rate}"
    )


@pytest.mark.integration
def test_main_backtest_id_flag_errors_on_strategy_mismatch(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--backtest-id pointing at a backtest belonging to a different
    extraction must be rejected pre-write.
    """
    monkeypatch.setenv("DATABASE_URL", database_url)
    extraction_a = _seed_extraction_chain(database_url)
    extraction_b = _seed_extraction_chain(database_url)

    # Find one of extraction_b's backtests.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM backtest_results WHERE strategy_id = %s LIMIT 1",
            (str(extraction_b),),
        )
        row = cur.fetchone()
    assert row is not None
    foreign_backtest_id = UUID(str(row[0]))

    rc = _SEED_MOD.main(
        _argv(
            extraction_a,
            extra=["--backtest-id", str(foreign_backtest_id)],
        ),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "not the requested extraction" in err


@pytest.mark.integration
def test_main_backtest_id_flag_errors_on_missing_row(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DATABASE_URL", database_url)
    extraction_id = _seed_extraction_chain(database_url)
    bogus = uuid4()
    rc = _SEED_MOD.main(
        _argv(extraction_id, extra=["--backtest-id", str(bogus)]),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "backtest_results row not found" in err


@pytest.mark.integration
def test_main_overfitting_id_flag_errors_on_backtest_mismatch(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--overfitting-id pointing at an analysis whose backtest_id
    differs from the chosen backtest must be rejected.
    """
    monkeypatch.setenv("DATABASE_URL", database_url)
    extraction_id = _seed_extraction_chain(database_url)

    # Find the first (auto-seeded) backtest_id and a different overfitting_id
    # by inserting a second backtest + analysis under the same extraction.
    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM backtest_results WHERE strategy_id = %s",
            (str(extraction_id),),
        )
        b_row = cur.fetchone()
    assert b_row is not None
    first_backtest_id = UUID(str(b_row[0]))

    _bt2, foreign_of = _add_extra_backtest(
        database_url,
        extraction_id,
        win_rate=0.66,
        created_at=datetime.now(UTC) + timedelta(seconds=5),
    )

    # Pin backtest to the first row, but overfitting to the second's analysis.
    rc = _SEED_MOD.main(
        _argv(
            extraction_id,
            extra=[
                "--backtest-id",
                str(first_backtest_id),
                "--overfitting-id",
                str(foreign_of),
            ],
        ),
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "not the chosen backtest" in err


@pytest.mark.integration
def test_main_dry_run_prints_resolved_ids(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run output must surface the exact backtest_id +
    overfitting_id that would be written, so the operator can
    verify before the real run.
    """
    monkeypatch.setenv("DATABASE_URL", database_url)
    extraction_id = _seed_extraction_chain(database_url)

    rc = _SEED_MOD.main(_argv(extraction_id, extra=["--dry-run"]))
    assert rc == 0
    out = capsys.readouterr().out
    assert "resolved_backtest_id" in out
    assert "resolved_overfitting_id" in out


@pytest.mark.integration
def test_main_writes_backtest_metrics_that_pass_admin_validator(
    database_url: str,
    _clean: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end round-trip: seed → fetch the version row's
    backtest_metrics JSONB → run it through the same validator the
    admin approve_paper endpoint uses. This is the integration
    proof that seed+approve work together.
    """
    from marketmind_api.routes.trader_admin import _missing_backtest_metric_keys

    monkeypatch.setenv("DATABASE_URL", database_url)
    extraction_id = _seed_extraction_chain(database_url)
    assert _SEED_MOD.main(_argv(extraction_id)) == 0

    with psycopg.connect(database_url) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT backtest_metrics FROM trader_strategy_versions LIMIT 1",
        )
        row = cur.fetchone()
    assert row is not None
    backtest_metrics = dict(row[0])
    missing = _missing_backtest_metric_keys(backtest_metrics)
    assert missing == [], f"seeded backtest_metrics failed admin validator: missing={missing}"
