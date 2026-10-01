# QEMU-10 worker motor adapter evidence

Date: 2026-09-26 UTC. Owner: simulation worker to reusable motor adapter
boundary. Producer: DM-MIT/legacy-float CAN frames; boundary:
`DmMotorBusAdapter`; consumer: `StepCoordinator` and the selected motor backend.

## Implementation

`DmMotorBusAdapter` is now the worker's only production owner for CAN command
decoding, effective DM motor ID mapping and feedback encoding. The worker's
duplicate codec and ID helpers, tuple-command dispatcher, and feedback encoding
fallback were removed. CLI defaults and effective-ID collision validation use
adapter-owned constants and `motor_ids()`. The worker retains plant dynamics and
CAN transport; the adapter API, CLI, CAN bytes and co-simulation v1/v2 protocols
did not change.

Changed files: `tools/dm_mc02_sim_worker.py`,
`tests/test_motor_adapter.py`, and `tests/test_motor_protocol.py`. Project
tracking and evidence are in `PLAN.md`, `REVIEW.md`, `CAPABILITIES.md`, and this
report.

## Verification

Focused motor tests passed 19 cases with one optional MuJoCo case skipped in the
focused invocation. Both direct integration smokes passed:

```bash
python3 -m pytest -q tests/test_motor_adapter.py tests/test_motor_protocol.py tests/test_step_coordinator.py
bash tools/run-dm-motor-smoke.sh
bash tools/run-qemu-v2-motor-smoke.sh
python3 tools/dm_mc02_test_gate.py --jobs 4
```

The canonical gate exited 0:

| Collection | Result |
| --- | ---: |
| QEMU Meson tests | 65/65 passed |
| Host CTest | 51/51 passed |
| pytest | 302/302 passed |
| shell smoke checks | 94/94 passed |

The gate used QEMU SHA-256
`753b34996e421708eef2b9650035ed7fd2443713a2cf2375f61d8d51c5df752a`. Full logs
and `summary.json` are in
`build/test-results/qemu-gate/20260926T155035.751095Z-447633/`.

## Limits and next slice

The adapter mapping still has no recorded physical FDCAN trace oracle. These
tests establish the worker/adapter software contract, not physical motor
fidelity. No `trobot/` files or QEMU SoC model were changed.

QEMU-10 closes Q05-F2. The next slice is Q05-F3: reject non-finite or
non-positive worker `rate` and `connect-timeout` values and validate the
backend factory's required method set during startup, before the first step.
