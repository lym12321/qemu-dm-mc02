# QEMU-11 worker/backend startup validation evidence

Date: 2026-09-26 UTC. Owner: simulation worker/backend startup boundary.
Producer: worker timing configuration and direct/registry factories; boundary:
worker admission; consumer: socket session, v1/v2 RESET, `StepCoordinator` and
backend `step()`.

## Implementation

The worker now rejects non-finite or non-positive `rate` and `connect-timeout`
values both during CLI parsing and at the direct `run()` entry. Direct factories
and registry factories pass through the same backend validation before RESET is
sent or `step()` can run. The required callable methods are `step`, `set_motor`,
`reset_motor`, `set_motor_enabled`, `set_motor_dm`, and `motor_feedback`;
optional `reset` and `close` members must be callable when present.

If a returned backend fails validation, the worker attempts `close()` when it
is callable. Backend admission errors close already-open co-sim, FDCAN and
SocketCAN sockets. `MotorBackend` now also declares the existing `step()`
operation. The generic backend registry remains protocol-neutral; no wire, CLI
option names, QEMU device model, or firmware behavior changed.

Changed implementation and test files: `tools/dm_mc02_sim_worker.py`,
`tools/dm_mc02_motor_adapter.py`, and `tests/test_worker_backend_registry.py`.
The worker contract is recorded in `AGENTS.md`, `ARCHITECTURE.md`,
`INTERFACES.md`, and `CAPABILITIES.md`; project/workspace status is in
`PLAN.md`, `REVIEW.md`, and the workspace `PLAN.md`/`PROGRESS_REPORT.md`.

## Verification

Focused tests and direct worker smokes passed:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  tests/test_worker_backend_registry.py tests/test_motor_adapter.py \
  tests/test_motor_protocol.py tests/test_step_coordinator.py
bash tools/run-backend-plugin-smoke.sh
bash tools/run-qemu-worker-smoke.sh
python3 -m py_compile tools/dm_mc02_sim_worker.py \
  tools/dm_mc02_motor_adapter.py tests/test_worker_backend_registry.py
python3 tools/dm_mc02_test_gate.py --jobs 4
```

The focused system-Python run reported 39 passed and one optional MuJoCo skip.
Both worker smokes passed. The canonical project gate exited 0:

| Collection | Result |
| --- | ---: |
| QEMU Meson tests | 65/65 passed |
| Host CTest | 51/51 passed |
| pytest | 318/318 passed |
| shell smoke checks | 94/94 passed |

The optional ROS2 and MuJoCo smoke paths both ran and passed in the canonical
gate. QEMU SHA-256 was
`753b34996e421708eef2b9650035ed7fd2443713a2cf2375f61d8d51c5df752a` before and
after testing; all 51 Host binary identities also remained unchanged. Full logs
and `summary.json` are in
`build/test-results/qemu-gate/20260926T160341.267059Z-458907/`.

## Limits and next slice

Admission checks callable method presence and finite positive timing values. It
does not introspect Python signatures, validate method result shapes, establish
plant fidelity, or define backend/machine migration. No `trobot/` files were
modified and Renode was not used.

QEMU-11 closes Q05-F3. The next slice is QEMU-12: use a reproducible observation
from the current Release firmware to identify one H723 gap that the firmware
actually reaches, then record its first incorrect/missing state and direct
consumer test before selecting a model change.
