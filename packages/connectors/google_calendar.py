"""Google Calendar — OAuth2 incremental sync via the Google Calendar API.

Runs out-of-process as ``python -m estormi_ingestion.google_calendar.sync``
(via the ``ScriptConnector`` base) so the connector layer does NOT import the
``estormi_ingestion.*`` package — that would invert the dependency direction
(the ``connectors`` layer must not import the ``estormi_ingestion`` engine it drives).
"""

from __future__ import annotations

from .base import ConnectorSpec, ScriptConnector, registry


@registry.register
class GoogleCalendarConnector(ScriptConnector):
    spec = ConnectorSpec(
        name="gcal",
        title="Google Calendar",
        description="Incremental sync of Google Calendar events using stored OAuth credentials.",
        # A pipeline stage, but NOT a default nightly stage — gcal runs only on
        # demand (per-source ▶ / scoped pipeline run), matching the historical
        # daily_ingestion.sh DEFAULT_STAGES array which omitted it.
        dag_stage=True,
        dag_order=4,
        default_stage=False,
        depth_window_env="GCAL_DAYS_WINDOW",
    )
    module = "estormi_ingestion.google_calendar.sync"
