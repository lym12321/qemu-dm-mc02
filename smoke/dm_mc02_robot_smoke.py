"""Test-only CLI/QEMU/chassis-worker/upper-process lifecycle consumer.

Reuses the Lab ELF: this gate verifies composition, not four-wheel control.
The dedicated ChassisTest firmware supplies the independent control evidence.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import yaml

ROOT, ELF, RUN = map(Path, sys.argv[1:])
sys.path.insert(0, str(ROOT))
from tools.dm_mc02_test_harness import stop


def identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "starttime": int(fields[19])}
    except (FileNotFoundError, ProcessLookupError):
        return None


upper = RUN / "upper.py"
observations = RUN / "upper.json"
external_qmp = RUN / "robot-qmp.sock"
upper.write_text('''import json,os,pathlib,sys,time
sys.path.insert(0,sys.argv[1])
from tools.dm_mc02_qmp import QmpSession
from sim.boards import QemuBoard
qmp=QmpSession(sys.argv[2],timeout=1,connect_timeout=1,close_timeout=1)
board=QemuBoard(qmp)
identities={}
try:
    while True:
        for pid in pathlib.Path(f"/proc/{os.getppid()}/task/{os.getppid()}/children").read_text().split():
            try:
                stat=pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")",1)[1].split()
                identities[pid]={"pid":int(pid),"starttime":int(stat[19])}
            except FileNotFoundError:
                pass
        state=board.snapshot()
        diagnostics=qmp.command("qom-get",{"path":"/machine","property":"cosim-diagnostics"})
        record={"state":state,"cosim":diagnostics,"identities":list(identities.values())}
        pathlib.Path(sys.argv[3]).write_text(json.dumps(record))
        print(json.dumps({"upper_time_ns":state["timestamp_ns"],"cosim":diagnostics}),flush=True)
        time.sleep(.05)
finally:
    qmp.close()
''')
document = {
    "schema": 1, "robot": {"name": "native-composition"},
    "boards": {"main": {"type": "qemu", "board": "dm-mc02", "firmware": str(ELF)}},
    "buses": {"drive": {"type": "can"}},
    "links": [{"from": "main.fdcan1", "to": "drive"}],
    "devices": {corner: {"type": "dji-m3508", "bus": "drive", "id": index,
                         "joint": "wheel_" + corner}
                for index, corner in enumerate(("fl", "fr", "rl", "rr"), 1)},
    "physics": {"backend": "mujoco", "model": str(ROOT / "models/four_wheel.xml")},
    "ready": {"endpoint": "main.uart1", "text": "LAB_READY\n"},
    "upper_computers": {"observer": {"command": [sys.executable, str(upper), str(ROOT),
                                                str(external_qmp), str(observations)]}},
}


def run_case(name, manifest, options):
    path, report = RUN / (name + ".yaml"), RUN / (name + ".json")
    path.write_text(yaml.safe_dump(manifest))
    process = None
    with (RUN / (name + ".log")).open("wb") as log:
        try:
            process = subprocess.Popen(
                [sys.executable, str(ROOT / "tools/sim"), "robot", "run", str(path),
                 "--qemu", os.environ.get("QEMU_SYSTEM_ARM", str(ROOT / "build/qemu/qemu-system-arm")),
                 "--report", str(report), *options],
                stdout=log, stderr=log, start_new_session=True)
            original = identity(process.pid)
            process.wait(timeout=20)
            assert identity(process.pid) != original, "CLI identity remains after reap"
        finally:
            stop(process)
    output = (RUN / (name + ".log")).read_bytes()[-12000:].decode(errors="replace")
    assert report.exists(), output
    record = json.loads(report.read_text())
    print(json.dumps({"case": name, "exit": process.returncode, "report": record}), flush=True)
    return process.returncode, record, output


code, report, output = run_case("robot", document, [
    "--duration", "2", "--startup-timeout", "5", "--timeout", "10", "--qmp", str(external_qmp)])
assert code == 0 and report["status"] == "passed", (report, output)
result = report["result"]
assert result["ready"] == "uart_marker" and result["end_ns"] - result["origin_ns"] >= 2_000_000_000
assert [child["label"] for child in result["children"]] == ["worker", "upper:observer"]
observed = json.loads(observations.read_text())
print(json.dumps({"case": "native-upper", "observation": observed}), flush=True)
diagnostics = dict(field.split("=", 1) for field in observed["cosim"].split(","))
assert diagnostics["v2_initialized"] == "1" and int(diagnostics["rx_frames"]) >= 100, observed
assert observed["state"]["board"] == "DM-MC02" and observed["state"]["timestamp_ns"] > 0
assert len(observed["identities"]) == 3, observed["identities"]
assert all(identity(item["pid"]) != item for item in observed["identities"])
assert not external_qmp.exists(), "native external QMP socket remains"
assert "upper_time_ns" in result["children"][1]["stdout"]
print("Robot native composition: actual UART ready, worker v2 traffic, native upper QMP and cleanup passed")

document["ready"]["text"] = "missing application marker"
document["upper_computers"] = {}
code, report, output = run_case("missing-ready", document, [
    "--duration", "1", "--startup-timeout", ".5", "--timeout", "2"])
assert code != 0 and report["status"] == "failed", (report, output)
assert report["error_type"] in ("TimeoutError", "StartupTimeoutError"), report
print("Robot missing application ready: bounded failure retained")

document.pop("ready")
document["upper_computers"] = {"observer": {"command": [sys.executable, "-c", "raise SystemExit(0)"]}}
code, report, output = run_case("early-exit", document, ["--duration", "2", "--timeout", "10"])
assert code != 0 and report["status"] == "failed", (report, output)
assert "upper:observer exited early (0)" in report["error"], report
print("Robot early upper exit 0: failure retained after cleanup")
