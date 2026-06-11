"""Phase C C.4.1 — SessionHours schema population tests.

C.1.1 shipped `SessionHours` as a field-free forward declaration plus
the `Instrument.session_hours: SessionHours | None = None` field.
C.4.1 (2026-05-26) populates the body per design doc §C.4 — minimum
path subset: schema fields only, no pandas_market_calendars library,
no DST helper, no per-venue calendar tables. The backtest engine
(C.5) consumes these fields via structural weekend-skip
(`df.index.weekday >= 5`) without a calendar library.

Test coverage:
  1. Required fields rejected when missing (calendar, open_utc, close_utc)
  2. Happy paths covering documented use cases (FX 24/5, NYSE equity)
  3. weekend_closed defaults to True (the FX-default — most non-crypto
     venues are weekend-closed)
  4. HH:MM regex correctly rejects invalid times (25:00, 9:00 missing
     leading zero, garbage)
  5. Optional pre/post-market fields accept None and valid HH:MM
  6. Round-trip serialisation is identity
  7. v1 regression sentinel — pre-C.4.1 specs (no SessionHours) validate
     identically because Instrument.session_hours defaults to None
"""

from __future__ import annotations

from typing import Any

import pytest
from marketmind_shared.schemas import validate_spec
from marketmind_shared.schemas.strategy_spec import Instrument, SessionHours

# ---- required field validation -------------------------------------------


def test_session_hours_missing_calendar_rejected() -> None:
    with pytest.raises(Exception, match=r"calendar"):
        SessionHours(open_utc="00:00", close_utc="00:00")  # type: ignore[call-arg]


def test_session_hours_missing_open_utc_rejected() -> None:
    with pytest.raises(Exception, match=r"open_utc"):
        SessionHours(calendar="cme_fx", close_utc="00:00")  # type: ignore[call-arg]


def test_session_hours_missing_close_utc_rejected() -> None:
    with pytest.raises(Exception, match=r"close_utc"):
        SessionHours(calendar="cme_fx", open_utc="00:00")  # type: ignore[call-arg]


def test_session_hours_empty_calendar_rejected() -> None:
    """Calendar string must be non-empty (Field(min_length=1))."""
    with pytest.raises(Exception, match=r"calendar"):
        SessionHours(calendar="", open_utc="00:00", close_utc="00:00")


# ---- documented happy-path examples --------------------------------------


def test_session_hours_fx_24_5_canonical_example() -> None:
    """Design doc §C.4 example: FX 24/5 via Oanda. Sunday 22:00 UTC
    open through Friday 22:00 UTC close.
    """
    sh = SessionHours(
        calendar="cme_fx",
        open_utc="22:00",
        close_utc="22:00",
        weekend_closed=True,
    )
    assert sh.calendar == "cme_fx"
    assert sh.open_utc == "22:00"
    assert sh.close_utc == "22:00"
    assert sh.weekend_closed is True
    assert sh.pre_market_open_utc is None
    assert sh.post_market_close_utc is None


def test_session_hours_nyse_equity_canonical_example() -> None:
    """Design doc §C.4 example: NYSE equity. 14:30-21:00 UTC (09:30-16:00
    ET during DST; the calendar-library integration in C.4-full handles
    DST conversion — this test pins the post-DST values directly).
    """
    sh = SessionHours(
        calendar="nyse",
        open_utc="14:30",
        close_utc="21:00",
        weekend_closed=True,
        pre_market_open_utc="09:00",  # 04:00 ET pre-market
        post_market_close_utc="00:00",  # 19:00 ET post-market close
    )
    assert sh.calendar == "nyse"
    assert sh.open_utc == "14:30"
    assert sh.close_utc == "21:00"
    assert sh.pre_market_open_utc == "09:00"
    assert sh.post_market_close_utc == "00:00"


def test_session_hours_24_7_identity_case() -> None:
    """Edge case: a 24/7 SessionHours (crypto-style) with weekend_closed
    False. The canonical crypto representation is Instrument.session_hours
    = None, but an explicit 24/7 SessionHours must be valid for callers
    that want symmetry (e.g., a future test fixture).
    """
    sh = SessionHours(
        calendar="24/7",
        open_utc="00:00",
        close_utc="00:00",
        weekend_closed=False,
    )
    assert sh.weekend_closed is False


# ---- weekend_closed default ----------------------------------------------


def test_weekend_closed_defaults_to_true() -> None:
    """Most non-crypto venues are weekend-closed — this is the right
    safe default for the C.4 minimum path.
    """
    sh = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00")
    assert sh.weekend_closed is True


# ---- HH:MM regex coverage -------------------------------------------------


@pytest.mark.parametrize(
    "valid_time",
    [
        "00:00",   # midnight
        "23:59",   # last minute of day
        "09:30",   # NYSE open in ET
        "14:30",   # NYSE open in UTC (DST)
        "22:00",   # FX session boundary
        "07:00",   # arbitrary single-digit-feeling hour with leading zero
        "12:00",
        "01:01",
    ],
)
def test_hhmm_regex_accepts_valid_times(valid_time: str) -> None:
    sh = SessionHours(calendar="x", open_utc=valid_time, close_utc=valid_time)
    assert sh.open_utc == valid_time
    assert sh.close_utc == valid_time


