# tests/conftest.py
"""
Redirects DBOS_SYSTEM_DATABASE_URL -> TESTING_DATABASE_URL for the whole
test suite. pipeline/persistence.py and pipeline/run.py both read
DBOS_SYSTEM_DATABASE_URL at call time (not import time), so this
monkeypatch is sufficient without touching either module — same DB
separation convention as the reset_dbos fixture in DBOS_Prompt.

Applies to every test under tests/, including tests/integration/
test_pipeline_approval.py's dbos_launched fixture, since that also reads
os.environ.get("DBOS_SYSTEM_DATABASE_URL") directly.
"""
import os
import pytest


@pytest.fixture(autouse=True)
def use_testing_database(monkeypatch):
    testing_url = os.environ.get("TESTING_DATABASE_URL")
    if not testing_url:
        pytest.fail(
            "TESTING_DATABASE_URL not set in .env — tests must not run "
            "against DBOS_SYSTEM_DATABASE_URL (the real DB)."
        )
    monkeypatch.setenv("DBOS_SYSTEM_DATABASE_URL", testing_url)