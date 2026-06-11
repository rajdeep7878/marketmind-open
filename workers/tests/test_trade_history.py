"""Tests for TradeHistory and SignalHistory — the outcome state the A.3b
Tier-3 iterative simulator maintains. `prior_trade` consults TradeHistory
(completed trades); `prior_signal` consults SignalHistory (every
evaluated signal, fired or skipped, with skipped ones scored by a
phantom outcome). Both are pure logic — no pandas, no market data.
"""

from __future__ import annotations

import pytest
from marketmind_workers.backtest.trade_history import (
    CompletedTrade,
    SignalHistory,
    TradeHistory,
    TradeOutcome,
    classify_outcome,
)


def _trade(trade_id: int, outcome: TradeOutcome, return_pct: float = 0.0) -> CompletedTrade:
    return CompletedTrade(
        trade_id=trade_id,
        entry_index=trade_id * 10,
        exit_index=trade_id * 10 + 5,
        entry_price=100.0,
        exit_price=100.0 * (1.0 + return_pct),
        pnl=return_pct * 100.0,
        return_pct=return_pct,
        outcome=outcome,
    )


# ---- classify_outcome ------------------------------------------------------


def test_classify_outcome_buckets() -> None:
    assert classify_outcome(0.05) is TradeOutcome.WIN
    assert classify_outcome(-0.05) is TradeOutcome.LOSS
    assert classify_outcome(0.0) is TradeOutcome.BREAKEVEN


def test_classify_outcome_breakeven_eps_absorbs_float_dust() -> None:
    # Sub-epsilon returns are breakeven, not a 1e-12 "win".
    assert classify_outcome(1e-12) is TradeOutcome.BREAKEVEN
    assert classify_outcome(-1e-12) is TradeOutcome.BREAKEVEN
    # Just past the boundary they classify normally.
    assert classify_outcome(1e-6) is TradeOutcome.WIN
    assert classify_outcome(-1e-6) is TradeOutcome.LOSS


# ---- basics ----------------------------------------------------------------


def test_empty_history() -> None:
    hist = TradeHistory()
    assert hist.count == 0
    assert hist.last_outcome() is None
    assert hist.trailing_run(TradeOutcome.WIN) == 0


def test_record_and_count() -> None:
    hist = TradeHistory()
    hist.record(_trade(1, TradeOutcome.WIN, 0.1))
    hist.record(_trade(2, TradeOutcome.LOSS, -0.1))
    assert hist.count == 2
    assert hist.last_outcome() is TradeOutcome.LOSS


def test_trailing_run_counts_consecutive_from_most_recent() -> None:
    hist = TradeHistory()
    for tid, outcome in enumerate(
        [TradeOutcome.LOSS, TradeOutcome.WIN, TradeOutcome.WIN, TradeOutcome.WIN],
    ):
        hist.record(_trade(tid, outcome))
    assert hist.trailing_run(TradeOutcome.WIN) == 3
    assert hist.trailing_run(TradeOutcome.LOSS) == 0


# ---- evaluate_predicate — the prior_trade gating logic ---------------------


def test_predicate_empty_history_is_always_false() -> None:
    hist = TradeHistory()
    for predicate in ("last_won", "last_lost", "consecutive_wins_at_least",
                      "consecutive_losses_at_least"):
        assert hist.evaluate_predicate(predicate, n=1) is False


def test_predicate_last_won_and_last_lost() -> None:
    hist = TradeHistory()
    hist.record(_trade(1, TradeOutcome.WIN, 0.1))
    assert hist.evaluate_predicate("last_won", n=1) is True
    assert hist.evaluate_predicate("last_lost", n=1) is False
    hist.record(_trade(2, TradeOutcome.LOSS, -0.1))
    assert hist.evaluate_predicate("last_won", n=1) is False
    assert hist.evaluate_predicate("last_lost", n=1) is True


def test_predicate_consecutive_runs_respect_n() -> None:
    hist = TradeHistory()
    hist.record(_trade(1, TradeOutcome.LOSS))
    hist.record(_trade(2, TradeOutcome.LOSS))
    assert hist.evaluate_predicate("consecutive_losses_at_least", n=2) is True
    assert hist.evaluate_predicate("consecutive_losses_at_least", n=3) is False
    assert hist.evaluate_predicate("consecutive_wins_at_least", n=1) is False


def test_breakeven_trade_ends_a_winning_run() -> None:
    # A scratch trade is neither — it must reset the run, so a
    # "skip after a winner" rule does not fire after a breakeven.
    hist = TradeHistory()
    hist.record(_trade(1, TradeOutcome.WIN, 0.1))
    hist.record(_trade(2, TradeOutcome.BREAKEVEN, 0.0))
    assert hist.last_outcome() is TradeOutcome.BREAKEVEN
    assert hist.evaluate_predicate("last_won", n=1) is False
    assert hist.trailing_run(TradeOutcome.WIN) == 0


def test_unknown_predicate_raises() -> None:
    with pytest.raises(ValueError, match="unknown prior_trade predicate"):
        TradeHistory().evaluate_predicate("usually_wins", n=1)


