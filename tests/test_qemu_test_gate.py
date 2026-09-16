"""Contract tests for the consolidated QEMU test gate."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = PROJECT_ROOT / "tools" / "dm_mc02_test_gate.py"
SPEC = importlib.util.spec_from_file_location("dm_mc02_test_gate_test", GATE_SCRIPT)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    tools = project / "tools"
    tools.mkdir(parents=True)
    shutil.copy2(GATE_SCRIPT, tools / GATE_SCRIPT.name)
    _write_executable(
        project / "build" / "qemu" / "qemu-system-arm",
        "exit 0\n",
    )
    return project


def _run_gate(project: Path, report_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(project / "tools" / GATE_SCRIPT.name),
            "--no-build",
            "--smoke-only",
            "--jobs",
            "1",
            "--report-dir",
            str(report_dir),
        ],
        cwd=project,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True,
        text=True,
        check=False,
    )


def _read_summary(report_dir: Path) -> dict[str, Any]:
    summaries = list(report_dir.rglob("summary.json"))
    assert len(summaries) == 1, summaries
    return json.loads(summaries[0].read_text(encoding="utf-8"))


def test_smoke_only_sorts_scripts_and_records_optional_skip(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    order = project / "order.txt"
    _write_executable(
        project / "tools" / "run-z-last-smoke.sh",
        f"printf '%s\\n' z >> {order!s}\n",
    )
    _write_executable(
        project / "tools" / "run-a-first-smoke.sh",
        f"printf '%s\\n' a >> {order!s}\n",
    )
    _write_executable(
        project / "tools" / "run-ros2-worker-smoke.sh",
        f"printf '%s\\n' ros2 >> {order!s}\nexit 77\n",
    )
    report_dir = tmp_path / "report"

    completed = _run_gate(project, report_dir)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert order.read_text(encoding="utf-8").splitlines() == ["a", "ros2", "z"]
    summary = _read_summary(report_dir)
    assert summary["overall"] == {"status": "PASS", "exit_code": 0}
    assert summary["collections"]["smoke"]["status"] == "PASS"
    assert summary["collections"]["smoke"]["total"] == 3
    assert summary["collections"]["smoke"]["passed"] == 2
    assert summary["collections"]["smoke"]["skipped"] == 1
    assert {
        test["name"]: (test["required"], test["status"], test["raw_exit"])
        for test in summary["collections"]["smoke"]["tests"]
    } == {
        "run-a-first-smoke.sh": (True, "PASS", 0),
        "run-ros2-worker-smoke.sh": (False, "SKIP", 77),
        "run-z-last-smoke.sh": (True, "PASS", 0),
    }


def test_required_exit_77_is_blocked(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_executable(
        project / "tools" / "run-required-smoke.sh",
        "exit 77\n",
    )
    report_dir = tmp_path / "report"

    completed = _run_gate(project, report_dir)

    assert completed.returncode == 78, completed.stdout + completed.stderr
    summary = _read_summary(report_dir)
    assert summary["overall"] == {"status": "BLOCKED", "exit_code": 78}
    smoke = summary["collections"]["smoke"]
    assert (smoke["status"], smoke["blocked"], smoke["total"]) == (
        "BLOCKED",
        1,
        1,
    )
    assert smoke["tests"][0]["name"] == "run-required-smoke.sh"
    assert smoke["tests"][0]["required"] is True


def test_failure_takes_precedence_over_blocked(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    _write_executable(
        project / "tools" / "run-a-blocked-smoke.sh",
        "exit 77\n",
    )
    _write_executable(
        project / "tools" / "run-b-failed-smoke.sh",
        "exit 9\n",
    )
    report_dir = tmp_path / "report"

    completed = _run_gate(project, report_dir)

    assert completed.returncode == 1, completed.stdout + completed.stderr
    summary = _read_summary(report_dir)
    assert summary["overall"] == {"status": "FAIL", "exit_code": 1}
    smoke = summary["collections"]["smoke"]
    assert smoke["status"] == "FAIL"
    assert (smoke["failed"], smoke["blocked"], smoke["total"]) == (1, 1, 2)


def test_binary_identity_drift_fails_the_gate(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    qemu_binary = project / "build" / "qemu" / "qemu-system-arm"
    _write_executable(
        project / "tools" / "run-mutating-smoke.sh",
        f"printf '%s\\n' changed >> {qemu_binary!s}\n",
    )
    report_dir = tmp_path / "report"

    completed = _run_gate(project, report_dir)

    assert completed.returncode == 1, completed.stdout + completed.stderr
    summary = _read_summary(report_dir)
    assert summary["overall"] == {"status": "FAIL", "exit_code": 1}
    assert summary["phases"]["identity"]["status"] == "FAIL"
    assert "SHA-256 identity changed" in summary["phases"]["identity"]["reason"]
    assert (
        summary["identities"]["before"]["qemu"]["sha256"]
        != summary["identities"]["after"]["qemu"]["sha256"]
    )


@pytest.mark.parametrize("arguments", [["--jobs", "0"], ["--jobs", "not-a-number"]])
def test_invalid_cli_returns_usage_error(arguments: list[str]) -> None:
    completed = subprocess.run(
        [sys.executable, str(GATE_SCRIPT), *arguments],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "usage:" in completed.stderr.lower()


def test_repository_smoke_inventory_has_one_stable_non_wrapper_set() -> None:
    smoke_scripts = [
        path.name for path in gate.discover_smoke_scripts(PROJECT_ROOT / "tools")
    ]

    assert len(smoke_scripts) == 93
    assert smoke_scripts == sorted(set(smoke_scripts))
    assert all("renode" not in name.lower() for name in smoke_scripts)


def test_public_status_and_sha_helpers_are_deterministic(tmp_path: Path) -> None:
    payload = tmp_path / "payload.bin"
    payload.write_bytes(b"DM-MC02 test gate\n")

    assert gate.sha256_file(payload) == (
        "1786b17cbd4c2d0dee983b05a67de7e9636e754d641808eddde082c3bc6112d6"
    )
    assert gate.aggregate_exit_code([]) == 0
    assert gate.aggregate_exit_code([gate.Status.PASS, gate.Status.SKIP]) == 0
    assert gate.aggregate_exit_code([gate.Status.BLOCKED]) == 78
    assert gate.aggregate_exit_code([gate.Status.BLOCKED, gate.Status.FAIL]) == 1
    assert gate.aggregate_status([gate.Status.SKIP]) is gate.Status.SKIP
    assert gate.aggregate_status([gate.Status.PASS, gate.Status.SKIP]) is gate.Status.PASS


def test_pytest_collection_disables_host_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = tmp_path / "report"
    report.mkdir()
    captured: dict[str, Any] = {}

    def fake_run_logged(
        command: list[str],
        *,
        cwd: Path,
        log_path: Path,
        report_root: Path,
        env: dict[str, str] | None = None,
        append: bool = False,
    ) -> Any:
        del cwd, log_path, report_root, append
        captured["command"] = command
        captured["env"] = env
        (report / "pytest-junit.xml").write_text(
            '<testsuite tests="1" failures="0" errors="0" skipped="0"/>\n',
            encoding="utf-8",
        )
        return gate.CommandResult(command, 0, 0.01, "pytest.log")

    monkeypatch.setattr(gate, "_run_logged", fake_run_logged)

    result = gate._run_pytest(tmp_path, report)

    assert result.status is gate.Status.PASS
    assert result.total == result.passed == 1
    assert captured["command"][1:3] == ["-m", "pytest"]
    assert captured["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert captured["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
