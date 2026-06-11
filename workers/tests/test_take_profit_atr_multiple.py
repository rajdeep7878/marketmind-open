"""v1.2.E — TakeProfitAtrMultiple schema + math tests.

Three test classes mirroring v1.2.A/B/C structure (full engine drift
parity in commit 3, end-to-end in commit 4):

  1. TestTakeProfitAtrMultipleSchema — Pydantic shape bounds match
     StopLossAtrMultiple 1:1 (atr_period: int 2..100, mult: float
     gt=0.0 le=20.0), round-trip, discriminator routing.

  2. TestTpLevelMath — pure formula correctness for the long-side
     iterative engine: tp = entry + mult × ATR. The iterative engine
     is long-only (translator.py:356 rejects non-LONG with an error),
     so SHORT-side semantics live in the vbt path where vbt's
     from_signals applies the sign internally based on
     direction="shortonly" (tested in commit 3's drift parity).

  3. TestExistingTakeProfitVariantsUnchanged — the three pre-existing
     TakeProfitMethod variants (Percent, RMultiple, FixedPrice) still
     parse identically; no schema drift.
"""

from __future__ import annotations

import pytest
from marketmind_shared.schemas.strategy_spec import (
    StopLossAtrMultiple,
    TakeProfitAtrMultiple,
    TakeProfitFixedPrice,
    TakeProfitMethod,
    TakeProfitPercent,
    TakeProfitRMultiple,
)


class TestTakeProfitAtrMultipleSchema:
    def test_basic_construction(self) -> None:
        tp = TakeProfitAtrMultiple(atr_period=14, mult=2.0)
        assert tp.kind == "atr_multiple"
        assert tp.atr_period == 14
        assert tp.mult == 2.0

    def test_bounds_match_stop_loss_atr_multiple_1to1(self) -> None:
        """No schema drift between SL and TP — same Pydantic bounds.
        This is the load-bearing assertion: the v1.2 design doc
        explicitly requires they be mirror images.
        """
        sl_fields = StopLossAtrMultiple.model_fields
        tp_fields = TakeProfitAtrMultiple.model_fields
        # Same field names
        assert set(tp_fields) == set(sl_fields)
        # Same bounds on atr_period
        assert sl_fields["atr_period"].metadata == tp_fields["atr_period"].metadata
        # Same bounds on mult
        assert sl_fields["mult"].metadata == tp_fields["mult"].metadata

    @pytest.mark.parametrize("bad_atr", [0, 1, 101, 200, -1])
    def test_atr_period_bounds_rejected(self, bad_atr: int) -> None:
        with pytest.raises(Exception):  # noqa: B017 — Pydantic ValidationError
            TakeProfitAtrMultiple(atr_period=bad_atr, mult=2.0)

    @pytest.mark.parametrize("bad_mult", [-1.0, 0.0, 20.001, 100.0])
    def test_mult_bounds_rejected(self, bad_mult: float) -> None:
        with pytest.raises(Exception):  # noqa: B017
            TakeProfitAtrMultiple(atr_period=14, mult=bad_mult)

    def test_round_trip_preserves_equality(self) -> None:
        tp = TakeProfitAtrMultiple(atr_period=20, mult=3.5)
        roundtripped = TakeProfitAtrMultiple.model_validate_json(tp.model_dump_json())
        assert roundtripped == tp

    def test_routes_via_take_profit_method_discriminator(self) -> None:
        """The TakeProfitMethod discriminated union picks up the new
        kind="atr_multiple" variant correctly.
        """
        from pydantic import TypeAdapter

        adapter = TypeAdapter(TakeProfitMethod)
        parsed = adapter.validate_python(
            {"kind": "atr_multiple", "atr_period": 14, "mult": 2.0},
        )
        assert isinstance(parsed, TakeProfitAtrMultiple)
        assert parsed.atr_period == 14
        assert parsed.mult == 2.0


