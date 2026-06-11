"""Feature metadata + snapshot hashing (mandate Stage 2).

``feature_meta.json`` is written next to every persisted dataset:
feature-config hash, code git SHA, column list, dtypes, fold boundaries,
purge/embargo parameters. Feature snapshots used in live paper decisions are
hashed with ``snapshot_hash`` and the hash stored on each DecisionRecord, so
any decision can be traced to the exact feature values that produced it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd


def current_git_sha() -> str:
    try:
        out = subprocess.run(  # read-only provenance lookup
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return out.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError):
        return "unknown"


def snapshot_hash(row: pd.Series) -> str:
    """Deterministic hash of one feature snapshot (a single feature row)."""
    payload = json.dumps(
        {str(k): (None if pd.isna(v) else round(float(v), 12)) for k, v in row.items()},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def write_feature_meta(
    path: Path,
    *,
    feature_config_hash: str,
    columns: list[str],
    dtypes: dict[str, str],
    fold_boundaries: list[dict[str, str]],
    purge_bars: int,
    embargo_hours: int,
    extra: dict[str, Any] | None = None,
) -> Path:
    meta = {
        "feature_config_hash": feature_config_hash,
        "git_sha": current_git_sha(),
        "columns": columns,
        "dtypes": dtypes,
        "fold_boundaries": fold_boundaries,
        "purge_bars": purge_bars,
        "embargo_hours": embargo_hours,
        **(extra or {}),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return path
