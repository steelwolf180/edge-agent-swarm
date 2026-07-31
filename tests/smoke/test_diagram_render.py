"""
tests/smoke/test_diagram_render.py

Smoke test for render_mermaid_diagram() (pipeline/run.py), added after the
adr_0002 incident (31 Jul 2026) — a malformed Architect diagram reached
human approval because nothing validated it before DBOS.recv(). This test
does not hit the real mermaid.ink container; http_get is injected with a
fake, same pattern as run_thermal_guard()'s injectable temp_reader/sleep_fn
in test_thermal_guard.py.

Key thing under test: mermaid.ink returns a lexer error as a plain-text
body (confirmed 400 with content-type text/plain in the wild, 31 Jul 2026
manual repro), not a raised exception from an HTTP client — so the check
must inspect content-type, not just call raise_for_status().
"""

from __future__ import annotations

import pytest

from pipeline.run import DiagramRenderError, render_mermaid_diagram

GOOD_DIAGRAM = """C4Context
    title DBOS Pipeline Skeleton System Context
    Person(developer, "Solo Developer", "Submits the project specification and reviews outputs")
    System(pipeline, "DBOS Pipeline", "Orchestrates the entire architecture generation process")
    System_boundary(local_environment, "Local Edge Environment") {
        System(llama_server, "LLaMA Server", "AI inference engine")
        System(postgres, "PostgreSQL", "Versioned artifact store")
    }
    Rel(developer, pipeline, "Submits Architecture Specification")
    Rel(pipeline, llama_server, "Sends prompts and receives generated content")
    Rel(pipeline, postgres, "Reads/Writes versioned artifacts and metadata")"""

# The actual invalid source that produced adr_0002's broken diagram —
# System_boundary() given a third (description) arg and never closed.
BAD_DIAGRAM = (
    'C4Context\n'
    '    System_boundary(local_environment, "Local Edge Environment", '
    '"The on-premise single machine hosting all components")'
)


class _FakeResponse:
    def __init__(self, content_type: str, status_code: int, text: str = "", content: bytes = b""):
        self.headers = {"content-type": content_type}
        self.status_code = status_code
        self.text = text
        self.content = content or text.encode()


def _fake_get_image(url: str, timeout: float):
    return _FakeResponse("image/png", 200, content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 128)


def _fake_get_lexer_error(url: str, timeout: float):
    # Reproduces the real mermaid.ink response observed 31 Jul 2026:
    # HTTP 400, text/plain body, not a raised client-side exception.
    return _FakeResponse(
        "text/plain; charset=utf-8",
        400,
        text="Lexical error on line 2. Unrecognized text.\n...(local_environment, \"Local\n----------------------^",
    )


def test_valid_diagram_passes(monkeypatch):
    result = render_mermaid_diagram(
        GOOD_DIAGRAM,
        mermaid_ink_url="http://localhost:3001",
        http_get=_fake_get_image,
    )
    assert result["ok"] is True
    assert result["content_type"] == "image/png"
    assert result["bytes"] > 0


def test_invalid_diagram_raises():
    with pytest.raises(DiagramRenderError, match="Lexical error"):
        render_mermaid_diagram(
            BAD_DIAGRAM,
            mermaid_ink_url="http://localhost:3001",
            http_get=_fake_get_lexer_error,
        )


def test_error_includes_status_and_content_type():
    """The failure message needs to be diagnostic on its own — this is what
    shows up in the DBOS workflow log when a real run fails here."""
    with pytest.raises(DiagramRenderError) as exc_info:
        render_mermaid_diagram(
            BAD_DIAGRAM,
            mermaid_ink_url="http://localhost:3001",
            http_get=_fake_get_lexer_error,
        )
    message = str(exc_info.value)
    assert "status=400" in message
    assert "text/plain" in message


def test_requires_mermaid_ink_url_env_when_not_passed(monkeypatch):
    """Same fail-loud contract as _require_env elsewhere — a missing
    MERMAID_INK_URL must raise, not silently default to some hardcoded URL."""
    monkeypatch.delenv("MERMAID_INK_URL", raising=False)
    with pytest.raises(ValueError, match="MERMAID_INK_URL"):
        render_mermaid_diagram(GOOD_DIAGRAM, http_get=_fake_get_image)