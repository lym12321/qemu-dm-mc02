# QEMU-07 independent source restore and build evidence

Date: 2026-09-26 UTC. Owner: QEMU build/verification boundary. Producer:
QEMU-06 corrected source package and recorded external inputs; boundary: restore,
configure and build in a separate tmpfs directory; consumer: canonical project
gate.

## Inputs and build

- Package: `build/source-packages/qemu06-delivery-20260926.tar.gz`, SHA-256
  `5f6c1cb9c9866a92f45fded6ce3a966c803b5ddaa653801027a854b95232c06f`.
- Restored project/build root: `/dev/shm/dm-mc02-qemu07-v3/project`; this had no
  Git metadata and no old build tree. The restored nested QEMU fork is
  `73ac4b85cfcd16a31211e2158fcd9788b7d8c429`.
- Host: Ubuntu 24.04 x86_64. Python dependencies were installed from the
  project's locked `uv` configuration; QEMU configure used system Python
  (`PYTHON=/usr/bin/python3`) because the isolated Python has no `ensurepip`.
- External Release input:
  `/home/lab/sim/trobot/build/Release-current/trobot.elf`, SHA-256
  `40a67471aae051785b2523be761b30492f9b6ff8cfe34fbeca455f2f729d8a09`.

Commands:

```bash
uv sync --locked --group dev --extra mujoco
PYTHON=/usr/bin/python3 bash tools/build-qemu.sh
cmake -S . -B build/host -DCMAKE_BUILD_TYPE=Release
cmake --build build/host --parallel 4
PYTHON=/usr/bin/python3 DM_MC02_ELF=/home/lab/sim/trobot/build/Release-current/trobot.elf \
  python3 tools/dm_mc02_test_gate.py --jobs 4
```

The canonical gate exited 0: Meson 65/65, native Host CTest 51/51,
pytest 300/300 and shell smoke 94/94. ROS2 and MuJoCo ran and passed. The gate
report and full logs are preserved at
`build/test-results/qemu-gate-qemu07-20260926/`; its summary is
`summary.json`. The independently built QEMU SHA-256 was
`7a47ae7023edc856eed08f977ee10920a8daaa274fb61172157d2fa79c6e00f5`.
The Release ELF identity remained the hash above. Host test executable
identities were recorded before and after the gate and remained unchanged.

## Findings and scope

The first clean configure exposed four missing QEMU-owned Berkeley wrap
`patch_directory` overlay files. QEMU-06 was corrected to package those files;
the corrected 11,608-path archive was then restored and built. The first
corrected-build pytest run had 299 passing and one failing source-packager test
because that test assumed `.git` metadata. The test was changed to assert the
explicit overlay path mapping; the final run passes 300/300. An initial
configure attempt also exposed the isolated Python `ensurepip` limitation;
selecting system Python resolved it. These findings and their fixes are covered
by the source-package tests and final gate.

This establishes source restoration, configuration, build and the project gate
on the recorded Ubuntu 24.04 x86_64 environment. It does not establish
cross-platform reproducibility, byte-identical binaries, bundled system
packages or firmware, the upstream QEMU test suite, or Release real-time
performance. QEMU-08 remains the next gate.

Changed files: `PLAN.md`, `README.md`, `CAPABILITIES.md`, `REVIEW.md`, this
report, workspace `PLAN.md` and `PROGRESS_REPORT.md`. No production model or
`trobot/` file changed.
