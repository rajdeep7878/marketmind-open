"""Public surface of the StrategySpec schema package.

The canonical document is docs/strategy-spec.md. This package is its
executable form. Anything not re-exported here is an implementation
detail subject to change without notice.

The `introspection` submodule (condition/expression tree walks) is
importable directly — it is internal tooling for the validator and the
backtest engine, not part of the stable public surface.
"""

from marketmind_shared.schemas.strategy_spec.common import (
    AssetClass,
    ContractSpecs,
    Direction,
    Instrument,
    OrderType,
    SessionHours,
    Timeframe,
    timeframe_rank,
)
from marketmind_shared.schemas.strategy_spec.conditions import (
    AndCondition,
    BollingerBandsCondition,
    CandlePatternCondition,
    CompareCondition,
    Condition,
    CrossoverCondition,
    DayOfWeekCondition,
    FallingCondition,
    NotCondition,
    OrCondition,
    PriorSignalCondition,
    PriorTradeCondition,
    RegimeStateCondition,
    RisingCondition,
    RSICondition,
    TimeOfDayCondition,
    WithinLastNBarsCondition,
    ZScoreCondition,
)
from marketmind_shared.schemas.strategy_spec.costs import DEFAULT_COST_MODEL, CostModel
from marketmind_shared.schemas.strategy_spec.entry import EntryRules
from marketmind_shared.schemas.strategy_spec.errors import (
    StrategySpecValidationError,
    StrategySpecValidationErrorGroup,
)
from marketmind_shared.schemas.strategy_spec.exit import (
    ConditionExit,
    ExitCondition,
    ExitRules,
    RMultipleExit,
    StopLossAtrMultiple,
    StopLossExit,
    StopLossFixedPrice,
    StopLossMethod,
    StopLossPercent,
    StopLossTrailingAtr,
    StopLossTrailingPercent,
    TakeProfitAtrMultiple,
    TakeProfitExit,
    TakeProfitFixedPrice,
    TakeProfitMethod,
    TakeProfitPercent,
    TakeProfitRMultiple,
    TimeExit,
    decompose_r_multiple,
)
from marketmind_shared.schemas.strategy_spec.expressions import (
    ConstantExpr,
    Expression,
    LaggedExpr,
    PercentileExpr,
    PriceExpr,
    RatchetExpr,
    ScaledExpr,
)
from marketmind_shared.schemas.strategy_spec.filters import (
    ConditionFilter,
    Filter,
    SessionFilter,
    WeekdayFilter,
)
from marketmind_shared.schemas.strategy_spec.indicators import (
    INDICATOR_DEFAULTS,
    INDICATOR_RULES,
    IndicatorExpr,
    IndicatorName,
    IndicatorParams,
)
from marketmind_shared.schemas.strategy_spec.legs import SpreadConfig, SpreadLeg
from marketmind_shared.schemas.strategy_spec.metadata import ExtractionNote, Metadata
from marketmind_shared.schemas.strategy_spec.sizing import (
    DEFAULT_POSITION_SIZING,
    FixedPercentEquitySizing,
    FixedQuantitySizing,
    PositionSizing,
    RiskBasedSizing,
)
from marketmind_shared.schemas.strategy_spec.spec import (
    StrategySpec,
    spec_uses_stateful_v2,
    spec_uses_tier3,
)
from marketmind_shared.schemas.strategy_spec.validator import validate_spec

__all__ = [
    "DEFAULT_COST_MODEL",
    "DEFAULT_POSITION_SIZING",
    "INDICATOR_DEFAULTS",
    "INDICATOR_RULES",
    "AndCondition",
    "AssetClass",
    "BollingerBandsCondition",
    "CandlePatternCondition",
    "CompareCondition",
    "Condition",
    "ConditionExit",
    "ConditionFilter",
    "ConstantExpr",
    "ContractSpecs",
    "CostModel",
    "CrossoverCondition",
    "DayOfWeekCondition",
    "Direction",
    "EntryRules",
    "ExitCondition",
    "ExitRules",
    "Expression",
    "ExtractionNote",
    "FallingCondition",
    "Filter",
    "FixedPercentEquitySizing",
    "FixedQuantitySizing",
    "IndicatorExpr",
    "IndicatorName",
    "IndicatorParams",
    "Instrument",
    "LaggedExpr",
    "Metadata",
    "NotCondition",
    "OrCondition",
    "OrderType",
    "PercentileExpr",
    "PositionSizing",
    "PriceExpr",
    "PriorSignalCondition",
    "PriorTradeCondition",
    "RMultipleExit",
    "RSICondition",
    "RatchetExpr",
    "RegimeStateCondition",
    "RisingCondition",
    "RiskBasedSizing",
    "ScaledExpr",
    "SessionFilter",
    "SessionHours",
    "SpreadConfig",
    "SpreadLeg",
    "StopLossAtrMultiple",
    "StopLossExit",
    "StopLossFixedPrice",
    "StopLossMethod",
    "StopLossPercent",
    "StopLossTrailingAtr",
    "StopLossTrailingPercent",
    "StrategySpec",
    "StrategySpecValidationError",
    "StrategySpecValidationErrorGroup",
    "TakeProfitAtrMultiple",
    "TakeProfitExit",
    "TakeProfitFixedPrice",
    "TakeProfitMethod",
    "TakeProfitPercent",
    "TakeProfitRMultiple",
    "TimeExit",
    "TimeOfDayCondition",
    "Timeframe",
    "WeekdayFilter",
    "WithinLastNBarsCondition",
    "ZScoreCondition",
    "decompose_r_multiple",
    "spec_uses_stateful_v2",
    "spec_uses_tier3",
    "timeframe_rank",
    "validate_spec",
]
