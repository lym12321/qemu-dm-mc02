#!/usr/bin/env python3
r"""Run a finite experiment with the existing minimal-board firmware.

From the repository root, with the project Python dependencies installed:

    cmake -S examples/minimal-board -B build/experiment-firmware -DCMAKE_BUILD_TYPE=Debug
    cmake --build build/experiment-firmware
    python examples/experiment/main.py --qemu build/qemu/qemu-system-arm \
        --firmware build/experiment-firmware/firmware.elf

Use --backend-root when QEMU's backend resources are in another checkout.
Assertions start at Simulation time zero; UART matches are observation upper
bounds. The result is printed only after the public API has cleaned up.
"""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sim.api import NodeSpec, Topology
from sim.apps.experiment import ExperimentInput, run_experiment
from sim.composition import builtin_registry
from sim.config.scene import Scene


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--qemu", type=Path, required=True, help="project QEMU executable")
    parser.add_argument("--firmware", type=Path, required=True, help="minimal-board firmware ELF")
    parser.add_argument("--backend-root", type=Path, default=ROOT)
    args = parser.parse_args()
    registry = builtin_registry(qemu=args.qemu.resolve(), backend_root=args.backend_root.resolve())
    scene = Scene(Topology((NodeSpec("main", "machine.qemu", {
        "model": "dm-mc02", "firmware": args.firmware.resolve()}),)))
    result = run_experiment(scene, registry, inputs=(
        ExperimentInput(40_000_000, "main.key", True),
        ExperimentInput(90_000_000, "main.key", False)), assertions=(
        {"before": "200ms", "uart_contains": {
            "endpoint": "main.uart1", "text": "SIM_READY minimal-board\n"}},
        {"before": "200ms", "uart_contains": {
            "endpoint": "main.uart1", "text": "KEY pressed B0=1\nKEY released B0=0\n"}},
        {"before": "50ms", "digital_equals": {"endpoint": "main.gpio.B0", "value": 1}}))
    print(json.dumps({"passed": result.passed, **asdict(result)}, indent=2))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
