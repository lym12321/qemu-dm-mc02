#!/usr/bin/env python3
"""Run canonical native paused-CAN gates; Rust adapters have separate tests.

The QEMU tests own the original periodic guest, fixed84 wire assertions,
backpressure, native timestamps,83+1 RX completion, FIFO/RAM/IRQ and cleanup.
Python private helper observation counts retired with the Python runtime.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from smoke.support.native_cli import regular_file
from smoke.support.process import identity, stop

GATES = ("paused-can-drain", "preblocked-cpu-can", "paused-can-receipt")


def fingerprint(path):
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", required=True)
    parser.add_argument("--qtest", help="matching canonical dm-mc02-cpu-test executable")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    binary = regular_file(args.qemu, "QEMU", executable=True)
    qtest = regular_file(args.qtest or binary.parent / "tests/qtest/dm-mc02-cpu-test", "QEMU qtest", executable=True)
    report = {"status": "failed", "qemu": fingerprint(binary), "qtest": fingerprint(qtest),
        "sources": fingerprint(ROOT / "qemu/upstream/tests/qtest/dm-mc02-cpu-test.c"), "gates": [],
        "retired": "Python helper monkeypatches, parser and QMP observation-group counts",
        "rust_adapter_evidence": ["can_boundary::actual_backpressure_preserves_exact_old_epoch_suffix_and_submission_count",
            "machine::canonical_can_receipt_includes_native_drops_and_output_keeps_its_source_time",
            "machine::can_counter_overrun_and_native_tx_drop_freeze_without_publishing_a_successful_cut"],
        "claims": "native fixed84 publication/receipt; no physical CAN, firmware consumption or global CPU/world barrier"}
    primary = None
    with args.report.open("x", encoding="utf-8") as output:
        try:
            for gate in GATES:
                row = {"test": "/dm-mc02/virtual-execution/" + gate}
                report["gates"].append(row)
                process = None
                started = time.monotonic()
                try:
                    process = subprocess.Popen([str(qtest), "-p", row["test"]], cwd=binary.parent,
                        env={**os.environ, "QTEST_QEMU_BINARY": str(binary)},
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
                    row["identity"] = identity(process.pid)
                    stdout, stderr = process.communicate(timeout=30)
                    row.update(returncode=process.returncode, stdout=stdout.decode(errors="replace"),
                               stderr=stderr.decode(errors="replace"))
                    if process.returncode:
                        raise RuntimeError("canonical native gate failed: " + gate)
                finally:
                    stop(process)
                    if process is not None:
                        process.stdout.close()
                        process.stderr.close()
                        row["identity_gone"] = identity(process.pid) != row["identity"]
                        row["reaped"] = process.returncode is not None
                    row["elapsed_s"] = time.monotonic() - started
                if not row["identity_gone"] or not row["reaped"]:
                    raise AssertionError("canonical qtest process remains")
            if report["qemu"] != fingerprint(binary) or report["qtest"] != fingerprint(qtest):
                raise AssertionError("native input changed during gate")
            report["status"] = "passed"
        except BaseException as error:
            primary = error
            report["error"] = {"type": type(error).__name__, "message": str(error),
                               "notes": list(getattr(error, "__notes__", ()))}
        json.dump(report, output, indent=2)
        output.write("\n")
    if primary is not None:
        raise primary
    print(f"RESULT: canonical native paused CAN gates passed; {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
