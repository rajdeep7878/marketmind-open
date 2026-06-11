/**
 * Plain-English interpretation of the equity curve. Pure function —
 * no React, no I/O. Drives the one-sentence line under the chart.
 */

export interface EquityInterpretationInputs {
  currentEquity: number;
  startingCash: number;
  daysElapsed: number;
}

function fmtGbpAbs(n: number): string {
  return `£${Math.abs(n).toLocaleString("en-GB", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/**
 * Three-branch interpretation:
 *
 *   - less than 7 days of data → "too early"
 *   - profit                   → "+£X over Y days, ~Z% / month"
 *   - loss                     → "down £X over Y days, normal early"
 *
 * The monthly-rate calculation linearly extrapolates the per-day
 * return to 30 days. That's deliberately naive (matches the
 * audience the dashboard speaks to — operators, not quants) and
 * fine for a copy line; quant readers get the numbers above.
 */
export function interpretEquityPerformance(inputs: EquityInterpretationInputs): string {
  const { currentEquity, startingCash, daysElapsed } = inputs;
  if (daysElapsed < 7) {
    const days = Math.max(0, Math.round(daysElapsed));
    return `Your bot has been running for ${days} ${days === 1 ? "day" : "days"}. It's too early to draw any conclusions.`;
  }
  const delta = currentEquity - startingCash;
  if (delta >= 0) {
    const monthlyPct = startingCash > 0 ? (delta / startingCash / daysElapsed) * 30 * 100 : 0;
    return `Your bot has made ${fmtGbpAbs(delta)} over ${Math.round(daysElapsed)} days. That works out to about ${monthlyPct.toFixed(1)}% per month.`;
  }
  return `Your bot is down ${fmtGbpAbs(delta)} over ${Math.round(daysElapsed)} days. This is normal in the early weeks — strategies need time to play out.`;
}

/**
 * "Today" delta for the headline-balance secondary line. Given the
 * latest equity and the first equity-curve point that landed today
 * (or null if no snapshot from today exists yet), returns:
 *
 *   { amount, pct, tone }   where tone ∈ "positive" | "negative" | "neutral"
 *
 * If no comparable snapshot is available, returns null so the
 * caller can render "—" rather than fabricating a delta.
 */
export interface TodayDelta {
  amount: number;
  pct: number;
  tone: "positive" | "negative" | "neutral";
}

export function computeTodayDelta(
  currentEquity: number,
  startOfDayEquity: number | null,
): TodayDelta | null {
  if (startOfDayEquity === null || startOfDayEquity <= 0) return null;
  const amount = currentEquity - startOfDayEquity;
  const pct = (amount / startOfDayEquity) * 100;
  const tone: TodayDelta["tone"] = amount > 0 ? "positive" : amount < 0 ? "negative" : "neutral";
  return { amount, pct, tone };
}
