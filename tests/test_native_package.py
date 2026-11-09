"""The firmware directory release carries one native backend and a Rust entry."""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


TOOL = Path(__file__).resolve().parents[1] / "tools/package-qemu.py"
SPEC = importlib.util.spec_from_file_location("native_package", TOOL)
native_package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native_package)


@pytest.mark.parametrize("suffix, windows_lab", [("", False), (".exe", False), (".exe", True)])
def test_package_keeps_launcher_and_backend_separate(monkeypatch, tmp_path, suffix, windows_lab):
    root = tmp_path / "checkout"
    native = root / "qemu/upstream"
    native.mkdir(parents=True)
    (root / "docs").mkdir()
    for filename in ("README.md", "docs/USAGE.md", "qemu.lock", "LICENSE"):
        (root / filename).write_text(filename)
    for filename in ("COPYING", "COPYING.LIB"):
        (native / filename).write_text(filename)
    notice = root / "crates/sim-host/src/native_process/LICENSE-process-wrap.txt"
    notice.parent.mkdir(parents=True)
    notice.write_text("fixed process-wrap MIT notice")
    qemu = tmp_path / ("qemu-system-arm" + suffix)
    qemu.write_bytes(b"native QEMU bytes")
    sim = tmp_path / ("built-sim" + suffix)
    sim.write_bytes(b"Rust executable")
    output = tmp_path / "release"
    monkeypatch.setattr(native_package, "ROOT", root)
    monkeypatch.setattr(native_package, "NATIVE", native)
    monkeypatch.setattr(native_package, "source_identities", lambda development=False: ("root", "native"))
    monkeypatch.setattr(native_package, "archive_source", lambda *args: {"commit": args[1]})
    monkeypatch.setattr(native_package, "archive_subproject", lambda *args: {"name": args[0]})
    monkeypatch.setattr(native_package, "windows_dependencies", lambda *args: [])
    monkeypatch.setattr(native_package, "rust_provenance", lambda launcher, *args: {
        "file": launcher.name, "sha256": native_package.digest(launcher),
        "scope": "firmware terminal and reset-paused GDB listener",
    })

    native_package.package(qemu, sim, output, tmp_path if suffix else None, windows_lab=windows_lab)

    assert (output / ("sim" + suffix)).read_bytes() == b"Rust executable"
    assert [(path.name, path.read_bytes()) for path in (output / "bin").iterdir()] == [
        (qemu.name, b"native QEMU bytes")]
    provenance = json.loads((output / "provenance.json").read_text())
    assert provenance["rust"]["sha256"] == hashlib.sha256(b"Rust executable").hexdigest()
    assert provenance["qemu"]["file"] == "bin/" + qemu.name
    assert (output / "licenses/LICENSE-process-wrap.txt").read_text() == notice.read_text()
    if windows_lab:
        assert provenance["rust"]["scope"] == "native Windows interactive/Web Lab and ARM GDB debugging"


def test_package_rejects_mixed_platform_before_staging(tmp_path):
    qemu, sim = tmp_path / "qemu-system-arm.exe", tmp_path / "sim"
    qemu.touch()
    sim.touch()
    output = tmp_path / "release"
    with pytest.raises(ValueError, match="same platform"):
        native_package.package(qemu, sim, output)
    assert not output.exists()
