"""Common bounded YAML admission retained after removing automatic Lab schema1."""

import socket
import subprocess

import pytest

from sim.composition import builtin_registry
from sim.config.scene import load_scene
from sim.config.yaml import ManifestError, parse_time


HEADER = "schema: sim/v1\nnodes:\n  wire: {kind: bus.can}\n"


@pytest.mark.parametrize("suffix", [
    "runtime: {duration: true}\n",
    "runtime: {duration: 0s}\n",
    "runtime: {duration: 3601s}\n",
    "runtime: {duration: 1.5ms}\n",
    "runtime: {duration: 1e3ms}\n",
    "runtime: {duration: -1ms}\n",
    "runtime: {duration: 1h}\n",
    "runtime: {duration: 999999999999999999999999ns}\n",
    "runtime: {duration: 1ms, duration: 2ms}\n",
    "links: &wire []\n",
    "unknown: &recursive [*recursive]\n",
    "unknown: !!python/object/apply:os.system [echo unsafe]\n",
    "unknown: !!bool invalid\n",
    "unknown: " + "[" * 17 + "0" + "]" * 17 + "\n",
    "---\nschema: sim/v1\n",
    "true: value\n",
    "unknown: [" + ",".join("0" for _ in range(40001)) + "]\n",
])
def test_structural_and_time_admission_before_resource_acquisition(tmp_path, monkeypatch, suffix):
    path = tmp_path / "scene.yaml"
    path.write_text(HEADER + suffix)
    def forbidden(*args, **kwargs):
        pytest.fail("pure admission acquired resources")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    with pytest.raises(ManifestError):
        load_scene(path, builtin_registry())


def test_file_bytes_utf8_and_integer_time_bounds(tmp_path):
    path = tmp_path / "scene.yaml"
    document = HEADER.encode()
    path.write_bytes(document + b"#" + b"x" * ((1 << 20) - len(document) - 1))
    assert load_scene(path, builtin_registry()).duration_ns == 200_000_000
    path.write_bytes(path.read_bytes() + b"x")
    with pytest.raises(ManifestError, match="1 MiB"):
        load_scene(path, builtin_registry())
    path.write_bytes(b"\xff")
    with pytest.raises(ManifestError):
        load_scene(path, builtin_registry())
    for bad in (tmp_path, tmp_path / "missing", True):
        with pytest.raises(ManifestError):
            load_scene(bad, builtin_registry())
    assert [parse_time(value, "time") for value in ("1ns", "1us", "1ms", "1s", "3600s")] == [
        1, 1000, 1_000_000, 1_000_000_000, 3_600_000_000_000]
