"""Actual shared-world/core/transport owners with isolated external acquisition.

Socketpairs and OS pipes remain real. Only physics, QEMU/QMP acquisition and
their independent response producers are fixtures; no protocol or core state
machine is replaced. Native QEMU and MuJoCo are separate consumer gates.
"""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import builtins
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim import processes
from sim.arena import runtime
from sim.arena.manifest import load_manifest
from sim.boards import qemu as board_qmp
from sim.boards.channels import ChannelEvent
from sim.core import Session, StepContext
from sim.runtime import Simulation
from sim.runtime import simulation as simulation_runtime
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.devices.referee import Hit, RefereeEngine, RefereeUartEndpoint, RobotStatus, ScriptEvent, Shot
from sim.fabric import CanBus
from sim.machines.qemu.endpoints import QemuCanEndpoint
from sim.lab.experiment import LabExperiment
from sim.lab.manifest import Assertion, LabManifest, Ready
from sim.robots import shared_world
from sim.scenario import ScenarioCanDelay, ScenarioManifest, ScenarioReferee, load_manifest as load_scenario
from sim.scenario import runtime as scenario_runtime
from sim.transports import cosim
from tools import dm_mc02_firmware_rtf as rtf
from sim.machines.qemu import deadline as qmp_deadline
from test_arena_manifest import ROBOT_DOCUMENT, referee_configuration, write_arena, write_executable
from test_lab_runtime import snapshot
from test_robot_runtime import Pipe
from test_sim_shared_world import Boards, NativeCpuBatchQmp, NativeStub, can_wire


class Peer:
    """One actual socket, with observable acquisition and cleanup failures."""

    def __init__(self, environment, index, name, sock, producer):
        self.environment, self.index, self.name = environment, index, name
        self.sock, self.producer = sock, producer

    def __getattr__(self, name):
        return getattr(self.sock, name)

    def recv(self, maximum):
        assert maximum == 4096
        if self.name.startswith("uart"):
            chunks = self.environment.uart_port_chunks.get(
                (self.index, self.name), self.environment.uart_chunks[self.index])
            if chunks:
                self.producer.send(chunks.pop(0))
            self.environment.uart_reads[self.index] += 1
            key = self.index, self.name
            self.environment.uart_port_reads[key] = self.environment.uart_port_reads.get(key, 0) + 1
        return self.sock.recv(maximum)

    def send(self, data):
        key = self.index, self.name
        budget = self.environment.uart_send_budget.get(key)
        if budget is not None:
            if budget == 0:
                raise BlockingIOError
            written = self.sock.send(data[:budget])
            self.environment.uart_send_budget[key] -= written
            return written
        return self.sock.send(data)

    def close(self):
        label = f"peer{self.index} {self.name}"
        self.environment.events.append(label + " close")
        self.sock.close()
        if label in self.environment.close_failures:
            raise OSError(label + " close failure")


class Environment:
    """Acquire real borrowed pipes/streams and an actual eight-device plant."""

    def __init__(self, tmp_path, monkeypatch):
        self.directory, self.monkeypatch = tmp_path, monkeypatch
        self.binary = write_executable(tmp_path / "qemu")
        self.clock = 100.0
        self.events, self.owners, self.launches, self.children, self.waits = [], [], [], [], []
        self.close_failures, self.owner_failures = set(), {}
        self.owner_entry_failure = self.connection_failure = self.spawn_failure = None
        self.owner_delays, self.connect_delay, self.spawn_delay = [0, 0], 0, 0
        self.plant_failure = self.stop_failure = None
        self.world_close_failure = None
        self.pause_callback = lambda index: None
        self.qmp_callback = lambda index, name: None
        self.batch_calls, self.qmp_timeouts = [], []
        self.batch_after = lambda index, commands, values: None
        self.uart_chunks, self.uart_reads = [[], []], [0, 0]
        self.uart_port_chunks, self.uart_port_reads, self.uart_send_budget = {}, {}, {}
        self.can_commands = [None, None]
        self.can_intervals = [{}, {}]
        self.cpus, self.execution_events = {}, []
        self.snapshots = [[], []]
        self.sockets, self.producers = {}, {}
        self.boards = self.plant = None
        self.native = NativeStub()
        native_close = self.native.close
        def close_world():
            self.events.append("native close")
            native_close()
            if self.world_close_failure is not None:
                raise self.world_close_failure
        self.native.close = close_world
        self.native.samples = {"alpha": self.native.samples["left"],
                               "bravo": self.native.samples["right"]}
        monkeypatch.setattr(runtime, "create_arena_plant", self.create_plant)
        monkeypatch.setattr(runtime, "running_qemu", self.owner)
        monkeypatch.setattr(runtime, "QmpSession", self.connect_qmp)
        monkeypatch.setattr(runtime, "connect_unix", self.connect)
        monkeypatch.setattr(runtime.subprocess, "Popen", self.popen)
        monkeypatch.setattr(runtime, "stop", self.stop)
        clock = SimpleNamespace(monotonic=lambda: self.clock,
                                monotonic_ns=lambda: round(self.clock * 1e9))
        for module in (runtime, shared_world, board_qmp, cosim, rtf, qmp_deadline):
            monkeypatch.setattr(module, "time", clock)
        monkeypatch.setattr(simulation_runtime, 'monotonic', clock.monotonic)
        monkeypatch.setattr(runtime, "select", SimpleNamespace(select=self.select), raising=False)

    def create_plant(self, manifest):
        self.events.append("plant acquire")
        if self.plant_failure is not None:
            raise self.plant_failure
        bindings = {}
        for member in manifest.robots:
            bus = CanBus()
            for motor in member.robot.devices:
                bindings[M3508Device(bus, motor.motor_id)] = member.prefix + motor.joint
        self.plant = M3508JointPlant(self.native, bindings)
        return self.plant

    @contextmanager
    def owner(self, command, *, sockets, startup_timeout, **kwargs):
        index = len(self.owners)
        self.events.append(f"owner{index} start")
        started = self.clock
        process = SimpleNamespace(returncode=None)
        process.poll = lambda: self.poll(index, process)
        owner = SimpleNamespace(start_ns=round(started * 1e9), process=process,
                                sockets={name: self.directory / f"peer{index}" / name
                                         for name in sockets})
        self.owners.append((owner, list(command), tuple(sockets), startup_timeout))
        self.clock += self.owner_delays[index]
        primary = None
        try:
            if self.owner_entry_failure == index:
                raise OSError(f"owner{index} startup failure")
            yield owner
        except BaseException as error:
            primary = error
            raise
        finally:
            self.events.append(f"owner{index} cleanup")
            error = self.owner_failures.get(index)
            if error is not None:
                if primary is not None:
                    primary.add_note(f"owner{index} cleanup failure: {error}")
                else:
                    raise error

    def poll(self, index, process):
        if self.boards is not None:
            self.boards()
        return process.returncode

    def connect_qmp(self, path, **kwargs):
        index = int(Path(path).parent.name[-1])
        self.events.append(f"peer{index} qmp connect")
        self.clock += self.connect_delay
        if self.connection_failure == (index, "qmp"):
            raise OSError(f"peer{index} qmp connection failure")
        return SimpleNamespace(command=lambda name, arguments=None: self.command(index, name, arguments),
                               command_many=lambda commands: self.command_many(index, commands),
                               settimeout=lambda value: self.qmp_timeouts.append((index, value)),
                               close=lambda: self.close_qmp(index))

    def connect(self, path, timeout, *, wrapped_can=False):
        index, name = int(Path(path).parent.name[-1]), Path(path).name
        self.events.append(f"peer{index} {name} connect")
        self.clock += self.connect_delay
        if self.connection_failure == (index, name):
            raise OSError(f"peer{index} {name} connection failure")
        left, right = socket.socketpair()
        for sock in (left, right):
            sock.setblocking(False)
        # Native execution admission needs this exact original AF_UNIX socket.
        # Direct observer isolation alone opts into a physical-write wrapper.
        value = left if name == "can1" and not wrapped_can else Peer(self, index, name, left, right)
        self.sockets[index, name], self.producers[index, name] = value, right
        if name == "can1":
            cpu = NativeCpuBatchQmp(index, right, (1000, 2000)[index], self.native, self.execution_events)
            cpu.output = self.can_intervals[index]
            self.cpus[index] = cpu
            if self.can_commands[index] is not None:
                self.schedule_can(index, can_wire((1000, 2000)[index], self.can_commands[index]), interval=1)
        if all((number, "cosim") in self.producers for number in range(2)):
            if self.boards is None:
                self.boards = Boards(tuple(self.producers[number, "cosim"] for number in range(2)),
                                     self.native)
        return value

    def close_qmp(self, index):
        label = f"peer{index} qmp"
        self.events.append(label + " close")
        if label in self.close_failures:
            raise OSError(label + " close failure")

    def command(self, index, name, arguments=None):
        self.qmp_callback(index, name)
        if name == "qom-get" and arguments == {"path": "/machine", "property": "board-state"}:
            stamp = self.cpus[index].current
            self.snapshots[index].append((stamp, self.uart_reads[index]))
            return snapshot(stamp)
        self.events.append(f"peer{index} {name}")
        if name == "stop":
            self.pause_callback(index)
            return
        return self.cpus[index].command(name, arguments)

    def command_many(self, index, commands):
        # The public batch uses the same acquired QMP session and deadline
        # wrapper. It has an independent producer, never a scalar dispatch loop.
        assert self.qmp_timeouts and self.qmp_timeouts[-1][0] == index
        assert self.qmp_timeouts[-1][1] > 0
        self.batch_calls.append((index, commands))
        self.events.append(f"peer{index} command_many")
        values = self.cpus[index].command_many(commands)
        self.batch_after(index, commands, values)
        return values

    def schedule_can(self, index, raw, *, interval=None):
        """The native producer emits these literal bytes inside one CPU run."""
        if interval is None:
            interval = len(self.cpus[index].requests) + 1
        self.can_intervals[index][interval] = self.can_intervals[index].get(interval, b"") + raw

    def received_can(self, index):
        return bytes(self.cpus[index].received)

    def popen(self, argv, **kwargs):
        self.launches.append((list(argv), kwargs))
        index = len(self.children)
        if self.spawn_failure == index:
            raise OSError(f"upper{index} spawn failure")
        process = SimpleNamespace(pid=1000 + index, returncode=None)
        process.poll = lambda: process.returncode
        process.stdout, process.stderr = (Pipe(self, f"upper{index} {name}")
                                          for name in ("stdout", "stderr"))
        self.children.append(process)
        self.clock += self.spawn_delay
        return process

    def stop(self, process):
        index = self.children.index(process)
        self.events.append(f"upper{index} stop")
        process.returncode = -signal.SIGTERM
        if self.stop_failure is not None:
            raise self.stop_failure

    def select(self, readers, writable, exceptional, timeout):
        self.waits.append(timeout)
        self.clock += timeout
        return [], [], []

    def manifest(self, *, ready=(None, None), ready_ports=("uart7", "uart7"),
                 upper=False, referees=None):
        documents = []
        for marker, port in zip(ready, ready_ports, strict=True):
            document = ROBOT_DOCUMENT
            if marker is not None:
                document += yaml.safe_dump({"ready": {"endpoint": "main." + port, "text": marker}})
            if upper:
                document += yaml.safe_dump({"upper_computers": {"vision": {"command": [
                    sys.executable, "literal;$HOME", "$(touch unwanted)", "", "two words"]}}})
            documents.append(document)
        return load_manifest(write_arena(self.directory, robot_documents=tuple(documents), referees=referees))

    def run(self, manifest=None, **options):
        arguments = {"qemu": self.binary, "duration": .0025, **options}
        return runtime.run_manifest(manifest or self.manifest(), **arguments)

    def assert_clean(self):
        assert self.native.closed == (1 if self.plant is not None else 0)
        assert all(peer.fileno() == -1 for peer in self.sockets.values())
        assert all(process.stdout.file.closed and process.stderr.file.closed
                   for process in self.children)
        assert all(f"owner{index} cleanup" in self.events for index in range(len(self.owners)))

    def release(self):
        for peer in self.sockets.values():
            (peer.sock if isinstance(peer, Peer) else peer).close()
        for producer in self.producers.values():
            producer.close()
        for process in self.children:
            for name in ("stdout", "stderr"):
                pipe = getattr(process, name)
                pipe.file.close()
                os.close(pipe.writer)
        if self.plant is not None and not self.plant.closed:
            self.plant.close()


@pytest.fixture
def environment(tmp_path, monkeypatch):
    value = Environment(tmp_path, monkeypatch)
    try:
        yield value
    finally:
        value.release()


def watch_referees(environment, monkeypatch):
    """Observe real owners once per call; retain original state machines and IO."""
    observed = SimpleNamespace(engines=[], endpoints=[], worlds=[], simulations=[], channels=[],
                               services=[], resets=[], steps=[], queues=[], attempts=[], publications=[], closes=[],
                               close_failures={})
    for owner, collection in ((RefereeEngine, observed.engines),
                              (RefereeUartEndpoint, observed.endpoints),
                              (shared_world.SharedWorldRuntime, observed.worlds),
                              (Simulation, observed.simulations)):
        original = owner.__init__
        def initialize(value, *args, original=original, collection=collection, **kwargs):
            original(value, *args, **kwargs)
            collection.append(value)
        monkeypatch.setattr(owner, "__init__", initialize)
    initialize_channels = runtime.LabChannels.__init__
    def channels(value, **kwargs):
        initialize_channels(value, **kwargs)
        observed.channels.append(kwargs["uarts"])
    monkeypatch.setattr(runtime.LabChannels, "__init__", channels)
    service = runtime.LabChannels.service
    def service_channels(value, timestamp_ns, **kwargs):
        observed.services.append(kwargs.get("flush_uart", True))
        return service(value, timestamp_ns, **kwargs)
    monkeypatch.setattr(runtime.LabChannels, "service", service_channels)
    reset, step = RefereeEngine.reset, RefereeEngine.step
    def reset_engine(value, session):
        observed.resets.append((value, session))
        return reset(value, session)
    def step_engine(value, context):
        observed.steps.append((value, context))
        return step(value, context)
    monkeypatch.setattr(RefereeEngine, "reset", reset_engine)
    monkeypatch.setattr(RefereeEngine, "step", step_engine)
    queue = shared_world.SharedWorldRuntime.queue_referee_events
    def queue_events(value, name, events, *, session_id):
        observed.queues.append((name, events, session_id, value.t_sim_ns,
                                tuple(environment.native.steps), tuple(environment.events)))
        return queue(value, name, events, session_id=session_id)
    monkeypatch.setattr(shared_world.SharedWorldRuntime, "queue_referee_events", queue_events)
    for owner, collection, label in ((RefereeEngine, observed.engines, "engine"),
                                     (RefereeUartEndpoint, observed.endpoints, "endpoint")):
        original = owner.close
        def close(value, original=original, collection=collection, label=label):
            name = label + str(collection.index(value))
            observed.closes.append(name)
            environment.events.append(name + " close")
            original(value)
            if name in observed.close_failures:
                raise observed.close_failures[name]
        monkeypatch.setattr(owner, "close", close)
    publish = shared_world.SharedWorldRuntime.publish_referee
    def publication(value, name, **kwargs):
        observed.attempts.append((name, value.t_sim_ns, tuple(environment.native.steps)))
        wire = publish(value, name, **kwargs)
        engine = next(item.engine for item in value.referees if item.name == name)
        observed.publications.append((name, value.t_sim_ns, wire, engine.get_state()))
        return wire
    monkeypatch.setattr(shared_world.SharedWorldRuntime, "publish_referee", publication)
    return observed


def watch_can_delays(environment, monkeypatch):
    """Observe public calls once; the real runtime and bus remain the owners."""
    observed = SimpleNamespace(worlds=[], calls=[], after_set=None)
    initialize = shared_world.SharedWorldRuntime.__init__
    def world(value, *args, **kwargs):
        initialize(value, *args, **kwargs)
        observed.worlds.append(value)
    monkeypatch.setattr(shared_world.SharedWorldRuntime, "__init__", world)
    setter = shared_world.SharedWorldRuntime.set_can_delay
    def set_delay(value, member, delay_ns):
        observed.calls.append((member, delay_ns, value.t_sim_ns,
                               tuple(environment.native.steps), tuple(environment.events),
                               tuple(environment.uart_reads)))
        result = setter(value, member, delay_ns)
        if observed.after_set is not None:
            observed.after_set(value, member, delay_ns)
        return result
    monkeypatch.setattr(shared_world.SharedWorldRuntime, "set_can_delay", set_delay)
    return observed


def watch_motor_feedback(environment, monkeypatch):
    """Record public decisions while preserving the lower dispatch owner."""
    delays = watch_can_delays(environment, monkeypatch)
    observed = SimpleNamespace(delays=delays, worlds=delays.worlds, calls=[], after_set=None)
    setter = shared_world.SharedWorldRuntime.set_motor_feedback_enabled

    def set_feedback(value, member, motor_id, enabled):
        observed.calls.append((member, motor_id, enabled, value.t_sim_ns,
                               tuple(environment.native.steps), tuple(environment.uart_reads)))
        result = setter(value, member, motor_id, enabled)
        if observed.after_set is not None:
            observed.after_set(value, member, motor_id, enabled)
        return result

    monkeypatch.setattr(shared_world.SharedWorldRuntime, "set_motor_feedback_enabled", set_feedback)
    return observed


@pytest.fixture
def arena_tool_observation(environment, monkeypatch):
    """Borrow the actual smoke observer over the existing eight-device fixture."""
    from tools import dm_mc02_arena_smoke as tool
    manifest = environment.manifest()
    report = {"native_calls": {"reset": 0, "step_ns": [], "close": 0},
              "feedback": [], "commands": [], "can_receive_errors": []}
    observer = tool.Observation(report, manifest, "normal")
    decisions, writes, streams, write_hook = [], [], [], [None]
    def acquire(actual):
        plant = environment.create_plant(actual)
        for bus in dict.fromkeys(device.bus for device in plant.devices):
            bus.log = decisions.append
        return plant
    canonical = runtime.CanStream
    def physical_can(sock):
        stream = canonical(sock)
        if isinstance(sock, Peer):
            return stream  # Direct IO isolation keeps its existing send hook.
        index = next(index for (index, name), peer in environment.sockets.items()
                     if name == "can1" and peer is sock)
        flush = stream.flush
        def physical_flush():
            before = b"".join(stream.tx_queue)
            if not before:
                return flush()
            event = report["feedback"][-1]
            decision = next(row for row in reversed(decisions) if row.destination == "qemu")
            attempt = index, decision, event["accepted"], observer.feedback_deliveries[index]
            if write_hook[0] is not None:
                write_hook[0](index, before)
            result = flush()
            after = b"".join(stream.tx_queue)
            assert before.endswith(after)
            if len(before) != len(after):
                writes.append(attempt)
            return result
        stream.flush = physical_flush
        return stream
    observer.original_world, observer.original_can = acquire, physical_can
    monkeypatch.setattr(runtime, "create_arena_plant", observer.world)
    monkeypatch.setattr(runtime, "CanStream", observer.can)
    # Select only the tool's input assertion profile. Actual current still
    # comes from literal socket commands consumed by the unchanged bus/driver.
    monkeypatch.setattr(tool, "COMMANDS", ("1000000000000000", "f000000000000000"))
    send = Peer.send
    def physical_write(peer, data):
        if peer.name == "can1":
            event = report["feedback"][-1]
            decision = next(row for row in reversed(decisions) if row.destination == "qemu")
            writes.append((peer.index, decision, event["accepted"],
                           observer.feedback_deliveries[peer.index]))
        return send(peer, data)
    monkeypatch.setattr(Peer, "send", physical_write)
    def direct():
        plant = observer.world(manifest)
        plant.reset(Session(7, 500))
        for index, bus in enumerate(dict.fromkeys(device.bus for device in plant.devices)):
            bus.reset(500)
            peer = environment.connect(environment.directory / f"peer{index}" / "can1", 1, wrapped_can=True)
            stream = observer.can(peer)
            streams.append(stream)
            endpoint = QemuCanEndpoint(bus, stream)
            endpoint.bind_mapper(cosim.QemuTimestampMapper((1000, 2000)[index], 500))
        return plant.devices[0], streams[0]
    return SimpleNamespace(tool=tool, observer=observer, report=report, manifest=manifest,
                           decisions=decisions, writes=writes, streams=streams, direct=direct,
                           write_hook=write_hook)


@pytest.mark.parametrize("invalid", ["missing-source", "source-time", "payload", "can-id"])
def test_arena_tool_observation_refuses_bad_public_delivery_before_endpoint_send(
        environment, arena_tool_observation, invalid):
    observed = arena_tool_observation
    device, stream = observed.direct()
    frame = device.feedback()
    sources = observed.observer.feedback_sources[0]
    if invalid == "missing-source":
        sources.pop(500)
    elif invalid == "source-time":
        sources[500]["world_ns"] = 501
    elif invalid == "payload":
        frame = replace(frame, data=bytes.fromhex("0000 0000 0001 19 00"))
    else:
        frame = replace(frame, can_id=0x205)
    device.bus.send(device, frame)
    with pytest.raises((AssertionError, KeyError)):
        device.bus.step(500)
    assert len(observed.decisions) == 4  # One previous log call per actual destination.
    assert observed.decisions[-1].destination == "qemu"
    assert observed.observer.feedback_deliveries == {0: None, 1: None}
    assert observed.report["feedback"] == [] and observed.writes == [] and not stream.tx_pending
    with pytest.raises(BlockingIOError):
        environment.producers[0, "can1"].recv(4096)


def test_arena_tool_observation_duplicate_source_and_drop_keep_original_missing_evidence(
        environment, arena_tool_observation):
    observed = arena_tool_observation
    device, stream = observed.direct()
    frame = device.feedback()
    source = observed.observer.feedback_sources[0][500]
    with pytest.raises(ValueError, match="duplicate feedback source"):
        device.feedback()
    assert observed.observer.feedback_sources[0] == {500: source}
    device.bus.drop = lambda origin, destination, incoming: isinstance(destination, QemuCanEndpoint)
    device.bus.send(device, frame)
    assert device.bus.step(500) == 1
    assert observed.decisions[-1].dropped and observed.decisions[-1].frame is frame
    assert observed.observer.feedback_sources[0][500] is source
    assert observed.observer.feedback_deliveries == {0: None, 1: None}
    assert observed.report["feedback"] == [] and observed.writes == [] and not stream.tx_pending
    with pytest.raises(BlockingIOError):
        environment.producers[0, "can1"].recv(4096)


def test_arena_tool_observation_source_capacity_uses_actual_completed_producer_times(arena_tool_observation):
    observed = arena_tool_observation
    device, _ = observed.direct()
    for index in range(1, 257):
        observed.observer.plant.step(StepContext(7, index, 500 + index, 1))
        device.feedback()
    sources = observed.observer.feedback_sources[0]
    assert tuple(sources) == tuple(range(501, 757))
    observed.observer.plant.step(StepContext(7, 257, 757, 1))
    with pytest.raises(BufferError, match="pending feedback source exhausted"):
        device.feedback()
    assert tuple(sources) == tuple(range(501, 757))
    assert observed.report["feedback"] == [] and observed.decisions == []


@pytest.mark.parametrize("invalid", ["flags", "dlc", "data"])
def test_arena_tool_observation_duplicate_attribution_and_wrong_send_consume_one_attempt(
        environment, arena_tool_observation, invalid):
    observed = arena_tool_observation
    device, stream = observed.direct()
    frame = device.feedback()
    device.bus.drop = lambda origin, destination, incoming: isinstance(destination, QemuCanEndpoint)
    device.bus.send(device, frame)
    device.bus.step(500)
    # Isolate the observer hook with the real frame and decision metadata;
    # changing the dropped flag here does not dispatch another bus frame.
    decision = replace(observed.decisions[-1], dropped=False)
    device.bus.log(decision)
    assert observed.observer.feedback_deliveries[0] is decision
    with pytest.raises(AssertionError, match="unconsumed Arena feedback delivery"):
        device.bus.log(decision)
    arguments = [0x201, 0, 8, 1000, bytes.fromhex("0000 0000 0000 19 00")]
    if invalid == "flags":
        arguments[1] = 1
    elif invalid == "dlc":
        arguments[2] = 7
    else:
        arguments[4] = bytes.fromhex("0000 0000 0001 19 00")
    with pytest.raises(AssertionError):
        stream.send(*arguments)
    assert observed.observer.feedback_deliveries[0] is None
    assert tuple(observed.observer.feedback_sources[0]) == (500,)
    with pytest.raises(AssertionError, match="no delivery attribution"):
        stream.send(0x201, 0, 8, 1000, frame.data)
    assert observed.report["feedback"] == [] and observed.writes == [] and not stream.tx_pending
    with pytest.raises(BlockingIOError):
        environment.producers[0, "can1"].recv(4096)


@pytest.mark.parametrize("interrupt", [False, True], ids=["io-error", "interrupt"])
def test_arena_tool_observation_io_failure_keeps_source_without_replaying_send(
        environment, monkeypatch, arena_tool_observation, interrupt):
    observed = arena_tool_observation
    device, stream = observed.direct()
    frame = device.feedback()
    source = observed.observer.feedback_sources[0][500]
    primary = KeyboardInterrupt("observer send interrupted") if interrupt else OSError("observer send failed")
    attempts = []
    def reject(peer, data):
        attempts.append(bytes(data))
        raise primary
    monkeypatch.setattr(Peer, "send", reject)
    device.bus.send(device, frame)
    with pytest.raises(type(primary)) as caught:
        device.bus.step(500)
    assert caught.value is primary and len(attempts) == 1
    assert observed.observer.feedback_sources[0][500] is source
    assert observed.observer.feedback_deliveries[0] is None and stream.tx_pending
    assert len(observed.report["feedback"]) == 1 and not observed.report["feedback"][0]["accepted"]
    with pytest.raises(AssertionError, match="no delivery attribution"):
        stream.send(0x201, 0, 8, 1000, frame.data)
    assert len(attempts) == 1 and len(observed.report["feedback"]) == 1


def test_arena_tool_observation_partial_socket_prefix_accepts_one_record_then_flushes_suffix(
        environment, arena_tool_observation):
    observed = arena_tool_observation
    device, stream = observed.direct()
    frame = device.feedback()
    environment.uart_send_budget[0, "can1"] = 17
    device.bus.send(device, frame)
    assert device.bus.step(500) == 1
    expected = struct.pack("<IIB3xQ64s", 0x201, 0, 8, 1000,
                           bytes.fromhex("0000 0000 0000 19 00").ljust(64, b"\0"))
    assert environment.producers[0, "can1"].recv(4096) == expected[:17]
    assert stream.tx_pending and observed.report["feedback"][0]["accepted"]
    assert not observed.observer.feedback_sources[0] and observed.observer.feedback_deliveries[0] is None
    with pytest.raises(AssertionError, match="no delivery attribution"):
        stream.send(0x201, 0, 8, 1000, frame.data)
    environment.uart_send_budget[0, "can1"] = 67
    assert stream.flush() and not stream.tx_pending
    assert environment.producers[0, "can1"].recv(4096) == expected[17:]
    assert len(observed.report["feedback"]) == 1
    assert [row[2:] for row in observed.writes] == [(False, None), (True, None)]


