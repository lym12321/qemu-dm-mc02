"""Execute admitted Scenario inputs through the existing Arena owner."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sim.arena.manifest import ArenaManifest
from sim.config.yaml import MAX_TIME_NS
from sim.scenario.manifest import (
    ScenarioCanDelay, ScenarioManifest, ScenarioMotorFeedback,
    ScenarioReferee, validate_assertions,
)

if TYPE_CHECKING:
    from sim.arena.runtime import ArenaResult


def run_manifest(manifest: ScenarioManifest, *, emit=None, on_response=None, on_observation=None,
                 qemu=None, startup_timeout=10.0, timeout=30.0,
                 referee_period_ns=100_000_000) -> ArenaResult:
    """Delegate once; Arena owns event admission, execution and cleanup.

    Events use the shared reset origin. Duration is active world time after all
    requested ready markers, exactly as in the referenced Arena.
    Typed records are projected in caller order; Arena owns their numeric and
    topology admission, scheduling, and all resource effects.
    Scoped assertions reuse LabExperiment with each member's actual initial
    RESET_ACK time. Startup and empty post-read observation batches count;
    success requires all assertions and the original Arena cleanup to complete.
    Optional on_observation borrows every Arena batch read-only and must not
    reenter the runtime. It runs before the internal assertion checker, including
    for members without assertions. Either failure terminates without replay;
    already captured observations remain available, but no result is returned.
    """
    if type(manifest) is not ScenarioManifest:
        raise TypeError("ScenarioManifest is required")
    if (type(manifest.motor_feedback) is not tuple
            or any(type(item) is not ScenarioMotorFeedback for item in manifest.motor_feedback)):
        raise TypeError("Scenario motor_feedback require a plain ScenarioMotorFeedback tuple")
    if any(type(item.offset_ns) is not int or type(item.member) is not str
           or type(item.motor_id) is not int or type(item.enabled) is not bool
           for item in manifest.motor_feedback):
        raise TypeError("Scenario motor_feedback require plain integer times and motor IDs, "
                        "member strings and boolean enabled values")
    if (type(manifest.can_delays) is not tuple
            or any(type(item) is not ScenarioCanDelay for item in manifest.can_delays)):
        raise TypeError("Scenario can_delays require a plain ScenarioCanDelay tuple")
    if any(type(item.offset_ns) is not int or type(item.member) is not str
           or type(item.delay_ns) is not int for item in manifest.can_delays):
        raise TypeError("Scenario can_delays require plain integer times and member strings")
    if type(manifest.arena) is not ArenaManifest:
        raise TypeError("Scenario requires an ArenaManifest")
    if type(manifest.duration_ns) is not int:
        raise TypeError("Scenario duration_ns must be a plain integer")
    if not 1 <= manifest.duration_ns <= MAX_TIME_NS:
        raise ValueError("Scenario duration_ns must be in 1ns..3600s")
    validate_assertions(manifest.assertions, arena=manifest.arena, duration_ns=manifest.duration_ns)
    if (type(manifest.referees) is not tuple
            or any(type(item) is not ScenarioReferee for item in manifest.referees)):
        raise TypeError("Scenario referees require a plain ScenarioReferee tuple")
    if any(type(item.member) is not str or type(item.events) is not tuple
           for item in manifest.referees):
        raise TypeError("Scenario referees require plain member strings and event tuples")
    if on_observation is not None and not callable(on_observation):
        raise TypeError("Arena observation sink must be callable")
    batches = tuple((item.member, item.events) for item in manifest.referees)
    actions = tuple((item.offset_ns, item.member, item.delay_ns)
                    for item in manifest.can_delays)
    can_options = {"can_delays": actions} if actions else {}
    feedback = tuple((item.offset_ns, item.member, item.motor_id, item.enabled)
                     for item in manifest.motor_feedback)
    if feedback:
        can_options["motor_feedback"] = feedback
    if on_observation is not None:
        can_options["on_observation"] = on_observation

    experiments = {}
    if manifest.assertions:
        # Do not hide an invalid caller callback behind the response wrapper.
        for callback, label in ((emit, "output sink"), (on_response, "response observer")):
            if callback is not None and not callable(callback):
                raise TypeError(f"Arena {label} must be callable")

        from sim.lab.experiment import LabExperiment
        from sim.lab.manifest import LabManifest
        from sim.transports import step_codec as codec

        grouped = {}
        for item in manifest.assertions:
            grouped.setdefault(item.member, []).append(item.assertion)
        labs = {member.name: LabManifest(manifest.path, member.name, "dm-mc02",
                    member.robot.board.firmware, manifest.duration_ns, (), tuple(grouped[member.name]))
                for member in manifest.arena.robots if member.name in grouped}

        def response(name, frame, decoded):
            if name in labs and frame.header.kind == codec.RESET_ACK:
                if name in experiments:
                    raise RuntimeError(f"Scenario member {name!r} received another RESET_ACK")
                experiments[name] = LabExperiment(labs[name], decoded.virtual_time_ns)
            if on_response is not None:
                on_response(name, frame, decoded)

        def observe(item):
            if on_observation is not None:
                on_observation(item)
            if item.name not in labs:
                return
            if item.name not in experiments:
                raise RuntimeError(f"Scenario member {item.name!r} has no RESET_ACK origin")
            experiment = experiments[item.name]
            experiment.observe(item.snapshot, item.events, item.snapshot["timestamp_ns"])
            if experiment.failures:
                raise RuntimeError(f"Scenario member {item.name!r}: " + "; ".join(experiment.failures))

        can_options["on_observation"] = observe

    from sim.arena.runtime import run_manifest as run_arena

    # The bounded binary64 round trip has <0.000472ns error; Arena's existing
    # round(seconds * 1e9) recovers every admitted integer nanosecond duration.
    result = run_arena(manifest.arena, duration=manifest.duration_ns / 1_000_000_000,
        referee_events=batches, emit=emit,
        on_response=response if manifest.assertions else on_response, qemu=qemu,
        startup_timeout=startup_timeout, timeout=timeout,
        referee_period_ns=referee_period_ns, **can_options)
    if manifest.assertions:
        boards = {board.name: board for board in result.boards}
        for name in labs:
            if name not in experiments or name not in boards:
                raise RuntimeError(f"Scenario member {name!r} lacks final assertion observations")
            experiment = experiments[name]
            if (boards[name].reset_qemu_ns != experiment.origin_ns
                    or not boards[name].observation_batches):
                raise RuntimeError(f"Scenario member {name!r} has inconsistent assertion observations")
            experiment.finish(boards[name].end_qemu_ns)
            if not experiment.all_passed:
                raise RuntimeError(f"Scenario member {name!r}: " + "; ".join(experiment.failures))
    return result