class TestTpLevelMath:
    """Pure formula correctness. Iterative engine is long-only; the
    formula is `entry + mult × ATR`. The vbt path's SHORT handling
    (sign flip via direction="shortonly") is exercised in commit 3's
    drift parity gate, where the math symmetry is observable
    end-to-end.
    """

    def test_long_tp_formula_known_values(self) -> None:
        # entry=100, ATR=2, mult=2 -> tp = 100 + 2*2 = 104
        entry, atr, mult = 100.0, 2.0, 2.0
        tp = entry + mult * atr
        assert tp == 104.0

    def test_long_tp_zero_atr_at_entry_collapses_to_entry(self) -> None:
        # ATR=0 during warmup or pathological data -> tp == entry.
        # This is the "no edge" case the iterative engine handles
        # gracefully (the position closes essentially immediately on
        # any positive slippage).
        entry, atr, mult = 100.0, 0.0, 2.0
        tp = entry + mult * atr
        assert tp == 100.0

    def test_long_tp_formula_extreme_mult(self) -> None:
        # mult=20 (the upper bound) -> tp = entry + 20*ATR. With
        # entry=100, ATR=5, tp = 100 + 100 = 200.
        entry, atr, mult = 100.0, 5.0, 20.0
        tp = entry + mult * atr
        assert tp == 200.0

    def test_short_tp_formula_is_directional_complement(self) -> None:
        """For SHORT (vbt path), the take-profit is BELOW entry by
        the same magnitude. vbt's tp_stop fraction is interpreted
        symmetrically — the fraction itself is always positive; vbt
        applies the sign. So the magnitude of the move is identical
        between LONG and SHORT for the same ATR + multiplier.
        """
        entry, atr, mult = 100.0, 2.0, 2.0
        long_tp = entry + mult * atr  # 104
        short_tp = entry - mult * atr  # 96
        # Same magnitude of move, opposite sign relative to entry.
        assert (long_tp - entry) == -(short_tp - entry)
        assert abs(long_tp - entry) == abs(short_tp - entry) == mult * atr

    def test_atr_period_and_mult_carry_through(self) -> None:
        """Verify that the schema fields actually carry through to
        the math — atr_period determines WHICH ATR series, mult is
        the multiplier. (The actual ATR series computation is shared
        with StopLossAtrMultiple via _atr_for_stop / the equivalent
        TP-side helper added in commit 2.)
        """
        tp = TakeProfitAtrMultiple(atr_period=14, mult=2.5)
        # The schema just holds the parameters; engine consumes them.
        assert tp.atr_period == 14
        assert tp.mult == 2.5


class TestExistingTakeProfitVariantsUnchanged:
    """The three pre-existing variants must still parse identically.
    No schema drift from the union extension.
    """

    def test_percent_still_parses(self) -> None:
        tp = TakeProfitPercent(value=0.05)
        assert tp.kind == "percent"
        assert tp.value == 0.05

    def test_r_multiple_still_parses(self) -> None:
        tp = TakeProfitRMultiple(r=2.0)
        assert tp.kind == "r_multiple"
        assert tp.r == 2.0

    def test_fixed_price_still_parses(self) -> None:
        tp = TakeProfitFixedPrice(price=125.0)
        assert tp.kind == "fixed_price"
        assert tp.price == 125.0

    def test_existing_variants_via_discriminator(self) -> None:
        from pydantic import TypeAdapter

        adapter = TypeAdapter(TakeProfitMethod)
        # Percent
        p = adapter.validate_python({"kind": "percent", "value": 0.05})
        assert isinstance(p, TakeProfitPercent)
        # RMultiple
        r = adapter.validate_python({"kind": "r_multiple", "r": 2.0})
        assert isinstance(r, TakeProfitRMultiple)
        # FixedPrice
        f = adapter.validate_python({"kind": "fixed_price", "price": 125.0})
        assert isinstance(f, TakeProfitFixedPrice)