def test_arena_tool_observation_tx_capacity_keeps_unaccepted_source_and_consumes_token(
        environment, arena_tool_observation):
    observed = arena_tool_observation
    device, stream = observed.direct()
    stream.max_pending = 1
    environment.uart_send_budget[0, "can1"] = 0
    first = device.feedback()
    device.bus.send(device, first)
    device.bus.step(500)
    other = observed.observer.plant.devices[1]
    second = other.feedback()
    source = observed.observer.feedback_sources[1][500]
    other.bus.send(other, second)
    with pytest.raises(BufferError, match="TX queue is full"):
        other.bus.step(500)
    assert [row["accepted"] for row in observed.report["feedback"]] == [True, False]
    assert not observed.observer.feedback_sources[0]
    assert observed.observer.feedback_sources[1][500] is source
    assert observed.observer.feedback_deliveries[0] is None and stream.tx_pending
    with pytest.raises(AssertionError, match="no delivery attribution"):
        stream.send(0x202, 0, 8, 1000, second.data)
    assert len(observed.report["feedback"]) == 2


@pytest.mark.parametrize("delayed,via_scenario", [(False, False), (True, False), (True, True)],
                         ids=["arena-zero-delay", "arena-delay-reduction", "scenario-delay-reduction"])
def test_arena_tool_observation_actual_socket_sources_follow_overtaking_and_equal_due_fifo(
        environment, monkeypatch, arena_tool_observation, delayed, via_scenario):
    fixture = arena_tool_observation
    observed = watch_can_delays(environment, monkeypatch)
    monkeypatch.setattr(runtime, "Session", lambda identifier: Session(identifier, 500))
    environment.can_commands = ["1000 0000 0000 0000", "f000 0000 0000 0000"]
    def restore(value, member, delay):
        if value.t_sim_ns == 2_000_500:
            monkeypatch.setattr(fixture.tool, "COMMANDS", ("2000000000000000", "f000000000000000"))
            environment.schedule_can(0, can_wire(1000, "2000 0000 0000 0000"))
    observed.after_set = restore
    actions = ((0, "east", 2_000_000), (2_000_000, "east", 0)) if delayed else ()
    if via_scenario:
        scenario = ScenarioManifest(environment.directory / "unread.yaml", fixture.manifest, 4_000_000, (),
                                    (ScenarioCanDelay(0, "east", 2_000_000), ScenarioCanDelay(2_000_000, "east", 0)))
        result = scenario_runtime.run_manifest(scenario, qemu=environment.binary)
    else:
        result = environment.run(fixture.manifest, duration=.004, can_delays=actions)
    environment.assert_clean()
    assert (result.origin_ns, result.end_ns, result.steps) == (500, 4_000_500, 4)
    assert [row.reset_qemu_ns for row in result.boards] == [1000, 2000]
    assert fixture.report["native_calls"] == {"reset": 1, "step_ns": [1_000_000] * 4, "close": 1}
    assert all(not sources for sources in fixture.observer.feedback_sources.values())
    assert fixture.observer.feedback_deliveries == {0: None, 1: None}
    world, = observed.worlds
    assert all(not item.endpoint.stream.tx_pending for item in world.bindings)
    assert [item.endpoint.bus.delay_ns for item in world.bindings] == [0, 0]
    rows = fixture.report["feedback"]
    assert len(rows) == 32 and all(row["accepted"] for row in rows)
    qemu_decisions = [row for row in fixture.decisions if row.destination == "qemu"]
    assert len(qemu_decisions) == 32 and len(fixture.writes) == 32
    assert all(not accepted and token is None for _, _, accepted, token in fixture.writes)
    assert [decision for _, decision, _, _ in fixture.writes] == qemu_decisions
    east = [row for row in rows if row["peer"] == 0 and row["can_id"] == 0x201]
    if delayed:
        assert [(row["world_ns"], row["sequence"], row["due_ns"], row["delivered_ns"],
                 row["qemu_ns"], row["producer_current_raw"], row["send_live_current_raw"])
                for row in east] == [
            (1_000_500, 1, 3_000_500, 3_000_500, 3_001_000, 0, 8192),
            (3_000_500, 10, 3_000_500, 3_000_500, 3_001_000, 4096, 8192),
            (2_000_500, 5, 4_000_500, 4_000_500, 4_001_000, 0, 8192),
            (4_000_500, 14, 4_000_500, 4_000_500, 4_001_000, 8192, 8192),
        ]
        groups = ((3_001_000, "0000"), (3_001_000, "1000"),
                  (4_001_000, "0000"), (4_001_000, "2000"))
    else:
        assert [(row["world_ns"], row["due_ns"], row["delivered_ns"], row["qemu_ns"],
                 row["producer_current_raw"], row["send_live_current_raw"]) for row in east] == [
            (1_000_500, 1_000_500, 1_000_500, 1_001_000, 0, 4096),
            (2_000_500, 2_000_500, 2_000_500, 2_001_000, 4096, 4096),
            (3_000_500, 3_000_500, 3_000_500, 3_001_000, 4096, 4096),
            (4_000_500, 4_000_500, 4_000_500, 4_001_000, 4096, 4096),
        ]
        groups = ((1_001_000, "0000"), (2_001_000, "1000"),
                  (3_001_000, "1000"), (4_001_000, "1000"))
    west = [row for row in rows if row["peer"] == 1 and row["can_id"] == 0x201]
    assert [(row["world_ns"], row["due_ns"], row["delivered_ns"], row["qemu_ns"],
             row["producer_current_raw"]) for row in west] == [
        (1_000_500, 1_000_500, 1_000_500, 1_002_000, 0),
        (2_000_500, 2_000_500, 2_000_500, 2_002_000, -4096),
        (3_000_500, 3_000_500, 3_000_500, 3_002_000, -4096),
        (4_000_500, 4_000_500, 4_000_500, 4_002_000, -4096),
    ]
    for index, group in enumerate((groups, ((1_002_000, "0000"), (2_002_000, "f000"),
                                          (3_002_000, "f000"), (4_002_000, "f000")))):
        expected = b"".join(struct.pack("<IIB3xQ64s", 0x200 + motor_id, 0, 8, stamp,
            bytes.fromhex("0000 0000 " + (current if motor_id == 1 else "0000") + " 19 00").ljust(64, b"\0"))
            for stamp, current in group for motor_id in range(1, 5))
        assert environment.received_can(index) == expected


@pytest.mark.parametrize("interrupt", [False, True], ids=["io-error", "interrupt"])
def test_arena_tool_observation_send_failure_retains_source_and_original_runtime_cleanup(
        environment, monkeypatch, arena_tool_observation, interrupt):
    fixture = arena_tool_observation
    primary = KeyboardInterrupt("observer callback interrupted") if interrupt else OSError("observer callback failed")
    environment.world_close_failure = OSError("observer cleanup failed")
    environment.world_close_failure.add_note("observer nested cleanup evidence")
    calls = []
    def reject(index, data):
        calls.append((index, bytes(data)))
        raise primary
    fixture.write_hook[0] = reject
    with pytest.raises(type(primary)) as caught:
        environment.run(environment.manifest(upper=True), duration=.001)
    assert caught.value is primary
    environment.assert_clean()
    assert len(calls) == 1 and fixture.report["native_calls"] == {
        "reset": 1, "step_ns": [1_000_000], "close": 1}
    assert len(fixture.report["feedback"]) == 1 and not fixture.report["feedback"][0]["accepted"]
    assert tuple(fixture.observer.feedback_sources[0]) == (1_000_000,)
    assert fixture.observer.feedback_deliveries == {0: None, 1: None}
    assert all(process.returncode == -signal.SIGTERM for process in environment.children)
    assert "observer nested cleanup evidence" in "\n".join(primary.__notes__)


# Independent 2026V1.1.0 packed layouts and pinned crccheck CRC literals, shared
# with the existing actual RefereeTest contract; no project encoder expected.
REFEREE_WIRES = tuple(bytes.fromhex(value) for value in (
    "a50b000002010021010008070605040302017d07"
    "a50d00018d010203026400c80003006400500005ef8a"
    "a50e00028b0202000000000000000028000a001400c1da"
    "a5080003040402050900070b0d00feec0b",
    "a50b00046301003103000807060504030201f59e"
    "a50d0005ec010203024600c800030064007800060a27"
    "a50e0006ea0202000000000000000028000c001900c5f8"
    "a508000765040208110013171d0080d577",
))
# Dynamic FIFO after the static1ms replacements: Hit10, RobotHP60, Hit5,
# Shot2,3 leaves HP55/heat14,28. CRC calculated from these literal packed bytes
# with pinned crccheck, independently of the project encoder.
REFEREE_QUEUED_WIRE = bytes.fromhex(
    "a50b00046301003103000807060504030201f59e"
    "a50d0005ec010203023700c80003006400780006512c"
    "a50e0006ea0202000000000000000028000e001c000bbf"
    "a508000765040208110013171d0080d577")
REFEREE_ZERO_EVENT_WIRES = tuple(bytes.fromhex(value) for value in (
    "a50b000002010021010008070605040302017d07"
    "a50d00018d010203025a00c80003006400500005fd50"
    "a50e00028b0202000000000000000028000b001600caf5"
    "a5080003040402050900070b0d00feec0b",
    "a50b00046301002101000807060504030201feb7"
    "a50d0005ec010203025a00c80003006400500005d243"
    "a50e0006ea0202000000000000000028000b0016002c2c"
    "a5080007650402050900070b0d00fe8474",
))


def short_referee_configuration():
    configuration = referee_configuration(scripted=True)
    # Explicit complete replacements at1ms produce the established secondbatch;
    # this tests publication/admission, without borrowing a physics/rule constant.
    configuration["events"] = [dict(event, offset_ns=1_000_000)
                               for event in configuration["events"][-3:]]
    configuration["events"].append({"offset_ns": 1_000_000,
                                    "power_heat": {"buffer_energy_j": 40,
                                                   "heat_17mm": 12, "heat_42mm": 25}})
    return configuration


@pytest.mark.parametrize("invalid", ["manifest-subclass", "arena-subclass", "duration-bool",
                                    "duration-zero", "duration-overflow", "mutable-referees",
                                    "foreign-record", "member-subclass", "mutable-events",
                                    "can-list", "can-tuple-subclass", "can-foreign-record",
                                    "can-record-subclass", "can-offset-bool", "can-offset-subclass",
                                    "can-delay-bool", "can-delay-subclass", "can-member-type",
                                    "can-member-subclass"])
def test_scenario_structure_rejects_before_lower_import_or_delegation(environment, monkeypatch, invalid):
    arena = environment.manifest()
    row = ScenarioReferee("east", ())
    manifest = ScenarioManifest(environment.directory / "unread.yaml", arena, 1, (row,))
    if invalid == "manifest-subclass":
        manifest = type("ScenarioSubclass", (ScenarioManifest,), {})(
            manifest.path, arena, 1, (row,))
    elif invalid == "arena-subclass":
        manifest = replace(manifest, arena=type("ArenaSubclass", (type(arena),), {})(
            **vars(arena)))
    elif invalid.startswith("duration-"):
        manifest = replace(manifest, duration_ns={"duration-bool": True, "duration-zero": 0,
                                                 "duration-overflow": 3_600_000_000_001}[invalid])
    elif invalid == "mutable-referees":
        manifest = replace(manifest, referees=[row])
    elif invalid == "foreign-record":
        manifest = replace(manifest, referees=(SimpleNamespace(member="east", events=()),))
    elif invalid.startswith("can-"):
        first = ScenarioCanDelay(0, "west", 7)
        last = ScenarioCanDelay(1, "east", 0)
        actions = (first, last)
        if invalid == "can-list":
            actions = []
        elif invalid == "can-tuple-subclass":
            actions = type("Actions", (tuple,), {})(())
        elif invalid == "can-foreign-record":
            class Foreign:
                def __getattr__(self, name):
                    pytest.fail("foreign CAN record fields were read before identity admission")
            actions = (first, Foreign())
        elif invalid == "can-record-subclass":
            actions = (first, type("CanDelaySubclass", (ScenarioCanDelay,), {})(1, "east", 0))
        else:
            if invalid.startswith("can-offset"):
                last = replace(last, offset_ns=True if invalid.endswith("bool")
                               else type("Time", (int,), {})(1))
            elif invalid.startswith("can-delay"):
                last = replace(last, delay_ns=True if invalid.endswith("bool")
                               else type("Time", (int,), {})(0))
            else:
                last = replace(last, member=None if invalid.endswith("type")
                               else type("Member", (str,), {})("east"))
            actions = (first, last)
        manifest = replace(manifest, can_delays=actions)
    else:
        if invalid == "member-subclass":
            row = replace(row, member=type("MemberSubclass", (str,), {})("east"))
        else:
            row = replace(row, events=[])
        manifest = replace(manifest, referees=(row,))
    original_import = builtins.__import__
    def admit_import(name, *args, **kwargs):
        if name == "sim.arena.runtime":
            pytest.fail("invalid Scenario reached lower runtime import")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", admit_import)
    with pytest.raises((TypeError, ValueError)):
        scenario_runtime.run_manifest(manifest, qemu=object(), startup_timeout=0)
    assert environment.events == [] and environment.owners == [] and environment.launches == []
    assert environment.plant is None and environment.native.resets == 0 and environment.native.steps == []


@pytest.mark.parametrize("invalid", ["outer_list", "outer_subclass", "foreign_record", "record_subclass",
    "offset_bool", "offset_subclass", "motor_bool", "motor_subclass", "member_type", "member_subclass",
    "enabled_int", "enabled_none"])
def test_scenario_motor_feedback_structure_rejects_before_runtime_import_and_effects(environment, monkeypatch, invalid):
    from sim.scenario import ScenarioMotorFeedback

    first, last = ScenarioMotorFeedback(0, "east", 1, False), ScenarioMotorFeedback(1, "west", 1, True)
    actions = (first, last)
    if invalid == "outer_list":
        actions = []
    elif invalid == "outer_subclass":
        actions = type("Actions", (tuple,), {})(())
    elif invalid == "foreign_record":
        class Foreign:
            def __getattr__(self, name):
                pytest.fail("foreign feedback fields were read before complete record identity admission")
        actions = (first, Foreign())
    elif invalid == "record_subclass":
        actions = (first, type("Feedback", (ScenarioMotorFeedback,), {})(1, "west", 1, True))
    else:
        field = invalid.split("_")[0]
        value = {"offset_bool": True, "offset_subclass": type("Time", (int,), {})(1),
                 "motor_bool": True, "motor_subclass": type("MotorId", (int,), {})(1),
                 "member_type": None, "member_subclass": type("Member", (str,), {})("west"),
                 "enabled_int": 0, "enabled_none": None}[invalid]
        actions = (first, replace(last, **{("motor_id" if field == "motor" else
                                          "offset_ns" if field == "offset" else field): value}))
    manifest = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(),
                                1_000_000, (), (), actions)
    original_import = builtins.__import__
    def forbid_import(name, *args, **kwargs):
        if name == "sim.arena.runtime":
            pytest.fail("invalid Scenario feedback reached lower runtime import")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", forbid_import)
    with pytest.raises(TypeError, match="motor.feedback|motor_feedback"):
        scenario_runtime.run_manifest(manifest, qemu=object(), startup_timeout=0)
    assert environment.events == [] and environment.owners == [] and environment.launches == []
    assert environment.plant is None and environment.native.resets == 0 and environment.native.steps == []


@pytest.mark.parametrize("kind", ["list", "tuple-subclass", "foreign-record", "record-subclass",
    "foreign-assertion", "member-subclass", "deadline-bool", "deadline-zero", "after-duration",
    "uart-encoding", "uart-empty", "uart-limit", "digital-bool", "can-kind", "unknown-member",
    "unopened-uart", "quota", "emit", "on_response"])
def test_scenario_assertion_preflight_rejects_before_runtime_import_or_resource_effects(environment, monkeypatch, kind):
    from sim.scenario import ScenarioAssertion

    arena = environment.manifest()
    entry = ScenarioAssertion("west", Assertion(1_000_000, "uart1", "uart_contains", b"READY"))
    assertions = (entry,)
    options = {}
    if kind == "list":
        assertions = []
    elif kind == "tuple-subclass":
        assertions = type("Assertions", (tuple,), {})(())
    elif kind == "foreign-record":
        class Foreign:
            def __getattr__(self, name):
                pytest.fail("foreign assertion fields read before complete record admission")
        assertions = (entry, Foreign())
    elif kind == "record-subclass":
        assertions = (type("AssertionSubclass", (ScenarioAssertion,), {})(entry.member, entry.assertion),)
    elif kind == "foreign-assertion":
        assertions = (replace(entry, assertion=SimpleNamespace(**vars(entry.assertion))),)
    elif kind == "member-subclass":
        assertions = (replace(entry, member=type("Member", (str,), {})("west")),)
    elif kind.startswith("deadline-") or kind == "after-duration":
        assertions = (replace(entry, assertion=replace(entry.assertion, before_ns={
            "deadline-bool": True, "deadline-zero": 0, "after-duration": 1_000_001}[kind])),)
    elif kind.startswith("uart-"):
        assertions = (replace(entry, assertion=replace(entry.assertion, value={
            "uart-encoding": b"\xff", "uart-empty": b"", "uart-limit": b"x" * 4097}[kind])),)
    elif kind == "digital-bool":
        assertions = (replace(entry, assertion=Assertion(1, "gpio.A0", "digital_equals", True)),)
    elif kind == "can-kind":
        assertions = (replace(entry, assertion=Assertion(1, "can1", "can_seen", object())),)
    elif kind == "unknown-member":
        assertions = (replace(entry, member="missing"),)
    elif kind == "unopened-uart":
        assertions = (replace(entry, assertion=replace(entry.assertion, endpoint="uart7")),)
    elif kind == "quota":
        assertions *= 1025
    else:
        options[kind] = 1
    manifest = ScenarioManifest(environment.directory / "unread.yaml", arena, 1_000_000, (),
                                assertions=assertions)
    original_import = builtins.__import__
    def forbid_import(name, *args, **kwargs):
        if name == "sim.arena.runtime":
            pytest.fail("invalid assertion input reached the Arena runtime import")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", forbid_import)
    with pytest.raises((TypeError, ValueError)):
        scenario_runtime.run_manifest(manifest, qemu=object(), **options)
    assert environment.events == [] and environment.owners == [] and environment.launches == []
    assert environment.plant is None and environment.native.resets == 0 and environment.native.steps == []


def test_scenario_assertion_typed_validator_reuses_pure_lab_parser_without_changing_records(environment, monkeypatch):
    from sim.lab import parse_assertion
    from sim.scenario import ScenarioAssertion
    from sim.scenario import manifest as scenario

    arena = environment.manifest()
    records = (ScenarioAssertion("east", Assertion(1_000_000, "uart1", "uart_contains", b"\xc3\xa9" * 2048)),
               ScenarioAssertion("west", Assertion(1, "gpio.H15", "digital_equals", 1)))
    assert scenario_runtime.validate_assertions is scenario.validate_assertions
    calls = []
    def parse(value, index):
        calls.append((index, value))
        return parse_assertion(value, index)
    def forbidden(*args, **kwargs):
        pytest.fail("typed assertion validation acquired IO")
    monkeypatch.setattr(scenario, "parse_assertion", parse)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    assert scenario.validate_assertions(records, arena=arena, duration_ns=1_000_000) is None
    assert calls == [(0, {"before": "1000000ns", "uart_contains": {"endpoint": "uart1", "text": "é" * 2048}}),
                     (1, {"before": "1ns", "digital_equals": {"endpoint": "gpio.H15", "value": 1}})]
    assert records[0].assertion.value == b"\xc3\xa9" * 2048 and records[1].assertion.before_ns == 1
    assert environment.events == [] and environment.plant is None


def test_arena_launch_borrows_the_public_member_uart_profile(environment, monkeypatch):
    from sim.arena.manifest import member_uarts

    assert runtime.member_uarts is member_uarts
    observed = watch_referees(environment, monkeypatch)
    environment.uart_chunks = [[b"READY"], [b"READY"]]
    arena = environment.manifest(ready=("READY", "READY"), ready_ports=("uart2", "uart7"),
                                 referees={"west": referee_configuration()})
    calls = []
    def uarts(member, *, referee):
        result = member_uarts(member, referee=referee)
        calls.append((member, referee, result))
        return result
    monkeypatch.setattr(runtime, "member_uarts", uarts)
    result = environment.run(arena, duration=.001)
    environment.assert_clean()
    assert calls == [(arena.robots[0], False, ("uart2",)), (arena.robots[1], True, ("uart7",))]
    assert {(index, name) for index, name in environment.sockets if name.startswith("uart")} == {
        (0, "uart2"), (1, "uart7")}
    assert len(observed.channels) == 2 and len(observed.endpoints) == 1
    assert result.end_ns - result.origin_ns == 1_000_000


@pytest.mark.parametrize("numeric", [False, True], ids=["typed_order", "lower_owns_numeric_policy"])
def test_scenario_motor_feedback_delegates_once_with_original_metadata_and_error_identity(environment, monkeypatch, numeric):
    from sim.scenario import ScenarioMotorFeedback

    arena = environment.manifest()
    events = (ScriptEvent(0, Hit(1)),)
    actions = (ScenarioMotorFeedback(0, "west", 1, False), ScenarioMotorFeedback(0, "east", 2, True),
               ScenarioMotorFeedback(1_234_567, "east", 1, False))
    if numeric:
        actions = (ScenarioMotorFeedback(1 << 64, "unresolved", 9, False), *actions)
    manifest = ScenarioManifest(environment.directory / "unread.yaml", arena, 1_234_567,
        (ScenarioReferee("east", events),), (ScenarioCanDelay(0, "west", 17),), actions)
    calls, result, primary = [], object(), OSError("lower feedback rejected")
    def delegate(actual, **options):
        calls.append((actual, options))
        if numeric:
            raise primary
        return result
    monkeypatch.setattr(runtime, "run_manifest", delegate)
    emit, observe = lambda event: None, lambda *items: None
    options = dict(emit=emit, on_response=observe, qemu=environment.binary,
                   startup_timeout=3., timeout=7., referee_period_ns=2_000_000)
    if numeric:
        with pytest.raises(OSError) as caught:
            scenario_runtime.run_manifest(manifest, **options)
        assert caught.value is primary
    else:
        assert scenario_runtime.run_manifest(manifest, **options) is result
    assert len(calls) == 1 and calls[0][0] is arena
    assert calls[0][1] == dict(options, duration=.001234567, referee_events=(("east", events),),
        can_delays=((0, "west", 17),), motor_feedback=tuple(
            (item.offset_ns, item.member, item.motor_id, item.enabled) for item in actions))
    assert calls[0][1]["referee_events"][0][1] is events
    assert calls[0][1]["emit"] is emit and calls[0][1]["on_response"] is observe
    assert manifest.motor_feedback is actions and not environment.events


@pytest.mark.parametrize("invalid", ["offset_overflow", "motor_range", "order", "quota", "topology"])
def test_scenario_motor_feedback_numeric_policy_reaches_arena_before_resources(environment, monkeypatch, invalid):
    from sim.scenario import ScenarioMotorFeedback

    first, last = ScenarioMotorFeedback(0, "east", 1, False), ScenarioMotorFeedback(1_000_000, "west", 1, True)
    actions = (first, last)
    if invalid == "offset_overflow":
        actions = (first, replace(last, offset_ns=1 << 64))
    elif invalid == "motor_range":
        actions = (first, replace(last, motor_id=9))
    elif invalid == "order":
        actions = (last, first)
    elif invalid == "quota":
        actions = (first,) * 1025
    else:
        actions = (first, replace(last, motor_id=5))
    arena, calls = environment.manifest(), []
    manifest = ScenarioManifest(environment.directory / "unread.yaml", arena, 1_000_000, (), (), actions)
    lower = runtime.run_manifest
    def delegate(actual, **options):
        calls.append((actual, options["motor_feedback"]))
        return lower(actual, **options)
    monkeypatch.setattr(runtime, "run_manifest", delegate)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid typed feedback reached resources or policy effects")
    for method in ("resolve_qemu", "regular_file", "create_arena_plant"):
        monkeypatch.setattr(runtime, method, forbidden)
    monkeypatch.setattr(shared_world.SharedWorldRuntime, "set_motor_feedback_enabled", forbidden)
    with pytest.raises(ValueError):
        scenario_runtime.run_manifest(manifest, qemu=environment.binary)
    assert len(calls) == 1 and calls[0][0] is arena
    assert calls[0][1] == tuple((item.offset_ns, item.member, item.motor_id, item.enabled) for item in actions)
    assert not environment.events and not environment.owners and not environment.launches
    assert environment.plant is None and environment.native.resets == 0


@pytest.fixture
def admitted_motor_feedback_scenario(environment):
    documents = tuple(ROBOT_DOCUMENT.replace("chassis_can", bus).replace("  fl:", "  front-left:")
                      for bus in ("drive_left", "drive-right"))
    arena = load_manifest(write_arena(environment.directory, robot_documents=documents))
    path = environment.directory / "motor-feedback.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": arena.path.name, "duration": "4500us", "events": [
        {"at": "0ns", "action": "motor_feedback", "target": "east.front-left", "value": False},
        {"at": "0ns", "action": "can_delay", "target": "east.drive_left", "value": "2ms"},
        {"at": "1ms", "action": "can_delay", "target": "east.drive_left", "value": "0ns"},
        {"at": "1500us", "action": "motor_feedback", "target": "east.front-left", "value": True},
        {"at": "1500us", "action": "motor_feedback", "target": "east.front-left", "value": False},
        {"at": "3500us", "action": "motor_feedback", "target": "east.front-left", "value": True},
        {"at": "4500us", "action": "motor_feedback", "target": "east.front-left", "value": False},
    ]}, sort_keys=False), encoding="utf-8")
    return load_scenario(path)


