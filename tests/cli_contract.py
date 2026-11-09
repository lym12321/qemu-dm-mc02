#!/usr/bin/env python3
"""Independent deployment consumer; imports no project runtime or optional SDK."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def check(executable):
    with tempfile.TemporaryDirectory(prefix="sim.cli-contract.") as name:
        root = Path(name)
        binary = root / "sim"
        shutil.copy2(executable, binary)
        environment = {"PATH": "/usr/bin:/bin", "HOME": str(root)}

        def run(*argv, code=0):
            result = subprocess.run([str(binary), *map(str, argv)], cwd=root,
                                    env=environment, capture_output=True, text=True, timeout=15)
            assert result.returncode == code, (argv, result.returncode, result.stdout, result.stderr)
            return result

        assert "firmware" in run("--help").stdout
        scene = root / "scene.yaml"
        scene.write_text('''schema: sim/v1
runtime: {duration: 3ms}
nodes:
  host: {kind: device.can-host}
  peer: {kind: device.can-host}
  bus: {kind: bus.can}
links:
  - [host.can, bus.can]
  - [peer.can, bus.can]
experiment:
  inputs:
    - {at: 1ms, endpoint: host.tx, value: {id: 291, dlc: 1, data_hex: "42"}}
  assertions:
    - before: 3ms
      can_seen: {endpoint: host.can, id: 291, data_hex: "42"}
''')
        run("validate", scene)
        live, offline, trace = (root / file for file in ("live.json", "offline.json", "observations.mcap"))
        run("run", scene, "--report", live, "--trace", trace)
        scene.unlink()
        run("replay", trace, "--report", offline)
        actual, replay = json.loads(live.read_text()), json.loads(offline.read_text())
        assert actual["status"] == replay["status"] == "passed"
        assert actual["result"] == replay["result"]
        assert actual["result"]["status"] == ["pass"]
        assert actual["result"]["delivered_inputs"] == 1
        (root / "firmware.elf").write_bytes(b"admission fixture; never executed")
        scene.write_text("schema: sim/v1\nnodes:\n  main:\n    kind: machine.qemu\n"
                         "    firmware: firmware.elf\nlinks: []\n")
        report = root / "blocked.json"
        run("run", scene, "--qemu", root / "missing-qemu", "--report", report, code=78)
        blocked = json.loads(report.read_text())
        assert blocked["status"] == "blocked" and blocked["error_type"] == "BackendUnavailable", blocked
        assert "missing-qemu" in blocked["error"]
        if shutil.which("ldd"):
            linkage = subprocess.check_output(["ldd", str(binary)], text=True).lower()
            assert "python" not in linkage, linkage
        assert not any(p.suffix in {".py", ".pyc"} for p in root.rglob("*"))
        print("PASS: standalone CLI, capture/offline replay, missing backend, no Python payload")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sim", type=Path, required=True)
    check(parser.parse_args().sim.resolve(strict=True))
