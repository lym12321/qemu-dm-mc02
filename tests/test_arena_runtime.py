"""Actual shared-world/core/transport owners with isolated external acquisition.

Socketpairs and OS pipes remain real. Only physics, QEMU/QMP acquisition and
their independent response producers are fixtures; no protocol or core state
machine is replaced. Native QEMU and MuJoCo are separate consumer gates.
"""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import builtins
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sim import processes
from sim.arena import runtime
from sim.arena.manifest import load_manifest
from sim.core import Coordinator
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.devices.referee import Hit, RefereeEngine, RefereeUartEndpoint, RobotStatus, ScriptEvent, Shot
from sim.fabric import CanBus
from sim.robots import shared_world
from sim.scenario import ScenarioManifest, ScenarioReferee, load_manifest as load_scenario
from sim.scenario import runtime as scenario_runtime
from sim.transports import cosim
from tools import dm_mc02_firmware_rtf as rtf
from test_arena_manifest import ROBOT_DOCUMENT, referee_configuration, write_arena, write_executable
from test_lab_runtime import snapshot
from test_robot_runtime import Pipe
from test_sim_shared_world import Boards, NativeStub, can_wire


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
        self.uart_chunks, self.uart_reads = [[], []], [0, 0]
        self.uart_port_chunks, self.uart_port_reads, self.uart_send_budget = {}, {}, {}
        self.can_commands = [None, None]
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
        for module in (runtime, shared_world, cosim, rtf):
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
        return SimpleNamespace(command=lambda name, arguments=None: self.command(index, name),
                               settimeout=lambda value: None,
                               close=lambda: self.close_qmp(index))

    def connect(self, path, timeout):
        index, name = int(Path(path).parent.name[-1]), Path(path).name
        self.events.append(f"peer{index} {name} connect")
        self.clock += self.connect_delay
        if self.connection_failure == (index, name):
            raise OSError(f"peer{index} {name} connection failure")
        left, right = socket.socketpair()
        for sock in (left, right):
            sock.setblocking(False)
        value = Peer(self, index, name, left, right)
        self.sockets[index, name], self.producers[index, name] = value, right
        if name == "can1" and self.can_commands[index] is not None:
            right.send(can_wire((1000, 2000)[index], self.can_commands[index]))
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

    def command(self, index, name):
        self.qmp_callback(index, name)
        if name == "qom-get":
            stamp = (1000, 2000)[index] + sum(self.native.steps) + 100 * len(self.snapshots[index])
            self.snapshots[index].append((stamp, self.uart_reads[index]))
            return snapshot(stamp)
        self.events.append(f"peer{index} {name}")
        if name == "stop":
            self.pause_callback(index)

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
        assert all(peer.sock.fileno() == -1 for peer in self.sockets.values())
        assert all(process.stdout.file.closed and process.stderr.file.closed
                   for process in self.children)
        assert all(f"owner{index} cleanup" in self.events for index in range(len(self.owners)))

    def release(self):
        for peer in self.sockets.values():
            peer.sock.close()
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
                                    "foreign-record", "member-subclass", "mutable-events"])
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
        scenario_runtime.run_manifest(manifest)
    assert environment.events == [] and environment.owners == []


@pytest.mark.parametrize("duration_ns,seconds", [(1, 0.000000001), (1_234_567, 0.001234567),
                                               (3_600_000_000_000, 3600.0)])
def test_scenario_delegates_once_with_original_order_options_and_exact_duration(
        environment, monkeypatch, duration_ns, seconds):
    arena = environment.manifest()
    first, second = (ScriptEvent(0, Hit(1)),), ()
    manifest = ScenarioManifest(environment.directory / "unread.yaml", arena, duration_ns,
                                (ScenarioReferee("west", first), ScenarioReferee("east", second)))
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
    assert calls[0][1] == dict(options, duration=seconds,
                             referee_events=(("west", first), ("east", second)))
    assert calls[0][1]["referee_events"][0][1] is first
    assert calls[0][1]["referee_events"][1][1] is second
    assert round(calls[0][1]["duration"] * 1_000_000_000) == duration_ns
    assert environment.events == []


@pytest.mark.parametrize("interrupt", [False, True])
def test_scenario_propagates_identical_lower_error_without_retry(environment, monkeypatch, interrupt):
    manifest = ScenarioManifest(environment.directory / "unread.yaml", environment.manifest(), 1, ())
    calls, primary = [], KeyboardInterrupt("Arena interrupted") if interrupt else OSError("Arena rejected")
    def delegate(*args, **kwargs):
        calls.append((args, kwargs))
        raise primary
    monkeypatch.setattr(runtime, "run_manifest", delegate)
    with pytest.raises(type(primary)) as caught:
        scenario_runtime.run_manifest(manifest)
    assert caught.value is primary and len(calls) == 1
    assert environment.events == []


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
    assert all("-S" in command for _, command, _, _ in environment.owners)
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
