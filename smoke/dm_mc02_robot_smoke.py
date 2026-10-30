"""Canonical scene CLI, actual UART readiness and trusted upper QMP consumer.

This busy-MMIO ELF executes one instruction per native virtual nanosecond.
Its 2 s functional gate uses 120 s of host time; the former free-running 10 s
budget is not a performance guarantee for exact CPU cuts. Keep its failed
migration measurement separate from functional acceptance.
"""

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys

import yaml

ROOT, ELF, RUN = map(Path, sys.argv[1:])
sys.path.insert(0, str(ROOT))
from sim.apps.presets import four_wheel_document
from sim.machines.qemu.process import stop

binary = Path(os.environ.get('QEMU_SYSTEM_ARM', str(ROOT / 'build/qemu/qemu-system-arm'))).resolve()
print(json.dumps({'firmware': str(ELF), 'elf_sha256': hashlib.sha256(ELF.read_bytes()).hexdigest(),
                  'qemu': str(binary), 'qemu_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
                  'qemu_version': subprocess.check_output([str(binary), '--version'], text=True)}), flush=True)


def identity(pid):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return {"pid": pid, "starttime": int(fields[19])}
    except (FileNotFoundError, ProcessLookupError):
        return None


upper, observations, external_qmp = RUN / "upper.py", RUN / "upper.json", RUN / "robot-qmp.sock"
upper.write_text('''import json,os,pathlib,sys,time
sys.path.insert(0,sys.argv[1])
from sim.machines.qemu.qmp import qmp_session_class
from sim.boards import QemuBoard
socket=pathlib.Path(sys.argv[2]); deadline=time.monotonic()+5
while not socket.exists():
    if time.monotonic()>deadline: raise TimeoutError('native monitor startup')
    time.sleep(.001)
qmp=qmp_session_class()(str(socket),timeout=1,connect_timeout=1,close_timeout=1)
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
        diagnostics=qmp.command('qom-get',{'path':'/machine','property':'cosim-diagnostics'})
        pathlib.Path(sys.argv[3]).write_text(json.dumps({'state':state,'cosim':diagnostics,'identities':list(identities.values())}))
        print(json.dumps({'upper_time_ns':state['timestamp_ns']}),flush=True)
        time.sleep(.05)
finally:
    qmp.close()
''')
document = four_wheel_document({"main": ELF}, ROOT / "models/four_wheel.xml",
    ready={"main": "LAB_READY\n"}, duration="2s")
document["nodes"]["upper"] = {"kind": "app.host-processes", "commands": {"observer": {
    "command": [sys.executable, str(upper), str(ROOT), str(external_qmp), str(observations)]}}}


def run_case(name, manifest, options):
    path, report = RUN / (name + ".yaml"), RUN / (name + ".json")
    path.write_text(yaml.safe_dump(manifest))
    process = None
    with (RUN / (name + ".log")).open("wb") as log:
        try:
            process = subprocess.Popen([sys.executable, "-m", "sim", "run", str(path),
                "--allow-host-processes", "--qemu", os.environ.get("QEMU_SYSTEM_ARM", str(ROOT / "build/qemu/qemu-system-arm")),
                "--report", str(report), *options], cwd=ROOT,
                stdout=log, stderr=log, start_new_session=True)
            original = identity(process.pid)
            process.wait(timeout=130)
            assert identity(process.pid) != original, "CLI identity remains after reap"
        finally:
            stop(process)
    output = (RUN / (name + ".log")).read_bytes()[-12000:].decode(errors="replace")
    assert report.exists(), output
    record = json.loads(report.read_text())
    print(json.dumps({"case": name, "exit": process.returncode, "report": record}), flush=True)
    return process.returncode, record, output


code, report, output = run_case("robot", document, ["--startup-timeout", "5", "--timeout", "120", "--qmp", str(external_qmp)])
assert code == 0 and report["status"] == "passed", (report, output)
result = report["result"]
assert result["end_ns"] - result["origin_ns"] == 2_000_000_000 and set(result["ready_receipts"]) == {"main"}
observed = json.loads(observations.read_text())
print(json.dumps({"case": "native-upper", "observation": observed}), flush=True)
diagnostics = dict(field.split("=", 1) for field in observed["cosim"].split(","))
assert diagnostics["v2_initialized"] == "1" and int(diagnostics["rx_frames"]) >= 100, observed
assert observed["state"]["board"] == "DM-MC02" and observed["state"]["timestamp_ns"] > 0
assert len(observed["identities"]) == 2, observed["identities"]
assert all(identity(item["pid"]) != item for item in observed["identities"])
assert not external_qmp.exists(), "native external QMP socket remains"
print("Scene native composition: actual UART ready, v2 traffic, upper QMP and cleanup passed")

document["experiment"]["ready"][0]["text"] = "missing application marker"
document["nodes"].pop("upper")
code, report, output = run_case("missing-ready", document, ["--startup-timeout", ".5", "--timeout", "2"])
assert code != 0 and report["status"] == "failed", (report, output)
assert report["error_type"] in ("TimeoutError", "StartupTimeoutError"), report

document["experiment"]["ready"] = []
document["nodes"]["upper"] = {"kind": "app.host-processes", "commands": {"observer": {
    "command": [sys.executable, "-c", "raise SystemExit(0)"]}}}
code, report, output = run_case("early-exit", document, ["--timeout", "10"])
assert code != 0 and report["status"] == "failed" and "observer exited (0)" in report["error"], (report, output)
print("Scene missing-ready and upper exit0: bounded failures retained after cleanup")
