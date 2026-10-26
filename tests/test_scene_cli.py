"""Public scene/CLI admission and the concrete fixed-state device consumer."""

import ast
import builtins
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from sim.api import LinkSpec, NodeDescription, NodeSpec, PortSpec, Topology
from sim.composition import builtin_registry
from sim.config.scene import load_scene
from sim.fabric import CanBusFactory, CanFrame
from sim.runtime import Simulation

ROOT = Path(__file__).resolve().parents[1]


def write_scene(tmp_path, *, motor=False):
    (tmp_path / "firmware.elf").write_bytes(b"test admission only")
    document = {"schema": "sim/v1", "runtime": {"quantum": "1ms", "duration": "150ms"},
                "nodes": {"main": {"kind": "machine.qemu", "model": "dm-mc02",
                                    "firmware": "firmware.elf"}}, "links": []}
    if motor:
        document["nodes"].update({"can0": {"kind": "bus.can"},
            "motor0": {"kind": "device.dji-m3508", "motor_id": 1,
                       "plant": "fixed-rotor", "position_rad": 0, "velocity_rad_s": 0}})
        document["links"] = [["main.fdcan1", "can0.can"], ["motor0.can", "can0.can"]]
    path = tmp_path / "scene.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    return path, document


def test_load_resolves_paths_without_starting_native(tmp_path, monkeypatch):
    path, _ = write_scene(tmp_path, motor=True)
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("native acquisition"))
    scene = load_scene(path, builtin_registry())
    assert scene.quantum_ns == 1_000_000 and scene.duration_ns == 150_000_000
    assert scene.topology.nodes[0].base_dir == tmp_path
    assert len(scene.topology.links) == 2


@pytest.mark.parametrize("change", [
    lambda d: d.update(schema=1),
    lambda d: d.update(unknown=True),
    lambda d: d["runtime"].update(mode="free-running"),
    lambda d: d["nodes"]["main"].update(model="invented-board"),
    lambda d: d["nodes"]["main"].update(shell="true"),
    lambda d: d["nodes"].update(external={"kind": "host.shell", "argv": ["true"]}),
    lambda d: d["links"].append(["main.fdcan99", "can0.can"]),
    lambda d: d["links"].append(["main.uart1", "can0.can"]),
    lambda d: d["links"].append(["main.fdcan1", "can0.can"]),
    lambda d: d["nodes"]["motor0"].update(motor_id=True),
    lambda d: d["nodes"]["motor0"].update(plant="implied-dynamics"),
])
def test_bad_scene_rejected_before_construction(tmp_path, change, monkeypatch):
    path, document = write_scene(tmp_path, motor=True)
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    registry = builtin_registry()
    for factory in registry.values():
        monkeypatch.setattr(factory, "create", lambda *_: pytest.fail("configuration had effects"))
    with pytest.raises((ValueError, TypeError)):
        load_scene(path, registry)


def test_unique_yaml_loader_still_rejects_alias_and_duplicate(tmp_path):
    path = tmp_path / "bad.yaml"
    for text in ("schema: sim/v1\nschema: sim/v1\nnodes: {}\n",
                 "schema: sim/v1\nnodes: &nodes {}\nlinks: *nodes\n"):
        path.write_text(text)
        with pytest.raises(ValueError):
            load_scene(path, builtin_registry())


@pytest.mark.parametrize("plant", ["fixed-rotor", "rigid-rotor"])
def test_rotor_plants_validate_without_physics_imports_or_resource_creation(tmp_path, monkeypatch, plant):
    path, document = write_scene(tmp_path, motor=True)
    document["nodes"]["motor0"]["plant"] = plant
    path.write_text(yaml.safe_dump(document, sort_keys=False))
    # The registry now includes a pure physics factory. Rotor admission itself
    # must still avoid importing physics implementations or creating resources.
    registry = builtin_registry()
    original_import = builtins.__import__

    def refuse_physics(name, *args, **kwargs):
        if name == "sim.physics" or name.startswith("sim.physics."):
            pytest.fail("pure factory admission imported physics")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_physics)
    for factory in registry.values():
        monkeypatch.setattr(factory, "create", lambda *_: pytest.fail("configuration had effects"))
    assert load_scene(path, registry).topology.nodes[-1].options["plant"] == plant


