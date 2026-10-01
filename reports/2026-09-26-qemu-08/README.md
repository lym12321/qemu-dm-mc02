# QEMU-08 Release real-time gate evidence

Date: 2026-09-26 UTC. Owner: Release performance measurement boundary.
Producer: fixed Release ELF and QEMU-07 independent build; boundary: the
validated QMP/tick/watchdog collector; consumer: the standard three-run
60-virtual-second Release gate.

## Inputs and command

- QEMU: `/dev/shm/dm-mc02-qemu07-v3/project/build/qemu/qemu-system-arm`,
  SHA-256 `7a47ae7023edc856eed08f977ee10920a8daaa274fb61172157d2fa79c6e00f5`.
- Firmware: `/home/lab/sim/trobot/build/Release-current/trobot.elf`, SHA-256
  `40a67471aae051785b2523be761b30492f9b6ff8cfe34fbeca455f2f729d8a09`.
- Profile: default DM-MC02 peripherals, `-nodefaults`, no worker or external
  plant, zero extra warmup, readiness at tick 250, three continuous 60 virtual
  second windows, target `1.0x` with the existing `0.999x` measurement tolerance.
- Command:

```bash
QEMU_SYSTEM_ARM=/dev/shm/dm-mc02-qemu07-v3/project/build/qemu/qemu-system-arm \
DM_MC02_ELF=/home/lab/sim/trobot/build/Release-current/trobot.elf \
  bash tools/run-release-rtf-gate.sh
```

Exit status: 0. Full stdout/stderr:
`build/test-results/qemu-08-20260926/release-rtf-gate.log`, SHA-256
`2841d2a3218f50c5af2451d7f4374086ca8e9f4544c8b15519b21a0ad72fb7c2`.

## Results

| Run | Startup latency | Host interval | Virtual interval | RTF | CPU | RSS at sample start | IWDG timeouts / window violations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.525901 s | 60.020332 s | 60.020000 s | 0.999994x | 115.8% | 50360 KiB | 0 / 0 |
| 2 | 0.524697 s | 60.008564 s | 60.007000 s | 0.999974x | 115.8% | 50380 KiB | 0 / 0 |
| 3 | 0.524512 s | 60.008334 s | 60.007000 s | 0.999978x | 115.8% | 50168 KiB | 0 / 0 |

All three runs passed. RTF min/mean/max was `0.999974x` / `0.999982x` /
`0.999994x`. Startup min/mean/max was `0.524512` / `0.525037` /
`0.525901` seconds. CPU min/mean/max was `115.750146%` / `115.753836%` /
`115.760773%`. RSS min/mean/max was `50168` / `50302.67` / `50380 KiB`.
RSS is the collector's sample-start resident size, not peak RSS. Each run
reported 999.994, 999.974 and 999.978 effective FreeRTOS ticks/s respectively;
IWDG starts/reloads were 1/12041, 1/12038 and 1/12038, with no timeout.

## Limits and next slice

This closes the named firmware-only Release pacing gate for these exact QEMU and
ELF identities on the recorded host. It does not measure unpaced capacity,
worker pacing, external plant behavior, another firmware build, physical
timing, business readiness or machine migration. It is not a cross-platform
performance claim.

QEMU-08 closes the current reproducible engineering baseline. The next
implementation slice is QEMU-09: internal Flash image persistence should reuse
the existing exact-size raw-image persistence adapter, with an isolated file
lifecycle test followed by a direct machine consumer gate. Q05-F2/F3 remain
separate deferred slices.
