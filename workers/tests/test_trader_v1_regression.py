"""A.5a v1 regression gate — the five hand-coded v1 templates must produce
identical signals before and after A.5a.

A.5a added `TemplateName.SPEC`, the `SpecTemplate`, migration 0012, and
the seed-script v2 routing — none of which touch the v1 template files.
This gate builds each v1 template via `build_template` and evaluates one
cycle on the frozen BTC/USDT 4h dataset (and on a mid-dataset window
where the breakout template fires a real BUY), asserting the
`SignalEvaluation` is unchanged. A drift here means A.5a — or a later
change — altered v1 behaviour: a stop-and-report regression.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from marketmind_shared.schemas.trader import TemplateName
from marketmind_workers.trader.templates import build_template

_PARQUET = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "market" / "btc_usdt_4h.parquet"
)

# (template, window_end) -> (kind, proposed_entry_price, proposed_stop_price).
# `window_end == 0` means the full dataset; otherwise df.iloc[:window_end].
# Captured from the v1 templates at default params; their source files are
# git-untouched by A.5a, so these values must not move.
_REFERENCE: dict[tuple[str, int], tuple[str, str, str]] = {
    ("ma_trend", 9000): ("HOLD", "46060.74", "0"),
    ("breakout", 9000): ("BUY", "46060.74", "45019.36281020"),
    ("rsi_mean_reversion", 9000): ("HOLD", "46060.74", "0"),
    ("bb_mean_reversion", 9000): ("HOLD", "46060.74", "0"),
    ("vcb", 9000): ("HOLD", "46060.74", "0"),
    ("ma_trend", 0): ("HOLD", "76834.36", "0"),
    ("breakout", 0): ("HOLD", "76834.36", "0"),
    ("rsi_mean_reversion", 0): ("HOLD", "76834.36", "0"),
    ("bb_mean_reversion", 0): ("HOLD", "76834.36", "0"),
    ("vcb", 0): ("HOLD", "76834.36", "0"),
}


@pytest.mark.parametrize(("template", "window_end"), sorted(_REFERENCE.keys()))
def test_v1_template_signal_is_unchanged(template: str, window_end: int) -> None:
    df = pd.read_parquet(_PARQUET)
    window = df if window_end == 0 else df.iloc[:window_end]
    ev = build_template(TemplateName(template), {}).evaluate(window, None)
    actual = (ev.kind.value, str(ev.proposed_entry_price), str(ev.proposed_stop_price))
    expected = _REFERENCE[(template, window_end)]
    assert actual == expected, (
        f"v1 template '{template}' (window_end={window_end}) drifted from the "
        f"pre-A.5a baseline.\n  expected: {expected}\n  actual:   {actual}\n"
        "The v1 template source files are untouched by A.5a — a drift here is "
        "a non-negotiable stop-and-report regression."
    )