@pytest.mark.parametrize(
    "invalid_time",
    [
        "25:00",   # hour out of range (doc's looser regex would accept)
        "24:00",   # 24:00 not a valid time
        "23:60",   # minute out of range
        "9:00",    # missing leading zero
        "00:0",    # missing leading zero on minute
        "ab:cd",   # garbage
        "",        # empty
        "00:00:00", # extra component
        "13.30",   # wrong separator
    ],
)
def test_hhmm_regex_rejects_invalid_times(invalid_time: str) -> None:
    with pytest.raises(Exception, match=r"open_utc|pattern"):
        SessionHours(calendar="x", open_utc=invalid_time, close_utc="00:00")


# ---- optional pre/post-market fields -------------------------------------


def test_pre_market_open_utc_accepts_none() -> None:
    sh = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00")
    assert sh.pre_market_open_utc is None


def test_pre_market_open_utc_validates_when_present() -> None:
    sh = SessionHours(
        calendar="nyse", open_utc="14:30", close_utc="21:00",
        pre_market_open_utc="09:00",
    )
    assert sh.pre_market_open_utc == "09:00"


def test_pre_market_open_utc_rejects_invalid_when_present() -> None:
    with pytest.raises(Exception, match=r"pre_market_open_utc|pattern"):
        SessionHours(
            calendar="nyse", open_utc="14:30", close_utc="21:00",
            pre_market_open_utc="25:00",
        )


def test_post_market_close_utc_accepts_none() -> None:
    sh = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00")
    assert sh.post_market_close_utc is None


def test_post_market_close_utc_validates_when_present() -> None:
    sh = SessionHours(
        calendar="nyse", open_utc="14:30", close_utc="21:00",
        post_market_close_utc="00:00",
    )
    assert sh.post_market_close_utc == "00:00"


# ---- round-trip serialisation --------------------------------------------


def test_round_trip_serialisation_is_identity() -> None:
    """model_dump → model_validate must yield an equal SessionHours."""
    original = SessionHours(
        calendar="nyse", open_utc="14:30", close_utc="21:00",
        weekend_closed=True,
        pre_market_open_utc="09:00",
        post_market_close_utc="00:00",
    )
    dumped = original.model_dump()
    restored = SessionHours.model_validate(dumped)
    assert original == restored


def test_round_trip_with_optional_fields_omitted() -> None:
    original = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00")
    dumped = original.model_dump()
    # Optional fields should still be None in the dump.
    assert dumped["pre_market_open_utc"] is None
    assert dumped["post_market_close_utc"] is None
    restored = SessionHours.model_validate(dumped)
    assert original == restored


def test_extra_fields_rejected() -> None:
    """_StrictModel forbids extra fields — pins the schema against typos."""
    with pytest.raises(Exception, match=r"extra_forbidden|Extra inputs"):
        SessionHours.model_validate({
            "calendar": "cme_fx",
            "open_utc": "22:00",
            "close_utc": "22:00",
            "weekend_close": True,  # typo: missing "d"
        })


# ---- Instrument integration: backward-compat sentinel --------------------


def test_instrument_default_session_hours_is_none() -> None:
    """C.1.1 wired session_hours=None default; C.4.1 must preserve.
    Every pre-C.4.1 crypto_spot spec validates identically because
    SessionHours stays None and only callers that explicitly set it
    see the new field.
    """
    i = Instrument(symbol="BTC/USDT", exchange="binance", quote_currency="USDT")
    assert i.session_hours is None


def test_instrument_accepts_fx_session_hours() -> None:
    """Forward-path: Instrument can carry a populated SessionHours."""
    sh = SessionHours(calendar="cme_fx", open_utc="22:00", close_utc="22:00")
    i = Instrument(
        symbol="EUR/USD",
        exchange="oanda",
        quote_currency="USD",
        asset_class="fx_spot",
        session_hours=sh,
    )
    assert i.session_hours == sh
    assert i.session_hours.calendar == "cme_fx"  # type: ignore[union-attr]


# ---- v1 regression sentinel: pre-C.4.1 spec round-trip --------------------


def _minimal_v1_spec_dict() -> dict[str, Any]:
    """A pre-Phase-C spec dict that omits asset_class + session_hours
    entirely — mirrors the shape of every fixture in tests/fixtures/
    strategies/valid/. C.1.1 + C.4.1 must keep this validating bit-
    identically; that's the load-bearing regression.
    """
    return {
        "schema_version": "1.0",
        "name": "v1 regression sentinel",
        "instrument": {
            "symbol": "BTC/USDT",
            "exchange": "binance",
            "quote_currency": "USDT",
        },
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


def test_pre_c41_crypto_spec_validates_with_no_session_hours() -> None:
    """A spec dict with no `session_hours` key on Instrument must
    validate to an Instrument with `session_hours=None`. This is the
    load-bearing backward-compat invariant for C.4.1.
    """
    spec, _warnings = validate_spec(_minimal_v1_spec_dict())
    assert spec.instrument.session_hours is None
    # Round-trip: dump + reload preserves the None.
    dumped = spec.model_dump(mode="json")
    spec2, _ = validate_spec(dumped)
    assert spec == spec2
