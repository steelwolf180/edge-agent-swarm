"""
Pytest version of the isolated unit test for
agents/scribe.py::_detect_ungrounded_content().

Purpose (KICKOFF_CHECKLIST.md §8.1, P0): every confirmed Scribe fabrication
occurrence so far (workflows 3303ce31-, 48b3f065-, 18ed94eb-) was caught by
_detect_example_domain_leak() or _detect_example_copying() -- both of which
key off signatures unique to the worked-example text (a glacier/Iridium
token, or near-verbatim structural similarity to Example 3). Neither run
independently exercised _detect_ungrounded_content(), the guard meant to
catch genuinely novel fabrication -- invented content that shares no
vocabulary with the real diff, but also doesn't copy the example.

This module does NOT import scribe.py directly, because scribe.py's
module-level code requires env vars (SCRIBE_TOKEN_BUDGET, LLAMA_SERVER_URL,
etc.) and packages (httpx, pydantic, deepdiff) plus two project-local
schema modules (schemas.adr, schemas.spec) that a pure unit test on this
one function shouldn't need to stand up. Since _content_tokens() and
_detect_ungrounded_content() are pure functions with no dependency on any
of that, this module extracts their exact source text (plus supporting
constants/regex) directly out of the real scribe.py by line range and
execs it in an isolated namespace once per test session -- so the test
exercises the real, current implementation byte-for-byte, not a
hand-copied reimplementation that could silently drift out of sync.

Run: pytest tests/unit/test_ungrounded_content_pytest.py -v
"""
import re
from pathlib import Path

import pytest

# Resolved relative to this test file's location, not the caller's cwd, so
# `pytest` from the repo root and `pytest tests/unit/` both find the real
# scribe.py. Adjust the parents[N] count if this file ever moves to a
# different depth under the repo root.
SCRIBE_PY_PATH = Path(__file__).resolve().parents[2] / "agents" / "scribe.py"

# Exact line ranges in the real scribe.py (1-indexed, inclusive) for every
# symbol _detect_ungrounded_content() and _content_tokens() depend on.
# If scribe.py is edited, re-check these ranges still point at the right
# blocks -- a silent off-by-one here would test stale logic without
# erroring (load_extracted_functions() below does sanity-check that both
# expected names come out defined, but not that their *bodies* are current).
_EXTRACT_RANGES = [
    (586, 592),   # _STOPWORDS
    (598, 598),   # _SCHEMA_NOISE_WORDS
    (600, 600),   # _WORD_RE
    (603, 609),   # _content_tokens()
    (620, 620),   # _MIN_GROUNDING_OVERLAP
    (625, 629),   # _ALREADY_FLAGGED_PREFIXES
    (632, 692),   # _detect_ungrounded_content()
]


@pytest.fixture(scope="module", autouse=True)
def use_testing_database():
    """Overrides tests/conftest.py's session-scoped, autouse
    use_testing_database fixture for this module only.

    That fixture requires TESTING_DATABASE_URL to guard against tests
    hitting DBOS_SYSTEM_DATABASE_URL (the real DB) by accident -- a real
    concern for tests that touch persistence. This module never opens a
    DB connection at all: it extracts and execs two pure functions
    (_detect_ungrounded_content, _content_tokens) out of scribe.py's
    source text and asserts on their return values. Pytest resolves a
    same-named fixture defined in a test module over one from conftest.py
    for that module's own tests, so this shadows the DB check here
    without changing its behavior for every other test in the suite --
    conftest.py is untouched.
    """
    yield


@pytest.fixture(scope="module")
def extracted():
    """Extracts and execs the real _detect_ungrounded_content() and
    _content_tokens() out of the live agents/scribe.py. Module-scoped so
    the file is only read/exec'd once per test run, not once per case."""
    assert SCRIBE_PY_PATH.exists(), (
        f"Expected agents/scribe.py at resolved path {SCRIBE_PY_PATH}, but "
        f"it doesn't exist. If the repo layout differs from "
        f"tests/unit/<this file> -> ../../agents/scribe.py, fix the "
        f"parents[N] index above, or hardcode the path."
    )

    with open(SCRIBE_PY_PATH, "r") as f:
        lines = f.readlines()

    source_chunks = [
        "".join(lines[start - 1:end]) for start, end in _EXTRACT_RANGES
    ]
    source = "\n".join(source_chunks)

    namespace = {"re": re}
    try:
        exec(compile(source, str(SCRIBE_PY_PATH), "exec"), namespace)
    except Exception as e:
        pytest.fail(
            f"Extracted source failed to exec -- line ranges in "
            f"_EXTRACT_RANGES likely no longer match scribe.py's current "
            f"layout. Re-check by hand. Error: {e}"
        )

    for required in ("_detect_ungrounded_content", "_content_tokens"):
        assert required in namespace, (
            f"Extracted source did not define {required} -- line ranges "
            f"are wrong. Failing rather than testing nothing under a "
            f"passing run."
        )

    return namespace


