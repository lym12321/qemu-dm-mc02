# QEMU-09 internal Flash persistence evidence

Date: 2026-09-26 UTC. Owner: STM32H723 internal Flash image lifecycle.
Producer: an optional exact-size raw image file; boundary: the reusable
`dm_nor_flash_persistence` adapter and DM-MC02 `flash-file` lifecycle;
consumer: the SoC-owned H723 internal Flash RAM.

## Implementation and behavior

The machine now uses `cosim/dm_nor_flash_persistence.[ch]` for internal Flash
image loading and normal-shutdown saving. A missing image preserves the SoC's
erased `0xff` bytes. Short or long images and other load errors reject machine
initialization before guest execution. A configured image saves the exact Flash
size on normal QEMU shutdown, and the `flash-file` property is locked after
machine initialization. Flash contents remain owned by the SoC RAM block; the
adapter only borrows that storage.

Changed implementation and verification files:

- `qemu/upstream/hw/arm/dm_mc02.c`
- `qemu/upstream/tests/qtest/dm-mc02-memory-test.c`
- `cosim/dm_nor_flash_persistence.[ch]`
- `tests/nor_flash_persistence_smoke.c`
- `tools/run-flash-smoke.sh`
- Project contracts and records: `AGENTS.md`, `ARCHITECTURE.md`,
  `INTERFACES.md`, `CAPABILITIES.md`, `PLAN.md`, `README.md`, and `REVIEW.md`

## Verification

The canonical project gate completed with exit status 0:

```bash
python3 tools/dm_mc02_test_gate.py --jobs 4
```

| Gate collection | Result |
| --- | ---: |
| QEMU Meson tests | 65/65 passed |
| Host CTest | 51/51 passed |
| pytest | 300/300 passed |
| shell smoke checks | 94/94 passed |

The isolated persistence adapter test passed with CTest 1/1. The direct
`dm-mc02-memory-test` qtest passed all 3 subtests, covering seeded-image load,
H723 program and normal-shutdown save, reload, and property locking. The Flash
smoke passed programming/sector erase, reset behavior, and bounded rejection of
short and long images:

```bash
ctest --test-dir build/host -R dm_nor_flash_persistence --no-tests=error --output-on-failure
tools/meson test -C build/qemu qtest-arm/dm-mc02-memory-test --no-rebuild --print-errorlogs
bash tools/run-flash-smoke.sh
```

The gate used `build/qemu/qemu-system-arm`, SHA-256
`753b34996e421708eef2b9650035ed7fd2443713a2cf2375f61d8d51c5df752a`. Its full
logs and machine-readable summary are in
`build/test-results/qemu-gate/20260926T145524.284389Z-420684/`; see
`summary.json`, `meson.log`, `host-ctest.log`, `pytest.log`, and the `smoke/`
directory.

## Limits and next slice

Persistence is saved on normal QEMU shutdown only. This does not provide
power-loss atomicity, crash recovery, Flash ECC or erase timing, or machine
migration. No firmware files under `trobot/` were changed.

QEMU-09 closes Q05-F1. The next planned slice is QEMU-10: remove the worker's
duplicate DM-MIT mapping and use the existing `DmMotorBusAdapter`, preserving
the current wire behavior.
