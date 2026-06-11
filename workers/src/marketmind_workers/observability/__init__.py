"""Observability for the running paper-trading bot.

The daily summary report — a structured snapshot of the trailing 24h plus
current state, written once a day (00:05 UTC) as JSON + rendered text.
This is operator-facing observability, separate from the trading logic.
"""
