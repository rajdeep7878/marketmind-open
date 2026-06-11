"""Phase C C.6 — unit tests for trader cycle weekend-skip helper.

Tests the C.6 alert-spam-suppression dispatch:
  1. Crypto bit-identity: crypto_spot NEVER skips, on any day of the
     week. The 3 production strategies see byte-identical cycle
     behaviour post-C.6.
  2. fx_spot / metals_spot / equity_* SKIP on Saturday + Sunday.
  3. fx_spot / metals_spot / equity_* RUN on Monday-Friday.
  4. UTC-aware datetime is required (positional contract).

The downstream alert-suppression behaviour (no `data_feed_failure`
alert fires during weekend FX cycles) is verified end-to-end in
`test_trader_ingestion_weekend_skip.py` — this module covers the
helper in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from marketmind_workers.trader.session_skip import should_skip_weekend

# Reference UTC datetimes — easy-to-read named days from 2026 (2026-01-05
# is a Monday → weekday 0; days roll through to Sunday weekday 6).
_MON = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
_TUE = datetime(2026, 1, 6, 12, 0, tzinfo=UTC)
_WED = datetime(2026, 1, 7, 12, 0, tzinfo=UTC)
_THU = datetime(2026, 1, 8, 12, 0, tzinfo=UTC)
_FRI = datetime(2026, 1, 9, 12, 0, tzinfo=UTC)
_SAT = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
_SUN = datetime(2026, 1, 11, 12, 0, tzinfo=UTC)


# ---- crypto bit-identity (THE load-bearing guard) ------------------------


@pytest.mark.parametrize("day", [_MON, _TUE, _WED, _THU, _FRI, _SAT, _SUN])
def test_crypto_spot_never_skips(day: datetime) -> None:
    """The 3 production strategies are all crypto_spot. The trader's
    cycle behaviour for crypto must be byte-identical to pre-C.6 —
    crypto is 24/7 so weekends are normal trading days.
    """
    assert should_skip_weekend("crypto_spot", day) is False, (
        f"crypto_spot must never skip; failed on {day.strftime('%A %Y-%m-%d')}"
    )


# ---- fx_spot weekend-skip behaviour --------------------------------------


@pytest.mark.parametrize(
    "weekday_dt", [_MON, _TUE, _WED, _THU, _FRI],
    ids=["Mon", "Tue", "Wed", "Thu", "Fri"],
)
def test_fx_spot_runs_on_weekdays(weekday_dt: datetime) -> None:
    """FX runs Mon-Fri (24/5)."""
    assert should_skip_weekend("fx_spot", weekday_dt) is False


@pytest.mark.parametrize("weekend_dt", [_SAT, _SUN], ids=["Sat", "Sun"])
def test_fx_spot_skips_on_weekend(weekend_dt: datetime) -> None:
    """FX skips Sat + Sun. This is the alert-spam-suppression path —
    without it, 3 consecutive Saturday cycle failures trip the
    `data_feed_failure` alert.
    """
    assert should_skip_weekend("fx_spot", weekend_dt) is True


# ---- other non-24/7 asset classes (parametrised over classes) ------------


@pytest.mark.parametrize("asset_class", ["metals_spot", "equity_etf", "equity_single"])
@pytest.mark.parametrize("weekend_dt", [_SAT, _SUN], ids=["Sat", "Sun"])
def test_non_247_classes_skip_on_weekend(asset_class: str, weekend_dt: datetime) -> None:
    """metals_spot (XAU/USD) + both equity classes follow the same
    weekend-closed rule as fx_spot. Equity holidays (e.g. Christmas
    on a Friday) are not handled here — that's C.4-full / C.9 scope.
    """
    assert should_skip_weekend(asset_class, weekend_dt) is True  # type: ignore[arg-type]


@pytest.mark.parametrize("asset_class", ["metals_spot", "equity_etf", "equity_single"])
@pytest.mark.parametrize("weekday_dt", [_MON, _WED, _FRI], ids=["Mon", "Wed", "Fri"])
def test_non_247_classes_run_on_weekday(asset_class: str, weekday_dt: datetime) -> None:
    assert should_skip_weekend(asset_class, weekday_dt) is False  # type: ignore[arg-type]


# ---- weekday-edge timestamps ---------------------------------------------


def test_friday_2359_utc_runs() -> None:
    """The last minute of Friday in UTC is still a Friday. fx_spot
    should NOT skip; the trader's next cycle (Sat 00:00 UTC) does.
    """
    fri_late = datetime(2026, 1, 9, 23, 59, tzinfo=UTC)
    assert should_skip_weekend("fx_spot", fri_late) is False


def test_saturday_0000_utc_skips() -> None:
    """First moment of Saturday: skip."""
    sat_early = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)
    assert should_skip_weekend("fx_spot", sat_early) is True


def test_sunday_2359_utc_skips() -> None:
    """Last moment of Sunday: still skipping. Monday 00:00 UTC resumes."""
    sun_late = datetime(2026, 1, 11, 23, 59, tzinfo=UTC)
    assert should_skip_weekend("fx_spot", sun_late) is True


def test_monday_0000_utc_runs() -> None:
    mon_early = datetime(2026, 1, 12, 0, 0, tzinfo=UTC)
    assert should_skip_weekend("fx_spot", mon_early) is False
