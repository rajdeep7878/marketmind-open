"""Phase C C.1.1 — AssetClass + ContractSpecs + SessionHours schema additions.

Three new pieces in `shared/.../strategy_spec/common.py` (2026-05-26):
  1. `AssetClass = Literal[crypto_spot, fx_spot, metals_spot, equity_etf,
     equity_single]` — the value set of supported venues.
  2. `Instrument` gains three fields with crypto-spot-compatible defaults:
     `asset_class="crypto_spot"`, `contract_specs=None`, `session_hours=None`.
  3. `ContractSpecs` and `SessionHours` are FORWARD-DECLARED `_StrictModel`s
     with no fields — populated in C.3 (lot/contract math) and C.4 (calendar
     handling). They must be importable, instantiable, and serialize as `{}`.

The load-bearing assertion across this whole sub-phase is **backward
compatibility**: every pre-Phase-C spec (with no asset_class field in its
serialized JSON) must parse into the new schema as `asset_class="crypto_spot"`
and produce no behavioural change. The 22-test fixture round-trip suite
(`test_valid_fixtures.py`) demonstrates this for the 11 valid corpus
fixtures; the tests here add specific defaults/enum-membership coverage.

Empirical-inspection step (META-PATTERN, v1.2 retrospective standing
rule): every assertion was checked via the smoke-test print before
encoding.
"""

from __future__ import annotations

import pytest
from marketmind_shared.schemas import validate_spec
from marketmind_shared.schemas.strategy_spec import (
    AssetClass,
    ContractSpecs,
    Instrument,
    SessionHours,
)


def _minimal_spec_dict(instrument_extras: dict[str, object] | None = None) -> dict[str, object]:
    """A minimal valid spec dict — caller can override the instrument block."""
    instrument: dict[str, object] = {
        "symbol": "BTC/USDT",
        "exchange": "binance",
        "quote_currency": "USDT",
    }
    if instrument_extras:
        instrument.update(instrument_extras)
    return {
        "schema_version": "1.0",
        "name": "C.1.1 fixture spec",
        "instrument": instrument,
        "primary_timeframe": "4h",
        "direction": "long",
        "entry": {
            "condition": {
                "type": "compare",
                "left": {"kind": "price", "field": "close"},
                "op": ">",
                "right": {"kind": "constant", "value": 100.0},
            },
            "order_type": "market",
        },
        "exit": {
            "exits": [
                {"type": "stop_loss", "method": {"kind": "percent", "value": 0.05}},
            ],
        },
    }


# --- Instrument default behaviour (backward-compat invariants) -----------


def test_instrument_omitted_asset_class_defaults_to_crypto_spot() -> None:
    """Every pre-Phase-C spec lacks an `asset_class` field. They must
    parse and produce `asset_class == "crypto_spot"`."""
    i = Instrument(symbol="BTC/USDT", exchange="binance", quote_currency="USDT")
    assert i.asset_class == "crypto_spot"
    assert i.contract_specs is None
    assert i.session_hours is None


def test_minimal_v1_spec_without_asset_class_still_validates() -> None:
    """End-to-end: a v1-shaped spec dict with no asset_class round-trips
    through validate_spec() and gets crypto_spot defaulted in."""
    spec, _warnings = validate_spec(_minimal_spec_dict())
    assert spec.instrument.asset_class == "crypto_spot"
    assert spec.instrument.contract_specs is None
    assert spec.instrument.session_hours is None


def test_instrument_serialised_v1_then_reloaded_gains_asset_class() -> None:
    """A pre-Phase-C instrument dict has 3 keys. Loading it and dumping
    the resulting model adds the three new keys with their defaults.
    This is the documented backward-compat behaviour — the loaded model
    is structurally enriched without altering downstream semantics."""
    pre_phase_c_dict = {"symbol": "ETH/USDT", "exchange": "binance", "quote_currency": "USDT"}
    i = Instrument.model_validate(pre_phase_c_dict)
    dumped = i.model_dump(mode="json")
    assert dumped == {
        "symbol": "ETH/USDT",
        "exchange": "binance",
        "quote_currency": "USDT",
        "asset_class": "crypto_spot",
        "contract_specs": None,
        "session_hours": None,
    }


# --- AssetClass enum-value membership ------------------------------------


