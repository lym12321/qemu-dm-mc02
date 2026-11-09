"""The firmware fixture is compiled Rust; Python only checks test acquisition."""
import importlib.util
import json
from pathlib import Path
import sys

PATH = Path(__file__).resolve().parents[1] / "tools/dm_mc02_dji_test.py"
SPEC = importlib.util.spec_from_file_location("dji_black_box", PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_missing_consumer_replaces_prior_pass_without_starting_qemu(tmp_path, monkeypatch):
    elf = tmp_path / "firmware.elf"
    elf.write_bytes(b"fixture")
    report = tmp_path / "report.json"
    report.write_text('{"result":"passed"}\n')
    monkeypatch.setattr(sys, "argv", ["dji", str(elf), str(report), str(tmp_path),
        "--qemu", "/bin/true", "--consumer", str(tmp_path / "missing-consumer")])
    monkeypatch.setattr(module, "running_qemu", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("unadmitted process acquisition")))
    assert module.main() == 1
    actual = json.loads(report.read_text())
    assert actual["result"] == "failed" and "missing-consumer" in actual["error"]
    assert actual["cases"] == [] and not actual["native_cleanup_complete"]
