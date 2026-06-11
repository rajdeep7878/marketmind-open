"""Outcome history for the Tier-3 iterative backtest path.

Two parallel ledgers, both consulted by the iterative simulator:

  * `TradeHistory` — the `prior_trade` condition gates an entry on the
    win/loss outcome of earlier *completed trades*.
  * `SignalHistory` — the `prior_signal` condition gates on earlier
    evaluated *entry signals*, fired or skipped. A skipped signal is
    scored by a *phantom outcome* (the trade it would have produced),
    which lets a skip-after-winner rule keep tracking each new breakout
    instead of latching shut. See
    docs/design/v2-phase-a-stateful-conditions.md section 4.7.

The vectorised engine cannot evaluate either — trade/signal outcomes do
not exist until the backtest has run. This module is pure data — no
pandas, no market data, no I/O — so the gating logic is trivial to
unit-test in isolation, separate from the (much larger) iterative
simulator that drives it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class TradeOutcome(StrEnum):
    """How a completed trade finished, for prior_trade gating.

    BREAKEVEN is a distinct bucket — not folded into WIN or LOSS — so a
    flat trade extends neither a winning nor a losing run; it ends both.
    That matches how a trader reads "after a winning trade" / "after a
    losing streak": a scratch trade is neither.
    """

    WIN = "win"
    LOSS = "loss"
    BREAKEVEN = "breakeven"


# Returns whose magnitude is below this count as breakeven, not as a
# 0.0000001%-"win" produced by float dust in the fee/slippage arithmetic.
_BREAKEVEN_EPS: float = 1e-9


def classify_outcome(return_pct: float, *, breakeven_eps: float = _BREAKEVEN_EPS) -> TradeOutcome:
    """Bucket a trade's net (post-cost) return into win / loss / breakeven."""
    if return_pct > breakeven_eps:
        return TradeOutcome.WIN
    if return_pct < -breakeven_eps:
        return TradeOutcome.LOSS
    return TradeOutcome.BREAKEVEN


@dataclass(frozen=True)
class CompletedTrade:
    """One closed trade, as the iterative simulator records it.

    `entry_index` / `exit_index` are bar positions (not timestamps) so
    the simulator and prior_trade evaluation stay index-arithmetic only.
    `pnl` and `return_pct` are net of fees and slippage.
    """

    trade_id: int
    entry_index: int
    exit_index: int
    entry_price: float
    exit_price: float
    pnl: float
    return_pct: float
    outcome: TradeOutcome


@dataclass
class TradeHistory:
    """Ordered history of completed trades plus the prior_trade predicate
    evaluator. Mutated in place by the iterative simulator as trades close.
    """

    trades: list[CompletedTrade] = field(default_factory=list)

    def record(self, trade: CompletedTrade) -> None:
        """Append a freshly-closed trade. Trades are recorded in close order."""
        self.trades.append(trade)

    @property
    def count(self) -> int:
        return len(self.trades)

    def last_outcome(self) -> TradeOutcome | None:
        """Outcome of the most recently closed trade, or None if there is none."""
        return self.trades[-1].outcome if self.trades else None

    def trailing_run(self, outcome: TradeOutcome) -> int:
        """Length of the trailing run of `outcome` — consecutive trades of
        that outcome counting back from the most recent. 0 if the last
        trade is not `outcome` (or there are no trades).
        """
        run = 0
        for trade in reversed(self.trades):
            if trade.outcome is outcome:
                run += 1
            else:
                break
        return run

    def evaluate_predicate(
        self,
        predicate: str,
        n: int,
        *,
        current_bar: int | None = None,
    ) -> bool:
        """Evaluate a `PriorTradeCondition` predicate against the history.

        `predicate` is one of the five `PriorTradeCondition.predicate`
        literals. With no completed trades yet every predicate is False —
        there is no prior trade to gate on, so a prior_trade condition
        simply does not fire until at least one trade closes.

        Parameters
        ----------
        predicate
            The literal to evaluate. last_won / last_lost ignore both
            `n` and `current_bar`. consecutive_* use `n` as the required
            run length; ignore `current_bar`.
            bars_since_last_at_least uses `n` as the required minimum
            bar count since the last trade's exit_index and REQUIRES
            `current_bar` to be supplied.
        n
            Run length for consecutive_*, or minimum bar count for
            bars_since_last_at_least. Schema bounds: 1..100_000.
        current_bar
            Keyword-only. Required for `bars_since_last_at_least` —
            the index of the bar at which the predicate is being
            evaluated. Ignored by every other predicate. Default
            `None` keeps the call sites for the original 4 predicates
            byte-identical (v1.2.B (2026-05-24) signature widening).

        v1.2.B: `bars_since_last_at_least` is the new branch.
        """
        if predicate == "last_won":
            return self.last_outcome() is TradeOutcome.WIN
        if predicate == "last_lost":
            return self.last_outcome() is TradeOutcome.LOSS
        if predicate == "consecutive_wins_at_least":
            return self.trailing_run(TradeOutcome.WIN) >= n
        if predicate == "consecutive_losses_at_least":
            return self.trailing_run(TradeOutcome.LOSS) >= n
        if predicate == "bars_since_last_at_least":
            # Time-based throttle (v1.2.B). No prior trade → False, same
            # convention as last_won / last_lost — a prior_trade
            # condition simply doesn't fire until at least one trade
            # has closed.
            if not self.trades:
                return False
            if current_bar is None:
                raise ValueError(
                    "current_bar is required for the "
                    "'bars_since_last_at_least' predicate but was not "
                    "supplied",
                )
            last_exit = self.trades[-1].exit_index
            return (current_bar - last_exit) >= n
        raise ValueError(f"unknown prior_trade predicate: {predicate!r}")


