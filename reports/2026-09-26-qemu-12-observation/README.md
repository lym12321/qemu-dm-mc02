# QEMU-12 Release ADC/DMA observation

Initial observation: 2026-09-26 UTC; correction: 2026-09-27 UTC. Status:
the H723 ADC rank processing-time gap is confirmed and corrected at the SoC
layer. The initial QMP run below is historical read-only evidence.

## Boundary and identity

- Producer: the existing Release firmware ELF; boundary: QEMU exception/error
  logging and QMP reads after stopping the guest; consumer: selection of the
  next evidence-backed H723 slice.
- QEMU: `build/qemu/qemu-system-arm`, SHA-256
  `753b34996e421708eef2b9650035ed7fd2443713a2cf2375f61d8d51c5df752a`.
- Firmware: `../trobot/build/Release-current/trobot.elf`, SHA-256
  `40a67471aae051785b2523be761b30492f9b6ff8cfe34fbeca455f2f729d8a09`.
- The initial observation changed no source, firmware, wire, or device-model
  files. The later correction changed the ADC model and qtests only.

## Observation

A two-second bounded diagnostic run used the same `dm-mc02` machine and Release
ELF, with QEMU `guest_errors`, `unimp`, `int`, and `cpu_reset` logging. The
expected `timeout` exit was 124. The log contained no guest-error or
unimplemented-access messages and no HardFault, BusFault, or UsageFault. It
recorded IRQ exception 29 frequently; the Release vector table maps this to
`DMA1_Stream2_IRQHandler`. The trace is perturbed by debug logging, so its
interrupt counts are not a timing measurement. The compressed raw trace is
`build/test-results/qemu-12-observation-20260926/qemu.log.gz`.

A separate QMP run started halted, continued until `xTickCount` reached 283
(about 0.304 host seconds), stopped, and queried the live peripheral window:

| State | Observed value |
| --- | ---: |
| `DMA1_LISR` / `DMA1_HISR` | `0x00000000` / `0x00000000` |
| DMA1 Stream2 `CR` / `NDTR` | `0x00002d1f` / `1` |
| DMA1 Stream2 `PAR` / `M0AR` | `0x40022040` / `0x2401bee0` |
| DMAMUX1 channel 2 `CCR` | `0x00000009` |
| ADC1 `ISR` / `CR` / `CFGR` / `SQR1` | `0x00000009` / `0x10000005` / `0x00002003` / `0x00013101` |

The firmware configures ADC1 for two regular ranks, continuous conversion,
circular DMA, and DMA1 Stream2. The stopped snapshot therefore confirms an
active ADC1-to-DMA1 Stream2 path and shows no DMA status flag latched at that
boundary. QMP also confirmed machine defaults `adc-accurate-timing=true`,
`accurate-timing=false`, and `adc-power-model=false`. The high IRQ count is
consistent with an actively clocked ADC and the enabled half/complete DMA
interrupts; this observation does not prove exact silicon cadence or flag
ordering.

## 2026-09-27 Release DMA buffer follow-up

The same QEMU and Release ELF identities were checked before this run. The ELF
symbol table places FreeRTOS `xTickCount` at `0x240005e4` and the firmware's
static `val[2]` at `0x2401bee0`, matching the earlier DMA Stream2 `M0AR`.
QEMU started with `-S`; QMP initially reported `prelaunch`. After `cont`,
the tick poll first observed `284` at the `>=283` threshold. A subsequent
`stop` was confirmed by `query-status=paused` before RAM/register reads.
QMP `quit` ended QEMU with exit code 0.

| Stopped-state read | Value |
| --- | --- |
| `/machine/vin-mv` / `adc-power-model` | `24000` / `false` |
| Firmware `val[0]` / `val[1]` via `xp /2hx 0x2401bee0` | `0xa941` / `0xffff` |
| ADC1 `ISR` / `CR` / `CFGR` / `CALFACT` | `0x9` / `0x10000005` / `0x2003` / `0x0` |

At the model's default 24 V input, its 11:1 divider and 3.3 V reference
calculate `round(24000000 * 65535 / (11 * 3300000)) = 43329 = 0xa941`.
The board source for channel 19 is `0xffff` while the modeled 3.3 V rail
is good. The Release firmware interprets these samples as about 24.00004 V
through `bsp_adc_vbus()` and no LCD key through `lcd_decode_key()`.
`adc-power-model=false` disables the optional ADC regulator/startup model;
it does not disable these board analog sources.

This checks the configured board source, rank order, DMA target, and firmware
buffer against the project's own model. It is not an independent silicon or
physical-board oracle and did not identify an incorrect ADC sample value.
The direct ST RM0468 PDF request still returned HTTP 567 on 2026-09-27.

## 2026-09-27 H723 conversion-deadline correction

The Release firmware configures ADC1 at 16-bit resolution with two regular
ranks sampled for 32.5 ADC clocks (`../trobot/Core/Src/adc.c`). Its observed
ADC1 `CFGR=0x2003` selects 16-bit resolution and continuous DMA. The local
ST HAL explicitly states 16-bit processing takes 16.5 ADC clocks
(`../trobot/Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_adc.c:346` and
`Inc/stm32h7xx_hal_adc.h:1247`). The old SoC ADC used a fixed 8.5 clocks.
At the modeled 1.5 MHz ADC clock, its first EOC/DMA result appeared after
27,334 ns, but the digital rank deadline is
`ceil((32.5 + 16.5) / 1.5 MHz) = 32,667 ns`.

