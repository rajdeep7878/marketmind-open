"""Worker-side services: content ingestion, transcription, LLM extraction.

These are the building blocks RQ job functions wire together. Each
service is a pure-Python module — no Redis, no DB calls, no FastAPI
imports — so it can be unit-tested in isolation and mocked at the job
boundary.
"""
