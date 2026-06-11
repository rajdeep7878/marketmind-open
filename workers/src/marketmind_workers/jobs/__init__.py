"""RQ job callables.

Each module here exposes a `run(...)` function that RQ resolves by
dotted string (see _JOB_TARGETS in api/routes/content.py). Jobs are
the thin glue between the API's request and the worker-side services
in marketmind_workers.services.
"""