The new ADC-only qtest first failed at 32,666 ns because EOC was already set.
The direct ADC1→DMA1 Stream2 qtest first failed at the same time because the
destination already held `0xa941` instead of zero. Both red tests used the
old QEMU binary and passed after `dm_mc02_adc.c` selected processing cycles
from `CFGR.RES` for regular and injected ranks. A resolution sweep checks
16/14/12/10-bit deadlines independently; pre-existing ADC and TIM2 qtest
timestamps were updated to the new 16-bit timing. No firmware or wire changed.

The corrected QEMU SHA-256 is
`be6e27b43605c5e9926dc9d54a55ae44a2ea8bc17fc86de293042830557db23f`;
the Release ELF remains
`40a67471aae051785b2523be761b30492f9b6ff8cfe34fbeca455f2f729d8a09`.
The ADC qtest passed 78/78, ADC-focused Meson targets 5/5 (four component
unit tests plus the ADC qtest), and TIM2 qtest
passed 22/22 after its compare fixture was spaced beyond the corrected
12 us reset-rank deadline. The first canonical gate exposed that stale TIM2
fixture (Meson 64/65; Host 51/51, pytest 318/318, smoke 94/94). After the
fixture correction, the authoritative gate exited 0: Meson 65/65, Host 51/51,
pytest 318/318, and smoke 94/94. Its report is
`build/test-results/qemu-gate/20260927T133048.217419Z-31236/`; QEMU and
51 Host binary identities did not change during the test phase.

The standard Release performance gate also exited 0 with the corrected QEMU
and the same ELF. Its exact output is
`build/test-results/qemu-12-release-rtf-20260927.log` (SHA-256
`2f3fe64414f5303174ca901ca89d6585b8769dbe75113b0b3630b22a56a00604`).

| Run | Startup | Host / virtual interval | RTF | QEMU CPU | RSS at sample start | IWDG timeout / window |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0.524866 s | 60.028314 / 60.028000 s | 0.999995x | 113.8% | 50036 KiB | 0 / 0 |
| 2 | 0.525158 s | 60.006135 / 60.005000 s | 0.999981x | 113.6% | 50436 KiB | 0 / 0 |
| 3 | 0.524100 s | 60.010406 / 60.010000 s | 0.999993x | 113.8% | 50256 KiB | 0 / 0 |

The 1.0x target passed with the documented 0.999x QMP/wall-clock sampling
tolerance. This fixed firmware-only paced profile is not an unpaced-capacity,
external-plant, or physical ADC timing measurement.

The slice changed `qemu/upstream/hw/arm/dm_mc02_adc.c`, its ADC qtest and
the TIM2 qtest fixture. The workspace/project `AGENTS.md`, `PLAN.md`,
`ARCHITECTURE.md`, `INTERFACES.md`, `REVIEW.md`, `CAPABILITIES.md`, and
`PROGRESS_REPORT.md` were synchronized with the public digital-deadline
boundary. No `trobot/` file was changed.

## Result and next gate

The first incorrect state was ADC EOC before the vendor-derived digital rank
deadline; DMA consumed that premature publication correctly. ADC remains an
`approximation`, and APP-01 business readiness remains `unverified`.

QEMU-13 will select one independently decidable Release firmware behavior and
trace its first lower-layer state. A reproducible board ADC EOC/DMA edge remains
the separate physical-timing gate for this corrected digital deadline. The
current model does not cover analog sample aperture, SAR transfer curve or DMA
bus arbitration.

## Commands

Correction verification:

```bash
tools/meson test -C build/qemu --num-processes 1 --print-errorlogs \
  'qtest-arm/dm-mc02-adc-test' 'test-dm-adc-*'
tools/meson test -C build/qemu --num-processes 1 --print-errorlogs \
  'qtest-arm/dm-mc02-tim2-test' 'qtest-arm/dm-mc02-adc-test'
python3 tools/dm_mc02_test_gate.py --no-build --jobs 4
QEMU_SYSTEM_ARM="$PWD/build/qemu/qemu-system-arm" \
DM_MC02_ELF="$PWD/../trobot/build/Release-current/trobot.elf" \
  bash tools/run-release-rtf-gate.sh
```

Diagnostic trace:

```bash
timeout --signal=INT --kill-after=2s 2s \
  build/qemu/qemu-system-arm -machine dm-mc02 \
  -kernel ../trobot/build/Release-current/trobot.elf \
  -nodefaults -display none -monitor none -serial none \
  -d guest_errors,unimp,int,cpu_reset \
  -D build/test-results/qemu-12-observation-20260926/qemu.log
```

QMP register observation used the same QEMU and ELF with `-S` and the existing
`tools/dm_mc02_qmp.py` client. It stopped the guest before reading registers.
The separately attempted overlong Unix socket path failed before QEMU startup
and is excluded from this evidence.

The follow-up started the same binaries with `-S -qmp unix:/tmp/dm-mc02-qemu12-adc-20260927.sock,server=on,wait=off`.
It polled `xp /1wx 0x240005e4`, then used QMP `stop`, `query-status`,
`qom-get` and `human-monitor-command` reads of `xp /2hx 0x2401bee0` and the
ADC1 register addresses above. QMP `quit` closed the run; the socket was
removed by QEMU.
