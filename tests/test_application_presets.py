"""Application presets are ordinary data consumed by the public scene admission."""
import json
import subprocess

import pytest

from sim.apps.presets import four_wheel_document, add_can_faults
from sim.composition import builtin_registry
from sim.config.scene import load_scene


def test_two_chassis_share_one_admitted_world_without_resources(tmp_path, monkeypatch):
    elf = tmp_path / "admission.elf"
    elf.write_bytes(b"admission only")
    model = tmp_path / "world.xml"
    model.write_text("<mujoco/>")
    document = four_wheel_document({"east": elf, "west": elf}, model,
        ready={"east": "FIRST", "west": "SECOND"})
    path = tmp_path / "scene.yaml"
    path.write_text(json.dumps(document))
    monkeypatch.setattr(subprocess, "Popen", lambda *_a, **_k: pytest.fail("pure admission started process"))
    registry = builtin_registry()
    for factory in registry.values():
        monkeypatch.setattr(factory, "create", lambda *_a: pytest.fail("pure admission created resource"))
    scene = load_scene(path, registry)
    assert len([node for node in scene.topology.nodes if node.kind == "physics.mujoco"]) == 1
    world = next(node for node in scene.topology.nodes if node.name == "world")
    assert world.options["joints"] == {prefix + "wheel_" + corner: prefix + "drive_" + corner
        for prefix in ("alpha/", "bravo/") for corner in ("fl", "fr", "rl", "rr")}
    assert [item.name for item in scene.experiment.ready] == ["east", "west"]


def test_fault_schedule_keeps_session_origin_and_per_motor_route(tmp_path):
    elf = tmp_path / "admission.elf"
    elf.write_bytes(b"admission only")
    model = tmp_path / "world.xml"
    model.write_text("<mujoco/>")
    document = four_wheel_document({"main": elf}, model, duration="1s")
    add_can_faults(document, "main", delays=((0, 5_000_000), (250_000_000, 0)),
                   feedback=((250_000_000, 1, False), (350_000_000, 1, True)))
    assert [(item["at"], item["origin"]) for item in document["experiment"]["inputs"]] == [
        ("0ns", "session"), ("250000000ns", "session"), ("250000000ns", "session"), ("350000000ns", "session")]
    profiles = document["nodes"]["main_can"]["fault_profiles"]
    loss = next(value for value in profiles.values() if value["drop"])
    assert loss == {"delay_ns": 0, "drop": [{"source": "main_motor_fl.can",
        "destination": "main.fdcan1", "can_id": 0x201}]}
    path = tmp_path / "scene.yaml"
    path.write_text(json.dumps(document))
    assert len(load_scene(path, builtin_registry()).experiment.inputs) == 4


def test_fault_profile_capacity_is_explicit():
    document = four_wheel_document({"main": "/unused.elf"}, "/unused.xml")
    with pytest.raises(ValueError, match="more than16"):
        add_can_faults(document, "main", delays=tuple((index, index) for index in range(17)))