@pytest.mark.parametrize("via_cli", [False, True], ids=["api", "cli"])
def test_loaded_scenario_motor_feedback_uses_actual_socket_dispatch_without_rereading(
        environment, monkeypatch, admitted_motor_feedback_scenario, via_cli, capsys):
    scenario = admitted_motor_feedback_scenario
    observed = watch_motor_feedback(environment, monkeypatch)
    monkeypatch.setattr(runtime, "Session", lambda session_id: Session(session_id, 500))
    deliveries, order, old_calls = ([], []), [], []
    factory = environment.create_plant
    def original(source, destination, frame):
        old_calls.append((source, destination, frame))
        return False
    def plant(manifest):
        value = factory(manifest)
        for index, bus in enumerate(dict.fromkeys(device.bus for device in value.devices)):
            bus.log = deliveries[index].append
            if index == 0:
                bus.drop = original
        return value
    monkeypatch.setattr(runtime, "create_arena_plant", plant)
    observed.delays.after_set = lambda value, member, delay: order.append((value.t_sim_ns, "delay", delay))
    observed.after_set = lambda value, member, motor_id, enabled: order.append((value.t_sim_ns, "feedback", enabled))
    environment.can_commands = ["1000 0000 0000 0000", "f000 0000 0000 0000"]
    if via_cli:
        from sim.apps import unmigrated_cli as cli
        results, calls = [], []
        run = scenario_runtime.run_manifest
        def execute(actual, **options):
            calls.append(actual)
            assert actual == scenario
            actual.path.unlink()
            actual.arena.path.unlink()
            result = run(actual, **options)
            results.append(result)
            return result
        monkeypatch.setattr(scenario_runtime, "run_manifest", execute)
        report, write = environment.directory / "feedback-report.json", Path.write_text
        def after_cleanup(path, *args, **kwargs):
            if path == report:
                environment.assert_clean()
                environment.events.append("feedback report write")
            return write(path, *args, **kwargs)
        monkeypatch.setattr(Path, "write_text", after_cleanup)
        assert cli.main(["scenario", "run", str(scenario.path), "--qemu", str(environment.binary),
                         "--report", str(report)]) == 0
        result, = results
        assert len(calls) == 1 and calls[0] is not scenario
        payload = json.loads(report.read_text())
        assert payload["status"] == "passed" and payload["result"]["steps"] == 7
        assert environment.events[-1] == "feedback report write"
        output = capsys.readouterr()
        assert "passed" in output.out and output.err == ""
    else:
        scenario.path.unlink()
        scenario.arena.path.unlink()
        result = scenario_runtime.run_manifest(scenario, qemu=environment.binary)
    environment.assert_clean()
    assert order == [(500, "delay", 2_000_000), (500, "feedback", False),
        (1_000_500, "delay", 0), (1_500_500, "feedback", True), (1_500_500, "feedback", False),
        (3_500_500, "feedback", True), (4_500_500, "feedback", False)]
    assert (result.origin_ns, result.end_ns, result.steps) == (500, 4_500_500, 7)
    assert environment.native.resets == 1
    # Old2ms command and3ms feedback dues split the surrounding outer steps.
    assert environment.native.steps == [1_000_000, 500_000, 500_000, 500_000, 500_000, 500_000, 1_000_000]
    zero = bytes.fromhex("0000 0000 0000 19 00")
    feedback = (((2_001_000, "0000", range(2, 5)), (3_001_000, "0000", range(2, 5)),
                 (3_001_000, "1000", range(2, 5)), (4_501_000, "1000", range(1, 5))),
                tuple((stamp, current, range(1, 5)) for stamp, current in (
                    (1_002_000, "0000"), (2_002_000, "f000"), (3_002_000, "f000"), (4_502_000, "f000"))))
    for index, groups in enumerate(feedback):
        assert environment.received_can(index) == b"".join(struct.pack(
            "<IIB3xQ64s", 0x200 + motor_id, 0, 8, stamp,
            (bytes.fromhex("0000 0000 " + current + " 19 00") if motor_id == 1 else zero).ljust(64, b"\0"))
            for stamp, current, ids in groups for motor_id in ids)
    assert [(row.sequence, row.timestamp_ns, row.due_ns, row.delivered_ns, row.dropped)
            for row in deliveries[0] if row.destination == "qemu" and row.frame.can_id == 0x201] == [
        (5, 2_000_500, 2_000_500, 2_000_500, True), (1, 1_000_500, 3_000_500, 3_000_500, True),
        (9, 3_000_500, 3_000_500, 3_000_500, True), (13, 4_500_500, 4_500_500, 4_500_500, False)]
    assert [(row.frame.data, row.timestamp_ns, row.due_ns, row.delivered_ns, row.dropped)
            for row in deliveries[0] if row.destination == "m3508-1" and row.frame.can_id == 0x200] == [
        (bytes.fromhex("1000 0000 0000 0000"), 500, 2_000_500, 2_000_500, False)]
    assert [device.current_raw for device in environment.plant.devices] == [4096, 0, 0, 0, -4096, 0, 0, 0]
    ratio = 3591 / 187  # Fixed DJI specification, not a production conversion helper.
    for offset, (current, west_current) in zip(range(0, 56, 8), (
            (0, 0), (0, -4096), (0, -4096), (4096, -4096),
            (4096, -4096), (4096, -4096), (4096, -4096))):
        group = environment.native.efforts[offset:offset + 8]
        assert group[0][1] == pytest.approx(current * 20 / 16384 * .02 * ratio)
        assert group[4][1] == pytest.approx(west_current * 20 / 16384 * .02 * ratio)
        assert all(group[index][1] == 0 for index in (1, 2, 3, 5, 6, 7))
    assert len(old_calls) == len(deliveries[0]) == 68
    world, = observed.worlds
    assert world.closed and all(item.endpoint.bus.pending == 0 for item in world.bindings)
    assert [item.endpoint.bus.drop for item in world.bindings] == [original, None]


@pytest.mark.parametrize("failure", ["reset", "eof", "interrupt"])
def test_loaded_scenario_motor_feedback_terminal_failure_keeps_partial_effects_and_cleanup(
        environment, monkeypatch, admitted_motor_feedback_scenario, failure):
    scenario = admitted_motor_feedback_scenario
    observed = watch_motor_feedback(environment, monkeypatch)
    primary = KeyboardInterrupt("feedback control interrupted")
    factory, original = environment.create_plant, lambda *args: False
    def plant(manifest):
        value = factory(manifest)
        value.devices[0].bus.drop = original
        return value
    monkeypatch.setattr(runtime, "create_arena_plant", plant)
    def fail(value, member, motor_id, enabled):
        if value.t_sim_ns != 1_500_000 or not enabled:
            return
        if failure == "interrupt":
            raise primary
        peer = environment.producers[0, "cosim"]
        if failure == "eof":
            peer.shutdown(socket.SHUT_WR)
        else:
            body = struct.pack("<IHHIQQQI", 0x32434D44, 2, 1, 0, 0, 9000, 0, 73)
            peer.send(struct.pack("<I", len(body)) + body)
    observed.after_set = fail
    environment.world_close_failure = OSError("feedback native cleanup failed")
    environment.world_close_failure.add_note("nested feedback cleanup diagnosis")
    scenario.path.unlink()
    scenario.arena.path.unlink()
    with pytest.raises({"reset": cosim.V2SessionReset, "eof": ConnectionError,
                        "interrupt": KeyboardInterrupt}[failure]) as caught:
        scenario_runtime.run_manifest(scenario, qemu=environment.binary)
    if failure == "interrupt":
        assert caught.value is primary
    elif failure == "reset":
        assert (caught.value.step_id, caught.value.t_sim_ns, caught.value.session_id) == (0, 9000, 73)
    environment.assert_clean()
    assert environment.native.resets == 1 and environment.native.steps == [1_000_000, 500_000]
    assert [row[3] for row in observed.calls] == ([0, 1_500_000] if failure == "interrupt" else
                                               [0, 1_500_000, 1_500_000])
    assert [item.endpoint.bus.drop for item in observed.worlds[0].bindings] == [original, None]
    assert "nested feedback cleanup diagnosis" in "\n".join(caught.value.__notes__)


@pytest.fixture
def admitted_can_delay_scenario(environment):
    """Real no-referee YAML input for the original Scenario execution consumer."""
    arena = environment.manifest()
    path = environment.directory / "can-delay.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": arena.path.name, "duration": "1ms",
        "events": [
            {"at": "0ns", "action": "can_delay", "target": "east.chassis_can", "value": "2ms"},
            {"at": "500us", "action": "can_delay", "target": "east.chassis_can", "value": "0s"},
        ]}), encoding="utf-8")
    return load_scenario(path)


@pytest.mark.parametrize("via_cli", [False, True], ids=["scenario", "scenario-cli"])
def test_loaded_scenario_can_delays_consumes_actual_arena_bus_without_rereading(
        environment, monkeypatch, admitted_can_delay_scenario, via_cli, capsys):
    scenario = admitted_can_delay_scenario
    assert scenario.can_delays == (ScenarioCanDelay(0, "east", 2_000_000),
                                   ScenarioCanDelay(500_000, "east", 0))
    assert scenario.referees == scenario.arena.referees == ()
    observed = watch_can_delays(environment, monkeypatch)
    environment.can_commands[0] = "1000 0000 0000 0000"
    def after_set(value, member, delay):
        if value.t_sim_ns == 500_000:
            environment.schedule_can(0, can_wire(1000, "0800 0000 0000 0000"))
    observed.after_set = after_set
    if via_cli:
        from sim.apps import unmigrated_cli as cli
        calls, results = [], []
        run_scenario = scenario_runtime.run_manifest
        def execute(actual, **options):
            calls.append(actual)
            assert actual == scenario
            actual.path.unlink()
            actual.arena.path.unlink()
            result = run_scenario(actual, **options)
            results.append(result)
            return result
        monkeypatch.setattr(scenario_runtime, "run_manifest", execute)
        report = environment.directory / "report.json"
        write_text = Path.write_text
        def write_after_cleanup(path, *args, **kwargs):
            if path == report:
                environment.assert_clean()
                environment.events.append("report write")
            return write_text(path, *args, **kwargs)
        monkeypatch.setattr(Path, "write_text", write_after_cleanup)
        assert cli.main(["scenario", "run", str(scenario.path), "--qemu", str(environment.binary),
                         "--report", str(report)]) == 0
        result, = results
        assert len(calls) == 1 and calls[0] is not scenario
        payload = json.loads(report.read_text(encoding="utf-8"))
        assert payload["status"] == "passed"
        assert (payload["result"]["origin_ns"], payload["result"]["end_ns"],
                payload["result"]["steps"], payload["result"]["referees"]) == (0, 1_000_000, 2, [])
        output = capsys.readouterr()
        assert f"Scenario {result.name!r}: passed" in output.out and output.err == ""
        assert environment.events[-1] == "report write"
        assert all(environment.events.index(f"owner{i} cleanup") < environment.events.index("report write")
                   for i in range(2))
    else:
        scenario.path.unlink()
        scenario.arena.path.unlink()
        result = scenario_runtime.run_manifest(scenario, qemu=environment.binary)
    environment.assert_clean()
    assert [(stamp, member, delay) for member, delay, stamp, *_ in observed.calls] == [
        (0, "east", 2_000_000), (500_000, "east", 0)]
    assert environment.native.resets == 1 and environment.native.steps == [500_000, 500_000]
    assert (result.origin_ns, result.end_ns, result.steps, result.referees) == (0, 1_000_000, 2, ())
    world, = observed.worlds
    assert world.closed and world.referees == ()
    assert [(item.endpoint.bus.delay_ns, item.endpoint.bus.pending) for item in world.bindings] == [(0, 1), (0, 0)]
    # The new command is admitted at the final CPU cut, after that interval's
    # feedback was produced. The old delayed command remains pending.
    zero = bytes.fromhex("0000 0000 0000 19 00")
    for index, stamp in enumerate((1_001_000, 1_002_000)):
        expected = b"".join(struct.pack("<IIB3xQ64s", 0x200 + motor_id, 0, 8, stamp,
            zero.ljust(64, b"\0"))
            for motor_id in range(1, 5))
        assert environment.received_can(index) == expected


@pytest.mark.parametrize("duration_ns,seconds,scheduled", [
    (1, 0.000000001, False), (1_234_567, 0.001234567, False),
    (3_600_000_000_000, 3600.0, False), (1_234_567, 0.001234567, True)])
def test_scenario_delegates_once_with_original_order_options_and_exact_duration(
        environment, monkeypatch, duration_ns, seconds, scheduled):
    arena = environment.manifest()
    first, second = (ScriptEvent(0, Hit(1)),), ()
    manifest = ScenarioManifest(environment.directory / "unread.yaml", arena, duration_ns,
                                (ScenarioReferee("west", first), ScenarioReferee("east", second)))
    assert manifest.assertions == ()
    manifest = replace(manifest, assertions=())
    if scheduled:
        manifest = replace(manifest, can_delays=(ScenarioCanDelay(0, "west", 11),
            ScenarioCanDelay(0, "east", 7), ScenarioCanDelay(1_234_567, "east", 0),
            ScenarioCanDelay(1_234_567, "east", 23)))
    calls, result = [], object()
    def delegate(actual, **options):
        calls.append((actual, options))
        return result
    monkeypatch.setattr(runtime, "run_manifest", delegate)
    emit, on_response = lambda event: None, lambda *items: None
    options = dict(emit=emit, on_response=on_response, qemu=environment.binary,
                   startup_timeout=3.0, timeout=7.0, referee_period_ns=2_000_000)
    assert scenario_runtime.run_manifest(manifest, **options) is result
    assert len(calls) == 1 and calls[0][0] is arena
    expected = dict(options, duration=seconds, referee_events=(("west", first), ("east", second)))
    if scheduled:
        expected["can_delays"] = ((0, "west", 11), (0, "east", 7),
                                  (1_234_567, "east", 0), (1_234_567, "east", 23))
    assert calls[0][1] == expected
    assert calls[0][1]["emit"] is emit and calls[0][1]["on_response"] is on_response
    assert calls[0][1]["referee_events"][0][1] is first
    assert calls[0][1]["referee_events"][1][1] is second
    assert round(calls[0][1]["duration"] * 1_000_000_000) == duration_ns
    assert environment.events == []


@pytest.mark.parametrize("interrupt", [False, True])
def test_scenario_propagates_identical_lower_error_without_retry(environment, monkeypatch, interrupt):
    manifest = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(), 1, (),
                                (ScenarioCanDelay(0, "east", 7),))
    calls, primary = [], KeyboardInterrupt("Arena interrupted") if interrupt else OSError("Arena rejected")
    def delegate(*args, **kwargs):
        calls.append((args, kwargs))
        raise primary
    monkeypatch.setattr(runtime, "run_manifest", delegate)
    with pytest.raises(type(primary)) as caught:
        scenario_runtime.run_manifest(manifest)
    assert caught.value is primary and len(calls) == 1
    assert calls[0][1]["can_delays"] == ((0, "east", 7),)
    assert environment.events == []


@pytest.mark.parametrize("invalid", ["delay-overflow", "unknown-member", "unordered",
                                    "after-duration", "quota", "duplicate-topology"])
def test_scenario_typed_can_values_reach_arena_preflight_before_resources(
        environment, monkeypatch, invalid):
    arena = environment.manifest()
    first, last = ScenarioCanDelay(0, "east", 7), ScenarioCanDelay(1_000_000, "west", 11)
    actions = (first, last)
    if invalid == "delay-overflow":
        actions = (first, replace(last, delay_ns=1 << 64))
    elif invalid == "unknown-member":
        actions = (first, replace(last, member="missing"))
    elif invalid == "unordered":
        actions = (last, first)
    elif invalid == "after-duration":
        actions = (first, replace(last, offset_ns=1_000_001))
    elif invalid == "quota":
        actions = (first,) * 1025
    else:
        arena = replace(arena, robots=(arena.robots[0], replace(arena.robots[1], name="east")))
    manifest = ScenarioManifest(environment.directory / "unread.yaml", arena, 1_000_000, (), actions)
    calls = []
    lower = runtime.run_manifest
    def delegate(actual, **options):
        calls.append(actual)
        return lower(actual, **options)
    monkeypatch.setattr(runtime, "run_manifest", delegate)
    def forbidden(*args, **kwargs):
        pytest.fail("invalid typed Scenario schedule reached resources or policy effects")
    for method in ("resolve_qemu", "regular_file", "create_arena_plant"):
        monkeypatch.setattr(runtime, method, forbidden)
    monkeypatch.setattr(shared_world.SharedWorldRuntime, "set_can_delay", forbidden)
    with pytest.raises(ValueError):
        scenario_runtime.run_manifest(manifest, qemu=environment.binary)
    assert len(calls) == 1 and calls[0] is arena
    assert environment.events == [] and environment.launches == [] and environment.owners == []
    assert environment.plant is None and environment.native.resets == 0 and environment.native.steps == []


@pytest.mark.parametrize("options", [
    {"duration": float("nan")}, {"duration": True}, {"duration": 1e-10},
    {"duration": 3601}, {"timeout": float("inf")},
    {"startup_timeout": 3, "timeout": 2}, {"emit": 1}, {"on_response": 1},
    {"on_observation": 1},
    {"referee_period_ns": True}, {"referee_period_ns": 1_000_000.0},
    {"referee_period_ns": 999_999}, {"referee_period_ns": 60_000_000_001},
])
def test_invalid_options_have_no_physics_process_or_transport_effect(environment, options):
    with pytest.raises((ValueError, TypeError)):
        environment.run(**options)
    assert environment.events == [] and environment.launches == []


@pytest.mark.parametrize("invalid", ["missing-ready", "wrong-uart", "foreign-member", "object",
                                    "mutable-referees", "duplicate-member", "wrong-ready-type",
                                    "foreign-ready-board", "unsupported-ready-uart", "duplicate-alias"])
def test_referee_association_rejects_before_resource_admission(environment, monkeypatch, invalid):
    manifest = environment.manifest(ready=("READY", None),
                                    referees={"east": referee_configuration()})
    record, = manifest.referees
    if invalid == "missing-ready":
        member = manifest.robots[0]
        manifest = replace(manifest, robots=(replace(member, robot=replace(member.robot, ready=None)),
                                             manifest.robots[1]))
    elif invalid == "wrong-uart":
        manifest = replace(manifest, referees=(replace(record, uart="main.uart1"),))
    elif invalid == "foreign-member":
        manifest = replace(manifest, referees=(replace(record, member="unknown"),))
    elif invalid == "object":
        manifest = replace(manifest, referees=(object(),))
    elif invalid == "mutable-referees":
        manifest = replace(manifest, referees=[record])
    elif invalid == "duplicate-member":
        manifest = replace(manifest, referees=(record, record))
    elif invalid == "duplicate-alias":
        manifest = replace(manifest, robots=(manifest.robots[0], replace(manifest.robots[1], name="east")))
    else:
        member = manifest.robots[0]
        ready = (object() if invalid == "wrong-ready-type" else replace(member.robot.ready,
                 endpoint="other.uart7" if invalid == "foreign-ready-board" else "main.uart99"))
        manifest = replace(manifest, robots=(replace(member, robot=replace(member.robot, ready=ready)),
                                             manifest.robots[1]))
    def forbidden(*args, **kwargs):
        pytest.fail("invalid Arena referee configuration reached resource admission")
    monkeypatch.setattr(runtime, "resolve_qemu", forbidden)
    monkeypatch.setattr(runtime, "regular_file", forbidden)
    with pytest.raises((ValueError, TypeError)):
        environment.run(manifest)
    assert environment.events == [] and environment.launches == []


@pytest.mark.parametrize("invalid,via_scenario", [
    (name, False) for name in ("outer_list", "entry_list", "entry_shape", "name_type",
                             "unknown", "unconfigured", "duplicate", "mutable_events",
                             "later_bad_event", "unordered", "after_duration", "aggregate_quota")
] + [(name, True) for name in ("unknown", "duplicate", "later_bad_event", "after_duration")])
def test_queued_referee_batches_are_completely_admitted_before_resources(
        environment, monkeypatch, invalid, via_scenario):
    manifest = environment.manifest(ready=("READY", "READY"), referees={
        "east": referee_configuration(), "west": referee_configuration()})
    events = (ScriptEvent(0, Hit(1)),)
    batches = (("east", events),)
    if invalid == "outer_list":
        batches = list(batches)
    elif invalid == "entry_list":
        batches = (["east", events],)
    elif invalid == "entry_shape":
        batches = (("east",),)
    elif invalid == "name_type":
        batches = ((None, events),)
    elif invalid == "unknown":
        batches = (("missing", events),)
    elif invalid == "unconfigured":
        manifest = replace(manifest, referees=manifest.referees[:1])
        batches = (("west", events),)
    elif invalid == "duplicate":
        batches = (("east", events), ("east", ()))
    elif invalid == "mutable_events":
        batches = (("east", list(events)),)
    elif invalid == "later_bad_event":
        batches = (("east", events), ("west", (ScriptEvent(0, Hit(True)),)))
    elif invalid == "unordered":
        batches = (("east", (ScriptEvent(1, Hit(0)), ScriptEvent(0, Hit(0)))),)
    elif invalid == "after_duration":
        batches = (("east", (ScriptEvent(2_500_001, Hit(0)),)),)
    else:
        batches = (("east", events * 512), ("west", events * 513))
    def forbidden(*args, **kwargs):
        pytest.fail("invalid queued batch reached file/native/resource admission")
    for method in ("resolve_qemu", "regular_file", "create_arena_plant"):
        monkeypatch.setattr(runtime, method, forbidden)
    with pytest.raises((ValueError, TypeError)):
        if via_scenario:
            scenario = ScenarioManifest(environment.directory / "unread.yaml", manifest, 2_500_000,
                                        tuple(ScenarioReferee(*batch) for batch in batches))
            scenario_runtime.run_manifest(scenario, qemu=environment.binary)
        else:
            environment.run(manifest, referee_events=batches)
    assert environment.events == [] and environment.launches == [] and environment.owners == []
    assert environment.native.resets == 0 and environment.native.steps == []


@pytest.mark.parametrize("full_quota", [False, True], ids=["empty", "aggregate1024"])
def test_admitted_batches_delegate_exactly_once_in_caller_order_after_shared_start(environment, monkeypatch, full_quota):
    observed = watch_referees(environment, monkeypatch)
    environment.uart_chunks = [[b"READY"], [b"READY"]]
    events = (ScriptEvent(0, Hit(0)),) * 512 if full_quota else ()
    batches = (("west", events), ("east", events))
    result = environment.run(environment.manifest(ready=("READY", "READY"), referees={
        "east": referee_configuration(), "west": referee_configuration()}),
        duration=.001, referee_events=batches)
    environment.assert_clean()
    assert [(name, events, session_id, stamp, steps) for name, events, session_id, stamp, steps, _ in observed.queues] == [
        ("west", events, 1, 0, ()), ("east", events, 1, 0, ())]
    assert all(row[1] is batch[1] for row, batch in zip(observed.queues, batches))
    assert all(not any(event.endswith(" cont") for event in row[5]) for row in observed.queues)
    assert len(observed.resets) == 2 and all(session.session_id == 1 for _, session in observed.resets)
    assert environment.native.resets == 1 and environment.native.steps == [1_000_000]
    assert result.origin_ns == 0 and result.end_ns == 1_000_000
    assert all(engine.closed for engine in observed.engines)


@pytest.mark.parametrize("invalid", ["outer_list", "outer_subclass", "entry_list", "entry_subclass",
                                    "entry_shape", "offset_bool", "offset_overflow", "delay_bool",
                                    "delay_overflow", "member_type", "member_subclass", "unknown",
                                    "unordered", "after_duration", "quota", "duplicate_member"])
def test_can_delay_schedule_is_completely_admitted_before_resources(environment, monkeypatch, invalid):
    manifest = environment.manifest()
    first, last = (0, "east", 7), (1_000_000, "west", 11)
    actions = (first, last)
    if invalid == "outer_list":
        actions = list(actions)
    elif invalid == "outer_subclass":
        actions = type("Actions", (tuple,), {})(actions)
    elif invalid == "entry_list":
        actions = (first, list(last))
    elif invalid == "entry_subclass":
        actions = (first, type("Action", (tuple,), {})(last))
    elif invalid == "entry_shape":
        actions = (first, (1_000_000, "west"))
    elif invalid in ("offset_bool", "offset_overflow"):
        actions = (first, (True if invalid == "offset_bool" else 1 << 64, "west", 11))
    elif invalid in ("delay_bool", "delay_overflow"):
        actions = (first, (1_000_000, "west", True if invalid == "delay_bool" else 1 << 64))
    elif invalid in ("member_type", "member_subclass", "unknown"):
        member = {"member_type": None, "member_subclass": type("Member", (str,), {})("west"),
                  "unknown": "missing"}[invalid]
        actions = (first, (1_000_000, member, 11))
    elif invalid == "unordered":
        actions = (last, first)
    elif invalid == "after_duration":
        actions = (first, (2_500_001, "west", 11))
    elif invalid == "quota":
        actions = (first,) * 1025
    else:
        manifest = replace(manifest, robots=(manifest.robots[0], replace(manifest.robots[1], name="east")))
    def forbidden(*args, **kwargs):
        pytest.fail("invalid CAN delay schedule reached resource admission or policy control")
    for method in ("resolve_qemu", "regular_file", "create_arena_plant"):
        monkeypatch.setattr(runtime, method, forbidden)
    monkeypatch.setattr(shared_world.SharedWorldRuntime, "set_can_delay", forbidden)
    with pytest.raises((TypeError, ValueError)):
        environment.run(manifest, can_delays=actions)
    assert environment.events == [] and environment.launches == [] and environment.owners == []
    assert environment.native.resets == 0 and environment.native.steps == []


@pytest.mark.parametrize("invalid", ["outer_list", "outer_subclass", "entry_list", "entry_subclass",
    "entry_shape", "offset_bool", "offset_negative", "offset_overflow", "offset_float",
    "member_type", "member_subclass", "unknown_member", "unordered", "after_duration", "quota",
    "motor_bool", "motor_subclass", "motor_zero", "motor_overflow", "motor_missing",
    "enabled_int", "enabled_none", "duplicate_member", "missing_record", "duplicate_record",
    "record_bool", "record_wrong_bus"])
