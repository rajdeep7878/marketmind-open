"""Trader-related API helpers.

The trader's runtime lives in `marketmind_workers.trader`; the
API never imports worker code (see Phase 0 architecture). This
subpackage holds read-only DB queries the API uses to serve
trader-related HTTP routes.
"""
