# tests/conftest.py
"""
Redirects DBOS_SYSTEM_DATABASE_URL -> TESTING_DATABASE_URL for the whole
test suite, session-wide, so it applies before ANY other fixture reads
the env var — including module-scoped ones like test_pipeline_approval.py's
dbos_launched. The built-in `monkeypatch` fixture is always function-scoped
and can't be depended on by a broader-scoped fixture, hence using
pytest.MonkeyPatch() directly here instead.
"""
import os
import pytest


@pytest.fixture(scope="session", autouse=True)
def use_testing_database():
    testing_url = os.environ.get("TESTING_DATABASE_URL")
    if not testing_url:
        pytest.fail(
            "TESTING_DATABASE_URL not set in .env — tests must not run "
            "against DBOS_SYSTEM_DATABASE_URL (the real DB)."
        )
    mp = pytest.MonkeyPatch()
    mp.setenv("DBOS_SYSTEM_DATABASE_URL", testing_url)
    yield
    mp.undo()