#!/usr/bin/env python3
"""Canonical build and test gate for the DM-MC02 QEMU project."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Iterable, Sequence
import xml.etree.ElementTree as ET


class Status(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    BLOCKED = "BLOCKED"


OPTIONAL_SMOKES = frozenset(
    {
        "run-mujoco-worker-smoke.sh",
        "run-ros2-worker-smoke.sh",
    }
)
SMOKE_WRAPPER = "run-qemu-smoke-suite.sh"
MESON_TEST_PREFIXES = ("test-dm-", "qtest-arm/dm-mc02-")
MESON_TEST_NAMES = frozenset({"qtest-arm/stm32h723-usb-host-test"})


@dataclass
class CommandResult:
    command: list[str]
    raw_exit: int
    duration_seconds: float
    log: str


@dataclass
class CollectionResult:
    status: Status
    required: bool
    total: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    blocked: int = 0
    raw_exit: int | None = None
    duration_seconds: float = 0.0
    log: str | None = None
    command: list[str] | None = None
    reason: str | None = None
    tests: list[dict[str, Any]] = field(default_factory=list)


def positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def aggregate_exit_code(statuses: Iterable[Status | str]) -> int:
    normalized = {Status(status) for status in statuses}
    if Status.FAIL in normalized:
        return 1
    if Status.BLOCKED in normalized:
        return 78
    return 0


def aggregate_status(statuses: Iterable[Status | str]) -> Status:
    normalized = [Status(status) for status in statuses]
    if Status.FAIL in normalized:
        return Status.FAIL
    if Status.BLOCKED in normalized:
        return Status.BLOCKED
    if normalized and all(status is Status.SKIP for status in normalized):
        return Status.SKIP
    return Status.PASS


def discover_smoke_scripts(tools_dir: Path) -> list[Path]:
    return sorted(
        path
        for path in tools_dir.glob("run-*-smoke.sh")
        if path.is_file() and path.name != SMOKE_WRAPPER
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def file_identity(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file():
        return {"path": str(resolved), "present": False}
    stat = resolved.stat()
    return {
        "path": str(resolved),
        "present": True,
        "size": stat.st_size,
        "sha256": sha256_file(resolved),
    }


def identities(paths: Iterable[Path]) -> dict[str, dict[str, Any]]:
    return {
        str(path.resolve()): file_identity(path)
        for path in sorted(set(paths), key=lambda item: str(item.resolve()))
    }


def _relative(path: Path, base: Path) -> str:
    try:
        return str(path.resolve().relative_to(base.resolve()))
    except ValueError:
        return str(path.resolve())


def _run_logged(
    command: Sequence[str],
    *,
    cwd: Path,
    log_path: Path,
    report_root: Path,
    env: dict[str, str] | None = None,
    append: bool = False,
) -> CommandResult:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    started = time.monotonic()
    raw_exit = 127
    with log_path.open(mode, encoding="utf-8", errors="replace") as log:
        log.write("$ " + " ".join(command) + "\n")
        log.flush()
        try:
            completed = subprocess.run(
                list(command),
                cwd=cwd,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            raw_exit = completed.returncode
        except OSError as exc:
            log.write(f"unable to execute command: {exc}\n")
    return CommandResult(
        command=list(command),
        raw_exit=raw_exit,
        duration_seconds=time.monotonic() - started,
        log=_relative(log_path, report_root),
    )


def _command_version(
    command: Sequence[str], cwd: Path, env: dict[str, str] | None = None
) -> str | None:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = completed.stdout.strip().splitlines()
    return output[0] if output else None


def _load_json_command(command: Sequence[str], cwd: Path) -> Any:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"command exited {completed.returncode}: {' '.join(command)}: {detail}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"invalid JSON from {' '.join(command)}: {exc}") from exc


def _select_meson_tests(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise RuntimeError("Meson test introspection did not return a list")
    selected = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            continue
        name = entry["name"]
        if name.startswith(MESON_TEST_PREFIXES) or name in MESON_TEST_NAMES:
            selected.append(entry)
    selected.sort(key=lambda entry: entry["name"])
    if not selected:
        raise RuntimeError("Meson project test selection is empty")
    return selected


def _property_value(test: dict[str, Any], name: str) -> Any:
    for prop in test.get("properties", []):
        if isinstance(prop, dict) and prop.get("name") == name:
            return prop.get("value")
    return None


def _select_host_tests(document: Any) -> list[dict[str, Any]]:
    if not isinstance(document, dict) or not isinstance(document.get("tests"), list):
        raise RuntimeError("CTest introspection is missing the tests list")
    selected = [
        test
        for test in document["tests"]
        if isinstance(test, dict)
    ]
    if not selected:
        raise RuntimeError("native Host CTest selection is empty")
    return selected


def _host_binary_paths(tests: Iterable[dict[str, Any]], build_dir: Path) -> list[Path]:
    paths = []
    for test in tests:
        command = test.get("command")
        if not isinstance(command, list) or not command or not isinstance(command[0], str):
            raise RuntimeError(f"CTest entry has no command: {test.get('name', '<unnamed>')}")
        path = Path(command[0])
        if not path.is_absolute():
            workdir = _property_value(test, "WORKING_DIRECTORY")
            path = Path(workdir) / path if isinstance(workdir, str) else build_dir / path
        paths.append(path)
    return paths


def _meson_binary_paths(tests: Iterable[dict[str, Any]]) -> list[Path]:
    paths = []
    for test in tests:
        command = test.get("cmd")
        if not isinstance(command, list) or not command or not isinstance(command[0], str):
            raise RuntimeError(
                f"Meson entry has no command: {test.get('name', '<unnamed>')}"
            )
        paths.append(Path(command[0]))
    return paths


def _junit_counts(path: Path) -> tuple[int, int, int, int]:
    root = ET.parse(path).getroot()

    def integer(element: ET.Element, key: str) -> int:
        try:
            return int(element.attrib.get(key, "0"))
        except ValueError as exc:
            raise RuntimeError(f"invalid JUnit {key} count in {path}") from exc

    if "tests" in root.attrib:
        total = integer(root, "tests")
        failures = integer(root, "failures") + integer(root, "errors")
        skipped = integer(root, "skipped") + integer(root, "disabled")
    else:
        suites = root.findall("./testsuite")
        total = sum(integer(suite, "tests") for suite in suites)
        failures = sum(
            integer(suite, "failures") + integer(suite, "errors")
            for suite in suites
        )
        skipped = sum(
            integer(suite, "skipped") + integer(suite, "disabled")
            for suite in suites
        )
    passed = total - failures - skipped
    if min(total, failures, skipped, passed) < 0:
        raise RuntimeError(f"inconsistent JUnit counts in {path}")
    return total, passed, failures, skipped


def _result_from_junit(
    command_result: CommandResult,
    junit: Path,
    *,
    required: bool,
) -> CollectionResult:
    try:
        total, passed, failed, skipped = _junit_counts(junit)
    except (OSError, ET.ParseError, RuntimeError) as exc:
        return CollectionResult(
            status=Status.FAIL,
            required=required,
            failed=1,
            raw_exit=command_result.raw_exit,
            duration_seconds=command_result.duration_seconds,
            log=command_result.log,
            command=command_result.command,
            reason=f"result report is unavailable or invalid: {exc}",
        )
    if command_result.raw_exit != 0 or failed:
        status = Status.FAIL
    elif skipped and required:
        status = Status.BLOCKED
    elif skipped == total and total:
        status = Status.SKIP
    else:
        status = Status.PASS
    return CollectionResult(
        status=status,
        required=required,
        total=total,
        passed=passed,
        failed=failed,
        skipped=skipped if not required else 0,
        blocked=skipped if required else 0,
        raw_exit=command_result.raw_exit,
        duration_seconds=command_result.duration_seconds,
        log=command_result.log,
        command=command_result.command,
    )


def _blocked_collection(reason: str, *, total: int = 0) -> CollectionResult:
    return CollectionResult(
        status=Status.BLOCKED,
        required=True,
        total=total,
        blocked=total or 1,
        reason=reason,
    )


def _run_meson_tests(
    root: Path,
    selected: list[dict[str, Any]],
    jobs: int,
    report: Path,
) -> CollectionResult:
    names = [entry["name"] for entry in selected]
    logbase = f"dm-mc02-gate-{os.getpid()}-{time.time_ns()}"
    command = [
        str(root / "tools" / "meson"),
        "test",
        "-C",
        str(root / "build" / "qemu"),
        "--no-rebuild",
        "--print-errorlogs",
        "--num-processes",
        str(jobs),
        "--logbase",
        logbase,
        *names,
    ]
    result = _run_logged(
        command,
        cwd=root,
        log_path=report / "meson.log",
        report_root=report,
    )
    meson_logs = root / "build" / "qemu" / "meson-logs"
    json_source = meson_logs / f"{logbase}.json"
    junit_source = meson_logs / f"{logbase}.junit.xml"
    json_target = report / "meson-results.json"
    junit_target = report / "meson-junit.xml"
    if json_source.is_file():
        shutil.copy2(json_source, json_target)
        json_source.unlink()
    if junit_source.is_file():
        shutil.copy2(junit_source, junit_target)
        junit_source.unlink()
    text_source = meson_logs / f"{logbase}.txt"
    if text_source.is_file():
        text_source.unlink()
    try:
        rows = [
            json.loads(line)
            for line in json_target.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(rows) != len(names):
            raise RuntimeError(
                f"Meson reported {len(rows)} tests for a {len(names)}-test selection"
            )
        failed = sum(bool(row.get("is_fail")) for row in rows)
        skipped = sum(row.get("result") == "SKIP" for row in rows)
        passed = len(rows) - failed - skipped
    except (OSError, json.JSONDecodeError, RuntimeError) as exc:
        return CollectionResult(
            status=Status.FAIL,
            required=True,
            total=len(names),
            failed=1,
            raw_exit=result.raw_exit,
            duration_seconds=result.duration_seconds,
            log=result.log,
            command=result.command,
            reason=f"Meson result report is unavailable or invalid: {exc}",
        )
    if result.raw_exit != 0 or failed:
        status = Status.FAIL
    elif skipped:
        status = Status.BLOCKED
    else:
        status = Status.PASS
    return CollectionResult(
        status=status,
        required=True,
        total=len(rows),
        passed=passed,
        failed=failed,
        blocked=skipped,
        raw_exit=result.raw_exit,
        duration_seconds=result.duration_seconds,
        log=result.log,
        command=result.command,
        tests=[{"name": name, "required": True} for name in names],
    )


def _run_host_tests(root: Path, jobs: int, report: Path) -> CollectionResult:
    junit = report / "host-junit.xml"
    command = [
        "ctest",
        "--test-dir",
        str(root / "build" / "host"),
        "--no-tests=error",
        "--output-on-failure",
        "--output-junit",
        str(junit),
        "--parallel",
        str(jobs),
    ]
    result = _run_logged(
        command,
        cwd=root,
        log_path=report / "host-ctest.log",
        report_root=report,
    )
    return _result_from_junit(result, junit, required=True)


def _run_pytest(root: Path, report: Path) -> CollectionResult:
    junit = report / "pytest-junit.xml"
    python = root / ".venv" / "bin" / "python"
    command = [
        str(python),
        "-m",
        "pytest",
        str(root / "tests"),
        "--junitxml",
        str(junit),
        "-q",
    ]
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = _run_logged(
        command,
        cwd=root,
        log_path=report / "pytest.log",
        report_root=report,
        env=env,
    )
    return _result_from_junit(result, junit, required=True)


def _run_smokes(root: Path, report: Path) -> CollectionResult:
    scripts = discover_smoke_scripts(root / "tools")
    if not scripts:
        return _blocked_collection("shell smoke discovery is empty")
    started = time.monotonic()
    results: list[dict[str, Any]] = []
    smoke_dir = report / "smoke"
    for script in scripts:
        optional = script.name in OPTIONAL_SMOKES
        command_result = _run_logged(
            ["bash", str(script)],
            cwd=root,
            log_path=smoke_dir / f"{script.stem}.log",
            report_root=report,
        )
        if command_result.raw_exit == 0:
            status = Status.PASS
        elif command_result.raw_exit == 77 and optional:
            status = Status.SKIP
        elif command_result.raw_exit in (77, 78):
            status = Status.BLOCKED
        else:
            status = Status.FAIL
        log_path = report / command_result.log
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            reason = lines[-1] if lines else None
        except OSError:
            reason = None
        results.append(
            {
                "name": script.name,
                "required": not optional,
                "status": status.value,
                "raw_exit": command_result.raw_exit,
                "duration_seconds": command_result.duration_seconds,
                "log": command_result.log,
                "reason": reason if status in (Status.SKIP, Status.BLOCKED) else None,
            }
        )
        print(f"[{status.value:7}] {script.name}", flush=True)
    statuses = [Status(result["status"]) for result in results]
    return CollectionResult(
        status=aggregate_status(statuses),
        required=True,
        total=len(results),
        passed=statuses.count(Status.PASS),
        failed=statuses.count(Status.FAIL),
        skipped=statuses.count(Status.SKIP),
        blocked=statuses.count(Status.BLOCKED),
        raw_exit=aggregate_exit_code(statuses),
        duration_seconds=time.monotonic() - started,
        log=_relative(smoke_dir, report),
        tests=results,
    )


def _prepare_inventory(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    meson_document = _load_json_command(
        [
            str(root / "tools" / "meson"),
            "introspect",
            "--tests",
            str(root / "build" / "qemu"),
        ],
        root,
    )
    ctest_document = _load_json_command(
        [
            "ctest",
            "--show-only=json-v1",
            "--test-dir",
            str(root / "build" / "host"),
        ],
        root,
    )
    return _select_meson_tests(meson_document), _select_host_tests(ctest_document)


def _build(
    root: Path,
    jobs: int,
    report: Path,
) -> tuple[CollectionResult, list[dict[str, Any]], list[dict[str, Any]]]:
    log = report / "build.log"
    commands = [
        ["bash", str(root / "tools" / "build-qemu.sh")],
        ["cmake", "-S", str(root), "-B", str(root / "build" / "host")],
        [
            "cmake",
            "--build",
            str(root / "build" / "host"),
            "--parallel",
            str(jobs),
        ],
    ]
    elapsed = 0.0
    for index, command in enumerate(commands):
        result = _run_logged(
            command,
            cwd=root,
            log_path=log,
            report_root=report,
            append=index != 0,
        )
        elapsed += result.duration_seconds
        if result.raw_exit != 0:
            status = (
                Status.BLOCKED
                if result.raw_exit in (77, 78, 127)
                else Status.FAIL
            )
            return (
                CollectionResult(
                    status=status,
                    required=True,
                    failed=1 if status is Status.FAIL else 0,
                    blocked=1 if status is Status.BLOCKED else 0,
                    raw_exit=result.raw_exit,
                    duration_seconds=elapsed,
                    log=result.log,
                    command=result.command,
                    reason="build command failed",
                ),
                [],
                [],
            )
    try:
        meson_tests, host_tests = _prepare_inventory(root)
    except (OSError, RuntimeError) as exc:
        return (
            _blocked_collection(f"cannot discover tests after build: {exc}"),
            [],
            [],
        )
    targets = sorted({Path(entry["cmd"][0]).name for entry in meson_tests})
    result = _run_logged(
        [
            str(root / "tools" / "meson"),
            "compile",
            "-C",
            str(root / "build" / "qemu"),
            "--jobs",
            str(jobs),
            *targets,
        ],
        cwd=root,
        log_path=log,
        report_root=report,
        append=True,
    )
    elapsed += result.duration_seconds
    if result.raw_exit != 0:
        status = Status.BLOCKED if result.raw_exit in (77, 78, 127) else Status.FAIL
        return (
            CollectionResult(
                status=status,
                required=True,
                failed=1 if status is Status.FAIL else 0,
                blocked=1 if status is Status.BLOCKED else 0,
                raw_exit=result.raw_exit,
                duration_seconds=elapsed,
                log=result.log,
                command=result.command,
                reason="Meson project-test build failed",
            ),
            meson_tests,
            host_tests,
        )
    return (
        CollectionResult(
            status=Status.PASS,
            required=True,
            total=len(targets) + len(host_tests) + 1,
            passed=len(targets) + len(host_tests) + 1,
            raw_exit=0,
            duration_seconds=elapsed,
            log=_relative(log, report),
        ),
        meson_tests,
        host_tests,
    )


def _runner_versions(root: Path, smoke_only: bool) -> dict[str, Any]:
    versions: dict[str, Any] = {
        "gate": str((root / "tools" / "dm_mc02_test_gate.py").resolve()),
        "python": {
            "path": sys.executable,
            "version": sys.version.splitlines()[0],
        },
        "bash": {
            "path": shutil.which("bash"),
            "version": _command_version(["bash", "--version"], root),
        },
    }
    if not smoke_only:
        pytest_env = os.environ.copy()
        pytest_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        pytest_env["PYTHONDONTWRITEBYTECODE"] = "1"
        versions.update(
            {
                "meson": {
                    "path": str((root / "tools" / "meson").resolve()),
                    "version": _command_version(
                        [str(root / "tools" / "meson"), "--version"], root
                    ),
                },
                "ninja": {
                    "path": shutil.which("ninja"),
                    "version": _command_version(["ninja", "--version"], root),
                },
                "cmake": {
                    "path": shutil.which("cmake"),
                    "version": _command_version(["cmake", "--version"], root),
                },
                "ctest": {
                    "path": shutil.which("ctest"),
                    "version": _command_version(["ctest", "--version"], root),
                },
                "pytest": {
                    "path": str((root / ".venv" / "bin" / "python").resolve()),
                    "version": _command_version(
                        [str(root / ".venv" / "bin" / "python"), "-m", "pytest", "--version"],
                        root,
                        pytest_env,
                    ),
                    "plugin_autoload": False,
                },
            }
        )
    return versions


def _serialize_collection(result: CollectionResult) -> dict[str, Any]:
    value = asdict(result)
    value["status"] = result.status.value
    return value


def _create_report_dir(base: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    report = base / f"{stamp}-{os.getpid()}"
    report.mkdir(parents=True, exist_ok=False)
    return report


def _write_summary(report: Path, summary: dict[str, Any]) -> None:
    target = report / "summary.json"
    temporary = report / ".summary.json.tmp"
    temporary.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build and run the canonical DM-MC02 QEMU test gate."
    )
    parser.add_argument(
        "--no-build",
        action="store_true",
        help="consume existing build products without configuring or compiling",
    )
    parser.add_argument(
        "--smoke-only",
        action="store_true",
        help="run only the stable shell smoke inventory",
    )
    parser.add_argument(
        "--jobs",
        type=positive_int,
        default=max(1, os.cpu_count() or 1),
        help="parallel jobs for build, Meson, and CTest",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        help="base directory for the timestamped report",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    report_base = (
        args.report_dir.resolve()
        if args.report_dir is not None
        else root / "build" / "test-results" / "qemu-gate"
    )
    report = _create_report_dir(report_base)
    started_utc = datetime.now(timezone.utc).isoformat()
    print(f"DM-MC02 test gate report: {report}", flush=True)

    phases: dict[str, CollectionResult] = {}
    collections: dict[str, CollectionResult] = {}
    qemu_binary = root / "build" / "qemu" / "qemu-system-arm"
    meson_tests: list[dict[str, Any]] = []
    host_tests: list[dict[str, Any]] = []

    required_commands = ["bash", "python3", "arm-none-eabi-gcc"]
    required_paths = [qemu_binary] if args.no_build or args.smoke_only else []
    if not args.smoke_only:
        required_commands.extend(["cmake", "ctest"])
        required_paths.extend(
            [
                root / ".venv" / "bin" / "python",
                root / "tools" / "meson",
            ]
        )
    if not args.no_build and not args.smoke_only:
        required_commands.extend(["cc", "ninja", "rg"])
        required_paths.extend(
            [
                root / "qemu" / "upstream" / "configure",
                root / "tools" / "build-qemu.sh",
            ]
        )
    missing_commands = [name for name in required_commands if shutil.which(name) is None]
    missing_paths = [str(path) for path in required_paths if not path.is_file()]
    if missing_commands or missing_paths:
        missing = [*(f"command:{name}" for name in missing_commands), *missing_paths]
        phases["prerequisites"] = _blocked_collection(
            "missing required inputs: " + ", ".join(missing)
        )

    if phases:
        pass
    elif args.smoke_only:
        if not qemu_binary.is_file():
            phases["inventory"] = _blocked_collection(
                f"QEMU binary is missing: {qemu_binary}"
            )
        else:
            phases["inventory"] = CollectionResult(
                status=Status.PASS, required=True, total=1, passed=1, raw_exit=0
            )
    elif args.no_build:
        try:
            meson_tests, host_tests = _prepare_inventory(root)
            phases["inventory"] = CollectionResult(
                status=Status.PASS,
                required=True,
                total=len(meson_tests) + len(host_tests),
                passed=len(meson_tests) + len(host_tests),
                raw_exit=0,
            )
        except (OSError, RuntimeError) as exc:
            phases["inventory"] = _blocked_collection(
                f"cannot discover prebuilt tests: {exc}"
            )
    else:
        build_result, meson_tests, host_tests = _build(root, args.jobs, report)
        phases["build"] = build_result

    inventory_ok = all(result.status is Status.PASS for result in phases.values())
    host_paths: list[Path] = []
    meson_paths: list[Path] = []
    if inventory_ok and not args.smoke_only:
        try:
            host_paths = _host_binary_paths(host_tests, root / "build" / "host")
            meson_paths = _meson_binary_paths(meson_tests)
        except RuntimeError as exc:
            phases["inventory"] = _blocked_collection(str(exc))
            inventory_ok = False

    qemu_before = file_identity(qemu_binary)
    host_before = identities(host_paths)
    missing_host = [path for path, identity in host_before.items() if not identity["present"]]
    missing_meson = [str(path) for path in meson_paths if not path.is_file()]
    if inventory_ok and not qemu_before["present"]:
        phases["identity"] = _blocked_collection(
            f"QEMU binary is missing: {qemu_binary}"
        )
        inventory_ok = False
    elif inventory_ok and missing_host:
        phases["identity"] = _blocked_collection(
            "Host test binaries are missing: " + ", ".join(missing_host)
        )
        inventory_ok = False
    elif inventory_ok and missing_meson:
        phases["identity"] = _blocked_collection(
            "Meson test binaries are missing: " + ", ".join(missing_meson)
        )
        inventory_ok = False

    if inventory_ok:
        if args.smoke_only:
            collections["smoke"] = _run_smokes(root, report)
        else:
            collections["meson"] = _run_meson_tests(
                root, meson_tests, args.jobs, report
            )
            collections["host_ctest"] = _run_host_tests(root, args.jobs, report)
            collections["pytest"] = _run_pytest(root, report)
            collections["smoke"] = _run_smokes(root, report)
    else:
        names = ["smoke"] if args.smoke_only else [
            "meson",
            "host_ctest",
            "pytest",
            "smoke",
        ]
        for name in names:
            collections[name] = _blocked_collection(
                "test collection was not started because inventory or identity failed"
            )

    qemu_after = file_identity(qemu_binary)
    host_after = identities(host_paths)
    if qemu_before != qemu_after or host_before != host_after:
        phases["identity"] = CollectionResult(
            status=Status.FAIL,
            required=True,
            failed=1,
            reason="QEMU or Host binary SHA-256 identity changed during testing",
        )
    elif "identity" not in phases and inventory_ok:
        phases["identity"] = CollectionResult(
            status=Status.PASS,
            required=True,
            total=1 + len(host_before),
            passed=1 + len(host_before),
            raw_exit=0,
        )
    elif "identity" not in phases:
        phases["identity"] = _blocked_collection(
            "binary identities were not established because prerequisites failed"
        )

    statuses = [result.status for result in phases.values()]
    statuses.extend(result.status for result in collections.values())
    exit_code = aggregate_exit_code(statuses)
    overall = Status.FAIL if exit_code == 1 else (
        Status.BLOCKED if exit_code == 78 else Status.PASS
    )
    summary = {
        "schema": 1,
        "started_utc": started_utc,
        "finished_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "mode": {
            "build": not args.no_build and not args.smoke_only,
            "smoke_only": args.smoke_only,
            "jobs": args.jobs,
        },
        "runners": _runner_versions(root, args.smoke_only),
        "phases": {
            name: _serialize_collection(result) for name, result in phases.items()
        },
        "collections": {
            name: _serialize_collection(result)
            for name, result in collections.items()
        },
        "identities": {
            "before": {"qemu": qemu_before, "host": host_before},
            "after": {"qemu": qemu_after, "host": host_after},
        },
        "overall": {"status": overall.value, "exit_code": exit_code},
    }
    _write_summary(report, summary)

    for name, result in collections.items():
        print(
            f"{name}: {result.status.value} "
            f"({result.passed} passed, {result.failed} failed, "
            f"{result.skipped} skipped, {result.blocked} blocked, "
            f"{result.total} total)",
            flush=True,
        )
    print(f"overall: {overall.value} (exit {exit_code})", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