# ---- v1.2.B: bars_since_last_at_least predicate ----------------------------


def test_bars_since_empty_history_is_false() -> None:
    # Same convention as last_won / last_lost: no prior trade -> False.
    # The predicate "at least N bars since last trade" is undefined when
    # there is no last trade; we resolve it to False so a NOT-gated
    # entry can fire (no throttle to wait out).
    hist = TradeHistory()
    assert (
        hist.evaluate_predicate(
            "bars_since_last_at_least", n=24, current_bar=100,
        )
        is False
    )


def test_bars_since_uses_last_trade_exit_index() -> None:
    """exit_index is 15 (trade_id=1 by _trade helper formula); the
    elapsed-bar comparison uses (current_bar - exit_index).
    """
    hist = TradeHistory()
    hist.record(_trade(1, TradeOutcome.WIN, 0.1))
    # exit_index = 1*10 + 5 = 15
    last = hist.trades[-1]
    assert last.exit_index == 15

    # current_bar = 38, n=24 -> elapsed = 38-15 = 23, NOT >= 24 -> False
    assert (
        hist.evaluate_predicate(
            "bars_since_last_at_least", n=24, current_bar=38,
        )
        is False
    )
    # current_bar = 39, n=24 -> elapsed = 39-15 = 24, IS >= 24 -> True
    assert (
        hist.evaluate_predicate(
            "bars_since_last_at_least", n=24, current_bar=39,
        )
        is True
    )
    # current_bar = 40, n=24 -> elapsed = 25, still True
    assert (
        hist.evaluate_predicate(
            "bars_since_last_at_least", n=24, current_bar=40,
        )
        is True
    )


def test_bars_since_only_considers_most_recent_trade() -> None:
    """Multiple trades — only the last trade's exit_index matters.
    The predicate is "bars since LAST trade", not "bars since any
    trade", so an older trade's exit doesn't move the goalposts."""
    hist = TradeHistory()
    hist.record(_trade(1, TradeOutcome.WIN))      # exit_index = 15
    hist.record(_trade(5, TradeOutcome.LOSS))     # exit_index = 55
    # current_bar = 70, n=24 -> elapsed = 70-55 = 15, NOT >= 24 -> False
    # (despite older trade #1's exit being 55 bars ago)
    assert (
        hist.evaluate_predicate(
            "bars_since_last_at_least", n=24, current_bar=70,
        )
        is False
    )
    # current_bar = 79, n=24 -> elapsed = 79-55 = 24 -> True
    assert (
        hist.evaluate_predicate(
            "bars_since_last_at_least", n=24, current_bar=79,
        )
        is True
    )


def test_bars_since_requires_current_bar() -> None:
    """The new predicate is the only one that requires current_bar.
    Forgetting to pass it raises ValueError (defensive guard, not
    silent False)."""
    hist = TradeHistory()
    hist.record(_trade(1, TradeOutcome.WIN))
    with pytest.raises(ValueError, match="current_bar is required"):
        hist.evaluate_predicate("bars_since_last_at_least", n=24)


def test_bars_since_zero_elapsed_edge_case() -> None:
    """current_bar == exit_index: elapsed = 0. For n=1, 0 < 1 -> False.
    For n=0 the schema rejects (ge=1)."""
    hist = TradeHistory()
    hist.record(_trade(1, TradeOutcome.WIN))  # exit_index = 15
    assert (
        hist.evaluate_predicate(
            "bars_since_last_at_least", n=1, current_bar=15,
        )
        is False
    )


def test_bars_since_large_n_works_post_widening() -> None:
    """v1.2.B widens the n upper bound from 100 to 100_000. Confirm
    that n=2880 (one month at 15m bars) computes correctly without
    being rejected by the schema or producing arithmetic surprises."""
    hist = TradeHistory()
    hist.record(_trade(1, TradeOutcome.WIN))  # exit_index = 15
    # 2895 - 15 = 2880, exactly the threshold -> True
    assert (
        hist.evaluate_predicate(
            "bars_since_last_at_least", n=2880, current_bar=2895,
        )
        is True
    )
    # 2894 - 15 = 2879, just under -> False
    assert (
        hist.evaluate_predicate(
            "bars_since_last_at_least", n=2880, current_bar=2894,
        )
        is False
    )


def test_existing_predicates_ignore_current_bar() -> None:
    """The keyword-only signature widening (commit 2) must not affect
    the four existing predicates. Passing current_bar to any of them
    is a silent no-op — they still consult outcome state only."""
    hist = TradeHistory()
    hist.record(_trade(1, TradeOutcome.WIN, 0.1))
    # Both forms produce the same answer.
    for predicate in (
        "last_won", "last_lost",
        "consecutive_wins_at_least", "consecutive_losses_at_least",
    ):
        without = hist.evaluate_predicate(predicate, n=1)
        with_bar = hist.evaluate_predicate(predicate, n=1, current_bar=999)
        assert without == with_bar, (
            f"predicate={predicate} changed answer when current_bar was passed: "
            f"{without} -> {with_bar}"
        )


# ---- the skip-after-winner contract ----------------------------------------


