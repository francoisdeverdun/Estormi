"""Server-side infrastructure for the Estormi MCP FastAPI app.

These submodules own startup/shutdown, request-level security, static mounts,
and the background job/scheduler state. They are wired together in
``main.py``. Tests reach shared state through its canonical module path
(e.g. ``server.jobs._dag_lock``).
"""
