"""The firmware directory release carries one native backend and a Rust entry."""

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


TOOL = Path(__file__).resolve().parents[1] / "tools/package-qemu.py"
SPEC = importlib.util.spec_from_file_location("native_package", TOOL)
native_package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(native_package)


@pytest.mark.parametrize("suffix, windows_lab, linux_full", [
    ("", False, False), (".exe", False, False), (".exe", True, False), ("", False, True)])
def test_package_keeps_launcher_and_backend_separate(
        monkeypatch, tmp_path, suffix, windows_lab, linux_full):
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
    material = root / "third_party/material/mdui"
    material.mkdir(parents=True)
    material_identity = {"name": "mdui", "version": "2.1.4",
                         "upstream": "https://github.com/zdhxiong/mdui",
                         "commit": "d6d465f1adc6852f40b1adced6dcd103d8c406db"}
    (material / "PROVENANCE.json").write_text(json.dumps(material_identity))
    for name in ("LICENSE", "THIRD-PARTY-NOTICES.txt"):
        (material / name).write_text("retained upstream " + name)
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
    monkeypatch.setitem(sys.modules, "linux_runtime", SimpleNamespace(collect=lambda *args, **kwargs: {}))
    monkeypatch.setattr(native_package, "rust_provenance", lambda launcher, *args: {
        "file": launcher.name, "sha256": native_package.digest(launcher),
        "scope": "firmware terminal and reset-paused GDB listener",
    })

    native_package.package(qemu, sim, output, tmp_path if suffix else None,
                           windows_lab=windows_lab, linux_full={"enabled": True} if linux_full else None)

    assert (output / ("sim" + suffix)).read_bytes() == b"Rust executable"
    assert [(path.name, path.read_bytes()) for path in (output / "bin").iterdir()] == [
        (qemu.name, b"native QEMU bytes")]
    provenance = json.loads((output / "provenance.json").read_text())
    assert provenance["rust"]["sha256"] == hashlib.sha256(b"Rust executable").hexdigest()
    assert provenance["qemu"]["file"] == "bin/" + qemu.name
    assert (output / "licenses/LICENSE-process-wrap.txt").read_text() == notice.read_text()
    if windows_lab:
        assert provenance["rust"]["scope"] == "native Windows interactive/Web Lab and ARM GDB debugging"
    if windows_lab or linux_full:
        assert provenance["web_ui"] == dict(
            material_identity, licenses="licenses/mdui", provenance="licenses/mdui/PROVENANCE.json",
            vendored_sources="third_party/material/mdui")
        for name in ("LICENSE", "THIRD-PARTY-NOTICES.txt", "PROVENANCE.json"):
            assert (output / "licenses/mdui" / name).read_bytes() == (material / name).read_bytes()
    else:
        assert "web_ui" not in provenance
        assert not (output / "licenses/mdui").exists()


def test_package_rejects_mixed_platform_before_staging(tmp_path):
    qemu, sim = tmp_path / "qemu-system-arm.exe", tmp_path / "sim"
    qemu.touch()
    sim.touch()
    output = tmp_path / "release"
    with pytest.raises(ValueError, match="same platform"):
        native_package.package(qemu, sim, output)
    assert not output.exists()
