# QEMU-02 unified test gate evidence

Date: 2026-09-16 UTC

## Source identities

- Outer project baseline before this slice:
  `e7ff7d78941c8fd2a73f64877c8a601791ff1ab9`
- Nested QEMU fork:
  `73ac4b85cfcd16a31211e2158fcd9788b7d8c429`
- QEMU build type: `release`
- Tested `qemu-system-arm` SHA-256 before and after:
  `29175e5f691cd73864f88b1765f7e1859281af4db32c95952e28d1f1d60dc248`
- The 51-entry Host executable identity map was identical before and after;
  its canonical-JSON SHA-256 was
  `31300f4a2a7e0cdfb8a1c5224cc1315e5c6bc0b294e99d9821c37aab8d153636`.

## Commands and results

Focused gate tests:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q \
  tests/test_qemu_test_gate.py
```

Result: 9 passed.

Authoritative project gate:

```text
python3 tools/dm_mc02_test_gate.py --jobs 4
```

Result: exit 0, PASS.

| Collection | Passed | Failed | Skipped | Blocked |
| --- | ---: | ---: | ---: | ---: |
| Meson project unit/qtest | 65 | 0 | 0 | 0 |
| Native Host CTest | 51 | 0 | 0 | 0 |
| Complete pytest | 265 | 0 | 0 | 0 |
| Stable shell smoke | 92 | 0 | 0 | 0 |

ROS2 and MuJoCo were available and ran successfully. The gate did not invoke
Renode. Raw runner logs and the structured summary remain under the ignored
local path
`build/test-results/qemu-gate/20260916T110426.363240Z-253113/`.

## Boundary and limits

This slice owns the validation-tool boundary. It adds no device, board,
firmware or co-simulation wire behavior. PASS proves only the four discovered
project inventories for the recorded binaries; it does not cover the complete
upstream QEMU suite, a real external plant performance profile, silicon timing
or whole-machine migration.