def test_motor_feedback_schedule_is_completely_admitted_before_resources(environment, monkeypatch, invalid):
    manifest = environment.manifest()
    first, last = (0, "east", 1, False), (1_000_000, "west", 1, True)
    actions = (first, last)
    if invalid == "outer_list":
        actions = list(actions)
    elif invalid == "outer_subclass":
        actions = type("Actions", (tuple,), {})(actions)
    elif invalid == "entry_list":
        actions = (first, list(last))
    elif invalid == "entry_subclass":
        actions = (first, type("Action", (tuple,), {})(last))
    elif invalid == "entry_shape":
        actions = (first, last[:3])
    elif invalid.startswith("offset_"):
        offset = {"offset_bool": True, "offset_negative": -1,
                  "offset_overflow": 1 << 64, "offset_float": 1.0}[invalid]
        actions = (first, (offset, "west", 1, True))
    elif invalid in ("member_type", "member_subclass", "unknown_member"):
        member = {"member_type": None, "member_subclass": type("Member", (str,), {})("west"),
                  "unknown_member": "missing"}[invalid]
        actions = (first, (1_000_000, member, 1, True))
    elif invalid == "unordered":
        actions = (last, first)
    elif invalid == "after_duration":
        actions = (first, (2_500_001, "west", 1, True))
    elif invalid == "quota":
        actions = (first,) * 1025
    elif invalid.startswith("motor_"):
        motor_id = {"motor_bool": True, "motor_subclass": type("MotorId", (int,), {})(1),
                    "motor_zero": 0, "motor_overflow": 9, "motor_missing": 5}[invalid]
        actions = (first, (1_000_000, "west", motor_id, True))
    elif invalid.startswith("enabled_"):
        actions = (first, (1_000_000, "west", 1, 0 if invalid == "enabled_int" else None))
    elif invalid == "duplicate_member":
        manifest = replace(manifest, robots=(manifest.robots[0], replace(manifest.robots[1], name="east")))
    else:
        member = manifest.robots[1]
        motors = member.robot.devices
        selected = next(motor for motor in motors if motor.motor_id == 1)
        if invalid == "missing_record":
            motors = tuple(motor for motor in motors if motor is not selected)
        elif invalid == "duplicate_record":
            motors += (selected,)
        else:
            changed = replace(selected, **({"motor_id": True} if invalid == "record_bool"
                                           else {"bus": "foreign_can"}))
            motors = tuple(changed if motor is selected else motor for motor in motors)
        member = replace(member, robot=replace(member.robot, devices=motors))
        manifest = replace(manifest, robots=(manifest.robots[0], member))

    def forbidden(*args, **kwargs):
        pytest.fail("invalid later motor feedback action reached resources or policy control")

    for method in ("resolve_qemu", "regular_file", "create_arena_plant"):
        monkeypatch.setattr(runtime, method, forbidden)
    monkeypatch.setattr(shared_world.SharedWorldRuntime, "set_motor_feedback_enabled", forbidden)
    with pytest.raises((TypeError, ValueError)):
        environment.run(manifest, motor_feedback=actions)
    assert environment.events == [] and environment.launches == [] and environment.owners == []
    assert environment.native.resets == 0 and environment.native.steps == []


def test_motor_feedback_full_independent_quota_keeps_equal_time_fifo(environment, monkeypatch):
    observed = watch_motor_feedback(environment, monkeypatch)
    actions = tuple((0, "east" if index % 2 else "west", 1, bool(index % 3))
                    for index in range(1024))
    delays = tuple((0, "east", 0) for _ in range(1024))
    result = environment.run(duration=.001, motor_feedback=actions, can_delays=delays)
    environment.assert_clean()
    assert runtime.MAX_MOTOR_FEEDBACK_EVENTS == 1024
    assert [(stamp, member, motor_id, enabled) for member, motor_id, enabled, stamp, *_
            in observed.calls] == list(actions)
    assert len(observed.delays.calls) == 1024
    assert all(steps == () and reads == (0, 0) for *_, steps, reads in observed.calls)
    assert result.steps == 1 and environment.native.steps == [1_000_000]
    assert all(item.endpoint.bus.drop is None for item in observed.worlds[0].bindings)


@pytest.mark.parametrize("delayed_ready", [False, True], ids=["reset", "startup"])
def test_motor_feedback_off_grid_union_fifo_delay_priority_and_reset_origin(environment, monkeypatch, delayed_ready):
    observed = watch_motor_feedback(environment, monkeypatch)
    reset = 500 if delayed_ready else 0
    if delayed_ready:
        monkeypatch.setattr(runtime, "Session", lambda session_id: Session(session_id, reset))
    environment.uart_chunks = [[b"", b"", b"", b"", b"READY"] if delayed_ready else [b"READY"], [b"READY"]]
    order = []
    observed.delays.after_set = lambda value, member, delay: order.append((value.t_sim_ns - reset, "delay", member))
    observed.after_set = lambda value, member, motor_id, enabled: order.append((value.t_sim_ns - reset, "feedback", member))
    actions = ((0, "west", 1, False), (0, "east", 1, False),
               (500_000, "east", 1, True), (500_000, "east", 1, False),
               (1_500_000, "west", 1, True), (2_500_000, "east", 1, True))
    delays = ((0, "east", 0), (500_000, "west", 0), (1_000_000, "west", 0),
              (1_500_000, "east", 0), (2_500_000, "west", 0))
    result = environment.run(environment.manifest(ready=("READY", "READY")),
                             motor_feedback=actions, can_delays=delays)
    environment.assert_clean()
    assert [(stamp - reset, member, motor_id, enabled) for member, motor_id, enabled, stamp, *_
            in observed.calls] == list(actions)
    assert order == [(0, "delay", "east"), (0, "feedback", "west"), (0, "feedback", "east"),
        (500_000, "delay", "west"), (500_000, "feedback", "east"), (500_000, "feedback", "east"),
        (1_000_000, "delay", "west"), (1_500_000, "delay", "east"), (1_500_000, "feedback", "west"),
        (2_500_000, "delay", "west"), (2_500_000, "feedback", "east")]
    assert [row[4] for row in observed.calls] == [(), (), (500_000,), (500_000,),
        (500_000, 500_000, 500_000), (500_000, 500_000, 500_000, 1_000_000)]
    steps = [500_000, 500_000, 500_000, 1_000_000] + ([1_000_000, 1_000_000, 500_000] if delayed_ready else [])
    assert environment.native.steps == steps and result.steps == len(steps)
    assert (result.origin_ns, result.end_ns) == ((2_500_500, 5_000_500) if delayed_ready else (0, 2_500_000))
    assert environment.native.resets == 1 and [board.reset_qemu_ns for board in result.boards] == [1000, 2000]
    assert all(row[5] == (0, 0) for row in observed.calls[:2])


def test_motor_feedback_zero_follows_referee_queue_and_end_follows_completed_receipts(environment, monkeypatch):
    referees = watch_referees(environment, monkeypatch)
    observed = watch_motor_feedback(environment, monkeypatch)
    environment.uart_chunks = [[b"READY"], [b"READY"]]
    seen = []
    def inspect(value, member, motor_id, enabled):
        seen.append((value.t_sim_ns, len(referees.queues), tuple(environment.uart_reads),
                     referees.engines[0].get_state().robot.current_hp,
                     tuple(environment.cpus[index].completed for index in range(2)),
                     len(referees.publications)))
    observed.after_set = inspect
    result = environment.run(environment.manifest(ready=("READY", "READY"),
        referees={"east": referee_configuration()}), duration=.001, referee_period_ns=1_000_000,
        referee_events=(("east", (ScriptEvent(0, Hit(10)),)),),
        motor_feedback=((0, "east", 1, True), (1_000_000, "east", 1, False)))
    environment.assert_clean()
    assert seen == [(0, 1, (0, 0), 100, (0, 0), 0),
                    (1_000_000, 1, (1, 1), 90, (4, 4), 1)]
    zero = bytes.fromhex("0000 0000 0000 19 00")
    # The action at T cannot retract feedback already delivered by that step.
    for index in range(2):
        assert environment.received_can(index) == b"".join(struct.pack(
            "<IIB3xQ64s", 0x200 + motor_id, 0, 8, 1_001_000 + 1000 * index, zero.ljust(64, b"\0"))
            for motor_id in range(1, 5))
    assert [(name, stamp) for name, stamp, *_ in referees.publications] == [("east", 0), ("east", 1_000_000)]
    assert result.end_ns == 1_000_000 and environment.native.steps == [1_000_000]
    assert all(item.endpoint.bus.drop is None for item in observed.worlds[0].bindings)


def test_motor_feedback_pending_dispatch_and_recovery_preserve_wire_commands_and_old_policy(environment, monkeypatch):
    observed = watch_motor_feedback(environment, monkeypatch)
    factory, deliveries, old_calls = environment.create_plant, ([], []), []
    def original(source, destination, frame):
        old_calls.append((source, destination, frame))
        return False
    def plant(manifest):
        value = factory(manifest)
        for index, bus in enumerate(dict.fromkeys(device.bus for device in value.devices)):
            bus.log = deliveries[index].append
            if index == 0:
                bus.drop = original
        return value
    monkeypatch.setattr(runtime, "create_arena_plant", plant)
    environment.can_commands = ["1000 0000 0000 0000", "f000 0000 0000 0000"]
    result = environment.run(duration=.005, can_delays=((0, "east", 2_000_000), (1_000_000, "east", 0)),
        motor_feedback=((1_000_000, "east", 1, False), (3_000_000, "east", 1, True)))
    environment.assert_clean()
    zero = bytes.fromhex("0000 0000 0000 19 00")
    for index, groups in enumerate((
            ((2_001_000, "0000", range(2, 5)), (3_001_000, "0000", range(2, 5)),
             (3_001_000, "1000", range(2, 5)), (4_001_000, "1000", range(1, 5)),
             (5_001_000, "1000", range(1, 5))),
            tuple((stamp, current, range(1, 5)) for stamp, current in (
                (1_002_000, "0000"), (2_002_000, "f000"), (3_002_000, "f000"),
                (4_002_000, "f000"), (5_002_000, "f000"))))):
        assert environment.received_can(index) == b"".join(struct.pack(
            "<IIB3xQ64s", 0x200 + motor_id, 0, 8, stamp,
            (bytes.fromhex("0000 0000 " + current + " 19 00") if motor_id == 1 else zero).ljust(64, b"\0"))
            for stamp, current, ids in groups for motor_id in ids)
    assert [(row.sequence, row.timestamp_ns, row.due_ns, row.delivered_ns, row.dropped)
            for row in deliveries[0] if row.destination == "qemu" and row.frame.can_id == 0x201] == [
        (5, 2_000_000, 2_000_000, 2_000_000, True), (1, 1_000_000, 3_000_000, 3_000_000, True),
        (9, 3_000_000, 3_000_000, 3_000_000, True), (13, 4_000_000, 4_000_000, 4_000_000, False),
        (17, 5_000_000, 5_000_000, 5_000_000, False)]
    assert [device.current_raw for device in environment.plant.devices] == [4096, 0, 0, 0, -4096, 0, 0, 0]
    assert len(old_calls) == len(deliveries[0]) == 84
    assert all(item.endpoint.bus.pending == 0 for item in observed.worlds[0].bindings)
    assert [(row.frame.data, row.timestamp_ns, row.due_ns, row.delivered_ns, row.dropped)
            for row in deliveries[0] if row.destination == "m3508-1" and row.frame.can_id == 0x200] == [
        (bytes.fromhex("1000 0000 0000 0000"), 0, 2_000_000, 2_000_000, False)]
    ratio = 3591 / 187  # Fixed DJI specification, independent of production conversion.
    for offset, currents in zip(range(0, 40, 8), ((0, 0), (0, -4096),
                                               (4096, -4096), (4096, -4096), (4096, -4096))):
        group = environment.native.efforts[offset:offset + 8]
        assert group[0][1] == pytest.approx(currents[0] * 20 / 16384 * .02 * ratio)
        assert group[4][1] == pytest.approx(currents[1] * 20 / 16384 * .02 * ratio)
        assert all(group[index][1] == 0 for index in (1, 2, 3, 5, 6, 7))
    assert [item.endpoint.bus.drop for item in observed.worlds[0].bindings] == [original, None]
    assert environment.native.steps == [1_000_000] * 5 and result.steps == 5


@pytest.mark.parametrize("violation", ["deadline", "child_exit"])
def test_motor_feedback_post_setter_guard_stops_equal_time_suffix_and_restores_policies(environment, monkeypatch, violation):
    observed = watch_motor_feedback(environment, monkeypatch)
    def violate(value, member, motor_id, enabled):
        if member == "west":
            if violation == "deadline":
                environment.clock += 4
            else:
                environment.children[0].returncode = 0
    observed.after_set = violate
    actions = ((0, "east", 1, False), (1_500_000, "west", 1, False), (1_500_000, "east", 1, True))
    with pytest.raises((RuntimeError, TimeoutError), match="host deadline|exited early.*0"):
        environment.run(environment.manifest(upper=True), motor_feedback=actions, startup_timeout=1, timeout=3)
    environment.assert_clean()
    assert [(name, motor_id, enabled, stamp) for name, motor_id, enabled, stamp, *_ in observed.calls] == [
        ("east", 1, False, 0), ("west", 1, False, 1_500_000)]
    assert environment.native.steps == [1_000_000, 500_000]
    assert all(item.endpoint.bus.drop is None for item in observed.worlds[0].bindings)
    assert all(process.returncode == -signal.SIGTERM for process in environment.children)


def test_motor_feedback_later_setter_primary_survives_owned_cleanup_and_restores_old_callback(environment, monkeypatch):
    observed = watch_motor_feedback(environment, monkeypatch)
    primary, factory = KeyboardInterrupt("feedback action interrupted"), environment.create_plant
    original = lambda *args: False
    def plant(manifest):
        value = factory(manifest)
        value.devices[0].bus.drop = original
        return value
    monkeypatch.setattr(runtime, "create_arena_plant", plant)
    setter = shared_world.SharedWorldRuntime.set_motor_feedback_enabled
    def reject(value, member, motor_id, enabled):
        if member == "west":
            raise primary
        return setter(value, member, motor_id, enabled)
    monkeypatch.setattr(shared_world.SharedWorldRuntime, "set_motor_feedback_enabled", reject)
    environment.world_close_failure = OSError("native feedback cleanup failed")
    environment.world_close_failure.add_note("nested feedback cleanup diagnosis")
    with pytest.raises(KeyboardInterrupt) as caught:
        environment.run(environment.manifest(upper=True), motor_feedback=(
            (0, "east", 1, False), (1_500_000, "west", 1, False), (1_500_000, "east", 1, True)))
    assert caught.value is primary
    environment.assert_clean()
    assert [(name, motor_id, enabled, stamp) for name, motor_id, enabled, stamp, *_ in observed.calls] == [("east", 1, False, 0)]
    assert environment.native.steps == [1_000_000, 500_000]
    assert [item.endpoint.bus.drop for item in observed.worlds[0].bindings] == [original, None]
    assert "nested feedback cleanup diagnosis" in "\n".join(primary.__notes__)


def test_can_delay_full_quota_preserves_equal_time_caller_order(environment, monkeypatch):
    observed = watch_can_delays(environment, monkeypatch)
    actions = tuple((0, "west" if index % 2 else "east", index) for index in range(1024))
    result = environment.run(duration=.001, can_delays=actions)
    environment.assert_clean()
    assert runtime.MAX_CAN_DELAY_EVENTS == 1024
    assert [(stamp, member, delay) for member, delay, stamp, *_ in observed.calls] == list(actions)
    assert all(steps == () and reads == (0, 0) for _, _, _, steps, _, reads in observed.calls)
    assert [item.endpoint.bus.delay_ns for item in observed.worlds[0].bindings] == [1022, 1023]
    assert result.steps == 1 and environment.native.steps == [1_000_000]


@pytest.mark.parametrize("delayed_ready", [False, True], ids=["ready_at_reset", "startup_steps"])
@pytest.mark.parametrize("via_scenario", [False, True], ids=["arena", "scenario"])
def test_can_delay_off_grid_fifo_and_exact_duration_use_reset_origin(
        environment, monkeypatch, delayed_ready, via_scenario):
    observed = watch_can_delays(environment, monkeypatch)
    reset_origin = 500 if delayed_ready else 0
    if delayed_ready:
        monkeypatch.setattr(runtime, "Session", lambda session_id: Session(session_id, reset_origin))
    environment.uart_chunks = [[b"", b"", b"", b"READY"] if delayed_ready else [b"READY"], [b"READY"]]
    actions = ((0, "west", 41), (0, "east", 9), (500_000, "east", 0),
               (500_000, "east", 23), (1_500_000, "west", 0),
               (2_500_000, "east", (1 << 64) - 1 if not delayed_ready else 0))
    arena = environment.manifest(ready=("READY", "READY"))
    if via_scenario:
        scenario = ScenarioManifest(environment.directory / "unread.yaml", arena, 2_500_000, (),
                                    tuple(ScenarioCanDelay(*action) for action in actions))
        result = scenario_runtime.run_manifest(scenario, qemu=environment.binary)
    else:
        result = environment.run(arena, can_delays=actions)
    environment.assert_clean()
    assert [(stamp - reset_origin, member, delay) for member, delay, stamp, *_ in observed.calls] == list(actions)
    assert [steps for _, _, _, steps, _, _ in observed.calls] == [
        (), (), (500_000,), (500_000,), (500_000, 1_000_000),
        (500_000, 1_000_000, 23, 18, 999_959)]
    assert all(not any(event.endswith(" cont") for event in row[4]) for row in observed.calls[:2])
    assert all(row[5] == (0, 0) for row in observed.calls[:2])
    assert observed.calls[2][5] == observed.calls[3][5] == (1, 1)
    # Both already-queued1.5ms feedback batches keep their old23/41ns dues.
    expected_steps = [500_000, 1_000_000, 23, 18, 999_959]
    if delayed_ready:
        expected_steps += [23, 999_977, 1_000_000, 500_000]
    assert environment.native.steps == expected_steps and result.steps == len(expected_steps)
    assert (result.origin_ns, result.end_ns) == ((2_500_500, 5_000_500) if delayed_ready else (0, 2_500_000))
    assert environment.native.resets == 1
    assert [board.reset_qemu_ns for board in result.boards] == [1000, 2000]


@pytest.mark.parametrize("via_scenario", [False, True], ids=["arena", "loaded-scenario"])
def test_can_delay_dispatch_follows_queued_referee_reset_and_completed_target_state(
        environment, monkeypatch, via_scenario):
    referees = watch_referees(environment, monkeypatch)
    observed = watch_can_delays(environment, monkeypatch)
    states = []
    def inspect(value, member, delay):
        states.append((value.t_sim_ns, len(referees.queues), tuple(environment.uart_reads),
                       referees.engines[0].get_state()))
    observed.after_set = inspect
    environment.uart_chunks = [[b"READY"], [b"READY"]]
    events = (ScriptEvent(0, Hit(10)), ScriptEvent(1_000_000, Shot(2, 3)))
    configuration = referee_configuration()
    configuration["cooling_42mm_per_s"] = 2
    arena = environment.manifest(ready=("READY", "READY"), referees={"east": configuration})
    if via_scenario:
        path = environment.directory / "combined-scenario.yaml"
        path.write_text(yaml.safe_dump({"schema": 1, "arena": arena.path.name, "duration": "1ms",
            "referee_events": {"east": [
                {"at": "0ns", "hit": {"damage_hp": 10}},
                {"at": "1ms", "shot": {"heat_17mm": 2, "heat_42mm": 3}},
            ]}, "events": [
                {"at": "0ns", "action": "can_delay", "target": "east.chassis_can", "value": "17ns"},
                {"at": "1ms", "action": "can_delay", "target": "east.chassis_can", "value": "0ns"},
            ]}), encoding="utf-8")
        scenario = load_scenario(path)
        assert scenario.referees == (ScenarioReferee("east", events),)
        assert scenario.can_delays == (ScenarioCanDelay(0, "east", 17), ScenarioCanDelay(1_000_000, "east", 0))
        events = scenario.referees[0].events
        path.unlink()
        arena.path.unlink()
        result = scenario_runtime.run_manifest(scenario, qemu=environment.binary,
                                               referee_period_ns=1_000_000)
    else:
        result = environment.run(arena, duration=.001, referee_period_ns=1_000_000,
            referee_events=(("east", events),), can_delays=((0, "east", 17), (1_000_000, "east", 0)))
    environment.assert_clean()
    assert len(referees.worlds) == len(referees.simulations) == 1
    assert environment.native.resets == 1 and environment.native.steps == [1_000_000]
    assert len(referees.queues) == 1 and referees.queues[0][1] is events
    assert not any(event.endswith(" cont") for event in observed.calls[0][4])
    assert [(stamp, queues, reads, state.robot.current_hp) for stamp, queues, reads, state in states] == [
        (0, 1, (0, 0), 100), (1_000_000, 1, (1, 1), 90)]
    assert (states[1][3].power_heat.heat_17mm, states[1][3].power_heat.heat_42mm) == (11, 22)
    assert [(name, stamp, wire) for name, stamp, wire, _ in referees.publications] == [
        ("east", 0, REFEREE_WIRES[0]), ("east", 1_000_000, REFEREE_ZERO_EVENT_WIRES[1])]
    assert environment.producers[0, "uart7"].recv(4096) == REFEREE_WIRES[0] + REFEREE_ZERO_EVENT_WIRES[1]
    assert result.origin_ns == 0 and result.end_ns == 1_000_000


@pytest.mark.parametrize("via_scenario", [False, True], ids=["arena", "scenario"])
def test_can_delay_schedule_keeps_queued_native_interval_wire_policy_by_actual_admission(
        environment, monkeypatch, via_scenario):
    observed = watch_can_delays(environment, monkeypatch)
    deliveries, ingresses, boundaries = ([], []), [], []
    factory = environment.create_plant
    def plant(manifest):
        value = factory(manifest)
        for bus, log in zip(dict.fromkeys(device.bus for device in value.devices), deliveries):
            bus.log = log.append
        return value
    monkeypatch.setattr(runtime, "create_arena_plant", plant)
    ingest = QemuCanEndpoint.ingest
    def admission(value, frame, current_time_ns):
        ingresses.append((frame.timestamp_ns, frame.data, current_time_ns, value.bus.delay_ns))
        return ingest(value, frame, current_time_ns)
    monkeypatch.setattr(QemuCanEndpoint, "ingest", admission)
    def after_set(value, member, delay):
        boundaries.append((value.t_sim_ns, tuple(item.endpoint.bus.pending for item in value.bindings)))
        if value.t_sim_ns == 0:
            # CPU output is drained in its explicit interval, before world or
            # ACK work. Its future metadata retains this admitted delay policy.
            environment.schedule_can(0, can_wire(1_701_000, "3000 0000 0000 0000"), interval=1)
        elif value.t_sim_ns == 1_500_000:
            # The next CPU interval emits an older-stamped command after control.
            environment.schedule_can(0, can_wire(1_201_000, "2000 0000 0000 0000"))
    observed.after_set = after_set
    environment.can_commands = ["1000 0000 0000 0000", "f000 0000 0000 0000"]
    actions = ((0, "east", 2_000_000), (1_000_000, "east", 3_000_000), (1_500_000, "east", 0))
    if via_scenario:
        scenario = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(),
                                    4_000_000, (), tuple(ScenarioCanDelay(*action) for action in actions))
        result = scenario_runtime.run_manifest(scenario, qemu=environment.binary)
    else:
        result = environment.run(duration=.004, can_delays=actions)
    environment.assert_clean()
    assert environment.native.steps == [1_000_000, 500_000, 500_000, 500_000,
                                        500_000, 500_000, 200_000, 300_000]
    assert (result.origin_ns, result.end_ns, result.steps) == (0, 4_000_000, 8)
    assert boundaries == [(0, (0, 0)), (1_000_000, (6, 0)), (1_500_000, (6, 0))]
    assert [item.endpoint.bus.delay_ns for item in observed.worlds[0].bindings] == [0, 0]
    old, future, newer = (bytes.fromhex(value) for value in (
        "1000 0000 0000 0000", "3000 0000 0000 0000", "2000 0000 0000 0000"))
    assert [row for row in ingresses if row[1] in (future, newer)] == [
        (1_701_000, future, 0, 2_000_000), (1_201_000, newer, 1_500_000, 0)]
    assert [(row.frame.data, row.sequence, row.timestamp_ns, row.due_ns, row.delivered_ns, row.late)
            for row in deliveries[0] if row.destination == "m3508-1" and row.frame.can_id == 0x200] == [
        (newer, 6, 1_200_000, 1_200_000, 2_000_000, True),
        (old, 0, 0, 2_000_000, 2_000_000, False),
        (future, 1, 1_700_000, 3_700_000, 3_700_000, False)]
    # Independent C620 layouts, fixed stationary rotor and separate QEMU epochs.
    # New feedback overtakes the completed1ms batch, whose old2ms delay remains.
    # Commands discovered during an interval affect only subsequent World work;
    # known queued2ms/3ms/3.7ms dues introduce exact cuts before the outer target.
    zero = bytes.fromhex("0000 0000 0000 19 00")
    feedbacks = (
        ((2_001_000, "0000"), (3_001_000, "0000"), (3_001_000, "1000"), (4_001_000, "3000")),
        ((1_002_000, "0000"), (2_002_000, "f000"), (3_002_000, "f000"), (4_002_000, "f000")),
    )
    for index, groups in enumerate(feedbacks):
        expected = b"".join(struct.pack("<IIB3xQ64s", 0x200 + motor_id, 0, 8, stamp,
            (bytes.fromhex("0000 0000 " + current + " 19 00") if motor_id == 1 else zero).ljust(64, b"\0"))
            for stamp, current in groups for motor_id in range(1, 5))
        assert environment.received_can(index) == expected
    assert [(row.sequence, row.timestamp_ns, row.due_ns, row.delivered_ns)
            for row in deliveries[0] if row.destination == "qemu" and row.frame.can_id == 0x201] == [
        (7, 2_000_000, 2_000_000, 2_000_000), (2, 1_000_000, 3_000_000, 3_000_000),
        (11, 3_000_000, 3_000_000, 3_000_000), (15, 4_000_000, 4_000_000, 4_000_000)]
    ratio = 3591 / 187  # Pinned DJI M3508 specification, no production converter.
    expected_currents = ((0, 0), (0, -4096), (0, -4096), (4096, -4096),
                         (4096, -4096), (4096, -4096), (4096, -4096), (12288, -4096))
    for offset, currents in zip(range(0, 64, 8), expected_currents):
        group = environment.native.efforts[offset:offset + 8]
        assert group[0][1] == pytest.approx(currents[0] * 20 / 16384 * .02 * ratio)
        assert group[4][1] == pytest.approx(currents[1] * 20 / 16384 * .02 * ratio)
        assert all(group[index][1] == 0 for index in (1, 2, 3, 5, 6, 7))


@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("via_scenario", [False, True], ids=["arena", "scenario"])
def test_can_delay_later_setter_failure_preserves_primary_policy_and_independent_cleanup(
        environment, monkeypatch, interrupt, via_scenario):
    observed = watch_can_delays(environment, monkeypatch)
    primary = KeyboardInterrupt("CAN policy interrupted") if interrupt else OSError("CAN policy rejected")
    environment.world_close_failure = OSError("native cleanup failed")
    environment.world_close_failure.add_note("CAN policy nested cleanup diagnosis")
    property_ = CanBus.delay_ns
    def reject(value, delay):
        if delay == 13:
            raise primary
        return property_.fset(value, delay)
    monkeypatch.setattr(CanBus, "delay_ns", property(property_.fget, reject))
    arena = environment.manifest(upper=True)
    actions = ((0, "east", 7), (1_500_000, "west", 13), (1_500_000, "east", 17))
    with pytest.raises(type(primary)) as caught:
        if via_scenario:
            scenario = ScenarioManifest(environment.directory / "unread.yaml", arena, 2_500_000, (),
                                        tuple(ScenarioCanDelay(*action) for action in actions))
            scenario_runtime.run_manifest(scenario, qemu=environment.binary)
        else:
            environment.run(arena, can_delays=actions)
    assert caught.value is primary
    environment.assert_clean()
    assert [(name, delay, stamp) for name, delay, stamp, *_ in observed.calls] == [
        ("east", 7, 0), ("west", 13, 1_500_000)]
    assert [item.endpoint.bus.delay_ns for item in observed.worlds[0].bindings] == [7, 0]
    assert environment.native.steps == [1_000_000, 7, 499_993]
    assert all(process.returncode == -signal.SIGTERM for process in environment.children)
    assert "CAN policy nested cleanup diagnosis" in "\n".join(primary.__notes__)


