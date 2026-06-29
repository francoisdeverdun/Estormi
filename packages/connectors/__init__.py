"""Per-source ingestion connectors.

Importing the submodules here registers each Connector subclass with the
shared `registry`. The imports look unused to static checkers — they are
side-effect imports, hence the `# noqa: F401` markers.

To add a new connector see ``docs/connectors.md``.
"""

from . import (
    apple_mail,  # noqa: F401
    apple_notes,  # noqa: F401
    documents,  # noqa: F401
    google_calendar,  # noqa: F401
    imessage,  # noqa: F401
    knowledge,  # noqa: F401
    reminders,  # noqa: F401
    whatsapp,  # noqa: F401
    whoop,  # noqa: F401
)
from .base import (
    Connector,
    ConnectorRegistry,
    ConnectorResult,
    ConnectorSpec,
    ScriptConnector,
    ShellConnector,
    dag_stages,
    registry,
)

__all__ = [
    "Connector",
    "ConnectorRegistry",
    "ConnectorResult",
    "ConnectorSpec",
    "ScriptConnector",
    "ShellConnector",
    "dag_stages",
    "registry",
]
