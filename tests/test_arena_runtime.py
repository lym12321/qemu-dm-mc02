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
from sim.core import Coordinator, Session, StepContext
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.devices.referee import Hit, RefereeEngine, RefereeUartEndpoint, RobotStatus, ScriptEvent, Shot
from sim.fabric import CanBus
from sim.fabric.endpoints import QemuCanEndpoint
from sim.robots import shared_world
from sim.scenario import ScenarioCanDelay, ScenarioManifest, ScenarioReferee, load_manifest as load_scenario
from sim.scenario import runtime as scenario_runtime
from sim.transports import cosim
from tools import dm_mc02_firmware_rtf as rtf
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
        for module in (runtime, shared_world, board_qmp, cosim, rtf):
            monkeypatch.setattr(module, "time", clock)
        monkeypatch.setattr(runtime, "select", SimpleNamespace(select=self.select))

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
    observed = SimpleNamespace(engines=[], endpoints=[], worlds=[], coordinators=[], channels=[],
                               services=[], resets=[], steps=[], queues=[], attempts=[], publications=[], closes=[],
                               close_failures={})
    for owner, collection in ((RefereeEngine, observed.engines),
                              (RefereeUartEndpoint, observed.endpoints),
                              (shared_world.SharedWorldRuntime, observed.worlds),
                              (Coordinator, observed.coordinators)):
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
            (3_000_500, 10, 3_000_500, 3_000_500, 3_001_000, 8192, 8192),
            (2_000_500, 5, 4_000_500, 4_000_500, 4_001_000, 0, 8192),
            (4_000_500, 14, 4_000_500, 4_000_500, 4_001_000, 8192, 8192),
        ]
        groups = ((3_001_000, "0000"), (3_001_000, "2000"),
                  (4_001_000, "0000"), (4_001_000, "2000"))
    else:
        assert [(row["world_ns"], row["due_ns"], row["delivered_ns"], row["qemu_ns"],
                 row["producer_current_raw"], row["send_live_current_raw"]) for row in east] == [
            (1_000_500, 1_000_500, 1_000_500, 1_001_000, 4096, 4096),
            (2_000_500, 2_000_500, 2_000_500, 2_001_000, 4096, 4096),
            (3_000_500, 3_000_500, 3_000_500, 3_001_000, 4096, 4096),
            (4_000_500, 4_000_500, 4_000_500, 4_001_000, 4096, 4096),
        ]
        groups = ((1_001_000, "1000"), (2_001_000, "1000"),
                  (3_001_000, "1000"), (4_001_000, "1000"))
    west = [row for row in rows if row["peer"] == 1 and row["can_id"] == 0x201]
    assert [(row["world_ns"], row["due_ns"], row["delivered_ns"], row["qemu_ns"],
             row["producer_current_raw"]) for row in west] == [
        (1_000_500, 1_000_500, 1_000_500, 1_002_000, -4096),
        (2_000_500, 2_000_500, 2_000_500, 2_002_000, -4096),
        (3_000_500, 3_000_500, 3_000_500, 3_002_000, -4096),
        (4_000_500, 4_000_500, 4_000_500, 4_002_000, -4096),
    ]
    for index, group in enumerate((groups, ((1_002_000, "f000"), (2_002_000, "f000"),
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
        from sim import cli
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
    # Independent fixed C620 bytes: the new command is admitted after the policy
    # change, while the old delayed command remains pending beyond this run.
    zero = bytes.fromhex("0000 0000 0000 19 00")
    current = bytes.fromhex("0000 0000 0800 19 00")
    for index, stamp in enumerate((1_001_000, 1_002_000)):
        expected = b"".join(struct.pack("<IIB3xQ64s", 0x200 + motor_id, 0, 8, stamp,
            (current if index == 0 and motor_id == 1 else zero).ljust(64, b"\0"))
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
        (500_000, 1_000_000, 1_000_000)]
    assert all(not any(event.endswith(" cont") for event in row[4]) for row in observed.calls[:2])
    assert all(row[5] == (0, 0) for row in observed.calls[:2])
    assert observed.calls[2][5] == observed.calls[3][5] == (1, 1)
    expected_steps = [500_000, 1_000_000, 1_000_000]
    if delayed_ready:
        expected_steps += [1_000_000, 1_000_000, 500_000]
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
    assert len(referees.worlds) == len(referees.coordinators) == 1
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
    assert environment.native.steps == [1_000_000, 500_000, 1_000_000, 1_000_000, 500_000]
    assert (result.origin_ns, result.end_ns, result.steps) == (0, 4_000_000, 5)
    assert boundaries == [(0, (0, 0)), (1_000_000, (6, 0)), (1_500_000, (6, 0))]
    assert [item.endpoint.bus.delay_ns for item in observed.worlds[0].bindings] == [0, 0]
    old, future, newer = (bytes.fromhex(value) for value in (
        "1000 0000 0000 0000", "3000 0000 0000 0000", "2000 0000 0000 0000"))
    assert [row for row in ingresses if row[1] in (future, newer)] == [
        (1_701_000, future, 0, 2_000_000), (1_201_000, newer, 1_500_000, 0)]
    assert [(row.frame.data, row.sequence, row.timestamp_ns, row.due_ns, row.delivered_ns, row.late)
            for row in deliveries[0] if row.destination == "m3508-1" and row.frame.can_id == 0x200] == [
        (newer, 6, 1_200_000, 1_200_000, 1_500_000, True),
        (old, 0, 0, 2_000_000, 2_500_000, True),
        (future, 1, 1_700_000, 3_700_000, 4_000_000, True)]
    # Independent C620 layouts, fixed stationary rotor and separate QEMU epochs.
    # New feedback overtakes the completed1ms batch, whose old2ms due remains.
    zero = bytes.fromhex("0000 0000 0000 19 00")
    feedbacks = (
        ((2_501_000, "2000"), (3_501_000, "0000"), (3_501_000, "1000"), (4_001_000, "1000")),
        ((1_002_000, "f000"), (2_502_000, "f000"), (3_502_000, "f000"), (4_002_000, "f000")),
    )
    for index, groups in enumerate(feedbacks):
        expected = b"".join(struct.pack("<IIB3xQ64s", 0x200 + motor_id, 0, 8, stamp,
            (bytes.fromhex("0000 0000 " + current + " 19 00") if motor_id == 1 else zero).ljust(64, b"\0"))
            for stamp, current in groups for motor_id in range(1, 5))
        assert environment.received_can(index) == expected
    assert [(row.sequence, row.timestamp_ns, row.due_ns, row.delivered_ns)
            for row in deliveries[0] if row.destination == "qemu" and row.frame.can_id == 0x201] == [
        (7, 2_500_000, 2_500_000, 2_500_000), (2, 1_000_000, 3_000_000, 3_500_000),
        (11, 3_500_000, 3_500_000, 3_500_000), (15, 4_000_000, 4_000_000, 4_000_000)]
    ratio = 3591 / 187  # Pinned DJI M3508 specification, no production converter.
    expected_currents = ((0, -4096), (0, -4096), (8192, -4096), (4096, -4096), (4096, -4096))
    for offset, currents in zip(range(0, 40, 8), expected_currents):
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
    assert environment.native.steps == [1_000_000, 500_000]
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
    assert environment.native.steps == [1_000_000, 500_000]
    assert all(process.returncode == -signal.SIGTERM for process in environment.children)


@pytest.mark.parametrize("via_scenario", [False, True], ids=["arena", "scenario"])
def test_can_delay_split_native_refusal_freezes_without_rounding_retry_or_target_action(
        environment, monkeypatch, via_scenario):
    observed = watch_can_delays(environment, monkeypatch)
    primary, admissions, closing = ValueError("native interval500000 refused"), [], []
    def validate(intervals):
        admissions.append(intervals)
        if intervals == (500_000,):
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
    assert admissions == [(1_000_000,), (500_000,)]
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
    for group in (environment.native.efforts[offset:offset + 8] for offset in (0, 8, 16)):
        assert [name for name, _ in group] == [
            "alpha/wheel_fl", "alpha/wheel_fr", "alpha/wheel_rl", "alpha/wheel_rr",
            "bravo/wheel_fl", "bravo/wheel_fr", "bravo/wheel_rl", "bravo/wheel_rr"]
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
    with pytest.raises((TimeoutError, RuntimeError), match="startup|deadline|timeout"):
        environment.run(environment.manifest(ready=("NEVER", None)),
                        startup_timeout=.003, timeout=.01)
    environment.assert_clean()
    assert environment.native.resets == 1 and environment.native.steps
    assert environment.waits and all(0 < value <= .001 for value in environment.waits)


def test_active_deadline_is_not_restarted_after_ready(environment):
    environment.uart_chunks = [[b"not yet", b"READY"], []]
    with pytest.raises((TimeoutError, RuntimeError), match="host|deadline|timeout"):
        environment.run(environment.manifest(ready=("READY", None)), duration=.1,
                        startup_timeout=.002, timeout=.003)
    environment.assert_clean()
    assert environment.native.resets == 1 and environment.native.steps
    assert sum(environment.native.steps) < 100_000_000


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
    assert len(observed.worlds) == len(observed.coordinators) == 1
    assert observed.services and not any(observed.services)
    assert list(observed.coordinators[0].participants) == ["can", "referee:east", "referee:west"]
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
