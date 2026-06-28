"""memory_core — canonical domain/storage/business logic for Estormi.

Callers import the submodules they need explicitly
(``from memory_core.embedder import embed``, ``from memory_core import dag_state``,
etc.). This package deliberately re-exports nothing — keeping the top-level
namespace empty avoids hiding the module a symbol actually lives in.
"""

__all__: list[str] = []
