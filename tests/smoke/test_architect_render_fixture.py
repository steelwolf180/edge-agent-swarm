"""
tests/smoke/test_architect_render_fixture.py

Fast counterpart to test_architect.py::test_diagram_renders_in_mermaid_ink —
same render path, but loads ArchitectOutput from a JSON fixture instead of
making a live Gemma call. Use this when iterating on the mermaid.ink
container, render.py, or a specific malformed-diagram edge case, without
paying the ~90s Gemma inference cost each time.

Reuses the real ArchitectOutput schema and render_diagram_image() function
directly — not a reimplementation — so it can't drift out of sync with
production code the way a standalone bash/jq/curl script can.

Preconditions:
    - mermaid.ink reachable at MERMAID_INK_URL (.env)
    - fixture JSON files under tests/smoke/fixtures/ match the current
      ArchitectOutput schema (schemas/architect.py)

Run from repo root:
    pytest tests/smoke/test_architect_render_fixture.py -v

Add more fixtures (e.g. a malformed-Mermaid case, a missing-title case)
by dropping a new *_stub.json file in fixtures/ and adding its path to
FIXTURE_PATHS below — no new test function needed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.render import render_diagram_image
from schemas.architect import ArchitectOutput

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURE_PATHS = [
    FIXTURES_DIR / "architect_output_stub.json",
]


def _load_architect_output(path: Path) -> ArchitectOutput:
    data = json.loads(path.read_text())
    # model_validate (not the constructor) so this fails loudly on schema
    # drift, same reasoning as test_architect_output_validates in
    # test_architect.py — a fixture written against an old field layout
    # should error here, not produce a misleading downstream failure.
    return ArchitectOutput.model_validate(data)


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_fixture_validates_against_schema(fixture_path: Path) -> None:
    output = _load_architect_output(fixture_path)
    assert output.context_diagram.startswith("C4Context")


@pytest.mark.parametrize("fixture_path", FIXTURE_PATHS, ids=lambda p: p.stem)
def test_fixture_renders_in_mermaid_ink(fixture_path: Path) -> None:
    output = _load_architect_output(fixture_path)
    image_bytes = render_diagram_image(output.context_diagram)
    # JPEG SOI magic bytes — mermaid.ink's /img/ endpoint returns JPEG,
    # not PNG, despite the field name. Confirmed via curl + Content-Type.
    assert image_bytes[:3] == b"\xff\xd8\xff"
