"""Match author performance claims against our measured backtest result.

For every AuthorClaim attached to a strategy, we:

  1. Parse the author's value string into a float when possible
     (handles "%", "x" multipliers, commas, sign).
  2. Map the claim type to the matching field on BacktestMetrics.
  3. Compute a discrepancy ratio when both numbers are known.
  4. Emit a plain-English explanation that calls out the usual reasons
     measured numbers diverge from authors' numbers — multi-asset
     portfolios, in-sample optimisation, different data windows.

Single-instrument backtests only. If the author tested a multi-asset
portfolio, we state that as a known divergence cause rather than
attempting it ourselves.
"""

from __future__ import annotations

import re
from datetime import datetime

from marketmind_shared.schemas import (
    AuthorClaim,
    AuthorClaimComparison,
    AuthorClaimType,
    BacktestMetrics,
)

# ---- Parsing ---------------------------------------------------------------


# Matches the first signed number with optional decimal + commas.
# Captures: sign, integer part (with commas), fractional part.
_NUMBER_RE: re.Pattern[str] = re.compile(
    r"[-+]?\d{1,3}(?:,\d{3})*(?:\.\d+)?|[-+]?\.\d+|[-+]?\d+(?:\.\d+)?",
)

# Indicators in the surrounding text that change parse semantics.
_MULTI_ASSET_TOKENS: tuple[str, ...] = (
    "portfolio",
    "basket",
    "multi-asset",
    "multi asset",
    "diversified",
    "across",
    "spx",
    "s&p",
    "stocks",
    "etfs",
    "etf",
    "altcoins",
    "top 10",
    "top 20",
)


def _parse_number(raw: str) -> tuple[float | None, str]:
    """Pull the first number out of `raw` and apply a couple of suffix
    rules. Returns (value or None, label) where label is a short token
    describing what we extracted ("percent", "multiplier", "count",
    "sharpe", "raw") for diagnostic use.
    """
    s = raw.strip().lower()
    if not s:
        return None, ""
    match = _NUMBER_RE.search(s)
    if not match:
        return None, ""
    try:
        n = float(match.group().replace(",", ""))
    except ValueError:
        return None, ""

    # "%": value is a percentage; convert to fraction.
    if "%" in s:
        return n / 100.0, "percent"
    # "x" multiplier: "10x return" -> total return of 9.0 (i.e., +900%).
    # Only treat as multiplier when the 'x' is right next to the number,
    # not when it shows up elsewhere in the sentence.
    tail = s[match.end() : match.end() + 3].strip()
    if tail.startswith("x"):
        return n - 1.0, "multiplier"
    # Bare integer is most useful as a count when in trade_count context.
    return n, "raw"


# ---- Per-claim-type comparison --------------------------------------------


def _measured_for(claim_type: AuthorClaimType, m: BacktestMetrics) -> tuple[float | None, str]:
    """Return (measured_value, human label) for the metric corresponding
    to `claim_type`. None when we don't have a comparable measured
    field (currently OTHER).
    """
    if claim_type is AuthorClaimType.RETURN:
        return m.total_return_pct, "total return"
    if claim_type is AuthorClaimType.DRAWDOWN:
        return m.max_drawdown_pct, "max drawdown"
    if claim_type is AuthorClaimType.WIN_RATE:
        return m.win_rate, "win rate"
    if claim_type is AuthorClaimType.TRADE_COUNT:
        return float(m.num_trades), "trade count"
    if claim_type is AuthorClaimType.SHARPE:
        return m.sharpe_ratio, "Sharpe ratio"
    return None, ""


def _is_multi_asset(claim: AuthorClaim, spec_symbol: str) -> bool:
    """Heuristic — true when the author's claim looks like it was
    measured against more than one instrument, or against a different
    instrument than the one we backtested.
    """
    fields = " ".join(
        [
            (claim.instrument or "").lower(),
            claim.quote.lower(),
            (claim.period or "").lower(),
            claim.value.lower(),
        ],
    )
    if any(token in fields for token in _MULTI_ASSET_TOKENS):
        return True
    # If the author explicitly named a different instrument than ours,
    # treat it as divergent. We don't try to be clever about the
    # equivalence of "BTCUSD" / "BTC/USDT" / "BTC".
    if claim.instrument:
        spec_token = spec_symbol.split("/")[0].lower()
        if spec_token and spec_token not in claim.instrument.lower():
            return True
    return False


def _format_value(value: float, label: str) -> str:
    if label in ("total return", "max drawdown", "win rate"):
        return f"{value * 100:.2f}%"
    if label == "trade count":
        return f"{round(value)}"
    if label == "Sharpe ratio":
        return f"{value:.2f}"
    return f"{value:.4g}"


