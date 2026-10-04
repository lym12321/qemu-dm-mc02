"""The host barrier is independent of devices, wire protocols and wall time."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.core import Coordinator, Session, StepResult


class Participant:
    def __init__(self):
        self.events = []
        self.result = None
        self.error = None

    def reset(self, session):
        self.events.append(session)

    def step(self, ctx):
        self.events.append(ctx)
        if self.error:
            raise self.error
        return self.result or StepResult("ok", ctx.t_sim_ns, {"sample": 7})

    def close(self):
        self.events.append("close")
        if self.error:
            raise self.error


def test_session_step_and_committed_barrier():
    a, b = Participant(), Participant()
    c = Coordinator({"replay": a, "world": b})
    with pytest.raises(RuntimeError):
        c.step(1)
    c.reset(Session(11, 100))
    results = c.step(10)
    assert c.t_sim_ns == 110 and c.step_id == 1
    assert results["world"].diagnostics == {"sample": 7}
    assert a.events[-1] == b.events[-1]
    assert (a.events[-1].session_id, a.events[-1].t_sim_ns, a.events[-1].dt_ns) == (11, 110, 10)
    for dt in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            c.step(dt)
    assert c.t_sim_ns == 110 and not c.failed
    c.reset(Session(12))
    assert c.step(5)["replay"].reached_time_ns == 5


@pytest.mark.parametrize("result", [StepResult("failed", 10), StepResult("ok", 9),
                                   StepResult("ok", 11), None])
def test_failed_or_wrong_barrier_freezes_clock_until_reset(result):
    a, b = Participant(), Participant()
    c = Coordinator({"first": a, "second": b})
    c.reset(Session(1))
    error = RuntimeError("backend stopped")
    b.result, b.error = result, error if result is None else None
    with pytest.raises(RuntimeError):
        c.step(10)
    assert c.t_sim_ns == 0 and c.step_id == 0 and c.failed
    with pytest.raises(RuntimeError):
        c.step(10)
    assert len(a.events) == 2
    b.result, b.error = None, None
    c.reset(Session(2))
    c.step(10)
    assert c.t_sim_ns == 10


def test_close_attempts_all_resources_once_even_on_interrupt():
    a, b = Participant(), Participant()
    c = Coordinator({"a": a, "b": b})
    b.error = KeyboardInterrupt("interrupted release")
    a.error = OSError("release failed")
    with pytest.raises(KeyboardInterrupt) as caught:
        c.close()
    assert caught.value is b.error
    assert "a close failed" in caught.value.__notes__[0]
    c.close()
    assert a.events == b.events == ["close"]
    with pytest.raises(RuntimeError):
        c.reset(Session(1))


def test_reset_failure_also_freezes_execution():
    a = Participant()
    c = Coordinator({"a": a})
    c.reset(Session(1))
    def fail(session):
        raise OSError("reset failed")
    a.reset = fail
    with pytest.raises(OSError):
        c.reset(Session(2))
    with pytest.raises(RuntimeError):
        c.step(1)
