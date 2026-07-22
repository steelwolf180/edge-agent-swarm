"""
tests/smoke/test_thermal_guard.py — standalone validation of the §7 thermal
guard, following the same pattern as the other §6 agent smoke tests: test
the plain function (run_thermal_guard / _read_cpu_package_temp_c), not the
@DBOS.step()-decorated wrapper, so this runs without DBOS.launch() or any
of the infra stack (Postgres, llama-server) being up.

Run just this file:
    pytest tests/smoke/test_thermal_guard.py -v

The last test (test_real_sensors_reading) is the one exception — it shells
out to the real `sensors` binary and is skipped automatically if it's not
on PATH or the machine has no readable coretemp/package sensor. That's the
one to watch during your live `watch -n 2 sensors` monitored run.
"""

from __future__ import annotations

import os
import shutil

import pytest

from pipeline.run import (
    ThermalGuardTimeoutError,
    _read_cpu_package_temp_c,
    _require_env_float,
    run_thermal_guard,
)


# ---------------------------------------------------------------------------
# Fakes — deterministic, no real sleeping
# ---------------------------------------------------------------------------

class _FakeClock:
    """Records sleep calls instead of actually waiting."""

    def __init__(self):
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.calls.append(seconds)


def _temp_sequence(values):
    """Returns a callable that yields each value in turn, then repeats the
    last one forever (mirrors a stuck-hot sensor)."""
    values = list(values)

    def _reader():
        return values.pop(0) if len(values) > 1 else values[0]

    return _reader


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def test_returns_immediately_when_already_cool():
    clock = _FakeClock()
    result = run_thermal_guard(
        label="test",
        max_c=60.0,
        poll_s=3.0,
        timeout_s=120.0,
        cooldown_s=15.0,
        temp_reader=_temp_sequence([45.0]),
        sleep_fn=clock.sleep,
    )
    assert result["ok"] is True
    assert result["temp_c"] == 45.0
    assert result["waited_s"] == 0.0
    # Only the unconditional cooldown sleep should have happened, no polling.
    assert clock.calls == [15.0]


def test_polls_until_cool_then_cools_down():
    clock = _FakeClock()
    # Hot, hot, hot, finally cool — three poll waits before it clears.
    result = run_thermal_guard(
        label="test",
        max_c=60.0,
        poll_s=3.0,
        timeout_s=120.0,
        cooldown_s=15.0,
        temp_reader=_temp_sequence([65.0, 63.0, 61.0, 58.0]),
        sleep_fn=clock.sleep,
    )
    assert result["ok"] is True
    assert result["temp_c"] == 58.0
    assert result["waited_s"] == 9.0  # 3 poll waits x 3s
    # Three poll sleeps, then the unconditional cooldown sleep.
    assert clock.calls == [3.0, 3.0, 3.0, 15.0]


def test_raises_on_timeout_without_proceeding():
    clock = _FakeClock()
    # Stays hot forever — should raise once elapsed >= timeout, not proceed.
    with pytest.raises(ThermalGuardTimeoutError, match=r"still at 70\.0"):
        run_thermal_guard(
            label="after Architect",
            max_c=60.0,
            poll_s=5.0,
            timeout_s=12.0,
            cooldown_s=15.0,
            temp_reader=_temp_sequence([70.0]),
            sleep_fn=clock.sleep,
        )
    # It should have polled 3 times (0s, 5s, 10s elapsed) before the 4th
    # check trips elapsed(15) >= timeout(12) and raises — no cooldown sleep
    # since it aborted rather than proceeding.
    assert 15.0 not in clock.calls


def test_label_appears_in_timeout_message():
    clock = _FakeClock()
    with pytest.raises(ThermalGuardTimeoutError, match=r"after Critic"):
        run_thermal_guard(
            label="after Critic",
            max_c=60.0,
            poll_s=5.0,
            timeout_s=5.0,
            cooldown_s=15.0,
            temp_reader=_temp_sequence([99.0]),
            sleep_fn=clock.sleep,
        )


# ---------------------------------------------------------------------------
# Env var loading — no silent fallback, matches *_TOKEN_BUDGET pattern
# ---------------------------------------------------------------------------

def test_require_env_float_raises_when_missing(monkeypatch):
    monkeypatch.delenv("THERMAL_MAX_C", raising=False)
    with pytest.raises(ValueError, match="THERMAL_MAX_C is not set"):
        _require_env_float("THERMAL_MAX_C")


def test_require_env_float_raises_on_garbage(monkeypatch):
    monkeypatch.setenv("THERMAL_MAX_C", "not-a-number")
    with pytest.raises(ValueError, match="not a valid number"):
        _require_env_float("THERMAL_MAX_C")


def test_require_env_float_parses_valid_value(monkeypatch):
    monkeypatch.setenv("THERMAL_MAX_C", "60")
    assert _require_env_float("THERMAL_MAX_C") == 60.0


# ---------------------------------------------------------------------------
# Real hardware check — only test that touches the actual `sensors` binary
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    shutil.which("sensors") is None,
    reason="lm-sensors not installed / `sensors` not on PATH",
)
def test_real_sensors_reading():
    """Confirms the parser finds a plausible package/core temp on THIS
    machine. Not a mock — this is the one test that would have caught a
    naming mismatch (e.g. no 'Package id 0' key on this board) before a
    live pipeline run silently fails inside a @DBOS.step()."""
    temp = _read_cpu_package_temp_c()
    assert 0.0 < temp < 110.0, (
        f"Got {temp}\u00b0C — outside a plausible CPU range, check the "
        f"`sensors -u` output format on this machine and the regexes in "
        f"_read_cpu_package_temp_c()."
    )
