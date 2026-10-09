"""Actual shared-world/core/transport owners with isolated external acquisition.

Socketpairs and OS pipes remain real. Only physics, QEMU/QMP acquisition and
their independent response producers are fixtures; no protocol or core state
machine is replaced. Native QEMU and MuJoCo are separate consumer gates.
"""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
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
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.fabric import CanBus
from sim.robots import shared_world
from sim.transports import cosim
from tools import dm_mc02_firmware_rtf as rtf
from test_arena_manifest import ROBOT_DOCUMENT, write_arena, write_executable
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
            chunks = self.environment.uart_chunks[self.index]
            if chunks:
                self.producer.send(chunks.pop(0))
            self.environment.uart_reads[self.index] += 1
        return self.sock.recv(maximum)

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

    def manifest(self, *, ready=(None, None), upper=False):
        documents = []
        for marker in ready:
            document = ROBOT_DOCUMENT
            if marker is not None:
                document += yaml.safe_dump({"ready": {"endpoint": "main.uart7", "text": marker}})
            if upper:
                document += yaml.safe_dump({"upper_computers": {"vision": {"command": [
                    sys.executable, "literal;$HOME", "$(touch unwanted)", "", "two words"]}}})
            documents.append(document)
        return load_manifest(write_arena(self.directory, robot_documents=tuple(documents)))

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


@pytest.mark.parametrize("options", [
    {"duration": float("nan")}, {"duration": True}, {"duration": 1e-10},
    {"duration": 3601}, {"timeout": float("inf")},
    {"startup_timeout": 3, "timeout": 2}, {"emit": 1}, {"on_response": 1},
])
def test_invalid_options_have_no_physics_process_or_transport_effect(environment, options):
    with pytest.raises((ValueError, TypeError)):
        environment.run(**options)
    assert environment.events == [] and environment.launches == []


def test_referee_configuration_is_rejected_before_options_or_resource_admission(environment, monkeypatch):
    manifest = replace(environment.manifest(), referees=(object(),))
    def forbidden(*args, **kwargs):
        pytest.fail("unsupported Arena referee configuration reached resource admission")
    monkeypatch.setattr(runtime, "resolve_qemu", forbidden)
    monkeypatch.setattr(runtime, "regular_file", forbidden)
    with pytest.raises(ValueError, match="Arena referee execution is not yet supported"):
        environment.run(manifest, duration=float("nan"), emit=object())
    assert environment.events == [] and environment.launches == []


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