@pytest.mark.parametrize("violation,via_scenario", [("deadline", False), ("child_exit", False),
    ("pre_child_exit", False), ("deadline", True), ("pre_child_exit", True)],
    ids=["arena-deadline", "arena-child-exit", "arena-pre-exit", "scenario-deadline", "scenario-pre-exit"])
def test_can_delay_post_setter_guard_prevents_equal_time_suffix_and_restep(
        environment, monkeypatch, violation, via_scenario):
    observed = watch_can_delays(environment, monkeypatch)
    def violate(value, name, delay):
        if value.t_sim_ns == 1_500_000 and violation != "pre_child_exit":
            if violation == "deadline":
                environment.clock += 4
            else:
                environment.children[0].returncode = 0
    observed.after_set = violate
    step = shared_world.SharedWorldRuntime.step
    def exit_after_step(value, dt_ns, **kwargs):
        result = step(value, dt_ns, **kwargs)
        if violation == "pre_child_exit" and value.t_sim_ns == 1_500_000:
            environment.children[0].returncode = 0
        return result
    monkeypatch.setattr(shared_world.SharedWorldRuntime, "step", exit_after_step)
    arena = environment.manifest(upper=True)
    actions = ((0, "east", 7), (1_500_000, "west", 13), (1_500_000, "east", 17))
    with pytest.raises((RuntimeError, TimeoutError), match="host deadline|exited early.*0"):
        if via_scenario:
            scenario = ScenarioManifest(environment.directory / "unread.yaml", arena, 2_500_000, (),
                                        tuple(ScenarioCanDelay(*action) for action in actions))
            scenario_runtime.run_manifest(scenario, qemu=environment.binary, startup_timeout=1, timeout=3)
        else:
            environment.run(arena, startup_timeout=1, timeout=3, can_delays=actions)
    environment.assert_clean()
    assert [(name, delay, stamp) for name, delay, stamp, *_ in observed.calls] == (
        [("east", 7, 0)] if violation == "pre_child_exit" else [("east", 7, 0), ("west", 13, 1_500_000)])
    assert [item.endpoint.bus.delay_ns for item in observed.worlds[0].bindings] == (
        [7, 0] if violation == "pre_child_exit" else [7, 13])
    assert environment.native.steps == [1_000_000, 7, 499_993]
    assert all(process.returncode == -signal.SIGTERM for process in environment.children)


@pytest.mark.parametrize("via_scenario", [False, True], ids=["arena", "scenario"])
def test_can_delay_split_native_refusal_freezes_without_rounding_retry_or_target_action(
        environment, monkeypatch, via_scenario):
    observed = watch_can_delays(environment, monkeypatch)
    # Feedback produced at1ms has an already-known1ms+7ns due. Refuse
    # that actual next interval before the later1.5ms policy boundary.
    primary, admissions, closing = ValueError("native interval7 refused"), [], []
    def validate(intervals):
        admissions.append(intervals)
        if intervals == (7,):
            raise primary
    environment.native.validate_steps = validate
    close = shared_world.SharedWorldRuntime.close
    def inspect(value):
        closing.append((value.failed, value.t_sim_ns, value.step_id))
        return close(value)
    monkeypatch.setattr(shared_world.SharedWorldRuntime, "close", inspect)
    with pytest.raises(ValueError) as caught:
        if via_scenario:
            scenario = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(),
                2_500_000, (), (ScenarioCanDelay(0, "east", 7), ScenarioCanDelay(1_500_000, "east", 0)))
            scenario_runtime.run_manifest(scenario, qemu=environment.binary)
        else:
            environment.run(can_delays=((0, "east", 7), (1_500_000, "east", 0)))
    assert caught.value is primary
    environment.assert_clean()
    assert admissions == [(1_000_000,), (7,)]
    assert environment.native.steps == [1_000_000] and len(environment.native.efforts) == 8
    assert closing == [(True, 1_000_000, 1)]
    assert [(name, delay, stamp) for name, delay, stamp, *_ in observed.calls] == [("east", 7, 0)]


@pytest.mark.parametrize("via_scenario", [False, True], ids=["arena", "scenario"])
def test_can_delay_maximum_is_admitted_then_existing_execution_overflow_is_terminal(
        environment, monkeypatch, via_scenario):
    observed = watch_can_delays(environment, monkeypatch)
    with pytest.raises(ValueError, match="unsigned 64-bit"):
        if via_scenario:
            scenario = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(),
                2_500_000, (), (ScenarioCanDelay(0, "east", (1 << 64) - 1), ScenarioCanDelay(1, "east", 0)))
            scenario_runtime.run_manifest(scenario, qemu=environment.binary)
        else:
            environment.run(can_delays=((0, "east", (1 << 64) - 1), (1, "east", 0)))
    environment.assert_clean()
    assert [(name, delay, stamp) for name, delay, stamp, *_ in observed.calls] == [("east", (1 << 64) - 1, 0)]
    assert environment.native.resets == 1 and environment.native.steps == []
    assert [item.endpoint.bus.delay_ns for item in observed.worlds[0].bindings] == [(1 << 64) - 1, 0]


@pytest.mark.parametrize("duration_ns,steps", [(1, [1]), (1_234_567, [1_000_000, 234_567])])
def test_scenario_exact_integer_duration_reaches_actual_arena_final_step(environment, duration_ns, steps):
    scenario = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(), duration_ns, ())
    result = scenario_runtime.run_manifest(scenario, qemu=environment.binary)
    environment.assert_clean()
    assert result.origin_ns == 0 and result.end_ns == duration_ns
    assert result.steps == len(steps) and environment.native.steps == steps
    assert environment.native.resets == 1


def test_changed_firmware_and_native_admission_fail_before_process(environment):
    manifest = environment.manifest()
    manifest.robots[1].robot.board.firmware.unlink()
    with pytest.raises(ValueError, match="firmware"):
        environment.run(manifest)
    assert environment.events == []
    manifest.robots[1].robot.board.firmware.write_bytes(b"readable fixture")
    environment.plant_failure = ValueError("native binding rejected")
    with pytest.raises(ValueError, match="native binding"):
        environment.run(manifest)
    assert environment.events == ["plant acquire"]


def test_setup_interrupt_after_factory_return_closes_caller_owned_plant(environment, monkeypatch):
    primary = KeyboardInterrupt("monotonic setup interrupted")
    def interrupted():
        raise primary
    monkeypatch.setattr(runtime, "time", SimpleNamespace(monotonic=interrupted))
    with pytest.raises(KeyboardInterrupt) as caught:
        environment.run()
    assert caught.value is primary
    environment.assert_clean()
    assert environment.events == ["plant acquire", "native close"]
    assert environment.owners == [] and environment.launches == []
    assert environment.native.resets == 0 and environment.native.steps == []


def test_one_owner_canonical_aliases_literal_upper_commands_and_exact_final_tick(environment):
    environment.can_commands = ["1000 0000 0000 0000", "f000 0000 0000 0000"]
    responses, events = [], []
    def before_cpu_request(index, name):
        if name != "x-run-to-virtual-time":
            return
        completed = len(environment.cpus[index].requests)
        assert all(len(cpu.receipts) == 4 * completed for cpu in environment.cpus.values())
        assert [frame.header.kind for _, frame, _ in responses[-2:]] == [5 if not completed else 3] * 2
    environment.qmp_callback = before_cpu_request
    result = environment.run(environment.manifest(upper=True), emit=events.append,
                             on_response=lambda *item: responses.append(item))
    environment.events.append("returned")
    environment.assert_clean()
    assert environment.events[-1] == "returned"
    assert all(environment.events.index(f"owner{i} cleanup") < environment.events.index("returned")
               for i in range(2))
    first_harness_cleanup = min(environment.events.index(f"owner{i} cleanup") for i in range(2))
    first_child_stop = min(environment.events.index(f"upper{i} stop") for i in range(2))
    first_peer_close = min(environment.events.index(f"peer{i} qmp close") for i in range(2))
    assert environment.events.index("native close") < first_child_stop < first_peer_close
    assert all(position < first_harness_cleanup for position, event in enumerate(environment.events)
               if event.endswith(" close") or event.endswith(" stop"))
    assert environment.native.resets == 1
    assert environment.native.steps == [1_000_000, 1_000_000, 500_000]
    assert len(environment.native.efforts) == 24
    for offset in (0, 8, 16):
        group = environment.native.efforts[offset:offset + 8]
        assert [name for name, _ in group] == [
            "alpha/wheel_fl", "alpha/wheel_fr", "alpha/wheel_rl", "alpha/wheel_rr",
            "bravo/wheel_fl", "bravo/wheel_fr", "bravo/wheel_rl", "bravo/wheel_rr"]
        if offset == 0:
            assert group[0][1] == group[4][1] == 0
        else:
            assert group[0][1] > 0 and group[4][1] < 0
        assert all(group[index][1] == 0 for index in (1, 2, 3, 5, 6, 7))
    assert (result.name, result.origin_ns, result.end_ns, result.steps) == (
        "two-four-wheel", 0, 2_500_000, 3)
    assert result.referees == ()
    assert [(board.name, board.prefix, board.ready, board.ready_qemu_ns) for board in result.boards] == [
        ("east", "alpha/", "not_waited", None), ("west", "bravo/", "not_waited", None)]
    assert [name for name, _, _ in responses] == ["east", "west"] * 4
    assert [frame.header.kind for _, frame, _ in responses] == [5, 5, 3, 3, 3, 3, 3, 3]
    assert environment.boards.events[:2] == [(0, 1, 0, ()), (1, 1, 0, ())]
    for _, command, _, _ in environment.owners:
        assert "-S" in command
        assert command[command.index("-accel") + 1] == "tcg,thread=single"
        assert command[command.index("-icount") + 1] == "shift=0,align=off,sleep=off"
    assert not any(event.endswith(" cont") for event in environment.events)
    assert [environment.cpus[index].requests for index in range(2)] == [
        [1_001_000, 2_001_000, 2_501_000], [1_002_000, 2_002_000, 2_502_000]]
    for index in range(2):
        cpu = environment.cpus[index]
        assert type(environment.sockets[index, "can1"]) is socket.socket
        assert cpu.state == "reached" and cpu.current == cpu.requests[-1]
        # Two completed 1ms batches; the final 0.5ms crosses no feedback period.
        assert len(cpu.receipts) == cpu.completed == 8 and not cpu.partial
        assert b"".join(cpu.receipts) == environment.received_can(index)
    assert len(environment.launches) == 2  # Upper processes only: no chassis workers.
    for (argv, options), member in zip(environment.launches, environment.manifest(upper=True).robots):
        assert argv == list(member.robot.upper_computers[0].command)
        assert options["cwd"] == member.robot.path.parent
        assert options["shell"] is False and options["start_new_session"] is True
        assert options["stdin"] == subprocess.DEVNULL
        assert options["stdout"] == subprocess.PIPE and options["stderr"] == subprocess.PIPE
    assert len(result.children) == 2
    with pytest.raises(FrozenInstanceError):
        result.origin_ns = 1


def test_later_native_cpu_target_failure_keeps_partial_execution_and_cleans_all(environment):
    primary = OSError("second CPU target refused")
    def reject(index, name):
        if index == 1 and name == "x-run-to-virtual-time":
            raise primary
    environment.qmp_callback = reject
    with pytest.raises(OSError) as caught:
        environment.run(environment.manifest(upper=True))
    assert caught.value is primary
    environment.assert_clean()
    assert environment.native.resets == 1 and environment.native.steps == []
    assert environment.cpus[0].requests == [1_001_000] and environment.cpus[0].state == "running"
    assert environment.cpus[1].requests == [] and environment.cpus[1].state == "idle"
    assert all(not cpu.receipts for cpu in environment.cpus.values())
    assert all(process.returncode == -signal.SIGTERM for process in environment.children)


def test_independent_split_nul_markers_use_post_read_stamps_and_completed_world_origin(environment):
    environment.uart_chunks = [[b"A\0", b"REA", b"DY"], [b"BRA", b"VO"]]
    events = []
    result = environment.run(environment.manifest(ready=("A\0READY", "BRAVO")), emit=events.append)
    environment.assert_clean()
    assert [board.ready for board in result.boards] == ["uart_marker", "uart_marker"]
    assert result.origin_ns > 0
    assert result.end_ns - result.origin_ns == 2_500_000
    assert sum(environment.native.steps) == result.end_ns
    assert environment.native.steps[-1] == 500_000
    assert result.steps == len(environment.native.steps)
    for index, board in enumerate(result.boards):
        marker_reads = (3, 2)[index]
        post_read = next(stamp for stamp, reads in environment.snapshots[index] if reads == marker_reads)
        assert board.ready_qemu_ns == post_read
        assert board.end_qemu_ns >= board.ready_qemu_ns
        assert board.observation_batches > 0
    assert [(event.name, event.event.data) for event in events] == [
        ("east", b"A\0"), ("west", b"BRA"), ("east", b"REA"),
        ("west", b"VO"), ("east", b"DY")]


@pytest.mark.parametrize("failure", ["second-owner", "second-cosim", "second-upper", "pipe-prepare"])
def test_later_acquisition_failure_releases_existing_plant_children_and_peers(
        environment, monkeypatch, failure):
    if failure == "second-owner":
        environment.owner_entry_failure = 1
    elif failure == "second-cosim":
        environment.connection_failure = (1, "cosim")
    elif failure == "second-upper":
        environment.spawn_failure = 1
    else:
        prepare = processes.ChildProcess.prepare
        def fail_prepare(child):
            prepare(child)
            if child.process is environment.children[-1] and len(environment.children) == 2:
                raise OSError("pipe preparation failure")
        monkeypatch.setattr(processes.ChildProcess, "prepare", fail_prepare)
    with pytest.raises(OSError, match="startup failure|connection failure|spawn failure|preparation failure"):
        environment.run(environment.manifest(upper=True))
    environment.assert_clean()
    assert environment.native.resets == 0 and environment.native.steps == []


@pytest.mark.parametrize("operation", ["second-launch", "connect", "upper-spawn"])
def test_absolute_startup_budget_includes_later_launch_connect_and_spawn(environment, operation):
    if operation == "second-launch":
        environment.owner_delays[1] = 2
    elif operation == "connect":
        environment.connect_delay = .3
    else:
        environment.spawn_delay = 1.1
    with pytest.raises((TimeoutError, RuntimeError), match="startup|deadline|timeout"):
        environment.run(environment.manifest(upper=True), startup_timeout=1, timeout=3)
    environment.assert_clean()
    assert environment.native.resets == 0 and environment.native.steps == []


def test_missing_ready_uses_one_startup_deadline_while_world_continues(environment):
    def elapsed_qmp(index, name):
        if name == "qom-get":
            environment.clock += .0001
    environment.qmp_callback = elapsed_qmp
    with pytest.raises((TimeoutError, RuntimeError), match="startup|deadline|timeout"):
        environment.run(environment.manifest(ready=("NEVER", None)),
                        startup_timeout=.003, timeout=.01)
    environment.assert_clean()
    assert environment.native.resets == 1 and environment.native.steps
    assert environment.waits == []


def test_active_deadline_is_not_restarted_after_ready(environment):
    def elapsed_qmp(index, name):
        if name == "qom-get":
            environment.clock += .0001
    environment.qmp_callback = elapsed_qmp
    environment.uart_chunks = [[b"not yet", b"READY"], []]
    with pytest.raises((TimeoutError, RuntimeError), match="host|deadline|timeout"):
        environment.run(environment.manifest(ready=("READY", None)), duration=.1,
                        startup_timeout=.002, timeout=.003)
    environment.assert_clean()
    assert environment.native.resets == 1 and environment.native.steps
    assert sum(environment.native.steps) < 100_000_000


def test_completed_paused_cuts_pump_delayed_uart_and_bounded_child_pipes_without_idle_wait(
        environment, monkeypatch):
    environment.uart_chunks = [[b"not", b"RE", b"AD", b"Y"], [b"READY"]]
    pumps, reads, children, descriptors = [], [], [], {}
    popen, pump, read = environment.popen, processes.ChildProcess.pump, os.read
    payload = b"x" * 4096 + b"\xff" + b"tail" * 475
    def launch(*args, **kwargs):
        process = popen(*args, **kwargs)
        for name in ("stdout", "stderr"):
            pipe = getattr(process, name)
            descriptors[pipe.fileno()] = len(environment.children) - 1, name
            assert os.write(pipe.writer, payload) == len(payload)
            os.close(pipe.writer)
            pipe.writer = os.open(os.devnull, os.O_WRONLY)
        return process
    def bounded_read(fd, maximum):
        if fd in descriptors:
            reads.append((descriptors[fd], maximum))
        return read(fd, maximum)
    def pump_child(value):
        before = len(reads)
        result = pump(value)
        children.append((value.label, len(reads) - before, frozenset(value.eof)))
        return result
    def forbidden_idle(*args):
        pytest.fail("Arena waited after a completed paused CPU cut")
    original_pump = runtime._Run.pump
    def pump_iteration(value):
        before = tuple(environment.uart_reads), len(children)
        ready = original_pump(value)
        pumps.append((tuple(environment.native.steps), before,
                      tuple(environment.uart_reads), len(children)))
        assert all(cpu.state in ("idle", "reached") for cpu in environment.cpus.values())
        return ready
    monkeypatch.setattr(runtime.subprocess, "Popen", launch)
    monkeypatch.setattr(processes.os, "read", bounded_read)
    monkeypatch.setattr(processes.ChildProcess, "pump", pump_child)
    monkeypatch.setattr(runtime._Run, "pump", pump_iteration)
    monkeypatch.setattr(runtime, "select", SimpleNamespace(select=forbidden_idle))
    result = environment.run(environment.manifest(ready=("READY", "READY"), upper=True))
    environment.assert_clean()
    assert (result.origin_ns, result.end_ns, result.steps) == (3_000_000, 5_500_000, 6)
    assert environment.native.steps == [1_000_000] * 5 + [500_000]
    assert len(pumps) == 8 and environment.uart_reads == [8, 8] and environment.waits == []
    assert all(after_reads == tuple(count + 1 for count in before[0])
               and after_children == before[1] + 2
               for _, before, after_reads, after_children in pumps)
    for index, child in enumerate(result.children):
        assert child["stdout"] == child["stderr"] == payload.decode("utf-8", errors="replace")
        rows = [row for row in children if row[0] == ("east", "west")[index] + "/upper:vision"]
        assert [row[1] for row in rows] == [2, 2, 2, 0, 0, 0, 0, 0]
        assert rows[2][2] == frozenset(("stdout", "stderr"))
    assert len(reads) == 12 and all(maximum == 4096 for _, maximum in reads)


@pytest.mark.parametrize("failure", ["child_exit", "uart_eof", "interrupt"])
def test_completed_paused_cut_next_pump_preserves_terminal_io_and_cleanup(environment, failure):
    environment.uart_chunks = [[b"waiting"], [b"READY"]]
    primary = KeyboardInterrupt("next paused-cut observation interrupted")
    def after_cut(index, name):
        if index != 0 or name != "qom-get" or not environment.native.steps:
            return
        assert all(cpu.state == "reached" for cpu in environment.cpus.values())
        if failure == "child_exit":
            environment.children[0].returncode = 0
        elif failure == "uart_eof":
            environment.producers[0, "uart7"].shutdown(socket.SHUT_WR)
        else:
            raise primary
    environment.qmp_callback = after_cut
    with pytest.raises({"child_exit": RuntimeError, "uart_eof": ConnectionError,
                        "interrupt": KeyboardInterrupt}[failure]) as caught:
        environment.run(environment.manifest(ready=("READY", "READY"), upper=True))
    if failure == "interrupt":
        assert caught.value is primary
    elif failure == "child_exit":
        assert "exited early (0)" in str(caught.value)
    environment.assert_clean()
    assert environment.native.steps == [1_000_000] and environment.waits == []
    assert all(environment.events.count(f"upper{index} stop") == 1 for index in range(2))


@pytest.mark.parametrize("resource", ["qemu", "upper"])
def test_zero_exit_during_final_pause_prevents_success_and_cleans_both_boards(environment, resource):
    def pause(index):
        if index == 1:
            process = (environment.owners[0][0].process if resource == "qemu"
                       else environment.children[0])
            process.returncode = 0
    environment.pause_callback = pause
    with pytest.raises(RuntimeError, match="exited.*0|early.*0"):
        environment.run(environment.manifest(upper=True))
    environment.assert_clean()
    assert environment.native.steps == [1_000_000, 1_000_000, 500_000]


def test_exit_during_result_observation_is_checked_before_cleanup(environment, monkeypatch):
    diagnostics = processes.ChildProcess.diagnostics
    def exit_during_observation(child):
        environment.children[0].returncode = 0
        return diagnostics(child)
    monkeypatch.setattr(processes.ChildProcess, "diagnostics", exit_during_observation)
    with pytest.raises(RuntimeError, match="exited early.*0"):
        environment.run(environment.manifest(upper=True))
    environment.assert_clean()
    assert environment.native.steps == [1_000_000, 1_000_000, 500_000]


@pytest.mark.parametrize("interrupt,same_error", [(False, False), (True, False), (True, True)])
def test_cleanup_is_independent_and_preserves_interrupt_object(environment, interrupt, same_error):
    primary = KeyboardInterrupt("output sink interrupted")
    environment.world_close_failure = primary if same_error else OSError("world close failure")
    environment.world_close_failure.add_note("nested native release failure")
    environment.stop_failure = OSError("upper group stop failure")
    environment.close_failures = {"upper0 stdout", "upper1 stderr", "peer0 uart1", "peer1 qmp"}
    environment.owner_failures = {0: OSError("first harness failure"),
                                  1: OSError("second harness failure")}
    environment.uart_chunks[0] = [b"observation"]
    def emit(event):
        if interrupt:
            raise primary
    with pytest.raises(KeyboardInterrupt if interrupt else OSError) as caught:
        environment.run(environment.manifest(upper=True), emit=emit)
    if interrupt:
        assert caught.value is primary
    environment.assert_clean()
    notes = "\n".join(getattr(caught.value, "__notes__", ())).lower()
    for label in ("world", "stop", "stdout", "stderr", "uart1", "qmp", "first harness", "second harness"):
        assert label in notes or label in str(caught.value)
    assert "nested native release failure" in notes
    assert len(getattr(caught.value, "__notes__", ())) < 20
    assert environment.events.count("upper0 stop") == 1
    assert environment.events.count("upper1 stop") == 1


def test_harness_failure_prevents_return_after_successful_execution(environment):
    error = OSError("second owner cleanup failure")
    environment.owner_failures[1] = error
    with pytest.raises(OSError) as caught:
        environment.run()
    assert caught.value is error
    environment.assert_clean()
    assert environment.native.steps == [1_000_000, 1_000_000, 500_000]


@pytest.mark.parametrize("producer", ["script", "scoped_queue", "scenario"])
def test_ready_uart7_borrows_one_transport_and_scoped_literal_batches_from_one_coordinator(
        environment, monkeypatch, producer):
    observed = watch_referees(environment, monkeypatch)
    environment.uart_chunks = [[b"READY"], [b"READY"]]
    configurations = {"west": short_referee_configuration(), "east": short_referee_configuration()}
    events = (ScriptEvent(1_000_000, Hit(10)),
              ScriptEvent(1_000_000, RobotStatus(3, 2, 60, 200, 3, 100, 120, 6)),
              ScriptEvent(1_000_000, Hit(5)), ScriptEvent(1_000_000, Shot(2, 3)))
    arena = environment.manifest(ready=("READY", "READY"), referees=configurations)
    queued = producer != "script"
    if producer == "scenario":
        path = environment.directory / "scenario.yaml"
        path.write_text(yaml.safe_dump({"schema": 1, "arena": arena.path.name, "duration": "1ms",
            "referee_events": {"east": [
                {"at": "1ms", "hit": {"damage_hp": 10}},
                {"at": "1ms", "robot": {"robot_id": 3, "robot_level": 2, "current_hp": 60,
                                         "maximum_hp": 200, "cooling_per_s": 3, "heat_limit": 100,
                                         "chassis_power_limit": 120, "power_outputs": 6}},
                {"at": "1ms", "hit": {"damage_hp": 5}},
                {"at": "1ms", "shot": {"heat_17mm": 2, "heat_42mm": 3}},
            ]}}, sort_keys=False), encoding="utf-8")
        scenario = load_scenario(path)
        assert scenario.referees[0].events == events
        events = scenario.referees[0].events
        path.unlink()
        arena.path.unlink()  # Execution consumes records, without rereading either YAML file.
        result = scenario_runtime.run_manifest(scenario, qemu=environment.binary,
                                               referee_period_ns=1_000_000)
    else:
        result = environment.run(arena, duration=.001, referee_period_ns=1_000_000,
                                 referee_events=(("east", events),) if queued else ())
    environment.assert_clean()
    assert len(observed.worlds) == len(observed.simulations) == 1
    assert observed.services and not any(observed.services)
    assert [node.name for node in observed.simulations[0].topology.nodes] == [
        'boards', 'can_0', 'can_1', 'plant', 'referee_0', 'referee_1']
    assert environment.native.resets == 1 and environment.native.steps == [1_000_000]
    assert len(observed.resets) == 2 and observed.resets[0][1] is observed.resets[1][1]
    assert [(session.session_id, session.t_sim_ns) for _, session in observed.resets] == [(1, 0), (1, 0)]
    assert [(context.session_id, context.step_id, context.t_sim_ns, context.dt_ns)
            for _, context in observed.steps] == [(1, 1, 1_000_000, 1_000_000)] * 2
    assert [(name, stamp, wire) for name, stamp, wire, _ in observed.publications] == [
        ("east", 0, REFEREE_WIRES[0]), ("west", 0, REFEREE_WIRES[0]),
        ("east", 1_000_000, REFEREE_QUEUED_WIRE if queued else REFEREE_WIRES[1]),
        ("west", 1_000_000, REFEREE_WIRES[1])]
    assert [(name, session_id, stamp, steps) for name, _, session_id, stamp, steps, _ in observed.queues] == (
        [("east", 1, 0, ())] if queued else [])
    if queued:
        assert observed.queues[0][1] is events
    for index, endpoint in enumerate(observed.endpoints):
        assert endpoint.transport is observed.channels[index]["uart7"]
        assert environment.events.count(f"peer{index} uart7 connect") == 1
        assert environment.owners[index][2].count("uart7") == 1
        expected = (REFEREE_WIRES[0], REFEREE_QUEUED_WIRE if queued and index == 0 else REFEREE_WIRES[1])
        assert environment.producers[index, "uart7"].recv(4096) == b"".join(expected)
        assert endpoint.next_sequence == 8 and endpoint.closed
    assert [(record.member, record.uart, record.period_ns, record.publications,
             record.first_logical_ns, record.last_logical_ns, record.next_sequence)
            for record in result.referees] == [
        ("east", "main.uart7", 1_000_000, 2, 0, 1_000_000, 8),
        ("west", "main.uart7", 1_000_000, 2, 0, 1_000_000, 8)]
    assert all(isinstance(record, runtime.ArenaRefereeResult) for record in result.referees)
    with pytest.raises(FrozenInstanceError):
        result.referees[0].publications = 0
    assert sorted(observed.closes) == ["endpoint0", "endpoint1", "engine0", "engine1"]


