"""Phase 4 overfitting analysis layer.

Five public modules:

  - walk_forward   : in-sample vs out-of-sample consistency across windows
  - parameter_sweep: how the strategy's return varies in parameter
                     neighborhoods (peakiness == overfitting)
  - monte_carlo    : permutation test on shuffled returns (the strategy's
                     edge vs no time-series structure)
  - deflated_sharpe: Bailey & López de Prado deflation of the observed
                     Sharpe given assumed number of trials + return shape
  - composite      : the four signals combined into a 0-100 score + verdict

Each module is independent — they all consume a StrategySpec + date
range, run their own batch of backtests, and produce a JSON-friendly
Pydantic result.
"""