def test_skip_after_winner_logic() -> None:
    """The deterministic core of Turtle System-1 skip-after-winner: a
    `not(prior_trade last_won)` entry gate is OPEN before any trade and
    after a loss, and CLOSED immediately after a winning trade.

    The full backtest-integration version of this — synthetic prices
    that produce two consecutive winning breakouts, asserting the second
    is skipped — belongs with the iterative simulator (it needs the
    simulator to run the scenario). This test pins the gating logic the
    simulator will call into.
    """
    hist = TradeHistory()
    # Before any trade: gate open (entry allowed).
    assert hist.evaluate_predicate("last_won", n=1) is False

    # A winning trade closes -> gate shut: the next breakout is skipped.
    hist.record(_trade(1, TradeOutcome.WIN, 0.20))
    assert hist.evaluate_predicate("last_won", n=1) is True

    # A losing trade closes -> gate open again.
    hist.record(_trade(2, TradeOutcome.LOSS, -0.08))
    assert hist.evaluate_predicate("last_won", n=1) is False


# ---- SignalHistory ---------------------------------------------------------


def test_signal_history_empty_predicates_are_false() -> None:
    hist = SignalHistory()
    assert hist.count == 0
    for predicate in ("last_would_have_won", "last_would_have_lost", "last_fired"):
        assert hist.evaluate_predicate(predicate, current_bar=100) is False


def test_signal_history_record_skipped_classifies_outcome() -> None:
    hist = SignalHistory()
    hist.record_skipped(signal_bar=5, return_pct=0.1, resolved_bar=10)
    assert hist.count == 1
    assert hist.evaluate_predicate("last_would_have_won", current_bar=10) is True
    assert hist.evaluate_predicate("last_would_have_lost", current_bar=10) is False
    # A skipped signal did NOT fire.
    assert hist.evaluate_predicate("last_fired", current_bar=10) is False


def test_signal_history_record_fired_then_resolve() -> None:
    hist = SignalHistory()
    hist.record_fired(signal_bar=5)
    # Pending — not yet resolved, so it is invisible to prior_signal.
    assert hist.evaluate_predicate("last_fired", current_bar=100) is False
    hist.resolve_last_pending(return_pct=-0.05, resolved_bar=12)
    assert hist.evaluate_predicate("last_fired", current_bar=12) is True
    assert hist.evaluate_predicate("last_would_have_lost", current_bar=12) is True
    assert hist.evaluate_predicate("last_would_have_won", current_bar=12) is False


def test_signal_history_resolved_bar_gates_look_ahead() -> None:
    # A signal resolved at bar 20 is invisible before bar 20 and visible
    # from bar 20 on — the no-look-ahead guarantee for phantom outcomes
    # computed eagerly from future bars.
    hist = SignalHistory()
    hist.record_skipped(signal_bar=5, return_pct=0.2, resolved_bar=20)
    assert hist.evaluate_predicate("last_would_have_won", current_bar=19) is False
    assert hist.evaluate_predicate("last_would_have_won", current_bar=20) is True
    assert hist.evaluate_predicate("last_would_have_won", current_bar=21) is True


def test_signal_history_skips_unresolved_to_find_most_recent_resolved() -> None:
    # signal@5 resolved@10 (a loss); signal@8 fired but still pending.
    # At bar 15 the most recent RESOLVED signal is @5 — the pending @8 is
    # skipped over, never treated as look-ahead.
    hist = SignalHistory()
    hist.record_skipped(signal_bar=5, return_pct=-0.1, resolved_bar=10)
    hist.record_fired(signal_bar=8)
    assert hist.evaluate_predicate("last_would_have_lost", current_bar=15) is True
    assert hist.evaluate_predicate("last_fired", current_bar=15) is False
    # Once @8 resolves it becomes the most recent.
    hist.resolve_last_pending(return_pct=0.3, resolved_bar=14)
    assert hist.evaluate_predicate("last_fired", current_bar=15) is True
    assert hist.evaluate_predicate("last_would_have_won", current_bar=15) is True


def test_signal_history_most_recent_resolved_is_by_signal_bar() -> None:
    # Two resolved signals; the more recent one (by signal bar) wins,
    # regardless of record/resolve order.
    hist = SignalHistory()
    hist.record_skipped(signal_bar=5, return_pct=0.1, resolved_bar=9)
    hist.record_skipped(signal_bar=12, return_pct=-0.1, resolved_bar=15)
    assert hist.evaluate_predicate("last_would_have_lost", current_bar=20) is True
    assert hist.evaluate_predicate("last_would_have_won", current_bar=20) is False


def test_signal_history_resolve_with_no_pending_raises() -> None:
    hist = SignalHistory()
    with pytest.raises(ValueError, match="no pending signal"):
        hist.resolve_last_pending(return_pct=0.1, resolved_bar=5)


def test_signal_history_unknown_predicate_raises() -> None:
    hist = SignalHistory()
    hist.record_skipped(signal_bar=1, return_pct=0.1, resolved_bar=2)
    # `last_won` is a prior_trade predicate, not a prior_signal one.
    with pytest.raises(ValueError, match="unknown prior_signal predicate"):
        hist.evaluate_predicate("last_won", current_bar=5)