@pytest.mark.parametrize("delayed_ready", [False, True], ids=["ready_at_reset", "startup_steps"])
def test_queued_zero_offset_waits_for_completed_step_and_uses_reset_origin_during_startup(
        environment, monkeypatch, delayed_ready):
    observed = watch_referees(environment, monkeypatch)
    environment.uart_chunks = [[b"", b"", b"READY"] if delayed_ready else [b"READY"], [b"READY"]]
    configurations = {name: referee_configuration() for name in ("west", "east")}
    for configuration in configurations.values():
        configuration["cooling_42mm_per_s"] = 2
    events = (ScriptEvent(0, Hit(10)), ScriptEvent(0, Shot(2, 3)))
    result = environment.run(environment.manifest(ready=("READY", "READY"), referees=configurations), duration=.001,
        referee_period_ns=1_000_000, referee_events=(("east", events),))
    environment.assert_clean()
    east = [(stamp, wire, state) for name, stamp, wire, state in observed.publications if name == "east"]
    if delayed_ready:
        assert result.origin_ns == 2_000_000 and result.end_ns == 3_000_000
        assert [(stamp, wire) for stamp, wire, _ in east] == [
            (2_000_000, REFEREE_ZERO_EVENT_WIRES[0]), (3_000_000, REFEREE_ZERO_EVENT_WIRES[1])]
    else:
        assert result.origin_ns == 0 and result.end_ns == 1_000_000
        assert [(stamp, wire) for stamp, wire, _ in east] == [
            (0, REFEREE_WIRES[0]), (1_000_000, REFEREE_ZERO_EVENT_WIRES[1])]
        assert east[0][2].robot.current_hp == 100  # Admission did not retroactively change reset state.
    assert east[-1][2].robot.current_hp == 90
    assert (east[-1][2].power_heat.heat_17mm, east[-1][2].power_heat.heat_42mm) == (11, 22)
    assert all(state.robot.current_hp == 100 for name, _, _, state in observed.publications if name == "west")
    assert environment.producers[0, "uart7"].recv(4096) == b"".join(wire for _, wire, _ in east)
    assert len(observed.queues) == 1 and observed.queues[0][1] is events
    assert all(session.t_sim_ns == 0 for _, session in observed.resets)
    assert environment.native.resets == 1 and environment.native.steps == [1_000_000] * (3 if delayed_ready else 1)


@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("via_scenario", [False, True], ids=["arena", "scenario"])
def test_later_queue_failure_preserves_primary_and_releases_every_owner_without_resume_or_step(
        environment, monkeypatch, interrupt, via_scenario):
    observed = watch_referees(environment, monkeypatch)
    primary = KeyboardInterrupt("later queue interrupted") if interrupt else OSError("later queue rejected")
    secondary = OSError("endpoint0 cleanup failed")
    secondary.add_note("queued consumer nested release diagnosis")
    observed.close_failures["endpoint0"] = secondary
    queue = RefereeEngine.queue_events
    def reject_later(value, events, *, session_id):
        if value is observed.engines[1]:
            raise primary
        return queue(value, events, session_id=session_id)
    monkeypatch.setattr(RefereeEngine, "queue_events", reject_later)
    first, second = (ScriptEvent(0, Hit(10)),), (ScriptEvent(0, Hit(20)),)
    arena = environment.manifest(ready=("READY", "READY"), upper=True, referees={
        "east": referee_configuration(), "west": referee_configuration()})
    with pytest.raises(KeyboardInterrupt if interrupt else OSError) as caught:
        if via_scenario:
            scenario = ScenarioManifest(environment.directory / "unread.yaml", arena, 2_500_000,
                                        (ScenarioReferee("east", first), ScenarioReferee("west", second)))
            scenario_runtime.run_manifest(scenario, qemu=environment.binary)
        else:
            environment.run(arena, referee_events=(("east", first), ("west", second)))
    assert caught.value is primary
    environment.assert_clean()
    assert [(name, events, session_id, stamp, steps) for name, events, session_id, stamp, steps, _ in observed.queues] == [
        ("east", first, 1, 0, ()), ("west", second, 1, 0, ())]
    assert observed.queues[0][1] is first and observed.queues[1][1] is second
    assert environment.native.resets == 1 and environment.native.steps == [] and observed.publications == []
    assert not any(event.endswith(" cont") for event in environment.events)
    assert len(observed.worlds) == 1 and observed.worlds[0].closed
    assert all(engine.closed and engine.get_state().robot.current_hp == 100 for engine in observed.engines)
    assert all(endpoint.closed for endpoint in observed.endpoints)
    assert sorted(observed.closes) == ["endpoint0", "endpoint1", "engine0", "engine1"]
    assert "queued consumer nested release diagnosis" in "\n".join(primary.__notes__)
    assert all(process.returncode == -signal.SIGTERM for process in environment.children)


@pytest.mark.parametrize("violation", ["deadline", "child_exit"])
def test_post_queue_guard_checks_deadline_and_child_exit_before_later_batch_or_resume(
        environment, monkeypatch, violation):
    observed = watch_referees(environment, monkeypatch)
    queue = shared_world.SharedWorldRuntime.queue_referee_events
    def queue_then_violate(value, name, events, *, session_id):
        result = queue(value, name, events, session_id=session_id)
        if violation == "deadline":
            environment.clock += 2
        else:
            environment.owners[0][0].process.returncode = 0
        return result
    monkeypatch.setattr(shared_world.SharedWorldRuntime, "queue_referee_events", queue_then_violate)
    with pytest.raises((RuntimeError, TimeoutError), match="startup|deadline|timeout|exited.*0"):
        environment.run(environment.manifest(ready=("READY", "READY"), referees={
            "east": referee_configuration(), "west": referee_configuration()}),
            referee_events=(("east", ()), ("west", ())), startup_timeout=1, timeout=3)
    environment.assert_clean()
    assert len(observed.queues) == 1 and observed.queues[0][0] == "east"
    assert environment.native.resets == 1 and environment.native.steps == [] and observed.publications == []
    assert not any(event.endswith(" cont") for event in environment.events)
    assert all(engine.closed for engine in observed.engines) and all(endpoint.closed for endpoint in observed.endpoints)


def test_independent_ready_publishes_latest_startup_state_and_absolute_quantized_periods(
        environment, monkeypatch):
    observed = watch_referees(environment, monkeypatch)
    environment.uart_chunks = [[b"A\0", b"REA", b"DY"], [b"BRA", b"VO"]]
    result = environment.run(environment.manifest(ready=("A\0READY", "BRAVO"), referees={
        "west": short_referee_configuration(), "east": short_referee_configuration()}),
        referee_period_ns=1_500_000)
    environment.assert_clean()
    assert (result.origin_ns, result.end_ns) == (2_000_000, 4_500_000)
    assert [(name, stamp) for name, stamp, _, _ in observed.publications] == [
        ("west", 1_000_000), ("east", 2_000_000), ("west", 3_000_000),
        ("east", 4_000_000), ("west", 4_000_000)]
    # Events still use RESET0, so the local first ready state includes startup
    # changes; the later board also includes its additional1ms cooling.
    first_west, first_east = (observed.publications[index][3] for index in (0, 1))
    assert (first_west.robot.current_hp, first_west.power_heat.heat_17mm,
            first_west.power_heat.heat_42mm) == (70, 12, 25)
    assert (first_east.robot.current_hp, first_east.power_heat.heat_17mm,
            first_east.power_heat.heat_42mm) == (70, 11, 24)
    assert len(observed.resets) == 2 and environment.native.resets == 1
    assert all(session.t_sim_ns == 0 for _, session in observed.resets)
    assert environment.native.steps == [1_000_000] * 4 + [500_000]
    assert [(record.member, record.publications, record.first_logical_ns,
             record.last_logical_ns, record.next_sequence) for record in result.referees] == [
        ("east", 2, 2_000_000, 4_000_000, 8), ("west", 3, 1_000_000, 4_000_000, 12)]
    for index, board in enumerate(result.boards):
        post_read = next(stamp for stamp, reads in environment.snapshots[index] if reads == (3, 2)[index])
        assert board.ready_qemu_ns == post_read


def test_wrong_uart_fragment_never_matches_or_extends_ready_tail(environment, monkeypatch):
    observed = watch_referees(environment, monkeypatch)
    environment.uart_port_chunks = {(0, "uart1"): [b"REA", b"", b"DY"], (0, "uart7"): [b"DY"]}
    events = []
    result = environment.run(environment.manifest(ready=("READY", None), ready_ports=("uart1", "uart7"),
                                                 referees={"east": referee_configuration()}),
                             duration=.001, emit=events.append)
    environment.assert_clean()
    assert result.origin_ns == 2_000_000 and result.referees[0].first_logical_ns == 2_000_000
    assert result.referees[0].period_ns == 100_000_000
    assert [(event.event.endpoint, event.event.data) for event in events] == [
        ("uart1", b"REA"), ("uart7", b"DY"), ("uart1", b"DY")]
    assert environment.events.count("peer0 uart1 connect") == environment.events.count("peer0 uart7 connect") == 1
    assert observed.endpoints[0].transport is observed.channels[0]["uart7"]
    assert observed.channels[0]["uart1"] is not observed.channels[0]["uart7"]


@pytest.mark.parametrize("period,expected", [(1_000_000, [0, 1_000_000, 2_000_000, 3_000_000]),
                                             (60_000_000_000, [0])])
def test_period_limits_keep_exact_final_target_without_extra_publication(environment, monkeypatch, period, expected):
    observed = watch_referees(environment, monkeypatch)
    environment.uart_chunks[0] = [b"READY"]
    result = environment.run(environment.manifest(ready=("READY", None),
                                                 referees={"east": referee_configuration()}),
                             duration=.0035, referee_period_ns=period)
    environment.assert_clean()
    assert [stamp for _, stamp, _, _ in observed.publications] == expected
    assert result.end_ns == 3_500_000 and environment.native.steps == [1_000_000] * 3 + [500_000]
    assert result.referees[0].next_sequence == 4 * len(expected)


def test_referee_capacity_failure_retains_original_suffix_and_stops_before_another_world_tick(
        environment, monkeypatch):
    observed = watch_referees(environment, monkeypatch)
    initialize = runtime.StreamTransport.__init__
    def one_slot(value, sock, **kwargs):
        if getattr(sock, "name", None) == "uart7":
            kwargs["max_pending"] = 1
        initialize(value, sock, **kwargs)
    monkeypatch.setattr(runtime.StreamTransport, "__init__", one_slot)
    environment.uart_send_budget[0, "uart7"] = 19
    environment.uart_chunks[0] = [b"READY"]
    with pytest.raises(BufferError, match="full"):
        environment.run(environment.manifest(ready=("READY", None),
                                             referees={"east": referee_configuration()}),
                        referee_period_ns=1_000_000)
    environment.assert_clean()
    assert [stamp for _, stamp, _ in observed.attempts] == [0, 1_000_000]
    assert len(observed.publications) == 1 and observed.publications[0][2] == REFEREE_WIRES[0]
    assert environment.native.steps == [1_000_000]
    endpoint, = observed.endpoints
    assert endpoint.next_sequence == 4 and endpoint.closed
    queued, = endpoint.transport.tx_queue
    assert queued.obj is observed.publications[0][2] and bytes(queued) == REFEREE_WIRES[0][19:]
    assert environment.producers[0, "uart7"].recv(4096) == REFEREE_WIRES[0][:19]
    world, = observed.worlds
    assert world.closed and world.t_sim_ns == 1_000_000
    with pytest.raises(RuntimeError):
        world.step(1_000_000, deadline=environment.clock + 1)
    assert environment.native.steps == [1_000_000]


@pytest.mark.parametrize("failure", ["second-engine", "plant", "second-uart", "shared-constructor"])
def test_referee_acquisition_failures_release_every_still_caller_owned_resource(
        environment, monkeypatch, failure):
    observed = watch_referees(environment, monkeypatch)
    primary = KeyboardInterrupt(failure + " acquisition interrupted")
    if failure == "second-engine":
        initialize = RefereeEngine.__init__
        def fail_second(value, *args, **kwargs):
            if len(observed.engines) == 1:
                raise primary
            initialize(value, *args, **kwargs)
        monkeypatch.setattr(RefereeEngine, "__init__", fail_second)
    elif failure == "plant":
        environment.plant_failure = primary
        secondary = OSError("engine1 owned cleanup failure")
        secondary.add_note("pretransfer nested engine release diagnosis")
        observed.close_failures["engine1"] = secondary
    elif failure == "second-uart":
        connect = environment.connect
        def fail_connect(path, timeout):
            if Path(path).parent.name == "peer1" and Path(path).name == "uart7":
                raise primary
            return connect(path, timeout)
        monkeypatch.setattr(runtime, "connect_unix", fail_connect)
    else:
        def fail_constructor(*args, **kwargs):
            raise primary
        monkeypatch.setattr(shared_world.SharedWorldRuntime, "__init__", fail_constructor)
    with pytest.raises(KeyboardInterrupt) as caught:
        environment.run(environment.manifest(ready=("READY", "READY"), referees={
            "east": referee_configuration(), "west": referee_configuration()}))
    assert caught.value is primary
    environment.assert_clean()
    assert environment.native.resets == 0 and environment.native.steps == []
    assert len(observed.engines) == (1 if failure == "second-engine" else 2)
    assert len(observed.endpoints) == {"second-engine": 0, "plant": 0,
                                      "second-uart": 1, "shared-constructor": 2}[failure]
    assert all(engine.closed for engine in observed.engines)
    assert all(endpoint.closed for endpoint in observed.endpoints)
    assert len(observed.closes) == len(observed.engines) + len(observed.endpoints)
    assert len(set(observed.closes)) == len(observed.closes)
    assert observed.worlds == []
    if failure in ("second-engine", "plant"):
        assert environment.owners == []
    if failure == "plant":
        assert "pretransfer nested engine release diagnosis" in "\n".join(primary.__notes__)


def test_post_transfer_interrupt_preserves_nested_owned_cleanup_and_closes_every_resource_once(
        environment, monkeypatch):
    observed = watch_referees(environment, monkeypatch)
    primary = KeyboardInterrupt("ready observation interrupted")
    for label in ("endpoint0", "engine1"):
        error = OSError(label + " owned close failure")
        error.add_note(label + " nested release diagnosis")
        observed.close_failures[label] = error
    environment.uart_chunks = [[b"READY"], [b"READY"]]
    def interrupt(event):
        raise primary
    with pytest.raises(KeyboardInterrupt) as caught:
        environment.run(environment.manifest(ready=("READY", "READY"), upper=True, referees={
            "east": referee_configuration(), "west": referee_configuration()}), emit=interrupt)
    assert caught.value is primary
    environment.assert_clean()
    assert len(observed.worlds) == 1 and observed.worlds[0].closed
    assert environment.native.resets == 1 and environment.native.steps == []
    assert sorted(observed.closes) == ["endpoint0", "endpoint1", "engine0", "engine1"]
    assert all(engine.closed for engine in observed.engines)
    assert all(endpoint.closed for endpoint in observed.endpoints)
    notes = "\n".join(primary.__notes__)
    assert "endpoint0 nested release diagnosis" in notes and "engine1 nested release diagnosis" in notes
    assert all(environment.events.index(label + " close") < environment.events.index("peer0 uart7 close")
               for label in observed.closes)
    assert all(process.returncode == -signal.SIGTERM for process in environment.children)


@pytest.mark.parametrize("via_scenario", [False, True], ids=["arena", "scenario"])
def test_arena_and_scenario_wire_public_batch_to_both_native_peers(environment, via_scenario):
    if via_scenario:
        manifest = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(), 2_500_000, ())
        result = scenario_runtime.run_manifest(manifest, qemu=environment.binary)
    else:
        result = environment.run()
    environment.assert_clean()
    assert result.steps == 3 and environment.native.steps == [1_000_000, 1_000_000, 500_000]
    assert {index for index, _ in environment.batch_calls} == {0, 1}
    for index, cpu in environment.cpus.items():
        assert {direction for direction, _ in cpu.groups} == {"initial", "output", "input"}
        assert all(type(values) is tuple and len(values) == 1
                   for direction, values in cpu.groups)
        assert cpu.completed == len(cpu.receipts) == 8 and not cpu.partial
        assert b"".join(cpu.receipts) == environment.received_can(index)
    assert all(arguments == (
        ("qom-get", {"path": "/machine", "property": "can-execution-state"}),)
               for _, arguments in environment.batch_calls)
    assert all(row[2] != "qom-get" for row in environment.execution_events if row[0] == "qmp")
    assert not any(event.endswith(" cont") for event in environment.events)


@pytest.mark.parametrize("capability", ["missing", None, True])
def test_arena_requires_callable_batch_and_cleans_acquired_resources(environment, monkeypatch, capability):
    connect = environment.connect_qmp
    def unavailable(path, **kwargs):
        qmp = connect(path, **kwargs)
        if Path(path).parent.name == "peer1":
            if capability == "missing":
                del qmp.command_many
            else:
                qmp.command_many = capability
        return qmp
    monkeypatch.setattr(runtime, "QmpSession", unavailable)
    with pytest.raises(TypeError, match="command_many"):
        environment.run(environment.manifest(upper=True))
    environment.assert_clean()
    assert environment.native.resets == 0 and environment.native.steps == []
    assert all(not cpu.requests and not cpu.receipts for cpu in environment.cpus.values())
    assert environment.batch_calls == []
    assert all(process.returncode == -signal.SIGTERM for process in environment.children)
    assert all(f"peer{index} qmp close" in environment.events for index in range(2))


@pytest.mark.parametrize("violation", ["deadline", "child_exit", "batch_error", "identity"])
def test_arena_post_batch_failure_stops_before_next_cpu_and_preserves_cleanup(environment, violation):
    primary = OSError("native public batch failed")
    def invalidate(index, commands, values):
        if index != 0 or not environment.native.steps or not environment.cpus[0].receipts:
            return
        if violation == "deadline":
            environment.clock += 4
        elif violation == "child_exit":
            environment.children[0].returncode = 0
        elif violation == "batch_error":
            raise primary
        else:
            values[0]["fdcan-transports"]["dm-mc02.fdcan1"]["path"] = "/chardevs/replacement"
    environment.batch_after = invalidate
    with pytest.raises((RuntimeError, TimeoutError, OSError)) as caught:
        environment.run(environment.manifest(upper=True), startup_timeout=1, timeout=3)
    if violation == "batch_error":
        assert caught.value is primary
    elif violation == "deadline":
        assert type(caught.value) is rtf.StartupTimeoutError
        assert str(caught.value) == "Arena exceeded startup timeout"
    elif violation == "child_exit":
        assert "exited early (0)" in str(caught.value)
    else:
        assert str(caught.value) == "CAN input transport identity changed"
    environment.assert_clean()
    assert environment.native.steps == [1_000_000]
    assert [cpu.requests for cpu in environment.cpus.values()] == [[1_001_000], [1_002_000]]
    assert len(environment.cpus[0].receipts) == 4 and not environment.cpus[0].partial
    assert environment.cpus[1].receipts == []
    assert all(process.returncode == -signal.SIGTERM for process in environment.children)


@pytest.mark.parametrize("configured", [False, True], ids=["default", "observation_sink"])
def test_arena_observation_borrows_postread_objects_after_emit_and_ready(environment, configured):
    trace, observations, snapshots, batches, timeouts = [], [], {}, {}, []
    run = runtime._Run(101, 102, None)
    guard, guards = run.guard, []
    def guarded():
        guards.append(None)
        return guard()
    run.guard = guarded
    for name, origin in (("east", 1000), ("west", 2000)):
        states = [snapshot(origin + offset) for offset in (1, 2, 3, 4)]
        events = ([ChannelEvent("uart1", origin + 1, b"RE"),
                   ChannelEvent("uart1", origin + 1, b"ADY")] if name == "east" else
                  [ChannelEvent("uart1", origin + 1, b"READY")])
        snapshots[name], batches[name] = states[:], [events, []]
        pending_states, pending_batches = iter(states), iter(batches[name])
        def read_state(name=name, states=pending_states):
            value = next(states)
            trace.append((name, "snapshot", value["timestamp_ns"]))
            return value
        def service(timestamp_ns, *, flush_uart, name=name, values=pending_batches):
            trace.append((name, "service", timestamp_ns, flush_uart))
            return next(values)
        member = SimpleNamespace(name=name, robot=SimpleNamespace(ready=Ready("main.uart1", b"READY")))
        run.peers.append(runtime._Peer(member, SimpleNamespace(process=SimpleNamespace(poll=lambda: None)),
            SimpleNamespace(settimeout=timeouts.append), SimpleNamespace(snapshot=read_state),
            SimpleNamespace(service=service), None, None, last_ns=origin))
    def emit(item):
        peer = next(peer for peer in run.peers if peer.member.name == item.name)
        trace.append((item.name, "emit", item.event.data, peer.ready_qemu_ns))
    def observe(item):
        peer = next(peer for peer in run.peers if peer.member.name == item.name)
        trace.append((item.name, "observe", item.snapshot["timestamp_ns"], peer.ready_qemu_ns))
        observations.append(item)
    run.emit, run.on_observation = emit, observe if configured else None
    assert run.pump() is True and run.pump() is True
    expected = []
    for batch in range(2):
        for name, origin in (("east", 1000), ("west", 2000)):
            before, after = origin + 1 + 2 * batch, origin + 2 + 2 * batch
            expected.extend([(name, "snapshot", before), (name, "service", before, False),
                             (name, "snapshot", after)])
            expected.extend((name, "emit", event.data, None) for event in batches[name][batch])
            if configured:
                expected.append((name, "observe", after, origin + 2))
    assert trace == expected
    assert len(timeouts) == 8 and len(guards) == (33 if configured else 25)
    assert [peer.batches for peer in run.peers] == [2, 2]
    assert [peer.last_ns for peer in run.peers] == [1004, 2004]
    assert [peer.ready_qemu_ns for peer in run.peers] == [1002, 2002]
    assert len(observations) == (4 if configured else 0)
    for index, item in enumerate(observations):
        name, batch = ("east", "west")[index % 2], index // 2
        assert type(item) is runtime.ArenaObservation and item.name == name
        assert item.snapshot is snapshots[name][1 + 2 * batch]
        assert item.snapshot is not snapshots[name][2 * batch]
        assert item.events is batches[name][batch]
        assert item.snapshot == snapshot((1000 if name == "east" else 2000) + 2 + 2 * batch)
        with pytest.raises(FrozenInstanceError):
            item.name = "replacement"


@pytest.mark.parametrize("invalid", ["backward", "malformed"])
def test_arena_observation_bad_postread_never_emits_or_calls_sink(environment, monkeypatch, invalid):
    command, calls, emitted = environment.command, [], []
    def postread(index, name, arguments=None):
        value = command(index, name, arguments)
        if name == "qom-get" and index == 0 and len(environment.snapshots[0]) == 3:
            if invalid == "backward":
                value["timestamp_ns"] = 999
            else:
                value["gpio"]["A"]["sampled"] = -1
        return value
    monkeypatch.setattr(environment, "command", postread)
    environment.uart_chunks[0] = [b"not published"]
    with pytest.raises((ValueError, RuntimeError), match="backwards|sampled"):
        environment.run(environment.manifest(upper=True), emit=emitted.append, on_observation=calls.append)
    assert calls == emitted == [] and environment.uart_reads == [1, 0]
    assert environment.native.resets == 1 and environment.native.steps == []
    environment.assert_clean()


def observation_experiments(arena, assertions):
    """Use the public Lab consumer with explicit independent QEMU origins."""
    return {member.name: LabExperiment(LabManifest(
                arena.path, member.name, "dm-mc02", member.robot.board.firmware,
                2_500_000, (), assertions), origin)
            for member, origin in zip(arena.robots, (1000, 2000), strict=True)}


def test_arena_observation_actual_socket_split_uart_member_and_endpoint_isolation(environment):
    arena = environment.manifest(ready=("READY", "READY"), ready_ports=("uart1", "uart1"),
                                 referees={"east": referee_configuration()})
    environment.uart_port_chunks = {
        (0, "uart1"): [b"READYhel", b"lo"], (0, "uart7"): [b"lo"],
        (1, "uart1"): [b"READYlo", b"hel"],
    }
    experiments = observation_experiments(arena, (Assertion(1_500_000, "uart1", "uart_contains", b"hello"),))
    history, emitted = [], []
    def observe(item):
        experiment = experiments[item.name]
        experiment.observe(item.snapshot, item.events, item.snapshot["timestamp_ns"])
        history.append((item.name, item.snapshot["timestamp_ns"], experiment.status,
                        [(event.endpoint, event.data) for event in item.events]))
    result = environment.run(arena, emit=emitted.append, on_observation=observe)
    environment.assert_clean()
    assert history[:4] == [
        ("east", 1000, ("pending",), [("uart1", b"READYhel"), ("uart7", b"lo")]),
        ("west", 2000, ("pending",), [("uart1", b"READYlo")]),
        ("east", 1_001_000, ("pass",), [("uart1", b"lo")]),
        ("west", 1_002_000, ("pending",), [("uart1", b"hel")]),
    ]
    assert [experiments[name].status for name in ("east", "west")] == [("pass",), ("fail",)]
    assert len(history) == sum(board.observation_batches for board in result.boards)
    assert [name for name, *_ in history] == ["east", "west"] * 5
    assert [(item.name, item.event.endpoint, item.event.data) for item in emitted] == [
        ("east", "uart1", b"READYhel"), ("east", "uart7", b"lo"),
        ("west", "uart1", b"READYlo"), ("east", "uart1", b"lo"),
        ("west", "uart1", b"hel")]
    assert [board.ready_qemu_ns for board in result.boards] == [1000, 2000]
    assert environment.native.steps == [1_000_000, 1_000_000, 500_000]