def test_core_static_import_direction():
    forbidden = ("sim.machines", "sim.devices", "sim.lab", "sim.robots", "sim.arena",
                 "sim.scenario", "sim.physics", "tools", "qemu", "mujoco", "gazebo")
    for directory in ("api", "runtime", "config", "fabric"):
        for path in (ROOT / "sim" / directory).rglob("*.py"):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
                assert not any(name == prefix or name.startswith(prefix + ".")
                               for name in names for prefix in forbidden), (path, names)
                if directory in ("api", "runtime", "config"):
                    assert not any(name == "sim.fabric" or name.startswith("sim.fabric.")
                                   for name in names), (path, names)


def test_help_and_validate_refuse_optional_imports_in_fresh_process(tmp_path):
    path, document = write_scene(tmp_path)
    document["experiment"] = {
        "ready": {"endpoint": "main.uart1", "text": "READY"},
        "inputs": [{"at": "1ms", "endpoint": "main.uart1", "value": "hello\0é"}],
        "assertions": [{"before": "10ms", "uart_contains": {"endpoint": "main.uart1", "text": "OK"}}],
    }
    path.write_text(yaml.safe_dump(document))
    program = '''
import importlib.abc, sys
sys.path.insert(0, sys.argv[1])
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'qemu','tools','mujoco','mcap','crccheck','aiohttp'}:
            raise AssertionError('optional import: '+fullname)
sys.meta_path.insert(0, Guard())
from sim.cli import main
raise SystemExit(main(sys.argv[2:]))
'''
    result = subprocess.run([sys.executable, "-I", "-c", program, str(ROOT), "validate", str(path)],
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "valid:" in result.stdout
    result = subprocess.run([sys.executable, "-I", "-c", program, str(ROOT), "--help"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "validate" in result.stdout and "run" in result.stdout
    assert "robot" not in result.stdout and "lab" not in result.stdout


def test_scene_experiment_pure_admission_canonicalizes_inputs_and_freezes_order(tmp_path, monkeypatch):
    path, document = write_scene(tmp_path)
    document["experiment"] = {
        "ready": {"endpoint": "main.uart1", "text": "就绪\0"},
        "inputs": [{"at": "2ms", "endpoint": "main.uart1", "value": "one\0é"},
                   {"at": "2ms", "endpoint": "main.key", "value": True},
                   {"at": "0ns", "endpoint": "main.gpio.B0", "value": 1}],
        "assertions": [{"before": "10ms", "digital_equals": {"endpoint": "main.led", "value": 1}}],
    }
    path.write_text(yaml.safe_dump(document))
    registry = builtin_registry()
    for factory in registry.values():
        monkeypatch.setattr(factory, "create", lambda *_: pytest.fail("static scene acquired resources"))
    configured = load_scene(path, registry)
    experiment = configured.experiment
    assert str(experiment.ready.endpoint) == "main.uart1"
    assert experiment.ready.text == "就绪\0".encode()
    assert [(item.at_ns, str(item.endpoint), item.payload) for item in experiment.inputs] == [
        (2_000_000, "main.uart1", b"one\0\xc3\xa9"),
        (2_000_000, "main.key", True), (0, "main.gpio.B0", 1)]
    with pytest.raises(TypeError):
        experiment.assertions[0]["before"] = "1ns"


@pytest.mark.parametrize("experiment", [
    {"unknown": True},
    {"ready": {"endpoint": "main.uart1", "text": ""}},
    {"ready": {"endpoint": "main.key", "text": "READY"}},
    {"ready": {"endpoint": "absent.uart1", "text": "READY"}},
    {"inputs": [{"at": "1ms", "endpoint": "main.key", "value": 1}]},
    {"inputs": [{"at": "151ms", "endpoint": "main.key", "value": True}]},
    {"inputs": [{"at": "1ms", "endpoint": "main.key", "value": True, "extra": 1}]},
    {"inputs": [{"at": "1ms", "endpoint": "main.key"}]},
    {"inputs": [{"at": "0ns", "endpoint": "main.key", "value": True} for _ in range(1025)]},
    {"assertions": [{"before": "151ms", "uart_contains": {"endpoint": "main.uart1", "text": "OK"}}]},
    {"assertions": [{"before": "1ms", "uart_contains": {"endpoint": "main.key", "text": "OK"}}]},
])
def test_bad_scene_experiment_rejects_before_any_component_creation(tmp_path, monkeypatch, experiment):
    path, document = write_scene(tmp_path)
    document["experiment"] = experiment
    path.write_text(yaml.safe_dump(document))
    registry = builtin_registry()
    for factory in registry.values():
        monkeypatch.setattr(factory, "create", lambda *_: pytest.fail("invalid scene acquired resources"))
    with pytest.raises((ValueError, TypeError)):
        load_scene(path, registry)


def test_scene_adc_batches_validate_before_io_and_distinguish_ready_origin(tmp_path, monkeypatch):
    path, document = write_scene(tmp_path)
    document["nodes"]["main"]["adc"] = True
    registry = builtin_registry()
    for factory in registry.values():
        monkeypatch.setattr(factory, "create", lambda *_: pytest.fail("ADC admission acquired resources"))
    input_at_zero = {"at": "0ns", "endpoint": "main.adc1.ch4", "value": {"raw": 12345}}
    document["experiment"] = {"inputs": [input_at_zero]}
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError, match="origin"):
        load_scene(path, registry)
    document["experiment"]["ready"] = {"endpoint": "main.uart1", "text": "READY"}
    path.write_text(yaml.safe_dump(document))
    configured = load_scene(path, registry)
    assert configured.experiment.inputs[0].payload == {"raw": 12345}
    document["experiment"]["inputs"] = [input_at_zero, {
        "at": "0ns", "endpoint": "main.adc1.ch4", "value": {"voltage_uv": 1650000}}]
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError, match="duplicate|conflict"):
        load_scene(path, registry)
    document["experiment"]["inputs"] = [{"at": "1ms", "endpoint": f"main.adc1.ch{index}",
                                          "value": {"raw": 1}} for index in range(17)]
    path.write_text(yaml.safe_dump(document))
    with pytest.raises(ValueError, match="16"):
        load_scene(path, registry)


def _cli_experiment(tmp_path, monkeypatch, *, payload="OK", close_error=None):
    from sim import composition

    report = tmp_path / "report.json"
    closed, delivered = [], []

    class Producer:
        def __init__(self, context):
            self.context = context

        def sample(self, cut):
            fragment = {10: b"R", 20: b"EADY OK"}.get(cut)
            if fragment is not None:
                self.publish(fragment)

        def publish(self, value):
            cut = self.context.t_sim_ns
            self.context.emit("uart", "uart", value, source_time_ns=700 + cut,
                              mapped_time_ns=cut, quality="upper_bound")

        def input(self, port, value):
            delivered.append((self.context.t_sim_ns, value))
            self.publish(value)

        def close(self):
            assert not report.exists(), "report was published before resource cleanup"
            closed.append(True)
            if close_error is not None:
                raise close_error

    class Factory:
        def describe(self, spec):
            return NodeDescription((PortSpec("uart", "uart", capabilities={"external_input"}),),
                                   {"sample", "input"})

        def validate_input(self, spec, port, value, *, connected_ports=()):
            if type(value) is str:
                value = value.encode("utf-8")
            if type(value) is not bytes or len(value) > 4096:
                raise ValueError("bounded UART bytes required")
            return value

        def create(self, spec, context):
            return Producer(context)

    monkeypatch.setattr(composition, "builtin_registry", lambda **kwargs: {"producer": Factory()})
    path = tmp_path / "cli-experiment.yaml"
    path.write_text(yaml.safe_dump({"schema": "sim/v1", "runtime": {
        "quantum": "10ns", "duration": "20ns"}, "nodes": {"port": {"kind": "producer"}},
        "experiment": {"ready": {"endpoint": "port.uart", "text": "READY"},
            "inputs": [{"at": "3ns", "endpoint": "port.uart", "value": payload}],
            "assertions": [{"before": "10ns", "uart_contains": {
                "endpoint": "port.uart", "text": "OK"}}]}}))
    return path, report, closed, delivered


@pytest.mark.parametrize("payload,expected_code,status", [("OK", 0, "passed"), ("BAD", 1, "failed")])
def test_cli_experiment_reports_real_ready_relative_assertions_after_cleanup(
        tmp_path, monkeypatch, capsys, payload, expected_code, status):
    from sim import cli

    path, report, closed, delivered = _cli_experiment(tmp_path, monkeypatch, payload=payload)
    assert cli.main(["run", str(path), "--report", str(report)]) == expected_code
    result = json.loads(report.read_text())
    assert result["status"] == status and closed == [True]
    assert delivered == [(23, payload.encode())]
    assert result["result"]["origin_ns"] == 20 and result["result"]["end_ns"] == 40
    assert result["result"]["delivered_inputs"] == 1
    ready = result["result"]["ready_receipt"]
    assert (ready["source_time_ns"], ready["mapped_time_ns"], ready["delivery_time_ns"]) == (720, 20, 20)
    assert result["result"]["status"] == (["pass"] if expected_code == 0 else ["fail"])
    assert f"{status}: time_ns=40" in capsys.readouterr().out


def test_cli_experiment_cleanup_failure_never_publishes_pass(tmp_path, monkeypatch, capsys):
    from sim import cli

    path, report, closed, _ = _cli_experiment(tmp_path, monkeypatch, close_error=OSError("close failed"))
    assert cli.main(["run", str(path), "--report", str(report)]) == 1
    result = json.loads(report.read_text())
    assert result["status"] == "failed" and result["error_type"] == "OSError"
    assert result["error"] == "close failed" and closed == [True]
    assert "passed:" not in capsys.readouterr().out


def test_cli_experiment_sink_interrupt_preserves_primary_and_cleanup_notes(tmp_path, monkeypatch, capsys):
    from sim import cli

    path, report, closed, _ = _cli_experiment(tmp_path, monkeypatch, close_error=OSError("close failed"))
    primary = KeyboardInterrupt("sink interrupted")
    def interrupted(event):
        if event.kind == "uart" and event.payload == b"OK":
            raise primary
    monkeypatch.setattr(cli, "_emit", interrupted)
    assert cli.main(["run", str(path), "--report", str(report)]) == 130
    result = json.loads(report.read_text())
    assert result["status"] == "failed" and result["error_type"] == "KeyboardInterrupt"
    assert result["error"] == "sink interrupted" and closed == [True]
    assert "close failed" in "\n".join(result["notes"])
    assert "close failed" in "\n".join(primary.__notes__)
    assert "passed:" not in capsys.readouterr().out


def test_teaching_scene_admits_qualified_key_inputs_without_native_acquisition(tmp_path, monkeypatch):
    document = yaml.safe_load((ROOT / "examples/teaching/gpio-key.yaml").read_text())
    firmware = tmp_path / "lesson.elf"
    firmware.write_bytes(b"static admission only")
    document["nodes"]["main"]["firmware"] = str(firmware)
    path = tmp_path / "lesson.yaml"
    path.write_text(yaml.safe_dump(document))
    registry = builtin_registry()
    for factory in registry.values():
        monkeypatch.setattr(factory, "create", lambda *_: pytest.fail("lesson admission acquired resources"))
    scene = load_scene(path, registry)
    assert scene.duration_ns == 800_000_000 and scene.quantum_ns == 1_000_000
    assert scene.topology.nodes[0].options["uarts"] == ()
    assert scene.experiment.ready is None
    assert [(item.at_ns, str(item.endpoint), item.payload) for item in scene.experiment.inputs] == [
        (100_000_000, "main.key", True), (600_000_000, "main.key", False)]
    assert scene.experiment.assertions == ({"before": "500ms", "digital_equals": {
        "endpoint": "main.gpio.B0", "value": 1}},)


@pytest.mark.parametrize("constant_low", [False, True])
def test_teaching_automatic_consumer_uses_real_simulation_cli_and_report(
        tmp_path, monkeypatch, constant_low):
    from contextlib import redirect_stderr, redirect_stdout
    from io import StringIO
    from sim import cli, composition
    from smoke import dm_mc02_teaching_smoke as teaching

    firmware = tmp_path / "lesson.elf"
    firmware.write_bytes(b"pure consumer fixture")
    report = tmp_path / "simulation-result.json"
    delivered, closed = [], []
    admission = builtin_registry()["machine.qemu"]

    class GpioFixture:
        level = 0

        def __init__(self, context):
            self.context = context

        def input(self, port, value):
            delivered.append((self.context.t_sim_ns, port, value))
            self.level = 0 if constant_low else int(value)

        def sample(self, cut):
            state = {"schema": 1, "timestamp_ns": cut, "board": "fixture", "vin_mv": 24000,
                "gpio": {bank: {"moder": 0, "sampled": self.level if bank == "B" else 0,
                                  "odr": 0, "output_mask": 0} for bank in "ABCDEFGH"},
                "key": {"pin": "A15", "pressed": False, "level": 1},
                "led": {"pin": "A7", "rgb": 0, "brightness": 0, "source": "gpio-latch"}}
            self.context.emit("snapshot", "snapshot", state, source_time_ns=cut,
                              mapped_time_ns=cut, quality="exact")

        def close(self):
            assert not report.exists(), "teaching report preceded cleanup"
            closed.append(True)

    class Factory:
        def describe(self, spec):
            description = admission.describe(spec)
            return NodeDescription(description.ports,
                (description.capabilities - {"machine", "input_commit"}) | {"sample"})

        def validate_input(self, *args, **kwargs):
            return admission.validate_input(*args, **kwargs)

        def create(self, spec, context):
            assert spec.options["firmware"] == str(firmware)
            return GpioFixture(context)

    monkeypatch.setattr(composition, "builtin_registry", lambda **kwargs: {"machine.qemu": Factory()})

    def command(argv, deadline, record):
        assert argv[:3] == [sys.executable, str(ROOT / "tools/sim"), "run"]
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(argv[2:])
        record.update(argv=argv, returncode=code, stdout=out.getvalue(), stderr=err.getvalue())
        return record

    monkeypatch.setattr(teaching, "command", command)
    record = {}
    teaching.simulation_cli(firmware, tmp_path / "unused-qemu",
        ROOT / "examples/teaching/gpio-key.yaml", tmp_path, record, expect_failure=constant_low)
    assert closed == [True]
    assert delivered == [(100_000_000, "key", True), (600_000_000, "key", False)]
    result = record["report"]["result"]
    assert result["status"] == (["fail"] if constant_low else ["pass"])
    if not constant_low:
        # A cut's input cannot alter the snapshot already sampled at that cut.
        assert result["matches"][0]["delivery_time_ns"] == 101_000_000


def test_missing_native_backend_is_blocked_and_preserves_report(tmp_path):
    from sim import cli
    path, _ = write_scene(tmp_path)
    report = tmp_path / "report.json"
    assert cli.main(["run", str(path), "--qemu", str(tmp_path / "missing"),
                     "--report", str(report)]) == 78
    assert json.loads(report.read_text())["status"] == "blocked"
    previous = report.read_bytes()
    assert cli.main(["run", str(path), "--report", str(report),
                     "--qemu", str(tmp_path / "missing")]) == 78
    assert report.read_bytes() == previous


def test_native_backends_share_availability_type_and_missing_mujoco_is_blocked(tmp_path, monkeypatch):
    from sim import cli
    from sim.api import BackendUnavailable
    from sim.machines.qemu.errors import BackendUnavailable as QemuUnavailable

    assert QemuUnavailable is BackendUnavailable
    model = tmp_path / "world.xml"
    model.write_text("not compiled during validation")
    scene = tmp_path / "scene.yaml"
    scene.write_text(yaml.safe_dump({"schema": "sim/v1", "nodes": {
        "world": {"kind": "physics.mujoco", "model": "world.xml",
                  "joints": {"shaft": "drive"}}}}))
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "mujoco":
            raise ModuleNotFoundError("missing optional MuJoCo", name="mujoco")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    assert cli.main(["validate", str(scene)]) == 0
    report = tmp_path / "missing.json"
    assert cli.main(["run", str(scene), "--report", str(report)]) == 78
    result = json.loads(report.read_text())
    assert result["status"] == "blocked" and "mujoco extra" in result["error"]


def test_fixed_rotor_reuses_actual_protocol_and_timeout():
    from sim.devices.dji.factory import M3508Factory

    class Sender:
        def __init__(self, spec, context):
            self.connection = context.attach("can", self)

        def reset(self, session):
            self.connection.send(CanFrame(0x200, data=bytes.fromhex("0040000000000000")))

        def receive(self, frame):
            pass

        def close(self):
            pass

    class SenderFactory:
        def describe(self, spec):
            return NodeDescription((PortSpec("can", "can"),))

        def create(self, spec, context):
            return Sender(spec, context)

    registry = {"bus.can": CanBusFactory(), "fixed": M3508Factory(), "sender": SenderFactory()}
    topology = Topology((NodeSpec("bus", "bus.can"), NodeSpec("motor", "fixed", {
        "motor_id": 1, "plant": "fixed-rotor", "position_rad": 0,
        "velocity_rad_s": 0}), NodeSpec("host", "sender")),
        (LinkSpec("bus.can", "motor.can"), LinkSpec("host.can", "bus.can")))
    observed = []
    with Simulation(topology, registry, on_event=observed.append) as simulation:
        simulation.step()
        feedback = [event.payload["frame"] for event in observed
                    if event.kind == "can.sent" and event.source_node == "motor"]
        assert feedback[-1].can_id == 0x201
        assert feedback[-1].data == bytes.fromhex("0000000000401900")
        simulation.run_until(101_000_000)
        feedback = [event.payload["frame"] for event in observed
                    if event.kind == "can.sent" and event.source_node == "motor"]
        assert feedback[-1].data == bytes.fromhex("0000000000001900")
        assert simulation.components["motor"].device.get_rotor_state() == (0.0, 0.0)


class InteractiveSimulation:
    state = "paused"
    t_sim_ns = 0

    def __init__(self):
        self.cuts = []

    def step(self, duration):
        self.t_sim_ns += duration
        self.cuts.append(self.t_sim_ns)

    def reset(self):
        self.t_sim_ns = 0

    def check_deadline(self):
        pass


def test_interactive_drains_piped_lines_and_final_unterminated_command(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from sim.cli import _interactive
    source = tmp_path / "commands"
    source.write_text("step 2ms\nstate\nreset\nstep 3ms")
    simulation = InteractiveSimulation()
    with source.open() as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        _interactive(simulation, SimpleNamespace(quantum_ns=1_000_000, duration_ns=10_000_000))
    assert simulation.cuts == [2_000_000, 3_000_000]
    assert '"time_ns": 2000000' in capsys.readouterr().out


def test_interactive_discards_long_line_and_continues_at_newline(tmp_path, monkeypatch, capsys):
    from sim.cli import _interactive
    source = tmp_path / "commands"
    source.write_text("a" * 20_000 + "\nstep 1ms\nquit\n")
    simulation = InteractiveSimulation()
    with source.open() as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        _interactive(simulation, None)
    assert simulation.cuts == [1_000_000]
    assert "discarded through newline" in capsys.readouterr().err


def test_interactive_component_error_escapes_for_cleanup(tmp_path, monkeypatch):
    from sim.cli import _interactive
    source = tmp_path / "commands"
    source.write_text("step 1ms\nquit\n")
    simulation = InteractiveSimulation()
    primary = ValueError("component failed after effect")
    def failed_step(_duration):
        raise primary
    simulation.step = failed_step
    with source.open() as stream:
        monkeypatch.setattr(sys, "stdin", stream)
        with pytest.raises(ValueError) as caught:
            _interactive(simulation, None)
    assert caught.value is primary
