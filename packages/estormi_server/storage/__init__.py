"""estormi_server.storage — SQLite chunk store + embedded Qdrant vectors.

``tools`` holds the shared mutable state (DB/Qdrant handles, write lock, embed
fns); ``writers``/``search``/``qdrant_helpers``/``chunk_admin`` reach it lazily.
"""
