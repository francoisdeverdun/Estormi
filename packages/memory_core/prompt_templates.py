"""Shared Jinja2 environment for LLM prompt templates.

All prompts that get fed to a local or cloud LLM live as ``.j2`` files under
``prompts/llm/``. Loading templates through one shared environment makes the
prompts editable without touching Python code and avoids scattering large
multi-line strings inside business logic.
"""

from __future__ import annotations

import os
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


def _resolve_prompts_dir() -> Path:
    """Locate ``prompts/llm`` for both the source checkout and the packaged app.

    A plain ``parents[2]`` only works when this module lives at
    ``<repo>/memory_core/prompt_templates.py``. In the macOS bundle
    ``memory_core`` is pip-installed under
    ``…/python/lib/python3.12/site-packages/`` while the prompts ship
    at the bundle resource root (``…/_up_/_up_/prompts/llm``), so the relative guess
    points at a directory that doesn't exist. Resolve robustly instead:

      1. ``ESTORMI_REPO_ROOT`` (the Rust sidecar sets it to the bundle resource
         root; dev runs set it to the checkout) → ``<root>/prompts/llm``;
      2. otherwise walk up from this file until a ``prompts/llm`` dir is found
         (handles both the source layout and the installed-package layout);
      3. last resort: the original repo-relative guess.
    """
    root = os.getenv("ESTORMI_REPO_ROOT", "").strip()
    if root:
        cand = Path(root) / "prompts" / "llm"
        if cand.is_dir():
            return cand
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / "prompts" / "llm"
        if cand.is_dir():
            return cand
    return here.parents[2] / "prompts" / "llm"


PROMPTS_DIR: Path = _resolve_prompts_dir()

# ``autoescape`` defaults to off for ``.j2`` files since the prompts are plain
# text fed to an LLM, not HTML. Trim/strip blocks keeps the rendered prompts
# compact even when the template uses Jinja control structures.
env: Environment = Environment(
    loader=FileSystemLoader(str(PROMPTS_DIR)),
    autoescape=select_autoescape(default=False),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=False,
)


def render(template_name: str, /, **context: object) -> str:
    """Render a ``.j2`` template by name from ``prompts/llm/``.

    The trailing ``.j2`` extension may be omitted.
    """
    if not template_name.endswith(".j2"):
        template_name = f"{template_name}.j2"
    return env.get_template(template_name).render(**context)


__all__ = ["PROMPTS_DIR", "render"]
