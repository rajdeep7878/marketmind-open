"""Paper-only proofs (mandate Stage 7):

- test_paper_only_no_private_api — PublicOnlyExchange raises on private
  methods; no API-key plumbing exists anywhere in FTR modules
- test_no_live_env_escape — exhaustive scan of FTR config/env parsing: no
  setting yields a non-PAPER execution mode
- test_uk_compliance_guard — perp/future/CFD instruments rejected for any
  execution mode; research_simulation_only backtest-only, refused by the
  paper trader by type
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from marketmind_workers.ftr.strategies.specs import (
    FTRInstrument,
    MLHourlySpec,
    OFIResearchSpec,
    validate_ftr_spec,
)
from marketmind_workers.ftr.trader.execution_mode import (
    EXECUTION_MODE,
    ExecutionMode,
    PaperOnlyViolation,
)
from marketmind_workers.ftr.trader.public_exchange import PublicOnlyExchange
from marketmind_workers.ftr.trader.runner import load_deployments
from pydantic import ValidationError

FTR_SRC = Path(__file__).resolve().parents[1] / "src" / "marketmind_workers" / "ftr"


# ---------------------------------------------------------------------------
# ExecutionMode: single member, no escape
# ---------------------------------------------------------------------------


def test_execution_mode_has_exactly_one_member() -> None:
    assert list(ExecutionMode) == [ExecutionMode.PAPER]
    assert EXECUTION_MODE is ExecutionMode.PAPER


def test_no_live_env_escape() -> None:
    """Scan every FTR source file: no env var, config field, or literal can
    introduce a non-PAPER execution mode, and TRADER_ALLOW_LIVE-style knobs
    do not exist in the FTR namespace."""
    forbidden = re.compile(
        r"(ALLOW_LIVE|allow_live|live_trading|LiveBroker|live_mode|"
        r"ExecutionMode\.LIVE|\"live\"\s*[:=]|'live'\s*[:=])"
    )
    for path in FTR_SRC.rglob("*.py"):
        text = path.read_text()
        match = forbidden.search(text)
        assert match is None, f"{path}: forbidden live-mode token {match.group(0)!r}"


def test_ftr_settings_has_no_execution_mode_field() -> None:
    from marketmind_workers.ftr.config.settings import FTRSettings

    field_names = set(FTRSettings.model_fields)
    assert not any("live" in f or "execution" in f or "mode" in f for f in field_names), (
        "FTRSettings must not carry any execution-mode knob"
    )


# ---------------------------------------------------------------------------
# PublicOnlyExchange: no private API surface
# ---------------------------------------------------------------------------


def test_public_only_exchange_blocks_private_methods() -> None:
    ex = PublicOnlyExchange("binance")
    for private in (
        "create_order",
        "cancel_order",
        "fetch_balance",
        "withdraw",
        "transfer",
        "fetch_my_trades",
        "create_market_buy_order",
    ):
        with pytest.raises(PaperOnlyViolation):
            getattr(ex, private)


def test_no_api_key_plumbing_in_ftr_modules() -> None:
    key_pattern = re.compile(r"(apiKey|api_key|API_KEY|secret\s*=|SECRET)")
    allowed = {"recorder.py"}  # mentions 'public/unauthenticated' in docstrings only
    for path in FTR_SRC.rglob("*.py"):
        text = path.read_text()
        for m in key_pattern.finditer(text):
            line = text[: m.start()].count("\n") + 1
            snippet = text.splitlines()[line - 1].strip()
            if snippet.startswith("#") or '"""' in snippet or "no API key" in snippet.lower():
                continue
            raise AssertionError(
                f"{path.name}:{line} possible API-key plumbing: {snippet!r}"
                f"{' (file not in allowlist)' if path.name not in allowed else ''}"
            )


# ---------------------------------------------------------------------------
# UK retail compliance guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "symbol",
    ["BTC-PERP", "BTCUSDT-PERPETUAL", "BTC/USD:FUTURE", "ETH-CFD", "BTC-MARGIN-SWAP"],
)
def test_uk_compliance_rejects_derivative_instruments(symbol: str) -> None:
    with pytest.raises(ValidationError, match="prohibited for UK retail"):
        FTRInstrument.model_validate({"symbol": symbol, "exchange": "binance"})


def test_instrument_type_is_spot_only_by_type() -> None:
    with pytest.raises(ValidationError):
        FTRInstrument.model_validate(
            {"symbol": "BTC/USDT", "exchange": "binance", "instrument_type": "perp"}
        )


def test_research_simulation_only_is_frozen_true_on_ofi() -> None:
    spec = validate_ftr_spec(
        {
            "kind": "ofi_microstructure_research",
            "strategy_id": "ofi-test",
            "venue_profile": "binance_spot_reference",
            "instrument": {"symbol": "BTC/USDT", "exchange": "binance"},
        }
    )
    assert isinstance(spec, OFIResearchSpec)
    assert spec.research_simulation_only is True
    # cannot be constructed with False
    with pytest.raises(ValidationError):
        validate_ftr_spec(
            {
                "kind": "ofi_microstructure_research",
                "strategy_id": "ofi-test",
                "venue_profile": "binance_spot_reference",
                "instrument": {"symbol": "BTC/USDT", "exchange": "binance"},
                "research_simulation_only": False,
            }
        )


def test_paper_trader_refuses_research_only_by_type(tmp_path: Path) -> None:
    deployments = tmp_path / "deployments.json"
    deployments.write_text(
        json.dumps(
            {
                "deployments": [
                    {
                        "kind": "ofi_microstructure_research",
                        "strategy_id": "ofi-sneak",
                        "venue_profile": "binance_spot_reference",
                        "instrument": {"symbol": "BTC/USDT", "exchange": "binance"},
                    }
                ]
            }
        )
    )
    with pytest.raises(TypeError, match=r"BY TYPE"):
        load_deployments(deployments, db_url=None)


def test_paper_trader_refuses_infeasible_venue(tmp_path: Path) -> None:
    deployments = tmp_path / "deployments.json"
    deployments.write_text(
        json.dumps(
            {
                "deployments": [
                    {
                        "kind": "ml_hourly_longflat",
                        "strategy_id": "ml-sneak",
                        "venue_profile": "binance_spot_reference",
                        "instrument": {"symbol": "BTC/USDT", "exchange": "binance"},
                        "horizon_bars": 12,
                    }
                ]
            }
        )
    )
    with pytest.raises(ValueError, match=r"uk_execution_feasible"):
        load_deployments(deployments, db_url=None)


def test_short_ml_horizons_locked_to_reference_profile() -> None:
    with pytest.raises(ValidationError, match="research-only on binance_spot_reference"):
        MLHourlySpec.model_validate(
            {
                "kind": "ml_hourly_longflat",
                "strategy_id": "ml-h1-sneak",
                "venue_profile": "kraken_pro_uk_tier0",
                "instrument": {"symbol": "BTC/USDT", "exchange": "binance"},
                "horizon_bars": 1,
            }
        )
