"""
Isolated unit test for agents/scribe.py::_detect_ungrounded_content().

Purpose (KICKOFF_CHECKLIST.md §8.1, P0): every confirmed Scribe fabrication
occurrence so far (workflows 3303ce31-, 48b3f065-, 18ed94eb-) was caught by
_detect_example_domain_leak() or _detect_example_copying() -- both of which
key off signatures unique to the worked-example text (a glacier/Iridium
token, or near-verbatim structural similarity to Example 3). Neither of
those runs has independently exercised _detect_ungrounded_content(), the
guard meant to catch genuinely novel fabrication -- invented content that
shares no vocabulary with the real diff, but also doesn't copy the example.

This script does NOT run the full pipeline or import scribe.py directly,
because scribe.py's module-level code requires env vars (SCRIBE_TOKEN_BUDGET,
LLAMA_SERVER_URL, etc.) and packages (httpx, pydantic, deepdiff) plus two
project-local schema modules (schemas.adr, schemas.spec) that aren't present
in this sandbox. Since _content_tokens() and _detect_ungrounded_content() are
pure functions with no dependency on any of that, this script extracts their
exact source text (plus their supporting constants/regex) directly out of
the real uploaded scribe.py by line range and executes it in an isolated
namespace -- so the test is exercising the real, current implementation
byte-for-byte, not a hand-copied re-implementation that could silently drift
out of sync with the actual file.

Run: python3 test_ungrounded_content.py
"""
import re
import sys
from pathlib import Path

# Resolved relative to this test file's location, not the caller's cwd, so
# `python tests/unit/test_ungrounded_content.py` from the repo root and
# `python test_ungrounded_content.py` from inside tests/unit/ both find the
# real scribe.py. Adjust the parents[N] count if this file ever moves to a
# different depth under the repo root.
SCRIBE_PY_PATH = Path(__file__).resolve().parents[2] / "agents" / "scribe.py"

# Exact line ranges in the real scribe.py (1-indexed, inclusive) for every
# symbol _detect_ungrounded_content() and _content_tokens() depend on.
# If scribe.py is edited, re-check these ranges still point at the right
# blocks -- a silent off-by-one here would test stale logic without erroring.
_EXTRACT_RANGES = [
    (586, 592),   # _STOPWORDS
    (598, 598),   # _SCHEMA_NOISE_WORDS
    (600, 600),   # _WORD_RE
    (603, 609),   # _content_tokens()
    (620, 620),   # _MIN_GROUNDING_OVERLAP
    (625, 629),   # _ALREADY_FLAGGED_PREFIXES
    (632, 692),   # _detect_ungrounded_content()
]


def load_real_function():
    if not SCRIBE_PY_PATH.exists():
        print(f"FATAL: expected agents/scribe.py at resolved path "
              f"{SCRIBE_PY_PATH}, but it doesn't exist. If the repo layout "
              f"differs from tests/unit/<this file> -> ../../agents/scribe.py, "
              f"fix the parents[N] index above, or just hardcode the path.")
        sys.exit(1)

    with open(SCRIBE_PY_PATH, "r") as f:
        lines = f.readlines()

    source_chunks = []
    for start, end in _EXTRACT_RANGES:
        chunk = "".join(lines[start - 1:end])
        source_chunks.append(chunk)
    source = "\n".join(source_chunks)

    namespace = {"re": re}
    try:
        exec(compile(source, str(SCRIBE_PY_PATH), "exec"), namespace)
    except Exception as e:
        print(f"FATAL: extracted source failed to exec -- line ranges in "
              f"_EXTRACT_RANGES likely no longer match scribe.py's current "
              f"layout. Re-check by hand. Error: {e}")
        sys.exit(1)

    for required in ("_detect_ungrounded_content", "_content_tokens"):
        if required not in namespace:
            print(f"FATAL: extracted source did not define {required} -- "
                  f"line ranges are wrong. Aborting rather than testing "
                  f"nothing under a passing exit code.")
            sys.exit(1)

    return namespace["_detect_ungrounded_content"], namespace["_content_tokens"]


# --- Test cases -------------------------------------------------------
# Each case: (label, parsed_dict, diff_summary, expect_flagged: bool, note)
REAL_DIFF_SUMMARY = (
    "- Added functional_requirements.integration_points: "
    "['AWS S3', 'AWS RDS', 'Confluence', 'hosted embedding API', 'hosted LLM API']\n"
    "- Added functional_requirements.core_features: "
    "['document ingestion', 'chunk-quality flagging', 'semantic retrieval']"
)

