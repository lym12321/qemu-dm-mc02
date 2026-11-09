"""Independent provenance for the compiled RefereeTest consumer.

Wire, suffix and firmware report behavior is covered by Rust referee_native
and referee_endpoint; this test owns only test-runner checkout observation.
"""
import importlib.util
from pathlib import Path
import subprocess

PATH = Path(__file__).resolve().parents[1] / "tools/dm_mc02_referee_test.py"
SPEC = importlib.util.spec_from_file_location("referee_test_tool", PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_checkout_observation_records_actual_unborn_repository(tmp_path, monkeypatch):
    project, firmware = tmp_path / "project", tmp_path / "firmware"
    for repo in (project, firmware):
        repo.mkdir()
        subprocess.run(["git", "init", "--quiet", str(repo)], check=True,
                       capture_output=True, timeout=2)
        (repo / "input.cc").write_text("fixed uncommitted source\n")
    (project / "qemu/upstream").mkdir(parents=True)
    for name in ("robomaster", "msg", "utils"):
        (firmware / "components" / name).mkdir(parents=True)
    monkeypatch.setattr(module, "ROOT", project)
    receipt = module.checkout_snapshot(firmware)
    assert len(receipt) == 6
    for row in receipt.values():
        assert row["head"] is None
        assert row["head_unavailable"] == "repository has no commit; input files are hashed"
        assert row["status"].startswith("?? ") and row["status"].endswith("input.cc")
