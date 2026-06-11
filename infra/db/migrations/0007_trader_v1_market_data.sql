-- Trader v1: OHLCV ingestion target.
--
-- Distinct from the Parquet cache used by the backtest engine
-- (`/data/cache/market/...`). That cache is a read-once-per-backtest,
-- append-on-miss store keyed off long historical ranges.
-- `trader_candles` is the live feed: written every ingestion cycle
-- (~once per minute), read every signal-execution cycle. NUMERIC
-- prices because the trader is the canonical source of fill prices
-- when the executor reads candle N+1's open — float drift here would
-- propagate into PnL.
--
-- `is_closed` distinguishes finalised bars (the only kind the signal
-- engine ever evaluates) from the in-flight current bar (visible for
-- monitoring but never read for decisions). The ingestion loop only
-- writes rows with is_closed = TRUE; if the live API later includes
-- an open bar, the writer must explicitly set it FALSE.

CREATE TABLE IF NOT EXISTS trader_candles (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    open_ts TIMESTAMPTZ NOT NULL,
    close_ts TIMESTAMPTZ NOT NULL,
    open NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    close NUMERIC NOT NULL,
    volume NUMERIC NOT NULL,
    is_closed BOOLEAN NOT NULL,
    source TEXT NOT NULL DEFAULT 'ccxt',
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (symbol, timeframe, close_ts)
);


-- Signal engine's read pattern: "give me the last N closed candles
-- for (symbol, timeframe), most-recent first". The DESC index lets
-- the planner walk the index directly without a sort.
CREATE INDEX IF NOT EXISTS trader_candles_symbol_tf_close_idx
    ON trader_candles (symbol, timeframe, close_ts DESC);