def _build_explanation(
    *,
    claim: AuthorClaim,
    claim_type: AuthorClaimType,
    parsed: float | None,
    measured: float | None,
    measured_label: str,
    multi_asset: bool,
    spec_symbol: str,
    period_supplied: bool,
) -> str:
    parts: list[str] = []

    if claim_type is AuthorClaimType.OTHER:
        return (
            f'The author claimed "{claim.value}" but this is not a category we '
            f"can match to a backtest metric. Recorded for context only."
        )

    if parsed is None:
        return (
            f'We could not extract a numeric value from "{claim.value}" to compare '
            f"against our measured {measured_label}. Recorded for context only."
        )

    if measured is None:
        return (
            f'The author claimed {measured_label} of "{claim.value}" but we did '
            f"not measure a corresponding value in this backtest run."
        )

    # Both sides numeric — compose a comparison sentence.
    measured_str = _format_value(measured, measured_label)
    author_str = _format_value(parsed, measured_label)
    diff = measured - parsed
    parts.append(
        f"Author claimed {measured_label} of {author_str}; we measured {measured_str}.",
    )

    if multi_asset:
        parts.append(
            f"The author's number appears to span multiple instruments or a "
            f"different instrument than the one we backtested ({spec_symbol}). "
            f"That alone can explain a large divergence — multi-asset portfolio "
            f"results are not directly comparable to a single-instrument "
            f"backtest.",
        )

    # Honest framing on direction of error.
    # For DRAWDOWN, a smaller (less negative magnitude) drawdown is better.
    is_dd = claim_type is AuthorClaimType.DRAWDOWN
    if claim_type is AuthorClaimType.TRADE_COUNT:
        if abs(diff) >= max(1.0, 0.2 * abs(parsed)):
            parts.append(
                "Trade count differs materially — the most common reason is a "
                "different time window or a slightly different entry rule than "
                "the author actually used.",
            )
    elif abs(parsed) > 1e-9 and abs(diff / parsed) > 0.5:
        if is_dd:
            if measured > parsed:
                parts.append(
                    "Our measured drawdown is much larger than the author's. "
                    "Common causes: the author may have used a different stop "
                    "rule, a less volatile sub-period, or selectively reported "
                    "favourable runs.",
                )
            else:
                parts.append(
                    "Our measured drawdown is much smaller than the author's, "
                    "which is unusual — double-check the timeframe and "
                    "instrument before treating this as confirmation.",
                )
        elif diff < 0:
            parts.append(
                "Our measured number is materially below the author's. "
                "The classic explanation is in-sample optimisation — strategies "
                "tuned on the same data they are reported on routinely "
                "underperform on any new window.",
            )
        else:
            parts.append(
                "Our measured number exceeds the author's. Be skeptical of "
                "this — check the period, fees, and slippage assumptions before "
                "trusting it.",
            )

    if not period_supplied and not multi_asset:
        parts.append(
            "The author did not specify a precise period; our window may differ from theirs.",
        )

    return " ".join(parts)


# ---- Public entry ---------------------------------------------------------


def compare_author_claims(
    claims: list[AuthorClaim],
    metrics: BacktestMetrics,
    *,
    spec_symbol: str,
    backtest_start: datetime | None = None,
    backtest_end: datetime | None = None,
) -> list[AuthorClaimComparison]:
    """Build one AuthorClaimComparison per author claim.

    `backtest_start` and `backtest_end` are accepted for forward
    compatibility with date-range comparisons; they are not used by the
    current heuristic but are checked for consistency by callers.
    """
    # Silence the unused-arg warning while keeping the API forward
    # compatible. Phase 4 may use these to flag window mismatches.
    _ = backtest_start
    _ = backtest_end

    out: list[AuthorClaimComparison] = []
    for claim in claims:
        measured_value, measured_label = _measured_for(claim.claim_type, metrics)
        parsed_value, _parse_label = _parse_number(claim.value)
        multi_asset = _is_multi_asset(claim, spec_symbol)

        # Discrepancy ratio + difference are only meaningful when both
        # sides are numeric and the author's value is non-zero. For
        # trade_count zero is plausible but the ratio is meaningless;
        # we still report it (0.0 → 0/N == 0.0 by convention) but flag
        # via the explanation.
        difference: float | None = None
        discrepancy_ratio: float | None = None
        if parsed_value is not None and measured_value is not None:
            difference = measured_value - parsed_value
            if abs(parsed_value) > 1e-9:
                discrepancy_ratio = difference / parsed_value

        explanation = _build_explanation(
            claim=claim,
            claim_type=claim.claim_type,
            parsed=parsed_value,
            measured=measured_value,
            measured_label=measured_label,
            multi_asset=multi_asset,
            spec_symbol=spec_symbol,
            period_supplied=bool(claim.period),
        )

        out.append(
            AuthorClaimComparison(
                claim_type=claim.claim_type,
                author_value_raw=claim.value,
                author_value_parsed=parsed_value,
                measured_value=measured_value,
                measured_label=measured_label,
                difference=difference,
                discrepancy_ratio=discrepancy_ratio,
                explanation=explanation,
            ),
        )
    return out


__all__ = ["compare_author_claims"]
