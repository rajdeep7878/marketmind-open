"""Trader admin HTTP routes (Step 11).

Three POST endpoints, all gated by the same HTTP Basic auth as
`/admin/*` (reuses `AdminUserDep` from `routes.admin`):

  - POST /trader/strategies/{id}/enable
  - POST /trader/strategies/{id}/disable
  - POST /trader/strategies/{id}/approve_paper

`approve_paper` ALSO validates the version's `backtest_metrics`
JSONB shape before flipping the flag — the same shape the drift
analyzer (Step 9) reads. The validation lives here AS WELL AS in
drift.py because the API cannot import worker code (Phase 0
architecture). A future refactor could move
`_extract_backtest_metrics`'s shape check to
`marketmind_shared.schemas.trader` and dedupe; for v1 the
duplication is bounded (one dict-key check).

No `approve_live` endpoint — `trader_strategy_versions.approved_for_live`
is pinned to `FALSE` by the immutability trigger (migration 0006).
v1 is paper-only; live execution would require a new schema
column path + a new admin endpoint, deliberately not present.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import structlog
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict

from marketmind_api.deps import DatabaseUrlDep
from marketmind_api.routes.admin import AdminUserDep
from marketmind_api.trader import read

router = APIRouter(prefix="/trader", tags=["trader-admin"])
log = structlog.get_logger(__name__)


class _StrictResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FlagUpdateResponse(_StrictResponse):
    """Returned from each admin POST. Echoes the post-update state
    so the caller can confirm without a follow-up GET.
    """

    id: str
    enabled: bool
    approved_for_paper: bool


class AuthCheckResponse(_StrictResponse):
    """Returned from `GET /trader/admin/auth_check`. Used by the
    trader dashboard's credential modal to validate Basic-auth
    creds BEFORE storing them in sessionStorage — avoids the
    "store, fail next click, re-prompt" UX flow.
    """

    ok: bool
    username: str


# ---- Backtest-metrics shape validation ------------------------------------


# Required keys at each nesting level. Mirrors the contract documented
# in `workers/trader/drift.py`'s "STEP 14 SEED-SCRIPT TRANSLATION"
# block — any change here must mirror there (and vice versa).
_WALK_FORWARD_REQUIRED_KEYS = ("out_of_sample_trade_freq_per_week",)
_SINGLE_PASS_REQUIRED_KEYS = (
    "win_rate",
    "avg_return_per_trade",
    "max_drawdown_pct",
)


def _missing_backtest_metric_keys(backtest_metrics: dict[str, Any]) -> list[str]:
    """Return the list of missing required keys (with subtree prefix)
    so the 422 response can name them precisely.
    """
    missing: list[str] = []
    wf = backtest_metrics.get("walk_forward")
    if not isinstance(wf, dict):
        missing.append("walk_forward (subtree missing)")
    else:
        for key in _WALK_FORWARD_REQUIRED_KEYS:
            if key not in wf:
                missing.append(f"walk_forward.{key}")
    sp = backtest_metrics.get("single_pass")
    if not isinstance(sp, dict):
        missing.append("single_pass (subtree missing)")
    else:
        for key in _SINGLE_PASS_REQUIRED_KEYS:
            if key not in sp:
                missing.append(f"single_pass.{key}")
    return missing


# ---- Helpers ---------------------------------------------------------------


def _load_version_or_404(
    database_url: str,
    version_id: UUID,
) -> dict[str, Any]:
    version = read.fetch_version_for_admin(database_url, version_id)
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"strategy version {version_id} not found",
        )
    return version


def _flag_response_after_update(
    database_url: str,
    version_id: UUID,
) -> FlagUpdateResponse:
    """Re-read the row after UPDATE so the response reflects the
    actual DB state (not an in-memory dict the route mutated).
    """
    version = _load_version_or_404(database_url, version_id)
    return FlagUpdateResponse(
        id=version["id"],
        enabled=version["enabled"],
        approved_for_paper=version["approved_for_paper"],
    )


# ---- Routes ----------------------------------------------------------------


@router.get(
    "/admin/auth_check",
    response_model=AuthCheckResponse,
    status_code=status.HTTP_200_OK,
)
def get_auth_check(admin: AdminUserDep) -> AuthCheckResponse:
    """Probe endpoint for the dashboard's credential prompt.

    Hits `AdminUserDep` which validates Basic auth and returns:
      - 200 + `{ok: True, username}` on valid creds
      - 401 on wrong/missing creds
      - 503 when admin auth is unconfigured on the server

    No side effects. The dashboard's modal posts creds, awaits this
    response, and only stores in sessionStorage on 200.
    """
    return AuthCheckResponse(ok=True, username=admin)


@router.post(
    "/strategies/{strategy_version_id}/enable",
    response_model=FlagUpdateResponse,
    status_code=status.HTTP_200_OK,
)
def post_enable_strategy_version(
    strategy_version_id: UUID,
    admin: AdminUserDep,
    database_url: DatabaseUrlDep,
) -> FlagUpdateResponse:
    """Flip `enabled = TRUE` on the version. No-op if already true.

    The immutability trigger (migration 0006) permits this column
    to mutate (along with `approved_for_paper` and `notes`).
    """
    _load_version_or_404(database_url, strategy_version_id)
    read.update_version_flag(
        database_url,
        strategy_version_id,
        enabled=True,
    )
    log.info(
        "trader_strategy_enabled",
        admin=admin,
        version_id=str(strategy_version_id),
    )
    return _flag_response_after_update(database_url, strategy_version_id)


@router.post(
    "/strategies/{strategy_version_id}/disable",
    response_model=FlagUpdateResponse,
    status_code=status.HTTP_200_OK,
)
def post_disable_strategy_version(
    strategy_version_id: UUID,
    admin: AdminUserDep,
    database_url: DatabaseUrlDep,
) -> FlagUpdateResponse:
    """Flip `enabled = FALSE`. The signal engine will stop
    evaluating this version on the next cycle. Open positions are
    NOT auto-closed — they continue to honour their stops + any
    EXIT signals (per the drift-advisory-only policy).
    """
    _load_version_or_404(database_url, strategy_version_id)
    read.update_version_flag(
        database_url,
        strategy_version_id,
        enabled=False,
    )
    log.info(
        "trader_strategy_disabled",
        admin=admin,
        version_id=str(strategy_version_id),
    )
    return _flag_response_after_update(database_url, strategy_version_id)


@router.post(
    "/strategies/{strategy_version_id}/approve_paper",
    response_model=FlagUpdateResponse,
    status_code=status.HTTP_200_OK,
)
def post_approve_paper(
    strategy_version_id: UUID,
    admin: AdminUserDep,
    database_url: DatabaseUrlDep,
) -> FlagUpdateResponse:
    """Flip `approved_for_paper = TRUE`. Rejects with 422 if the
    version's `backtest_metrics` JSONB lacks the keys the drift
    analyzer (Step 9) requires. This is the load-bearing
    gatekeeper for paper-trading approval — once flipped, the
    signal engine starts evaluating the version on its next cycle.

    There is NO equivalent `approve_live` endpoint in v1. The
    `approved_for_live` column on `trader_strategy_versions` is
    locked at `FALSE` by the immutability trigger (migration 0006).
    """
    version = _load_version_or_404(database_url, strategy_version_id)
    missing = _missing_backtest_metric_keys(version["backtest_metrics"])
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "backtest_metrics_incomplete",
                "message": (
                    "Cannot approve for paper: trader_strategy_versions.backtest_metrics "
                    "is missing required walk-forward / single-pass keys. "
                    "The drift analyzer in Step 9 needs these to compare "
                    "paper performance against the approving backtest."
                ),
                "missing_keys": missing,
            },
        )
    read.update_version_flag(
        database_url,
        strategy_version_id,
        approved_for_paper=True,
    )
    log.info(
        "trader_strategy_approved_paper",
        admin=admin,
        version_id=str(strategy_version_id),
    )
    return _flag_response_after_update(database_url, strategy_version_id)


__all__ = ["router"]