CASES = [
    (
        "clean_grounded_pass",
        {"decision": "Integrate AWS S3, AWS RDS, and Confluence as external "
                     "sources for document ingestion and semantic retrieval."},
        REAL_DIFF_SUMMARY,
        False,
        "Correctly grounded decision reusing real diff vocabulary -- must "
        "NOT be flagged. Sanity check that the guard doesn't over-fire on "
        "legitimate output.",
    ),
    (
        "novel_ungrounded_fabrication_no_domain_token",
        {"decision": "Consolidate all tenant billing records onto a "
                     "quarterly batch export schedule to lower audit "
                     "overhead."},
        REAL_DIFF_SUMMARY,
        True,
        "THE CASE THAT MATTERS. Fabricated content with zero glacier/"
        "Iridium tokens (won't trip _detect_example_domain_leak) and no "
        "structural resemblance to Example 3 (won't trip "
        "_detect_example_copying's 0.90 fuzzy threshold), and -- unlike an "
        "earlier draft of this case -- zero true token overlap with the "
        "diff (verified: no shared word with REAL_DIFF_SUMMARY's content "
        "tokens). This is exactly the class of failure none of the other "
        "three guards can see. If this case flags, "
        "_detect_ungrounded_content is confirmed working independently -- "
        "this is the missing confirmation from the checklist's 8.1 P0 "
        "entry.",
    ),
    (
        "known_limitation_one_real_noun_rides_along",
        {"decision": "Consolidate all tenant data into a single shared "
                     "retrieval index to reduce operating cost."},
        REAL_DIFF_SUMMARY,
        False,
        "NOT a target for this confirmation -- documents an already-known, "
        "accepted gap. Shares exactly one real diff word ('retrieval') "
        "while the rest of the sentence (tenant billing consolidation, "
        "cost reduction) is fully invented and absent from the diff. Per "
        "the function's own docstring, this deliberately passes: "
        "'a field that shares one real noun with the diff but invents "
        "unsupported detail around it will still pass.' Kept here so this "
        "known limitation stays visible and isn't rediscovered as a "
        "surprise later, but expect_flagged=False is correct as designed, "
        "not a bug.",
    ),
    (
        "zero_diff_run_exempted",
        {"decision": "No meaningful decision to record: no spec changes "
                     "were detected in this run."},
        "No field-level changes detected.",
        False,
        "Genuine zero-diff run -- SCRIBE_SYSTEM_PROMPT's CRITICAL NO-DIFF "
        "RULE case. Must be exempted per the function's own docstring "
        "(nothing to ground against by design), not flagged as fabrication.",
    ),
    (
        "already_flagged_by_earlier_guard_skipped",
        {"decision": "POSSIBLE EXAMPLE COPY -- FLAG FOR HUMAN REVIEW: "
                     "Establish initial architecture..."},
        REAL_DIFF_SUMMARY,
        False,
        "Field already carries a flag prefix from an earlier guard in the "
        "run_scribe() pipeline -- should be skipped here, not double-"
        "flagged with a second, redundant prefix.",
    ),
    (
        "one_shared_word_is_enough_to_pass",
        {"decision": "Deploy a global content delivery network for static "
                     "asset caching."},
        REAL_DIFF_SUMMARY,
        True,
        "Deliberately loose threshold (_MIN_GROUNDING_OVERLAP=1) means this "
        "shares essentially no real vocabulary with the diff and should "
        "still flag -- confirms the guard catches near-zero, not just "
        "exactly-zero, overlap. ('network' is stripped by neither stopword "
        "list; check the actual overlap count if this doesn't match.)",
    ),
]


def main():
    detect, content_tokens = load_real_function()
    print(f"Loaded _detect_ungrounded_content and _content_tokens from the "
          f"real {SCRIBE_PY_PATH} (extracted, not re-implemented).\n")

    failures = []
    for label, parsed, diff_summary, expect_flagged, note in CASES:
        result = detect(parsed, diff_summary)
        got_flagged = "decision" in result
        status = "PASS" if got_flagged == expect_flagged else "FAIL"
        if status == "FAIL":
            failures.append(label)

        print(f"[{status}] {label}")
        print(f"    decision       : {parsed.get('decision')!r}")
        print(f"    expect flagged : {expect_flagged}   got: {got_flagged}   "
              f"(guard returned: {result})")
        print(f"    note           : {note}")
        print()

    print("=" * 70)
    if failures:
        print(f"{len(failures)}/{len(CASES)} case(s) FAILED: {failures}")
        print("Do not treat P0's guard-coverage question as answered until "
              "these pass.")
        sys.exit(1)
    else:
        print(f"All {len(CASES)} cases PASSED.")
        print(
            "Confirms _detect_ungrounded_content() independently catches "
            "novel, non-example-copying fabrication (the "
            "'novel_ungrounded_fabrication_no_domain_token' case) without "
            "over-firing on grounded, zero-diff, or already-flagged input. "
            "This closes the specific confirmation gap noted in "
            "KICKOFF_CHECKLIST.md §8.1's 15 Aug (18ed94eb-...) update -- "
            "next step is still a live cloud_rag.json re-run to confirm "
            "this holds against real LFM output, not just synthetic cases."
        )


if __name__ == "__main__":
    main()