# QEMU-03 firmware RTF epoch evidence

Date: 2026-09-16 UTC

## Source and input identities

- Outer project baseline before this slice: `f245218`
- Nested QEMU fork: `73ac4b85cfcd16a31211e2158fcd9788b7d8c429`
- Tested `qemu-system-arm` SHA-256 before and after:
  `29175e5f691cd73864f88b1765f7e1859281af4db32c95952e28d1f1d60dc248`
- Read-only Release-current `trobot.elf` SHA-256:
  `40a67471aae051785b2523be761b30492f9b6ff8cfe34fbeca455f2f729d8a09`
- The canonical gate reported identical before/after identities for QEMU and
  all 51 native Host test executables.

## Focused checks

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_firmware_rtf.py tests/test_qemu_test_gate.py
```

Result: 25 passed.

```text
bash tools/run-firmware-rtf-reset-smoke.sh
```

Result: PASS. QEMU first emitted `RESUME,STOP`, which the tracker accepted at
zero tick progress. A subsequent QMP `system_reset` emitted
`RESUME,RESET(reason=host-qmp-system-reset)`; the tracker rejected that batch
before changing its previous tick or total progress.

Shell syntax, Python compilation and `git diff --check` also passed.

## Real firmware consumer checks

Virtual-window command:

```text
bash tools/collect-firmware-rtf.sh \
  --elf ../trobot/build/Release-current/trobot.elf \
  --warmup 0 --virtual-seconds 0.1 --ready-tick 1 --poll-interval 0.05
```

Result: 104 ticks / 0.104380 host seconds, RTF `0.996358x`, CPU `105.4%`,
RSS `55304 KiB`, IWDG timeouts `0`.

Baseline command:

```text
bash tools/collect-firmware-rtf.sh \
  --elf ../trobot/build/Release-current/trobot.elf \
  --warmup 0.25 --duration 0.1 --poll-interval 0.05
```

Result: 102 ticks / 0.102846 host seconds, RTF `0.991770x`, CPU `116.7%`,
RSS `50484 KiB`, IWDG timeouts `0`. These short samples verify the normal
collector path; they are not the standard three-run Release performance gate.

## Authoritative project gate

```text
python3 tools/dm_mc02_test_gate.py --no-build --jobs 4
```

Result: exit 0, PASS.

| Collection | Passed | Failed | Skipped | Blocked |
| --- | ---: | ---: | ---: | ---: |
| Meson project unit/qtest | 65 | 0 | 0 | 0 |
| Native Host CTest | 51 | 0 | 0 | 0 |
| Complete pytest | 281 | 0 | 0 | 0 |
| Stable shell smoke | 93 | 0 | 0 | 0 |

ROS2 and MuJoCo were available and passed. The gate did not invoke Renode.
Raw logs and structured output remain in the ignored local directory
`build/test-results/qemu-gate/20260916T113407.918735Z-269450/`.

## Boundary and limits

This is a tooling sampling-validity slice. It does not change the STM32H723 or
DM-MC02 model, firmware, wire protocol, component VMState or external plant.
The FreeRTOS tick remains a proxy for this fixed firmware profile. QEMU-04
still owns finite-number validation, the startup-ready deadline, connect and
command timeout composition, stderr buffering and bounded cleanup. Passing
this slice does not establish another firmware's timing, worker pacing,
silicon reset behavior, or machine migration.
