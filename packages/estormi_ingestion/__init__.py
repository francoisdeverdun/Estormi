"""estormi_ingestion — per-source data pipelines and shared chunking.

Per-source ingestion scripts (``google_calendar``, ``whoop``, ``whatsapp``,
``knowledge``, …) are driven by ``connectors``; cross-cutting helpers
live under ``estormi_ingestion.shared`` — the ingestion core (``chunker``,
``watermark``, ``config``, ``paths``, ``emit``, ``http_client``, ``token_store``)
plus ``shared.delivery`` (vault write + push) and ``shared.host`` (macOS host
integration). Text-safety primitives (``pii_filter``) live in ``memory_core``.
Callers import the submodule they need explicitly (``from
estormi_ingestion.shared import chunker``). This package deliberately re-exports
nothing — keeping the top-level namespace empty avoids hiding the module a
symbol actually lives in.
"""

__all__: list[str] = []
