"""Independent board exchanges around one owned joint world.

The runtime borrows board endpoints and all nonblocking streams. One Coordinator
owns the existing CAN/plant participant and optional referee engines; referee
endpoints have a separate sequence lifecycle. This is a synchronous host
composition. Explicit execution bindings additionally coordinate native CPU
targets and paused CAN transport; they do not close equal-time timers or create
a global physics clock. Execution failure is terminal except
snapshot capacity refusal: recreate the owner and boards, never replay a tick.
Pure admission refusal preserves runtime readiness.
Callbacks may observe state but must not reenter execution or cleanup.
"""

from dataclasses import dataclass
import math
import re
import select
import socket
import time
from typing import Callable

from sim.boards import (
    drain_paused_can, observe_initial_paused_can, wait_paused_can_input,
)
from sim.core import Coordinator, Session
from sim.devices.bmi088 import imu_to_bmi088_units
from sim.devices.dji import M3508JointPlant
from sim.devices.dji.protocol import validate_motor_id
from sim.devices.referee import RefereeEngine, RefereeUartEndpoint, ScriptEvent
from sim.fabric import CanBus, CanFrame
from sim.fabric.can import check_can_time
from sim.fabric.endpoints import QemuCanEndpoint
from sim.fabric.runtime import CanParticipant
from sim.transports.can_stream import CanStream
from sim.transports.cosim import (
    QemuTimestampMapper, StepFramedSocket, V2SessionReset,
    poll_v2_response, send_v2_step, wait_v2_response,
)
from sim.transports import step_codec as codec


@dataclass(frozen=True)
class BoardExecution:
    """Borrow a bounded native QMP command callable and CAN identities.

    The caller excludes reset, restore, rail/configuration changes, reconnect,
    extra CAN input and other CPU execution owners throughout the attempt.
    ``command(name, arguments=None)`` must have its own finite command bound.
    Optional ``command_many(requests)`` must have its own finite group bound;
    it is used only for one fresh independent CAN observation.
    Guards surround each scalar call or whole group; they cannot preempt a
    blocked borrowed callable.
    """

    command: Callable
    controller: str
    chardev: str
    command_many: Callable | None = None


@dataclass(frozen=True)
class BoardBinding:
    name: str
    endpoint: QemuCanEndpoint
    cosim: StepFramedSocket
    imu: str
    execution: BoardExecution | None = None


@dataclass(frozen=True)
class _ExecutionCommands:
    """Expose guarded borrowed callables to the existing board helpers."""

    command: Callable
    command_many: Callable | None = None


@dataclass(frozen=True)
class RefereeBinding:
    name: str
    engine: RefereeEngine
    endpoint: RefereeUartEndpoint


@dataclass(frozen=True)
class _MotorFeedbackPolicy:
    bus: CanBus
    original_drop: Callable | None
    drop: Callable
    blocked: set[int]


def _finite_seconds(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be finite")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")
    return value


