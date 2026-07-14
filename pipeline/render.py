"""
pipeline/render.py

Turns Mermaid C4Context source (ArchitectOutput.context_diagram) into a
PNG via the self-hosted mermaid.ink container. Lives outside agents/
because it's not Architect-specific — the /review page (spec §5 data
flow: "Mermaid rendered via mermaid.ink") needs the same function.

Mirrors the smoke test in KICKOFF_CHECKLIST.md §3: GET, not POST;
URL-safe base64, not standard.
"""


from __future__ import annotations

import base64
import httpx
import os

from dotenv import load_dotenv
load_dotenv()


def _require_env(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {key} (check your .env file)"
        )
    return value


MERMAID_URL = _require_env("MERMAID_INK_URL")


def render_diagram_image(
    mermaid_source: str,
    base_url: str = MERMAID_URL,
    timeout: float = 15.0,
) -> bytes:
    """Render Mermaid source to PNG bytes via self-hosted mermaid.ink.

    Raises httpx.HTTPStatusError if mermaid.ink rejects the source
    (e.g. invalid Mermaid syntax) or is unreachable.
    """
    encoded = base64.urlsafe_b64encode(mermaid_source.encode("utf-8")).decode("ascii")
    url = f"{base_url}/img/{encoded}"
    response = httpx.get(url, timeout=timeout)
    response.raise_for_status()
    return response.content
