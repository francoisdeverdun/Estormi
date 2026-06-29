"""Shared HTML rendering for the OAuth browser-redirect landing pages.

The Google-Calendar (:mod:`api.calendar_oauth`) and WHOOP
(:mod:`api.whoop_oauth`) authorization-code flows both redirect the
system browser back to a server endpoint that renders a small
self-contained success/error page. Those pages previously each carried a
byte-identical ``<head>``/``<style>`` shell and ``_oauth_html`` builder;
this module holds the single copy, parameterised over the only bit that
differs — the service name in the ``<title>``.
"""

from __future__ import annotations

from fastapi.responses import HTMLResponse

_PAGE_TAIL = "</body></html>"


def _page_head(service: str) -> str:
    return (
        "<!doctype html><html><head>"
        "<meta charset='utf-8'>"
        f"<title>Estormi — {service}</title>"
        "<style>"
        "body{font-family:-apple-system,Segoe UI,system-ui,sans-serif;"
        "background:#0b0b0c;color:#e8e3d4;padding:48px;line-height:1.55;"
        "max-width:680px;margin:0 auto}"
        "h2{font-family:'Times New Roman',serif;font-weight:600;margin:0 0 14px}"
        "h2.ok{color:#9ab190}"
        "h2.err{color:#d97b7b}"
        "code{background:#1a1a1d;border:1px solid #2a2a2e;padding:1px 6px}"
        "a{color:#e6cb8c}"
        "ol{padding-left:20px}"
        "ol li{margin:6px 0}"
        ".note{margin-top:18px;padding:12px 14px;background:rgba(196,154,58,.06);"
        "border:1px solid #2a2a2e;font-size:14px;color:#a8a394}"
        "</style></head><body>"
    )


def render_oauth_page(service: str, body: str, status_code: int = 200) -> HTMLResponse:
    """Wrap an OAuth landing-page ``body`` in the shared HTML shell.

    ``service`` names the connector (e.g. ``"Google Calendar"`` or
    ``"WHOOP"``) and appears in the page ``<title>``; ``body`` is the
    inner HTML (a success ``<h2 class='ok'>`` or error ``<h2 class='err'>``
    block); ``status_code`` is the HTTP status to return.
    """
    return HTMLResponse(_page_head(service) + body + _PAGE_TAIL, status_code=status_code)
