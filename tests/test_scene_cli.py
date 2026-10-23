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
    path, _ = write_scene(tmp_path)
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