class SharedWorldRuntime:
    """Own one plant and optional referees, borrowing board/UART transports.

    Each referee engine shares the sole Coordinator's Session and StepContext.
    Endpoint sequence reset is separate; startup preserves its current sequence
    and physical queued bytes. No snapshot is sent automatically. After observing
    firmware UART readiness, the caller explicitly publishes a completed state.

    ``on_response(name, frame, decoded)`` sees returned RESET/STEP ACKs and
    idle observations, including actual advisory DONE masks/status. Existing
    active waits may skip advisory frames; this is not a complete frame trace.
    Absolute host deadlines raise RuntimeError so the canonical STEP helper
    cannot swallow them as an internal retry timeout. A TimeoutError from wait
    servicing also becomes RuntimeError with the original exception as cause;
    only the helper's own ACK timeout may trigger retry. Native dependencies must
    return: this synchronous owner cannot preempt a blocked solver/callback.
    """

    def __init__(self, plant: M3508JointPlant,
                 bindings: tuple[BoardBinding, ...], *,
                 referees: tuple[RefereeBinding, ...] = (),
                 check_running=None, on_response=None):
        if not isinstance(plant, M3508JointPlant) or plant.closed:
            raise TypeError("shared world requires an open M3508 joint plant")
        if (type(bindings) is not tuple or not bindings
                or any(type(item) is not BoardBinding for item in bindings)):
            raise TypeError("shared world requires a nonempty plain BoardBinding tuple")
        if (type(referees) is not tuple
                or any(type(item) is not RefereeBinding for item in referees)):
            raise TypeError("shared world requires a plain RefereeBinding tuple")
        for callback, name in ((check_running, "check_running"),
                               (on_response, "on_response")):
            if callback is not None and not callable(callback):
                raise TypeError(f"{name} must be callable or None")
        for item in bindings:
            if (not isinstance(item.name, str) or not item.name or "\0" in item.name
                    or not isinstance(item.imu, str) or not item.imu or "\0" in item.imu):
                raise ValueError("board names and IMU bindings must be nonempty without NUL")
            if not isinstance(item.endpoint, QemuCanEndpoint):
                raise TypeError("board endpoint requires QemuCanEndpoint")
            if not isinstance(item.endpoint.stream, CanStream):
                raise TypeError("board CAN requires the canonical CanStream")
            if not isinstance(item.cosim, StepFramedSocket):
                raise TypeError("board co-sim requires StepFramedSocket")
            if item.execution is not None:
                if type(item.execution) is not BoardExecution:
                    raise TypeError("board execution requires BoardExecution or None")
                if not callable(item.execution.command):
                    raise TypeError("board execution command must be callable")
                if (item.execution.command_many is not None and
                        not callable(item.execution.command_many)):
                    raise TypeError("board execution command_many must be callable or None")
                for name in ("controller", "chardev"):
                    value = getattr(item.execution, name)
                    if (not isinstance(value, str) or
                            re.fullmatch(r"[A-Za-z0-9_.-]+", value) is None):
                        raise ValueError(f"{name} must be a nonempty native identity")
                stream = item.endpoint.stream
                if (type(stream) is not CanStream or
                        type(stream.sock) is not socket.socket or
                        stream.sock.family != socket.AF_UNIX or
                        stream.sock.type != socket.SOCK_STREAM or
                        stream.sock.fileno() < 0 or stream.sock.getblocking()):
                    raise ValueError(
                        "execution requires canonical CanStream on a nonblocking AF_UNIX stream")
                budget = item.endpoint.bus.dispatch_budget
                if type(budget) is not int or budget <= 0:
                    raise ValueError("execution requires a positive fixed CAN dispatch budget")
        executions = tuple(item.execution for item in bindings)
        if any(item is not None for item in executions) and any(
                item is None for item in executions):
            raise ValueError("board execution bindings must be all present or all absent")
        for item in referees:
            if not isinstance(item.name, str) or not item.name or "\0" in item.name:
                raise ValueError("referee names must be nonempty without NUL")
            if not isinstance(item.engine, RefereeEngine) or item.engine.closed:
                raise TypeError("referee binding requires an open RefereeEngine")
            if not isinstance(item.endpoint, RefereeUartEndpoint) or item.endpoint.closed:
                raise TypeError("referee binding requires an open RefereeUartEndpoint")
        for values, label in ((tuple(item.name for item in referees), "referee name"),
                              (tuple(id(item.engine) for item in referees), "referee engine"),
                              (tuple(id(item.endpoint) for item in referees), "referee endpoint")):
            if len(set(values)) != len(values):
                raise ValueError(f"each {label} must appear once")
        buses = tuple(item.endpoint.bus for item in bindings)
        streams = tuple(stream for item in bindings
                        for stream in (item.endpoint.stream, item.cosim)) + tuple(
                            item.endpoint.transport for item in referees)
        if len({item.name for item in bindings}) != len(bindings):
            raise ValueError("each board name must appear once")
        if len({id(bus) for bus in buses}) != len(buses):
            raise ValueError("each board CAN bus must appear once")
        if len({id(stream) for stream in streams}) != len(streams):
            raise ValueError("each borrowed stream must appear once")
        sockets = tuple(stream.sock for stream in streams)
        if (any(sock.fileno() < 0 or sock.gettimeout() != 0 for sock in sockets)
                or len({sock.fileno() for sock in sockets}) != len(sockets)):
            raise ValueError("borrowed sockets must be open, nonblocking and unique")
        if (any(device.closed for device in plant.devices)
                or {id(device.bus) for device in plant.devices} != {id(bus) for bus in buses}):
            raise ValueError("open plant devices must represent exactly the board buses")
        getter = getattr(plant.physics, "get_imu", None)
        if not callable(getter):
            raise TypeError("shared world physics requires get_imu")
        exact_execution = executions[0] is not None
        can_wiring = tuple(
            (item.endpoint.bus, item.endpoint.stream, item.endpoint.stream.sock,
             item.endpoint.bus.dispatch_budget) for item in bindings)
        # Getters are public, completed-state observations. No wiring, reset,
        # solver, transport or ownership effect occurs before full admission.
        for item in bindings:
            gyro, accel = imu_to_bmi088_units(getter(item.imu))
            codec.encode_imu_sample_v2(codec.ImuSampleV2(
                gyro, accel, (0.0,) * 3, (0.0,) * 3, 0, 1))
            if exact_execution:
                for board, (bus, stream, sock, budget) in zip(bindings, can_wiring):
                    if (board.endpoint.bus is not bus or board.endpoint.stream is not stream or
                            stream.sock is not sock or type(bus.dispatch_budget) is not int or
                            bus.dispatch_budget != budget):
                        raise RuntimeError(
                            "shared world execution CAN wiring or dispatch budget changed")
        coordinator = Coordinator({"can": CanParticipant(buses, (plant,)),
                                   **{"referee:" + item.name: item.engine for item in referees}})
        self.bindings = bindings
        self.referees = referees
        self.plant = plant
        self._coordinator = coordinator
        self._check_running = check_running
        self._on_response = on_response
        self._attempted = False
        self._failed = True
        self._busy = False
        self._rx = ()
        self._tx = ()
        self._mappers = ()
        self._exact_execution = exact_execution
        self._can_wiring = can_wiring
        self._can_baselines = ()
        self._can_input_states = ()
        self._motor_feedback_policies = {}

    @property
    def t_sim_ns(self):
        return self._coordinator.t_sim_ns

    @property
    def step_id(self):
        return self._coordinator.step_id

    @property
    def failed(self):
        return self._failed

    @property
    def closed(self):
        return self._coordinator.closed

    @property
    def can_input_states(self):
        """Return the last complete native input observation group, if owned.

        Counts include native rejects; ``rx_dropped`` is retained diagnostic
        evidence, not firmware consumption. A failed attempt leaves the last
        complete group available while preserving all subsequent native effects.
        """
        return tuple(dict(state) for state in self._can_input_states)

    def _guard(self, deadline):
        self._check_execution_wiring()
        if self._check_running is not None:
            self._check_running()
        self._check_execution_wiring()
        if time.monotonic() >= deadline:
            raise RuntimeError("shared world host deadline expired")

    def _check_execution_wiring(self):
        if self._motor_feedback_policies and any(
                policy.bus.drop is not policy.drop
                for policy in self._motor_feedback_policies.values()):
            raise RuntimeError("shared world CAN feedback drop policy changed")
        if not self._exact_execution:
            return
        for item, (bus, stream, sock, budget) in zip(self.bindings, self._can_wiring):
            if (item.endpoint.bus is not bus or item.endpoint.stream is not stream or
                    stream.sock is not sock or type(bus.dispatch_budget) is not int or
                    bus.dispatch_budget != budget or sock.fileno() < 0 or
                    sock.getblocking()):
                raise RuntimeError("shared world execution CAN wiring or dispatch budget changed")

    def _execution_io(self, index, deadline):
        item = self.bindings[index]

        def command(name, arguments=None):
            self._guard(deadline)
            result = item.execution.command(name, arguments)
            self._guard(deadline)
            return result

        command_many = None
        if item.execution.command_many is not None:
            def command_many(requests):
                self._guard(deadline)
                result = item.execution.command_many(requests)
                self._guard(deadline)
                return result

        return {
            "qmp": _ExecutionCommands(command, command_many), "stream": item.endpoint.stream,
            "controller": item.execution.controller, "chardev": item.execution.chardev,
            "deadline": deadline, "check": lambda: self._guard(deadline),
        }

    def _input_goal(self, index):
        baseline = self._can_baselines[index]
        count = self.bindings[index].endpoint.submitted_frames
        if type(count) is not int or count < baseline["submitted_frames"]:
            raise RuntimeError("shared world CAN submission count moved backwards")
        goal = baseline["completed_frames"] + count - baseline["submitted_frames"]
        if goal > 0xfffffffffffffffe:
            raise ValueError("shared world CAN input goal exceeds native counter headroom")
        return goal

    def _wait_can_input(self, index, target, deadline):
        rx = wait_paused_can_input(
            **self._execution_io(index, deadline), target_ns=target,
            completed_frames=self._input_goal(index))
        if rx["rx_dropped"] < self._can_input_states[index]["rx_dropped"]:
            raise RuntimeError("shared world CAN input drop count moved backwards")
        return rx

    def _run_cpus(self, targets, deadline):
        # Revalidate the previous cut before any new CPU is released. The first
        # cut has idle/no-target semantics, not a fabricated reached target.
        starts = tuple(mapper.unmap(self.t_sim_ns, mapper.qemu_origin_ns)
                       for mapper in self._mappers)
        for index, start in enumerate(starts):
            if self.step_id:
                self._wait_can_input(index, start, deadline)
            else:
                observed = observe_initial_paused_can(
                    **self._execution_io(index, deadline), timestamp_ns=start)
                baseline = self._can_baselines[index]
                if (any(observed["tx"][field] != baseline[field]
                        for field in ("tx_dropped", "tx_short_writes")) or
                        any(observed["rx"][field] != baseline[field]
                            for field in ("completed_frames", "rx_dropped")) or
                        self.bindings[index].endpoint.submitted_frames !=
                        baseline["submitted_frames"]):
                    raise RuntimeError("shared world initial CAN baseline changed")
        commands = tuple(self._execution_io(index, deadline)["qmp"].command
                         for index in range(len(self.bindings)))
        # Admission replies do not prove completion. Submit every target before
        # asking any board whether its native run actually reached that target.
        for command, target in zip(commands, targets):
            command("x-run-to-virtual-time", {"target-ns": target})
        pending = set(range(len(self.bindings)))
        previous = list(starts)
        while pending:
            for index in tuple(sorted(pending)):
                execution = commands[index]("x-query-virtual-execution")
                if not isinstance(execution, dict):
                    raise ValueError("native execution must be an object")
                current = execution.get("current-ns")
                target = execution.get("target-ns")
                if (type(current) is not int or type(target) is not int or
                        not 0 <= current <= 0x7fffffffffffffff or
                        target != targets[index]):
                    raise RuntimeError("native CPU execution time or target is invalid")
                if not previous[index] <= current <= target:
                    raise RuntimeError("native CPU execution reversed or overshot its target")
                previous[index] = current
                state = execution.get("state")
                if state not in ("running", "reached"):
                    raise RuntimeError("native CPU execution was cancelled or is malformed")
                if state == "reached":
                    if current != target:
                        raise RuntimeError("native CPU reached a different target")
                    status = commands[index]("query-status")
                    if (not isinstance(status, dict) or status.get("running") is not False or
                            status.get("status") != "paused"):
                        raise RuntimeError("native CPU must be paused at its reached target")
                    pending.remove(index)
            if pending:
                self._service(deadline, 0.001)
        # Every output producer is now quiescent. All canonical drains precede
        # the same sole world step; interior timestamps keep the existing bus
        # sampled-target policy and cannot retroactively affect this interval.
        for index, target in enumerate(targets):
            endpoint = self.bindings[index].endpoint

            def sink(frame, endpoint=endpoint):
                can_id, flags, dlc, timestamp, data = frame
                endpoint.ingest(CanFrame(can_id, flags, dlc, data, timestamp), self.t_sim_ns)

            drain_paused_can(
                **self._execution_io(index, deadline), sink=sink, target_ns=target,
                tx_dropped=self._can_baselines[index]["tx_dropped"])

    def _observe(self, index, frame, decoded):
        if self._on_response is not None:
            self._on_response(self.bindings[index].name, frame, decoded)

    def _poll(self, index):
        response = poll_v2_response(self.bindings[index].cosim, self._rx[index])
        if response is None:
            return
        frame, decoded = response
        self._observe(index, frame, decoded)
        if frame.header.kind == codec.RESET:
            raise V2SessionReset(frame.header.step_id, frame.header.t_sim_ns,
                                 frame.header.session_id)

    def _service(self, deadline, timeout, *, active=None, acked=None):
        self._guard(deadline)
        # During start, only already-ACKed peers may be polled. Other RESET_ACKs
        # remain physically queued for their exclusive active wait. CAN RX is
        # also held until every origin is bound and the sole world reset ends.
        idle = (tuple(i for i in range(len(self.bindings)) if i != active)
                if acked is None else tuple(i for i in acked if i != active))
        for index in idle:
            self._poll(index)
            self._guard(deadline)
        readable = [item.cosim.sock for item in self.bindings]
        writable = [item.cosim.sock for item in self.bindings if item.cosim.tx_pending]
        if acked is None and not self._exact_execution:
            readable.extend(item.endpoint.stream.sock for item in self.bindings)
            writable.extend(item.endpoint.stream.sock for item in self.bindings
                            if item.endpoint.stream.tx_pending)
        if acked is None:
            writable.extend(item.endpoint.transport.sock for item in self.referees
                            if item.endpoint.transport.tx_pending)
        _, ready_write, _ = select.select(
            readable, writable, [], min(timeout, max(0, deadline - time.monotonic())))
        self._guard(deadline)
        for item in self.bindings:
            if item.cosim.sock in ready_write:
                item.cosim.flush()
            if (acked is None and not self._exact_execution and
                    item.endpoint.stream.sock in ready_write):
                item.endpoint.stream.flush()
            self._guard(deadline)
        if acked is None:
            for item in self.referees:
                if item.endpoint.transport.sock in ready_write:
                    item.endpoint.flush()
                self._guard(deadline)
        # One canonical CAN read per stream; parsing/ingress never advances
        # time. The bus retains original ordering/future delivery and queues.
        if acked is None and not self._exact_execution:
            for item in self.bindings:
                for can_id, flags, dlc, timestamp, data in item.endpoint.stream.receive():
                    item.endpoint.ingest(CanFrame(can_id, flags, dlc, data, timestamp),
                                         self.t_sim_ns)
                self._guard(deadline)

    def _wait_service(self, deadline, timeout, active):
        try:
            self._service(deadline, timeout, active=active)
        except TimeoutError as exc:
            # The canonical STEP helper retries its own ACK wait timeouts.
            # Callback/transport failures are terminal, even if transient.
            raise RuntimeError("shared world wait service failed") from exc

    def start(self, session: Session, wire_sessions: tuple[int, ...], *, deadline):
        """Return all real RESET_ACKs, then bind epochs and reset the world once."""
        if self.closed or self._busy or self._attempted:
            raise RuntimeError("shared world start requires a fresh owner")
        self._attempted = True
        self._busy = True
        try:
            deadline = _finite_seconds(deadline, "deadline")
            if not isinstance(session, Session):
                raise TypeError("shared world start requires a Session")
            check_can_time(session.t_sim_ns)
            if (type(wire_sessions) is not tuple
                    or len(wire_sessions) != len(self.bindings)
                    or any(type(value) is not int or not 0 < value <= codec.UINT32_MAX
                           for value in wire_sessions)):
                raise ValueError("wire sessions must be a matching plain tuple of nonzero u32 IDs")
            resets = tuple(codec.build_step_frame(
                codec.RESET, 0, session.t_sim_ns, session_id=value)
                for value in wire_sessions)
            tx = tuple(codec.StepSessionValidator.host_to_qemu() for _ in self.bindings)
            rx = tuple(codec.StepSessionValidator.qemu_to_host(require_step_done=False)
                       for _ in self.bindings)
            for validator_tx, validator_rx, wire, value in zip(tx, rx, resets, wire_sessions):
                validator_tx.accept(codec.decode_step_frame(wire))
                validator_rx.begin_reset(0, session.t_sim_ns, value)
            self._tx, self._rx = tx, rx
            self._guard(deadline)
            for item, wire in zip(self.bindings, resets):
                self._guard(deadline)
                item.cosim.send(wire)
                self._guard(deadline)
            responses = []
            mappers = []
            for index, item in enumerate(self.bindings):
                self._guard(deadline)
                response = wait_v2_response(
                    item.cosim, rx[index], codec.RESET_ACK, 0, session.t_sim_ns,
                    timeout=max(0, deadline - time.monotonic()), interrupt_on_reset=True,
                    service_io=lambda timeout, active=index: self._service(
                        deadline, timeout, active=active, acked=range(len(responses))))
                self._guard(deadline)
                ack = codec.decode_reset_ack_payload(response.payload)
                required = codec.CAP_STEP | codec.CAP_IMU
                if ack.capabilities & required != required:
                    raise RuntimeError("board RESET_ACK lacks STEP/IMU capability")
                mapper = QemuTimestampMapper(ack.virtual_time_ns, session.t_sim_ns)
                check_can_time(mapper.qemu_origin_ns)
                responses.append(response)
                mappers.append(mapper)
                self._observe(index, response, ack)
                self._guard(deadline)
            self._mappers = tuple(mappers)
            self._service(deadline, 0, acked=range(len(self.bindings)))
            for item, mapper in zip(self.bindings, mappers):
                item.endpoint.bind_mapper(mapper)
            self._guard(deadline)
            self._coordinator.reset(session)
            for item in self.referees:
                self._guard(deadline)
                # Logical reset preserves configured sequence and physical
                # queued bytes/suffixes; it does not isolate old UART input.
                item.endpoint.reset(item.endpoint.next_sequence)
            self._guard(deadline)
            if self._exact_execution:
                baselines, input_states = [], []
                for index, mapper in enumerate(mappers):
                    submitted = self.bindings[index].endpoint.submitted_frames
                    observed = observe_initial_paused_can(
                        **self._execution_io(index, deadline),
                        timestamp_ns=mapper.qemu_origin_ns)
                    if self.bindings[index].endpoint.submitted_frames != submitted:
                        raise RuntimeError("shared world initial CAN submissions changed")
                    baselines.append({
                        **{name: observed["tx"][name]
                           for name in ("tx_dropped", "tx_short_writes")},
                        **{name: observed["rx"][name]
                           for name in ("completed_frames", "rx_dropped")},
                        "submitted_frames": submitted,
                    })
                    input_states.append(dict(observed["rx"]))
                self._guard(deadline)
                self._can_baselines = tuple(baselines)
                self._can_input_states = tuple(input_states)
            self._failed = False
            return tuple(responses)
        finally:
            self._busy = False

    def _enter(self):
        if self.closed or self._busy or self.failed:
            raise RuntimeError("shared world requires a successful start and fresh runtime")
        self._busy = True
        self._failed = True

    def service(self, *, deadline, timeout=0):
        """Service one bounded pass without advancing the world or borrowed clocks."""
        self._enter()
        try:
            deadline = _finite_seconds(deadline, "deadline")
            timeout = _finite_seconds(timeout, "timeout")
            if timeout < 0:
                raise ValueError("timeout must be nonnegative")
            self._service(deadline, timeout)
            self._guard(deadline)
            self._failed = False
        finally:
            self._busy = False

    def publish_referee(self, name: str, *, deadline) -> bytes:
        """Admit one completed snapshot; caller decides when UART is ready.

        No world or engine is advanced. Capacity refusal may drain old bytes
        but admits no new snapshot/sequence and permits retry. Other failures
        are terminal, including liveness/deadline failure after an attempted send.
        Pending output is flushed by the existing active service pass.
        """
        self._enter()
        try:
            deadline = _finite_seconds(deadline, "deadline")
            if not isinstance(name, str):
                raise TypeError("referee name must be a string")
            item = next((item for item in self.referees if item.name == name), None)
            if item is None:
                raise ValueError(f"unknown referee: {name!r}")
            self._guard(deadline)
            state = item.engine.get_state()
            try:
                wire = item.endpoint.send_state(state)
            except BufferError:
                self._guard(deadline)
                self._failed = False
                raise
            self._guard(deadline)
            self._failed = False
            return wire
        finally:
            self._busy = False

    def queue_referee_events(self, name: str, events: tuple[ScriptEvent, ...], *,
                             session_id: int) -> None:
        """Admit scoped events without stepping, publishing or servicing IO.

        Offsets use the host reset origin and session ID. Admission refusal
        preserves runtime readiness; the selected engine owns validation and
        pending events. Calls during active execution or cleanup are rejected.
        """
        if self.closed or self._busy or self.failed:
            raise RuntimeError("shared world requires a successful start and fresh runtime")
        if type(name) is not str:
            raise TypeError("referee name must be a string")
        item = next((item for item in self.referees if item.name == name), None)
        if item is None:
            raise ValueError(f"unknown referee: {name!r}")
        item.engine.queue_events(events, session_id=session_id)

    def set_can_delay(self, name: str, delay_ns: int) -> None:
        """Change one board bus's subsequent-enqueue policy without IO or ticks.

        Pending due times remain unchanged. Admission refusal preserves runtime
        readiness; the borrowed bus owns value validation and the current policy.
        Calls during active execution or cleanup are rejected.
        """
        if self.closed or self._busy or self.failed:
            raise RuntimeError("shared world requires a successful start and fresh runtime")
        if type(name) is not str:
            raise TypeError("board name must be a string")
        item = next((item for item in self.bindings if item.name == name), None)
        if item is None:
            raise ValueError(f"unknown board: {name!r}")
        item.endpoint.bus.delay_ns = delay_ns

    def set_motor_feedback_enabled(self, name: str, motor_id: int,
                                   enabled: bool) -> None:
        """Allow or drop one actual motor's feedback to its board at dispatch.

        This changes feedback reachability, not device power or command/plant
        behavior. Queued frames retain their due times; dropped frames are never
        replayed. Existing drop decisions run first and remain effective.
        The caller must not replace an installed policy. Clearing the last block
        or closing the runtime restores the original borrowed callback.
        """
        if self.closed or self._busy or self.failed:
            raise RuntimeError("shared world requires a successful start and fresh runtime")
        if type(name) is not str:
            raise TypeError("board name must be a string")
        validate_motor_id(motor_id)
        if type(enabled) is not bool:
            raise TypeError("enabled must be a boolean")
        item = next((item for item in self.bindings if item.name == name), None)
        if item is None:
            raise ValueError(f"unknown board: {name!r}")
        device = next((device for device in self.plant.devices
                       if device.bus is item.endpoint.bus and device.motor_id == motor_id), None)
        if device is None:
            raise ValueError(f"unknown motor {motor_id} on board {name!r}")
        self._check_execution_wiring()
        policy = self._motor_feedback_policies.get(name)
        if enabled:
            if policy is not None:
                policy.blocked.discard(id(device))
                if not policy.blocked:
                    policy.bus.drop = policy.original_drop
                    del self._motor_feedback_policies[name]
        elif policy is not None:
            policy.blocked.add(id(device))
        else:
            bus, endpoint = item.endpoint.bus, item.endpoint
            original = bus.drop
            if original is not None and not callable(original):
                raise TypeError("CAN drop must be callable or None")
            blocked = {id(device)}

            def drop(source, destination, frame):
                inherited = bool(original and original(source, destination, frame))
                return inherited or (destination is endpoint and id(source) in blocked)

            bus.drop = drop
            self._motor_feedback_policies[name] = _MotorFeedbackPolicy(
                bus, original, drop, blocked)

    def step(self, dt_ns: int, *, deadline):
        """Advance the sole world once; return actual per-board STEP_ACKs."""
        self._enter()
        try:
            deadline = _finite_seconds(deadline, "deadline")
            if type(dt_ns) is not int or not 0 < dt_ns <= 0x7FFFFFFFFFFFFFFF:
                raise ValueError("dt_ns must be a positive signed64 integer")
            target = self.t_sim_ns + dt_ns
            check_can_time(target)
            next_step = self.step_id + 1
            if next_step > codec.UINT32_MAX:
                raise ValueError("STEP sample ID must fit the unsigned 32-bit field")
            for item in self.bindings:
                check_can_time(target + item.endpoint.bus.delay_ns)
            targets = tuple(mapper.unmap(target, mapper.qemu_origin_ns)
                            for mapper in self._mappers)
            for native_target in targets:
                check_can_time(native_target)
                if self._exact_execution and native_target > 0x7fffffffffffffff:
                    raise ValueError("native CPU target must fit signed64")
            if self._exact_execution:
                self._check_execution_wiring()
                for index, (_, _, _, budget) in enumerate(self._can_wiring):
                    if self._input_goal(index) + 2 * budget > 0xfffffffffffffffe:
                        raise ValueError("shared world CAN input lacks native counter headroom")
            self._guard(deadline)
            self._service(deadline, 0)
            self._guard(deadline)
            if self._exact_execution:
                self._run_cpus(targets, deadline)
                self._service(deadline, 0)
                self._guard(deadline)
            self._coordinator.step(dt_ns)
            self._guard(deadline)
            # Collect every completed IMU and encode every outgoing frame
            # before sending any STEP; a later bad source cannot publish half
            # the board group. CAN/native effects already performed are retained.
            frames = []
            for item, validator in zip(self.bindings, self._tx):
                self._guard(deadline)
                gyro, accel = imu_to_bmi088_units(self.plant.physics.get_imu(item.imu))
                payload = codec.encode_imu_sample_v2(codec.ImuSampleV2(
                    gyro, accel, (0.0,) * 3, (0.0,) * 3, target, next_step))
                wire = codec.build_step_frame(codec.STEP, next_step, target, dt_ns,
                                             session_id=validator.session_id, payload=payload)
                frames.append((codec.decode_step_frame(wire), wire))
                self._guard(deadline)
            for validator, (frame, _) in zip(self._tx, frames):
                validator.accept(frame)
            responses = []
            for index, (item, (_, wire)) in enumerate(zip(self.bindings, frames)):
                self._guard(deadline)
                response = send_v2_step(
                    item.cosim, self._rx[index], wire, next_step, target,
                    service_io=lambda timeout, active=index: self._wait_service(
                        deadline, timeout, active))
                self._guard(deadline)
                responses.append(response)
                self._observe(index, response, codec.decode_step_ack_payload(response.payload))
                self._guard(deadline)
            if self._exact_execution:
                states = tuple(dict(self._wait_can_input(index, native_target, deadline))
                               for index, native_target in enumerate(targets))
                self._guard(deadline)
                self._can_input_states = states
            self._failed = False
            return tuple(responses)
        finally:
            self._busy = False

    def close(self):
        """Independently close owned participants/endpoints, preserving errors."""
        if self._busy:
            raise RuntimeError("shared world callbacks must not reenter cleanup")
        if self.closed:
            return
        self._failed = True
        self._busy = True
        primary = None
        try:
            for item in reversed(self.referees):
                try:
                    item.endpoint.close()
                except BaseException as exc:
                    if primary is None:
                        primary = exc
                    else:
                        primary.add_note(f"referee {item.name} endpoint close failed: {type(exc).__name__}: {exc}")
                        for note in (() if exc is primary else tuple(getattr(exc, "__notes__", ()))):
                            primary.add_note(note)
            try:
                self._coordinator.close()
            except BaseException as exc:
                if primary is None:
                    primary = exc
                else:
                    primary.add_note(f"shared world participants close failed: {type(exc).__name__}: {exc}")
                    for note in (() if exc is primary else tuple(getattr(exc, "__notes__", ()))):
                        primary.add_note(note)
            for name, policy in reversed(tuple(self._motor_feedback_policies.items())):
                try:
                    if policy.bus.drop is not policy.drop:
                        raise RuntimeError(f"CAN feedback drop policy changed for board {name!r}")
                    policy.bus.drop = policy.original_drop
                except BaseException as exc:
                    if primary is None:
                        primary = exc
                    else:
                        primary.add_note(f"CAN feedback policy cleanup failed: {type(exc).__name__}: {exc}")
                # A replacement belongs to its caller; never overwrite it.
            self._motor_feedback_policies.clear()
            if primary is not None:
                raise primary
        finally:
            self._busy = False