@pytest.mark.parametrize(
    "asset_class",
    ["crypto_spot", "fx_spot", "metals_spot", "equity_etf", "equity_single"],
)
def test_asset_class_accepts_each_documented_value(asset_class: str) -> None:
    """Each of the 5 design-doc AssetClass values must be acceptable
    on Instrument without validation error."""
    i = Instrument(
        symbol="BTC/USDT",
        exchange="binance",
        quote_currency="USDT",
        asset_class=asset_class,  # type: ignore[arg-type]
    )
    assert i.asset_class == asset_class


@pytest.mark.parametrize(
    "bad_value",
    ["", "futures", "crypto", "spot", "CRYPTO_SPOT", "fx", " crypto_spot ", "options"],
)
def test_asset_class_rejects_unknown_values(bad_value: str) -> None:
    """Unknown asset_class values must raise (StrictModel + Literal type
    coverage). This catches typos and prevents silent acceptance of
    unsupported venues that would later cascade into adapter-dispatch
    KeyErrors in C.1.4."""
    with pytest.raises(Exception, match=r"literal_error|asset_class|Input should be"):
        Instrument(
            symbol="BTC/USDT",
            exchange="binance",
            quote_currency="USDT",
            asset_class=bad_value,  # type: ignore[arg-type]
        )


def test_asset_class_literal_exposes_all_values_at_import_time() -> None:
    """`AssetClass.__args__` lets callers (e.g. adapter dispatch in C.1.4)
    enumerate the supported set at runtime. Pinning the exact set here
    guarantees that any value addition in a future sub-phase trips this
    test and forces explicit acknowledgement.
    """
    from typing import get_args

    # Phase E.3 (2026-06-06) added "crypto_perp" (Binance USDM perpetuals) —
    # additive; every prior value retained, so the pre-E.3 corpus is unaffected.
    expected = (
        "crypto_spot", "fx_spot", "metals_spot", "equity_etf", "equity_single",
        "crypto_perp",
    )
    assert get_args(AssetClass) == expected


# --- ContractSpecs / SessionHours forward refs ---------------------------


def test_contract_specs_is_importable_and_instantiable() -> None:
    """Forward-declared model: no fields yet, but must be instantiable
    and serialize cleanly."""
    cs = ContractSpecs()
    assert cs.model_dump() == {}
    # Round-trips through JSON without issue.
    assert ContractSpecs.model_validate({}) == cs


def test_session_hours_is_importable_and_instantiable() -> None:
    """Forward decl from C.1.1; body populated in C.4.1 with required
    fields (calendar, open_utc, close_utc). The C.1.1 test asserted
    SessionHours() was constructible with no args — that assumption no
    longer holds post-C.4.1. The updated assertion: SessionHours with
    its canonical FX 24/5 args constructs cleanly + round-trips.
    """
    sh = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00")
    assert sh.calendar == "cme_fx"
    # Round-trip via model_dump → model_validate is identity.
    assert SessionHours.model_validate(sh.model_dump()) == sh


def test_contract_specs_rejects_unknown_fields() -> None:
    """The _StrictModel base forbids extra fields. A future sub-phase
    that wants to add contract_size MUST land it as an explicit field;
    the validator won't silently accept ad-hoc payload keys."""
    with pytest.raises(Exception, match=r"extra_forbidden|Extra inputs"):
        ContractSpecs.model_validate({"contract_size": 100_000})


def test_session_hours_rejects_unknown_fields() -> None:
    """Same _StrictModel guarantee for SessionHours: extras rejected.
    Post-C.4.1, the payload must carry the three required fields plus
    any unknown — so the test now sends a complete-but-extra payload
    to exercise extra_forbidden specifically (rather than missing-
    required-field rejection)."""
    with pytest.raises(Exception, match=r"extra_forbidden|Extra inputs"):
        SessionHours.model_validate({
            "calendar": "nyse",
            "open_utc": "14:30",
            "close_utc": "21:00",
            "weekend_close": True,  # typo: missing "d"
        })


def test_instrument_accepts_empty_contract_specs_and_session_hours() -> None:
    """ContractSpecs stays a forward declaration (no required fields
    yet) so `ContractSpecs()` continues to construct. SessionHours got
    its fields populated in C.4.1, so the test now uses the canonical
    FX 24/5 SessionHours instead of the empty form."""
    i = Instrument(
        symbol="EUR/USD",
        exchange="oanda",
        quote_currency="USD",
        asset_class="fx_spot",
        contract_specs=ContractSpecs(),
        session_hours=SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00"),
    )
    assert i.asset_class == "fx_spot"
    assert i.contract_specs == ContractSpecs()
    assert i.session_hours == SessionHours(
        calendar="cme_fx", open_utc="22:00", close_utc="22:00",
    )
