"""iCloud Drive + local documents — PDF, DOCX, ODT, PPTX, XLSX, plain text."""

from __future__ import annotations

from .base import ConnectorSpec, ScriptConnector, registry


@registry.register
class DocumentsConnector(ScriptConnector):
    spec = ConnectorSpec(
        name="documents",
        title="Documents",
        description="Indexes documents under the configured roots; supports PDF, DOCX, ODT, PPTX, XLSX, txt.",
        dag_stage=True,
        dag_order=8,
        default_stage=True,
        uses_watermark=True,
        requires_root=True,
        macos_permissions=("FilesAndFolders",),
    )
    module = "estormi_ingestion.documents.ingest_documents"
    # Root is not passed as a CLI flag: ingest_documents reads it from the
    # DOCUMENTS_ROOT env (exported by server/jobs.py) for both the scheduled
    # pipeline run and the per-source manual run.
