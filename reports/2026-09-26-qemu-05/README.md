# QEMU-05 current capability matrix evidence

Date: 2026-09-26 UTC. This is a documentation/evidence slice. The producer is
current source and registered tests, the public boundary is `CAPABILITIES.md`,
and the consumers are README, plans, review and support claims. No production
code, firmware or wire contract changed.

## Input identity

- Outer project HEAD before this slice: `254b23ee4a850284162b6ca27d1f8c003b346ebd`;
  QEMU fork HEAD: `73ac4b85cfcd16a31211e2158fcd9788b7d8c429`.
- Tested `build/qemu/qemu-system-arm` SHA-256 before and after:
  `29175e5f691cd73864f88b1765f7e1859281af4db32c95952e28d1f1d60dc248`.
- Read-only `../trobot/build/Release-current/trobot.elf` SHA-256:
  `40a67471aae051785b2523be761b30492f9b6ff8cfe34fbeca455f2f729d8a09`.
- The gate reported identical before/after identities for all 51 Host test
  executables. It used existing binaries (`--no-build`), not a clean rebuild.

## Focused and integration checks

- All relative links in `CAPABILITIES.md` resolve after this record was added.
- `dm_mc02_adc.c` decodes regular `SQR1..SQR4`, ranks 1..16; the focused ADC
  qtest passed its 75 subtests. Internal `flash-file` load/save still uses
  machine-local `g_file_*` calls and has the divergent size behavior recorded
  as Q05-F1 in `REVIEW.md`.
- `python3 tools/dm_mc02_test_gate.py --no-build --jobs 4`: exit 0, PASS.

| Collection | Passed | Failed | Skipped | Blocked |
| --- | ---: | ---: | ---: | ---: |
| Meson project unit/qtest | 65 | 0 | 0 | 0 |
| Native Host CTest | 51 | 0 | 0 | 0 |
| Complete pytest | 296 | 0 | 0 | 0 |
| Stable shell smoke | 94 | 0 | 0 | 0 |

ROS2 and MuJoCo smokes passed; Renode was not invoked. Raw logs and structured
output are in the ignored local directory
`build/test-results/qemu-gate/20260926T120835.912585Z-362407/`.

## Scope and next gate

Changed files: `CAPABILITIES.md`, `README.md`, `AGENTS.md`, `ARCHITECTURE.md`,
`INTERFACES.md`, `PLAN.md`, `REVIEW.md`, this record, and workspace `AGENTS.md`,
`PLAN.md`, `PROGRESS_REPORT.md`. The matrix records bounded component evidence;
it does not prove physical accuracy, full business readiness, machine migration,
reproducible build or the current three-run Release real-time gate. QEMU-06 must
inventory canonical source, local changes, dependencies, build configuration
and firmware identity; QEMU-07 must then rebuild in an isolated directory.
