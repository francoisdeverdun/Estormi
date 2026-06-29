"""WHOOP — OAuth2 pull of daily physiological cycles from the WHOOP Cloud API.

Runs out-of-process as ``python -m estormi_ingestion.whoop.sync`` (via the
``ScriptConnector`` base) so the connector layer does NOT import the
``estormi_ingestion.*`` package — that would invert the dependency direction
(the ``connectors`` layer must not import the ``estormi_ingestion`` engine it drives).
"""

from __future__ import annotations

from .base import ConnectorSpec, ScriptConnector, registry


@registry.register
class WhoopConnector(ScriptConnector):
    spec = ConnectorSpec(
        name="whoop",
        title="WHOOP",
        description="Daily recovery, sleep, strain and workouts pulled from the WHOOP Cloud API.",
        # A pipeline stage, but NOT a default nightly stage — like gcal, whoop runs
        # only on demand (per-source ▶ / scoped pipeline run) until the user has wired
        # OAuth, so a fresh install's nightly run doesn't fail on a missing
        # token.
        dag_stage=True,
        # Unique order after the last default stage (knowledge=11); whoop is an
        # on-demand stage so it sorts last in a full `--all` pipeline run.
        dag_order=12,
        default_stage=False,
        # estormi_ingestion.whoop.sync writes a watermark via set_watermark(SOURCE, …),
        # so the catalogue/Metrics UI must report this source as watermarked.
        uses_watermark=True,
        depth_window_env="WHOOP_DAYS_WINDOW",
    )
    module = "estormi_ingestion.whoop.sync"
