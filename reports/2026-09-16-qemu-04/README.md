# QEMU-04 startup deadline and cleanup evidence

Date: 2026-09-16 UTC

## Source and input identities

- Outer project baseline before this slice:
  `0a84f8cfc6070e6aeb498e2aa3c7953df0b7c965`
- Nested QEMU fork: `73ac4b85cfcd16a31211e2158fcd9788b7d8c429`
- Tested `qemu-system-arm` SHA-256 before and after:
  `29175e5f691cd73864f88b1765f7e1859281af4db32c95952e28d1f1d60dc248`
- Read-only Release-current `trobot.elf` SHA-256:
  `40a67471aae051785b2523be761b30492f9b6ff8cfe34fbeca455f2f729d8a09`
- The canonical gate reported identical before/after identities for QEMU and
  all 51 native Host test executables.

## Focused and direct checks

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_qmp_client.py tests/test_firmware_rtf.py \
  tests/test_qemu_test_gate.py
```

Result: 41 passed with one existing upstream event-loop deprecation warning.
Coverage includes a QMP server that accepts but never sends its greeting, an
upstream disconnect coroutine that never completes, frozen ready tick, early
exit, QMP disconnect, non-finite inputs, zero-tick windows, bounded TERM/KILL
and release after a close timeout.

```text
bash tools/run-firmware-rtf-lifecycle-smoke.sh
```

Result: PASS. A real DM-MC02 QEMU WFI guest completed a normal baseline and was
reaped. The same live guest with a frozen tick failed its 0.10-second
startup-ready deadline and was reaped. A controlled status-17 process retained
its stderr diagnostic and exited. PID plus `/proc/<pid>/stat` start time proved
the process identity in all paths.

Shell syntax, Python compilation and `git diff --check` also passed.

## Real firmware consumer checks

Release-current virtual sample: 103 ticks / 0.103930 host seconds, RTF
`0.991049x`, CPU `96.2%`, RSS `54808 KiB`, IWDG timeouts `0`.

Release-current baseline after 0.25-second warmup: 102 ticks / 0.102643 host
seconds, RTF `0.993738x`, CPU `107.2%`, RSS `50496 KiB`, IWDG timeouts `0`.
These short samples verify the normal collector lifecycle; they are not the
standard three-run Release performance gate.

## Authoritative project gate

```text
python3 tools/dm_mc02_test_gate.py --no-build --jobs 4
```

Result: exit 0, PASS.

| Collection | Passed | Failed | Skipped | Blocked |
| --- | ---: | ---: | ---: | ---: |
| Meson project unit/qtest | 65 | 0 | 0 | 0 |
| Native Host CTest | 51 | 0 | 0 | 0 |
| Complete pytest | 296 | 0 | 0 | 0 |
| Stable shell smoke | 94 | 0 | 0 | 0 |

ROS2 and MuJoCo were available and passed. The gate did not invoke Renode.
Raw logs and structured output remain in the ignored local directory
`build/test-results/qemu-gate/20260916T145247.710509Z-279569/`.

## Boundary and limits

This slice changes host tooling lifecycle only. It does not change the H723 or
DM-MC02 model, firmware, co-simulation wire, component VMState or external
plant. The 10-second startup deadline is a collector policy, not a guest timing
contract. Passing it does not establish another firmware's startup, unpaced
capacity, worker pacing, silicon behavior or machine migration.