def test_arena_observation_actual_postread_deadline_rejects_earlier_poll_bytes(environment, monkeypatch):
    arena = environment.manifest()
    experiments = observation_experiments(arena, (Assertion(50, "uart1", "uart_contains", b"OK"),))
    environment.uart_chunks = [[b"OK"], [b"OK"]]
    command, history = environment.command, []
    def postread(index, name, arguments=None):
        value = command(index, name, arguments)
        if name == "qom-get" and environment.uart_reads[index]:
            value["timestamp_ns"] += 50
        return value
    monkeypatch.setattr(environment, "command", postread)
    def observe(item):
        experiment = experiments[item.name]
        experiment.observe(item.snapshot, item.events, item.snapshot["timestamp_ns"])
        history.append((item.name, item.snapshot["timestamp_ns"],
                        [(event.timestamp_ns, event.data) for event in item.events], experiment.status))
    environment.run(arena, on_observation=observe)
    environment.assert_clean()
    assert history[:2] == [("east", 1050, [(1000, b"OK")], ("fail",)),
                           ("west", 2050, [(2000, b"OK")], ("fail",))]
    assert all(row[-1] == ("fail",) for row in history)


def test_arena_observation_empty_batch_expires_terminally_and_uses_sampled_gpio(environment, monkeypatch):
    arena = environment.manifest()
    assertions = (Assertion(1_000_000, "uart1", "uart_contains", b"hello"),
                  Assertion(1_000_000, "gpio.A0", "digital_equals", 1))
    experiments = observation_experiments(arena, assertions)
    environment.uart_chunks = [[b"hel", b"", b"hello"], [b"hello"]]
    command, history = environment.command, []
    def digital_state(index, name, arguments=None):
        value = command(index, name, arguments)
        if name == "qom-get":
            value["gpio"]["A"].update(sampled=index, odr=1 - index, output_mask=1)
        return value
    monkeypatch.setattr(environment, "command", digital_state)
    def observe(item):
        experiment = experiments[item.name]
        experiment.observe(item.snapshot, item.events, item.snapshot["timestamp_ns"])
        history.append((item.name, item.snapshot["timestamp_ns"], experiment.status,
                        tuple(event.data for event in item.events)))
    result = environment.run(arena, on_observation=observe)
    environment.assert_clean()
    assert history[:6] == [
        ("east", 1000, ("pending", "pending"), (b"hel",)),
        ("west", 2000, ("pass", "pass"), (b"hello",)),
        ("east", 1_001_000, ("fail", "fail"), ()),
        ("west", 1_002_000, ("pass", "pass"), ()),
        ("east", 2_001_000, ("fail", "fail"), (b"hello",)),
        ("west", 2_002_000, ("pass", "pass"), ()),
    ]
    for board in result.boards:
        assert experiments[board.name].finish(board.end_qemu_ns) == experiments[board.name].status
    assert len(experiments["east"].failures) == 2 and experiments["west"].all_passed


@pytest.mark.parametrize("interrupt", [False, True], ids=["error", "interrupt"])
def test_arena_observation_sink_failure_preserves_identity_without_replay_and_cleans_all(environment, interrupt):
    primary = KeyboardInterrupt("observation interrupted") if interrupt else OSError("observation failed")
    environment.world_close_failure = OSError("observation world cleanup failed")
    environment.world_close_failure.add_note("nested observation release evidence")
    environment.close_failures = {"peer0 uart1", "peer1 qmp"}
    environment.owner_failures = {0: OSError("first observation harness cleanup failed")}
    environment.uart_chunks = [[b"one"], [b"two"]]
    calls, emitted = [], []
    def observe(item):
        calls.append(item)
        raise primary
    with pytest.raises(type(primary)) as caught:
        environment.run(environment.manifest(upper=True), emit=emitted.append, on_observation=observe)
    assert caught.value is primary
    assert len(calls) == 1 and calls[0].name == "east" and calls[0].events[0].data == b"one"
    assert [(item.name, item.event.data) for item in emitted] == [("east", b"one")]
    assert environment.uart_reads == [1, 0] and environment.native.steps == []
    assert all(not cpu.requests for cpu in environment.cpus.values())
    notes = "\n".join(primary.__notes__)
    for label in ("world cleanup", "nested observation release", "uart1", "qmp", "harness cleanup"):
        assert label in notes
    environment.assert_clean()
    assert all(environment.events.count(f"upper{index} stop") == 1 for index in range(2))


@pytest.mark.parametrize("violation", ["deadline", "qemu_exit", "upper_exit"])
def test_arena_observation_postcallback_guard_stops_before_later_peer_and_world_step(environment, violation):
    calls = []
    def observe(item):
        calls.append(item)
        if violation == "deadline":
            environment.clock += 4
        elif violation == "qemu_exit":
            environment.owners[0][0].process.returncode = 0
        else:
            environment.children[0].returncode = 0
    with pytest.raises(TimeoutError if violation == "deadline" else RuntimeError,
                       match="startup deadline expired|exited early \\(0\\)"):
        environment.run(environment.manifest(upper=True), on_observation=observe,
                        startup_timeout=1, timeout=3)
    assert len(calls) == 1 and calls[0].name == "east" and calls[0].events == []
    assert environment.uart_reads == [1, 0] and environment.native.steps == []
    assert all(not cpu.requests for cpu in environment.cpus.values())
    environment.assert_clean()


@pytest.fixture
def admitted_assertion_scenario(environment):
    arena = environment.manifest(ready=("READY", "READY"), ready_ports=("uart1", "uart1"))
    path = environment.directory / "assertions.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": arena.path.name, "duration": "2500us", "assertions": [
        {"member": "west", "before": "1500us", "uart_contains": {"endpoint": "uart1", "text": "world"}},
        {"member": "east", "before": "1500us", "uart_contains": {"endpoint": "uart1", "text": "hello"}},
        {"member": "east", "before": "1ns", "digital_equals": {"endpoint": "gpio.A0", "value": 1}}]}),
        encoding="utf-8")
    return load_scenario(path)


@pytest.mark.parametrize("via_cli", [False, True], ids=["api", "cli"])
def test_loaded_scenario_assertions_use_reset_epochs_and_actual_startup_batches(
        environment, monkeypatch, admitted_assertion_scenario, via_cli, capsys):
    scenario = admitted_assertion_scenario
    environment.uart_chunks = [[b"he", b"llo", b"READY"], [b"wo", b"rld", b"READY"]]
    monkeypatch.setattr(runtime, "Session", lambda session_id: Session(session_id, 500))
    command = environment.command
    def digital_state(index, name, arguments=None):
        state = command(index, name, arguments)
        if name == "qom-get" and arguments.get("property") == "board-state":
            state["gpio"]["A"].update(sampled=1, odr=0, output_mask=1)
        return state
    monkeypatch.setattr(environment, "command", digital_state)
    experiments, observations, finishes, responses, results = {}, [], [], [], []
    current = None
    initialize, observe, finish = LabExperiment.__init__, LabExperiment.observe, LabExperiment.finish
    def initialize_lab(value, manifest, origin_ns):
        initialize(value, manifest, origin_ns)
        assert manifest.stimulus == ()
        experiments[origin_ns] = value
    def observe_lab(value, state, events, upper_bound):
        assert current is not None and state is current.snapshot and events is current.events
        assert upper_bound == state["timestamp_ns"]
        observe(value, state, events, upper_bound)
        observations.append((current.name, upper_bound, value.status, tuple(event.data for event in events)))
    def finish_lab(value, timestamp):
        environment.assert_clean()
        finishes.append((value.origin_ns, timestamp))
        return finish(value, timestamp)
    monkeypatch.setattr(LabExperiment, "__init__", initialize_lab)
    monkeypatch.setattr(LabExperiment, "observe", observe_lab)
    monkeypatch.setattr(LabExperiment, "finish", finish_lab)
    run_arena = runtime.run_manifest
    def lower(actual, **options):
        nonlocal current
        response, observation = options["on_response"], options["on_observation"]
        def on_response(*item):
            responses.append(item)
            response(*item)
        def on_observation(item):
            nonlocal current
            current = item
            observation(item)
        options.update(on_response=on_response, on_observation=on_observation)
        result = run_arena(actual, **options)
        results.append(result)
        return result
    monkeypatch.setattr(runtime, "run_manifest", lower)
    if via_cli:
        from sim.apps import unmigrated_cli as cli
        report, write = environment.directory / "assertion-report.json", Path.write_text
        def report_after_cleanup(path, *args, **kwargs):
            if path == report:
                environment.assert_clean()
                assert len(finishes) == 2
                environment.events.append("assertion report write")
            return write(path, *args, **kwargs)
        monkeypatch.setattr(Path, "write_text", report_after_cleanup)
        assert cli.main(["scenario", "run", str(scenario.path), "--qemu", str(environment.binary),
                         "--report", str(report)]) == 0
        assert json.loads(report.read_text())["status"] == "passed"
        assert environment.events[-1] == "assertion report write"
        assert "passed" in capsys.readouterr().out
        result, = results
    else:
        seen = []
        scenario.path.unlink()
        scenario.arena.path.unlink()
        result = scenario_runtime.run_manifest(scenario, qemu=environment.binary,
                                               on_response=lambda *items: seen.append(items))
        assert result is results[0] and len(seen) == len(responses)
        assert all(all(left is right for left, right in zip(a, b)) for a, b in zip(seen, responses))
    environment.assert_clean()
    assert sorted(experiments) == [1000, 2000]
    assert all(value.all_passed for value in experiments.values())
    assert [(name, decoded.virtual_time_ns) for name, frame, decoded in responses if frame.header.kind == 5] == [
        ("east", 1000), ("west", 2000)]
    assert [board.ready_qemu_ns for board in result.boards] == [2_001_000, 2_002_000]
    assert observations[:4] == [("east", 1000, ("pending", "pass"), (b"he",)),
                                ("west", 2000, ("pending",), (b"wo",)),
                                ("east", 1_001_000, ("pass", "pass"), (b"llo",)),
                                ("west", 1_002_000, ("pass",), (b"rld",))]
    assert any(not events for _, _, _, events in observations)
    assert len(observations) == sum(board.observation_batches for board in result.boards)
    assert sorted(finishes) == [(1000, 4_501_000), (2000, 4_502_000)]
    assert (result.origin_ns, result.end_ns, result.steps) == (2_000_500, 4_500_500, 5)


@pytest.mark.parametrize("failure", ["postread", "empty", "scope", "endpoint", "gpio"])
def test_scenario_assertion_failure_stops_at_first_real_boundary_and_cleans(
        environment, monkeypatch, failure):
    from sim.scenario import ScenarioAssertion

    arena = environment.manifest(ready=("READY", "READY"), ready_ports=("uart1", "uart1"),
                                 referees={"east": referee_configuration()})
    environment.uart_port_chunks = {(0, "uart1"): [b"READY"], (0, "uart7"): [], (1, "uart1"): [b"READY"]}
    entry = ScenarioAssertion("west" if failure == "scope" else "east",
        Assertion(50 if failure == "postread" else 1_000_000, "uart1", "uart_contains", b"OK"))
    if failure == "postread":
        environment.uart_port_chunks[0, "uart1"] = [b"READYOK"]
    elif failure == "scope":
        environment.uart_port_chunks[0, "uart1"] = [b"READYOK"]
    elif failure == "endpoint":
        environment.uart_port_chunks[0, "uart7"] = [b"OK"]
    elif failure == "gpio":
        entry = replace(entry, assertion=Assertion(1_000_000, "gpio.A0", "digital_equals", 1))
    command = environment.command
    def state(index, name, arguments=None):
        value = command(index, name, arguments)
        if name == "qom-get" and arguments.get("property") == "board-state":
            if failure == "postread" and environment.uart_reads[index]:
                value["timestamp_ns"] += 50
            if failure == "gpio":
                value["gpio"]["A"].update(sampled=0, odr=1, output_mask=1)
        return value
    monkeypatch.setattr(environment, "command", state)
    scenario = ScenarioManifest(environment.directory / "unread.yaml", arena, 2_500_000, (), assertions=(entry,))
    with pytest.raises(RuntimeError, match=entry.member + ".*did not match before"):
        scenario_runtime.run_manifest(scenario, qemu=environment.binary)
    environment.assert_clean()
    assert environment.native.steps == ([] if failure == "postread" else [1_000_000])
    if failure == "postread":
        assert environment.snapshots[0][-1][0] == 1000
        assert environment.uart_port_reads == {(0, "uart1"): 1, (0, "uart7"): 1}
    else:
        assert environment.uart_port_reads == {(0, "uart1"): 2, (0, "uart7"): 2,
                                               (1, "uart1"): 2 if failure == "scope" else 1}


@pytest.mark.parametrize("callback,interrupt", [("emit", False), ("on_response", True)])
def test_scenario_assertion_callback_primary_survives_independent_cleanup(
        environment, admitted_assertion_scenario, callback, interrupt):
    primary = KeyboardInterrupt("assertion response interrupted") if interrupt else OSError("assertion emit failed")
    environment.world_close_failure = OSError("assertion world cleanup failed")
    environment.close_failures = {"peer0 uart1", "peer1 qmp"}
    environment.owner_failures = {0: OSError("assertion harness cleanup failed")}
    environment.uart_chunks = [[b"helloREADY"], [b"worldREADY"]]
    calls = []
    def failed(*items):
        calls.append(items)
        raise primary
    with pytest.raises(type(primary)) as caught:
        scenario_runtime.run_manifest(admitted_assertion_scenario, qemu=environment.binary, **{callback: failed})
    assert caught.value is primary and len(calls) == 1
    assert environment.native.steps == []
    environment.assert_clean()
    notes = "\n".join(primary.__notes__)
    assert all(label in notes for label in ("world cleanup", "uart1", "qmp", "harness cleanup"))


@pytest.mark.parametrize("violation", ["deadline", "child_exit"])
def test_scenario_assertion_callback_guard_stops_before_observation_or_world_step(
        environment, violation):
    from sim.scenario import ScenarioAssertion

    arena = environment.manifest(upper=True)
    scenario = ScenarioManifest(environment.directory / "unread.yaml", arena, 1_000_000, (), assertions=(
        ScenarioAssertion("east", Assertion(1_000_000, "uart1", "uart_contains", b"OK")),))
    environment.uart_chunks = [[b"OK"], []]
    calls = []
    def emit(item):
        calls.append(item)
        if violation == "deadline":
            environment.clock += 4
        else:
            environment.children[0].returncode = 0
    with pytest.raises(TimeoutError if violation == "deadline" else RuntimeError,
                       match="startup deadline expired|exited early \\(0\\)"):
        scenario_runtime.run_manifest(scenario, qemu=environment.binary, emit=emit, startup_timeout=1, timeout=3)
    assert len(calls) == 1 and environment.uart_reads == [1, 0] and environment.native.steps == []
    environment.assert_clean()


def test_scenario_assertion_cli_reports_failure_only_after_cleanup(environment, monkeypatch, capsys):
    from sim.apps import unmigrated_cli as cli

    arena = environment.manifest()
    path, report = environment.directory / "failure.yaml", environment.directory / "failure-report.json"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": arena.path.name, "duration": "1ms", "assertions": [
        {"member": "east", "before": "1ms", "uart_contains": {"endpoint": "uart1", "text": "missing"}}]}),
        encoding="utf-8")
    write = Path.write_text
    def after_cleanup(path, *args, **kwargs):
        if path == report:
            environment.assert_clean()
            environment.events.append("failed assertion report write")
        return write(path, *args, **kwargs)
    monkeypatch.setattr(Path, "write_text", after_cleanup)
    assert cli.main(["scenario", "run", str(path), "--qemu", str(environment.binary), "--report", str(report)]) == 1
    payload = json.loads(report.read_text())
    assert payload["status"] == "failed" and payload["error_type"] == "RuntimeError"
    assert "east" in payload["error"] and "did not match before" in payload["error"]
    assert environment.events[-1] == "failed assertion report write"
    output = capsys.readouterr()
    assert "passed" not in output.out and "did not match before" in output.err


@pytest.mark.parametrize("missing", ["board", "reset", "batch", "duration"])
def test_scenario_assertion_final_gate_requires_real_completed_board_evidence(environment, monkeypatch, missing):
    from sim.scenario import ScenarioAssertion

    environment.uart_chunks = [[b"OK"], []]
    scenario = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(), 1_000_000, (),
        assertions=(ScenarioAssertion("east", Assertion(1_000_000, "uart1", "uart_contains", b"OK")),))
    run_arena, results = runtime.run_manifest, []
    def lower(actual, **options):
        result = run_arena(actual, **options)
        environment.assert_clean()
        results.append(result)
        east, west = result.boards
        if missing == "board":
            return replace(result, boards=(west,))
        change = {"reset": {"reset_qemu_ns": 1001}, "batch": {"observation_batches": 0},
                  "duration": {"end_qemu_ns": 1000}}[missing]
        return replace(result, boards=(replace(east, **change), west))
    monkeypatch.setattr(runtime, "run_manifest", lower)
    message = "simulation time must not decrease" if missing == "duration" else "lacks final|inconsistent"
    with pytest.raises((RuntimeError, ValueError), match=message):
        scenario_runtime.run_manifest(scenario, qemu=environment.binary)
    assert len(results) == 1 and environment.native.steps == [1_000_000]
    environment.assert_clean()


def test_scenario_assertion_observation_cannot_substitute_a_missing_reset_ack(environment, monkeypatch):
    from sim.scenario import ScenarioAssertion

    environment.uart_chunks = [[b"OK"], []]
    scenario = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(), 1_000_000, (),
        assertions=(ScenarioAssertion("east", Assertion(1_000_000, "uart1", "uart_contains", b"OK")),))
    run_arena = runtime.run_manifest
    def lower(actual, **options):
        response = options["on_response"]
        options["on_response"] = lambda name, frame, decoded: (
            None if name == "east" and frame.header.kind == 5 else response(name, frame, decoded))
        return run_arena(actual, **options)
    monkeypatch.setattr(runtime, "run_manifest", lower)
    with pytest.raises(RuntimeError, match="east.*no RESET_ACK origin"):
        scenario_runtime.run_manifest(scenario, qemu=environment.binary)
    assert environment.native.steps == [] and environment.uart_reads == [1, 0]
    environment.assert_clean()


def test_arena_trace_actual_socket_batches_reproduce_member_endpoint_and_split_byte_scope(environment):
    from io import BytesIO
    from sim.arena.trace import ArenaTrace, decode_observation
    from sim.trace import TraceWriter, iter_events

    arena = environment.manifest(ready=("READY", "READY"), ready_ports=("uart1", "uart1"),
                                 referees={"east": referee_configuration()})
    environment.uart_port_chunks = {
        (0, "uart1"): [b"READYhel", b"lo"], (0, "uart7"): [b"lo"],
        (1, "uart1"): [b"READYlo", b"hel"],
    }
    clocks = {"east": "qemu:east:run7", "west": "qemu:west:run2"}
    output, origins, original = BytesIO(), {}, []
    writer = TraceWriter(output)
    sink = ArenaTrace(writer, clocks=clocks)
    def response(name, frame, decoded):
        if frame.header.kind == runtime.codec.RESET_ACK:
            assert name not in origins
            origins[name] = decoded.virtual_time_ns
    def observe(item):
        sink(item)
        original.append(item)
    result = environment.run(arena, on_response=response, on_observation=observe)
    environment.assert_clean()
    assert origins == {"east": 1000, "west": 2000}
    writer.finish()  # Caller finishes only after successful Arena cleanup.
    recorded = list(iter_events(BytesIO(output.getvalue())))  # Validate EOF before replay.
    batches = [decode_observation(event, clocks=clocks) for event in recorded]
    assert batches == original
    assert len(batches) == sum(board.observation_batches for board in result.boards)
    assert [event.source for event in recorded] == ["east", "west"] * 5
    assert [event.clock for event in recorded] == [clocks["east"], clocks["west"]] * 5
    experiments = {member.name: LabExperiment(LabManifest(
        arena.path, member.name, "dm-mc02", member.robot.board.firmware, 2_500_000, (),
        (Assertion(1_500_000, "uart1", "uart_contains", b"hello"),)), origins[member.name])
        for member in arena.robots}
    history = []
    for item in batches:
        experiment = experiments[item.name]
        experiment.observe(item.snapshot, item.events, item.snapshot["timestamp_ns"])
        history.append((item.name, item.snapshot["timestamp_ns"], experiment.status,
                        [(event.endpoint, event.timestamp_ns, event.data) for event in item.events]))
    assert history[:4] == [
        ("east", 1000, ("pending",), [("uart1", 1000, b"READYhel"), ("uart7", 1000, b"lo")]),
        ("west", 2000, ("pending",), [("uart1", 2000, b"READYlo")]),
        ("east", 1_001_000, ("pass",), [("uart1", 1_001_000, b"lo")]),
        ("west", 1_002_000, ("pending",), [("uart1", 1_002_000, b"hel")]),
    ]
    for board in result.boards:
        experiments[board.name].finish(board.end_qemu_ns)
    assert experiments["east"].all_passed and experiments["west"].status == ("fail",)
    assert any(not item.events for item in batches) and not output.closed


def test_arena_trace_preserves_actual_postread_upper_bound_separate_from_poll(environment, monkeypatch):
    from io import BytesIO
    from sim.arena.trace import ArenaTrace, decode_observation
    from sim.trace import TraceWriter, iter_events

    arena = environment.manifest()
    environment.uart_chunks = [[b"OK"], [b"OK"]]
    command = environment.command
    def postread(index, name, arguments=None):
        value = command(index, name, arguments)
        if name == "qom-get" and environment.uart_reads[index]:
            value["timestamp_ns"] += 50
        return value
    monkeypatch.setattr(environment, "command", postread)
    clocks = {"east": "qemu:east:run0", "west": "qemu:west:run0"}
    output, origins = BytesIO(), {}
    writer = TraceWriter(output)
    def response(name, frame, decoded):
        if frame.header.kind == runtime.codec.RESET_ACK:
            origins[name] = decoded.virtual_time_ns
    environment.run(arena, on_response=response,
                    on_observation=ArenaTrace(writer, clocks=clocks))
    environment.assert_clean()
    writer.finish()
    recorded = list(iter_events(BytesIO(output.getvalue())))
    batches = [decode_observation(event, clocks=clocks) for event in recorded]
    assert [(item.name, item.snapshot["timestamp_ns"],
             [(event.timestamp_ns, event.data) for event in item.events]) for item in batches[:2]] == [
        ("east", 1050, [(1000, b"OK")]), ("west", 2050, [(2000, b"OK")])]
    experiments = {member.name: LabExperiment(LabManifest(
        arena.path, member.name, "dm-mc02", member.robot.board.firmware, 2_500_000, (),
        (Assertion(50, "uart1", "uart_contains", b"OK"),)), origins[member.name])
        for member in arena.robots}
    for item in batches:
        experiment = experiments[item.name]
        experiment.observe(item.snapshot, item.events, item.snapshot["timestamp_ns"])
        assert experiment.status == ("fail",)  # Poll is before deadline; actual bound is at it.


def test_arena_trace_empty_batches_preserve_terminal_expiry_and_sampled_gpio(environment, monkeypatch):
    from io import BytesIO
    from sim.arena.trace import ArenaTrace, decode_observation
    from sim.trace import TraceWriter, iter_events

    arena = environment.manifest()
    environment.uart_chunks = [[b"hel", b"", b"hello"], [b"hello"]]
    command = environment.command
    def digital_state(index, name, arguments=None):
        value = command(index, name, arguments)
        if name == "qom-get":
            value["gpio"]["A"].update(sampled=index, odr=1 - index, output_mask=1)
        return value
    monkeypatch.setattr(environment, "command", digital_state)
    clocks = {"east": "qemu:east:reset0", "west": "qemu:west:reset0"}
    output, origins = BytesIO(), {}
    writer = TraceWriter(output)
    def response(name, frame, decoded):
        if frame.header.kind == runtime.codec.RESET_ACK:
            origins[name] = decoded.virtual_time_ns
    result = environment.run(arena, on_response=response,
                             on_observation=ArenaTrace(writer, clocks=clocks))
    environment.assert_clean()
    writer.finish()
    recorded = list(iter_events(BytesIO(output.getvalue())))
    batches = [decode_observation(event, clocks=clocks) for event in recorded]
    experiments = {member.name: LabExperiment(LabManifest(
        arena.path, member.name, "dm-mc02", member.robot.board.firmware, 2_500_000, (),
        (Assertion(1_000_000, "uart1", "uart_contains", b"hello"),
         Assertion(1_000_000, "gpio.A0", "digital_equals", 1))), origins[member.name])
        for member in arena.robots}
    history = []
    for item in batches:
        experiment = experiments[item.name]
        experiment.observe(item.snapshot, item.events, item.snapshot["timestamp_ns"])
        history.append((item.name, item.snapshot["timestamp_ns"], experiment.status,
                        tuple(event.data for event in item.events)))
    assert history[:6] == [
        ("east", 1000, ("pending", "pending"), (b"hel",)),
        ("west", 2000, ("pass", "pass"), (b"hello",)),
        ("east", 1_001_000, ("fail", "fail"), ()),
        ("west", 1_002_000, ("pass", "pass"), ()),
        ("east", 2_001_000, ("fail", "fail"), (b"hello",)),
        ("west", 2_002_000, ("pass", "pass"), ()),
    ]
    assert batches[0].snapshot["gpio"]["A"] == {
        "moder": 0, "sampled": 0, "odr": 1, "output_mask": 1}
    for board in result.boards:
        assert experiments[board.name].finish(board.end_qemu_ns) == experiments[board.name].status
    assert len(experiments["east"].failures) == 2 and experiments["west"].all_passed


@pytest.mark.parametrize("interrupt", [False, True], ids=["output-error", "output-interrupt"])
def test_arena_trace_actual_output_failure_keeps_primary_cleans_all_and_never_replays(environment, interrupt):
    from io import BytesIO
    from sim.arena.trace import ArenaTrace
    from sim.trace import TraceWriter

    primary = KeyboardInterrupt("trace sink interrupted") if interrupt else OSError("trace sink failed")
    class Output(BytesIO):
        failure = False
        writes = 0
        def write(self, data):
            self.writes += 1
            if self.failure:
                raise primary
            return super().write(data)
    output = Output()
    writer = TraceWriter(output)
    sink = ArenaTrace(writer, clocks={"east": "qemu:east:run0", "west": "qemu:west:run0"})
    output.failure = True
    environment.uart_chunks = [[b"one"], [b"two"]]
    environment.world_close_failure = OSError("trace world cleanup failed")
    environment.close_failures = {"peer0 uart1", "peer1 qmp"}
    emitted = []
    with pytest.raises(type(primary)) as caught:
        environment.run(environment.manifest(upper=True), emit=emitted.append, on_observation=sink)
    assert caught.value is primary
    assert [(item.name, item.event.data) for item in emitted] == [("east", b"one")]
    assert environment.uart_reads == [1, 0] and environment.native.steps == []
    assert all(not cpu.requests for cpu in environment.cpus.values())
    notes = "\n".join(primary.__notes__)
    assert all(label in notes for label in ("world cleanup", "uart1", "qmp"))
    environment.assert_clean()
    assert all(environment.events.count(f"upper{index} stop") == 1 for index in range(2))
    stopped = output.getvalue(), output.writes
    output.failure = False
    with pytest.raises(RuntimeError):
        sink(runtime.ArenaObservation("east", snapshot(1000), []))
    assert (output.getvalue(), output.writes) == stopped and not output.closed


