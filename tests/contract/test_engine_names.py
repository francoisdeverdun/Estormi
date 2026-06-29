"""Contract: the engine names tuple is a cross-surface constant.

The iOS app, the web-UI, and the engine queue all depend on these exact names.
A rename without updating downstream consumers silently breaks them.
"""

import pytest

from estormi_server.server.jobs import ENGINES


@pytest.mark.contract
def test_engine_names_golden():
    assert ENGINES == ("ingestion", "briefing", "distill")
