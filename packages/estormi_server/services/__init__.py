"""Service / application layer between the FastAPI routers and storage.

The routers under :mod:`estormi_server.api` own HTTP concerns only — request
parsing, status codes, response models, rate-limit decorators, auth. The real
business logic (validation chains, data shaping, classification heuristics,
multi-step orchestration) lives here so it can be unit-tested directly,
without an ASGI client.

Layering (see ``CLAUDE.md`` and ``[tool.importlinter]`` in ``pyproject.toml``):
this layer may use
``tools`` / ``search_api`` / ``qdrant_helpers`` and ``memory_core``, but must
NOT import the FastAPI routers (:mod:`estormi_server.api`) or
:mod:`estormi_server.main`. The dependency direction is one-way:

    api/*  ->  services/*  ->  tools / memory_core
"""
