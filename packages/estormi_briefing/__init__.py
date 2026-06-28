"""estormi_briefing — the Briefing engine (scheduled daily-note composition).

Entrypoint ``run_briefing.py`` is launched as a subprocess by the server's
briefing launcher; the deterministic correlation spine lives in
``compose/graph.py``. Callers import the submodule they need explicitly
(``from estormi_briefing.run_briefing import run``,
``from estormi_briefing.compose import graph``, etc.). This package
deliberately re-exports nothing — keeping the top-level namespace empty avoids
hiding the module a symbol actually lives in.
"""

__all__: list[str] = []
