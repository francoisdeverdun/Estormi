"""Connector registry contract: spec names + DAG order are unique.

Two connectors with the same ``ConnectorSpec.name`` would collide in the
``ConnectorRegistry`` map (URLs, settings keys, metrics tags all key off
``name``). The registry itself rejects duplicate registration at import
time, but this test pins the invariant explicitly so failures surface as
contract regressions rather than opaque ``RuntimeError`` traces.

The second half of the test guards the DAG ordering: any spec marked
``default_stage=True`` must have a unique ``dag_order``. Two default
stages sharing a position would make ``dag_stages()`` order
non-deterministic, breaking the nightly DAG.
"""

from __future__ import annotations

from collections import Counter

import pytest

pytestmark = pytest.mark.contract


def test_registered_specs_have_unique_names():
    from connectors import registry

    specs = list(registry.specs())
    assert specs, "registry has no specs — connector imports failed?"

    name_counts = Counter(spec.name for spec in specs)
    duplicates = {name: count for name, count in name_counts.items() if count > 1}
    assert not duplicates, f"duplicate ConnectorSpec.name values: {duplicates}"


def test_default_stage_specs_have_unique_dag_order():
    from connectors import registry

    default_specs = [s for s in registry.specs() if s.default_stage]
    assert default_specs, (
        "no default_stage=True specs registered — the DAG would have nothing to run nightly"
    )

    order_counts = Counter(spec.dag_order for spec in default_specs)
    duplicates = {
        order: [s.name for s in default_specs if s.dag_order == order]
        for order, count in order_counts.items()
        if count > 1
    }
    assert not duplicates, (
        f"default_stage=True specs share a dag_order — DAG order is non-deterministic: {duplicates}"
    )


def test_all_dag_stage_specs_have_unique_dag_order():
    from connectors import registry

    dag_specs = [s for s in registry.specs() if s.dag_stage]
    assert dag_specs, "no dag_stage=True specs registered"
    order_counts = Counter(s.dag_order for s in dag_specs)
    duplicates = {
        order: [s.name for s in dag_specs if s.dag_order == order]
        for order, count in order_counts.items()
        if count > 1
    }
    assert not duplicates, (
        f"dag_stage=True specs share a dag_order — `connectors stages --all` / pipeline.DAG_STAGES order is non-deterministic: {duplicates}"
    )
