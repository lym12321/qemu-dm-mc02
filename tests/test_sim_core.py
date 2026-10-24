"""The host barrier is independent of devices, wire protocols and wall time."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim.core import Coordinator, Session, StepContext, StepResult
from sim.physics import PhysicsParticipant


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


@pytest.mark.parametrize("same_error", [False, True])
def test_close_attempts_all_resources_once_even_on_interrupt(same_error):
    a, b = Participant(), Participant()
    c = Coordinator({"a": a, "b": b})
    b.error = KeyboardInterrupt("interrupted release")
    a.error = b.error if same_error else OSError("release failed")
    a.error.add_note("nested resource release failed")
    with pytest.raises(KeyboardInterrupt) as caught:
        c.close()
    assert caught.value is b.error
    assert any("a close failed" in note for note in caught.value.__notes__)
    assert caught.value.__notes__.count("nested resource release failed") == 1
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


class PhysicsStub:
    """Record the public boundary; fixed observations do not integrate time."""

    def __init__(self):
        self.events = []
        self.errors = {}
        self.native_time = 7.125
        self.reset_time = 0.0
        self.step_time = 0.123456789123

    @property
    def time_s(self):
        self.events.append("time_s")
        if "time_s" in self.errors:
            raise self.errors["time_s"]
        return self.native_time

    def reset(self):
        self.events.append("reset")
        if "reset" in self.errors:
            raise self.errors["reset"]
        self.native_time = self.reset_time

    def validate_step(self, dt_ns):
        self.events.append(("validate_step", dt_ns))
        if "validate_step" in self.errors:
            raise self.errors["validate_step"]

    def step(self, dt_ns):
        self.events.append(("step", dt_ns))
        self.native_time = self.step_time
        if "step" in self.errors:
            raise self.errors["step"]

    def close(self):
        self.events.append("close")
        if "close" in self.errors:
            raise self.errors["close"]


@pytest.mark.parametrize("method", ["reset", "validate_step", "step", "close"])
def test_physics_admission_requires_public_methods_without_effects(method):
    physics = PhysicsStub()
    setattr(physics, method, None)
    with pytest.raises(TypeError, match=method):
        PhysicsParticipant(physics)
    assert physics.events == []
    assert physics.native_time == 7.125


@pytest.mark.parametrize("time", [0, float("nan"), -1.0])
def test_physics_admission_requires_native_float_time_without_release(time):
    physics = PhysicsStub()
    physics.native_time = time
    with pytest.raises(RuntimeError, match="time_s"):
        PhysicsParticipant(physics)
    assert physics.events == ["time_s"]


def test_physics_time_getter_admission_error_keeps_caller_ownership():
    physics = PhysicsStub()
    error = OSError("native observation unavailable")
    physics.errors["time_s"] = error
    with pytest.raises(OSError) as caught:
        PhysicsParticipant(physics)
    assert caught.value is error
    assert physics.events == ["time_s"]
    physics.close()
    assert physics.events[-1] == "close"


def test_physics_coordinator_preserves_origin_calls_and_aliased_native_seconds():
    physics = PhysicsStub()
    participant = PhysicsParticipant(physics)
    assert participant.physics is physics
    assert physics.events == ["time_s"]
    with pytest.raises(RuntimeError, match="reset"):
        participant.step(StepContext(1, 1, 7, 7))
    assert physics.events == ["time_s"]

    coordinator = Coordinator({"world": participant})
    origin = 0xFFFFFFFFFFFF0000 + 17
    coordinator.reset(Session(11, origin))
    assert physics.events == ["time_s", "reset", "time_s"]
    assert participant.t_sim_ns == coordinator.t_sim_ns == origin
    for step_id in (1, 2):
        result = coordinator.step(7)["world"]
        assert result == StepResult("ok", origin + 7 * step_id,
                                    {"native_time_s": 0.123456789123})
        assert participant.t_sim_ns == coordinator.t_sim_ns == origin + 7 * step_id
        assert not participant.failed
    # Both completed native observations are identical; no float-progress gate.
    assert physics.events[3:] == [("validate_step", 7), ("step", 7), "time_s"] * 2
    coordinator.close()
    coordinator.close()
    assert physics.events[-1] == "close" and physics.events.count("close") == 1


def test_session_and_step_time_fit_unsigned64_without_float_conversion():
    limit = 0xFFFFFFFFFFFFFFFF
    assert Session(1, limit).t_sim_ns == limit
    assert StepContext(1, 1, limit, 1).t_sim_ns == limit
    for create in (lambda: Session(1, limit + 1),
                   lambda: StepContext(1, 1, limit + 1, 1)):
        with pytest.raises(ValueError, match="unsigned64"):
            create()


def test_physics_bad_context_and_reset_type_reject_before_effects():
    physics = PhysicsStub()
    participant = PhysicsParticipant(physics)
    participant.reset(Session(11, 100))
    events = list(physics.events)
    for ctx in (object(), StepContext(12, 1, 107, 7),
                StepContext(11, 2, 107, 7), StepContext(11, 1, 108, 7)):
        with pytest.raises(ValueError, match="session and exact target"):
            participant.step(ctx)
    with pytest.raises(TypeError, match="Session"):
        participant.reset(object())
    assert physics.events == events
    assert participant.t_sim_ns == 100 and not participant.failed
    assert participant.step(StepContext(11, 1, 107, 7)).reached_time_ns == 107


@pytest.mark.parametrize("stage", ["validate_step", "step", "time_s"])
def test_physics_failed_step_freezes_and_reset_recovers_without_replay(stage):
    physics = PhysicsStub()
    participant = PhysicsParticipant(physics)
    coordinator = Coordinator({"world": participant})
    coordinator.reset(Session(1, 100))
    physics.events.clear()
    error = OSError(f"{stage} failed")
    physics.errors[stage] = error
    with pytest.raises(OSError) as caught:
        coordinator.step(7)
    assert caught.value is error
    assert participant.failed and coordinator.failed
    assert participant.t_sim_ns == coordinator.t_sim_ns == 100
    expected = [("validate_step", 7)]
    if stage != "validate_step":
        expected.append(("step", 7))
        assert physics.native_time == physics.step_time  # partial effects survive
    if stage == "time_s":
        expected.append("time_s")
    assert physics.events == expected
    with pytest.raises(RuntimeError, match="reset"):
        participant.step(StepContext(1, 1, 107, 7))
    with pytest.raises(RuntimeError, match="reset"):
        coordinator.step(7)
    assert physics.events == expected
    physics.errors.clear()
    coordinator.reset(Session(2, 500))
    assert coordinator.step(9)["world"].reached_time_ns == 509
    assert physics.events[len(expected):] == ["reset", "time_s",
                                             ("validate_step", 9), ("step", 9), "time_s"]
    coordinator.close()


@pytest.mark.parametrize("failure", ["reset", "time_s", "nonzero"])
def test_physics_failed_reset_freezes_old_origin_until_success(failure):
    physics = PhysicsStub()
    participant = PhysicsParticipant(physics)
    participant.reset(Session(1, 100))
    physics.events.clear()
    error = OSError("reset observation failed")
    if failure == "nonzero":
        physics.reset_time = 0.5
    else:
        physics.errors[failure] = error
    with pytest.raises(RuntimeError if failure == "nonzero" else OSError) as caught:
        participant.reset(Session(2, 500))
    if failure != "nonzero":
        assert caught.value is error
    assert participant.failed and participant.t_sim_ns == 100
    events = ["reset"] if failure == "reset" else ["reset", "time_s"]
    assert physics.events == events
    with pytest.raises(RuntimeError, match="reset"):
        participant.step(StepContext(1, 1, 107, 7))
    assert physics.events == events
    physics.errors.clear()
    physics.reset_time = 0.0
    participant.reset(Session(3, 900))
    assert participant.step(StepContext(3, 1, 907, 7)).reached_time_ns == 907
    participant.close()


def test_physics_close_error_is_terminal_and_not_retried():
    physics = PhysicsStub()
    participant = PhysicsParticipant(physics)
    participant.reset(Session(1))
    physics.events.clear()
    error = KeyboardInterrupt("native close interrupted")
    physics.errors["close"] = error
    with pytest.raises(KeyboardInterrupt) as caught:
        participant.close()
    assert caught.value is error and participant.closed
    participant.close()
    with pytest.raises(RuntimeError, match="closed"):
        participant.reset(Session(2))
    with pytest.raises(RuntimeError, match="reset"):
        participant.step(StepContext(1, 1, 7, 7))
    assert physics.events == ["close"]
