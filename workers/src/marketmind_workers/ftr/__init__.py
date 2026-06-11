"""FTR — Frequent-Trading Research module (Phase D, paper-only).

A research branch targeting a realized frequency of roughly 1-5 trades/day
as an OUTCOME of honest cost-gated calibration, never as a forced target.
Strategies pass through the validation gauntlet per (strategy x venue cost
profile); a verdict of REJECTED is a successful outcome.

Hard invariants (see docs/INTEGRATION_PLAN.md):
- Paper only. The sole Broker implementation is PaperBroker; ExecutionMode
  has a single member, PAPER.
- No LLM anywhere in the trading decision path.
- Deterministic: fixed seeds, single-threaded XGBoost hist; same inputs +
  same config => byte-identical decision logs and equity curves.
- All timestamps UTC tz-aware; Decimal at the accounting boundary.
- Costs are explicit per-venue-profile config, never hardcoded, never zero.
- UK retail compliance: spot-only, long/flat-only execution path; any
  non-spot crypto instrument is rejected unless research_simulation_only,
  and even then is never routed to the paper trader.
"""