# ---- signal history (prior_signal) -----------------------------------------


@dataclass
class SignalRecord:
    """One evaluated entry signal, as the iterative simulator records it.

    A signal is a bar where the entry's raw (non-gate) condition fired
    while the strategy was flat. `fired` records whether a gate let it
    become a real trade or skipped it. `outcome` / `return_pct` are the
    signal's result — the real trade's if it fired, a simulated *phantom*
    trade's if it was skipped — and stay None until that (real or
    phantom) trade has closed. `resolved_bar` is the bar position the
    outcome became known; a `prior_signal` condition only ever consults
    signals resolved by the current bar, so a phantom computed from
    later bars introduces no look-ahead.
    """

    signal_bar: int
    fired: bool
    return_pct: float | None = None
    outcome: TradeOutcome | None = None
    resolved_bar: int | None = None


@dataclass
class SignalHistory:
    """Ordered history of evaluated entry signals plus the `prior_signal`
    predicate evaluator. Records are appended in signal-bar order and
    mutated in place by the iterative simulator as outcomes resolve.
    """

    signals: list[SignalRecord] = field(default_factory=list)

    def record_skipped(self, signal_bar: int, return_pct: float, resolved_bar: int) -> None:
        """Record a signal a gate skipped, scored by its phantom outcome.

        `return_pct` is the net (post-cost) return the would-have-been
        trade produced; `resolved_bar` is the bar that trade would have
        closed on.
        """
        self.signals.append(
            SignalRecord(
                signal_bar=signal_bar,
                fired=False,
                return_pct=return_pct,
                outcome=classify_outcome(return_pct),
                resolved_bar=resolved_bar,
            ),
        )

    def record_fired(self, signal_bar: int) -> None:
        """Record a signal that fired into a real trade. Its outcome is
        pending — `resolve_last_pending` fills it when the trade closes.
        """
        self.signals.append(SignalRecord(signal_bar=signal_bar, fired=True))

    def record_skipped_pending(self, signal_bar: int) -> None:
        """Record a gate-skipped signal whose phantom trade has not yet
        resolved — the A.6 live path, where the phantom is simulated one
        bar per cycle (design doc §6C.3). `resolve` fills the outcome when
        the phantom trade closes. (The backtest's `record_skipped` instead
        stores an already-resolved phantom — it can peek forward.)
        """
        self.signals.append(SignalRecord(signal_bar=signal_bar, fired=False))

    def resolve(self, signal_bar: int, return_pct: float, resolved_bar: int) -> None:
        """Fill the pending (real or phantom) signal recorded at `signal_bar`
        with its trade's result. Unlike `resolve_last_pending`, this targets
        a specific bar — the A.6 live path can have several pending phantoms
        at once, so "most recent pending" is ambiguous.
        """
        for rec in self.signals:
            if rec.signal_bar == signal_bar and rec.outcome is None:
                rec.return_pct = return_pct
                rec.outcome = classify_outcome(return_pct)
                rec.resolved_bar = resolved_bar
                return
        raise ValueError(f"resolve called with no pending signal at bar {signal_bar}")

    def resolve_last_pending(self, return_pct: float, resolved_bar: int) -> None:
        """Fill the most recent pending (fired, unresolved) signal with its
        real trade's result. The single-position simulator never has more
        than one pending fired signal at a time, so "most recent pending"
        is unambiguous.
        """
        for rec in reversed(self.signals):
            if rec.outcome is None:
                rec.return_pct = return_pct
                rec.outcome = classify_outcome(return_pct)
                rec.resolved_bar = resolved_bar
                return
        raise ValueError("resolve_last_pending called with no pending signal")

    @property
    def count(self) -> int:
        return len(self.signals)

    def _most_recent_resolved(self, current_bar: int) -> SignalRecord | None:
        """The latest-signal-bar record whose outcome is known by `current_bar`.

        Records are signal-bar ordered, so a reverse scan finds the most
        recent. Pending signals, and signals whose (real or phantom) trade
        has not closed by `current_bar`, are skipped — that is what keeps
        `prior_signal` free of look-ahead.
        """
        for rec in reversed(self.signals):
            if rec.resolved_bar is not None and rec.resolved_bar <= current_bar:
                return rec
        return None

    def evaluate_predicate(self, predicate: str, current_bar: int) -> bool:
        """Evaluate a `PriorSignalCondition` predicate at `current_bar`.

        `predicate` is one of the three `PriorSignalCondition.predicate`
        literals. With no signal yet resolved every predicate is False —
        there is no prior signal to gate on, exactly like `prior_trade`
        on an empty history.
        """
        rec = self._most_recent_resolved(current_bar)
        if rec is None:
            return False
        if predicate == "last_would_have_won":
            return rec.outcome is TradeOutcome.WIN
        if predicate == "last_would_have_lost":
            return rec.outcome is TradeOutcome.LOSS
        if predicate == "last_fired":
            return rec.fired
        raise ValueError(f"unknown prior_signal predicate: {predicate!r}")


__all__ = [
    "CompletedTrade",
    "SignalHistory",
    "SignalRecord",
    "TradeHistory",
    "TradeOutcome",
    "classify_outcome",
]
