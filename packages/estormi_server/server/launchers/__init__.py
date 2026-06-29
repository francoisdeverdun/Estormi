"""Per-engine launcher modules.

Each module owns the spawn / wait / log-handling logic for one engine.
The shared subprocess handles, log paths, locks and helper plumbing still
live in ``server.jobs`` — the launchers read and mutate that module via
``from server import jobs as _jobs`` so existing tests that patch
``server.jobs.<name>`` continue to drive the launcher path.

Public symbols are re-exported from ``server.jobs`` for back-compat;
external callers should keep importing from there.
"""