@pytest.fixture(scope="module")
def detect_ungrounded_content(extracted):
    return extracted["_detect_ungrounded_content"]


@pytest.fixture(scope="module")
def content_tokens(extracted):
    return extracted["_content_tokens"]


# --- Shared fixture data -------------------------------------------------
REAL_DIFF_SUMMARY = (
    "- Added functional_requirements.integration_points: "
    "['AWS S3', 'AWS RDS', 'Confluence', 'hosted embedding API', 'hosted LLM API']\n"
    "- Added functional_requirements.core_features: "
    "['document ingestion', 'chunk-quality flagging', 'semantic retrieval']"
)


# --- Cases -----------------------------------------------------------
# Each case: (id, parsed_dict, diff_summary, expect_flagged)
CASES = [
    pytest.param(
        {"decision": "Integrate AWS S3, AWS RDS, and Confluence as external "
                     "sources for document ingestion and semantic retrieval."},
        REAL_DIFF_SUMMARY,
        False,
        id="clean_grounded_pass",
    ),
    pytest.param(
        {"decision": "Consolidate all tenant billing records onto a "
                     "quarterly batch export schedule to lower audit "
                     "overhead."},
        REAL_DIFF_SUMMARY,
        True,
        id="novel_ungrounded_fabrication_no_domain_token",
    ),
    pytest.param(
        {"decision": "Consolidate all tenant data into a single shared "
                     "retrieval index to reduce operating cost."},
        REAL_DIFF_SUMMARY,
        False,
        id="known_limitation_one_real_noun_rides_along",
    ),
    pytest.param(
        {"decision": "No meaningful decision to record: no spec changes "
                     "were detected in this run."},
        "No field-level changes detected.",
        False,
        id="zero_diff_run_exempted",
    ),
    pytest.param(
        {"decision": "POSSIBLE EXAMPLE COPY -- FLAG FOR HUMAN REVIEW: "
                     "Establish initial architecture..."},
        REAL_DIFF_SUMMARY,
        False,
        id="already_flagged_by_earlier_guard_skipped",
    ),
    pytest.param(
        {"decision": "Deploy a global content delivery network for static "
                     "asset caching."},
        REAL_DIFF_SUMMARY,
        True,
        id="one_shared_word_is_enough_to_pass",
    ),
]


@pytest.mark.parametrize("parsed, diff_summary, expect_flagged", CASES)
def test_detect_ungrounded_content(
    detect_ungrounded_content, parsed, diff_summary, expect_flagged
):
    result = detect_ungrounded_content(parsed, diff_summary)
    got_flagged = "decision" in result
    assert got_flagged == expect_flagged, (
        f"decision={parsed.get('decision')!r}\n"
        f"diff_summary={diff_summary!r}\n"
        f"expected flagged={expect_flagged}, got={got_flagged} "
        f"(guard returned: {result})"
    )


def test_novel_fabrication_is_the_target_case(detect_ungrounded_content):
    """Explicit standalone assertion for the confirmation the checklist's
    §8.1 P0 entry was actually waiting on: a fabricated decision sharing
    zero vocabulary with the diff, no domain-leak token, and no
    example-copy structural match still gets caught. This duplicates one
    row of the parametrized table above on purpose -- as a named test
    it survives independently of any future edits to the CASES list, and
    a reviewer scanning test names sees this confirmation called out
    explicitly rather than buried as one row among six."""
    parsed = {
        "decision": "Consolidate all tenant billing records onto a "
                     "quarterly batch export schedule to lower audit "
                     "overhead."
    }
    result = detect_ungrounded_content(parsed, REAL_DIFF_SUMMARY)
    assert result == ["decision"], (
        "This is the specific coverage gap KICKOFF_CHECKLIST.md's 8.1 P0 "
        "entry (18ed94eb-... update) flagged: _detect_ungrounded_content() "
        "had never independently fired on a fabrication that domain-leak "
        "and example-copy guards couldn't also catch. If this assertion "
        "fails, that confirmation gap is still open."
    )


def test_known_limitation_documented_not_silently_accepted(
    detect_ungrounded_content,
):
    """Not a target for closing P0 -- this documents an already-known,
    accepted gap so it isn't rediscovered as a surprise later. A
    fabricated sentence sharing exactly one real diff noun still passes,
    by design (_MIN_GROUNDING_OVERLAP=1). If this test ever starts
    failing because someone tightened the threshold, that's a deliberate
    behavior change worth a checklist note, not a regression to silently
    fix here."""
    parsed = {
        "decision": "Consolidate all tenant data into a single shared "
                     "retrieval index to reduce operating cost."
    }
    result = detect_ungrounded_content(parsed, REAL_DIFF_SUMMARY)
    assert result == [], (
        "Expected this to pass unflagged per the documented "
        "one-shared-noun loophole. If it's now flagged, "
        "_MIN_GROUNDING_OVERLAP or the guard's logic changed -- update "
        "this test's expectation deliberately, don't just delete it."
    )