def test_arena_trace_completion_actual_result_matches_original_reset_ack_and_captured_batches(environment):
    from io import BytesIO
    from sim.arena.trace import ArenaTrace, decode_completion, decode_observation
    from sim.trace import TraceWriter, iter_events

    arena = environment.manifest(ready=("READY", "READY"), ready_ports=("uart1", "uart1"))
    environment.uart_chunks = [[b"RE", b"ADYhel", b"lo"], [b"READYlo", b"hel"]]
    clocks = {"east": "qemu:east:run13", "west": "qemu:west:run13"}
    output, origins, history = BytesIO(), {}, {"east": [], "west": []}
    writer = TraceWriter(output)
    sink = ArenaTrace(writer, clocks=clocks)
    def response(name, frame, decoded):
        if frame.header.kind == runtime.codec.RESET_ACK:
            assert name not in origins
            origins[name] = decoded.virtual_time_ns
    def capture(item):
        sink(item)
        history[item.name].append(item)
    result = environment.run(arena, on_response=response, on_observation=capture)
    environment.assert_clean()
    sink.complete(result)  # The real result is available only after all cleanup.
    writer.finish()
    recorded = list(iter_events(BytesIO(output.getvalue())))
    complete = [decode_completion(event, clocks=clocks) for event in recorded[-2:]]
    observations = [decode_observation(event, clocks=clocks) for event in recorded[:-2]]
    assert origins == {"east": 1000, "west": 2000}
    assert [(item.name, item.snapshot["timestamp_ns"], tuple(event.data for event in item.events))
            for item in observations[:4]] == [
        ("east", 1000, (b"RE",)), ("west", 2000, (b"READYlo",)),
        ("east", 1_001_000, (b"ADYhel",)), ("west", 1_002_000, (b"hel",))]
    assert [item.name for item in complete] == [board.name for board in result.boards]
    for board, metadata in zip(result.boards, complete, strict=True):
        captured = history[board.name]
        assert [item for item in observations if item.name == board.name] == captured
        assert metadata.name == board.name and metadata.clock == clocks[board.name]
        assert metadata.reset_qemu_ns == board.reset_qemu_ns == origins[board.name]
        assert metadata.end_qemu_ns == board.end_qemu_ns == captured[-1].snapshot["timestamp_ns"]
        assert metadata.observation_batches == board.observation_batches == len(captured)
        assert captured[0].snapshot["timestamp_ns"] == origins[board.name]
        assert any(not item.events for item in captured)
    assert result.boards[0].ready_qemu_ns > origins["east"]
    assert [event.data for item in history["east"] for event in item.events] == [b"RE", b"ADYhel", b"lo"]
    assert not output.closed


@pytest.mark.parametrize("failure", ["runtime", "cleanup"])
def test_arena_trace_completion_caller_cannot_publish_after_real_run_or_cleanup_failure(environment, failure):
    from io import BytesIO
    from sim.arena.trace import ArenaTrace
    from sim.trace import TraceWriter, iter_events

    primary = OSError(f"completion {failure} failure")
    if failure == "runtime":
        def fail_after_step(index, name):
            if name == "qom-get" and environment.native.steps:
                raise primary
        environment.qmp_callback = fail_after_step
    else:
        environment.world_close_failure = primary
    environment.uart_chunks = [[b"one"], [b"two"]]
    output = BytesIO()
    writer = TraceWriter(output)
    sink = ArenaTrace(writer, clocks={"east": "qemu:east:run13", "west": "qemu:west:run13"})
    result = None
    with pytest.raises(OSError) as caught:
        result = environment.run(environment.manifest(upper=True), on_observation=sink)
        sink.complete(result)
    assert caught.value is primary and result is None
    environment.assert_clean()
    writer.finish()
    recorded = list(iter_events(BytesIO(output.getvalue())))
    assert recorded and all(event.event_type == "arena.observation/1" for event in recorded)
    assert all(environment.events.count(f"upper{index} stop") == 1 for index in range(2))


def test_offline_replay_actual_socket_capture_matches_live_experiments_and_original_scenario(
        environment, monkeypatch):
    from io import BytesIO
    from sim.arena.trace import ArenaTrace
    from sim.scenario import ScenarioAssertion
    from sim.scenario.replay import replay_assertions
    from sim.trace import TraceWriter

    arena = environment.manifest(ready=("READY", "READY"), ready_ports=("uart1", "uart1"))
    environment.uart_chunks = [[b"RE", b"ADYhel", b"lo"], [b"READYhe", b"llo"]]
    command = environment.command
    def sampled_state(index, name, arguments=None):
        state = command(index, name, arguments)
        if name == "qom-get":
            state["gpio"]["A"].update(sampled=index, odr=1 - index, output_mask=1)
        return state
    monkeypatch.setattr(environment, "command", sampled_state)
    assertions = (
        ScenarioAssertion("west", Assertion(2_000_000, "uart1", "uart_contains", b"hello")),
        ScenarioAssertion("east", Assertion(500_000, "uart1", "uart_contains", b"RE")),
        ScenarioAssertion("east", Assertion(2_500_000, "uart1", "uart_contains", b"hello")),
        ScenarioAssertion("west", Assertion(1_000_000, "uart1", "uart_contains", b"llo")),
        ScenarioAssertion("east", Assertion(1_000_000, "gpio.A0", "digital_equals", 1)),
        ScenarioAssertion("west", Assertion(1_000_000, "gpio.A0", "digital_equals", 1)),
        ScenarioAssertion("east", Assertion(2_500_000, "uart1", "uart_contains", b"missing")),
    )
    scenario = ScenarioManifest(environment.directory / "original-scenario.yaml", arena, 2_500_000, (),
                                assertions=assertions)
    clocks = {"east": "qemu:east:run14", "west": "qemu:west:run14"}
    output, origins, live, history = BytesIO(), {}, {}, []
    writer = TraceWriter(output)
    capture = ArenaTrace(writer, clocks=clocks)
    def response(name, frame, decoded):
        if frame.header.kind == runtime.codec.RESET_ACK:
            assert name not in origins
            origins[name] = decoded.virtual_time_ns
            member = next(member for member in arena.robots if member.name == name)
            live[name] = LabExperiment(LabManifest(scenario.path, name, "dm-mc02",
                member.robot.board.firmware, scenario.duration_ns, (),
                tuple(item.assertion for item in assertions if item.member == name)), origins[name])
    def observe(item):
        capture(item)
        live[item.name].observe(item.snapshot, item.events, item.snapshot["timestamp_ns"])
        history.append((item.name, item.snapshot["timestamp_ns"], tuple(event.data for event in item.events)))
    result = environment.run(arena, on_response=response, on_observation=observe)
    environment.assert_clean()
    capture.complete(result)
    writer.finish()
    assert origins == {"east": 1000, "west": 2000}
    assert result.boards[0].ready_qemu_ns > origins["east"]
    assert history[:4] == [("east", 1000, (b"RE",)), ("west", 2000, (b"READYhe",)),
                          ("east", 1_001_000, (b"ADYhel",)), ("west", 1_002_000, (b"llo",))]
    assert any(not batch for _, _, batch in history)
    for board in result.boards:
        live[board.name].finish(board.end_qemu_ns)
    # This independent literal verdict also checks that the real live consumer
    # received startup, split, empty, GPIO and exact-deadline observations.
    assert live["east"].status == ("pass", "pass", "fail", "fail")
    assert live["west"].status == ("pass", "fail", "pass")
    output.seek(0)
    replayed = replay_assertions(output, scenario, clocks=clocks)
    assert replayed.status == ("pass", "pass", "pass", "fail", "fail", "pass", "fail")
    assert len(replayed.failures) == 3 and not replayed.passed
    assert {item.name: item.reset_qemu_ns for item in replayed.completions} == origins
    assert {item.name: item.end_qemu_ns for item in replayed.completions} == {
        item.name: item.end_qemu_ns for item in result.boards}
    assert output.tell() == len(output.getvalue()) and not output.closed
    environment.assert_clean()


def test_offline_replay_actual_uart_uses_postread_snapshot_upper_bound(environment, monkeypatch):
    from io import BytesIO
    from sim.arena.trace import ArenaTrace
    from sim.scenario import ScenarioAssertion
    from sim.scenario.replay import replay_assertions
    from sim.trace import TraceWriter

    arena = environment.manifest()
    environment.uart_chunks = [[b"OK"], [b"OK"]]
    command = environment.command
    def postread(index, name, arguments=None):
        state = command(index, name, arguments)
        if name == "qom-get" and environment.uart_reads[index]:
            state["timestamp_ns"] += 50
        return state
    monkeypatch.setattr(environment, "command", postread)
    assertions = tuple(ScenarioAssertion(name, Assertion(50, "uart1", "uart_contains", b"OK"))
                       for name in ("west", "east"))
    scenario = ScenarioManifest(environment.directory / "original-scenario.yaml", arena, 2_500_000, (),
                                assertions=assertions)
    clocks = {"east": "qemu:east:run14", "west": "qemu:west:run14"}
    output, batches = BytesIO(), []
    writer = TraceWriter(output)
    capture = ArenaTrace(writer, clocks=clocks)
    def observe(item):
        capture(item)
        batches.append(item)
    result = environment.run(arena, on_observation=observe)
    environment.assert_clean()
    capture.complete(result)
    writer.finish()
    assert [(item.name, item.snapshot["timestamp_ns"], item.events[0].timestamp_ns)
            for item in batches[:2]] == [("east", 1050, 1000), ("west", 2050, 2000)]
    output.seek(0)
    replayed = replay_assertions(output, scenario, clocks=clocks)
    assert replayed.status == ("fail", "fail") and not replayed.passed
    assert len(replayed.failures) == 2 and not output.closed
    environment.assert_clean()


@pytest.mark.parametrize("failure", ["runtime", "cleanup"])
def test_offline_replay_rejects_actual_failure_diagnostics_without_completion(environment, failure):
    from io import BytesIO
    from sim.arena.trace import ArenaTrace
    from sim.scenario.replay import replay_assertions
    from sim.trace import TraceWriter

    arena = environment.manifest(upper=True)
    primary = OSError(f"offline diagnostic {failure}")
    if failure == "runtime":
        def fail_after_step(index, name):
            if name == "qom-get" and environment.native.steps:
                raise primary
        environment.qmp_callback = fail_after_step
    else:
        environment.world_close_failure = primary
    environment.uart_chunks = [[b"one"], [b"two"]]
    clocks = {"east": "qemu:east:run14", "west": "qemu:west:run14"}
    output = BytesIO()
    writer = TraceWriter(output)
    capture = ArenaTrace(writer, clocks=clocks)
    with pytest.raises(OSError) as caught:
        result = environment.run(arena, on_observation=capture)
        capture.complete(result)
    assert caught.value is primary
    environment.assert_clean()
    writer.finish()  # Retain a complete diagnostic container, not a completed run.
    output.seek(0)
    scenario = ScenarioManifest(environment.directory / "original-scenario.yaml", arena, 2_500_000, ())
    with pytest.raises(ValueError, match="coverage|completion"):
        replay_assertions(output, scenario, clocks=clocks)
    assert not output.closed
    assert all(environment.events.count(f"upper{index} stop") == 1 for index in range(2))
    environment.assert_clean()


def test_offline_replay_cli_consumes_actual_socket_capture_after_cleanup(environment, monkeypatch, capsys):
    from sim.apps import unmigrated_cli as cli
    from sim.arena.trace import ArenaTrace
    from sim.trace import TraceWriter

    arena = environment.manifest()
    path = environment.directory / "original-replay.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": arena.path.name, "duration": "2500us",
        "assertions": [
            {"member": "west", "before": "2ms", "uart_contains": {"endpoint": "uart1", "text": "hello"}},
            {"member": "east", "before": "2ms", "uart_contains": {"endpoint": "uart1", "text": "hello"}},
        ]}), encoding="utf-8")
    original = load_scenario(path)
    environment.uart_chunks = [[b"hel", b"lo"], [b"hello"]]
    clocks = {"east": "qemu:east:run15", "west": "qemu:west:run15"}
    trace = environment.directory / "actual-capture.mcap"
    with trace.open("xb") as output:
        writer = TraceWriter(output)
        capture = ArenaTrace(writer, clocks=clocks)
        result = environment.run(original.arena, on_observation=capture)
        environment.assert_clean()
        capture.complete(result)
        writer.finish()
    history = list(environment.events)
    protected = path.read_bytes(), arena.path.read_bytes(), trace.read_bytes()
    def forbidden(*args, **kwargs):
        pytest.fail("offline CLI acquired another live runtime")
    monkeypatch.setattr(runtime, "running_qemu", forbidden)
    monkeypatch.setattr(runtime, "create_arena_plant", forbidden)
    monkeypatch.setattr(scenario_runtime, "run_manifest", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    report = environment.directory / "replayed.json"
    arguments = ["scenario", "replay", str(path), str(trace), "--report", str(report)]
    for member, epoch in clocks.items():
        arguments += ["--clock", f"{member}={epoch}"]
    assert cli.main(arguments) == 0
    payload = json.loads(report.read_text())
    assert payload["status"] == "passed" and payload["result"]["status"] == ["pass", "pass"]
    assert [(item["name"], item["reset_qemu_ns"], item["end_qemu_ns"], item["observation_batches"])
            for item in payload["result"]["completions"]] == [
        (board.name, board.reset_qemu_ns, board.end_qemu_ns, board.observation_batches)
        for board in result.boards]
    assert "Scenario replay: passed" in capsys.readouterr().out
    assert environment.events == history
    assert (path.read_bytes(), arena.path.read_bytes(), trace.read_bytes()) == protected
    environment.assert_clean()


def test_scenario_capture_isolation_forwards_exact_callback_result_and_options(environment, monkeypatch):
    arena = environment.manifest()
    manifest = ScenarioManifest(environment.directory / "unread.yaml", arena, 1_234_567, (),
                                can_delays=(ScenarioCanDelay(0, "west", 7),))
    observed, calls, result = [], [], object()
    item = runtime.ArenaObservation("east", snapshot(1000), [])
    class Sink:
        def __call__(self, value):
            observed.append(value)
        def close(self):
            pytest.fail("Scenario closed its borrowed observation sink")
    sink = Sink()
    emit, response = lambda value: None, lambda *values: None
    options = dict(emit=emit, on_response=response, qemu=environment.binary,
                   startup_timeout=3, timeout=7, referee_period_ns=2_000_000)
    def lower(actual, **forwarded):
        calls.append((actual, forwarded))
        forwarded["on_observation"](item)
        return result
    monkeypatch.setattr(runtime, "run_manifest", lower)
    assert scenario_runtime.run_manifest(manifest, on_observation=sink, **options) is result
    assert len(calls) == 1 and calls[0][0] is arena
    assert calls[0][1] == dict(options, duration=.001234567, referee_events=(),
                               can_delays=((0, "west", 7),), on_observation=sink)
    assert calls[0][1]["on_observation"] is sink and observed == [item] and observed[0] is item
    assert environment.events == [] and environment.plant is None


@pytest.mark.parametrize("assertions", [False, True], ids=["direct", "composite"])
def test_scenario_capture_isolation_rejects_bad_sink_before_lower_import(environment, monkeypatch, assertions):
    from sim.scenario import ScenarioAssertion

    manifest = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(),
        1_000_000, (), assertions=(ScenarioAssertion("east",
            Assertion(1_000_000, "uart1", "uart_contains", b"OK")),) if assertions else ())
    original_import = builtins.__import__
    def forbid_lower(name, *args, **kwargs):
        if name == "sim.arena.runtime":
            pytest.fail("invalid Scenario observation sink reached lower import")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", forbid_lower)
    with pytest.raises(TypeError, match="observation sink.*callable"):
        scenario_runtime.run_manifest(manifest, on_observation=object())
    assert environment.events == [] and environment.owners == [] and environment.launches == []
    assert environment.plant is None and environment.native.resets == 0 and environment.native.steps == []


def test_scenario_capture_isolation_external_precedes_real_checker_for_all_original_batches(environment, monkeypatch):
    from sim.scenario import ScenarioAssertion

    manifest = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(),
        2_500_000, (), assertions=(ScenarioAssertion("east",
            Assertion(1_000_000, "uart1", "uart_contains", b"OK")),))
    batches = [runtime.ArenaObservation("east", snapshot(1000), []),
               runtime.ArenaObservation("west", snapshot(2000), []),
               runtime.ArenaObservation("east", snapshot(1001), [ChannelEvent("uart1", 1000, b"OK")]),
               runtime.ArenaObservation("west", snapshot(2001), [ChannelEvent("uart1", 2000, b"other")]),
               runtime.ArenaObservation("east", snapshot(2_501_000), [])]
    result = runtime.ArenaResult("isolated", 0, 2_500_000, 3, (
        runtime.ArenaBoardResult("east", "alpha_", "not requested", None, 2_501_000, 1000, 3),
        runtime.ArenaBoardResult("west", "bravo_", "not requested", None, 2_502_000, 2000, 2)), ())
    sequence, external, statuses = [], [], []
    observe = LabExperiment.observe
    def checker(experiment, state, events, bound):
        item = external[-1]
        assert state is item.snapshot and events is item.events and bound == state["timestamp_ns"]
        assert sequence[-1] == ("external", id(item))
        sequence.append(("checker", id(item)))
        observe(experiment, state, events, bound)
        statuses.append(experiment.status)
    monkeypatch.setattr(LabExperiment, "observe", checker)
    def sink(item):
        external.append(item)
        sequence.append(("external", id(item)))
    calls = []
    def lower(actual, **options):
        calls.append(actual)
        options["on_response"]("east", SimpleNamespace(header=SimpleNamespace(kind=runtime.codec.RESET_ACK)),
                               SimpleNamespace(virtual_time_ns=1000))
        for item in batches:
            options["on_observation"](item)
        return result
    monkeypatch.setattr(runtime, "run_manifest", lower)
    assert scenario_runtime.run_manifest(manifest, on_observation=sink) is result
    assert calls == [manifest.arena] and all(a is b for a, b in zip(external, batches, strict=True))
    assert sequence == [(kind, id(item)) for item in batches
                        for kind in (("external", "checker") if item.name == "east" else ("external",))]
    assert statuses == [("pending",), ("pass",), ("pass",)]
    assert environment.events == [] and environment.plant is None


@pytest.mark.parametrize("interrupt", [False, True], ids=["error", "interrupt"])
def test_scenario_capture_actual_sink_failure_skips_checker_and_preserves_cleanup(environment, monkeypatch, interrupt):
    from sim.scenario import ScenarioAssertion

    arena = environment.manifest(upper=True)
    manifest = ScenarioManifest(environment.directory / "unread.yaml", arena, 1_000_000, (),
        assertions=(ScenarioAssertion("east", Assertion(1_000_000, "uart1", "uart_contains", b"OK")),))
    environment.uart_chunks = [[b"OK"], [b"unread"]]
    primary = KeyboardInterrupt("Scenario capture interrupted") if interrupt else OSError("Scenario capture failed")
    environment.world_close_failure = OSError("capture world cleanup failed")
    environment.world_close_failure.add_note("nested capture cleanup evidence")
    environment.close_failures = {"peer0 uart1", "peer1 qmp"}
    environment.owner_failures = {0: OSError("capture harness cleanup failed")}
    calls, result = [], None
    def forbidden(*args):
        pytest.fail("failed external sink reached internal checker")
    monkeypatch.setattr(LabExperiment, "observe", forbidden)
    def sink(item):
        calls.append(item)
        raise primary
    with pytest.raises(type(primary)) as caught:
        result = scenario_runtime.run_manifest(manifest, qemu=environment.binary, on_observation=sink)
    assert caught.value is primary and result is None
    assert len(calls) == 1 and calls[0].name == "east" and calls[0].events[0].data == b"OK"
    assert environment.uart_reads == [1, 0] and environment.native.steps == []
    assert all(not cpu.requests for cpu in environment.cpus.values())
    notes = "\n".join(primary.__notes__)
    assert all(label in notes for label in ("world cleanup", "nested capture cleanup", "uart1", "qmp", "harness cleanup"))
    environment.assert_clean()
    assert all(environment.events.count(f"upper{index} stop") == 1 for index in range(2))


def test_scenario_capture_actual_assertion_failure_retains_its_batch_without_completion(environment):
    from io import BytesIO
    from sim.arena.trace import ArenaTrace, decode_observation
    from sim.scenario import ScenarioAssertion
    from sim.trace import TraceWriter, iter_events

    manifest = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(),
        2_500_000, (), assertions=(ScenarioAssertion("east",
            Assertion(1_000_000, "uart1", "uart_contains", b"missing")),))
    clocks = {"east": "qemu:east:run16", "west": "qemu:west:run16"}
    output = BytesIO()
    writer = TraceWriter(output)
    capture, result = ArenaTrace(writer, clocks=clocks), None
    with pytest.raises(RuntimeError, match="east.*did not match before"):
        result = scenario_runtime.run_manifest(manifest, qemu=environment.binary, on_observation=capture)
        capture.complete(result)
    assert result is None
    environment.assert_clean()
    writer.finish()  # Keep diagnostic observations, without pretending the run completed.
    events = list(iter_events(BytesIO(output.getvalue())))
    assert len(events) == 3 and all(event.event_type == "arena.observation/1" for event in events)
    batches = [decode_observation(event, clocks=clocks) for event in events]
    assert [(item.name, item.snapshot["timestamp_ns"], item.events) for item in batches] == [
        ("east", 1000, []), ("west", 2000, []), ("east", 1_001_000, [])]
    assert environment.native.steps == [1_000_000] and environment.uart_reads == [2, 1]
    assert not output.closed


def test_scenario_capture_actual_live_assertions_complete_then_original_cli_replay(environment, monkeypatch, capsys):
    from sim.apps import unmigrated_cli as cli
    from sim.arena.trace import ArenaTrace
    from sim.trace import TraceWriter

    arena = environment.manifest(ready=("READY", "READY"), ready_ports=("uart1", "uart1"))
    path = environment.directory / "captured-scenario.yaml"
    path.write_text(yaml.safe_dump({"schema": 1, "arena": arena.path.name, "duration": "2500us",
        "assertions": [
            {"member": "west", "before": "2ms", "uart_contains": {"endpoint": "uart1", "text": "world"}},
            {"member": "east", "before": "2ms", "uart_contains": {"endpoint": "uart1", "text": "hello"}},
            {"member": "east", "before": "1ns", "digital_equals": {"endpoint": "gpio.A0", "value": 1}},
        ]}), encoding="utf-8")
    original = load_scenario(path)
    environment.uart_chunks = [[b"he", b"llo", b"READY"], [b"wo", b"rld", b"READY"]]
    command = environment.command
    def sampled_state(index, name, arguments=None):
        state = command(index, name, arguments)
        if name == "qom-get":
            state["gpio"]["A"].update(sampled=1, odr=0, output_mask=1)
        return state
    monkeypatch.setattr(environment, "command", sampled_state)
    observe, finish = LabExperiment.observe, LabExperiment.finish
    live, current, history, finishes = {}, None, [], []
    def checker(experiment, state, events, bound):
        assert current is not None and state is current.snapshot and events is current.events
        observe(experiment, state, events, bound)
        live[experiment.manifest.name] = experiment
    def finish_after_cleanup(experiment, stamp):
        environment.assert_clean()
        finishes.append((experiment.manifest.name, stamp))
        return finish(experiment, stamp)
    trace = environment.directory / "scenario-capture.mcap"
    clocks = {"east": "qemu:east:run16", "west": "qemu:west:run16"}
    with monkeypatch.context() as live_spies:
        live_spies.setattr(LabExperiment, "observe", checker)
        live_spies.setattr(LabExperiment, "finish", finish_after_cleanup)
        with trace.open("xb") as output:
            writer = TraceWriter(output)
            capture = ArenaTrace(writer, clocks=clocks)
            def sink(item):
                nonlocal current
                current = item
                capture(item)
                history.append(item)
            result = scenario_runtime.run_manifest(original, qemu=environment.binary, on_observation=sink)
            environment.assert_clean()
            assert len(finishes) == 2 and live["east"].status == ("pass", "pass")
            assert live["west"].status == ("pass",)
            capture.complete(result)
            writer.finish()
    assert [(item.name, item.snapshot["timestamp_ns"], tuple(event.data for event in item.events))
            for item in history[:4]] == [
        ("east", 1000, (b"he",)), ("west", 2000, (b"wo",)),
        ("east", 1_001_000, (b"llo",)), ("west", 1_002_000, (b"rld",))]
    assert any(not item.events for item in history)
    protected = path.read_bytes(), arena.path.read_bytes(), trace.read_bytes()
    lifecycle = list(environment.events)
    def forbidden(*args, **kwargs):
        pytest.fail("offline Scenario capture replay acquired native resources")
    monkeypatch.setattr(runtime, "running_qemu", forbidden)
    monkeypatch.setattr(runtime, "create_arena_plant", forbidden)
    monkeypatch.setattr(scenario_runtime, "run_manifest", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    report = environment.directory / "capture-replayed.json"
    arguments = ["scenario", "replay", str(path), str(trace), "--report", str(report)]
    for member, epoch in clocks.items():
        arguments += ["--clock", f"{member}={epoch}"]
    assert cli.main(arguments) == 0
    payload = json.loads(report.read_text())
    assert payload["status"] == "passed" and payload["result"]["status"] == ["pass", "pass", "pass"]
    assert payload["result"]["failures"] == []
    assert [(item["name"], item["reset_qemu_ns"], item["end_qemu_ns"], item["observation_batches"])
            for item in payload["result"]["completions"]] == [
        (board.name, board.reset_qemu_ns, board.end_qemu_ns, board.observation_batches)
        for board in result.boards]
    assert [board.reset_qemu_ns for board in result.boards] == [1000, 2000]
    assert all(board.ready_qemu_ns > board.reset_qemu_ns for board in result.boards)
    assert "Scenario replay: passed" in capsys.readouterr().out
    assert environment.events == lifecycle
    assert (path.read_bytes(), arena.path.read_bytes(), trace.read_bytes()) == protected
    environment.assert_clean()
