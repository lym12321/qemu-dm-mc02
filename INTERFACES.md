# 0.51 STM32H723 SoC RAM ownership and migration boundary

`DmMc02SocMemory` owns the backing `MemoryRegion` objects for the H723 internal
Flash and CPU RAM windows. The initialization boundary is:

```c
void dm_mc02_soc_memory_init(DmMc02SocMemory *, Object *machine_owner,
                             MemoryRegion *system_memory,
                             const DmMc02SocProfile *, Error **errp);
```

`machine_owner` remains in the API for composition compatibility, but the
standard RAM regions deliberately pass `NULL` to QEMU's initializer. A
`MachineState` is not a `DeviceState`; `NULL` selects QEMU's global RAMBlock
registration and therefore gives each region a migration identity without
coupling this reusable SoC layer to machine QOM ownership.

The following six regions are dynamic and migratable through QEMU's standard
RAM machinery:

```text
dm-mc02.flash       0x08000000  1 MiB
dm-mc02.itcm        0x00000000 64 KiB
dm-mc02.dtcm        0x20000000 128 KiB
dm-mc02.axi-sram    0x24000000 320 KiB
dm-mc02.d2-sram     0x30000000 32 KiB
dm-mc02.d3-sram     0x38000000 16 KiB
```

The factory UID/calibration window is deterministic immutable profile data;
it remains a `memory_region_init_rom_nomigrate()` region and is reconstructed
at realize time. The FDCAN Message RAM is a separate `DmMessageRam` owner and
is not duplicated by this API.

This boundary changes migration registration only. It does not add a local
VMState description, alter the memory map, or decide reset behavior. Warm
reset retains ordinary RAM and Flash; the existing explicit cold-reset path
clears the five volatile CPU RAM regions, while Flash and calibration remain.
The isolated gate is `test-dm-soc-memory` (`2/2`) and the direct consumer gate
is `dm-mc02-memory-test` (`2/2`). A machine-level composite must still define
the restore order for these RAMBlocks relative to CPU/IRQ, DMA, devices,
external buses and co-simulation queues.

# 0.50 STM32H723 OCTOSPI/OCTOSPIM component VMState boundary

`DmMc02Ospi` is the STM32H723 register-facing producer. Its reusable
component-only state interface is:

```c
bool dm_mc02_ospi_state_valid(const DmMc02Ospi *);
void dm_mc02_ospi_sync_runtime(DmMc02Ospi *);
const VMStateDescription *dm_mc02_ospi_vmstate(void);
const VMStateDescription *dm_mc02_ospi_vmstate_raw(void);
const VMStateDescription *dm_mc02_ospim_vmstate(void);
const VMStateDescription *dm_mc02_ospim_vmstate_raw(void);
```

The version-1 `DmMc02Ospi` stream contains the controller register mirror,
bounded RX data and cursor, page-program TX staging and cursor, command
address/opcode, and command-valid/started state. RX length is serialized before
the variable buffer; the incoming buffer is allocated only after the length is
read, and lengths are bounded to 1 MiB. The post-load validator rejects cursor
overflow, impossible TX lengths, invalid active-command combinations, and
truncated streams before runtime projection.

`MemoryRegion`, DMA endpoint callbacks, QEMU SSI bus/device, Flash storage,
geometry and JEDEC configuration are destination-owned wiring or static profile
configuration. The standard `w25q64/m25p80` child remains the sole owner of NOR
command/storage state and its own QEMU VMState; this controller description does
not duplicate it. Normal post-load derives the memory-mapped gate from
`CR.FMODE` and re-selects SSI CS only for an interrupted page-program command.
The raw description validates the same fields without projecting memory or CS,
for use by a future parent composite.

`DmMc02Ospim` has a separate version-1 register-only description. Neither
description registers machine-level migration. A future board composite must
restore Flash child state, controller state, DMA/IRQ/CPU state and board mapping
in an explicit order; passing this component test is not whole-machine
snapshot/migration support.

The isolated gate is `test-dm-ospi-vmstate` (`6/6`). Direct consumers are
`run-ospi-smoke.sh` and `run-board-profile-smoke.sh`; they cover the existing
SSI, DMA endpoint, memory-map and raw-image lifecycle contracts.

# 0.49 DM-MC02 board power component VMState boundary

`DmMc02Power` is the board-level producer for the external VIN input, GPIO
output-latch request and optional electrical-power policy. Its component state
interface is:

```c
bool dm_mc02_power_state_valid(const DmMc02Power *);
void dm_mc02_power_sync_runtime(DmMc02Power *);
const VMStateDescription *dm_mc02_power_vmstate(void);
const VMStateDescription *dm_mc02_power_vmstate_raw(void);
```

The version-1 state contains only the dynamic inputs:

```text
vin_mv, gpio_odr, electrical_power
```

The ADC pointer, output masks, ADC channel numbers and all derived values are
destination-owned. Derived values include the normal/undervoltage/off state,
output enables, 5 V/3.3 V and switched-output-good flags, ADC source raw
values, and `last_update_ns`. This keeps profile wiring static and lets a
destination with a different ADC object rebuild its output projection.

The ordinary post-load validates the version and then invokes
`dm_mc02_power_sync_runtime()` once. That projection updates the derived rails
and the board-source ADC callback. The raw description has the same fields but
does not call the callback, so an enclosing board composite can restore all
children before choosing its single runtime synchronization point. A future
composite must configure identical profile masks/channels before loading this
state; this component does not serialize board topology.

The default DM-MC02 VIN is 24,000 mV. VIN 0 is an explicit disconnected/off
input, 1..11,999 mV is undervoltage, and values at or above 12,000 mV are in
the normal discrete state. The model intentionally does not claim converter
transient, current, ripple, or analog undervoltage behavior.

The isolated gate is `test-dm-power-vmstate` (`4/4`), covering normal rail/ADC
reprojection, raw no-side-effect loading, rejection of an unmasked GPIO ODR
and truncated-state rejection. The
direct board gates are `run-power-boundary-smoke.sh` and
`run-power-runtime-smoke.sh`. This remains a component contract and does not
establish DM-MC02 machine-level snapshot/migration.

# 0.48 Reusable ARMv7-M CPU/NVIC/SysTick boundary

The DM-MC02 SoC composes QEMU's existing ARMv7-M implementation; it does not
introduce a local CPU, NVIC, or SysTick state machine. `ARMv7MState` creates
the `nvic` and `systick-reg-ns` children with `object_initialize_child()`;
`armv7m_realize()` creates the configured CPU as the `cpu` child and realizes
it with `qdev_realize()`. This establishes QOM ownership and canonical paths.
The CPU is realized before NVIC, and SysTick is realized after its clock input
is connected. NVIC input GPIOs are passed to the board only after NVIC
realization, when the external input array exists.

The public board-facing contract is the existing ARMv7-M object interface and
PPB/SCS register map:

```text
ARMv7MState cpu-type = cortex-m7-arm-cpu
ARMv7MState num-irq  = 179       (complete vector count)
DM-MC02 external IRQ inputs      = 163
```

`num-irq` includes the 16 architecturally internal exception vectors, so it
must not be used as the board external-input count. External IRQs enter via
the passed NVIC GPIO inputs and become visible through the existing NVIC
enable/pending/priority/level semantics. SysTick is an internal exception:
its trigger is wired to NVIC's `systick-trigger` input and its pending state
is observed through `SCB->ICSR.PENDSTSET`, not an external `ISPR` bit.

The DM-MC02 reset clock fixture is 64 MHz; with the CPU clock selected by
SysTick, reload `63` expires after 1 virtual microsecond. The test uses
controlled QEMU virtual time, not wall-clock delay.

No new public VMState API is needed. QEMU's native ARM CPU VMState (v22),
NVIC VMState (v4), and SysTick VMState (v3) are supplied by their device/class
definitions and are registered by QEMU's realized-device path in
`qdev_realize()`/`device_set_realized()`. The target must restore the QOM
children, clock sources, CPU/NVIC/SysTick wiring and memory map before loading
those child states. This is a component boundary only: it does not register
DM-MC02 machine migration or claim complete CPU, RAM, peripheral, IRQ-bus or
co-simulation restore.

The direct boundary gate is `dm-mc02-cpu-test` (`2/2`). It checks the three
QOM children, vector-width distinction, external IRQ behavior, SysTick
virtual-time behavior, internal pending visibility and reset clearing.

# 0.46 Reusable message-RAM owner

`DmMessageRam` is the board-independent owner for a controller message-RAM
window backed by QEMU RAM:

```c
bool dm_message_ram_init(DmMessageRam *, DeviceState *owner,
                         const char *name, uint64_t size, Error **errp);
void dm_message_ram_reset(DmMessageRam *);
uint8_t *dm_message_ram_data(DmMessageRam *);
const uint8_t *dm_message_ram_const_data(const DmMessageRam *);
uint64_t dm_message_ram_size(const DmMessageRam *);
MemoryRegion *dm_message_ram_region(DmMessageRam *);
```

`init()` is a one-time initializer for an embedded `DmMessageRam`. `size` must
be non-zero and `name` must be present; allocation failures are returned
through `Error **` and do not expose a data pointer. `owner` is passed to
QEMU's `memory_region_init_ram()` and therefore must be `NULL` or a
`DeviceState`; it is the QEMU memory-region owner, not a second logical SoC
owner. The current STM32H723 composition intentionally passes `NULL`, making
the region a global RAM block and using QEMU's standard RAM migration
registration.

The data and region accessors borrow storage owned by the initialized QEMU
`MemoryRegion`; callers must not free it, replace the region, or retain the
pointer after that region's lifetime. `dm_message_ram_reset()` is explicit and
zeros the complete configured byte range. Initialization does not imply reset;
the SoC/board reset owner decides whether a reset is destructive. No message
RAM bytes are duplicated in FDCAN component VMState.

The isolated gate is `test-dm-message-ram` (`2/2`). Because QEMU's real RAM
allocator belongs to system-mode memory code, that unit target uses a tiny
test-only MemoryRegion symbol stub to exercise owner/accessor/reset logic. The
real implementation and address-space mapping are covered by
`dm-mc02-memory-test` (`1/1`), and the FDCAN standard/medium/bus-off consumers
remain the direct message-RAM regression gates. This interface does not make
DM-MC02 machine-level migration supported; all other machine state and the
cross-component restore order still require their own contracts.

# 0.47 Reusable STM32H723 FDCAN component VMState

`DmMc02Fdcan` exposes the following component-only state boundary:

```c
bool dm_mc02_fdcan_state_valid(const DmMc02Fdcan *);
void dm_mc02_fdcan_sync_runtime(DmMc02Fdcan *);
const VMStateDescription *dm_mc02_fdcan_vmstate(void);
const VMStateDescription *dm_mc02_fdcan_vmstate_raw(void);
```

The current version-2 field set contains the CPU-visible register mirror, the partial
84-byte host-wire receive frame, RX FIFO/Buffer cursors and fill levels, TX
FIFO cursor and pending mask, controller counters, and the bounded 16-entry
host-wire TX queue with per-frame offsets and its absolute virtual retry
deadline, plus the controller's `bus_off` participation gate. The queue is ordered;
a frame may be pending without a deadline
when its chardev is disconnected, while a nonzero deadline always requires a
nonempty queue.

Version-1 streams remain loadable; because they predate `bus_off`, the loader
reconstructs that internal gate from the serialized `PSR.BO` bit before validation.

`MemoryRegion`, message RAM pointer and bytes, QEMU `CanBusState`, chardev,
IRQ handles, transceiver power and host-ACK policy are destination-owned
runtime or board policy and are not serialized. The ordinary post-load checks
the derived FIFO bounds, pending-mask range, queue ring and deadline before
calling `dm_mc02_fdcan_sync_runtime()`. Sync rebuilds derived FIFO sizes,
re-arms the destination timer without writing to the host wire, and projects
the restored IRQ level. The raw description shares the fields and only
normalizes/validates legacy `bus_off`; it performs no timer, IRQ, bus or
chardev projection for a future parent composite.

The isolated gate is `test-dm-fdcan-vmstate` (`5/5`), covering wrapped queue
round-trip/IRQ projection, malformed pending state, truncation, bus-off
round-trip/v1 compatibility and raw no-projection. Existing FDCAN standard-bus,
host-wire, clock, extended-filter,
dedicated-Rx-buffer and bus-off firmware smoke gates remain the direct consumer
checks. This is a component contract only: it does not migrate message RAM,
external CAN-bus state, chardev peer state or the complete DM-MC02 machine.

# 0.46 Reusable STM32H723 PWR/RCC component VMState

`DmMc02PwrRcc` exposes two component-only VMState descriptions:

```c
void dm_mc02_pwr_rcc_sync_runtime(DmMc02PwrRcc *);
bool dm_mc02_pwr_rcc_state_valid(const DmMc02PwrRcc *);
const VMStateDescription *dm_mc02_pwr_rcc_vmstate(void);
const VMStateDescription *dm_mc02_pwr_rcc_vmstate_raw(void);
```

The version-1 ordinary stream serializes the state required to reproduce the
next PWR/RCC-visible result:

```text
pwr_regs[0x400], rcc_regs[0x400], system_clock_source,
adc_clock_configured
```

`system_clock_source` is the effective source after the RCC `SW` request and
readiness boundary, not a duplicate request field. `adc_clock_configured`
preserves the compatibility/default-versus-explicit PLL2 clock phase. The
`MemoryRegion` objects, clock callback, callback opaque, QOM owner, and board
wiring are destination-owned runtime state and are never serialized.

The ordinary post-load accepts only version 1 and an effective source in
`HSI..PLL1` (`0..3`). After all fields are loaded it invokes
`dm_mc02_pwr_rcc_sync_runtime()` once, allowing the DM-MC02 clock root to
reproject CPU, timer, ADC and peripheral kernel clocks. The raw description
shares the same fields but has no post-load side effect; a parent composite
must validate it and choose the restore order before calling its own runtime
sync. VMState field writes before a failure are not transactional rollback.

The isolated gate is `test-dm-pwr-rcc-vmstate` (`4/4`), covering ordinary
round-trip/one notification, invalid-source and truncated-stream rejection,
and raw round-trip without notification. Direct PWR/RCC, zero-clock, dynamic
TIM2, USART/FDCAN clock and alternate-profile gates, QEMU smoke `89/89`, Host
CTest `54/54`, and the ARM system-target relink pass. This component is not
registered with the DM-MC02 machine and does not establish whole-machine
snapshot or migration support; oscillator settling, complete H723 register
semantics, and joint PWR/RCC/peripheral restore order remain separate gates.

# 0.7 STM32H723 USART kernel-clock boundary

`DmMc02PwrRcc` owns the H723 `RCC_D2CCIP2R` USART source muxes and exposes
two board-independent queries:
`dm_mc02_pwr_rcc_usart16_kernel_clock_hz()` for USART1/6/10 (mux bits 5:3)
and `dm_mc02_pwr_rcc_usart234578_kernel_clock_hz()` for USART2/3/UART4/5/7/8
(mux bits 2:0). The source mapping is `000=APB`, `001=PLL2Q`, `010=PLL3Q`,
`011=HSI`, `100=CSI`, `101=LSE`; reserved encodings produce `0 Hz`. APB
sources consume APB2 for the USART16 group and APB1 for the USART234578 group.
PLL sources require the selected PLL, input oscillator, valid divisors, and
the corresponding Q output enable. HSI/CSI/LSE use the modeled immediate
readiness rules already owned by RCC.

The RCC write path marks USART clock state dirty when a source mux, APB
divider, system-clock divider/source, PLL configuration/output enable, or
LSE state can change the result. The DM-MC02 composition root creates one
kernel-clock root per mux group and one child clock per routed UART, then
connects the child to `DmMc02Uart` through
`dm_mc02_uart_set_kernel_clock()`. The UART caches the current rate and does
not read RCC registers or board profile state. The read-only `/machine`
properties `usart16-kernel-clock-hz` and `usart234578-kernel-clock-hz` are
diagnostic/test oracles. The reusable UART consumer below consumes this cached
rate for virtual-time TX pacing; the RCC boundary itself remains unaware of
baud timing.

`tools/run-uart-clock-smoke.sh` is the direct H723-to-board boundary gate.
It starts a fresh guest for each source configuration and checks APB, PLL2Q,
PLL3Q, HSI/HSIDIV, CSI, LSE, and reserved source results for both groups,
including different APB1/APB2 dividers. Separate launches are intentional:
the current QEMU HMP command set has no supported memory-write command for
changing RCC registers during a running fixture. This slice therefore does not
claim oscillator settling, CSS, clock gates, or the complete USART kernel-clock
matrix; the reusable UART consumer's separate virtual-time TX boundary is
documented below.

# 0.6 STM32H723 FDCAN kernel-clock diagnostic boundary

`DmMc02PwrRcc` owns the H723 `RCC_D2CCIP1R.FDCANSEL` source selection at
bits `29:28` and exposes the board-independent query
`dm_mc02_pwr_rcc_fdcan_kernel_clock_hz()`. The source mapping is `00=HSE`,
`01=PLL1Q`, `10=PLL2Q`, and `11=unavailable`; there is no additional FDCAN
prescaler. HSE must be enabled for the HSE source. PLL outputs require the
selected PLL to be enabled, its input oscillator to be enabled, valid M/N/Q
dividers, and the corresponding `PLLCFGR.DIVQEN` bit.

The RCC write path still updates this diagnostic result when the source, PLL
input, divider, fractional, output-enable, or oscillator-enable state can
change. The read-only `/machine` property `fdcan-kernel-clock-hz` is retained
for diagnostics and deterministic test oracles. The FDCAN register model does
not consume this value to schedule frames: QEMU's standard `CanBusState` has no
frame-duration or bit-timing contract, so the previous `set_clock_hz()` and
`fdcan-accurate-timing` production path were removed.

`tools/run-fdcan-clock-smoke.sh` remains the H723 RCC diagnostic gate and checks
all four source encodings: 24 MHz, 120 MHz, 96 MHz, and 0 Hz. It does not claim
PLL lock/oscillator settling, clock gates, CAN bit timing, arbitration or
physical-layer behavior.

# 0.10 QEMU standard CAN bus adapter

`DmCanBusAdapter` is the reusable controller-to-bus boundary. Its producer is
a controller's protocol-neutral frame conversion, the boundary is QEMU's
`CanBusClientState`/`CanBusState`, and consumers are other QEMU CAN clients.
The adapter owns neither FDCAN message RAM, acceptance filters, board power
rails nor host wire framing.

The lifecycle is explicit:

```c
void dm_can_bus_adapter_init(DmCanBusAdapter *adapter);
bool dm_can_bus_adapter_connect(DmCanBusAdapter *, CanBusState *, void *,
                                 DmCanBusCanReceive, DmCanBusReceive);
ssize_t dm_can_bus_adapter_send(DmCanBusAdapter *, const qemu_can_frame *);
void dm_can_bus_adapter_disconnect(DmCanBusAdapter *);
```

Callers initialize once, connect, and disconnect before reconnecting or
destroying the owner. `can_receive()` indicates bus-level participation and
is evaluated by QEMU when determining whether a peer can ACK; it is not an
acceptance-filter or FIFO-capacity result. `receive()` is called for accepted
peer delivery. `send()` returns `-1` for a disconnected endpoint, `0` when no
peer accepts the frame, and a positive value when at least one peer accepts it.
QEMU's standard bus excludes the sender from delivery.

FDCAN converts its internal DLC to QEMU's actual byte length (`0..8, 12, 16,
20, 24, 32, 48, 64`) and converts the QEMU ID/FD flags back on ingress.
Standard-bus ingress has no timestamp field, so the FDCAN consumer stamps a
received frame with the current `QEMU_CLOCK_VIRTUAL` time. The fixed 84-byte
chardev is a separate legacy host wire and preserves its own external
timestamp; it does not pass through the standard bus.

# 0.11 Reusable CORDIC VMState contract

`dm_mc02_cordic_vmstate()` returns the VMState description for the
board-independent `DmMc02Cordic` component. It serializes only the state that
changes the next register-visible operation:

```text
csr, args[2], arg_count, results[2], result_count
```

The `MemoryRegion`, QOM owner and any callbacks are runtime wiring and are not
serialized. The post-load check rejects `arg_count > 2` or `result_count > 2`
with an error, so a malformed stream cannot create an invalid FIFO cursor.
The current description is version 1 and is tested by
`test-dm-cordic-vmstate` using a QEMUFile round-trip. It is a component
contract only: the DM-MC02 machine does not register it yet, and this interface
does not establish whole-machine snapshot, save/load or migration support.

# 0.12 Reusable CRC VMState contract

`dm_mc02_crc_vmstate()` returns the VMState description for the
board-independent `DmMc02Crc` component. It serializes the complete
CPU-visible register mirror `regs[DM_MC02_CRC_REGION_SIZE / 4]` and the
current CRC accumulator `value`:

```text
regs[64], value
```

The `MemoryRegion` is runtime wiring and is not serialized. The description is
version 1 and is tested by `test-dm-crc-vmstate` with a QEMUFile round-trip.
The DM-MC02 machine does not register it yet, so this component contract does
not establish whole-machine snapshot, save/load or migration support.

# 0.13 Reusable RNG VMState contract

`dm_mc02_rng_vmstate()` returns the VMState description for the
board-independent `DmMc02Rng` component. It serializes the state that changes
the next register-visible operation:

```text
regs[DM_MC02_RNG_REGION_SIZE / 4], prng, seed,
fifo[DM_MC02_RNG_FIFO_DEPTH], status, fifo_count, fifo_index, refill_armed
```

The `MemoryRegion` and `qemu_irq` are runtime wiring and are not serialized.
The post-load check rejects a FIFO count or index outside the fixed capacity,
and rejects a refill marker paired with non-empty data. The description is
version 1 and is tested by `test-dm-rng-vmstate` with a QEMUFile round-trip and
malformed-state cases. The DM-MC02 machine does not register it yet, so this
component contract does not establish whole-machine snapshot, save/load or
migration support.

# 0.14 Reusable DBGMCU VMState contract

`dm_mc02_dbgmcu_vmstate()` returns the VMState description for the
board-independent `DmMc02Dbgmcu` register window. It serializes the mutable
register mirror:

```text
regs[DM_MC02_DBGMCU_REGION_SIZE]
```

`MemoryRegion` and QOM ownership are runtime wiring and are not serialized.
`DBGMCU_IDCODE` at offset `0x00` is a fixed, read-only H723 identity
(`0x20030483`) and is derived by the register consumer rather than loaded
from the mirror. The description is version 1; `test-dm-dbgmcu-vmstate`
covers full round-trip and truncated-stream rejection, while
`tools/run-dbgmcu-smoke.sh` covers IDCODE read-only behavior and control
register readback. This component contract does not establish whole-machine
snapshot, save/load or migration support.

# 0.15 Reusable SYSCFG VMState contract

`dm_mc02_syscfg_vmstate()` returns the VMState description for the
board-independent `DmMc02Syscfg` register window. It serializes only the
CPU-visible register mirror:

```text
regs[DM_MC02_SYSCFG_REGION_SIZE]
```

`MemoryRegion`, the `changed` callback, and its opaque pointer are runtime
wiring and are not serialized. After a successful load, the description calls
the destination's existing `changed` callback once. This lets a board or
another consumer re-derive EXTI routing from the restored `EXTICR1..4`
bytes without making the callback part of the migration stream. The
description is version 1; `test-dm-syscfg-vmstate` covers exact round-trip,
post-load consumer notification, and truncated-stream rejection. This
component contract does not establish whole-machine snapshot, save/load or
migration support.

# 0.16 Reusable FMC VMState contract

`dm_mc02_fmc_vmstate()` returns the VMState description for the board-independent
`DmMc02Fmc` register window. It serializes only the CPU-visible register mirror:

```text
regs[DM_MC02_FMC_REG_REGION_SIZE]
```

`MemoryRegion` and QOM ownership are runtime wiring and are not serialized. The
description is version 1 and is tested by `test-dm-fmc-vmstate` with exact
round-trip and truncated-stream rejection. `dm-mc02-fmc-test` additionally
checks the direct machine MMIO boundary, including byte-lane writes and reset.
This component contract does not establish whole-machine snapshot, save/load
or migration support. External memory devices and FMC transaction timing are
not modeled by the current FMC register-window component.

# 0.17 Reusable GPIO VMState contract

`dm_mc02_gpio_vmstate()` returns the VMState description for the reusable
STM32H723 GPIO bank state. Its producer is the CPU-visible bank register
mirror and its consumers are the future machine-level migration composition
and current board routes that re-derive outputs from the restored bank:

```text
MODER, OTYPER, OSPEEDR, PUPDR, IDR, ODR, AFR0, AFR1
```

`bank_index` is profile metadata. `MemoryRegion`, the ODR-changed callback and
its opaque pointer are runtime wiring and are not serialized. After a
successful version-1 load, the destination callback is invoked once with the
destination bank metadata and restored `ODR`, so board-level LED, buzzer,
power, chip-select and transceiver projections can be rebuilt. A rejected
version or truncated stream does not invoke the callback. The component is
compiled into the STM32H723 ARM target and tested independently by
`test-dm-gpio-vmstate` (`2/2`) and at the direct board boundary by
`tools/run-gpio-smoke.sh`.

This is a component contract only. The DM-MC02 machine does not register it,
so it does not establish whole-machine snapshot, save/load or migration
support. The model does not claim complete H723 electrical contention,
analog input, pad-lock or all reset-domain behavior.

# 0.18 Reusable STM32H723 internal Flash VMState contract

`dm_mc02_flash_vmstate()` returns the version-1 VMState description for the
board-independent STM32H723 Flash register component. Its producer is the
CPU-visible Flash register mirror and unlock-sequence state:

```text
regs[DM_MC02_FLASH_REG_REGION_SIZE], key1_seen, optkey_seen
```

The caller owns the Flash storage bytes and supplies their `MemoryRegion` to
`dm_mc02_flash_init()`. The storage pointer, storage size, backing bytes,
`program_window`, `program_enabled`, and all `MemoryRegion` wiring are runtime
state and are not serialized. `dm_mc02_flash_sync_runtime()` derives the
programming overlay from the restored `FLASH_CR1.LOCK` and `FLASH_CR1.PG` bits;
the same boundary is used after a `FLASH_CR1` write. A successful load invokes
this synchronization only after all VMState fields have been restored, while
a rejected/truncated stream does not invoke it.

The public lifecycle is:

```c
void dm_mc02_flash_init(DmMc02Flash *, Object *, uint8_t *, size_t,
                        MemoryRegion *);
void dm_mc02_flash_reset(DmMc02Flash *);
void dm_mc02_flash_sync_runtime(DmMc02Flash *);
const VMStateDescription *dm_mc02_flash_vmstate(void);
```

The model keeps warm-reset Flash contents in the caller-owned storage but
resets the controller lock and disables programming. Programming is exposed
only while `CR1.LOCK=0` and `CR1.PG=1`, and applies NOR one-to-zero semantics;
timing, ECC, option-byte behavior and power-fail semantics remain outside this
component contract. The description is independently tested by
`test-dm-flash-vmstate` (`3/3`) and the direct guest boundary by
`tools/run-flash-smoke.sh`. It is not registered with the DM-MC02 machine and
therefore does not establish whole-machine snapshot, save/load or migration.

# 0.19 Reusable STM32H723 EXTI VMState contract

`dm_mc02_exti_vmstate()` returns the version-1 VMState description for the
board-independent EXTI component. Its producer is the CPU-visible register
mirror plus the sampled level of the sixteen external lines:

```text
regs[DM_MC02_EXTI_REGION_SIZE], line_level
```

The `MemoryRegion`, seven `qemu_irq` handles, and derived `irq_level[7]` are
runtime wiring and are not serialized. `line_level` is retained because it is
the input history required to distinguish a new rising or falling edge after
load. After a successful load, `dm_mc02_exti_sync_runtime()` recomputes every
group from the restored `PR1`, `C1IMR1`, and group mask, then force-drives each
connected IRQ. This also repairs a destination handle that still has a stale
level; it does not serialize or recreate the handle itself.

The public component boundary is:

```c
void dm_mc02_exti_init(DmMc02Exti *, Object *);
void dm_mc02_exti_reset(DmMc02Exti *);
void dm_mc02_exti_set_irq(DmMc02Exti *, unsigned group, qemu_irq);
void dm_mc02_exti_set_line(DmMc02Exti *, unsigned line, bool level);
void dm_mc02_exti_sync_runtime(DmMc02Exti *);
const VMStateDescription *dm_mc02_exti_vmstate(void);
```

Version errors and truncated streams are rejected before the post-load
runtime projection is invoked. The isolated `test-dm-exti-vmstate` test is
`2/2`; `tools/run-exti-smoke.sh` is the direct consumer gate for software
trigger, GPIO edge, W1C pending clear, IRQ projection and live SYSCFG remap.
This is a component contract only: the DM-MC02 machine does not register it,
so it does not establish whole-machine snapshot, save/load or migration
support. Complete H723 EXTI line/event/security-domain behavior is outside
the current model.

# 0.20 Reusable STM32H723 DMAMUX VMState contract

`dm_mc02_dmamux_vmstate()` returns the version-1 VMState description for the
board-independent DMAMUX component. Its producer is the CPU-visible DMAMUX
register mirror and the configuration generation used by the DMA request
cache:

```text
regs[DM_MC02_DMAMUX_REGION_SIZE], generation
```

`MemoryRegion` is runtime wiring and is not serialized. `generation` is not a
hardware register; it is the local cache-coherence epoch incremented on a
valid DMAMUX write and reset. Saving it preserves the boundary that lets a
DMA consumer reject a request-cache snapshot built for a different DMAMUX
configuration. The component has no callback, timer, IRQ handle or other
post-load side effect.

The public component boundary is:

```c
void dm_mc02_dmamux_init(DmMc02Dmamux *, Object *, const char *name);
void dm_mc02_dmamux_reset(DmMc02Dmamux *);
const VMStateDescription *dm_mc02_dmamux_vmstate(void);
```

The register model continues to expose bounded little-endian accesses and
increments `generation` after each accepted write; reset clears the register
mirror and increments the epoch. The isolated `test-dm-dmamux-vmstate` test
is `2/2`, and the direct DMA consumer gates are
`tools/run-dma-smoke.sh`, `tools/run-dma-arbitration-smoke.sh`, and
`tools/run-dma-batch-smoke.sh`. This is a component contract only: the
DM-MC02 machine does not register it, so it does not establish whole-machine
snapshot, save/load or migration support. DMA stream live state, FIFO and
IRQ state remain a separate contract.

# 0.21 Reusable STM32H723 DMA stream VMState contract

`dm_mc02_dma_vmstate()` returns the version-1 VMState description for the
board-independent DMA stream component. Its producer is the DMA register
window plus the private state needed to continue an enabled peripheral,
circular, double-buffered, or FIFO transfer:

```text
regs[DM_MC02_DMA_REGION_SIZE],
reload_ndtr[8], reload_par[8], reload_m0ar[8], reload_m1ar[8],
cursor_m0ar[8], cursor_m1ar[8],
fifo[8][DM_MC02_DMA_FIFO_BYTES], fifo_head[8], fifo_length[8]
```

`MemoryRegion`, `dmamux_channel_offset`, stream-enable callback/opaque,
stream IRQ handles, request-stream cache, cache generation/validity, and
derived IRQ levels are runtime wiring or derived state. They are not
serialized. After a successful load, `dm_mc02_dma_sync_runtime()` invalidates
the request cache and reprojects level-sensitive IRQ outputs through the
destination handles. A truncated or invalid stream is rejected before this
runtime synchronization; reload counts above the 16-bit NDTR capacity and
FIFO head/length states outside the four-word (16-byte) modeled capacity are
invalid.

The public component boundary is:

```c
void dm_mc02_dma_init(DmMc02Dma *, Object *, const char *name);
void dm_mc02_dma_reset(DmMc02Dma *);
void dm_mc02_dma_sync_runtime(DmMc02Dma *);
const VMStateDescription *dm_mc02_dma_vmstate(void);
```

The isolated `test-dm-dma-vmstate` test is `3/3`; the direct consumer gates
remain `tools/run-dma-smoke.sh`, `tools/run-dma-arbitration-smoke.sh`, and
`tools/run-dma-batch-smoke.sh`. This is still a component contract only: the
DM-MC02 machine does not register it, so it does not establish whole-machine
snapshot, save/load or migration support. DMA/DMAMUX joint load ordering,
external endpoint state, asynchronous bus timing, and any future timer-owned
transfer scheduler require separate contracts.

# 0.22 Reusable STM32H723 timer VMState contract

`dm_mc02_tim2_vmstate()` returns the version-1 VMState description for the
board-independent timer component shared by TIM2 and the auxiliary timer
instances. Its producer state is:

```text
regs[DM_MC02_TIM2_REGION_SIZE / sizeof(uint32_t)],
active_psc, active_arr, active_ccr[4], active_rcr,
repetition_remaining, start_ns, start_counting_down,
next_update_ns, next_compare_ns, compare_channel_mask,
cc1_active..cc4_active, ocref_level_mask,
break_input_level, break_latched, update_batch, compare_batch
```

`start_ns`, `next_update_ns` and `next_compare_ns` are monotonic virtual
nanosecond timestamps. A zero deadline means that the corresponding virtual
timer is not armed. Deadlines that have elapsed by the time a destination is
restored are re-armed at the destination's current virtual time; missed timer
callbacks are not replayed in a wall-clock loop. The post-load boundary rejects
invalid 16-bit PSC/RCR shadows, impossible repetition state, invalid
OCREF/compare masks, out-of-range timestamps, disabled timers with armed
deadlines, and empty compare masks paired with a compare deadline.

The `MemoryRegion`, `QEMUTimer` objects, `Clock`, IRQ handle and cached IRQ
validity are runtime wiring or derived state and are not serialized. Update,
compare and master-event callbacks are also runtime wiring. The static timer
capability flags (`repetition_supported`, `mms2_supported`,
`break_supported`, and `complementary_supported`) must be supplied by the
destination profile before post-load; they are not board state. The batching
policy is serialized because it changes the observable callback/deadline
behavior and is independently configurable by the caller.

The public component boundary is:

```c
void dm_mc02_tim2_sync_runtime(DmMc02Tim2 *);
bool dm_mc02_tim2_validate_vmstate(const DmMc02Tim2 *);
const VMStateDescription *dm_mc02_tim2_vmstate(void);
```

`dm_mc02_tim2_validate_vmstate()` checks the serialized phase/deadline order
and, when a compare deadline is armed, checks that every serialized channel
is an active output compare with a representable active CCR/ARR match. It is
called before `dm_mc02_tim2_sync_runtime()`; a failed check has no timer, IRQ,
or consumer-notification side effect. A successful post-load then invokes the
runtime `changed` callback once so board projections can refresh. This does
not validate the external `Clock` identity or virtual-clock epoch; those are
owned by the future machine-level composition boundary.

The isolated `test-dm-tim2-vmstate` test is `5/5`; the direct consumer gate
is `dm-mc02-tim2-test` with 22/22 TIM2/TIM8/TIM1 qtest cases. This remains a
component contract only: the DM-MC02 machine does not register it, so it does
not establish whole-machine snapshot, save/load or migration support. Exact
clock-source migration, timer/ADC joint restore ordering, and board-level
trigger consumer state require separate contracts.

# 0.23 Reusable STM32H723 ADC VMState contract

`dm_mc02_adc_vmstate()` returns the version-1 component state description for
the board-independent ADC model. Its producer state is:

```text
regs[DM_MC02_ADC_REGION_SIZE], regular/injected conversion state,
regular/injected rank progress and virtual deadlines,
calibration and regulator progress, linear calibration windows,
injected active/pending JSQR contexts, rank cursors,
legacy samples, board/external channel values and override bitmaps
```

The regular/calibration deadline is `next_sample_ns`; the injected group has
the independent `next_injected_sample_ns`. All timestamps are monotonic
virtual nanoseconds. A zero deadline means that the corresponding ADC-owned
timer is not armed, including the intentional stopped-kernel-clock case.
Remaining half-cycles/cycles are retained so a later clock update can resume
the operation without replaying wall-clock events. During component restore,
`dm_mc02_adc_sync_runtime()` treats a zero deadline plus active progress as a
paused operation: with a valid destination clock it schedules from the
remaining work and phase anchor; with a stopped destination clock it keeps the
progress paused and the deadline zero.

The `MemoryRegion`, `QEMUTimer` objects, DMA/DMAMUX pointers and endpoint
callback, IRQ handle/derived level, common-clock callback, current `clock_hz`,
and timing/power policy are runtime wiring or configuration and are not
serialized. DMA requests, trigger-bus connections and common-clock
propagation are reattached by the destination composition. Board source
values and external raw/pin-voltage overrides are serialized because they
change the next conversion result; their validity bitmaps and override kind
must agree exactly.

The public component boundary is:

```c
void dm_mc02_adc_sync_runtime(DmMc02Adc *);
const VMStateDescription *dm_mc02_adc_vmstate(void);
```

Post-load validates the register-visible enable/start/calibration state,
regular/injected rank bounds, AUTDLY/DISCEN/JDISCEN/JAUTO wait states,
injected active/pending context snapshots, calibration windows, timestamp
ordering and representability, stopped-clock progress, and source override
bitmaps before calling `dm_mc02_adc_sync_runtime()`. A rejected or truncated
stream has no timer or IRQ side effect. A successful load only rebuilds
ADC-owned regulator/sample timers and the level-sensitive IRQ projection; it
does not invoke the common-clock callback or reconstruct DMA/trigger wiring.

The isolated `test-dm-adc-vmstate` test is `3/3`, covering exact dynamic
round-trip, inconsistent active-state rejection and truncated-stream
rejection. `test-dm-adc-runtime-sync` covers regular, injected and calibration
paused-progress restore, including exact virtual deadlines. The direct
consumer gate is `dm-mc02-adc-test` with `40/40` qtest
cases, complemented by the ADC analog/input/IRQ/DMA/power/trigger and
JAUTO-DMA bare-metal smokes. This remains a component contract only: the
DM-MC02 machine does not register it, so it does not establish whole-machine
snapshot, save/load or migration support. CDR2, other multimode formats, CDR
read side effects, analog electrical effects and machine-level ADC/DMA/timer
restore ordering remain outside this ADC component contract. The ADC12
common/DMA boundary is defined separately below.

# 0.9 Reusable USART RX wire queue and IDLE virtual-time boundary

`DmMc02Uart` exposes a board-independent receive pipeline with two bounded
queues. Host chardev bytes are the producer and enter the 256-byte
`rx_wire_fifo`; with a valid result from `dm_uart_timing_calculate()`, the
`QEMU_CLOCK_VIRTUAL` RX timer delivers one byte per default 8N1 frame into the
256-byte CPU-visible `rx_fifo`. `RDR` polling and the optional DMA RX endpoint
share `uart_rx_pop()`, so neither consumer can observe a byte before its wire
frame has elapsed. The advertised chardev capacity is the sum of both queues.

When the baud result is invalid, the wire queue is drained immediately to
preserve the pre-timing compatibility behavior. Queue overflow drops the
newest byte and increments the existing RX drop counter. `RE` and the board's
RS485 power gate remain the admission boundary; disabled input is not accepted.

After the last byte is delivered from wire to RX FIFO, the one-shot IDLE timer
waits one more frame duration before setting `USART_ISR.IDLE`. A host chardev
chunk is not an idle boundary. New accepted input cancels the pending IDLE
deadline; `IDLE` remains latched until `USART_ICR.IDLECF`, and its IRQ output
uses the existing `CR1.IDLEIE` gate. Recalculation caused by kernel-clock or
baud-register changes preserves an already pending IDLE deadline with the new
frame duration. Reset, chardev close, and transceiver power-off clear both RX
queues and cancel both RX timers.

The direct virtual-time gate is
`tools/run-uart-rx-timing-smoke.sh`. It uses qtest `clock_step` with a 64 MHz
USART1 clock and `BRR=6400` to assert the exact `frame-1/frame` boundaries,
continuous multi-byte delivery, and post-frame IDLE. Existing UART polling,
IDLE, and UART1/UART2 DMA endpoint tests remain the immediate consumer gates.
This boundary still does not define parity/stop-bit configuration,
oversampling, LIN/Smartcard/IrDA, overrun/error flags, or physical line
levels.

# 0.8 Reusable USART BRR/PRESC virtual-time TX boundary

`qemu/upstream/include/hw/char/dm_uart_timing.h` contains the board-independent
`dm_uart_timing_calculate()` helper. It accepts the cached USART kernel clock,
raw `BRR`, raw `PRESC` code, and `CR1.OVER8`; it does not read RCC registers,
QEMU clocks, board profiles, or chardev state. The result reports validity,
prescaler, prescaled clock, effective divider, exact integer baud ratio, and
the default 8N1 frame duration rounded up to virtual nanoseconds.

The helper maps `PRESC` codes `0..11` to
`/1,/2,/4,/6,/8,/10,/12,/16,/32,/64,/128,/256`. With `OVER8=0`, the divider
is `BRR`; with `OVER8=1`, `BRR bit 3` must be zero and the effective divider is
`(BRR & 0xfff0) | ((BRR & 0x000f) << 1)`. Zero clocks, reserved prescalers,
out-of-range BRR values, and dividers below 16 are invalid. Calculations use
`__uint128_t` and do not use floating point or wall-clock sleeps.

`DmMc02Uart` consumes only the helper result. It recalculates timing after a
kernel-clock callback or `BRR/PRESC/CR1.OVER8` write. A valid configuration
starts one virtual timer per queued byte, while chardev short writes retry
without blocking the emulated CPU; `TC` is asserted only after the TX queue is
empty. During reset or before a valid baud configuration, the existing immediate
compatibility path remains active. RX uses the separate wire-queue and
frame/IDLE boundary documented in section 0.9; it is not a bit-level sampling
model.

The isolated gate is `dm_uart_timing_smoke`; the direct QEMU gate is
`tools/run-uart-timing-smoke.sh`, which writes USART1 registers through qtest
and advances `QEMU_CLOCK_VIRTUAL` by exact nanoseconds. This boundary proves
TX pacing and `TC/TXE` settling, but does not yet model parity/stop-bit choices,
LIN/Smartcard/IrDA modes, RX sampling, physical line levels, or oscillator
settling.

# 0.4 STM32H723 RCC source readiness boundary

`DmMc02PwrRcc` keeps the guest-requested `RCC_CFGR.SW` and the effective system
clock source as separate state. `SW` is the requested source (`HSI=0`, `CSI=1`,
`HSE=2`, `PLL1=3`); read-only `SWS` reports the source currently feeding the
system clock using the same encoding. A request for an unsupported encoding or
an unready source is retained in `SW` but does not change `SWS` or the clock
callback value.

The modeled readiness boundary is immediate but not unconditional: HSI/CSI/HSE
require their `*ON` bit, while PLL1/2/3 require their `*ON` bit, a ready PLL
input oscillator, non-zero M/P (or R) divisors, and a valid configured output.
When a source becomes ready, the RCC write path retries the saved `SW` request;
there is no need for firmware to rewrite `CFGR`. A source becoming disabled does
not silently select a fallback source; the effective source identity remains
visible in `SWS` and its derived rate becomes zero until a valid requested source
is selected.

The RCC callback is the boundary to direct clock consumers. It carries the
effective CPU frequency; the DM-MC02 board consumer derives timer and ADC rates
from the RCC model. Source/readiness parsing is confined to RCC register writes
and reads, not timer/ADC runtime paths. The current model intentionally does not
cover oscillator startup time, PLL lock time, CSS, low-power clock switching,
backup-domain mux behavior, or the complete H723 kernel-clock matrix.

# 0.5 STM32H7 D1 CPU/HCLK divider boundary

`qemu/upstream/hw/arm/dm_stm32h7_clock_tree.[ch]` is a board-independent,
QEMU-independent helper boundary for the STM32H7 D1 prescalers. It accepts the
effective SYSCLK and raw `RCC_D1CFGR` value, then exposes:

```c
uint64_t dm_stm32h7_cpu_clock_hz(uint64_t sysclk_hz, uint32_t d1cfgr);
uint64_t dm_stm32h7_hclk_hz(uint64_t sysclk_hz, uint32_t d1cfgr);
```

`D1CPRE` (bits 11:8) is applied only to the CPU clock. `HPRE` (bits 3:0) uses
the H723 encoding `/1` for 0..7, then `/2`, `/4`, `/8`, `/16`, `/64`,
`/128`, `/256`, and `/512` for 8..15. The helper does not select SYSCLK,
check oscillator/PLL readiness, model APB clocks, or own any board policy.

`DmMc02PwrRcc` owns the producer-side source/readiness state and exposes
`dm_mc02_pwr_rcc_hclk_hz()` as the RCC-to-consumer boundary. DM-MC02 ADC common
`CCR.CKMODE=01/10/11` consumes `HCLK/{1,2,4}` respectively. The common ADC
callback remains responsible for the ADC kernel-source (`CKMODE=00`) path and
for applying `CCR.PRESC`; ADC does not read RCC registers or the helper's
private state.

The pure helper is tested by `dm_stm32h7_clock_tree_smoke`. The direct consumer
is tested by `/dm-mc02/adc/common-clock-hpre-modes` and
`/dm-mc02/adc/common-clock-hpre-live-change` with QEMU virtual time. This
boundary intentionally does not define APB/timer peripheral clocks, clock
settling latency, CSS, low-power switching, cache effects, or electrical timing.

# 2026-09-01 H723 USB host-channel multi-packet PIO/DMA contract

`DmStm32H7OtgHostChannel` owns the controller-side continuation state. The
producer is the guest programming `HCCHAR`, `HCTSIZ`, `HCDMA`, and `HCFIFO`
and reporting an accepted packet; the boundary is the channel register/data
path contract; the consumer is a board-independent host-channel transport.

On a rising `HCCHAR.CHENA` edge, `HCTSIZ.DPID` seeds the private
`next_pid`. Each accepted packet submits the current `next_pid`, then toggles
only that private state between `DATA0` and `DATA1`. `HCTSIZ.DPID` remains the
initial programmed value and is not rewritten as continuation state. A NAK,
STALL, or transaction error does not advance the toggle; an accepted short
packet stops the channel after committing the accepted length.

For PIO, the consumer reads OUT bytes from the channel `HCFIFO` stream and
writes IN bytes to it. For DMA, the consumer uses the current byte-address
`HCDMA` through the injected memory callback when `GAHBCFG.DMAEN` is set.
Accepted length is the sole producer of `HCTSIZ.XFERSIZE`/`PKTCNT` progress;
DMA address progress follows the same accepted length, while PIO FIFO state is
not advanced by the DMA path. The submit callback owns its transport fixture
context, and PIO callbacks own the host context; these opaque owners must not
be conflated.

The focused tests cover PIO and DMA `64+64+2` OUT continuation, accepted short
packet stop, PID and register progression, HCDMA/FIFO contents, and terminal
`HCINT` state. Existing verification records are host-controller `16/16`,
host transport `14/14`, data-path `3/3`, channel-control `4/4`, completion
scheduler `6/6`, QEMU adapter `13/13`, and H723 qtest `4/4`.

This contract does not define USB bus/PHY timing, SOF retry/timeout policy,
global FIFO arbitration, descriptor DMA, ISO/split transactions,
hub/topology, passthrough, or a DM-MC02 host-role composition. The next
interface gate is the H723 retry/SOF and DMA/FIFO boundary; only after that
gate may a host-role board profile consume this channel contract.

## 2026-09-01 H723 host SOF scheduling boundary

`DmStm32H7OtgHost` advances `HFNUM` on its configured virtual SOF interval. The
SOF-driven service path gates ISO and interrupt channels (`HCCHAR.EPTYPE=1/3`)
with `HCCHAR.ODDFRM`: a set bit selects odd `HFNUM` frames and a clear bit
selects even frames. Control and bulk channels are serviced on every eligible
SOF, preserving their existing NAK retry behavior. A channel enabled by a
`HCCHAR.CHENA` rising edge and an explicit `dm_stm32h7_otg_host_service_channel()`
call remain caller-driven immediate operations and do not acquire an implicit
SOF parity gate.

The host controller does not automatically retry a periodic NAK. A periodic
NAK sets `HCINT.NAK|CHHLTD` and leaves PID, `HCTSIZ`, and DMA progress
unchanged; software must re-enable the channel. This matches the current
reference DWC2 host behavior used as the boundary oracle. Per-endpoint polling
interval, complete high-speed microframe bandwidth scheduling, global FIFO
arbitration, split/ISO payload rules, descriptor DMA, PHY, and electrical timing
remain outside this small reusable contract.

# DM-MC02 仿真接口设计

## 0.1 Shared v2 wire framing codec

`cosim/dm_mc02_v2_wire.[ch]` 是板卡无关、transport 无关的 v2 framing boundary。
它可以被 QEMU link、host worker 和其它 board profile 复用，不拥有 socket/chardev
队列、虚拟时钟、session 生命周期或 typed payload policy。

完整 stream frame 的布局如下：

```text
u32 outer_body_len       // transport owns this prefix
u32 magic = DM02
u16 version = 2
u16 kind
u32 payload_len          // exact bytes after the 36-byte header
u64 step_id
u64 t_sim_ns
u64 dt_ns
u32 session_id
u8  payload[payload_len]
```

`dm_mc02_v2_wire_encode()`/`decode()` 只处理 `magic` 后的 36-byte header 和 payload
（调用缓冲区从 `magic` 开始时，outer prefix 仍需由 transport 预留/发送），接受的
payload 最大为 512 bytes，并使用 `dm_mc02_wire.[ch]` 的 little-endian helpers。encode
返回 body 长度；decode 要求输入长度等于 `36 + payload_len`，拒绝未知 kind、版本、
超限或截断 frame。outer `body_len` 不计入 `payload_len`，不能用一个字段替代另一个。

STEP payload 有两种结构：兼容的 60-byte compact `ImuSampleV2`，或
`u16 section_count, u16 marker/reserved` 后跟重复的
`u16 type, u16 flags, u32 length, u8 payload[length]`。`dm_mc02_v2_wire_step_payload_begin()`
和 `dm_mc02_v2_wire_next_section()` 只负责 section 结构边界；`flags` 必须为零，section
payload 语义、session/step 顺序和 ACK 状态由 QEMU/Python consumer 负责。显式 marker
用于处理 60-byte section payload 与 compact IMU 的歧义；若 compact payload 的判别字段
与 section marker 冲突，底层拒绝该模糊输入，不能静默把它当成 section payload。

隔离 fixture 位于 `tests/v2_wire_vectors.txt`，由 `tests/v2_wire_vectors.c` 和
`tests/test_v2_wire_vectors.py` 共同消费。该 parity 门保证 C/Python 对 reset、session
reset、compact IMU、section IMU 和混合 ADC sections 的 decode/re-encode 结果保持字节
一致；它不声明 typed RESET_ACK、STEP_ACK、TELEMETRY 或 MOTOR_STATE schema 已由底层
codec 统一。

## 0.2 Shared v2 fixed payload codec

`cosim/dm_mc02_v2_payload.[ch]` 负责五类固定 payload 的公共布局。producer 提供结构化
状态，codec 负责 little-endian 序列化和协议约束，consumer 负责把 payload 放入 v2 frame
并维护 session/step 生命周期。codec 不访问 QEMU、transport、board profile 或 plant。

```c
typedef struct DmMc02V2ResetAckPayload {
    uint32_t status;
    uint32_t capabilities;
    uint64_t virtual_time_ns;
    uint32_t queue_depth;
} DmMc02V2ResetAckPayload;

typedef struct DmMc02V2StepAckPayload {
    uint32_t status;
    uint32_t queue_depth;
    uint32_t dropped_count;
} DmMc02V2StepAckPayload;
```

对应的 `Diagnostics` 为五个 `u64`，`StepDone` 为
`status:u32, consumed_mask:u32, missing_mask:u32, queue_depth:u32, dropped_count:u32`，
board telemetry 为六个 `u32`：`led_rgb, led_brightness, buzzer, board_flags,
buzzer_frequency_hz, buzzer_duty_permille`。编码器自动写入协议保留字段；解码器要求
精确长度，并拒绝未知 status/capability、非法 consumed/missing mask 或非零 reserved。
成功的 `StepDone` 必须同时消费 accel 与 gyro；非成功状态可报告缺失 mask。

公共接口形如 `dm_mc02_v2_payload_encode_*()`、`decode_*()` 和
`validate_*()`。encode 返回固定 payload 长度，参数或能力位不合法时返回 0；decode
先执行同一验证再填充结构。固定响应的向量位于 `tests/v2_payload_vectors.txt`，由 C
和 Python 测试共同消费。该层不覆盖 IMU/MotorCommand/MotorState 的变长 section schema，
也不替代 v2 session validator。

## 0.3 Shared v2 ADC section payload

`DmMc02V2AdcInputPayload` 的 wire layout 是 `channel:u16, raw:u16, reserved:u32`，
固定长度 8 bytes。`DmMc02V2AdcVoltagePayload` 的 layout 是
`channel:u16, flags:u16, voltage_uv:u32, reserved:u32`，固定长度 12 bytes。公共 codec
接受 channel `0..31`；voltage flags 目前只允许
`DM_MC02_V2_ADC_VOLTAGE_FLAG_PIN_OVERRIDE`，电压上限为 `3300000 uV`，两个 reserved
字段必须为零。

`dm_mc02_v2_payload_encode_adc_input()`、`encode_adc_voltage()` 写入规范化 reserved
字段；对应 decode/validate 在填充结构前执行同一边界检查。QEMU link 只消费解码后的
channel/raw 或 channel/flags/voltage，并继续负责虚拟时间排队、队列溢出和板级 ADC 回调。
该接口不负责 ADC trigger source、sample cadence、校准或 analog power semantics。

## 0. STM32H723 trigger bus

`DmMc02TriggerBus` 是芯片层 timer、ADC 和其它触发源之间的通用同步事件接口，
不依赖 DM-MC02 board profile、ADC 类型或 timer 类型。接口为固定容量注册表：
初始化阶段最多连接 8 个 sink；连接成功后按注册顺序同步调用所有 sink，不排队、
不丢弃、不分配内存。sink 只在回调期间借用 event 指针，不能保存它；当前 QEMU
事件循环是单线程，因此 sink 在回调中直接更新本 peripheral 的状态。

```c
typedef struct DmMc02TriggerEvent {
    uint32_t source_id;
    bool rising;
    unsigned event_count;
    uint64_t timestamp_ns;
} DmMc02TriggerEvent;

void dm_mc02_trigger_bus_publish(DmMc02TriggerBus *bus,
                                 uint32_t source_id, bool rising,
                                 uint64_t timestamp_ns);
void dm_mc02_trigger_bus_publish_batch(DmMc02TriggerBus *bus,
                                       uint32_t source_id, bool rising,
                                       unsigned event_count,
                                       uint64_t timestamp_ns);
```

`event_count` 大于 1 时表示连续边沿在同步接口上被合并；sink 若必须观察每个边沿，
应使用不做批处理的 timer 配置。ADC sink 将一个批次视为一次启动机会：如果 ADC 空闲，
最多启动一个 regular/injected sequence；如果对应 group 正在转换，整个批次被忽略，
不会伪造 ADC 触发 FIFO、也不会把被压缩的边沿重新展开。`bool` 返回值只表示该调用是否
接受了一个启动，不表示批次中的每个边沿都被消费。

当前 H723 ADC source 映射只接入以下六个 timer update source。trigger bus 的
`source_id` 使用 regular group 的 `EXTSEL` 编码作为稳定的物理 source identifier；
ADC sink 会在 injected group 单独转换为 H723 的 `JEXTSEL` 编码，调用方不能把
regular 与 injected 的 mux 数字直接互换：

| regular `EXTSEL` / bus source | injected `JEXTSEL` | source | 发布者 | 当前消费者 |
|---:|---:|---|---|---|
| 4 | 12 | `TIM3_TRGO` | board timer route callback | ADC1、ADC2 |
| 7 | 9 | `TIM8_TRGO` | board timer route callback | ADC1、ADC2 |
| 8 | 10 | `TIM8_TRGO2` | board timer route callback | ADC1、ADC2 |
| 9 | 0 | `TIM1_TRGO` | board timer route callback | ADC1、ADC2 |
| 10 | 8 | `TIM1_TRGO2` | board timer route callback | ADC1、ADC2 |
| 11 | 2 | `TIM2_TRGO` | board timer route callback | ADC1、ADC2 |
| 15 | 4 | `TIM3_CH4` | TIM3 `OC4REF` event-specific route | ADC1、ADC2 |

### 0.0.1 通用 timer master route

timer 的 master 输出由 board profile 的 route 数据描述，而不是在 ADC 或 timer 芯片
模型中硬编码开发板连接。每个 timer route 包含 timer instance 的 base/IRQ 以及
独立的 `trgo_source_id`、`trgo2_source_id`；未接出的输出使用无效 source。对于需要区分
同一 master output 上不同事件的 profile，可选地设置
`master_event_source_id[2][DM_MC02_BOARD_TIMER_EVENT_KIND_COUNT]` 和对应的
`master_event_source_valid[2]` 位图。valid 位清零时沿用该 output 的旧
`trgo_source_id`/`trgo2_source_id`，因此旧 profile 不需要初始化新表。timer 芯片层
只产生 `TRGO`/`TRGO2` 抽象事件，板级组合层负责按 route 发布到 trigger bus，ADC sink
仍只按 source ID 和自身的 `EXTSEL`/`JEXTSEL` 配置过滤事件。

DM-MC02 和 STM32H723-EVAL profile 已将 TIM1/TIM2/TIM3/TIM8 route 接入该接口；TIM12/TIM24
的未接出 master output 使用 `DM_MC02_TRIGGER_SOURCE_NONE`。各 timer 的稳定 bus source ID
使用 regular mux 编码，对应 ADC regular `EXTSEL`；同一物理源用于 injected group 时，ADC
芯片层映射为独立的 `JEXTSEL` 编码：

| route output | trigger bus / regular `EXTSEL` | injected `JEXTSEL` | 状态 |
|---|---:|---:|---|
| `TIM2_TRGO` | 11 | 2 | 已实现并通过 ADC qtest |
| `TIM3_TRGO` | 4 | 12 | 已实现并通过 ADC qtest/smoke |
| `TIM3_CH4` (`OC4REF`) | 15 | 4 | 已实现并通过 ADC qtest/smoke |
| `TIM8_TRGO` | 7 | 9 | 已实现并通过 ADC qtest |
| `TIM8_TRGO2` | 8 | 10 | 已实现并通过 ADC qtest |
| `TIM1_TRGO` | 9 | 0 | 已实现并通过 ADC qtest |
| `TIM1_TRGO2` | 10 | 8 | 已实现并通过 ADC qtest |

该 route wiring、芯片层 mux 分支和边界测试已完成；qtest 同时确认 ADC 选择其它 source
时不会响应未选择的 timer 事件。该接口保持 timer、ADC 和 board profile 之间的解耦，未来
可复用于其它 timer 或其它 board profile。当前仅 DM-MC02/EVAL 的 TIM3 `TRGO` output
配置了 `OC4REF -> TIM3_CH4` event-specific route；没有 valid 位的其它事件仍按旧
update-source fallback，未接入的 H723 source 不因其保留 mux 编码而自动可用。

ADC sink 使用 `EXTEN` 解释上升沿、下降沿和双边沿，并继续检查 `ADEN`、`ADSTART`
及当前转换状态。TIM8 的 WS2812 DMA request 由独立的板级 compare callback 处理；
trigger bus 不知道 DMA 或板级 pin。旧 `dm_mc02_adc_external_trigger()` 作为兼容
包装保留，其时间戳取当前 QEMU virtual clock。

ADC regular `CFGR.AUTDLY` 已在芯片层实现为有界数据消费状态：当一项 regular 结果的
`EOC` 仍置位时暂停下一 rank/sequence 的 virtual timer；guest 读取 `ADC_DR` 后恢复。
DMA 对 `ADC_DR` 的同步读取会立即释放该等待，符合 H723 对“自动等待”不建议与普通 DMA
长期组合使用的边界。默认 `AUTDLY=0` 不改变原有转换 cadence；该状态不扩展为一般
external-trigger queue。

Regular discontinuous mode 的 `CFGR.DISCEN/DISCNUM` 也在芯片层实现：`DISCNUM` 编码
为每个匹配外部边沿最多推进 `1..8` 个 rank；subgroup 未到 sequence 尾部时保留
`current_rank` 和 `ADSTART`，停止 virtual timer，下一匹配边沿继续。最后一个 subgroup
才置 `EOS` 并清 `ADSTART`。软件启动（`EXTEN=0`）仍执行完整 sequence，`CONT` 与
`DISCEN` 的非法组合按连续模式处理；忙时边沿不排队。`AUTDLY` 与 discontinuous
边界组合时，`ADC_DR` 读取先释放当前结果，再等待下一外部边沿。该接口不依赖 board
route，未来可复用于其它 H723 board profile。

Injected discontinuous mode 的 `CFGR.JDISCEN` 也在芯片层实现：H723 的 injected
序列每个匹配外部边沿只推进一个 rank，subgroup 间保留 `current_injected_rank` 和
`JADSTART`，最后一个 rank 才置 `JEOS` 并清除 `JADSTART`。单 rank 序列忽略该位，
不复用 regular 的 `DISCNUM`；忙时边沿不排队。

Injected context queue 由 ADC 芯片层以固定容量实现，不依赖 board route：queue-enabled
模式保留一个 active `JSQR` 快照和一个 FIFO pending 快照；完整 `JSQR` 写入是 context
admission 边界，partial write 在队列模式下被拒绝，不会形成隐式 context。活动转换只
读取 active 快照，因此 guest 后续写入不会改变正在执行的 rank。第三个待入队 context
不会覆盖已有内容，只置位 `ISR.JQOVF`；`IER.JQOVFIE` 仅控制共享 ADC IRQ level，
`ISR` 仍按 W1C 清除该原因。

`JQM=0` 在 pending 为空时保留最后 active context；当前转换结束仍清除 `JADSTART`，
需要新的 start/trigger。`JQM=1` 在没有 pending context 的 sequence 完成后清空
active context 和 CPU-visible `JSQR`，保留 `JADSTART` 的外部 arm，直到新的完整
`JSQR` 写入恢复 context。`JQDIS=1` 选择单 context 直接替换语义，并是软件 `JADSTART`
的必要配置；stateful 单 context 测试必须显式设置该位。该模型的容量和 admission
策略是当前可复用芯片接口，不宣称任意深度硬件 FIFO。

`CFGR.JAUTO` 是 regular-to-injected 的芯片层同步边界。regular sequence 的 producer
在最后一个 rank 完成时提交 EOS；当 `JAUTO=1`、injected context 有效、`JSQR.JEXTEN=0`
且 `DISCEN/JDISCEN` 均为 0 时，ADC 自动消费该 active context，置位 `CR.JADSTART` 并
启动 injected virtual conversion。`JADSTART` 的自动置位不会经过 board route，也不
伪造外部 trigger event；injected result/status 是该边界的 consumer。

非连续 regular start 只产生一次自动 injected sequence。连续 regular start 在 injected
sequence 完成前暂停；完成后清除内部等待状态并按当前 virtual timestamp 恢复下一组。
若 guest 写 `JADSTP` 中止自动 injected，ADC 立即停止 injected timer、清除 `JADSTART`
并释放连续 regular 等待；若 `AUTDLY` 仍保持 regular `EOC`，恢复仍由 guest 读取
`ADC_DR` 驱动。JAUTO 下直接写 `JADSTART` 被忽略，避免把自动状态扩展为软件队列。

当前接口明确不覆盖 `JAUTO` 与 `DISCEN/JDISCEN` 的非法组合、低功耗自动断电，以及
完整 injected 外部 trigger source matrix；`JQM` 的 context admission 可复用。
`JAUTO+JQM+regular circular DMA` 的成功组合已通过
`tools/run-adc-jauto-dma-smoke.sh` 的 guest/QEMU 边界回归：regular EOS 自动启动
injected sequence，`JQM=1` 在无 pending context 时清空 `JSQR`，regular DMA 随后
继续运行并完成 HT/TC。该回归覆盖 regular/injected 各两个 rank 的成功路径，
不扩大为完整 injected queue 或 trigger matrix 支持。

通用 timer 芯片层通过独立的 master-event callback 解码 `CR2.MMS`；TIM8 额外显式
打开 H723 TIM1/TIM8 的 `MMS2` 能力，板级回调再把 `TRGO/TRGO2` 映射为 profile
中的数据源编号。当前已实现 `MMS/MMS2=0` 的 `EGR.UG` reset、`=1` 的 `CEN` enable、`=2`
的 update（包括 UG 产生的 update）、`=3` 的 CC1 compare pulse，以及 edge-aligned
up-counting PWM1/PWM2 下 `=4..7` 的 `OC1REF..OC4REF` 边沿事件。`MMS=3` 只由 CC1
命中发布 compare pulse；同一 compare scheduler 中的 CC2..CC4 命中不会伪装成 CC1。
普通 compare 匹配按虚拟时间计算，芯片层支持 CC1..CC4 的 `SR.CCxIF` 锁存、独立
`CCxIE` IRQ 门控和 `SR` 写零清除；输入捕获 `CCxS!=0` 不进入该 compare 路径。其它
timer 即使复用同一 C 实现也不会自动获得 `MMS2`。

timer 的 CPU-visible `PSC/ARR/CCR1..4` 保留 shadow 值，虚拟计数和 compare deadline
使用 active 值。`PSC` 在运行中写入后于下一个 update/UG transfer，`ARR` 由 `CR1.ARPE`
选择立即或 update transfer，`CCR` 由对应 `OCxPE` 选择立即或 update transfer；停机时
无 preload 的配置可直接成为 active 值。`EGR.UG` 会在 update callback 和 master event
之前完成传输，active 周期变化会在该 update 重新锚定 CNT。读取寄存器仍返回 guest
写入的 shadow 值。

`OCxREF` 事件只表达芯片内部参考波形，忽略 `CCxE` pin output gate；当前支持 PWM1、
PWM2、forced inactive/active，以及 edge-aligned up-counting 下的 frozen、
active-on-match、inactive-on-match、toggle-on-match 最小状态语义。PWM1/PWM2 也支持
中心对齐的 `0..ARR..0` 三角计数。事件只在实际电平变化时发布，并复用已有
`DmMc02TimMasterEvent` 的
`kind/rising/event_count/timestamp_ns`。没有为每个输出边沿创建 host timer。

中心对齐模式使用 `2*ARR` 个 prescaled timer tick 作为完整 counter cycle；`CNT` 读回
按该三角 phase 惰性计算，update deadline 位于下数回到零的边界。`CMS=01/10/11`
分别将 compare flag/DMA match 限制为下数、上数或双向；master OCREF route 不受该
compare flag 过滤影响，因此仍能观察内部参考波形的两类边沿。`ARR=0` 的中心对齐
退化为静态零计数，不产生伪造的高频 compare 事件。

stateful OCREF 模式不能使用 `event_count > 1` 的无方向批处理：active/inactive 的
首个匹配可能是唯一一次电平变化，toggle 的积压边沿还会交替。因此 timer 在宿主回调
晚到时沿原始 virtual deadline 逐事件重放，所有下游事件的 `event_count` 保持为 1，
`timestamp_ns` 是该单个事件的计划时间。该恢复路径的成本与积压事件数线性相关；普通
PWM 和无状态 compare 仍可走批处理。中心对齐已实现，但组合/边沿模式及高级 OCxM
扩展仍 pending；其它 `EXTSEL` source、EXTI/HRTIM/LPTIM 触发和硬件级跨总线边沿延迟也仍
pending，不能把当前接口当作完整 H723 触发矩阵。

高级定时器的 `BDTR` 输出级由通用 timer 通过 capability 显式启用；当前 DM-MC02
profile 只为 `TIM1/TIM8` 打开该 capability，普通 `TIM2/TIM3/TIM12/TIM24` 对
`BDTR` 保持保留区行为。已实现 `DTG`/`OSSI`/`OSSR` 的寄存器保留以及
`BKE/BKP/AOE/MOE` 的最小语义：`MOE` 复位为 0，`get_pwm_state()` 只有在 `CEN`、
`CCxE`、PWM 模式、`MOE` 都有效且 Break 未激活时才报告外部主 PWM 输出有效。
`OCxREF`、compare flag、DMA request 和 TRGO/TRGO2 是内部信号，不被 `MOE` 或
Break 门控。`get_pwm_output_state()` 额外返回 CH1..CH3 的互补使能/电平和按 `DTG`
及 `CKD` 换算的死区 ticks/cycles/ns；门控观察按当前虚拟相位计算，不创建每个 PWM
边沿的宿主定时器。forced inactive/active 为恒定 OCREF，已支持；H723 `OCxM[3]`
位按高级 timer capability 解码，未实现的扩展 OCxM 明确报告 `unsupported`。

芯片层公共输入接口为：

```c
void dm_mc02_tim2_set_break_supported(DmMc02Tim2 *state, bool supported);
void dm_mc02_tim2_set_break_input(DmMc02Tim2 *state, bool level);
void dm_mc02_tim2_set_complementary_supported(DmMc02Tim2 *state, bool supported);
```

`level` 是物理 `BKIN` 电平，`BDTR.BKP` 选择高/低有效。Break 激活时硬件清除
`MOE` 并记住一次 fault；输入释放后，若 `AOE=1`，下一个 qualified update 才
恢复 `MOE`。`OCxM` 的高位只由 profile capability 启用；普通 timer 不解释其
保留位。当前没有实现第二 Break 输入、`LOCK` 写保护、完整 off-state 或逐边沿
事件队列，因此该接口不宣称完整的高级定时器输出级或电机安全时序。

## 0.1 STM32H723 ADC calibration data

ADC 芯片层把 regular 与 injected conversion 都交给同一个纯转换函数。channel 0..19
由 `DIFSEL[i]` 选择 `CALFACT_S[10:0]` 或 `CALFACT_D[10:0]`；offset 以无符号
16-bit 结果的减法应用并在 `0..0xffff` 饱和。`ADC_DR` 和 `JDR1..4` 因此共享完全
一致的校准边界，ADC DMA 读取的是已经校准后的 `ADC_DR`。

H723 的 160-bit `LINCALFACT` 通过六个 `LINCALRDYW1..6` 窗口映射到
`ADC_CALFACT2[29:0]`。清除 ready bit 选择对应保存 word 供读取，ready bit 从 0
置 1 提交当前窗口的写入；word 6 只有 `[9:0]` 有效。该窗口状态属于 ADC 芯片对象，
不依赖 DM-MC02 board profile。

当前 ADC 没有 SAR 电容级模拟核心，因此数据变换将 word 1 解释为 signed Q0.30
增益 delta（零值为恒等变换），word 2..6 只保留并按真实窗口协议读写。这个近似是
明确的替换边界，不应被用于声称真实线性误差统计或 bit-level ADC fidelity；以后接入
更高保真的模拟核心时，guest-facing register/API 可以保持不变。

## 0.2 STM32H723 DMA endpoint/FIFO/DBM

DMA 的外设数据面通过板卡无关的 `DmMc02DmaEndpoint` 解耦。DMAMUX request 和
`PAR` 仍由 DMA 芯片层匹配；匹配后每个 peripheral-width beat 同步调用 endpoint，
P2M 使用 `read(opaque, data, size, timestamp_ns)`，M2P 使用
`write(opaque, data, size, timestamp_ns)`。endpoint 只能观察或提供该 beat，不能访问
DMA 私有 cursor、`NDTR` 或板级 pin policy；DMA 继续拥有地址、计数、HT/TC、错误和
IRQ 状态。

```c
typedef struct DmMc02DmaEndpoint {
    DmMc02DmaEndpointRead *read;
    DmMc02DmaEndpointWrite *write;
    void *opaque;
} DmMc02DmaEndpoint;

bool dm_mc02_dma_request_endpoint(
    DmMc02Dma *state, const DmMc02Dmamux *dmamux, uint32_t request_id,
    hwaddr peripheral_addr, const DmMc02DmaEndpoint *endpoint,
    uint64_t timestamp_ns);
```

当 `SxFCR.DMDIS=1` 时，当前可复用同步 FIFO 使用 16-byte（四个 32-bit word）字节
存储；`FTH=0..3` 对应 1/4、1/2、3/4 和 full threshold，读取 `FS` 返回按字节占用
计算的空/1/4/1/2/3/4/full 档。M2P 先按 memory width 预取到 FIFO，再按 peripheral
width 交付 endpoint；P2M 先把 endpoint 的 peripheral-width 数据暂存，达到 threshold
或传输结束时按 memory width 写入 guest memory，末尾不完整 memory beat 以零填充。
这只表达确定性的 packing/unpacking 和暂存边界，不表达总线周期或 burst 占用。

`DBM=1` 的 peripheral request 使用 M0/M1 配置基址、私有 current cursor 和 `CT` 交替
状态；每个 buffer 完成后重装 `NDTR`，清空当前 FIFO，再切换到另一个 buffer。`M0AR`/
`M1AR` 的 guest 读回值始终是配置基址，不能用作当前 cursor。`HTIF`/`TCIF` 锁存状态，
`HTIE`/`TCIE` 只门控 IRQ；DBM 在当前模型中隐含 circular reload。

在 P2M FIFO 路径中，DMA 会在读取 peripheral MMIO 或调用 endpoint 之前检查当前 FIFO
能否容纳完整的 `PSIZE` beat；空间不足时锁存 `FEIF`、清空 FIFO 并清除 `EN`，不消费
外设数据，也不推进该 beat 的 `NDTR`、地址或 guest memory。`FEIE` 只门控 FEIF 的 IRQ
投影，不影响状态锁存。其它 endpoint 返回错误、地址空间访问失败或宽度非法仍走 fatal
DMA error path；endpoint 已经接受后的外部副作用不提供回滚。
`dm_mc02_dma_request_endpoint_batch()` 仍逐 beat 执行上述状态机，但只在 batch 边界
更新 IRQ，调用者提供的 timestamp 会传给该批次的每个 endpoint beat。

当前接口已由 `tests/dma_fifo_dbm_endpoint_smoke.c` 覆盖 M2P/P2M、16-bit 到 8-bit
packing、FIFO threshold/FS、DBM M0/M1 切换、`NDTR/CT/M0AR/M1AR`、HT/TC、endpoint
字节和 timestamp。`SxCR.PBURST[22:21]` 与 `SxCR.MBURST[24:23]` 使用
`SINGLE/INCR4/INCR8/INCR16` 编码；在 `M2P+FIFO` 下，`MBURST` 限制一次 FIFO fill
中每个连续 memory burst 的 beat 数，FIFO 容量可以截断一个更大的 burst。`PBURST`
不会扩大 endpoint callback：每个 peripheral request 仍按 `PSIZE` 交付一个逻辑 beat，
因此 UART `TDR` 始终逐字节产生 side effect。`NDTR`、地址推进、HT/TC 和 DBM 边界
仍按逻辑 beat 更新。尚未实现真实 FEIF 触发条件、时钟级总线仲裁、
异步 backpressure、per-beat rollback、M2M DBM 和完整错误恢复；板级消费者必须继续
通过各自的窄 boundary 接入。

通用 timer 事件接口为：

```c
typedef struct DmMc02TimMasterEvent {
    DmMc02TimMasterOutput output; /* TRGO or TRGO2 */
    DmMc02TimMasterEventKind kind;
    bool rising;
    unsigned event_count;
    uint64_t timestamp_ns; /* planned time of the last represented event */
} DmMc02TimMasterEvent;

void dm_mc02_tim2_set_event_callback(
    DmMc02Tim2 *state, DmMc02TimMasterEventCallback *callback,
    void *opaque);
void dm_mc02_tim2_set_mms2_supported(DmMc02Tim2 *state, bool supported);
```

compare callback 与 master-event callback 是同步、单线程接口；没有动态分配或每个
输出边沿的 host 队列。`compare_batch` 表示一次 callback 消费的连续 CC1 事件数；
CC2..CC4 当前只产生芯片层 status/IRQ 语义；它们不进入该 CC1 callback，但已在选择
对应 `OCxREF` master source 时作为独立的 `OCxREF` event kind 输出。
`events` 会包含 callback deadline 之后已经跨过的单周期事件，`timestamp_ns` 是本次
callback 所代表的最后一个事件的计划 virtual timestamp。下一次 deadline 从原有
绝对相位推进，不从 callback 执行时刻重新起算；无法用 `unsigned` 表示的极端事件数
会饱和到 `UINT_MAX`，这不是完整的长期积压恢复模型。选择 `MMS/MMS2=CC1` 时
compare batch 被强制为 1，正常 master pulse 的 `event_count` 为 1；无状态 compare
的连续积压事件会合并在一次 master-event callback 中。stateful OCREF compare 则逐
事件回调并保持 `event_count=1`，以保留每个边沿的方向和时间。timer 的普通 update
callback 与 compare callback、master-event callback 彼此独立，板级 callback 才
负责把抽象事件连接到 DMA 或 trigger bus。

TIM8 的 `CC1DE` 是一个带 DMA 外设副作用的 compare request。精确模式的板级 callback
使用 `dm_mc02_dma_request_batch()`，以便每个 half-word 仍通过 QEMU address space
写入 `TIM8_CCR1`，同时复用批量 request 的匹配和 IRQ 聚合；默认近似模式使用
`dm_mc02_dma_request_batch_coalesced()`，只保留固定端点在该批次的最后一次写入，
但仍推进 `NDTR`、HT/TC、circular 状态和 IRQ。合并接口只适用于单匹配、direct mode、
M2P、等宽、固定 peripheral address、非 DBM 的 endpoint-sampled consumer；需要每个
MMIO 副作用、FIFO、DBM 或逐事件边界的调用方必须使用普通 batch 或单事件 API。
`dm_mc02_dma_advance_stream()` 是明确的 state-only 快速路径，不会执行外设 MMIO 写入，
不能用于 WS2812 或其它需要真实外设端点副作用的消费者。live `CCR1` 更新会改变下一
compare 阈值，但不会重置 CNT 的 virtual phase。

RNG 芯片层映射 H723 `RNG_CR/SR/DR/HTCR` 的固件可见子集。`CR` 支持
`RNGEN/IE/CED/RNG_CONFIG1/2/3/NISTC/CLKDIV/CONDRST/CONFIGLOCK`；配置字段
在 `CONFIGLOCK` 置位后保持不变，普通 reset 会解锁并清零。`HTCR` 在未锁定时
可读写，锁定后保持原值。启用后首次填充四个 32-bit 输出字；读取完四字后
`DRDY` 暂时清零，下一次 `SR` 轮询仍返回空状态，后续轮询补充下一组四字。
这保持了 HAL 文档描述的耗尽边界，同时不为每个随机字创建 QEMU virtual
timer。输出为 deterministic xorshift32，不能用于熵质量、健康测试或精确 RNG
生成延迟验证。
若 RNG 已启用且输出缓冲为空时重新从 0 写入 `IE`，模型也会补充一组数据并
重新评估 IRQ 电平，以支持 HAL 的重复中断模式调用。
`CONDRST=1` 时清空输出、清除错误状态且不报告 `DRDY`；写回 0 后按当前
`RNGEN` 状态重新填充。当前错误状态 `CECS/SECS` 阻止新数据生成；clock
error 不会丢弃错误发生前已生成的可读数据。`CEIS/SEIS` 是锁存的中断原因，
本身不等同于当前错误；因此 `SECS=0, SEIS=1` 时保留的 FIFO 数据仍可读，
符合 HAL 的自动恢复/清除锁存路径。

本文档区分“当前已实现接口”和“建议的下一版稳定接口”。当前 v1 接口保持兼容；建议接口用于后续 Gazebo/MuJoCo、回放和测试控制，不要求一次性改完现有 QEMU 后端。

## 1. QEMU 启动与通道分配

机器名：`dm-mc02`
CPU：`cortex-m7`
固件：`-kernel <elf>`
当前 QEMU 不提供 Web 面板。

QEMU 内部按三层组织：芯片模型只实现寄存器和通用数据面；`DmMc02BoardProfile`
提供地址、serial slot、DMAMUX/request、RS485 和 IRQ wiring；`dm_mc02.c` 只负责
实例化、连接和装载固件。新增同类开发板时应优先新增 profile 数据，不复制芯片模型。

当前注册表提供两个 profile：默认 `DM-MC02` 和用于复用验收的虚拟
`STM32H723-EVAL`。启动前可使用 `-machine dm-mc02,board-profile=<name>` 选择；
`dm_mc02_board_lookup(name)` 负责严格查找，未知名称必须在初始化阶段失败，初始化
完成后 `board-profile` 只读。两个 profile 共用 `dm_mc02_stm32h723_soc`，而 GPIO/pin、
串口/FDCAN 数量和 DMA route 由各自 profile 数据提供。SoC profile 的 `cpu_type` 是
ARMv7M 设备属性所需的完整 QOM 类型名 `cortex-m7-arm-cpu`，不能直接使用命令行
`-cpu cortex-m7` 的简写。

SoC 在 `0x1FF1E000` 提供固定的 UID/ADC factory calibration 只读窗口。当前已定义的
测试值位于 `+0x800/+0x804/+0x808`（UID）和 `+0x820/+0x840/+0x860`（校准值）；
这些值属于 SoC profile 的通用数据，不属于 DM-MC02 pin wiring。guest 对该窗口的
写入被 ROM memory region 忽略，不能改变后续读取结果。

机器属性 `board-profile` 在 machine 初始化前选择已注册的板级 profile，默认值为
`DM-MC02`；machine 初始化后该属性锁定。profile 必须使用独立的 SoC 地址/IRQ/DMAMUX
和板级 pin 数据，不能在芯片模型中读取固定的 DM-MC02 引脚。

建议的基础启动形式：

```bash
qemu-system-arm \
  -machine dm-mc02 -kernel trobot.elf -nodefaults \
  -display none -monitor none \
  -chardev socket,id=cosim,path=/tmp/dm-mc02-cosim.sock,server=on,wait=off \
  -serial chardev:cosim
```

`serial_hd()` 槽位如下。为使用后面的槽位，命令行必须用 `-serial none` 占位。

| 槽位 | 逻辑接口 | 数据格式 | 方向 |
|---:|---|---|---|
| 0 | co-sim | 4-byte LE length + v1 protocol frame | 双向 |
| 1 | USART1 | 原始 byte stream | 双向 |
| 2 | USART2 / RS485 | 原始 byte stream | 双向 |
| 3 | USART3 / RS485 | 原始 byte stream | 双向 |
| 4 | UART5 | 原始 byte stream | 双向 |
| 5 | UART7 | 原始 byte stream | 双向 |
| 6 | USART10 | 原始 byte stream | 双向 |
| 7 | FDCAN1 | 固定 84-byte CAN frame | 双向 |
| 8 | FDCAN2 | 固定 84-byte CAN frame | 双向 |
| 9 | FDCAN3 | 固定 84-byte CAN frame | 双向 |
| 10 | USB OTG HS virtual CDC pipe | FIFO0 原始 byte stream | 双向 |

USB slot 10 需要用前十个 `-serial` 槽位占位，再提供第 11 个 `-serial` 后端。固件
通过 DWC2 `FIFO0`（`0x40041000`）读写字节；RX 可通过 `GINTSTS.RXFLVL`（bit 4）
轮询。该接口用于快速 host/device 数据测试，不模拟 USB 枚举、descriptor、PHY 或
真实 USB 总线时序。

QEMU 内部 qtest packet harness 另有私有测试窗口，可验证 EP0 控制事务和板级描述符
使用的 EP1..EP5 FIFO/完成状态；它不连接 serial slot 10，也不代表这些端点已经接入
真实 DWC2 guest 状态机或宿主机 USB 总线。

### USB endpoint packet queue（可复用器件边界）

`DmUsbEndpointQueue` 是板卡无关的固定容量 packet FIFO，位于 USB controller adapter
和具体 bus/PHY 实现之间。queue 只拥有 packet storage，不解释 token、PID、data toggle
或 endpoint 状态机；`peek()` 返回的 packet data 只在下一次 queue 操作或 cleanup 前借用。

```c
typedef struct DmUsbEndpointConfig {
    uint8_t number;
    DmUsbEndpointDirection direction;
    DmUsbEndpointType type;
    uint16_t max_packet_size;
    size_t capacity;
} DmUsbEndpointConfig;

typedef struct DmUsbEndpointPacket {
    const uint8_t *data;
    size_t length;
    uint64_t timestamp_ns;
} DmUsbEndpointPacket;

DmUsbEndpointQueueResult dm_usb_endpoint_queue_init(
    DmUsbEndpointQueue *, const DmUsbEndpointConfig *);
DmUsbEndpointQueueResult dm_usb_endpoint_queue_enqueue(
    DmUsbEndpointQueue *, const uint8_t *, size_t, uint64_t timestamp_ns);
DmUsbEndpointQueueResult dm_usb_endpoint_queue_peek(
    const DmUsbEndpointQueue *, DmUsbEndpointPacket *);
DmUsbEndpointQueueResult dm_usb_endpoint_queue_dequeue(
    DmUsbEndpointQueue *, DmUsbEndpointPacket *);
void dm_usb_endpoint_queue_reset(DmUsbEndpointQueue *);
void dm_usb_endpoint_queue_cleanup(DmUsbEndpointQueue *);
```

`enqueue()` 接受 `0 <= length <= max_packet_size`，`NULL` data 只允许 zero-length
packet；队列满、超长、空队列和非法/未初始化参数分别返回明确结果，不覆盖旧 packet。
DM-MC02 adapter 当前将 EP1..EP5 的 byte FIFO 按 64-byte MPS 切分后入队，TX 先写入
有界 pending buffer，收到 IN 请求时才提交完整 packet。RX queue 满或 pending buffer
不足时保留已接受的前缀并累计内部丢弃字节；该策略不等价于 USB NAK/backpressure。

隔离契约由 `tests/unit/test-dm-usb-endpoint-queue` 覆盖，controller 边界由
`tests/qtest/dm-mc02-usb-test` 覆盖。这个 queue 不改变 serial slot 10 的 FIFO0 原始
byte pipe，也没有把私有 qtest window 变成宿主机可枚举 USB 设备。

### USB control-transfer core（可复用器件边界）

`DmUsbControlDevice` 位于 USB controller adapter 与具体板级 descriptor/class policy
之间。它只处理 control transfer 的 setup/data/status 阶段和固定大小的数据缓存，不
依赖 QEMU USB bus、DM-MC02 pin map、chardev 或 PHY。controller adapter 负责把真实
transaction（当前 DM-MC02 使用私有 qtest packet harness）转换成以下三个调用：

```c
void dm_usb_control_init(DmUsbControlDevice *, const DmUsbControlOps *,
                         void *opaque, uint16_t max_packet_size);
void dm_usb_control_reset(DmUsbControlDevice *);
void dm_usb_control_bus_reset(DmUsbControlDevice *);
DmUsbControlResult dm_usb_control_setup(DmUsbControlDevice *,
                                        const uint8_t setup[8]);
DmUsbControlResult dm_usb_control_in(DmUsbControlDevice *, uint8_t *data,
                                     size_t capacity, size_t *length);
DmUsbControlResult dm_usb_control_out(DmUsbControlDevice *,
                                      const uint8_t *data, size_t length);
DmUsbControlResult dm_usb_control_execute_request(
    DmUsbControlDevice *, const DmUsbControlRequest *, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    size_t *actual_length);
```

`DmUsbControlOps.get_descriptor()` 提供 descriptor 的只读指针和长度；
`class_request()` 是板卡/器件策略回调。对于 OUT class request，core 在 setup 阶段
以 `out_length=0` 调用一次进行 admission，数据阶段累积到 `wLength` 后再调用一次
提交；回调失败会转为 STALL。IN 数据由 core 按 `max_packet_size` 分包；当实际数据
长度小于 `wLength` 且恰好落在 MPS 边界时，core 追加一个 zero-length packet。没有数据
的控制读请求进入 status-OUT；没有数据的控制写请求进入 status-IN。

当前标准请求集合是 `GET_STATUS`、`SET_ADDRESS`、`GET_DESCRIPTOR`、
`GET_CONFIGURATION` 和 `SET_CONFIGURATION`。address/configuration 的新值暂存到
status-IN 被消费时才提交，并通过 `set_address`/`set_configuration` 回调通知 consumer；
这避免在 setup 阶段提前改变 USB device state。未知或不支持的请求进入 `STALLED`。
`dm_usb_control_reset()` 只重置 control core 自身；`dm_usb_control_bus_reset()` 还同步通知
consumer 将 address/configuration 归零，因此适用于 USB port reset，而不是 controller core
reset 的替代品。

DM-MC02 adapter 通过该接口提供 descriptor、CDC `GET/SET_LINE_CODING` 和
`SET_CONTROL_LINE_STATE`，并把 address 同步到 DWC2 `DCFG.DAD[10:4]`。`test-dm-usb-control`
是隔离契约门（当前 `7/7`）；`tests/qtest/dm-mc02-usb-test` 是直接 consumer 边界门
（当前 `9/9`，包括 board-visible EP0 STALL、空 IN 的 NAK 和 DAD 位域）。control core 不负责
token/PID、endpoint 状态机、DMA、USB PHY、总线仲裁、宿主机枚举或电气时序；这些由
下一层 transaction/transport 接口负责。

### USB device transaction core（可复用器件边界）

`DmUsbTransactionDevice` 位于 USB token producer 与 control/endpoint data consumer
之间。它不依赖 QEMU `USBPacket`、USB bus、QOM、DM-MC02 寄存器或 PHY；未来的
QEMU USB adapter、私有 qtest harness 和 synthetic upstream host 都可以使用同一接口。

```c
void dm_usb_transaction_init(DmUsbTransactionDevice *, DmUsbControlDevice *,
                             const DmUsbTransactionOps *, void *opaque,
                             uint16_t ep0_max_packet_size);
DmUsbTransactionStatus dm_usb_transaction_configure_endpoint(
    DmUsbTransactionDevice *, uint8_t endpoint, DmUsbEndpointDirection,
    DmUsbEndpointType, uint16_t max_packet_size, bool enabled);
DmUsbTransactionResult dm_usb_transaction_submit(
    DmUsbTransactionDevice *, const DmUsbTransaction *);
```

`DmUsbTransaction` 明确区分 `SETUP`、`IN`、`OUT` token，并可由调用者指定
`DATA0/DATA1` 或使用 `PID_AUTO`。复位后非 EP0 端点从 DATA0 开始；成功的数据事务
才翻转该方向的 PID，`NAK`、`INVALID` 和 `STALL` 不翻转。setup 成功会让 EP0 的
control/data 阶段从 DATA1 开始。`SETUP` 必须是 EP0 的 8-byte OUT 数据；EP0 的
后续阶段直接复用 `DmUsbControlDevice`。

IN/OUT callback 是唯一的板级数据边界，callback 收到 transaction 的
`timestamp_ns`，由 producer 提供的虚拟时间可以不经过 wall-clock 转换直接进入
队列或外部 adapter。callback 返回 `ACCEPTED`、`NAK`、`STALL` 或 `INVALID`；成功
IN 返回精确 `actual_length`，空 IN 不自动变成成功的 zero-length packet。对非 EP0
端点，`STALL` 会锁存方向对应的 halt，`clear_halt()` 清除 halt 并将该方向 PID
恢复为 DATA0；新 EP0 setup 会清除 EP0 halt。`reset()` 保留端点配置，仅清除 halt
和 PID 状态。

`test-dm-usb-transaction` 是独立契约门（当前 `5/5`），覆盖 data callback、PID
toggle、时间戳传递、NAK、STALL/clear-halt、EP0 status PID 和 reset；DM-MC02 qtest
通过 transaction adapter 覆盖原有 descriptor、CDC、FIFO、中断和空 IN NAK。该层仍
不是完整 DWC2 device controller：没有 QEMU USB bus 的 upstream host attachment、
真实 DMA/FIFO、SOF、PHY、电气时序或宿主机枚举。

### USB synthetic upstream host（可复用 producer 边界）

`DmUsbHost` 是 transaction producer 的同步、进程内实现。它只持有一个提交回调，
因此可以连接 `DmUsbTransactionDevice`、未来的 QEMU USB transport，或独立测试 fixture；
它不拥有 device 的端点队列、PID 状态或板级寄存器。

```c
typedef DmUsbTransactionResult DmUsbHostSubmitTransaction(
    void *opaque, const DmUsbTransaction *transaction);
void dm_usb_host_init(DmUsbHost *, DmUsbHostSubmitTransaction *, void *,
                      uint16_t ep0_max_packet_size);
DmUsbHostResult dm_usb_host_control_transfer(
    DmUsbHost *, const uint8_t setup[8], const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    uint64_t timestamp_ns);
DmUsbHostResult dm_usb_host_bulk_out(
    DmUsbHost *, uint8_t endpoint, uint16_t max_packet_size,
    const uint8_t *data, size_t length, uint64_t timestamp_ns);
DmUsbHostResult dm_usb_host_bulk_in(
    DmUsbHost *, uint8_t endpoint, uint16_t max_packet_size,
    uint8_t *data, size_t capacity, uint64_t timestamp_ns);
```

控制传输会生成一个 SETUP token，随后按 setup direction 生成 DATA token 和对应的
status token；EP0 data packet 使用 host 初始化时的 MPS。bulk 接口使用调用者从
descriptor 得到的 endpoint MPS，并在一个调用内完成短包或 buffer 边界。所有 token
使用 `PID_AUTO`，由 transaction consumer 维护每方向 toggle；host 不重复提交被 NAK
的 token，直接返回 `DmUsbHostResult.status`，调用者可按自己的调度策略重试。
`actual_length` 是已成功提交的 data 字节数，`transactions` 包含本次调用已经提交
的 token 数；每个 token 使用调用者给出的虚拟 `timestamp_ns`。

`test-dm-usb-host` 是 host 与 transaction 的隔离契约门（当前 `4/4`）。该接口仍
不是真实 USB host controller：没有 SOF、帧调度、设备地址/枚举拓扑、异步完成/取消、
总线仲裁、重试/duplicate DATA ACK 物理语义、DWC2 FIFO/DMA/IRQ、PHY 或宿主机 USB
设备挂载。

### USB host-port lifecycle（可复用 transport 边界）

`DmUsbHostPort` 与 `DmUsbHost` 分开：前者只表达一个已连接 port 的生命周期事件，后者
只编排 SETUP/IN/OUT transaction。该边界不依赖 QEMU `USBPort`、QOM、DWC2 寄存器或
DM-MC02 板级状态。

```c
typedef void DmUsbHostPortReset(void *opaque, uint64_t timestamp_ns);

void dm_usb_host_port_init(DmUsbHostPort *, DmUsbHostPortReset *, void *);
void dm_usb_host_port_reset(DmUsbHostPort *, uint64_t timestamp_ns);
```

`reset()` 是同步调用：调用者提供单调的虚拟时间，callback 恰好接收一次相同的 `opaque`
和时间戳；它不生成 `DmUsbTransaction`，也不返回 packet 级结果。没有 callback（或空
port）时是明确 no-op。attach/detach、speed、枚举、SOF、async completion/cancel、重试和
host-controller 寄存器不属于该最小边界。

`test-dm-usb-host-port` 是隔离门（当前 `2/2`），覆盖 callback 的 `opaque`/时间戳透传和
无 callback 的 no-op。`DmUsbQemuAdapter` 是已验证的直接 producer，`DmUsbDwc2Device`
提供已验证的 bus-reset consumer；两者都通过此边界而不把 port reset 解释成
`GRSTCTL.CSFTRST` core reset。

### STM32H7 OTG host-port（芯片层）

`DmStm32H7OtgHost` 是 STM32H7 DWC2 host-port 的最小、可复用寄存器状态模型。它属于
芯片层，不依赖 DM-MC02 的地址、NVIC、GPIO、QEMU `USBPort`、QOM 或外部 plant；板卡
adapter 将来只需要转发 32-bit MMIO、QEMU virtual timestamp 和 IRQ callback。当前它尚未
挂入 DM-MC02 machine，因为实际 `trobot` 固件使用同一个 `USB1_OTG_HS` block 的 device
mode，不能让 host 和 device 两套模型同时占用该地址窗口。

```c
void dm_stm32h7_otg_host_init(DmStm32H7OtgHost *,
                               DmUsbHostPortReset *, void *,
                               DmStm32H7OtgHostIrq *, void *);
void dm_stm32h7_otg_host_reset(DmStm32H7OtgHost *);
void dm_stm32h7_otg_host_set_port_connected(
    DmStm32H7OtgHost *, bool connected, DmStm32H7OtgPortSpeed speed);
uint32_t dm_stm32h7_otg_host_read(const DmStm32H7OtgHost *, uint32_t offset);
void dm_stm32h7_otg_host_write(DmStm32H7OtgHost *, uint32_t offset,
                                uint32_t value, uint64_t timestamp_ns);
```

当前 MMIO 子集是 `GAHBCFG`、`GINTSTS/GINTMSK`、`HCFG`、`HFIR` 和 `HPRT0`，使用真实
DWC2 偏移和 bit：`HPRT0` 在 `0x440`，port interrupt 在 `GINTSTS[24]`。reset 的默认
值与 upstream DWC2 host 一致：`HCFG.RESVALID=2`、`HFIR=60000`、`HPRT0.PWR=1`。外部
fixture/transport 通过 `set_port_connected()` 注入 attach/detach；连接变化置
`CONNDET`，detach 额外置 `ENACHG`，两者均按 `GAHBCFG.GINT` 和 `GINTMSK.PRTINT`
产生 level IRQ。`HPRT0.CONNDET/ENACHG/OVRCURRCHG` 是 W1C，guest 不能直接置
`CONNSTS`/`ENA`。

写 `HPRT0.RST=1` 仅进入 reset-asserted 状态；随后写 `RST=0` 时，如果 port 仍连接，
模型置 `ENA|ENACHG`，并通过内嵌 `DmUsbHostPort` 同步发出一次 reset callback。回调携带
调用者提供的虚拟时间戳，因而可直接连接 `dm_usb_dwc2_bus_reset()`，但 host 模型不访问
DWC2 的私有状态。

`test-dm-stm32h7-otg-host` 是芯片层与直接消费者的门（当前 `5/5`）：覆盖 reset 默认值、
Full-Speed attach、change IRQ/W1C、RST assert/deassert、空 port no-op，以及 host-port 到
`DmUsbDwc2Device` bus reset 的精确时间戳、address/configuration/DAD/endpoint runtime
清理。它不表示已有可启动的 host firmware path。故意未实现的部分包括 `GUSBCFG` role
切换、`GRSTCTL`、SOF/frame、host channel/DMA/FIFO、PHY/VBUS/SE0 时序、枚举、async
completion/cancel、QEMU USB bus topology 和 host-machine USB passthrough。

### USB DWC2 device-mode core（可复用器件边界）

`DmUsbDwc2Device` 是 transaction dispatcher 之上的板卡无关 device-mode core。它拥有
端点寄存器、每端点独立的 IN/OUT FIFO、传输计数和 device-mode interrupt 状态；它不
拥有 DM-MC02 地址映射、NVIC 线路、DMA stream、USB bus 或 PHY。板级 consumer 应将
MMIO offset 和 IRQ callback 接到 core，host/transport producer 通过
`dm_usb_dwc2_submit()` 提交 token。

```c
void dm_usb_dwc2_init(DmUsbDwc2Device *, DmUsbControlDevice *,
                      DmUsbDwc2Irq *, void *, uint16_t ep0_mps);
void dm_usb_dwc2_reset(DmUsbDwc2Device *);
void dm_usb_dwc2_bus_reset(DmUsbDwc2Device *, uint64_t timestamp_ns);
DmUsbTransactionResult dm_usb_dwc2_submit(
    DmUsbDwc2Device *, const DmUsbTransaction *);
bool dm_usb_dwc2_endpoint_fifo_count(const DmUsbDwc2Device *,
                                     unsigned endpoint, bool in,
                                     size_t *count);
uint64_t dm_usb_dwc2_read(DmUsbDwc2Device *, uint32_t offset, unsigned size);
void dm_usb_dwc2_write(DmUsbDwc2Device *, uint32_t offset,
                       uint64_t value, unsigned size);
```

EP0 的 SETUP/DATA/status 阶段复用 `DmUsbControlDevice`。非 EP0 端点通过
`DIEPCTL/DOEPCTL` 的 `USBAEP`、endpoint type、MPS 和 `EPENA` 配置，guest 对
`FIFO0 + endpoint * FIFO_STRIDE` 的写入形成 IN producer 数据，对应 OUT FIFO 读出会
消费数据。`DIEPTSIZ/DOEPTSIZ` 的 packet count 和 transfer size 在每个已接受 token
后递减；count 归零时清除 `EPENA` 并置位 `XFRC`。`DIEPINT/DOEPINT` 是 W1C，
`DAINT` 暴露原始端点状态，`GINTSTS.IEPINT/OEPINT` 按 endpoint/global masks 派生，
IRQ callback 只在 level 变化时调用。FIFO read 明确是 destructive operation，故 API
接收可变 `DmUsbDwc2Device *`。

`dm_usb_dwc2_endpoint_fifo_count()` 是非破坏性的只读观察接口：`in=true` 返回指定
端点待发送的 IN FIFO 字节数，`in=false` 返回待 guest 消费的 OUT FIFO 字节数；设备为空、
端点超出 `0..15` 或 `count` 为空时返回 `false`，且不改变状态。DM-MC02 adapter 只通过
该接口观察 pending bytes，不得访问 `DmUsbDwc2Endpoint` 或 `DmUsbDwc2Device` 的私有字段。

`GRSTCTL.CSFTRST` 在当前 core 中是同步操作：复位请求自清除，并置位
`CSFTRSTDONE`，直到下一次 core reset。DM-MC02 adapter 的 legacy FIFO0 byte pipe
仍由 adapter 自己拥有；它只在 `GINTSTS` 读取时合并该 FIFO 的 `RXFLVL`，不改变
通用 DWC2 endpoint 状态。

`DCFG.DAD` 使用真实的 bit `4..10`。`dm_usb_dwc2_bus_reset()` 接收 producer 的虚拟
时间戳并同步恢复 USB device default state：它调用 control bus reset、清零 DAD、PID/halt、
endpoint FIFO、transfer size、endpoint interrupt 和 `EPENA/STALL` runtime 状态。它保留
DCFG 的非 DAD 位、global config/mask、FIFO sizing、DMA 地址和 endpoint 的 `USBAEP`/MPS/type
编程字段；因此它不是全控制器 reset，也不模拟 USB reset interrupt、枚举、SOF、PHY 或
电气时序。

`DIEPTXF0` 至 `DIEPTXF14` 位于 core 的 `0x100 + 4*n`，每个寄存器的高 half-word 是
FIFO depth（以 32-bit words 计），低 half-word 是 FIFO start address。复位后的每个
depth 必须为非零，guest 可按 CherryUSB 的动态布局重新写入并读回这些寄存器；该状态
属于 DWC2 core，不得只写 DM-MC02 的 legacy compatibility array。当前模型不声称实现
真实 FIFO RAM 仲裁或 USB DMA。

DM-MC02 的 USB `MemoryRegion` 保持非对齐访问无效，并将实现访问作为整体交给 valid
校验，避免 QEMU AddressSpace 把跨 `DIEPTXF` 寄存器的访问拆成多个自然对齐写入；因此
非法跨寄存器/非对齐访问不会修改相邻寄存器。该 MemoryRegion 规则属于板级 MMIO 边界，
不改变 core 公共 API 的直接调用语义。

`test-dm-usb-dwc2-device` 是该层隔离门（当前 `7/7`），覆盖 core reset defaults、EP1
bulk IN/OUT 分包、精确 FIFO 字节、计数和 IRQ、EP0 descriptor control transfer、
mask/W1C、非法跨界 MMIO、core reset IRQ deassert 和独立 bus reset。当前 deliberate limits 是 DMA、
`RXFLVL`/SOF、异步 token、PHY、电气时序、QEMU USB bus attachment、宿主机枚举和
真实 H723 device-mode register quirks。DM-MC02 已将该 core 映射到 USB OTG HS 窗口，
并通过 board profile 的 `irqs.usb=77` 接到 ARMv7M/NVIC；`/dm-mc02/usb/irq-reaches-nvic`
验证 `GINTSTS -> IRQ 77 -> ISPR2` 的边界。当前 DM-MC02 USB qtest 为 `10/10`，其中
`/dm-mc02/usb/power-on-tx-fifo-registers` 直接验证上电 depth 和动态读回。下一道门是明确 transport adapter 与 QEMU
USB bus 的集成，而非把当前私有 MMIO harness 当成宿主机枚举。

### USB DWC2 device-mode VMState（可复用组件状态边界）

`dm_usb_dwc2_vmstate()` 是 `DmUsbDwc2Device` 的组件级状态契约，不是 machine-level
迁移注册。状态 producer 是 DWC2 device-mode core 的 guest-visible 寄存器、端点 FIFO
和 transaction endpoint state；consumer 是未来的组合/快照恢复流程。它保存全局/设备
寄存器、`DIEPTXF[0..14]`、每个端点的 IN/OUT FIFO 与游标/count、传输计数、DMA 地址、
DATA PID/halt/type/config 和 overflow 计数。FIFO 游标虽然在 C API 中是 `size_t`，在线格式
固定为 64-bit big-endian；枚举在线格式固定为 32-bit big-endian，避免主机字长或枚举布局
改变状态流。

```c
void dm_usb_dwc2_sync_runtime(DmUsbDwc2Device *);
const VMStateDescription *dm_usb_dwc2_vmstate(void);
```

`ep0_max_packet_size` 是目的端静态配置，用 equal field 校验而不是迁移覆盖；EP0 必须
保持 control type、双向 enabled 和匹配的 MPS。恢复前拒绝不支持版本、FIFO head/count
越界、非法 endpoint type、非 DATA0/DATA1 PID 或不一致的 EP0 配置。control device、
transaction callback/opaque、IRQ callback/opaque 和当前 `irq_level` 都是目的端 runtime
wiring，不进入状态流。

post-load 只有在全部序列化字段和派生不变量通过后才执行 `dm_usb_dwc2_sync_runtime()`。
该函数重新绑定 transaction 的 control/ops/opaque，并先撤销目的端旧的 asserted IRQ，再
依据恢复后的 masks/status/endpoint interrupt 重新投影 level-sensitive IRQ。失败或版本不
匹配时不调用该函数，因此不会改变目的端 runtime wiring；已加载的普通状态字段仍由 QEMU
VMState 的失败语义处理，不能据此宣称事务级回滚。

该描述不保存 `DmUsbControlDevice` 自身状态，也不包含 `MemoryRegion`、QOM ownership、
USB PHY、DMA、SOF、宿主机 USB bus 或异步 transaction。`test-dm-usb-dwc2-vmstate` 的
`4/4` 覆盖 round-trip/继续传输、非法 FIFO、非法 PID、版本拒绝及失败时 runtime wiring
保持不变；它与 DWC2 core、QEMU adapter 和 DM-MC02 USB qtest 分开作为直接门。该组件仍
未注册到 DM-MC02 machine，control/IRQ/NVIC、RAM、co-sim 队列的联合恢复顺序必须另行
定义和测试。

DM-MC02 machine 可配置以下低频模型参数（默认值不改变既有行为）：

```text
-machine dm-mc02,accurate-timing=on
-machine dm-mc02,dma-batch-limit=8
-machine dm-mc02,imu-noise-gyro-dps=0.02
-machine dm-mc02,imu-noise-accel-g=0.001
-machine dm-mc02,imu-bias-gyro-dps=0.1:0:0
-machine dm-mc02,imu-bias-accel-g=0:0:0.01
-machine dm-mc02,imu-temperature-c=25
-machine dm-mc02,imu-temp-coeff-gyro-dps-per-c=0:0:0
-machine dm-mc02,imu-temp-coeff-accel-g-per-c=0:0:0
-machine dm-mc02,imu-bias-random-walk-gyro-dps-per-sqrt-s=0:0:0
-machine dm-mc02,imu-bias-random-walk-accel-g-per-sqrt-s=0:0:0
-machine dm-mc02,imu-seed=1234
-machine dm-mc02,vin-mv=24000
-machine dm-mc02,user-key=on
-machine dm-mc02,gpio-input=A14=1
-machine dm-mc02,flash-file=/tmp/dm-mc02.flash
-machine dm-mc02,cold-reset=on
-machine dm-mc02,fdcan-host-ack=on
-machine dm-mc02,adc-power-model=on
-object can-bus,id=canbus -machine dm-mc02,canbus=canbus
```

噪声使用六个均匀分布、方差归一化的确定性 LCG 近似标准正态分布；单位分别为 dps 和 g。reset
会恢复随机序列起点，便于回归测试。`accurate-timing` 关闭 WS2812 高频批处理，并关闭 ADC
连续序列的 1 ms 事件限流，使 ADC 按当前 DM-MC02 PLL2P/PLL3R/CLKP 选择与 common
`CCR.CKMODE/PRESC` 计算出的有效转换时钟运行，
`dma-batch-limit` 仅限制 SPI2 DMA 每次虚拟 timer callback 的最大搬运数。
`adc-power-model=on` 启用 ADC 芯片层的 H723 低功耗序列：复位时 `DEEPPWD=1`、
`ADVREGEN=0`；guest 清除 `DEEPPWD` 并置位 `ADVREGEN` 后，regulator 在 10 us
虚拟时间启动，之后 `ADEN`/`ADSTART` 才会生效。该等待使用 QEMU virtual clock，
不消耗 host wall-clock。默认值为 `off`，仅作为旧 fixture 的显式兼容路径；严格的
板级/固件回归应开启它。该切片不模拟模拟电源纹波、欠压曲线或 ADC enable 的模拟
起振细节。
`flash-file` 非空时在启动时加载最多 1 MiB，并在 QEMU 正常退出时写回整个 backing；
默认为空，因此默认运行完全以内存为后端。该文件只保存片上 Flash，不保存 SRAM 或外设
状态。默认 `system_reset` 是 warm reset，会保留 SRAM/Flash；`cold-reset=on` 会在每次
reset 时清空 ITCM、DTCM、AXI SRAM、D2/D3 SRAM，但仍保留片上和外部 Flash。

`vin-mv` 是外部电源输入，默认 `24000` mV。它既可在启动参数中设置，也可通过 QMP
`qom-set` 对 `/machine` 的 `vin-mv` 属性运行时修改；修改会立即重算 VIN ADC 分压和
`electrical-power` 策略下的 5 V/CAN/RS485 供电状态。`system_reset` 会保留 VIN，GPIO
控制的 24 V/5 V enable 则按 MCU reset 恢复。

VIN=0 是 MCU 的 power-off/reset 边界，不依赖 `electrical-power` 是否开启：machine 会复位并
保持 Cortex-M7 halted，所有 guest 指令和 FreeRTOS tick 停止；恢复到非零 VIN 后会从
复位入口重新启动。只读 QOM 属性 `mcu-power-good` 可查询当前 MCU 状态。低于板级
欠压阈值但不等于零的输入仍沿用当前离散电源模型，不等同于完整模拟的 brownout 曲线。

板级实际供电状态也通过 `/machine` 只读 QOM 属性提供：`out1-enabled`、`out2-enabled`
和 `5v-switch-enabled` 表示 GPIOC PC14/PC13/PC15 的 enable 请求；`out1-good`、
`out2-good`、`system-5v-good` 和 `system-3v3-good` 表示经过 VIN、欠压以及
`electrical-power` switched-5V 策略计算后的实际可用状态。GPIOC ODR 运行时变化会立即
更新这些属性及 FDCAN/USART2/USART3 收发器供电，不需要 QMP 轮询后再手动刷新模型。

FDCAN1/2/3 默认连接 machine 自动创建的内部 QEMU `CanBusState`；也可以显式创建并连接
外部 bus：`-object can-bus,id=canbus -machine dm-mc02,canbus=canbus`。标准 bus 只提供
即时广播、发送者不回环和“至少一个 peer 可接收”返回值；它不提供 CAN-ID 仲裁、帧持续时间、
bit stuffing、物理 ACK、error frame 或总线级 bus-off 恢复。FDCAN 收到 standard-bus 帧时
使用当前 `QEMU_CLOCK_VIRTUAL` 作为时间戳，因此不能把该路径当作带外部时间戳的回放接口。

FDCAN 的粗粒度 `TEC/PSR.BO/IR.BO` 与显式 `CCCR.INIT` 恢复仍存在于 controller model，
但仅用于无 ACK/供电故障等 functional error 诊断，不代表真实 CAN error frame、位级重试或
129×11 recessive-bit recovery。需要可重复帧时序时，应在 standard bus 之上另行接入独立的
virtual-time scheduler adapter，不把时序策略重新写入 FDCAN 或 board profile。

`fdcan-host-ack=on` 是显式 host bridge policy：当 FDCAN chardev backend 已连接且帧已
进入 QEMU 的 host TX queue 时，将其作为外部 ACK 参与者。它适合由外部总线/plant 负责
仲裁的连接；它不等同于 Linux SocketCAN 驱动报告的物理 ACK，默认关闭，以保留 no-ACK
故障注入语义。

`user-key` 控制板上 PA15 的 active-low 用户键，默认 `off`（松开，GPIO IDR 为高电平）。
它既可作为启动属性设置，也可通过 QMP `qom-set /machine user-key true|false` 运行时按下或
释放；按键边沿经 EXTI15_10 的 RTSR1/FTSR1、PR1 和 C1IMR1 进入 NVIC。该接口不绕过
GPIO 输入寄存器，适合调试模式启动时模拟“按住 KEY”。

`gpio-input` 是通用外部输入接口，格式为 `PORTPIN=0|1`（例如 `A14=0`），可在启动时
设置或通过 QMP `qom-set /machine gpio-input "A14=0"` 修改。输入保持在对应 GPIO 的
IDR；对 EXTI0--15，模型读取 SYSCFG EXTICR 的端口选择后再将输入边沿送入 EXTI。
EXTICR1..4 运行时改写会立即重新应用当前 GPIO 输入，因此已选端口的已有电平边沿
不会因为复用切换而静默丢失。

蜂鸣器 PB15 的输出观察通过 `/machine` 的只读 QOM 属性提供：

```text
qom-get /machine buzzer-enabled
qom-get /machine buzzer-level
qom-get /machine buzzer-frequency-hz
qom-get /machine buzzer-duty-permille
```

`buzzer-enabled` 仅在 PB15 为 GPIO 输出高电平，或为 AF2/TIM12_CH2 且 PWM
通道有效时为真；`buzzer-level` 按当前 QEMU 虚拟时间惰性计算，不为每个 PWM 边沿
创建 host timer。频率单位为 Hz，占空比单位为千分比。v1 telemetry 保持原有 12-byte
格式，只把 `buzzer` 作为非零输出使能标志，避免破坏现有 worker。

QEMU 主循环不应因 host 读端慢而阻塞 guest。co-sim telemetry 使用 8 项有界、非阻塞 whole-frame 队列；FDCAN 使用 16 项有界帧队列；UART 使用 4096-byte 有界字节队列。队列满时分别按 telemetry drop-oldest、CAN/UART drop-newest，并通过模型内 `tx_dropped`/`tx_short_writes` 计数记录；chardev 短写会在虚拟时间中继续重试。上述计数可通过 QMP `qom-get /machine cosim-diagnostics` 读取；v1 协议没有 ACK/重传，v2 仅对 STEP ACK 提供 worker 有界重发和最近一步的幂等确认。

v2 的 guest-consumption token 也使用固定容量队列。队列满时新的 STEP 返回 `QUEUE_FULL`，不会再交付该 IMU；legacy/v1 路径仍保持非阻塞兼容行为，并通过 `consume_dropped` 诊断计数暴露无法建立消费确认的情况。

可选真实供电策略：`-machine dm-mc02,electrical-power=true`。默认 `false` 是 `ideal_power` 兼容模式；开启后 VIN、PC15 switched-5V 会实际门控 CAN 收发器和 USART2/USART3 RS485 收发路径。收发器掉电会丢弃其待发送 host 数据并使该 FDCAN endpoint 不参与 QEMU standard CAN bus；恢复供电后不会复活掉电前数据。QMP `system_reset` 会清理 GPIO、UART/FDCAN/ADC/DMA FIFO、BMI088 raw、timer 和 co-sim parser 状态，并恢复 BMI088 CS 高电平。

BMI088 的当前数据面遵循以下轻量时序契约：写 accel `ACC_CONF[3:0]` 的有效 ODR 码 `5..12` 配置 12.5..1600 Hz；写 gyro `BANDWIDTH[3:0]` 的码 `0..7` 配置 2000/2000/1000/400/200/100/200/100 Hz。配置前 ODR 为零，保持兼容模式——每个合法 `IMU_SAMPLE` 立即可见；配置后仅接受 `virtual_time_ns >= next_sample_time_ns` 的输入帧，较早帧不会覆盖已锁存数据。接受的样本按当前量程转为 raw，置 accel `STATUS[7]` 与 gyro `INT_STAT_1[7]`，并将 accel `SENSORTIME[23:0]` 更新为 frame 时间按 25.6 kHz 换算的计数。`imu-temperature-c` 以 BMI088 的 11-bit、0.125°C、23°C offset 格式写入 `TEMP_M/TEMP_L`，默认 25°C。一次完整三轴数据 burst 会消费本模型的 DRDY 指示。

采样信号层还提供可复用的每轴温漂和 bias random walk 参数。温漂单位为
`dps/°C` 或 `g/°C`，以 25°C 为参考；random walk 单位为
`dps/sqrt(s)` 或 `g/sqrt(s)`，按接受样本的 virtual timestamp 用
`sqrt(dt)` 推进。两者默认均为零，不改变现有确定性无漂移行为；`imu-seed`
决定随机游走序列。machine 属性使用三个逗号或冒号分隔的轴值，运行时修改
只改变后续采样，QMP `qom-get` 返回当前配置。

加速度计 FIFO 使用真实的 `FIFO_LENGTH(0x24..0x25)`、`FIFO_DATA(0x26)`、`FIFO_DOWNS(0x45)`、`FIFO_CONFIG_0(0x48)` 与 `FIFO_CONFIG_1(0x49)` 数据面：启用 `ACC_EN` 后，co-sim 已接受的样本以 `0x84 + XYZ little-endian` 的 7-byte frame 写入 1024-byte FIFO；`FIFO_DOWNS[6:4]` 按 `2^k` 对输入采样降采样；`mode=0` 为 stream，`mode=1` 为 stop-at-full。配置变化会插入基础 `0x48` config frame，溢出会以不占 FIFO 容量的 skip frame 报告，FIFO 清空时可在同一 burst 返回 `0x44 + 3-byte sensor-time`；一次未读完整的 accel frame 在下一次 FIFO transaction 从帧首重读。`0x7e=0xb0` 清空 accel FIFO。陀螺仪使用 `FIFO_STATUS(0x0e)`、`FIFO_CONFIG_0(0x3d)`、`FIFO_CONFIG_1(0x3e)` 与 `FIFO_DATA(0x3f)`：`0x40` 为 stop-at-full、`0x80` 为 stream，数据以 8-byte frame 存储：6-byte XYZ little-endian rate 后跟 2-byte sampled interrupt field；未读完整 gyro frame 在下一次 transaction 被丢弃。FIFO 只在 host/co-sim 提供样本时推进，不创建额外高频 QEMU timer，因此默认实时路径没有额外事件负担。

当前未实现 accel FIFO 的 INT tag、sample-drop frame 和 FIFO 中断引脚映射，也未实现 gyro FIFO 的外部 tag 与精确 watermark/full interrupt 时序；不能将这些状态当作完整器件级 FIFO 验证。

soft-reset 使用真实 BMI088 寄存器：accel `0x7E=0xB6`，gyro `0x14=0xB6`；两者都会恢复对应 die 的寄存器、量程、ODR 和数据状态。

## 2. Co-sim v1 wire protocol（当前）

v1 的协议边界由板卡无关的 `cosim/dm_mc02_wire.[ch]` 统一实现。host
`dm_mc02_protocol` 和 QEMU `dm_mc02_cosim_link` 共用同一份 magic、header、
little-endian 原语、type/payload 长度和 body encoder/decoder；transport/chardev
仍分别拥有自己的 partial-I/O、队列、虚拟时间调度和 callback 分发。codec 的
`encode/decode` 只检查结构和长度，`dm_mc02_wire_payload_valid()` 负责 finite
float、reserved、ADC 范围等语义校验，以保持旧 API “编解码后显式 validate” 的
调用契约。

外层：

```text
u32 frame_len_le
u32 magic_le          = 0x32434d44   # bytes "DMC2"
u16 version_le        = 1
u16 type_le
u32 payload_len_le
u64 sequence_le
u64 virtual_time_ns_le
u8  payload[payload_len]
```

`frame_len = 28 + payload_len`，允许范围为 28..156；payload 必须精确匹配 type。所有整数 little-endian，float 为 IEEE-754 binary32 little-endian。

### 类型和 payload

| type | 名称 | payload |
|---:|---|---|
| 1 | RESET | 空 |
| 2 | IMU_SAMPLE | `gyro[3]:f32` dps，`accel[3]:f32` g，共 24 bytes |
| 3 | TELEMETRY | `led_rgb:u32` (`0xRRGGBB`)、`brightness:u32`、`buzzer:u8`、`board_flags:u8`、`reserved:u16`，共 12 bytes |
| 4 | ACK | 空；当前只被 codec 接受，QEMU 不主动产生 ACK |
| 5 | ADC_INPUT | `channel:u16`、`raw:u16`、`reserved:u32`，共 8 bytes |
| 6 | ADC_PIN_VOLTAGE | `channel:u16`、`flags:u16`、`voltage_uv:u32`、`reserved:u32`，共 12 bytes |

ADC channel 范围 0..31；pin voltage 范围 0..3,300,000 uV；reserved 必须为 0；IMU 六个 float 必须有限。

当前 telemetry 的 `board_flags`：bit 0 表示 PC13/24V enable，bit 1 表示 PC15/5V enable。它们是 GPIO/模型观察值，不等同于“rail 已供电”；rail 行为由 `electrical-power` 决定。`buzzer` 只有开关语义，不携带频率、占空比或方波采样。

### 时序与 session

- host→QEMU 和 QEMU→host 各自维护 sequence，不能共用一个 validator。
- RESET 建立新接收 session；RESET 自身的 sequence/time 成为基线。
- QEMU 在已连接的 chardev 发生 machine/system reset 或重新连接时，会先发送出站 RESET，再发送新 session 的强制 telemetry；出站 telemetry 序号从 1 重建。
- 普通 frame 的 sequence 和 virtual time 必须严格递增。
- 建议 RESET 使用 `sequence=0`，`virtual_time_ns=clock_origin`；第一帧数据使用正数 sequence 和严格更大的时间。
- RESET 把该帧的 host `virtual_time_ns` 映射到当时的 QEMU virtual clock；之后未来 IMU 样本会在对应 QEMU 时刻通过一个有界 FIFO 交付，迟到样本立即交付。该机制不阻塞 guest、不改变 QEMU clock，也不重放中间遗漏样本。
- v1 没有 clock negotiation、ACK、重传、丢帧补偿和 pause/step 命令。

## 3. FDCAN fixed frame（当前）

每帧严格 84 bytes：

```text
offset  size  field
0       4     can_id:u32_le       # 不含 Linux EFF/RTR 标志
4       4     flags:u32_le       # bit0 EXT, bit1 RTR, bit2 FD, bit3 BRS
8       1     dlc:u8
9       3     reserved           # 必须为 0，QEMU 接收端会拒绝非零值
12      8     virtual_time_ns:u64_le
20      64    data[64]
```

classic CAN 的 DLC 为 0..8；CAN-FD DLC 使用长度表 `0,1,..,8,12,16,20,24,32,48,64`。
QEMU 接收端会拒绝 reserved 非零、DLC 高四位非零、未知 flags、越界 CAN ID、classic
DLC>8、CAN-FD RTR 和 classic BRS。当前 v1 wire 没有 ESI 位，SocketCAN bridge 遇到
CAN-FD ESI 或未知 native flag 会显式拒绝，不会静默丢失状态；bridge 仍不模拟物理位
时序、真实物理 ACK、error frame 或完整 bus-off recovery。

每个 FDCAN 控制器均提供 IT0/IT1 两条 NVIC 输出；`ILS` 按中断状态位选择线路，`ILE`
分别控制两条线路。未设置 `ILS` 时事件默认走 IT0。

## 4. UART/RS485 当前语义

UART 通道是原始字节流。寄存器 polling 支持 TXE/TC、RXNE/RDR；RX FIFO 为 256 bytes；DMA 是按 byte 的最小 request 路径。

USART2 的 RS485 DE 映射为 PD4，USART3 的 DE 映射为 PB14：

- GPIO 普通输出模式：ODR 手动控制 DE；
- AF7 + USART `CR3.DEM`：抽象为自动发送门控；
- 未连接：发送被禁止；
- 当前没有字符间 idle 时间和完整 `ReceiveToIdle_DMA` event-size 语义；TX 队列溢出仍会丢弃最新字节。

## 5. 当前 host API

C codec/transport：

```c
size_t dm_mc02_frame_encode(uint8_t *out, size_t capacity,
                            const DmMc02Frame *frame);
bool dm_mc02_frame_decode(DmMc02Frame *frame,
                          const uint8_t *wire, size_t wire_len);
bool dm_mc02_validate_frame(DmMc02StreamValidator *validator,
                            const DmMc02Frame *frame);

DmMc02TransportResult dm_mc02_transport_send_frame(
    DmMc02Transport *, const DmMc02Frame *);
DmMc02TransportResult dm_mc02_transport_recv_frame(
    DmMc02Transport *, DmMc02Frame *);
int dm_mc02_transport_fd(const DmMc02Transport *);
```

transport 支持 Unix-domain/TCP、blocking/nonblocking、partial I/O。`WOULD_BLOCK` 时必须重试同一个尚未完成的 send/receive 操作；当前 API 没有 token 来防止调用者误把新 frame 传入同一个 pending send。

Python worker（Null/MuJoCo 使用 uv；ROS2 使用已 source ROS 安装的系统 Python）：

```text
tools/run-worker.sh \
  --cosim <unix-socket> \
  --engine null|mujoco|ros2 \
  [--fdcan <unix-socket>] [--socketcan can0] \
  [--dm-motor INDEX:SLAVE_ID:FEEDBACK_ID] \
  [--rate Hz] [--realtime] [--frames N] [--wait-step-done]
```

统一入口 `tools/run-worker.sh` 会按 `--engine` 选择运行环境：Null 使用
`uv run --project . python`，MuJoCo 使用 `uv run --project . --extra mujoco python`；
ROS2 需要先 source 对应 ROS 发行版，例如 `source /opt/ros/jazzy/setup.bash`，入口再
使用该环境的 `python3`。这样 smoke、性能工具和用户命令共享同一启动契约，也不会把
ROS2 的系统依赖错误地伪装成 uv 项目依赖。

ROS2 IMU 的 `header.stamp` 会在首次收到有效消息时建立 external-time origin；后续
前进的时间戳映射到 worker virtual time。重复、回退或缺失的 stamp 使用固定步长
fallback，确保 QEMU 收到的 frame 时间严格递增。ROS2 reset 后由新 worker session
重新建立 origin；这仍不提供跨进程 ACK 或回放无损保证。

engine 最小契约：`set_motor(index, value)` 接收旧版直接力矩；`set_motor_enabled()` 和
`set_motor_dm()` 接收 DM-MIT 控制；`step(dt)` 返回 `(gyro_dps[3], accel_g[3])`，
`motor_feedback(index)` 返回 position/velocity/effort。NullEngine、MuJoCo 和 ROS2
均执行 `T = T_ff + Kp*(p_des-p) + Kd*(v_des-v)` 并受 enable 和 `--dm-t-max` 限制；
MuJoCo 按 actuator 对应 joint 读取反馈，ROS2 按可配置 `sensor_msgs/JointState`
数组索引读取反馈。ROS2 没有状态消息时退化为零状态，但不会绕过 enable gate。

DM-MIT 默认按 `slave-id + index` 接收控制、按 `feedback-id + index` 发送反馈；需要
非连续 CAN ID 时可重复指定 `--dm-motor index:slave_id:feedback_id`，未指定的索引
继续使用默认连续规则。

worker 的 co-sim、FDCAN 和 SocketCAN 发送端均为非阻塞、有界、保序队列，默认最多缓存 256 个待发送帧。QEMU FDCAN host backend 尚未连接时也会暂存最多 16 个板端输出帧；队列满时按接口策略丢弃并累计诊断计数。有限帧运行结束时会在 1 秒上限内排空。v2 STEP ACK 支持有限超时重发；QEMU 对同一 session 内完全相同的最近一个已成功 STEP 只重发 ACK，不重复注入数据；严格消费模式还会重发最近完成的 `STEP_DONE`。该机制不覆盖断线后的无损恢复或排空握手；QEMU 对已接收的 future-timestamp IMU burst 会在 peer 关闭后继续交付，新连接或 machine reset 才清理旧 session。

worker backend 的加载接口与 QEMU wire 解耦：

```text
--backend MODULE:FACTORY
--backend-registry MODULE:INITIALIZER --engine NAME
```

`FACTORY(config)` 接收 worker 的 `argparse.Namespace` 并返回 plant backend；
`INITIALIZER(registry)` 向本地 `BackendRegistry` 注册一个或多个名称。注册表实例
按 worker 创建、不会共享全局状态；重复名称、空名称、未知名称和不可调用目标均明确
失败。backend 至少实现 `step(dt)`、`set_motor(index, value)`、
`reset_motor(index)`、`set_motor_enabled(index, enabled)`、
`set_motor_dm(index, command)` 和 `motor_feedback(index)`；可选 `reset()`、`close()`，
或 `imu_timestamp_ns`（后者启用外部时间戳映射）。直接 factory 优先于 registry
initializer，二者不能同时指定。`run-worker.sh` 仍按 `--engine` 选择 uv、MuJoCo
extra 或已 source 的 ROS2 Python；自定义 backend 不改变 v1/v2 协议和时钟 owner。

## 6. v2 plant/control interface（当前最小实现）

目标是使 QEMU、MuJoCo、Gazebo 和录放系统共享同一时钟/执行器边界。v1 仍是默认协议；
v2 通过同一个 chardev 的版本字段分流，不改变已有 v1 payload。

### Step exchange

```text
StepHeader {
  u16 version = 2;
  u16 kind;                 // RESET, STEP, STEP_ACK, DIAGNOSTICS, RESET_ACK, STEP_DONE, TELEMETRY, MOTOR_STATE
  u32 payload_len;
  u64 step_id;
  u64 t_sim_ns;
  u64 dt_ns;
  u32 flags;                // session_id; zero keeps legacy v2 mode
}
```

完整 v2 frame 为 `u32 body_len`、`u32 magic`、上述 36-byte header 和 payload。当前 QEMU
支持的 host->QEMU 消息为 `RESET`、`STEP`、`DIAGNOSTICS`，QEMU 返回 `RESET_ACK`、
`STEP_ACK`、`STEP_DONE`、`TELEMETRY`、`MOTOR_STATE` 和 diagnostics。每条连接只能选择一个版本；v2 RESET
后不能在同一 session 混入 v1 frame，v1 RESET 也不能降级一个已经建立的 v2 session。

单一 owner 推进仿真时钟：plant 在 `t_sim_ns` 计算状态，QEMU 只消费带有严格递增
`step_id/t_sim_ns` 的输入。当前 QEMU 使用 RESET 时刻建立 host-time 到 QEMU virtual-time
的映射，STEP 会进入已有 IMU 有界队列；v2 worker 还使用 RESET_ACK 的 `qemu_time_ns`
把 QEMU FDCAN timestamp 映射到 worker step epoch，并在发送回 QEMU 时做反向映射。
`STEP_ACK` 表示样本已被 QEMU 接收并排队或立即
交付，不代表 guest 已经完成 SPI 读取。对需要 guest-consumption 栅栏的运行，`STEP_DONE`
会使用与对应 `STEP_ACK` 相同的 `step_id/t_sim_ns/dt_ns`，并在 guest 完整读取 accel 与
gyro raw data burst 或 FIFO frame 后发送。`tools/run-worker.sh --protocol v2 --wait-step-done`
会等待该二阶段响应；默认不开启以兼容不发送 `STEP_DONE` 的旧 endpoint。禁止依赖 host wall
clock 作为物理时间；worker 的 `--realtime` 只是 pacing policy。

当前 `STEP` 支持两种 payload：无扩展输入时为兼容的 60-byte `ImuSampleV2`；需要附加
输入时为 `section_count:u16, reserved:u16` 加重复的
`type:u16, flags:u16, length:u32, payload[]`。当前 QEMU 消费 IMU section、ADC_INPUT
section（8 bytes）和 ADC_PIN_VOLTAGE section（12 bytes）；worker 的
`--protocol v2 --adc-input/--adc-voltage` 会生成这些 section。QEMU 使用 IMU 中的 gyro/accel
六个物理 float，bias、sensor-time 和 sample-id 作为 plant 元数据保留，板级 QEMU
sensor noise/bias 仍由 machine 属性控制。若 link 安装了 motor callback，STEP 还可以携带
一个 `MotorCommand` section，并同步返回同一 step 的 `MOTOR_STATE`；默认 board machine
没有 callback，因此该 section 返回 `STEP_ACK(UNSUPPORTED)`。默认电机 CAN 控制仍走
FDCAN 通道。

section header 的 `flags` 当前是保留字段，必须为零；它不能替代
`MotorCommand.flags`。后者位于 MotorCommand payload 内，由 endpoint 自行定义，QEMU
link 会原样传递而不解释。

当前 ACK payload：`RESET_ACK` 为 `status:u32, capabilities:u32, qemu_time_ns:u64,
queue_depth:u32, reserved:u32`；`STEP_ACK` 为 `status:u32, queue_depth:u32,
imu_dropped:u32, reserved:u32`。diagnostics response 为五个 little-endian `u64`：
`rx_frames, rx_bad_frames, rx_dropped_bytes, tx_frames, tx_dropped`。status 为零表示成功，
非零表示当前 step 未被接受。`STEP_DONE` 为
`status:u32, consumed_mask:u32, missing_mask:u32, queue_depth:u32, dropped_count:u32`；
当前 accel bit 为 bit 0、gyro bit 为 bit 1，成功时 `consumed_mask=3`、`missing_mask=0`。

### Actuator/sensor messages

```text
MotorCommand {
  u16 motor_count;
  u16 flags;                // endpoint-defined command metadata
  repeated { u16 index; f32 command; };
}

MotorState {
  u16 motor_count;
  repeated { u16 index; f32 position; f32 velocity; f32 effort; };
}

ImuSampleV2 {
  f32 gyro_dps[3];
  f32 accel_g[3];
  f32 gyro_bias_dps[3];
  f32 accel_bias_g[3];
  u64 sensor_time_ns;
  u32 sample_id;
}
```

电机 command 不再隐式编码为 CAN ID + 前 4 bytes float；CAN 映射作为 adapter policy 配置（base ID、端序、缩放、周期、反馈 ID），这样同一 plant 可直接连接 MuJoCo/Gazebo joint controller。

host-side `DmMotorBusAdapter` 负责 CAN wire 到协议中立命令/状态的映射，
`StepCoordinator` 负责按 `virtual_time_ns` 排序并在统一的 plant step 边界应用命令。
backend 不应直接解析 CAN 帧；QEMU 默认仍通过 FDCAN 通道验证真实板级协议。

### Reset/diagnostics

标准 reset 顺序：

```text
controller -> RESET(session_id) -> plant reset -> QEMU board reset
           <- RESET_ACK(initial_time, capabilities, power_policy)
controller -> STEP/inputs
```

当前最小实现的 `RESET_ACK` 返回能力位图、QEMU 时间和 IMU 队列深度；完整的时钟模式、
power policy 和各通道计数仍待补齐。QEMU machine reset 会向已建立的 v2 session 发送新的
RESET/RESET_ACK。v2 session 要求先 RESET，并拒绝旧 step 的序号/时间；worker 对 STEP ACK
执行有限重试，QEMU 对最近一个成功 STEP 提供幂等 ACK。断线后的 session 恢复仍未实现。
需要 guest 消费栅栏时使用 `STEP_DONE`，它已覆盖 BMI088
direct raw burst 和 FIFO data frame 的完整读取。

当前能力位定义为：bit0=`STEP`，bit1=`IMU`，bit2=`DIAGNOSTICS`，bit3=`ADC`，
bit4=`TELEMETRY`，bit5=`MOTOR`（仅安装 motor callback 时声明）。
QMP `cosim-diagnostics` 分别报告 `imu_dropped` 和 `adc_dropped`，两者均为有界队列
溢出计数，不代表 guest 已消费或 plant 已处理；`tx_queue` 和
`telemetry_pending` 可用于判断板级状态是否仍在等待发送。telemetry 的 pending 状态
不改变 v1/v2 wire 格式。

当前 status 定义为 `OK=0`、`QUEUE_FULL=1`、`PROTOCOL=2`、`UNSUPPORTED=3`。
v2 header 的 `flags` 字段承载 session ID；RESET 建立 session，后续 frame 必须回显相同
ID，旧或错误 ID 会被拒绝。包含
包含 `MotorCommand` 的合法 STEP 在未安装 callback 时返回
`STEP_ACK(UNSUPPORTED)`；安装 callback 后，状态通过同一 step 的 `MOTOR_STATE`
响应返回。最近一次成功 STEP 的状态会随幂等 ACK 一起重放；这不覆盖断线后的无损恢复。
开发和协议验收可使用 `-machine dm-mc02,cosim-motor-loopback=on` 获得确定性的
`MOTOR_STATE`，该 fixture 不代表真实电机动力学。

## 7. 能力分级

| 等级 | 含义 |
|---|---|
| Functional | 寄存器/数据面能使目标固件路径运行 |
| Timed | 时钟树、timer、DMA request 与 timestamp 可用于时序测试 |
| Electrical | 供电、收发器、GPIO/AF、电气门控有明确行为 |
| Validated | 有针对真实板卡/外部模型的验收数据 |

当前 QEMU 主要处于 Functional；CPU/SysTick 的当前配置接近 Timed。BMI088
已经提供带虚拟时间戳的部分采样/ODR/FIFO、参数化噪声和漂移数据面，但其完整
器件时序、引脚中断和真实统计标定仍未达到 Timed/Electrical/Validated。通用外设时钟、reset
和电源也只在各自已覆盖的子集内具备相应等级。后续新增能力必须在 capability
probe 和测试报告中标明等级。
## Reusable layering

The QEMU integration is split into three practical layers:

- Chip models (`dm_mc02_bmi088_signal`, `dm_mc02_bmi088`, `dm_mc02_dma`, `dm_mc02_adc`, `dm_mc02_uart`, `dm_mc02_fdcan`,
  `dm_mc02_tim2`, and the other `dm_mc02_*` modules) own register behavior,
  virtual-time state, and generic callbacks. They do not know DM-MC02 pin names,
  serial slot numbers, or DMA stream assignments.

The DMA model exposes `dm_mc02_dma_request()` for one peripheral event and
`dm_mc02_dma_request_batch()` for a bounded run of equivalent events. The batch
API preserves one address-space transaction per item, including peripheral
MMIO side effects, while resolving the DMAMUX/endpoint set once and updating
level-sensitive stream IRQ outputs at the batch boundary. It is appropriate
for throughput-oriented device timers such as UART TX; consumers that require
an interrupt-visible boundary after every event must use the single-request
API. The FIFO/DME extension is deliberately scoped to the reusable stream data
path: `SxFCR` retains `FTH/DMDIS/FEIE`, `FS` reports the occupancy of a four-word
byte FIFO, and direct-mode width mismatch sets `DMEIF` and clears `EN`;
`DMEIE` gates only the IRQ output. If more than one enabled stream matches one
request and peripheral address, the DMA controller arbitrates that event using
`SxCR.PL[17:16]`; higher priority wins and the lower stream number breaks
ties. A batch repeats that arbitration for every item, so one peripheral event
is never broadcast to multiple streams. FIFO mode packs/unpacks 1/2/4-byte
memory and peripheral beats, drains at the configured threshold, preserves
level-sensitive HT/TC state, and supports circular reload. The state-only
`dm_mc02_dma_advance_stream()` path models FIFO M2P occupancy without inventing
a peripheral side effect; P2M requires `dm_mc02_dma_request()`. Exact FEIF
conditions, burst bus timing, and full error recovery remain unsupported.

In DBM mode, `M0AR` and `M1AR` are exposed as configuration bases while the
currently advancing address is kept in a private cursor. A running stream may
replace an `MxAR` base; changing the inactive target also resets that target's
private cursor, so the next `CT` transition starts at the new address without
disturbing the active transfer. `NDTR`, `CT`, HT/TC flags, and endpoint callback
ownership remain in the DMA model. The verified boundary is the basic
peripheral-request path; FIFO plus DBM endpoint staging is covered by
`dm_mc02_dma_fifo_dbm_endpoint_smoke`, including retry/fill consistency.
peripheral-request path; FIFO plus DBM endpoint staging is covered by
`dm_mc02_dma_fifo_dbm_endpoint_smoke`, including retry/fill consistency.

The reusable `DmMc02DmaEndpoint` contract is an alternative peripheral boundary
for devices that should not be represented as an MMIO address:

```c
typedef bool DmMc02DmaEndpointRead(void *, uint8_t *, unsigned, uint64_t);
typedef bool DmMc02DmaEndpointWrite(void *, const uint8_t *, unsigned, uint64_t);
typedef struct DmMc02DmaEndpoint {
    DmMc02DmaEndpointRead *read;
    DmMc02DmaEndpointWrite *write;
    void *opaque;
} DmMc02DmaEndpoint;

bool dm_mc02_dma_request_endpoint(DmMc02Dma *, const DmMc02Dmamux *,
                                  uint32_t request_id, hwaddr peripheral_addr,
                                  const DmMc02DmaEndpoint *,
                                  uint64_t timestamp_ns);
```

The `peripheral_addr` remains the configured request-routing identity, while
the callback is the data producer/consumer at the boundary. P2M calls `read`
and M2P calls `write`; callback success is required for the beat to commit and
the timestamp is the request's virtual time. The initial contract is synchronous.
Direct mode deliberately requires equal peripheral/memory widths; FIFO mode
may perform the DMA model's supported 1/2/4-byte packing or unpacking and
invokes the endpoint once per peripheral beat. Endpoint callbacks remain
synchronous and must report success before the corresponding DMA beat commits.
`dm_mc02_dma_endpoint_smoke` verifies this board-independent callback contract;
it does not instantiate the QEMU DMA device or link against QEMU's private
composition archives. Controller-plus-device integration is verified through
the relevant guest smoke fixture.

ADC1 is the first real consumer. Its P2M endpoint reads the current regular
`ADC_DR` value and applies the same EOC/`AUTDLY` data-consumption side effects as
a guest MMIO read. The DM-MC02 machine enables this path by default and exposes
`adc-dma-endpoint=off` as an explicit compatibility switch to the old
address-space MMIO path. Both paths retain DMA ownership of request routing,
stream arbitration, NDTR/address progression, circular reload, and HT/TC flags.

SPI2 is the second real consumer. `dm_mc02_spi` is a reusable, board-independent
SPI data-path module: it owns SPI register state, a bounded target table,
chip-select mask routing, and optional TX/RX DMA endpoint callbacks. It does
not know BMI088 registers or co-simulation state. A target implements the
`DmMc02SpiTarget` callback contract in `dm_mc02_spi_target.h`; `transfer()` is
one byte with a virtual timestamp, and `select()` observes every CS mask
change, including an invalid multi-select mask. Only a single selected target
receives transfer bytes; no selected target or a multi-select mask returns zero
data in this functional model.

`dm_mc02_bmi088_spi` is the reusable BMI088 bus adapter above that boundary. It
owns command/read/write framing, accel's read dummy byte, register
auto-increment, FIFO streaming addresses, and the direct/FIFO frame-consumed
notification. It delegates register and sample state to `DmMc02Bmi088` and
does not own GPIO, DMA, QEMU link, or board pin mapping. The DM-MC02
composition root supplies the SPI instance's DMA channel tuple, DMAMUX request
IDs, peripheral addresses, and board GPIO selection, then binds two BMI088
adapter targets to the SPI target table.

`dm_mc02_spi_reset()` resets the core register/DMA state and deasserts the
selected mask; the board reset path also resets both BMI088 adapters and chip
objects explicitly. This keeps target-specific state out of the SPI core and
makes reset ordering visible at the composition boundary. The optional TX
timer is required for a configured deferred DMA TX path; the current DM-MC02
SPI2 instance enables it, while SPI1 is configured without DMA channels.

The `spi-dma-endpoint` machine property defaults to `on`; `off` selects the
legacy MMIO path for compatibility. `tools/run-spi2-dma-smoke.sh on|off` uses
the same guest fixture for both modes, while `dm_mc02_bmi088_spi_smoke` tests
the adapter without QEMU SPI or board wiring.

UART1 and USART2 are endpoint consumers for byte-wide DMA. A direct P2M
endpoint reserves one RX FIFO head byte without consuming it; the DMA
destination transaction decides whether that byte is committed. FIFO P2M and
the legacy MMIO compatibility path retain immediate consumption. The endpoint
does not own DMA registers or host chardev framing. The machine property
`uart-dma-endpoint` defaults to `on`; `off` selects the legacy MMIO path. UART
TX may submit a bounded batch, but every byte still passes through the DMA
stream's normal address and status transition. `tools/run-uart-dma-smoke.sh`
and `tools/run-uart2-dma-smoke.sh` are the controller-plus-UART boundary tests
and must be run in both modes.

`peripheral_addr` is a fixed endpoint identity for request routing. For a
stream with `PINC`, DMA matches requests against the captured initial `PAR`
(`reload_par`) but performs each beat at live `SxPAR`. An endpoint callback is
invoked only while that live address equals the fixed endpoint identity. After
`PINC` moves to another address, the beat uses normal address-space MMIO, so a
device callback cannot consume an unrelated register address. `NDTR`, `PAR`,
completion flags, and error behavior still belong to DMA even when the callback
is not selected for that beat.

This contract does not provide asynchronous scheduling or rollback of external
callback side effects. For FIFO M2P, the DMA path rolls back newly staged
speculative bytes on `RETRY` so its committed FIFO/cursor state remains paired.
A consumer must reject unsupported beat sizes or failed callbacks
without claiming a committed beat; a failed FIFO callback also abandons staged
FIFO bytes when the stream is disabled. Basic DBM inactive-buffer reconfiguration
is covered by `run-dma-dbm-reconfigure-smoke.sh`; FIFO plus DBM endpoint staging is
covered by `dm_mc02_dma_fifo_dbm_endpoint_smoke`, including retry/fill consistency.

`DmMc02Bmi088Signal` is the signal-processing portion of the BMI088 model. It
accepts physical accel/gyro samples with virtual timestamps and produces the
filtered, biased/noisy physical values or saturated 16-bit sensor codes. It
owns no SPI registers, FIFO bytes, QEMU timers, GPIO, or board wiring. The
`DmMc02Bmi088` composes that signal layer with the BMI088 die's register,
sampling, raw-register, FIFO, temperature, soft-reset, and virtual-time state.
It exposes `dm_mc02_bmi088_sample()`, register read/write operations, and
`DmMc02Bmi088ReadEvent`. The read event reports only completion of a complete
FIFO data frame and its `fifo_sample_sequence`; it does not contain a board or
co-simulation token. FIFO config/skip/sensortime records do not report a data
frame completion event. This keeps the chip model independent of SPI framing,
DMA routing, GPIO, and co-simulation protocol state.

`dm_mc02_bmi088_is_powered()` derives the die state from the device registers:
the accelerometer requires active `PWR_CONF` plus `PWR_CTRL.ACC_EN`, while the
gyro rejects `LPM1` suspend/deep-suspend modes. `dm_mc02_bmi088_sample()` returns
false and leaves raw/FIFO sample data unchanged while the die is off; turning a
die off also clears its DRDY bit. This is register-level sampling gating only,
not an analog power ramp, current model, or power-domain timing model.

The board composition owns the current outer `consume_step_id` and connects
the BMI088 adapter's frame-consumed callback to the co-simulation link. On a
complete direct raw burst or FIFO data frame it passes that outer token to the
link; the token is not taken from `fifo_sample_sequence`. `STEP_DONE`
therefore means that the guest consumed the visible raw/FIFO data associated
with that outer step. It does not require a new ODR-accepted sample on every
step. The SPI core and BMI088 chip model remain independent of this token.

The BMI088 implementation is intentionally a functional/partial device model:
accel FIFO INT tag, sample-drop frame, FIFO interrupt and real DRDY pin
mapping, gyro external tag, and exact watermark/full interrupt timing remain
unsupported. The signal layer provides configurable independent noise, static
bias, ODR acceptance, first-order bandwidth filtering, temperature drift around
25 C, per-axis bias random walk, and raw saturation. Complete filter
discrete-response/group-delay calibration, real-device statistical calibration,
and a dynamic physical temperature source are not implemented.
- The board profile (`qemu/upstream/hw/arm/dm_mc02_board.[ch]`) owns static
  address and wiring data: memory bases, UART/FDCAN slots, DMAMUX request IDs,
  DMA controller selection, RS485 DE pin/AF, and peripheral-to-NVIC IRQ vectors.
  `dm_mc02.c` is the composition root that instantiates models and applies this
  profile.
- Host adapters (`cosim/`, `tools/dm_mc02_sim_worker.py`, and QEMU chardev
  endpoints) own framing, transport, engine selection, and external simulator
  policy. They do not bypass guest peripheral register paths.

The intended extension point for another board is a second
`DmMc02BoardProfile` returned by a board-specific constructor. New chip models
should expose a narrow callback/API surface and must not acquire board tables.
The profile is static data and the hot path still uses direct callbacks and
fixed-size queues; this keeps the abstraction from adding per-sample allocation
or dynamic dispatch overhead.

Each timer, UART, and FDCAN route stores its own NVIC vector alongside its base
address and channel metadata. EXTI, ADC, and DMA retain SoC-level IRQ maps
because those vectors describe shared controller outputs rather than a
per-instance route. `board-profile` can select a registered profile before
machine initialization and becomes read-only after composition starts. The
board module owns a static profile registry; adding a same-SoC board extends
that registry without changing chip-model code or the host adapters.

The profile keeps independent interrupt fields for TIM2 and the auxiliary timer
instances. `irqs.tim2` is the TIM2 vector, while `irqs.timer[i]` corresponds to
`timers[i]`; index zero is TIM1 and index one is TIM3 on the DM-MC02 profile,
therefore they map to IRQ25 and IRQ29 respectively. Board reset code consumes `DmMc02BoardPin` entries rather than hard-coded
GPIO banks or pins.

The co-sim RX path is a bounded linear buffer with a read cursor. Consuming a
frame advances the cursor without copying the remaining bytes; compaction only
happens when a new chardev chunk cannot fit at the tail. This is an internal
performance detail and does not change v1 framing, ordering, or drop counters.

## Current correction set

The DMA peripheral-request path treats `DBM=1` as double-buffer circular mode
even when `CIRC=0`, matching STM32H7 behavior. ADC12 common `CCR` writes are
distinguished from ADCx `CR` writes despite both using offset `0x08`, and
`ADRDY` is only visible after `ADEN` and is cleared by `ADDIS`/stop. These paths
have dedicated QTest or bare-metal coverage.

## QEMU USB adapter

`DmUsbQemuAdapter` is a synchronous QEMU `USBDevice` wrapper with two explicit
boundaries. `dm_usb_qemu_adapter_set_callbacks()` installs the bulk/data
transaction submit and optional bus-reset callbacks. The submit callback owns
the lower device/transport and receives a `timestamp_ns` sampled from QEMU
virtual time for each data transaction. The bus-reset callback uses the same
virtual clock through the reusable `DmUsbHostPort` boundary. The submit
callback returns `DmUsbTransactionResult`, which the adapter maps to QEMU
`USB_RET_SUCCESS`, `USB_RET_NAK`, `USB_RET_STALL`, or `USB_RET_IOERROR`.

`dm_usb_qemu_adapter_set_control_callback()` installs the control request
callback:

```c
typedef DmUsbControlResult DmUsbQemuControlSubmit(
    void *opaque, const DmUsbControlRequest *request,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length, uint64_t timestamp_ns);
```

QEMU's `USBDeviceClass.handle_control` calls this callback once for the
complete request-level operation. For an IN request, `in_data` has capacity
`request->length` and `actual_length` is the returned payload length; for an
OUT request, `out_data` contains exactly the complete data stage and
`actual_length` must remain zero. The callback is synchronous, and its
arguments are borrowed only for the duration of the call. The adapter rejects
missing callbacks, malformed 16-bit request fields, over-capacity payloads,
and a callback result that violates the direction/length contract. Control
`ACCEPTED`/`STALL`/`INVALID` map to QEMU success/stall/I/O error respectively.

The reusable `dm_usb_control_execute_request()` helper drives one complete
request through the existing `DmUsbControlDevice` state machine. It is the
intended consumer for a request callback backed by the local control core; it
does not create a second control state machine or expose QEMU packet phases.

The adapter supports Full-Speed control and bulk packets with a 64-byte maximum packet
size. It deliberately does not advertise High-Speed until a separate configuration can
provide matching endpoint MPS and lower-layer semantics. QEMU generic control handling
is intentionally consumed as one request-level callback; the adapter does not synthesize
lower SETUP/DATA/STATUS transactions. This is a synchronous compatibility boundary, not
an asynchronous USB host-controller implementation.

The QEMU `USBDevice.data_buf` is used as the short-lived request/packet scratch buffer.
Both callbacks must return before the next adapter operation; nested adapter handlers are
rejected while a callback is active. There is no thread-safe access contract, completion
callback, cancel path, SOF scheduler, PHY model, or host enumeration.

The adapter's QEMU ownership contract is covered by a test-only concrete QOM host with
one Full-Speed `USBPort`. The caller installs callbacks and a selected port path before
`usb_realize_and_unref()`; QEMU then owns the realized device through the `USBBus` and
the port reset callback moves it to address 0/default state. Teardown must unparent the
device before unregistering its port, releasing the bus, and unparenting the stack bus.
This fixture proves generic QEMU bus lifecycle and address-0 routing only. It is not a
DM-MC02 machine attachment, a host-controller transport, or host-machine USB support.

`DmUsbDwc2Device` may be the lower transaction consumer without changing this adapter:
the submit callback forwards the immutable `DmUsbTransaction` to
`dm_usb_dwc2_submit()`, and the bus-reset callback forwards to
`dm_usb_dwc2_bus_reset()` with the adapter's virtual timestamp.
Endpoint arming, FIFO contents, transfer counters, completion bits and IRQ policy remain
owned by the DWC2 device-mode core. The adapter must not access DWC2 endpoint fields or
DM-MC02 MMIO directly. The tested boundary is synchronous QEMU packet -> transaction ->
DWC2 FIFO/register state; it is still not a QEMU host-controller schedule or board USB
transport.

### STM32H7 OTG host channel and transaction transport

`DmStm32H7OtgHost` now also owns the minimal H723 DWC2 host-channel register
contract. It remains a chip-layer model: the twelve channels use real DWC2
offsets `HCCHAR(n)=0x500+0x20*n`, `HCINT(n)=0x508+0x20*n`,
`HCINTMSK(n)=0x50c+0x20*n`, and `HCTSIZ(n)=0x510+0x20*n`; `HAINT=0x414`,
`HAINTMSK=0x418`, and `GINTSTS.HCINT` bit 25 summarize masked channel
interrupts. The controller still has no DM-MC02 address, NVIC, QOM, USBPort,
FIFO, DMA, PHY, or board-role dependency.

```c
void dm_stm32h7_otg_host_set_channel_start(
    DmStm32H7OtgHost *, DmStm32H7OtgHostChannelStart *, void *);
bool dm_stm32h7_otg_host_service_channel(DmStm32H7OtgHost *, unsigned,
                                         uint64_t timestamp_ns);
void dm_stm32h7_otg_host_complete_channel(
    DmStm32H7OtgHost *, unsigned,
    DmStm32H7OtgHostChannelCompletion, uint32_t actual_length);
void dm_stm32h7_otg_host_complete_channel_with_token(
    DmStm32H7OtgHost *, unsigned, uint64_t completion_token,
    DmStm32H7OtgHostChannelCompletion, uint32_t actual_length);
```

After the guest programs `HCTSIZ` and raises `HCCHAR.CHENA`, an enabled,
powered, connected and reset-released port produces at most one
`DmStm32H7OtgHostChannelRequest`. The request contains the channel number,
address, endpoint, endpoint type/direction, MPS, packet-sized transfer length,
remaining packet count, PID and caller-supplied virtual timestamp. The channel
and its nonzero `completion_token` are waiting until its consumer calls
`complete_channel()` or the token-checked variant. Accepted packets
decrement `HCTSIZ.XFERSIZE/PKTCNT`; a short packet or exhausted count raises
`XFRC|CHHLTD` and clears `CHENA`. Control/bulk NAK raises `NAK` but leaves the
channel serviceable; interrupt/isochronous NAK, STALL and transaction error
halt the channel. `HCINT` is W1C and its mask drives `HAINT`, `HAINTMSK`,
`GINTSTS.HCINT`, `GINTMSK` and `GAHBCFG.GINT` in that order.
`dm_stm32h7_otg_host_advance_time()` consumes the caller's monotonic virtual
timestamp after a reset-released port starts the frame clock: Full/Low-Speed
advance once per 1 ms and High-Speed once per 125 us. Every frame increments
`HFNUM.FRNUM`, raises the W1C `GINTSTS.SOF` source and services eligible
channels through the same public `service_channel()` boundary. Reset assert,
disconnect and PWR-off stop the frame clock; PWR restore requires a new port
reset release before it starts again.

`DmUsbHostChannelTransport` is the direct reusable consumer of that request
and the producer for `DmUsbTransaction`:

```c
void dm_usb_host_channel_transport_init(
    DmUsbHostChannelTransport *, DmStm32H7OtgHost *,
    DmUsbHostChannelTransportSubmit *,
    DmUsbHostChannelTransportReadOut *,
    DmUsbHostChannelTransportWriteIn *, void *);
void dm_usb_host_channel_transport_init_with_opaques(
    DmUsbHostChannelTransport *, DmStm32H7OtgHost *,
    DmUsbHostChannelTransportSubmit *, void *submit_opaque,
    DmUsbHostChannelTransportReadOut *, void *read_out_opaque,
    DmUsbHostChannelTransportWriteIn *, void *write_in_opaque);
void dm_usb_host_channel_transport_set_route(
    DmUsbHostChannelTransport *, DmUsbHostChannelTransportRoute *, void *);
void dm_usb_host_channel_transport_set_completion_scheduler(
    DmUsbHostChannelTransport *,
    DmUsbHostChannelTransportScheduleCompletion *, void *);
```

It maps SETUP, IN and OUT packets and transaction results synchronously by
default, while
caller-provided `read_out` and `write_in` callbacks own the still-unmodelled
PIO/DMA data path. One 2047-byte reusable packet buffer (the DWC2 MPS limit)
is sufficient because the transaction and any accepted IN data are completed
inside the start callback. DATA0/DATA1 map exactly to the generic transaction
PID; DATA2/MDATA are currently passed as AUTO and must not be treated as a
complete isochronous contract. This adapter has no board wiring and can later
be paired with a PIO FIFO, DMA memory mover, fixture, or QEMU USB transport.
The original initializer remains a compatibility wrapper which assigns its one
opaque pointer to all three callbacks. `init_with_opaques` gives submit, OUT
reader and IN writer independent contexts, which is required when a transaction
consumer and a PIO/DMA selector have different owners. It changes neither
ordering nor the synchronous/non-reentrant lifetime of the packet buffer.

`DmUsbHostChannelTransportScheduleCompletion` is an optional terminal boundary:

```c
typedef void DmUsbHostChannelTransportScheduleCompletion(
    void *opaque, DmStm32H7OtgHost *host, unsigned channel,
    uint64_t completion_token,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length);
```

When installed, the transport invokes it after the transaction result has been
validated and accepted IN data has been copied through `write_in`; the callback
owns delivery of exactly one completion, immediately or later, by calling
`dm_stm32h7_otg_host_complete_channel_with_token()`. It receives only stable
values, including the nonzero token, and the host object, never the stack
request or reusable packet buffer. A token mismatch after channel reset, halt,
or reuse is ignored by the host. When absent, the transport keeps the original
immediate completion behavior. The hook is a scheduling boundary only: it does
not provide a QEMU timer, thread, locking, cancellation, queue ownership, or
automatic timestamp progression.

`DmUsbHostChannelTransportRoute` is an optional, board-independent routing
boundary:

```c
typedef DmUsbTransactionResult DmUsbHostChannelTransportRoute(
    void *opaque, uint8_t device_address,
    const DmUsbTransaction *transaction);
```

When installed, it receives the exact `HCCHAR.DEVADDR` from the same immutable
channel request and takes precedence over the legacy `submit` callback. The
legacy callback remains the behavior when no route is installed, including for
nonzero device addresses. Neither form changes the synchronous transaction or
packet-buffer lifetime. `DmUsbTransaction` deliberately remains address-free:
the address belongs to host-controller routing, while token/endpoint/PID/data
remain reusable device-transport semantics.

The focused controller test is `test-dm-stm32h7-otg-host` (`15/15`): defaults,
port gate, request fields/timestamps, multi-packet state, NAK retry, halt, W1C,
global IRQ summary, SOF/HFNUM intervals, port-power frame stop, PIO word order
and controller-reset FIFO clearing are asserted. The immediate transport test
is `test-dm-usb-host-channel-transport` (`10/10`): exact bulk OUT bytes, short
IN data/readback, NAK-to-STALL mapping, FIFO-full error, oversized IN result
rejection, legacy single-target compatibility, exact device-address routing,
and DMA/QEMU-memory paths are asserted. It also defers one accepted completion
and rejects a stale token before verifying register/IRQ state on later delivery
(`12/12`).
Neither test creates a host-role DM-MC02 machine or proves global FIFO
allocation/arbitration, DMA, enumeration, OTG, USB bus topology, PHY timing,
passthrough, or a timer-backed asynchronous scheduler.

### STM32H7 synchronous host control scheduler

`DmUsbHostChannelControl` is a small H723 host-controller client for fixtures
and integration tests. It is independent of QEMU USB objects, board wiring and
device policy, but deliberately depends on the real reusable H723 host-channel
register contract rather than inventing a parallel transaction path:

```c
void dm_usb_host_channel_control_init(DmUsbHostChannelControl *,
                                      DmStm32H7OtgHost *, unsigned channel,
                                      uint16_t ep0_max_packet_size);
DmUsbHostChannelControlResult dm_usb_host_channel_control_transfer(
    DmUsbHostChannelControl *, uint8_t device_address,
    const uint8_t setup[8], const uint8_t *out_data, size_t out_length,
    uint8_t *in_data, size_t in_capacity, uint64_t timestamp_ns);
```

It drives one selected channel synchronously through `HCTSIZ`, `HCCHAR`,
`HCINT` and that channel's PIO FIFO. Every stage has `PKTCNT=1`: SETUP uses
the H723 SETUP PID, data begins at DATA1 and alternates per packet, and the
opposite-direction zero-length status stage uses DATA1. OUT is placed in the
controller's exact-byte FIFO helper before `CHENA`; IN is read back only after
the accepted completion reports its exact length. A completion is decoded from
the controller's `HCINT`, not directly from a transport return value.

The helper validates the USB setup-direction/length relationship and bounds
`device_address` to `0..127` before encoding `HCCHAR.DEVADDR`. It emits every
stage at the supplied address. Consequently a standard `SET_ADDRESS` call
runs both setup and status-IN at address 0; only a later call can use the new
address after the device-side status completion has committed it. It does not
own port reset, an address allocator, descriptor interpretation, NAK retry,
asynchronous completion, topology, hub requests, global FIFO arbitration,
DMA, PID/toggle fidelity beyond its programmed channel fields, PHY/VBUS, or a
DM-MC02 host board profile.

`test-dm-usb-host-channel-control` (`4/4`) covers multi-packet control IN,
control OUT plus status, address-0-to-13 routing after `SET_ADDRESS`, and
rejection before an out-of-range address can corrupt adjacent `HCCHAR` bits.
The QEMU integration case uses the real `DmUsbHostQemuPort` lifecycle plus
port router to complete `GET_DESCRIPTOR -> SET_ADDRESS -> GET_STATUS`; it
proves QEMU address 0 becomes unreachable only after status-IN and address 13
is then routed through `usb_find_device()`.

### STM32H7 host PIO FIFO

`DmUsbHostPioFifo` is a board-independent, fixed-capacity byte ring used by the
host-controller PIO boundary. It owns no DWC2 register policy, QEMU object,
DMA address, board route or transport callback:

```c
size_t dm_usb_host_pio_fifo_write(DmUsbHostPioFifo *, const uint8_t *, size_t);
size_t dm_usb_host_pio_fifo_read(DmUsbHostPioFifo *, uint8_t *, size_t);
bool dm_usb_host_pio_fifo_read_exact(DmUsbHostPioFifo *, uint8_t *, size_t);
```

Its capacity is exactly 2048 bytes. `write` and `read` transfer as much as
fits or is available; `read_exact` returns false without consuming any byte
when the requested packet is incomplete. Reset clears the logical queue and
does not require zeroing its storage. This lets a PIO OUT packet wait for all
bytes before a synchronous host transaction consumes it.

`DmStm32H7OtgHost` currently composes one OUT FIFO and one IN FIFO for each of
its twelve channels (48 KiB per host instance). `HCFIFO(n)` is exposed at
`0x1000 + 0x1000*n`; 32-bit guest writes append four little-endian bytes to the
channel OUT stream, and 32-bit reads consume up to four bytes from its IN
stream, returning zero in unavailable high-order bytes. The host reset clears
all streams. The direct helpers accept one to four bytes for focused consumers
and tests, but the current register API is word PIO only.

```c
bool dm_stm32h7_otg_host_read_out_fifo(void *, unsigned, uint8_t *, uint32_t);
bool dm_stm32h7_otg_host_write_in_fifo(void *, unsigned,
                                       const uint8_t *, uint32_t);
```

Those callbacks are the direct `DmUsbHostChannelTransport` data boundary.
OUT consumes exactly one issued packet or fails, while IN accepts its complete
packet or returns false. A false callback result becomes the channel's
`XACTERR|CHHLTD` completion. The transport is synchronous and has one
2047-byte packet scratch buffer; an accepted result larger than the issued
packet is rejected before the IN writer is invoked. No callback, queue or
buffer is thread-safe or async.

This is intentionally not a claim that the full DWC2 global FIFO architecture
is modeled. RX status pop/push, `GRXFSIZ`, `GNPTXFSIZ`, `HPTXFSIZ`, FIFO flush,
arbitration, HCDMA, cache maintenance and QEMU USB bus attachment remain
outside this contract. A future HCDMA layer must use a separate SoC-memory
boundary rather than access these FIFO internals.

### STM32H7 host HCDMA data path

`DmStm32H7OtgHost` exposes `GAHBCFG.DMAEN` (bit 5) and
`HCDMA(n)=0x514+0x20*n` in the same chip-layer channel register contract.
`HCDMA` is a read/write byte address. On an accepted channel completion, the
controller decrements `HCTSIZ` as usual and increments `HCDMA` by the exact
accepted length only when `DMAEN` is set. PIO operation, NAK, STALL and
transaction error leave the address unchanged. Controller reset clears all
host channel registers and active state, including `HCDMA`.

`DmUsbHostChannelDataPath` is the immediate, board-independent consumer of
that state. It gives `DmUsbHostChannelTransport` a single PIO/DMA selector:

```c
void dm_usb_host_channel_data_path_init(
    DmUsbHostChannelDataPath *, DmStm32H7OtgHost *,
    DmUsbHostChannelMemoryRead *, DmUsbHostChannelMemoryWrite *, void *);
bool dm_usb_host_channel_data_path_read_out(void *, unsigned,
                                            uint8_t *, uint32_t);
bool dm_usb_host_channel_data_path_write_in(void *, unsigned,
                                            const uint8_t *, uint32_t);
```

With DMA disabled, it delegates to the host PIO FIFO contract. With DMA
enabled, OUT reads and IN writes use the current channel `HCDMA` address via
the injected synchronous memory callbacks. The callbacks return false for a
missing binding or failed memory access; transport maps that to
`XACTERR|CHHLTD`. The selector does not advance the address itself, does not
know QEMU `AddressSpace`, cache state, a board memory map, or USB packets.

The controller test is now `14/14`, the data-path isolation test is `3/3`, and
the channel-transport test is `7/7`. They establish byte routing and register
semantics only. `AddressSpace` binding, cache maintenance, AHB burst/error
behavior, `HCDMAB` descriptors, async completion, FIFO arbitration, host bus
composition and board host-role wiring remain outside this interface.

### QEMU binding for the host DMA memory boundary

`DmUsbHostQemuMemory` is the QEMU-only adapter for the generic
`DmUsbHostChannelMemoryRead` and `DmUsbHostChannelMemoryWrite` callbacks. The
caller initializes it with the already-selected system `AddressSpace`; no
machine-global state, board profile, USB port, transport queue, or controller
register is retained outside that supplied pointer:

```c
void dm_usb_host_qemu_memory_init(DmUsbHostQemuMemory *, AddressSpace *);
bool dm_usb_host_qemu_memory_read(void *, uint32_t address,
                                  uint8_t *, uint32_t length);
bool dm_usb_host_qemu_memory_write(void *, uint32_t address,
                                   const uint8_t *, uint32_t length);
```

The adapter uses `dma_memory_read()` and `dma_memory_write()` with
`MEMTXATTRS_UNSPECIFIED`. `MEMTX_OK` is success; any other `MemTxResult`
returns false to the data path, whose direct transport consumer reports
`XACTERR|CHHLTD`. The 32-bit address is the H723 host channel's HCDMA byte
address. The adapter does not advance HCDMA, infer a memory map, allocate
staging storage, retry transactions, model cache maintenance/IOMMU/AHB bursts,
or own asynchronous completion.

`test-dm-usb-host-qemu-memory` (`1/1`) provides a controlled
`address_space_rw()` boundary stub and checks exact transfer direction, address,
length, bytes, and failure mapping. The QEMU-memory transport integration case
makes `test-dm-usb-host-channel-transport` `8/8`; it checks the complete
`transport -> data path -> QemuMemory -> dma_memory_*` path. The isolated stub
does not prove a full QEMU system-memory link or any host-role board
composition; those remain separate system and qtest gates.

### QEMU USB host transports

`DmUsbHostQemuTransport` is the QEMU-only synchronous consumer for a host
transaction callback. It owns only a caller-selected, QEMU-owned `USBDevice *`:

```c
void dm_usb_host_qemu_transport_init(DmUsbHostQemuTransport *, USBDevice *);
DmUsbTransactionResult dm_usb_host_qemu_transport_submit(
    void *, const DmUsbTransaction *);
```

The target must remain attached and in `USB_STATE_DEFAULT` for the complete
call. `SETUP`, `IN`, and `OUT` use the target's QEMU endpoint and synchronous
`usb_handle_packet()` lifecycle. QEMU `USB_RET_SUCCESS`, `USB_RET_NAK`, and
`USB_RET_STALL` map to the corresponding generic transaction result. A target
not ready for packets, malformed transaction shape, any other QEMU result, or
an accepted packet with an invalid actual length returns `INVALID`. When QEMU
returns an inflight packet, the adapter cancels it before cleaning up its
stack-owned `USBPacket` and returns `INVALID`; no async continuation is stored.

The direct form does not advance HCDMA, consume a FIFO, set an IRQ, retain the
transaction timestamp, map DATA PID/toggle, or look up a device address. Device,
port, and bus lifetime remain QEMU caller responsibilities.

The same adapter can instead bind a caller-owned QEMU port and directly satisfy
the reusable channel-route callback:

```c
void dm_usb_host_qemu_transport_init_port(DmUsbHostQemuTransport *, USBPort *);
DmUsbTransactionResult dm_usb_host_qemu_transport_route(
    void *, uint8_t device_address, const DmUsbTransaction *);
```

The route calls `usb_find_device(port, device_address)` and then uses the same
synchronous packet conversion. It never assigns an address itself. A successful
standard, device-recipient `SET_ADDRESS` has already completed its generic
status-IN transaction when `DmUsbQemuAdapter` copies the validated address into
QEMU `USBDevice.addr`; QEMU reset clears it to 0. A host-to-device control
request with no data stage skips data OUT and proceeds directly to status IN.

This transport is QEMU address lookup, not an enumeration or topology
implementation. It does not create a hub, allocate addresses, own a
control-enumeration sequence, propagate DATA PID/toggle, retain async
completion, model PHY/VBUS, or attach a host controller to the DM-MC02
Device-mode window.

`test-dm-usb-qemu-adapter` is now `13/13`. Its cases use a real QOM bus to
verify SETUP/OUT/short-IN and NAK/STALL mapping, pre-reset rejection, async
packet cancellation, address-0 to address-13 routing across a completed
`SET_ADDRESS`, and the immediate PIO composition:
`HCFIFO -> DmStm32H7OtgHost -> DmUsbHostChannelTransport ->
DmUsbHostQemuTransport -> USBDevice -> DmUsbTransactionDevice`. This is a
test-only QEMU composition fixture. Its control-scheduler case additionally
verifies port reset lifecycle, descriptor transfer, address commit and
post-address request routing. It is not a DM-MC02 host machine, a general USB
enumerator, physical USB, or passthrough claim.

### QEMU USBPort host lifecycle

`DmUsbHostQemuPort` is the QEMU-only composition owner for one root
`USBPort`. The caller owns the `USBBus` and `DmStm32H7OtgHost`; the adapter
owns only the registered port and must be cleaned up before its bus:

```c
void dm_usb_host_qemu_port_init(DmUsbHostQemuPort *, USBBus *,
                                DmStm32H7OtgHost *, int index);
void dm_usb_host_qemu_port_cleanup(DmUsbHostQemuPort *);
void dm_usb_host_qemu_port_reset(void *opaque, uint64_t timestamp_ns);
```

The host is initialized with `dm_usb_host_qemu_port_reset` and the adapter as
its port-reset opaque context. QEMU device attach maps negotiated Low, Full,
or High speed to `DmStm32H7OtgHost` port state; detach produces the existing
HPRT0 disconnect semantics. Host-driven `HPRT0.RST` calls `usb_port_reset()`.
QEMU implements that reset using a temporary detach/attach pair, so the
adapter suppresses only those callbacks while the reset is active: the device
is reset to address 0 but its physical connection remains asserted. A later
external detach is still observable as `CONNDET|ENACHG`.

The adapter has no DM-MC02 address, IRQ, pin, MMIO window, device routing,
enumeration scheduler, topology policy, VBUS/PHY model, passthrough support,
or asynchronous completion queue. It is a reusable QEMU composition building
block for a future host-role profile, not a claim that the current DM-MC02
Device-mode port can host USB devices.

`test-dm-usb-qemu-adapter` is now `13/13`; lifecycle coverage asserts initial
attach, HPRT0 W1C/reset behavior, reset address clearing without a false
disconnect, a true detach/re-attach cycle, and controller-driven control
transfers across that lifecycle boundary.

### STM32H7 QEMU host-controller wrapper and reference profile

`DmStm32H7OtgHostQemu` is the QEMU-only `SysBusDevice` composition of the
already reusable `DmStm32H7OtgHost`. It owns the host MMIO wrapper, one sysbus
IRQ output, QEMU virtual-time SOF timer, `USBBus`, one
`DmUsbHostQemuPort`, QEMU address-space DMA callbacks, and the existing
channel transport/router objects. Its public QOM type and MMIO span are:

```c
#define TYPE_DM_STM32H7_OTG_HOST_QEMU "dm-stm32h7-otg-host-qemu"
#define DM_STM32H7_OTG_HOST_MMIO_SIZE 0xd000u
```

The wrapper maps every controller register directly to `DmStm32H7OtgHost` and
keeps the FIFO access width at 1, 2, or 4 bytes. At each guest MMIO access and
at each scheduled QEMU virtual timer expiry, it advances the generic host to
the current `QEMU_CLOCK_VIRTUAL` timestamp and arms the next SOF only while the
port is enabled. It owns no board address, NVIC number, chip pin, external
plant, firmware policy, or descriptor parser. `HPRT0.RST` still enters through
the existing root-port adapter; the wrapper's system reset resets the generic
controller and restores the connection state of an attached QEMU device.

`stm32h723-usb-host` is a deliberately minimal, independent H723 reference
profile. It composes the reusable H723 memory profile, one ARMv7-M CPU, and
one wrapper at the canonical `STM32H723.usb_hs_base = 0x40040000`; this profile
alone maps its IRQ output to NVIC external IRQ 77. It does not instantiate the
DM-MC02 board profile or reuse its USB Device-mode adapter. QEMU virtual USB
devices can be attached through its sole root port, for example:

```sh
qemu-system-arm -machine stm32h723-usb-host \\
    -device usb-kbd,bus=usb-bus.0,port=1
```

The explicit `bus` and `port` binding is required for this one-root-port
fixture. A bare `-device usb-kbd` makes QEMU automatically insert a hub when
the sole port is free; hub topology/enumeration is outside this profile.

The direct composition is:

```text
guest MMIO -> DmStm32H7OtgHost -> channel transport/data path
           -> DmUsbHostQemuTransport -> QEMU USB device
```

`stm32h723-usb-host-test` (`2/2`) uses a real QEMU `usb-kbd` to assert root
port attach/reset, IRQ 77/NVIC pending delivery, virtual SOF progression, and
an EP0 `GET_DESCRIPTOR` setup/data transfer through the mapped FIFO and host
channel registers. It reads `HPRT0.SPD` before advancing the virtual clock, so
the assertion advances exactly one frame at 125 us for High-Speed or 1 ms for
Full/Low-Speed. Release QEMU relink, the adjacent host/controller/adapter
regressions, DM-MC02 USB qtest, and the serial QEMU smoke suite (`80/80`) also
pass. This proves one machine composition and synchronous QEMU
device route, not device enumeration, hubs, generic bulk/interrupt scheduling,
async packets, FIFO arbitration, PHY/VBUS/electrical timing, host USB
passthrough, or any host capability on `dm-mc02`.

### Guest-side STM32H7 EP0 polling client

`firmware/dm_stm32h7_usb_host_control.[ch]` is a small freestanding client of
the H723 host-controller register contract. It has no QEMU, QOM, `USBBus`,
DM-MC02 pin, IRQ, board-profile, descriptor-parser, or address-allocation
dependency:

```c
void dm_stm32h7_usb_host_control_init(DmStm32H7UsbHostControl *,
                                      uintptr_t base, uint8_t channel,
                                      uint16_t ep0_max_packet_size,
                                      uint8_t device_address);
void dm_stm32h7_usb_host_control_set_poll(
    DmStm32H7UsbHostControl *, DmStm32H7UsbHostControlPoll, void *opaque);
bool dm_stm32h7_usb_host_control_port_reset(DmStm32H7UsbHostControl *);
DmStm32H7UsbHostControlResult dm_stm32h7_usb_host_control_transfer(
    DmStm32H7UsbHostControl *, const uint8_t setup[8],
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length);
```

The caller owns the selected channel, the MMIO base, EP0 MPS, address policy,
and all request parsing. `port_reset()` performs the HPRT0 reset pulse and
requires an attached device and rejects an invalid H723 channel, address, or
MPS before touching MMIO; `control_transfer()` emits SETUP, MPS-sized
IN/OUT data packets, and an opposite-direction zero-length status packet
through `HCINT`, `HCTSIZ`, `HCCHAR`, and the channel FIFO. It returns the
observed synchronous completion as `OK`, `NAK`, `STALL`, or transaction error,
or `INVALID` for malformed buffer direction/length or register-field input.
After `actual_length` is non-NULL, the helper initializes it to zero before
starting MMIO work; every non-`OK` result therefore reports zero accepted
bytes, while a successful result reports the accumulated transfer length.
`NAK` is terminal for this one caller-owned attempt and does not retry any
stage internally. The caller owns the retry timing and resubmits the complete
control transfer later.

`DmStm32H7UsbHostControlPoll` is optional. When installed, it is called in the
same caller context immediately before each `HCINT` read, allowing a virtual
controller fixture to advance deterministic state. It must not retain the
request or buffer, and production users can leave it unset. The callback is a
progress hook, not a retry policy or a wall-clock scheduler.
The address remains unchanged by the helper, so a caller must apply a
successful `SET_ADDRESS` only after that transfer returns.

`tools/run-stm32h723-usb-host-smoke.sh` compiles a Cortex-M7 guest and composes
this client with the separate descriptor parser to run
`GET_DESCRIPTOR -> SET_ADDRESS(5) -> GET_STATUS -> GET_CONFIGURATION(header/full)
-> parse -> SET_CONFIGURATION(1)`. The direct QEMU keyboard's status payload
is `0x0000`; it is control data, not a transport result. This is a
guest/MMIO/FIFO/route integration gate only. It does not add generic endpoint
configuration/scheduling, hubs, multi-device topology, NAK retry/timeout,
asynchronous completion, PID/toggle validation, global FIFO/HCDMA descriptors,
PHY/VBUS timing, passthrough, or USB Host mode to `dm-mc02`.

### Freestanding USB configuration descriptor parser

`firmware/dm_usb_host_descriptor.[ch]` is a freestanding, controller- and
board-independent parser for one already-transferred USB configuration
descriptor. It has no MMIO, QEMU, endpoint scheduler, address allocator, or
DM-MC02 dependency:

```c
DmUsbHostDescriptorResult dm_usb_host_descriptor_parse_configuration_header(
    const uint8_t *data, size_t length, DmUsbHostConfiguration *configuration);
DmUsbHostDescriptorResult dm_usb_host_descriptor_parse_configuration(
    const uint8_t *data, size_t length, DmUsbHostConfiguration *configuration);
DmUsbHostDescriptorResult dm_usb_host_descriptor_find_interface(
    const uint8_t *data, size_t length, uint8_t interface_number,
    uint8_t alternate_setting, DmUsbHostInterface *interface_descriptor);
DmUsbHostDescriptorResult dm_usb_host_descriptor_find_endpoint(
    const uint8_t *data, size_t length, uint8_t interface_number,
    uint8_t alternate_setting, uint8_t endpoint_index,
    DmUsbHostEndpoint *endpoint);
```

The header parser accepts the fixed nine-byte configuration header and returns
`wTotalLength`, interface count, configuration value, attributes, and maximum
power. Full parsing requires the supplied byte span to cover the declared
descriptor sequence through `wTotalLength`; zero-length, truncated, or
undersized interface/endpoint descriptors return `INVALID` or `TRUNCATED`.
Interface and endpoint lookup revalidates that sequence, then reports one
explicit interface alternate setting or endpoint ordinal. It neither selects a
configuration nor opens, schedules, or transfers an endpoint.

`dm_usb_host_descriptor_smoke` covers header/full validation, interface and
endpoint lookup, absent entries, an exaggerated total length, and a zero-length
descriptor. The Cortex-M7 host smoke uses the same parser after
`GET_CONFIGURATION` header and full transfers, finds QEMU `usb-kbd` HID
interface `0/0` and interrupt-IN endpoint `0x81` (attributes 3, MPS 8), then
issues `SET_CONFIGURATION(1)`. A bare `-device usb-kbd` is not equivalent to
this direct fixture: QEMU may insert an automatic hub, whose configuration is
a different device contract. Users must bind the keyboard directly to
`bus=usb-bus.0,port=1` for this single-root-port test.

### Freestanding USB endpoint pipe configuration

`firmware/dm_usb_host_pipe.[ch]` converts one parsed non-control endpoint into
a reusable runtime configuration. It depends only on the descriptor types,
not on the H723 register layout, QEMU, a board, a bus speed, or a scheduler:

```c
DmUsbHostPipeResult dm_usb_host_pipe_from_endpoint(
    DmUsbHostPipe *pipe, uint8_t device_address,
    const DmUsbHostEndpoint *endpoint);
```

On success `DmUsbHostPipe` contains the 7-bit device address, endpoint number,
direction, raw endpoint attributes, transfer type, effective MPS, interval,
and `transactions_per_microframe`. The latter is decoded from descriptor
`wMaxPacketSize[12:11] + 1`; this preserves High-Speed interrupt/isochronous
configuration without claiming that the active port actually supports it.

The constructor rejects an address above 127, endpoint zero, reserved endpoint
address bits, zero MPS, reserved `wMaxPacketSize[15:13]`, and a control/bulk
descriptor with nonzero transactions-per-microframe bits. It writes the output
only after all validation succeeds. It deliberately does not validate a
speed-dependent MPS limit, choose a channel, program `HCCHAR`, own toggles, or
submit/retry any transaction.

`dm_usb_host_pipe_smoke` asserts a keyboard interrupt-IN pipe, a three-
transaction isochronous configuration, each public validation boundary, and
no output mutation on rejection. The Cortex-M7 smoke turns the discovered
keyboard endpoint into a pipe before `SET_CONFIGURATION(1)`, checking address
5, endpoint 1, IN direction, interrupt type, MPS 8, and one transaction. This
is descriptor-to-driver configuration only; generic interrupt/bulk transfer
scheduling remains a separate lower-layer gate.

### STM32H7 single-packet pipe client

`firmware/dm_stm32h7_usb_host_pipe.[ch]` is the immediate H723 consumer of a
validated pipe. Its pure encoder provides a host-side gate before any volatile
MMIO access:

```c
DmStm32H7UsbHostPipeResult dm_stm32h7_usb_host_pipe_encode(
    const DmUsbHostPipe *, DmStm32H7UsbHostPipePid, size_t length,
    uint32_t *hcchar, uint32_t *hctsiz);
bool dm_stm32h7_usb_host_pipe_client_init(
    DmStm32H7UsbHostPipeClient *, uintptr_t,
    DmUsbHostChannelAllocator *, const DmUsbHostChannelLease *);
DmStm32H7UsbHostPipeResult dm_stm32h7_usb_host_pipe_transfer(
    DmStm32H7UsbHostPipeClient *, const DmUsbHostPipe *,
    DmStm32H7UsbHostPipePid, const uint8_t *out_data, size_t out_length,
    uint8_t *in_data, size_t in_capacity, size_t *actual_length);
```

The encoder accepts only one packet with explicit DATA0 or DATA1 and one
transaction per microframe. It encodes pipe device address, endpoint number,
type, direction, MPS and payload length into `HCCHAR`/`HCTSIZ` with
`PKTCNT=1`. The transfer client owns no endpoint state: it clears `HCINT`,
writes/reads the selected channel PIO FIFO, enables the channel, waits for
`CHHLTD`, and maps `XFRC`, `NAK`, `STALL`, and transaction error to its result.
It borrows an active generic channel lease: client initialization accepts only
H723 channel IDs `0..11`, and every transfer rechecks that lease before any
MMIO access. The caller retains allocator and lease storage for the client's
lifetime and owns acquire/release, PID/toggle progression, timing and retry
policy. No timeout is synthesized for this synchronous reference fixture.

`dm_stm32h7_usb_host_pipe_smoke` verifies exact keyboard interrupt-IN and
bulk-OUT register words, zero-length data packet encoding, multi-transaction
rejection, unsupported PID rejection, descriptor/type consistency, MPS limits,
an out-of-range generic lease, and a released lease before PIO. The Cortex-M7
smoke configures direct `usb-kbd`, then sends DATA0 interrupt-IN through a
leased channel 1. Its idle `NAK` and recorded channel-1 `HCCHAR` fields prove
the pipe reaches the real H723 channel/QEMU route without pretending that lack
of keyboard input is a successful zero-byte report. This remains a
single-packet PIO boundary, not a periodic interrupt scheduler or a HID driver.

### USB endpoint data-toggle state

`firmware/dm_usb_host_endpoint_state.[ch]` owns the controller-independent
state of one already-configured pipe:

```c
void dm_usb_host_endpoint_state_init(DmUsbHostEndpointState *,
                                     const DmUsbHostPipe *);
DmUsbHostEndpointStateResult dm_usb_host_endpoint_state_prepare(
    const DmUsbHostEndpointState *, const DmUsbHostPipe **,
    DmUsbHostEndpointDataPid *);
DmUsbHostEndpointStateResult dm_usb_host_endpoint_state_complete(
    DmUsbHostEndpointState *, DmUsbHostEndpointCompletion,
    size_t requested_length, size_t actual_length);
```

It starts at DATA0. Successful Bulk or Interrupt packets toggle DATA0/DATA1,
including a short successful packet; NAK and transaction error preserve the
toggle. STALL records the halted state, after which `prepare` and `complete`
return `HALTED` until `clear_halt()` or `reset()` restores DATA0. Isochronous
completions do not use or modify the toggle. The object validates only that an
accepted actual length is not larger than the submitted packet. It deliberately
does not decide whether a short packet completes a higher-level request, issue
CLEAR_FEATURE, select a channel, retry NAK, or schedule a period.

`dm_stm32h7_usb_host_endpoint_transfer()` is the thin H723 adapter. It obtains
the immutable pipe and explicit current PID, invokes the existing PIO packet
client, maps its completion back to the generic endpoint state, and returns the
same transport result. An idle `usb-kbd` interrupt-IN NAK therefore leaves the
guest state at DATA0; this is asserted by the Cortex-M7 smoke. The state smoke
covers accepted/short packet toggle, NAK/error preservation, STALL/halt,
clear-halt/reset behavior, invalid completion length, and isochronous no-toggle.

### USB periodic endpoint eligibility

`firmware/dm_usb_host_periodic_schedule.[ch]` is the time contract between a
validated periodic `DmUsbHostPipe` and a future controller scheduler. It owns
no MMIO channel, QEMU timer, board profile, endpoint PID, retry budget, or
transfer result:

```c
DmUsbHostPeriodicScheduleResult dm_usb_host_periodic_schedule_init(
    DmUsbHostPeriodicSchedule *, const DmUsbHostPipe *, DmUsbHostSpeed,
    uint64_t origin_ns);
bool dm_usb_host_periodic_schedule_eligible(
    const DmUsbHostPeriodicSchedule *, uint64_t timestamp_ns);
bool dm_usb_host_periodic_schedule_advance(
    DmUsbHostPeriodicSchedule *, uint64_t timestamp_ns);
```

The caller chooses a virtual-time origin, which is the first eligible slot.
`eligible()` is read-only; after a poll is actually submitted, `advance()` moves
to the first slot strictly after that timestamp. Thus a virtual-time jump makes
at most one poll eligible and never replays missed slots. It returns false and
does not change state before the due slot.

The constructor copies the pipe and validates the speed-dependent periodic
rules: High-Speed interrupt/isochronous `bInterval` is `1..16` and maps to
`125 us * 2^(bInterval - 1)`; Full-Speed interrupt is `1..255 ms`; Low-Speed
interrupt is `10..255 ms`; and Full-Speed isochronous is exactly one frame.
Low-Speed isochronous and multi-transaction Full/Low-Speed configurations are
rejected. The pipe constructor remains speed-neutral, so these rules belong
here rather than in descriptor parsing.

`dm_usb_host_periodic_schedule_smoke` verifies exact High-Speed microframe and
Full/Low-Speed frame intervals, isochronous rules, invalid combinations, and a
late poll that skips to the next future slot. It is a freestanding gate only;
the next direct-consumer gate is to pass actual H723 SOF timestamps to a host
driver without changing the current DM-MC02 USB Device-mode board role.

### USB periodic poll composition

`firmware/dm_usb_host_periodic_poller.[ch]` combines one schedule with one
endpoint state through a controller-neutral submit callback. It is the owner of
the sequence `eligible -> submit one packet -> endpoint completion -> advance`:

```c
typedef DmUsbHostPeriodicSubmitResult DmUsbHostPeriodicSubmit(
    void *, const DmUsbHostPipe *, DmUsbHostEndpointDataPid,
    const uint8_t *, size_t, uint8_t *, size_t,
    DmUsbHostEndpointCompletion *, size_t *);
DmUsbHostPeriodicPollResult dm_usb_host_periodic_poller_poll(
    DmUsbHostPeriodicPoller *, uint64_t timestamp_ns,
    const uint8_t *, size_t, uint8_t *, size_t,
    DmUsbHostEndpointCompletion *);
```

Before the due timestamp it returns `NOT_DUE` and does not call the submitter.
The submitter may return `DEFERRED` when no packet was submitted because a
temporary resource is unavailable; the poller then returns `POLL_DEFERRED`
without changing endpoint state or advancing the schedule. A submitted NAK
consumes that poll slot but retains DATA PID; accepted packets apply the
existing toggle state; and a submitted STALL consumes the slot and halts the
endpoint. A halted endpoint does not invoke the callback. An invalid submit
does not mutate endpoint state or schedule. The callback is a generic
pipe/PID/bytes boundary, so it can be implemented by a different controller
without depending on H723 MMIO.

`firmware/dm_stm32h7_usb_host_periodic.[ch]` is the thin H723 implementation
of that callback. It holds only the caller's MMIO base and generic channel
allocator. For each due callback it acquires one lease using the periodic
object as owner, binds a short-lived one-packet PIO client, maps
`OK/NAK/STALL/XACTERR` into the generic endpoint completion, and releases the
lease after the synchronous transfer returns. If no channel is available it
returns `DEFERRED`, leaving the poller's endpoint and schedule unchanged. It
owns no timer, SOF source, descriptor parser, retry policy or board
configuration.

The default initializer binds the real H723 PIO transfer. The explicit
`dm_stm32h7_usb_host_periodic_init_with_transfer()` variant accepts the same
H723 client transfer signature as an injection boundary for another H723 data
path or a deterministic fixture; it changes neither channel ownership nor
poller semantics. A transfer callback is invoked while its temporary lease is
active, and the adapter releases that lease before returning to the poller.
The Cortex-M7 reference smoke obtains the real keyboard pipe, initializes the
adapter at virtual origin zero, submits its first poll, then calls it at the
same timestamp. The first result is the real idle NAK and the second is
`NOT_DUE`; PID remains DATA0. Because the 64-bit late-slot computation needs
the ARM EABI division helper, its freestanding smoke link explicitly includes
compiler-provided `libgcc`.

### USB SOF virtual-time source

`firmware/dm_usb_host_sof_clock.[ch]` converts an observed 16-bit SOF frame
counter to monotonically increasing virtual nanoseconds. It is independent of
MMIO, QEMU, board profile and endpoint policy. At High-Speed each counter tick
is one 125-us microframe; at Full/Low-Speed it is one 1-ms frame. The unsigned
16-bit delta preserves normal `HFNUM.FRNUM` wraparound, and the driver must
reinitialize the clock after a port reset rather than infer a reset from a
counter value.

`firmware/dm_stm32h7_usb_host_sof.[ch]` is the H723 register adapter. `init()`
snapshots `HPRT0.SPD` and `HFNUM.FRNUM` at a caller-selected origin;
`timestamp()` reads only `HFNUM` and advances the generic clock. It neither
enables SOF interrupts nor programs host channels. The reference guest now
initializes this source after configuration, performs an initial periodic NAK,
observes the same-timestamp defer, waits until the reconstructed timestamp
reaches `next_slot_ns`, and receives a second real keyboard NAK. This verifies
the virtual SOF timestamp path without making the controller parse `bInterval`.

### H723 periodic SOF-event adapter

`firmware/dm_stm32h7_usb_host_periodic_sof.[ch]` connects an initialized
`DmStm32H7UsbHostSof` source to an already-configured
`DmUsbHostPeriodicPoller`. Its `on_event()` method reads one current
`HFNUM.FRNUM` timestamp and submits at most one packet through the generic
poller. The adapter owns no endpoint state, descriptor, PIO client, IRQ
enablement, QEMU timer, board route or retry policy.

Initialization rejects a source/poller link-speed mismatch, keeping the
`HFNUM` tick duration and periodic `bInterval` interpretation consistent. The
H723 guest polls `GINTSTS.SOF`, W1C-clears each observed status bit, and calls
this adapter once per event. It records an initial NAK, same-slot `NOT_DUE`,
then a second NAK only after enough actual SOF events have occurred. An IRQ
handler can invoke exactly the same adapter later; that wiring is deliberately
separate from this single-channel polling event loop.

### Periodic endpoint registry

`firmware/dm_usb_host_periodic_registry.[ch]` is a controller-independent,
fixed-capacity collection of periodic pollers sharing one USB link speed. An
entry copies a poller pointer, caller-owned IN/OUT buffer span, and optional
completion callback. The caller keeps those buffers valid until removal:

```c
bool dm_usb_host_periodic_registry_add(
    DmUsbHostPeriodicRegistry *, const DmUsbHostPeriodicRegistryEntry *);
unsigned dm_usb_host_periodic_registry_dispatch(
    DmUsbHostPeriodicRegistry *, uint64_t timestamp_ns);
```

Registration rejects a duplicate poller, a full registry, and a link-speed
mismatch. One dispatch checks each active entry once; `NOT_DUE` does not enter
the endpoint submit path. Submitted completions invoke the optional callback.
Submitted STALL, a halted poller, or an invalid submit removes that entry, so a
terminal endpoint does not remain on every SOF hot path. Removal is explicit
for disconnect/reconfiguration and uses an unordered compact array; no memory
allocation, queue or lock is involved.

`firmware/dm_stm32h7_usb_host_periodic_registry_sof.[ch]` is the H723 event
adapter. It verifies the source and registry speeds match, then dispatches the
registry at the source's current `HFNUM` timestamp. The reference guest
registers the discovered keyboard pipe after its first NAK and drives it from
W1C-cleared `GINTSTS.SOF`; the next due SOF reports the second NAK through the
registry callback. The generic registry and H723 adapter each have isolated
fake-submit/fake-MMIO smoke coverage.

### H723 SOF IRQ boundary

`firmware/dm_stm32h7_usb_host_sof_irq.[ch]` is the last H723-specific driver
step before firmware interrupt policy. Its handler adapter only reads
`GINTSTS`, returns when `SOF` is absent, W1C-clears an observed `SOF`, and
dispatches the existing registry event. It does not configure `GAHBCFG`,
`GINTMSK`, NVIC, channel interrupts, retry, DMA, or endpoint metadata.

The reference guest owns those outer actions: it installs IRQ 77 at vector
index `16 + 77`, clears stale NVIC pending state, enables `GINTMSK.SOF` and
`GAHBCFG.GINT`, then enables `NVIC_ISER2` bit 13 and waits with `wfi`. Its SOF
handler invokes the adapter; the smoke reaches the next due keyboard NAK only
from that IRQ path. This leaves the same adapter available to any H723 board
profile while preserving board-specific IRQ routing outside the driver.

### USB host channel lease allocator

`firmware/dm_usb_host_channel_allocator.[ch]` owns the smallest reusable
resource boundary between endpoint scheduling and a controller's finite host
channels. It copies a caller-provided, unique list of arbitrary channel IDs;
the IDs have no H723, DWC2, QEMU, board or endpoint meaning in this layer:

```c
bool dm_usb_host_channel_allocator_init(
    DmUsbHostChannelAllocator *, const uint8_t *channels,
    unsigned channel_count);
bool dm_usb_host_channel_allocator_acquire(
    DmUsbHostChannelAllocator *, const void *owner,
    DmUsbHostChannelLease *);
bool dm_usb_host_channel_allocator_lease_active(
    const DmUsbHostChannelAllocator *, const DmUsbHostChannelLease *);
bool dm_usb_host_channel_allocator_release(
    DmUsbHostChannelAllocator *, DmUsbHostChannelLease *);
```

Capacity is a compile-time bound of 16 entries. Initialization accepts an empty
set and rejects an oversized, missing or duplicate nonempty set without
changing the allocator. A successful reinitialization is a lifecycle boundary:
callers must discard prior leases and rebind consumers before acquiring again.
Acquisition assigns the first free channel in initialization order to a
non-null caller-owned identity; an exhausted allocator leaves the output lease
unchanged. A lease carries the channel ID, owner identity and a per-entry
generation. `release()` requires all three fields to still match, then clears
the allocator entry and zeroes the caller's lease, preventing a copied stale
lease from releasing a newer assignment in the same allocator lifetime.

This is a synchronous caller-serialized ownership helper, not a transfer
scheduler. It does not program `HCCHAR`, choose retry/timeout behavior, manage
FIFO/DMA, define port reset behavior or configure an interrupt. The next H723
driver slice may consume a verified lease instead of a raw channel number;
that adapter must preserve this generic ownership boundary and add a direct
controller/guest integration gate separately.

### USB bulk transfer composition

`firmware/dm_usb_host_bulk.[ch]` composes a configured bulk endpoint into a
sequence of synchronous, controller-neutral packet submissions. It consumes an
existing `DmUsbHostEndpointState` and a callback that submits exactly one
packet:

```c
typedef DmUsbHostBulkSubmitResult DmUsbHostBulkSubmit(
    void *, const DmUsbHostPipe *, DmUsbHostEndpointDataPid,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, DmUsbHostEndpointCompletion *, size_t *actual_length);

bool dm_usb_host_bulk_transfer_init(
    DmUsbHostBulkTransfer *, DmUsbHostEndpointState *,
    DmUsbHostBulkSubmit *, void *);
DmUsbHostBulkTransferResult dm_usb_host_bulk_transfer_run(
    DmUsbHostBulkTransfer *, const uint8_t *out_data, size_t out_length,
    uint8_t *in_data, size_t in_capacity, bool append_zero_packet,
    size_t *actual_length);
```

The endpoint must already be a validated bulk pipe with one transaction per
microframe. OUT data is split at `max_packet_size`. IN requests are also
bounded by that size and stop on a short accepted packet or when the caller's
buffer is full. For an OUT length that is an exact multiple of MPS,
`append_zero_packet=true` submits one explicit zero-length packet; it is false
by default and never applies to IN.

An accepted packet is passed to `DmUsbHostEndpointState`, so Bulk DATA PID
state changes exactly once per accepted packet. `NAK` returns the number of
bytes accepted earlier in this call and leaves the current packet PID ready for
retry. `DEFERRED` means no packet was submitted and leaves both PID and local
progress unchanged. `STALL` records endpoint halt and returns `STALL` on later
calls until the caller clears the endpoint state. A caller retrying after a
partial result must pass the remaining buffer span; the function does not keep
an asynchronous transaction object or replay accepted packets on its own.
`actual_length` is the accumulated accepted data length for the current call;
the submitter reports packet-level completion and actual length.

`dm_usb_host_bulk_transfer_run()` has no wall-clock retry, allocation, channel
selection, or controller register access. `dm_usb_host_bulk_smoke` covers
multi-packet OUT, IN short termination, explicit ZLP, NAK/DEFERRED progress and
PID preservation, STALL halt, and non-bulk rejection. This boundary is not yet
an H723 bulk adapter, QEMU USB host bus, enumeration path, DMA/FIFO engine,
async completion/cancel path, or DM-MC02 host-mode feature.

### STM32H7 bulk packet adapter

`firmware/dm_stm32h7_usb_host_bulk.[ch]` binds the generic bulk composer to
the existing H723 single-packet PIO client. It keeps the controller-specific
channel lifetime above the generic bulk layer:

```c
bool dm_stm32h7_usb_host_bulk_init_with_transfer(
    DmStm32H7UsbHostBulk *, uintptr_t,
    DmUsbHostChannelAllocator *, DmStm32H7UsbHostPipeTransfer *, void *,
    DmUsbHostEndpointState *);
bool dm_stm32h7_usb_host_bulk_init(
    DmStm32H7UsbHostBulk *, uintptr_t,
    DmUsbHostChannelAllocator *, DmUsbHostEndpointState *);
DmUsbHostBulkTransferResult dm_stm32h7_usb_host_bulk_run(
    DmStm32H7UsbHostBulk *, const uint8_t *, size_t, uint8_t *, size_t,
    bool append_zero_packet, size_t *actual_length);
```

Each packet submission acquires a temporary lease using the adapter object as
the owner, binds that active lease to `DmStm32H7UsbHostPipeClient`, invokes the
injected packet callback (or the real PIO callback), and releases the lease
before returning. An exhausted allocator maps to generic `DEFERRED`; it does
not submit a packet and the generic endpoint/bulk state remains unchanged.
The lease is therefore not valid across packets and cannot support an async
completion; an async controller adapter needs a separate ownership contract.

`dm_stm32h7_usb_host_bulk_smoke` is the direct adapter gate. It verifies OUT
MPS packetization, accepted IN packets, partial NAK with remaining-span retry,
active lease observation and release for every packet, resource exhaustion,
and STALL halt. The default initializer is the only path that binds real PIO;
the smoke's injected callback is a test seam and does not represent QEMU USB
bus attachment or DM-MC02 host support. The next consumer gate must use a
reference host profile with an explicitly configured bulk endpoint and assert
real `HCCHAR/HCTSIZ/HCFIFO/HCINT` and packet lengths before adding a guest
multi-packet test.

### STM32H723/QEMU bulk direct consumer

`tools/run-stm32h723-usb-host-bulk-smoke.sh` is the first direct consumer of
the adapter. It starts the independent `stm32h723-usb-host` reference profile
with QEMU's `usb-serial` on its root port. The Cortex-M7 guest discovers the
configuration, selects interface 0, builds the 64-byte bulk OUT pipe for
endpoint 2, and calls the default H723 bulk adapter with a 130-byte payload.
The QEMU file chardev is the external consumer oracle and must contain exactly
bytes `0..129`. The guest also records the final channel-1 register state and
checks that the endpoint has toggled DATA0 -> DATA1 over three accepted
packets and that the temporary channel lease is released.

This smoke proves the synchronous path
`descriptor -> pipe/state -> H723 PIO -> H723 host model -> QEMU USB device ->
chardev`; it does not prove individual packet tracing, bulk IN injection,
NAK retry policy, asynchronous completion/cancel, DMA/FIFO scheduling,
multi-device topology, PHY/VBUS behavior, passthrough, or DM-MC02 USB Host
wiring. The script is automatically included by
`tools/run-qemu-smoke-suite.sh` through its `run-*-smoke.sh` discovery.

## STM32H723 ADC regular sequence registers

The ADC regular sequence boundary decodes the H723 register layout without
depending on the DM-MC02 board profile. `SQR1` fields `SQ1..SQ4` select ranks
1..4 at shifts `6, 12, 18, 24`; `SQR2` fields `SQ5..SQ9` select ranks 5..9 at
shifts `0, 6, 12, 18, 24`; `SQR3` fields `SQ10..SQ14` select ranks 10..14 at
the same shifts; and `SQR4` fields `SQ15..SQ16` select ranks 15..16 at shifts
`0, 6`. Each field is six bits wide; only the low five bits are a channel
number. The sequence length is `SQR1.L + 1`, with the H723 four-bit length
field allowing 1..16 ranks.

Any MMIO write overlapping SQR1, SQR2, SQR3 or SQR4, including byte and half-word
writes, selects the register-backed regular sequence. Conversion consumes the
selected channels in rank order and exposes each result through the existing
`DR/EOC/EOS` boundary. The decoder does not change ADC timing, channel-source
ownership, DMA policy, IRQ wiring or board pin mapping.

The component intentionally does not claim ADC2/common shared state or complete
analog/electrical behavior. The direct regressions are
`/dm-mc02/adc/regular-sequence-sqr2-sqr3` and
`/dm-mc02/adc/regular-sequence-sqr4`; their consumers check all 14/16 `DR`
values and the terminal `EOS` transition.

# 0.19 Reusable STM32H723 IWDG VMState contract

`dm_mc02_iwdg_vmstate()` returns the version-2 state description for the
board-independent IWDG register component. Its producer is the CPU-visible
register mirror, watchdog control state and any deferred configuration update:

```text
regs[DM_MC02_IWDG_REGION_SIZE / 4], boot_grace_pending, write_unlocked,
started, next_timeout_ns, pending_{pr,rlr,winr}, update_deadline_ns[3]
```

`next_timeout_ns` is an absolute monotonic virtual-nanosecond deadline. It is
updated whenever the watchdog is started or reloaded and is cleared on reset,
timeout, or stop. `QEMUTimer`, its owner, `MemoryRegion`, LSI frequency,
boot-grace configuration, and monotonic diagnostic counters are runtime or
configuration state and are not serialized. `regs[SR]` is part of the
serialized register image: its `PVU/RVU/WVU` bits identify which pending
value/deadline pairs are active. A successful post-load invokes
`dm_mc02_iwdg_sync_runtime()` only after checking committed and pending PR/RLR/
WINR masks, each SR/deadline pair, the started/deadline invariant, and signed
`QEMUTimer` timestamp ranges. It restores both the watchdog and deferred-
update timers; an already due deadline is rearmed at the destination's current
virtual time so restore cannot silently extend a watchdog window or lose a
pending register commit. Version 1 remains load-compatible: it has no deferred
state, so the loader normalizes SR and all v2 fields to inactive before the
same validation.

The public lifecycle is:

```c
void dm_mc02_iwdg_sync_runtime(DmMc02Iwdg *state);
const VMStateDescription *dm_mc02_iwdg_vmstate(void);
```

The component is independently tested by `test-dm-iwdg-vmstate` (`9/9`) and
the direct H723 consumer is covered by `tools/run-iwdg-smoke.sh`. This is a
component contract only: the DM-MC02 machine does not register it, so it does
not establish whole-machine snapshot, save/load, migration, LSI settling,
or watchdog reset-reason support.

## 0.6 STM32H7 D2 APB and timer clock boundary

`qemu/upstream/hw/arm/dm_stm32h7_clock_tree.[ch]` provides the reusable D2
divider boundary. `D2CFGR.D2PPRE1` (bits 6:4) and `D2PPRE2` (bits 10:8) use
the H723 encodings `/1` for 0..3, then `/2`, `/4`, `/8`, and `/16`. The
helper exposes these dividers, APB1/APB2 PCLK frequencies, and the two timer
kernel frequencies:

```c
uint64_t dm_stm32h7_apb1_hz(uint64_t hclk_hz, uint32_t d2cfgr);
uint64_t dm_stm32h7_apb2_hz(uint64_t hclk_hz, uint32_t d2cfgr);
uint64_t dm_stm32h7_apb1_timer_clock_hz(
    uint64_t hclk_hz, uint32_t d2cfgr, uint32_t cfgr);
uint64_t dm_stm32h7_apb2_timer_clock_hz(
    uint64_t hclk_hz, uint32_t d2cfgr, uint32_t cfgr);
```

With `RCC_CFGR.TIMPRE=0`, a timer uses PCLK for APB `/1` and `2*PCLK`
otherwise. With `TIMPRE=1`, it uses HCLK for APB `/1`, `/2`, and `/4`, and
`4*PCLK` for `/8` and `/16`. The helper is board and QEMU independent; it does
not select a kernel source or model clock settling.

`DmMc02PwrRcc` exposes the same effective values through
`dm_mc02_pwr_rcc_apb{1,2}_clock_hz()` and
`dm_mc02_pwr_rcc_apb{1,2}_timer_clock_hz()`. A write touching `D2CFGR` or
`CFGR.TIMPRE` invokes the existing clock-change callback. DM-MC02 consumes the
timer values through two QEMU clocks. `DmMc02BoardTimerRoute.clock_domain`
selects APB1 or APB2 for each board timer; the profile owns only this routing
data and does not duplicate clock-tree arithmetic.

The effective source identity remains selected when that source later becomes
unready, but its derived APB and timer rates become `0 Hz`; the board consumer
must propagate that value instead of retaining a stale clock. The read-only
machine QOM properties `apb1-timer-clock-hz` and `apb2-timer-clock-hz` expose
the two propagated rates for diagnostics and deterministic test oracles only.
The properties do not create a second clock source or alter timer behavior.

The isolated gate is `dm_stm32h7_clock_tree_smoke`; the direct boundary gate is
`/dm-mc02/tim2/apb-timer-domains-independent`. The latter changes APB1 while
TIM2 is running and verifies TIM8/APB2 remains at its prior rate and both
counters retain phase. Reset D2CFGR is APB `/1`, so this model's reset timer
clock is 64 MHz; tests that need the former 32 MHz fixture configure HCLK and
both APB domains explicitly.

`tools/run-clock-zero-smoke.sh` is the direct readiness-loss gate. It requests
an unavailable PLL1, disables the currently effective HSI, and verifies the
APB1 timer clock reaches `0 Hz` through the board boundary. This does not model
automatic source fallback, oscillator settling, or timer bus power gating.

## 2026-09-01 Reusable NOR Flash raw-image persistence adapter

`cosim/dm_nor_flash_persistence.[ch]` is a board-independent host-side adapter
for caller-owned NOR storage. `dm_nor_flash_persistence_load(path, storage,
storage_size)` and `_save(...)` operate on the complete byte array and require
the file length to equal `storage_size` exactly. The file format is therefore a
raw image with exact device geometry: no header, metadata, padding, or
translation layer is added. The adapter does not know about OSPI registers,
QEMU machine state, or Flash command semantics.

The result enum is part of the boundary contract: `OK` means the complete
operation succeeded; `DISABLED` means a null or empty path and no disk I/O was
attempted; `NOT_FOUND` means load found no file and leaves the caller's storage
unchanged; `SIZE_MISMATCH` rejects a non-exact image before loading bytes and
leaves storage unchanged; `IO_ERROR` reports open, seek, read, write, flush, or
close failure; and `INVALID` reports null storage or zero storage size. Callers
normally initialize storage to the erased value before load so `NOT_FOUND` and
invalid configured images retain that state.

`DmMc02Ospi` exposes only thin `dm_mc02_ospi_load_persistence()` and
`dm_mc02_ospi_save_persistence()` wrappers. They pass the OSPI backing storage
and its configured `flash_size` to the reusable adapter; they do not duplicate
file or geometry logic. The DM-MC02 machine's `ospi2-flash-file` property must
be set before board-profile/machine initialization locks the property. During
initialization, a configured non-empty path is loaded after OSPI2 creation; a
missing file is accepted, while other load results warn and retain the erased
image. During the normal-shutdown notifier, a configured non-empty path saves
the complete image and reports failures as warnings. An empty default path is
guarded before these calls and performs no disk I/O.

This interface is lifecycle persistence only. It does not provide real Flash
latency, ECC, crash/abnormal-termination persistence, or power-loss atomicity;
save is performed only on normal QEMU exit and is not an atomic power-fail-safe
commit protocol.

The isolated `dm_nor_flash_persistence_smoke` checks disabled/not-found,
round-trip bytes, exact-size rejection without mutation, and I/O error results.
The direct OSPI boundary smoke checks the 8 MiB W25Q64 raw image loaded at
startup and saved after a normal QMP `quit`; `tools/run-ospi-smoke.sh` then
checks the exact output size and bytes. These tests establish the adapter and
machine lifecycle boundary, not physical persistence behavior.

## OCTOSPI DMA endpoint boundary

`dm_mc02_ospi_dma_endpoint()` exposes the OSPI data path through the reusable
`DmMc02DmaEndpoint` contract. It does not expose the OSPI register window or
DMA state:

```c
DmMc02DmaEndpoint dm_mc02_ospi_dma_endpoint(DmMc02Ospi *state);
```

For P2M, `read()` is valid only after an indirect read command has prepared an
RX queue. It copies exactly the requested beat and rejects zero-length,
queue-exhausting, invalid, or reset state without advancing the queue. For
M2P, `write()` is valid only for an active page-program transaction and stages
the exact peripheral beat through the existing OSPI data path. DLR length,
page-size, command, and reset checks remain owned by OSPI; DMA owns memory
address progression, `NDTR`, status flags, and stream enable state.

The endpoint accepts the caller's `timestamp_ns` as part of the common
callback signature but currently adds no virtual or wall-clock delay. The
endpoint is board-independent and does not select a DMAMUX request or infer a
board route. A real board composition must provide the verified request ID,
PAR identity, and OCTOSPIM routing at the board boundary.

`dm_mc02_ospi_dma_endpoint_smoke` is the isolated endpoint gate.
`dm_mc02_ospi_dma_integration_smoke` is the direct DMA consumer gate: it uses
the real DMA/DMAMUX model with a bounded guest-RAM address-space fixture to
verify page-program M2P, flash-read P2M, address/`NDTR`/TC progression, and
endpoint rejection propagating to TEIF without committing the rejected P2M
beat to RAM. It uses synthetic routing IDs by design and is not evidence that
the current `trobot` firmware enables OSPI DMA.

## DM-MC02 OCTOSPI Flash profile composition

`DmMc02BoardFlashProfile` is board-owned data describing one external Flash
attached to an OCTOSPI instance. It contains the OSPI instance number, device
name, storage/page/sector geometry, JEDEC ID, and whether the board exposes a
memory-mapped capability. It does not contain command parsing or storage
ownership. The DM-MC02 profile maps `OCTOSPI2` to `W25Q64JV` with 8 MiB
storage, 256-byte pages, 4 KiB sectors, and JEDEC ID `ef 40 17`. The
`STM32H723-EVAL` profile intentionally has no external Flash profile.

During machine initialization, the board adapter copies the selected profile
into the reusable `DmMc02OspiFlashConfig`. Production `DmMc02Ospi` uses the
board-independent `DmMc02SsiNor` adapter; the realized QEMU `w25q64`/`m25p80`
device owns its backing storage and command state. The profile only selects
composition. The OSPI register window is present for every profile. A Flash
memory region is added only when the profile has a Flash and advertises memory
mapping, and the guest's `CR.FMODE` still gates access to that region at
runtime. No uninitialized memory region is installed for a profile without
Flash.

Profile validation requires an OSPI instance of 1 or 2, non-zero compatible
geometry, a page size no larger than the bounded OSPI staging buffer, a
non-zero JEDEC ID, unique OSPI ownership, and a valid SoC memory-mapped base
when mapping is requested. Persistence, Flash command timing, line mode/DTR,
DMA, ECC, and option-byte behavior remain outside this composition contract.

The OSPI DLR contract is `transfer_length = DLR + 1`; the raw value is checked
before addition. `0xffffffff` therefore produces `SR.TEF` for read and page
program operations instead of wrapping to an accepted zero-length transfer.
Oversized page programs are rejected atomically at command acceptance and at
the commit boundary; neither invalid path changes backing storage or clears a
previously set WEL. `dm_mc02_board_profile_smoke` is the profile isolation
gate, while `run-ospi-smoke.sh` and `run-board-profile-smoke.sh` are the
direct ARM/QEMU composition gates.

## NOR erase granularity

The following caller-owned `DmNorFlash` API is retained only as an explicit
host test fixture (`DM_MC02_OSPI_TEST_FIXTURE=1`); it is not a production
alternative to QEMU `m25p80`. Within that fixture, `dm_nor_flash_erase()` is
the reusable erase boundary. The caller supplies an
address and an erase size; the size must be non-zero, no smaller than the
configured sector size, an integer multiple of that sector size, and a divisor
of the backing storage size. The address may be inside the target block and is
aligned down to that erase size. A rejected geometry, busy operation,
write-protected operation, or out-of-range address leaves storage unchanged;
an invalid erase size also leaves WEL set so the caller can correct its
request. A successful erase sets synchronous WIP for the operation body,
restores the selected bytes to `0xff`, clears WIP, and consumes WEL.

`dm_nor_flash_sector_erase()` is the configured-sector specialization and
`dm_nor_flash_chip_erase()` erases the complete caller-owned array. These APIs
do not know W25Q opcodes, OSPI registers, board wiring, or persistence. A
future NOR device with different legal granularities can reuse the same core
provided its adapter binds those sizes explicitly.

The DM-MC02 OCTOSPI adapter maps W25Q64 commands `0x20` to sector erase,
`0x52` to 32 KiB block erase, `0xd8` to 64 KiB block erase, and `0xc7` to chip
erase. The first three commands consume the address phase; chip erase is a
no-address command and executes after its instruction phase. The adapter only
translates the command and delegates geometry/status to `DmNorFlash`.
`dm_nor_flash_smoke` is the isolated core gate and `run-ospi-smoke.sh` is the
direct register/board gate. Erase latency, asynchronous WIP, suspend/resume,
power-loss behavior, block protection, ECC, and persistence remain outside
this interface.

## Reusable NOR storage core

`cosim/dm_nor_flash.[ch]` is the board- and QEMU-independent storage semantic
layer used by the OSPI adapter. The producer is a command/framing adapter such
as OCTOSPI or a future SPI target; the boundary is the geometry, status and
operation API; the consumer is the caller-owned byte backing store. The core
does not allocate storage, know a bus line mode, or own a board profile.

`dm_nor_flash_init()` binds caller-owned storage and requires a non-zero
geometry with `storage_size` divisible by both `page_size` and `sector_size`.
`dm_nor_flash_page_program()` requires WEL, accepts at most one page, wraps
bytes at the addressed page boundary, and applies NOR one-to-zero programming.
`dm_nor_flash_sector_erase()` aligns the address down, restores one sector to
`0xff`, and consumes WEL only on success. Invalid length, busy, protection and
out-of-range results leave the backing store unchanged; failed operations do
not clear WEL. Reads outside the array return erased bus data (`0xff`).

The core is intentionally synchronous: WIP is asserted during the operation
body and is clear after the function returns. It does not model erase/program
latency, suspend/resume, power loss, ECC, status-register persistence or
non-volatile file I/O. Those behaviors belong to a timing or persistence
adapter, not to this storage contract.

`dm_nor_flash_smoke` is the isolated gate for initialization geometry, WEL/WIP
status, busy rejection, NOR 1-to-0 programming, page wrap, sector erase,
reset preservation and invalid-length/out-of-range atomicity. OSPI remains the
direct-consumer gate and must continue to pass independently before a generic
SPI Flash framing adapter is added.

## QEMU SSI m25p80 adapter

`DmMc02SsiNor` is a board-independent adapter from an owner-provided QEMU SSI
bus parent to a realized standard `w25q64`/`m25p80` device. Its public
operations are initialization with expected geometry/JEDEC identity,
active-low CS selection, one-byte transfer, and device reset. The adapter does
not implement NOR commands, own backing storage, or expose m25p80 private state.
The realized m25p80 device owns the 8 MiB storage; narrow accessors provide only
the storage pointer, size, and JEDEC identity needed by a memory-mapped alias
and the lifecycle persistence adapter.

`DmMc02Ospi` remains the STM32H723 OCTOSPI register boundary. It translates the
guest `CR`, `DLR`, `CCR`, `IR`, `AR`, `DR`, and `FCR` transactions into SSI byte
transfers and owns only controller-side staging, DLR validation, completion
flags, DMA endpoint cursors, and the gated memory-mapped window. The supported
DM-MC02 path covers JEDEC/status/WREN, page program, normal/fast/quad reads,
4 KiB/32 KiB/64 KiB/chip erase, and raw-image persistence through the shared
storage view. Unsupported line-mode electrical timing, DTR, asynchronous WIP,
ECC and complete OCTOSPI protocol semantics remain outside this contract.

QEMU's m25p80 boundary is locally patched for W25Q-compatible WEL consumption
after successful program/erase and for erase-block address alignment. A
rejected oversized or invalid OCTOSPI transaction does not reach the die and
does not clear WEL. `DM_MC02_OSPI_TEST_FIXTURE=1` is a host-only compatibility
fixture that retains the earlier caller-owned `DmNorFlash` implementation; it
is not a production fallback.

The direct `run-ospi-smoke.sh` gate checks the realized `w25q64` ID and geometry,
read/program/erase behavior, missing-WREN protection, non-aligned erase,
maximum-DLR rejection, memory-mapped reads, DMA endpoint use, and persistence.

## Reusable SPI NOR framing adapter (host test fixture)

`qemu/upstream/hw/arm/dm_mc02_spi_nor.[ch]` is a board-independent, host-only
test-fixture command framing adapter between the byte-oriented
`DmMc02SpiTarget` callback and the
reusable `DmNorFlash` storage core. The adapter does not own backing storage,
Flash geometry, or the SPI core. The caller owns the `DmNorFlash` object and
must keep it valid for the lifetime of the adapter.

The adapter accepts `WREN (0x06)`, `RDSR (0x05)`, `JEDEC ID (0x9f)`, normal
`READ (0x03)`, `FAST READ (0x0b)`, `PAGE PROGRAM (0x02)`, and 4 KiB
`SECTOR ERASE (0x20)`. Addresses are three-byte big-endian SPI addresses.
Normal and fast reads increment through the linear storage address space;
only page program wraps at the configured page boundary, as defined by
`DmNorFlash`. Reads beyond the backing array return `0xff`.

The adapter commits program and erase operations when CS is released. Program
bytes are staged in a 257-byte bounded buffer so a transaction larger than the
maximum 256-byte page is rejected atomically by `DmNorFlash`; the WEL latch and
backing storage remain unchanged on that rejection. A status or JEDEC read
streams its result until CS release. `last_result` reports the most recent
program or erase result and is initialized to `DM_NOR_FLASH_INVALID`.

`dm_spi_nor_flash_target()` returns the target callback pair for the generic
SPI target table. The configured `selected_mask` must match the complete bus
mask; a different mask, including a multi-select mask, enters an ignore state
and cannot execute commands or change WEL. Any CS mask transition terminates
the previous transaction. `dm_spi_nor_flash_reset()` discards framing state
and a partial transaction; non-volatile bytes remain owned by and unchanged
in the `DmNorFlash` core. Timestamp values are accepted at the callback
boundary and currently do not add operation latency.

`dm_spi_nor_flash_smoke` is the isolation gate. It covers JEDEC/status/WEL,
page-wrap program, linear normal and fast reads, NOR program, oversized
program atomic rejection, sector erase, invalid CS masks, and CS-interrupted
transactions. This adapter still does not implement quad/DTR/bit-level SPI
timing, Flash persistence, a full W25Q status register, or asynchronous WIP;
those belong to later, separately tested layers.

`dm_mc02_spi_target_smoke` is the direct consumer gate for the current core
boundary. It registers the adapter in the real `DmMc02Spi` target table and
drives it through the SPI core's `TXDR`/`RXDR` MMIO callbacks and
`dm_mc02_spi_select_mask()`. It verifies JEDEC/status reads, program and
linear readback, CS interruption, and that an invalid multi-select mask does
not execute `WREN`. This proves the core-to-adapter composition, but it does
not yet prove a board-level Flash device profile.

## DM-MC02 SPI CS board route

`DmMc02BoardProfile.spi_cs_routes` is the board-layer source of truth for
software-controlled SPI chip selects. Each `DmMc02BoardSpiCsRoute` maps an
SPI controller and target-table bit to a GPIO pin and records its polarity.
`dm_mc02_board_decode_spi_selected_mask()` reads the GPIO `MODER` and `ODR`
snapshot and returns a mask for one controller; only pins in GPIO output mode
can select a target. An active-low route is selected when its output latch is
low. The caller supplies target capacity so the board layer does not depend on
the SPI implementation's fixed table size.

The DM-MC02 profile maps SPI2 target 0/1 to BMI088 accelerometer/gyroscope CS
on PC0/PC3; the STM32H723-EVAL profile uses PC4/PC5. The machine composes the
existing BMI088 adapters into target bits 0/1 and consumes the decoded mask;
no CS pin or polarity is hard-coded in `dm_mc02_spi.c`. Profile validation
rejects missing/invalid pins and duplicate controller/target routes, and
requires the two BMI088 target routes used by the current machine composition.

This is a software-GPIO route only. Hardware NSS/alternate-function CS,
electrical contention, and board-level SPI Flash composition remain separate
future boundaries.

### H723 synchronous control NAK contract

The reusable H723 control PIO helper treats `HCINT.NAK` as a
caller-visible terminal result for the current synchronous transfer attempt.
It waits for `HCINT.CHHLTD | HCINT.NAK`; when NAK is observed, it requests
`HCCHAR.CHDIS` and waits for `HCINT.CHHLTD` before returning
`DM_STM32H7_USB_HOST_CONTROL_NAK`. The
helper does not advance to the next control stage, report accepted data, or
retry internally. The caller owns any later retry of the complete control
transfer and must provide the scheduling or retry policy.

The deterministic `dm_stm32h7_usb_host_control_nak_smoke` uses a simple
board-independent array MMIO and the optional poll hook to inject one NAK
after the driver's pending-interrupt clear. It checks that no data/status
stage follows, `HCCHAR.CHDIS` is requested, `HCINT.CHHLTD` is observed, and
`actual_length` is zero. The
fixture deliberately does not claim to implement the controller's complete
W1C behavior; that register contract is covered by the H723/QEMU controller
tests. The same smoke also covers excessive `HCTSIZ.XFERSIZE`, missing IN/OUT
buffers, and invalid channel/address/MPS inputs. It passes together with the
affected host CTest `16/16`, three real Cortex-M7/QEMU host smokes, and the
full CTest `45/45`.

### H723 host lifecycle and packet accounting

`DmStm32H7OtgHost` owns the lifetime of an active channel operation. Port
disconnect, `HPRT0.PWR` transition to off, controller reset, and an explicit
`HCCHAR.CHDIS` all cancel a channel that is waiting for an external completion.
The optional `DmStm32H7OtgHostChannelCancel` callback receives the host pointer,
channel number, and the operation's completion token:

```c
typedef void DmStm32H7OtgHostChannelCancel(
    void *, DmStm32H7OtgHost *, unsigned channel, uint64_t completion_token);
```

Cancellation is a lifecycle event, not a USB completion. The host clears the
channel's active/waiting state, token, and `HCCHAR.CHENA`; it does not invent
`CHHLTD`, `XFRC`, `NAK`, or an IRQ. A scheduler or other owner must remove its
matching pending work. Completion delivery is token-checked when the caller
uses `dm_stm32h7_otg_host_complete_channel_with_token()`, so a late event from
an older channel use is ignored without changing registers or endpoint state.
The token is opaque to the transport and must be treated as invalid after
cancellation, reset, disconnect, or channel reuse.

For an accepted packet, `actual_length` is bounded by the issued packet
length. `HCTSIZ.XFERSIZE` decreases by that length and `HCDMA` advances by it
when DMA is enabled. `PKTCNT` decreases by
`ceil(actual_length / max_packet_size)`; a zero-length accepted packet consumes
zero packets and does not toggle the private DATA PID. A short packet or an
exhausted transfer halts with `XFRC`; an accepted full packet with remaining
work leaves the channel available for the next service event. The programmed
`HCTSIZ.DPID` remains the operation's initial value while the private toggle
state advances only for consumed packets.

### Host-channel transport token boundary

`DmUsbHostChannelTransport` converts a board-independent H723 channel request
to one `DmUsbTransaction`. A SETUP transaction is selected only when both
`request->endpoint_type == DM_USB_ENDPOINT_CONTROL` and
`request->pid == DM_STM32H7_OTG_HOST_PID_SETUP`. A bulk, interrupt, or other
non-control request carrying the SETUP PID is still routed by
`HCCHAR.EPDIR`: it becomes IN when `direction_in` is true and OUT otherwise.
This prevents a malformed or reused PID field from changing the transaction
direction at the transport boundary.

The transport copies OUT/SETUP bytes into its bounded packet storage before
submission and copies accepted IN bytes to the caller's FIFO callback before
completion is scheduled. If a completion scheduler is installed, it receives
only stable host/channel/token/result/length scalars and must deliver exactly
one token-checked terminal completion; it must not retain the request,
transaction, or packet pointer. Without a scheduler, the legacy synchronous
completion path remains in use.

The isolated gates are `test-dm-stm32h7-otg-host` (`22/22`) and
`test-dm-usb-host-channel-transport` (`15/15`). They cover zero-length and
short packets, PID/`PKTCNT` accounting, disconnect/power/reset cancellation,
stale completion rejection, and explicit control-endpoint SETUP detection.
These contracts do not imply DMA/FIFO arbitration, retry scheduling, or
DM-MC02 host-role composition.

### H723 completion-driven asynchronous bulk composition

`firmware/dm_stm32h7_usb_host_bulk_async.[ch]` is a reusable H723 layer above
the endpoint state and async channel dispatcher. Its producer is one completed
H723 PIO packet; its boundary is the stable bulk object, dispatcher slot and
completion token; its consumer is the caller's IRQ/event loop. The object keeps
the caller's OUT/IN buffer pointers and cumulative progress stable while one
packet is pending. The caller must keep those buffers valid until the request
is complete or cancelled.

```c
bool dm_stm32h7_usb_host_bulk_async_init(
    DmStm32H7UsbHostBulkAsync *, DmStm32H7UsbHostPipeAsyncDispatch *,
    uintptr_t, DmUsbHostChannelAllocator *, DmUsbHostEndpointState *);
DmStm32H7UsbHostBulkAsyncResult dm_stm32h7_usb_host_bulk_async_start(
    DmStm32H7UsbHostBulkAsync *, const uint8_t *, size_t,
    uint8_t *, size_t, bool append_zero_packet,
    DmUsbHostChannelCompletionToken *);
DmStm32H7UsbHostBulkAsyncResult dm_stm32h7_usb_host_bulk_async_poll(
    DmStm32H7UsbHostBulkAsync *,
    const DmUsbHostChannelCompletionToken *, uint64_t,
    size_t *, DmUsbHostChannelCompletionToken *);
```

The transfer object must be zero-initialized before its first `init()`. A
successful `start()` submits the first packet, and an accepted full packet
automatically starts the next packet through the same dispatcher boundary.
Only accepted bytes advance the endpoint DATA PID and local offset. A short
accepted packet completes the request; an exact OUT multiple can append one
explicit zero packet. Channel exhaustion returns `DEFERRED` with state
`READY`; `resume()` retries the same packet. A NAK also returns `NAK` with
state `READY`, preserving the packet span and DATA PID for an explicit later
resume. `cancel()` stops the pending channel through the dispatcher and
releases its lease.

`poll()` owns the call to
`dm_stm32h7_usb_host_pipe_async_dispatch_handle_channel()` for its leased
channel and must pass its current token. The dispatcher acquires and releases
its configured lock around this call; a caller must not already hold the same
non-reentrant lock. If an IRQ handler has already consumed the channel event,
`poll()` consumes the recorded terminal result without dispatching it again.
It returns `PENDING` when no terminal HCINT is visible, `STARTED` when the next
packet was accepted by the dispatcher, and the terminal packet result
otherwise. The composition intentionally has no
wall-clock wait, allocation, retry budget, DMA/FIFO arbitration, SOF policy,
or QEMU dependency beyond the lower H723 MMIO adapter. Its direct isolated
gate is `dm_stm32h7_usb_host_bulk_async_smoke`: it checks `64+64+2` packet
progress, generation changes, defer/resume, NAK PID preservation and cancel.

The current smoke is a firmware-side boundary test; it does not yet replace
the synchronous QEMU guest bulk smoke or claim controller-internal multi-packet
DMA behavior. The next gate is an actual guest consumer, followed by the
H723 QEMU controller's explicit `PKTCNT/XFERSIZE/HCDMA` continuation contract.

### Real guest asynchronous IRQ consumer

`smoke/stm32h723_usb_host_async_smoke.c` is the direct guest consumer gate for
the deferred-completion composition. It deliberately keeps enumeration
synchronous: it performs port reset, EP0 device/configuration discovery,
endpoint parsing and `SET_CONFIGURATION` against `usb-kbd` before using the
non-control endpoint async API. The async encoder rejects endpoint 0, so
control transfers must not be submitted through this path.

The guest initializes a reusable channel allocator with `{0, 1}`, two stable
`DmStm32H7UsbHostPipeAsync` objects and one
`DmStm32H7UsbHostPipeAsyncDispatch`. It programs both channel `HCINTMSK`
registers, `HAINTMSK` bits 0/1, and the `GAHBCFG/GINTMSK` gates. The vector
table routes external IRQ 77 through index `16 + 77`; the handler calls:

```c
dm_stm32h7_usb_host_pipe_async_dispatch_handle(&dispatch, 0);
```

Both interrupt-IN packets are submitted through
`dm_stm32h7_usb_host_pipe_async_dispatch_start()`. The guest then cancels
channel 0 before opening the global/NVIC gate. `HCCHAR.CHDIS` invokes the QEMU
scheduler's token-matched cancellation and the H723 host produces/clears the
local halt event; cancellation is read directly from the completion state and
is not emitted as a USB completion. Channel 1 remains pending until the QEMU
virtual timer delivers the keyboard's idle NAK. IRQ 77 dispatches that NAK,
clears its HCINT observation, and releases the channel lease and dispatch slot.

`tools/run-stm32h723-usb-host-async-smoke.sh` compiles the freestanding guest
with the existing linker script and polls only the result block for process
startup/termination coordination. It asserts EP0 results, endpoint shape,
nonzero per-operation tokens, channel 0 `CANCELLED`, channel 1 `COMPLETED`
with `NAK`, exactly one IRQ completion, and zero remaining allocator owners and
dispatch slots. The smoke uses the H723 `PRIMASK` lock callbacks around
start/cancel/handle; production callers must provide a matching lock pair and
keep the lock object stable for the same boundary.

This is a real H723/QEMU guest integration gate, not a complete asynchronous
host claim. It still excludes keyboard report injection, multi-packet PIO,
DMA/FIFO arbitration, SOF retry policy, ISO, hubs/multi-device topology,
PHY/VBUS, passthrough and DM-MC02 host-role board wiring.

### STM32H7 IRQ lock adapter

`firmware/dm_stm32h7_irq_lock.[ch]` is the chip-layer implementation for
dispatch boundaries that must serialize a Cortex-M7 main context with a
maskable IRQ. `dm_stm32h7_irq_lock_enter()` saves `PRIMASK` on the outermost
entry and executes `cpsid i`; nested entries only increase `depth`. The final
matching leave restores the saved state with `cpsie i` or `cpsid i`.

The callbacks require a non-null opaque pointer and must use the same lock
object for properly nested enter/leave pairs. `PRIMASK` masks configurable
exceptions only; this adapter is not an RTOS `BASEPRI` primitive, an SMP lock,
or a DMA cache/barrier contract. It is intentionally compiled only into
Cortex-M7 freestanding guests; native host tests should exercise the dispatcher
with their own fake lock callbacks.

### QEMU virtual-clock completion scheduler fixture

`DmUsbHostQemuCompletionScheduler` is a test-only consumer of the
`DmUsbHostChannelTransportScheduleCompletion` callback. Its producer is the
transport's already-decoded completion; its boundary is a fixed array of 12
`QEMU_CLOCK_VIRTUAL` timers; its consumer is
`dm_stm32h7_otg_host_complete_channel_with_token()`. The scheduler stores only
the stable host pointer, channel, nonzero completion token, completion code,
and actual length. It never retains a request, transaction, or packet buffer.

`dm_usb_host_qemu_completion_scheduler_schedule()` uses the configured default
delay. `schedule_after()` accepts an explicit virtual delay. A new completion
for the same host/channel cancels and replaces the previous timer, so channel
reuse cannot leave two terminal events pending for one channel. `reset()`
cancels all active timers without completing their host channels;
`destroy()` additionally frees the timer objects. A timer callback decrements
the pending count before delivering the token-checked completion, preserving
the existing HCINT/HAINT/global IRQ path.

The fixture requires a stable scheduler and host lifetime until all pending
timers are reset, delivered, or destroyed. Its capacity is intentionally
limited to one pending entry per host/channel up to 12 entries; callers that
need a larger topology must define a separate capacity and routing contract.
The scheduler exposes a separate host-channel cancel callback for the
composition that owns it:

```c
typedef void DmStm32H7OtgHostChannelCancel(
    void *, DmStm32H7OtgHost *, unsigned channel,
    uint64_t completion_token);
void dm_stm32h7_otg_host_set_channel_cancel(
    DmStm32H7OtgHost *, DmStm32H7OtgHostChannelCancel *, void *opaque);
void dm_usb_host_qemu_completion_scheduler_cancel(
    void *, DmStm32H7OtgHost *, unsigned channel,
    uint64_t completion_token);
```

The H723 host invokes this callback only when guest `HCCHAR.CHDIS` or host
reset cancels a channel that is waiting for a completion. The QEMU scheduler
matches host, channel, and token, removes only that timer, and never emits a
USB completion for the cancellation. The host then reports the normal
`CHHLTD` register event; other pending channels remain untouched. The callback
must be non-blocking and must not mutate host registers. Composition teardown
clears the callback before destroying the scheduler; reset cancels scheduler
timers before resetting the host.

This boundary does not provide locking, callback dispatch, SOF retry
scheduling, DMA/FIFO arbitration, USB topology, PHY/VBUS, passthrough, or
DM-MC02 USB Host wiring. The isolated gate is
`test-dm-usb-host-qemu-completion-scheduler`; it uses a controlled
`cpu_get_clock()` and advances by deadline deltas, so it does not depend on
wall-clock sleeps.

The QEMU composition device exposes two initialization properties:
`completion-scheduler` (default `false`) selects the virtual-timer boundary,
and `completion-delay-ns` (default `0`) sets its default virtual delay. The
independent `stm32h723-usb-host` reference profile enables the scheduler while
leaving the delay at zero; callers can override the delay before device
realization for a deterministic timing fixture. A zero delay is still a
virtual-timer event and is not a synchronous completion. Device reset first
cancels scheduler timers, and unrealize clears the transport callback before
destroying the scheduler, so no pending callback can outlive its QEMU owner.
The reference qtest overrides the delay to `1000 ns` and verifies the
pre-deadline pending state, simultaneous completion on channels 0 and 1, and
channel 0 cancellation while channel 1 still completes.

### H723 asynchronous host-channel IRQ dispatcher

`firmware/dm_stm32h7_usb_host_pipe_async_dispatch.[ch]` is the first H723
consumer of the controller-independent completion record. It owns a fixed
12-entry channel-to-`DmStm32H7UsbHostPipeAsync` table and stores a copy of the
completion token, so a late event from an earlier operation cannot complete a
reused channel.

The dispatcher reads the H723 interrupt gates in this order:

```text
GAHBCFG.GINT -> GINTSTS.HCINT & GINTMSK.HCINT -> HAINT & HAINTMSK
```

For every registered pending channel in the snapshot it calls the async
adapter with the stored token and caller-supplied virtual timestamp. A pending
poll does not change the operation; a terminal poll clears the table slot and
returns one count. An externally completed or cancelled record is detected by
token validation and its stale slot is removed without touching a new lease.

`dispatch_start()` holds the supplied lock across controller programming and
registration, closing the start-to-owner-map window when the same lock is
also used by the real IRQ boundary. `dispatch_cancel()` and `dispatch_handle()`
use the same lock. Both lock callbacks must be supplied as a pair; leaving
them null is only suitable for an already serialized fixture.

In addition to the global IRQ entry, the dispatcher exposes
`dispatch_handle_channel(dispatch, channel, token, timestamp_ns)`. It consumes
only the matching channel and token, allowing a bulk consumer to poll without
stealing another operation's completion. A terminal `POLL_INVALID` is treated
as an invalid operation: the dispatcher cancels the operation, clears its slot,
and releases its channel lease. The global entry counts only normal terminal
completions; stale-slot cleanup and invalid termination do not inflate that
count. A base mismatch is rejected before H723 registers are touched.

The isolated gates are
`dm_stm32h7_usb_host_pipe_async_dispatch_smoke` and
`dm_stm32h7_usb_host_async_dispatch_contract_smoke`. They use fake MMIO to
verify global/channel masking, no-event stability, three-channel routing,
selected-channel isolation, accepted/NAK completion, explicit cancellation,
stale-slot cleanup, malformed completion cleanup, base validation, and lock
pairing. This interface does not install an NVIC handler, handle SOF or other
global sources, add DMA/FIFO arbitration, or connect a QEMU deferred transport.
The real guest IRQ smoke remains the direct H723 integration gate; this is not
a complete asynchronous USB host claim.

### Completion-driven channel operation

`firmware/dm_usb_host_channel_operation.[ch]` provides a reusable resource
lifecycle above `DmUsbHostChannelAllocator` and below a controller adapter. The
operation object is the allocator owner, so it must remain at a stable address
until its terminal event. Its public lifecycle is:

```c
bool dm_usb_host_channel_operation_init(DmUsbHostChannelOperation *operation);
bool dm_usb_host_channel_operation_begin(
    DmUsbHostChannelOperation *, DmUsbHostChannelAllocator *);
bool dm_usb_host_channel_operation_pending(
    const DmUsbHostChannelOperation *);
DmUsbHostChannelLease *dm_usb_host_channel_operation_lease(
    DmUsbHostChannelOperation *);
bool dm_usb_host_channel_operation_complete(DmUsbHostChannelOperation *);
bool dm_usb_host_channel_operation_cancel(DmUsbHostChannelOperation *);
```

`begin()` acquires one generation-protected lease and enters `PENDING`. The
controller may borrow the lease while pending; `complete()` is the normal
controller completion path and `cancel()` is the cancellation path. Both release
the lease before entering their respective terminal state. A failed begin due to
resource exhaustion leaves the operation unchanged; a pending operation cannot
be begun again or terminalized twice. An operation in a terminal state may be
reused with a later `begin()`.

The operation layer does not dispatch callbacks, take locks, wait, sleep, or
interpret USB completion codes. It is therefore usable by synchronous PIO and
future asynchronous controllers. The current H723 bulk and periodic adapters
keep an operation local for one synchronous packet and complete it before
returning; an asynchronous adapter must store the operation in stable memory and
call complete/cancel from its own event boundary. Reinitializing or moving a
pending operation is outside the contract because the allocator owner identity
would no longer match.

### Controller-independent channel completion record

`firmware/dm_usb_host_channel_completion.[ch]` adds the smallest event-facing
record above the operation lifecycle. It is reusable by an H723 IRQ adapter,
another host controller, or a deterministic fixture without importing QEMU
event-loop types or board policy:

```c
bool dm_usb_host_channel_completion_init(
    DmUsbHostChannelCompletion *);
bool dm_usb_host_channel_completion_begin(
    DmUsbHostChannelCompletion *, DmUsbHostChannelAllocator *,
    size_t requested_length, DmUsbHostChannelCompletionToken *);
bool dm_usb_host_channel_completion_pending(
    const DmUsbHostChannelCompletion *);
bool dm_usb_host_channel_completion_token_active(
    const DmUsbHostChannelCompletion *,
    const DmUsbHostChannelCompletionToken *);
bool dm_usb_host_channel_completion_lease(
    const DmUsbHostChannelCompletion *, DmUsbHostChannelLease *);
bool dm_usb_host_channel_completion_result(
    const DmUsbHostChannelCompletion *, DmUsbHostEndpointCompletion *,
    size_t *, uint64_t *);
size_t dm_usb_host_channel_completion_actual_length(
    const DmUsbHostChannelCompletion *);
uint64_t dm_usb_host_channel_completion_timestamp_ns(
    const DmUsbHostChannelCompletion *);
bool dm_usb_host_channel_completion_complete(
    DmUsbHostChannelCompletion *,
    const DmUsbHostChannelCompletionToken *, DmUsbHostEndpointCompletion,
    size_t actual_length, uint64_t timestamp_ns);
bool dm_usb_host_channel_completion_cancel(
    DmUsbHostChannelCompletion *,
    const DmUsbHostChannelCompletionToken *, uint64_t timestamp_ns);
```

`begin()` retains the channel lease and records the requested packet length;
packet buffers remain owned by the caller and must remain valid until a
terminal event. It also returns a copyable generation token; the token
generation is owned by the completion record, increments on every successful
`begin()` (skipping zero), and is deliberately independent of the allocator's
per-channel lease generation. Completion and cancellation events must supply
that token, so a delayed event from an older use cannot terminate a reused
record, including when the new use is assigned a different channel.
`token_active()` is the non-mutating check for an event adapter.
`complete()` validates the result and actual length before releasing the lease,
then records the exact controller result, accepted byte count and supplied
virtual timestamp. An invalid completion leaves the record pending so the
controller can report the correct event or cancel it. `cancel()` records
cancellation and its timestamp without inventing a USB completion code.
Terminal records may be reused by a later `begin()`; a non-pending `init()`
resets the record while preserving the token counter.

`DmUsbHostChannelCompletion` is an opaque fixed-capacity object with
`DM_USB_HOST_CHANNEL_COMPLETION_STORAGE_SIZE` bytes of storage. The operation
embedded in the record is the allocator owner, so the complete record must
retain a stable address while pending. `lease()` copies the active lease into
caller-provided storage; it never returns a writable pointer into the record.
`result()` is valid only for a completed record with `has_completion=true`;
cancelled or otherwise non-terminal records have no USB completion result.
The actual-length and timestamp accessors expose the recorded scalar values,
with zero for idle/default values. The module does not dispatch callbacks,
lock, wait, sleep, retry, update endpoint PID state, or touch controller
registers. It is an event/lifetime boundary only.

`dm_usb_host_channel_completion_smoke` is the isolated gate. It covers resource
retention, completion validation, accepted/NAK recording, cancellation,
timestamp propagation, terminal-event rejection, reuse and stale-token rejection
after channel migration. Passing this test does not imply H723 IRQ dispatch,
DMA/FIFO, SOF, QEMU bus, or DM-MC02 host support.

### STM32H7 async single-packet PIO adapter

`firmware/dm_stm32h7_usb_host_pipe.[ch]` also provides a caller-driven async
adapter around the completion record:

```c
bool dm_stm32h7_usb_host_pipe_async_init(
    DmStm32H7UsbHostPipeAsync *);
DmStm32H7UsbHostPipeAsyncStartResult
dm_stm32h7_usb_host_pipe_async_start(
    DmStm32H7UsbHostPipeAsync *, uintptr_t,
    DmUsbHostChannelAllocator *, const DmUsbHostPipe *,
    DmStm32H7UsbHostPipePid, const uint8_t *, size_t, uint8_t *, size_t,
    DmUsbHostChannelCompletionToken *);
DmStm32H7UsbHostPipeAsyncPollResult
dm_stm32h7_usb_host_pipe_async_poll(
    DmStm32H7UsbHostPipeAsync *,
    const DmUsbHostChannelCompletionToken *, uint64_t timestamp_ns);
bool dm_stm32h7_usb_host_pipe_async_cancel(
    DmStm32H7UsbHostPipeAsync *,
    const DmUsbHostChannelCompletionToken *, uint64_t timestamp_ns);
```

`start()` validates the endpoint request, acquires a generic channel lease,
writes one packet to the H723 PIO FIFO/registers, and returns `STARTED`; an
unavailable channel returns `DEFERRED` without submitting a packet. `poll()`
returns `PENDING` without mutation until `HCINT` reports a terminal condition,
then decodes `XFRC`, `NAK`, `STALL` or transaction error, reads accepted IN
bytes, commits the matching completion token and clears the observed interrupt.
`cancel()` disables the channel, clears its interrupt observation, and commits
the matching cancellation timestamp. A delayed or wrong token returns the
adapter's invalid result and cannot inspect or release the current channel.

This adapter is one-packet PIO and caller/event-loop driven; it does not install
an IRQ handler, take an IRQ lock, schedule SOF retries, update endpoint PID
state, arbitrate DMA/FIFO, or attach a QEMU USB bus. The async object, allocator,
and IN buffer must remain valid until `poll()` or `cancel()` reaches a terminal
state. `dm_stm32h7_usb_host_pipe_async_smoke` is the isolated gate, while the
two `stm32h723-usb-host` guest smokes are the direct H723/QEMU integration gate.

### Controller-independent retry policy

`firmware/dm_usb_host_retry.[ch]` owns the finite retry contract for one logical
packet attempt. The caller initializes a policy, calls `begin(started_at_ns)`
before the first submission, and supplies a nondecreasing virtual timestamp to
`check()`/`on_nak()` on later calls. Timestamp subtraction is modulo `uint64_t`,
but differences at or beyond half the counter range are rejected as ambiguous.
`max_retries` counts only retries after the initial packet; `timeout_ns == 0`
means no time limit. A successful packet uses `complete()`, an exhausted or
timed-out attempt uses `finish()`, and cancellation or invalid caller state uses
`reset()`.

The policy is intentionally not a scheduler and never sleeps or spins. A
controller adapter must return `NAK` or `DEFERRED` to its caller and let a later
virtual-time event invoke the next submission. `DEFERRED` means no packet was
submitted and does not consume retry budget.

### Bulk retry composition

`dm_usb_host_bulk_transfer_run_with_retry()` composes the existing multi-packet
bulk boundary with an optional caller-owned retry policy. Accepted packets update
the endpoint DATA PID and are counted in `actual_length`; a NAK leaves both the
current packet and PID unchanged. When a call returns `DM_USB_HOST_BULK_RETRY`,
the caller must call again with the same unaccepted packet span, normally by
advancing the data pointer and reducing the length by the previously returned
`actual_length` from the interrupted request. The object intentionally does not
retain hidden multi-packet progress, so it can be reused by different host
controllers without an async transaction lifetime.

### Periodic retry composition

`dm_usb_host_periodic_poller_init_with_retry()` optionally binds the same policy
to one periodic endpoint. A NAK retry returns `POLL_SUBMITTED` while retaining
the due schedule slot, DATA PID, and the caller's buffer pointers. The caller
must keep those buffers valid until the retry is accepted or reaches
`POLL_RETRY_EXHAUSTED`/`POLL_RETRY_TIMEOUT`. A `POLL_DEFERRED` submission did not
issue a packet and leaves endpoint state and schedule unchanged. The registry
keeps an endpoint for deferred work, while terminal retry results advance the
slot and are reported to the caller.

The H723 periodic and bulk adapters own a temporary channel lease only around a
synchronous PIO packet. They stop/release that lease before returning a NAK or
other completion. This ownership rule must not be reused for an asynchronous
controller until a separate completion/cancel contract exists.

### H723 bulk NAK completion and synchronous channel ownership

For a synchronous PIO packet, the H723 producer may complete a control/bulk
channel with `HCINT.NAK` while `HCCHAR.CHENA` is still set. The PIO consumer
waits for `HCINT.CHHLTD | HCINT.NAK`; on NAK it writes the current channel
configuration with `HCCHAR.CHDIS` before the enclosing bulk adapter releases
the temporary channel lease. The endpoint state receives `NAK`, so the
accepted byte count and DATA PID do not advance. A later caller retry owns a
new lease and resubmits the unaccepted packet.

This stop-before-release rule is specific to the synchronous H723 PIO
adapter. It does not implement a retry budget, SOF scheduler, asynchronous
completion/cancel, DMA/FIFO arbitration, or concurrent channel locking. The
reference guest smoke consumes a real QEMU `usb-serial` bulk IN endpoint,
checks the successful short packet, then performs an idle IN request and
requires `NAK`, zero actual bytes, and a cleared `CHENA`. This locks the
producer (`QEMU USB device`), boundary (`H723 HCINT/HCCHAR`), and consumer
(`DmStm32H7UsbHostBulk`/endpoint state) lifecycle without claiming DM-MC02
USB Host support.

### Bulk direct consumer update

The current direct consumer also exercises the real bulk IN path. The guest
discovers endpoint `0x81` (IN) and endpoint `0x02` (OUT), sends 130 bytes on
OUT, then receives one 64-byte IN request that completes as a 12-byte short
packet: the QEMU FTDI serial device contributes its `b1 00` modem-status
header followed by ten bytes injected through the chardev. The final H723
register assertions expect bulk `HCCHAR` type `2`, IN direction, endpoint `1`,
and `HCTSIZ` remaining length `52`; both temporary channel leases are empty
after completion.

The guest smoke uses a SysTick/WFI handoff at the input boundary so QEMU can
process the chardev callback while the CPU is idle. This is a test scheduling
contract, not a USB retry implementation. `dm_usb_host_bulk_transfer_run()`
also guarantees that an IN submitter receives `out_length == 0`; an OUT
submitter receives `in_capacity == 0`. NAK retry budgets, asynchronous
completion/cancel, DMA/FIFO scheduling, multi-device topology, PHY/VBUS,
passthrough, and DM-MC02 USB Host wiring remain outside this interface.

### STM32H723/QEMU bulk direct consumer

`tools/run-stm32h723-usb-host-bulk-smoke.sh` is the first direct consumer of
the adapter. It starts the independent `stm32h723-usb-host` reference profile
with QEMU's `usb-serial` on its root port. The Cortex-M7 guest discovers the
configuration, selects interface 0, builds the 64-byte bulk OUT pipe for
endpoint 2, and calls the default H723 bulk adapter with a 130-byte payload.
The QEMU file chardev is the external consumer oracle and must contain exactly
bytes `0..129`. The guest also records the final channel-1 register state and
checks that the endpoint has toggled DATA0 -> DATA1 over three accepted
packets and that the temporary channel lease is released.

This smoke proves the synchronous path
`descriptor -> pipe/state -> H723 PIO -> H723 host model -> QEMU USB device ->
chardev`; it does not prove individual packet tracing, bulk IN injection,
NAK retry policy, asynchronous completion/cancel, DMA/FIFO scheduling,
multi-device topology, PHY/VBUS behavior, passthrough, or DM-MC02 USB Host
wiring. The script is automatically included by
`tools/run-qemu-smoke-suite.sh` through its `run-*-smoke.sh` discovery.
## 0.24 Reusable STM32H723 ADC12 common register contract

`DmMc02AdcCommon` is the board-independent register block for the ADC1/ADC2
common address window. It exposes `CSR` at offset `0x00`, `CCR` at `0x08`,
`CDR` at `0x0c`, and `CDR2` at `0x10` in a `0x100`-byte little-endian
`MemoryRegion`.

`CCR` accepts only `DUAL[4:0]`, `DELAY[11:8]`, `DAMDF[15:14]`,
`CKMODE[17:16]`, `PRESC[21:18]`, `VREFEN`, `TSEN`, and `VBATEN`; reserved bits
read as zero and are discarded on every write. Full-width, half-word and
byte-lane writes merge into the same 32-bit state. A write touching any CCR
lane invokes the configured `DmMc02AdcCommonClockChanged` callback exactly
once after masking, including a write whose changed bits are reserved.

`CSR` is derived on every read from two `DmMc02AdcCommonStatusRead` callbacks.
The master status is masked by `0x47f` and returned in bits `6:0`/`10`; the
slave status uses the same mask and is shifted to bits `22:16`/`26`. The
component does not cache these status flags. `CDR` and `CDR2` are read-only;
`dm_mc02_adc_common_set_data()` remains an explicit staging hook for callers
that need to seed these registers; it does not bypass the regular-pair
producer or implement multimode DMA.

The component VMState is version 5. It saves `ccr`, `cdr`, `cdr2`, the CDR2
valid/source metadata, the configured marker, the monotonic
`next_conversion_id`, both bounded pending sample slots, and the DAMDF=3
accumulator (`damdf8_data`, count, expected source and maximum aggregate
timestamp). Status and clock callbacks, the regular-start peer callback, the
CDR/CDR2 data-ready and read-ack callbacks, `MemoryRegion`, and owner are
runtime wiring and are not serialized. Versions 1–4 remain accepted: the v1
post-load path advances `next_conversion_id` past any restored pending ID;
v2 streams initialize CDR2 valid/source metadata as empty; v1–v3 streams
clear the DAMDF=3 accumulator; and v4 streams clear its new aggregate
timestamp.
Post-load rejects reserved CCR bits, invalid pending IDs/ranks/timestamps, and
pending state under an unsupported dual/data format before invoking the clock
callback. `dm_mc02_adc_common_reset()` clears registers, pending state and the
ID generator while preserving runtime callback wiring.

The isolated `test-dm-adc-common` gate is `14/14`, including CDR2 source/valid
state, DAMDF=3 accumulator round-trip and an explicit version-1 stream. The
direct machine gate is `dm-mc02-adc-test` `59/59`. CDR2 alternate data and
its DMA boundary are defined by interfaces 0.29 and 0.32; interleaved DAMDF=3
CDR packing is defined by interface 0.33. Machine-level migration remains
outside this component contract.

## 0.25 ADC12 regular-simultaneous sample boundary

`DmMc02Adc` exposes the board-independent producer callback:

```c
typedef struct DmMc02AdcRegularSample {
    /* Non-zero only for a common-owned regular ADC pair. */
    uint64_t conversion_id;
    unsigned rank;       /* zero-based rank index, 0..15 */
    uint16_t value;
    uint64_t timestamp_ns; /* QEMU virtual monotonic nanoseconds */
} DmMc02AdcRegularSample;

typedef void (*DmMc02AdcRegularSampleReady)(
    void *, unsigned adc_index, const DmMc02AdcRegularSample *);
```

The callback is runtime wiring and is not part of ADC VMState. A non-zero
`conversion_id` identifies one common-owned software regular conversion; an
independent ADC sequence or an externally triggered sequence emits zero and
cannot enter the common matcher. `rank` identifies the rank within that
conversion, and `timestamp_ns` is the virtual time at which the rank became
available. ADC1 uses `adc_index == 0` (master), ADC2 uses `1` (slave). The
producer emits the event after the normal ADC DR/EOC data path has produced
the rank, so this callback does not replace DR reads or DMA.

`DmMc02AdcCommon` consumes those events through:

```c
DmMc02AdcCommonRegularSampleResult
dm_mc02_adc_common_submit_regular_sample(
    DmMc02AdcCommon *, unsigned source, uint64_t conversion_id,
    uint32_t rank, uint16_t value, uint64_t timestamp_ns);
```

`source == 0` is master and `source == 1` is slave. The current supported
format is exactly `CCR.DUAL == 0x6` (regular simultaneous) with `DAMDF == 2`
or `3`:

| DAMDF | CDR result |
| --- | --- |
| `2` | master sample in bits `15:0`, slave sample in bits `31:16` |
| `3` | two exact pairs form one CDR word: `M0[7:0]`, `S0[15:8]`, `M1[23:16]`, `S1[31:24]` |

The second sample may arrive first. A pair is accepted only when both
`conversion_id` and rank match; producer timestamps are diagnostic and need
not be identical. ID zero is rejected, so independent or external-trigger
samples cannot be accidentally paired. A pair never uses wall-clock arrival
order or synthesizes a missing sample. One pending slot per source is the
fixed capacity. `PENDING` records the first half, `PACKED` updates CDR,
`PACKING` records a complete DAMDF=3 pair that is awaiting the second pair,
`MISMATCH` drops the old peer and retains the new event, `DUPLICATE` replaces
the same-source pending event, and `UNSUPPORTED` leaves the pending state
unchanged. DAMDF=3 emits no data-ready event for its first pair and publishes
the completed word with the maximum timestamp of all four values. A
DUAL/DAMDF mode write and reset clear both slots and any DAMDF=3 accumulator.
`CDR2` remains unchanged in this boundary.

The common VMState version is 5 and includes CCR/CDR/CDR2, CDR2 validity and
source, the configured marker, `next_conversion_id`, both pending slots
(including conversion ID, rank, value and timestamp), and DAMDF=3 partial-word
state including its aggregate timestamp. Versions 1–4 are accepted; v1 is
accepted without the generator field and reconstructs its frontier from
pending IDs, v2 starts with no restored CDR2 acknowledgement metadata, and
pre-v4/pre-v5 streams respectively clear the DAMDF=3 accumulator/timestamp.
It excludes status/clock callbacks, the regular-start peer callback,
QOM ownership and MemoryRegion. The component is not registered with the
DM-MC02 machine migration composition.

The isolated gate is `test-dm-adc-common` `14/14`; the direct boundary is
`/dm-mc02/adc/common-cdr-regular-simultaneous`; the full ADC qtest is `59/59`.
This interface does not claim external-trigger synchronized ADC start, CDR2 alternate data,
other dual modes, multimode DMA, or whole-machine
migration.

## 0.26 ADC12 regular-simultaneous software-start admission

The ADC common block exposes a runtime-only start admission callback:

```c
typedef bool (*DmMc02AdcCommonRegularStartPeer)(void *, unsigned source,
                                                uint64_t conversion_id);
bool dm_mc02_adc_common_admit_regular_start(DmMc02AdcCommon *, unsigned source);
```

For `CCR.DUAL == 0x6`, `source == 0` is ADC1/master and is admitted only when
the composition callback successfully starts the enabled ADC2/slave. The
common block does not inspect ADC2 private state; the DM-MC02 composition owns
the peer callback and uses the public `dm_mc02_adc_enabled()` and
`dm_mc02_adc_start()` interfaces. `source == 1` is rejected, so a slave cannot
create an independent regular sequence. If the peer is absent or disabled,
the master `ADSTART` request is rejected as well. Other DUAL values retain the
independent-ADC admission path.

The callback is runtime wiring and survives ADC/common reset; it is excluded
from common VMState. Each admitted master start allocates a non-zero,
monotonically increasing `conversion_id` and passes it to the composition
callback. The DM-MC02 callback seeds both ADC kernels with that ID before
starting ADC2, allowing continuous sequences to advance both IDs together.
Independent ADC sequences emit `conversion_id == 0`. Regular external-triggered
sequences use ID zero in this software-start contract; the later 0.27
external-trigger admission contract assigns a shared non-zero ID in
`DUAL=0x6`.

The boundary covers software `ADSTART` only. External timer-triggered starts
are defined by interface 0.27; CDR2 DMA is defined by interface 0.32, while
other multimode formats and whole-machine migration remain separate
unsupported boundaries.

The direct qtest `/dm-mc02/adc/common-master-start-drives-slave` verifies the
HAL ordering (enable ADC2, then start ADC1), rejects an ADC2-only start, and
repeats the master-start path after reset. The full ADC qtest is `59/59`.

## 0.27 ADC12 regular-simultaneous external-trigger admission

Regular external-trigger ownership is split between the reusable ADC/common
boundary and the DM-MC02 composition. The ADC exposes the following runtime
callback shape:

```c
typedef bool (*DmMc02AdcRegularExternalTriggerRequest)(
    void *, unsigned adc_index, uint32_t trigger_source, bool rising,
    unsigned event_count, uint64_t timestamp_ns, uint64_t *conversion_id);
void dm_mc02_adc_set_regular_trigger_callback(
    DmMc02Adc *, unsigned adc_index,
    DmMc02AdcRegularExternalTriggerRequest, void *opaque);
```

For `CCR.DUAL == 0x6`, an external regular-trigger edge is common-owned:
`adc_index == 0` (ADC1/master) is the only admitted source. The common block
allocates a non-zero monotonic `conversion_id` and calls the composition peer
boundary:

```c
typedef bool (*DmMc02AdcCommonExternalTriggerPeer)(
    void *, unsigned source, uint32_t trigger_source, bool rising,
    unsigned event_count, uint64_t timestamp_ns, uint64_t conversion_id);
bool dm_mc02_adc_common_admit_external_trigger(
    DmMc02AdcCommon *, unsigned source, uint32_t trigger_source,
    bool rising, unsigned event_count, uint64_t timestamp_ns,
    uint64_t *conversion_id);
bool dm_mc02_adc_external_trigger_with_id(
    DmMc02Adc *, uint32_t trigger_source, bool rising, unsigned event_count,
    uint64_t timestamp_ns, uint64_t conversion_id);
```

The peer callback must start an already enabled and externally armed ADC2
through the public `dm_mc02_adc_external_trigger_with_id()` API. It must not
re-publish the edge to the common admission callback. If the peer is absent,
disabled, incorrectly armed, or rejects the event, admission fails and the
caller receives `conversion_id == 0`; the generator may advance but a failed
ID is never published as a valid sample identity. ADC2's ordinary trigger-bus
sink is rejected in this mode, preventing a second local conversion or a
second ID allocation.

The ADC still validates `EXTSEL`, `EXTEN`, `ADEN`, `ADSTART`, calibration and
busy state at the local consumer. Rising/falling/both-edge matching is owned
by the ADC kernel. `event_count == 0` is normalized to one. A coalesced batch
currently arms one regular sequence; it does not represent a queue of
individual edges. The timestamp is monotonic virtual nanoseconds. If it is in
the future, the ADC schedules from that timestamp; wall-clock arrival is not
part of the contract. Injected external triggers remain on the ADC-local
path. For non-simultaneous DUAL modes the callback leaves `conversion_id == 0`
and preserves independent ADC behavior.

The direct boundary is `/dm-mc02/adc/common-external-trigger-drives-slave`;
the isolated common gate is `test-dm-adc-common` `14/14`, and the full ADC qtest
is `59/59`. CDR2 DMA is defined by interface 0.32. This interface does not
claim other dual modes, multimode formats beyond 0.28/0.32, exact per-edge batching, or
machine-level migration.

## 0.60 IWDG reset reason projection to RCC_RSR

`DmMc02Iwdg` exposes a board-independent `DmMc02IwdgResetRequested` callback.
On timeout or an early window reload, the IWDG component first invokes this
callback and then requests the QEMU guest reset. The callback is destination
wiring and is not part of IWDG component VMState; the reusable IWDG code has no
dependency on PWR/RCC types.

DM-MC02 binds that callback to `dm_mc02_pwr_rcc_note_iwdg_reset()`. The PWR/RCC
consumer sets the STM32H723 `RCC_RSR` bit at offset `0xd0`,
`IWDG1RSTF` bit 26. The source bit is read-only. A guest write with `RMVF`
bit 16 set clears the currently modelled reset-source bit; writes of source
bits do nothing. Sub-word writes that reach RMVF follow the same W1C rule.

The PWR/RCC reset path preserves modelled `RCC_RSR` source flags across an
ordinary system reset, so the next guest boot can inspect the watchdog cause.
The focused PWR/RCC unit covers projection, source-bit immutability, reset
preservation and RMVF clearing. `dm-mc02-iwdg-test` covers timeout, reset
projection, reset preservation and guest clearing. This is only the IWDG1
projection; other H723 reset sources, full reset-domain behavior and
machine-level migration remain outside the contract.

## 0.61 H723 SYSRESETREQ software-reset reason projection

The CPU-side producer is QEMU's native ARMv7-M/NVIC `SYSRESETREQ` named output.
DM-MC02 connects that output, after the `armv7m` child is realized, to a
destination-owned `qemu_irq` handler:

```c
void dm_mc02_pwr_rcc_note_software_reset(void *opaque,
                                         int line, int level);
```

The handler ignores the deasserted half of the pulse and sets the H723
`RCC_RSR.SFTRSTF` bit (offset `0xd0`, bit 24) on its asserted edge. QEMU's
existing reset request path then performs the ordinary machine reset. This
keeps AIRCR/NVIC state in QEMU's native CPU child and keeps reset-cause state in
PWR/RCC; the `qemu_irq` itself is runtime wiring and is not serialized.

`RCC_RSR.RMVF` remains write-one-to-clear for all currently modelled sources,
and a host/QMP `system_reset` does not synthesize `SFTRSTF`. The direct CPU
qtest verifies the guest AIRCR write, reset completion, flag preservation over
an ordinary reset, and explicit RMVF clearing. This slice covers only
SYSRESETREQ→SFTRSTF; it does not cover CPURSTF, D1/D2, BOR/POR, pin, low-power,
WWDG, or machine-level reset-cause migration.

## 0.62 H723 power-on reset reason projection

The initial DM-MC02 machine construction is an explicit board power-on event.
After the PWR/RCC component has been initialized, the machine calls:

```c
void dm_mc02_pwr_rcc_note_power_on_reset(DmMc02PwrRcc *state);
```

The consumer latches H723 `RCC_RSR.PORRSTF` at offset `0xd0`, bit 23. The hook
is deliberately separate from `dm_mc02_pwr_rcc_reset()`: an ordinary QMP or
guest system reset preserves all currently modelled reset-source flags and does
not create a new POR event. `PORRSTF` is read-only; `RCC_RSR.RMVF` remains the
only guest-visible clear operation.

The PWR/RCC unit checks explicit notification, idempotence, ordinary-reset
preservation and RMVF clearing. The direct `dm-mc02-cpu-test` checks the
guest-visible initial flag, preservation over a system reset and clearing. This
is only the startup producer boundary; no BOR/PIN/domain/CPU/low-power source
or machine-level reset-cause migration is implied.

## 0.28 ADC12 regular-simultaneous CDR DMA boundary

The common block publishes a packed regular result only after its bounded
matcher has received the same non-zero `conversion_id` and zero-based rank from
ADC1/master and ADC2/slave. The reusable callback is:

```c
typedef void (*DmMc02AdcCommonDataReady)(void *, uint32_t data,
                                         uint64_t timestamp_ns);
void dm_mc02_adc_common_set_data_ready_callback(
    DmMc02AdcCommon *, DmMc02AdcCommonDataReady, void *opaque);
```

The callback's timestamp is monotonic `QEMU_CLOCK_VIRTUAL` time and is runtime
wiring; it is not serialized by the common VMState. The common component does
not know about DMA, board pins, or a particular memory address.

For regular simultaneous `CCR.DUAL=0x6`, `DAMDF=0x2` packs ADC1 into CDR
bits 15:0 and ADC2 into bits 31:16, yielding one beat per completed pair. Its
`DAMDF=0x3` form accumulates two matching pairs and yields one CDR beat with
`M0,S0,M1,S1` in successive byte lanes. For regular-interleaved
`CCR.DUAL=0x7` or `0x3`, `DAMDF=0x3` accumulates the strict source
sequence master/slave/master/slave into the same CDR byte layout and yields
one beat only after all four values arrive. The data-ready timestamp is the
maximum of the values represented in the emitted word. CDR2 remains the
distinct interleaved `DAMDF=0x2` boundary; all other dual/DAMDF modes are
outside this boundary.

The DM-MC02 composition consumes the event only when public
`dm_mc02_adc_regular_dma_enabled()` reports non-zero ADC1 DMA data management.
It raises the board ADC1 request (request 9) through
`dm_mc02_dma_request_endpoint()` in endpoint mode, or
`dm_mc02_dma_request()` with the `ADC12_COMMON.CDR` address in MMIO mode.
Both paths share the DMA stream arbitration and NDTR/IRQ semantics. DMA request
lookup is keyed by `(request_id, peripheral_addr)`, because ADC1 `DR` and
`ADC12_COMMON.CDR` may legally share request 9; a cached negative lookup for
one endpoint must not suppress the other.

The direct gates are `/dm-mc02/adc/common-multimode-dma-endpoint`,
`/dm-mc02/adc/common-multimode-dma-mmio`,
`/dm-mc02/adc/common-interleaved-damdf8-dma-endpoint`, and
`/dm-mc02/adc/common-interleaved-damdf8-dma-mmio`. They configure common CCR
before the HAL-equivalent sequence (DMA, ADC2 enable, ADC1 master start), then
verify packed bytes/words, partial-word request suppression, NDTR zero and the
DMA transfer-complete flag. The isolated common test is
`test-dm-adc-common` `14/14`; the full ADC qtest is `59/59`. This boundary
does not establish physical DMA arbitration/timing or machine-level migration.
CDR read acknowledgement is defined by interface 0.30 below.

## 0.29 ADC12 regular-interleaved CDR2 boundary

The ADC common block also consumes completed regular-rank events for the H723
alternate regular-data register. The existing producer callback remains the
boundary:

```c
typedef struct DmMc02AdcRegularSample {
    uint64_t conversion_id;
    uint32_t rank;
    uint16_t value;
    uint64_t timestamp_ns;
} DmMc02AdcRegularSample;
```

For `CCR.DUAL == 0x7` (regular interleaved), and `CCR.DUAL == 0x3`
(regular interleaved plus injected simultaneous), a valid regular sample
updates `CDR2.RDATA_ALT` as a zero-extended 16-bit value. The latest completed
rank wins. The common component does not pair master/slave samples for this
path, and the producer's conversion ID may be zero because CDR2 does not need
a pair identity. `rank` remains bounded to `0..15` and `timestamp_ns` remains
diagnostic virtual time.

`DAMDF == 0` is the polling-only form of this boundary. `DAMDF == 0x2` is
the separately defined 32-bit CDR2 DMA form in interface 0.32. `DAMDF == 0x3`
is deliberately not a CDR2 format: it is the four-value CDR packing boundary
in interface 0.33. Other non-zero formats are rejected explicitly. CDR2
updates do not invoke `DmMc02AdcCommonDataReady`; CDR remains unchanged by a
CDR2 event, and reset clears CDR2.

The isolated `test-dm-adc-common` gate is `14/14`. The direct machine gate is
`/dm-mc02/adc/common-cdr2-regular-interleaved`, which checks both dual values,
successive rank publication, CDR non-use and reset. The exact interleaved
master/slave cadence is defined by interface 0.31 below; its DMA consumer
boundary is defined by interface 0.32. Other dual/DAMDF formats and
machine-level migration remain outside this interface.

## 0.30 ADC12 CDR read acknowledgement

The common component exposes a runtime-only acknowledgement callback and an
explicit notification entry point:

```c
typedef void (*DmMc02AdcCommonCdrRead)(void *opaque);
void dm_mc02_adc_common_set_cdr_read_callback(
    DmMc02AdcCommon *, DmMc02AdcCommonCdrRead, void *opaque);
void dm_mc02_adc_common_notify_cdr_read(DmMc02AdcCommon *);
```

For regular-simultaneous `CCR.DUAL == 0x6` with `DAMDF == 0x2` or `0x3`,
and for regular-interleaved `CCR.DUAL == 0x7` or `0x3` with
`DAMDF == 0x3`, a legal MMIO access overlapping `CDR` invokes the callback
after the value has been assembled. The callback is not invoked for `CDR2`
or unsupported/unused CDR formats. CDR2 read acknowledgement is a separate
source-specific boundary in interface 0.32. This callback is runtime wiring
and survives component reset; it is excluded from VMState.

The DM-MC02 composition callback acknowledges both ADC regular producers via
the public `dm_mc02_adc_acknowledge_regular_data()` API. That API clears EOC,
reuses the ADC data-consumption continuation and therefore releases AUTDLY at
the same boundary. Endpoint DMA does not traverse the common MemoryRegion, so
the board consumer calls `dm_mc02_adc_common_notify_cdr_read()` only after a
successful endpoint transfer. MMIO DMA naturally obtains the same effect from
the common read callback.

The direct gate is `/dm-mc02/adc/common-cdr-read-acknowledges-both-eoc`; it
checks MMIO read and AUTDLY continuation. The isolated common gate checks the
unsupported-format negative case, while endpoint and MMIO CDR DMA paths are
covered by the adjacent DMA gates. The isolated common gate is
`test-dm-adc-common` `14/14`, and the full ADC qtest is `59/59`. This
contract does not claim acknowledgement semantics for
other dual/DAMDF modes, physical DMA timing, or machine-level migration.

## 0.31 ADC12 regular-interleaved cadence and delayed peer start

The ADC common admission boundary owns regular start/trigger ownership for
`CCR.DUAL == 0x7` (regular interleaved) and `CCR.DUAL == 0x3` (regular
interleaved plus injected simultaneous). Only ADC1/master may submit the
regular software start or regular external-trigger edge. The common block
allocates the non-zero shared conversion ID and asks the composition to start
an enabled ADC2 peer; a direct ADC2 start/second trigger is rejected. The
callback is runtime wiring and does not inspect ADC private state.

The reusable ADC kernel exposes the following composition APIs:

```c
bool dm_mc02_adc_start_regular_at(DmMc02Adc *, uint64_t conversion_id,
                                  uint64_t start_ns);
bool dm_mc02_adc_external_trigger_with_id_at(
    DmMc02Adc *, uint32_t source, bool rising, unsigned event_count,
    uint64_t trigger_timestamp_ns, uint64_t conversion_start_ns,
    uint64_t conversion_id);
uint64_t dm_mc02_adc_interleaved_slave_start_ns(
    const DmMc02Adc *, uint64_t master_start_ns, unsigned delay_code);
```

`dm_mc02_adc_interleaved_slave_start_ns()` resolves the slave sampling start
as `master_start + master sampling phase + CCR.DELAY`. The delay table is
resolution-dependent for 16/14/12/10-bit conversions and follows RM0468
Table 236. Sampling time, conversion time, current ADC kernel clock and
absolute virtual timestamps are resolved by the ADC kernel; integer nanosecond
deadlines round up and a stopped kernel clock returns `UINT64_MAX`. The
composition converts that stopped-clock marker into a same-epoch armed peer;
the ADC clock callback then keeps progress paused until a valid clock exists.

`DUAL=0x6` remains regular-simultaneous and does not use this delayed peer
phase. `DUAL=0x3` injected simultaneous trigger delivery remains a separate
ADC group: both injected conversions complete at the shared trigger schedule,
but no injected common-data packing is claimed.

The direct gates are `/dm-mc02/adc/common-interleaved-external-trigger-cadence`
plus `/dm-mc02/adc/common-interleaved-injected-simultaneous`; the full ADC
qtest is `59/59`. CDR2 DMA is defined by interface 0.32. This contract does
not claim exact per-edge event-count queueing, other dual/DAMDF formats,
physical DMA timing, or
machine-level migration.

## 0.32 ADC12 regular-interleaved CDR2 DMA boundary

`DmMc02AdcCommon` publishes a CDR2 data-ready event for each completed regular
rank in the supported interleaved modes. The reusable callback is:

```c
typedef void (*DmMc02AdcCommonCdr2DataReady)(
    void *, uint32_t data, unsigned source, uint64_t timestamp_ns);
void dm_mc02_adc_common_set_cdr2_data_ready_callback(
    DmMc02AdcCommon *, DmMc02AdcCommonCdr2DataReady, void *opaque);
void dm_mc02_adc_common_notify_cdr2_read(DmMc02AdcCommon *);
```

The supported configuration is `CCR.DUAL == 0x7` (regular interleaved) or
`0x3` (regular interleaved plus injected simultaneous), with `DAMDF == 0x2`.
Each ADC1/ADC2 regular-rank producer event updates the single
`CDR2.RDATA_ALT` register with the zero-extended 16-bit result and emits one
data-ready callback carrying the source (`0` for ADC1, `1` for ADC2) and its
monotonic virtual timestamp. The latest event overwrites the register; there
is no queue at this boundary. Injected results are not packed here.

The DM-MC02 consumer checks the public ADC1 regular-DMA enable predicate and
routes each event as DMA request 9 to the `ADC12_COMMON.CDR2` address. Endpoint
DMA receives the captured data directly and calls
`dm_mc02_adc_common_notify_cdr2_read()` only after a successful transfer.
MMIO DMA reads the common CDR2 `MemoryRegion`, which invokes the same
acknowledgement path after the value has been assembled. A CDR2 read consumes
the current single-register event and acknowledges only the ADC that produced
it through `dm_mc02_adc_acknowledge_regular_data()`; a failed DMA request does
not acknowledge it, and a later producer event replaces it.

CDR2 data-ready/read callbacks are runtime wiring and are separate from the
common VMState. `cdr2_valid` and `cdr2_source` are component state. The
component VMState is version 5 and persists the current CDR2 value plus its
valid/source metadata, so a component round-trip cannot lose which ADC's EOC
the next successful read must clear. The machine still does not register this
component description for machine-level migration.

The isolated gate is `test-dm-adc-common` `14/14`; direct gates are
`/dm-mc02/adc/common-cdr2-dma-endpoint` and
`/dm-mc02/adc/common-cdr2-dma-mmio`; the full ADC qtest is `59/59`.
These tests cover both `DUAL=0x7` and `DUAL=0x3`, two successive regular
beats, source-specific acknowledgement, NDTR completion and DMA TC. This
boundary does not claim other DAMDF=3 behavior beyond the separate CDR packing
boundary in interface 0.33, other dual/DAMDF modes,
precise physical DMA arbitration or transfer timing, injected common-data
packing, or machine-level migration.

## 0.33 ADC12 regular-interleaved DAMDF=3 CDR packing

This is the dedicated CDR packing boundary for regular-interleaved
`CCR.DUAL == 0x7` and regular-interleaved-plus-injected-simultaneous
`CCR.DUAL == 0x3` with `DAMDF == 0x3`. The producer is the existing
`dm_mc02_adc_common_submit_regular_sample()` regular-rank event; the consumer
is the common `DmMc02AdcCommonDataReady` callback and, in the DM-MC02
composition, ADC1 DMA request 9. No new board-specific producer API is
introduced.

The common block accepts exactly four consecutive source events in this order:
ADC1/master, ADC2/slave, ADC1/master, ADC2/slave. Each value contributes its
low eight bits to `CDR`:

| Event | CDR lane |
| --- | --- |
| master rank 0 | `[7:0]` |
| slave rank 0 | `[15:8]` |
| master rank 1 | `[23:16]` |
| slave rank 1 | `[31:24]` |

The first three values are retained in a fixed-size accumulator and do not
emit a data-ready event or DMA request. An out-of-order source clears the
partial word and returns `MISMATCH`; reset or a DUAL/DAMDF mode change also
clears it. Once the fourth value arrives, the complete little-endian CDR word
is published exactly once and the accumulator is cleared. The published
timestamp is the maximum producer timestamp represented by all four values,
so callback ordering or a non-increasing producer timestamp cannot move the
event time backwards. `CDR2` is not updated by this path.

The DM-MC02 consumer emits one request-9 DMA beat per complete word through
either the endpoint or CDR `MemoryRegion` path. A partial word must not change
DMA `NDTR`; a successful transfer invokes the same CDR read-acknowledgement
boundary as a direct CDR read. `DUAL=0x3` injected results remain outside this
regular-data word. Regular-simultaneous `DUAL=0x6` with `DAMDF=0x3` uses the
separate two-pair matcher described in interface 0.25/0.28, not this strict
interleaved source-order path.

The common component VMState is version 5 and preserves the partial data,
source cursor, count and aggregate timestamp. Versions 1–4 remain loadable;
streams without these fields clear the accumulator and/or timestamp during
post-load. Callbacks, DMA routing, `MemoryRegion`, and machine ownership are
runtime wiring and are not serialized. The isolated gate is
`test-dm-adc-common` `14/14`; direct gates are
`/dm-mc02/adc/common-interleaved-damdf8-dma-endpoint` and
`/dm-mc02/adc/common-interleaved-damdf8-dma-mmio`; the full ADC qtest is
`59/59`.

This boundary does not claim CDR2 semantics for `DAMDF=0x3`, other
dual/data-management formats, exact physical DMA arbitration or transfer
timing, exact per-edge trigger queueing, injected common-data packing, or
machine-level migration.

## 0.34 ADC regular DMA overrun request gate

This boundary belongs to the reusable STM32H723 ADC regular data path. The
producer is `adc_emit_sample()` after a regular rank completes; the boundary
is the ADC-local regular DMA request decision; the consumer is the already
configured ADC `DR` DMA endpoint, either through the endpoint callback or the
`MemoryRegion` path. It does not alter the separate ADC12 common CDR/CDR2
data-ready callbacks.

Before publishing a new regular result, the ADC samples the entry `ISR.OVR`
state and checks whether the previous regular `EOC` is still pending. The
following rules are explicit:

| Entry/current state | `ADC_DR` action | ADC-local DMA request |
| --- | --- | --- |
| no `EOC`, no `OVR` | publish the result | issue one request if DMA is configured and routable |
| `EOC` set | set `OVR`; preserve or overwrite DR according to `CFGR.OVRMOD` | suppress the request for this result |
| `OVR` already set | publish according to the current `OVRMOD`/`EOC` state | suppress the request until ISR W1C clears `OVR` |

`CFGR.OVRMOD` affects only whether an overrun result replaces the data
register. Reading `ADC_DR` clears `EOC` through the normal ADC consumer
boundary; it does not clear `OVR`. Firmware must clear `OVR` with an ISR
write-one-to-clear operation. Once `OVR` and any pending `EOC` have been
consumed, the next eligible regular result can issue a DMA request again.
There is no DMA-private latch: the guest-visible ADC `OVR` bit is the sole
persistent gate state.

The gate is intentionally applied only to the ADC-local `DR` request emitted
by `adc_emit_sample()`. DAMDF=3 regular-interleaved CDR accumulation can keep
ADC `EOC` asserted while it is collecting the four source values; its common
CDR request remains governed by the common data-ready boundary. Applying the
ADC-local gate to that distinct common producer would incorrectly turn a
partial but valid CDR word into a lost transfer.

The direct qtest
`/dm-mc02/adc/regular-dma-overrun-request-gate` runs four fresh machine cases:
endpoint and MMIO consumers crossed with `OVRMOD=0/1`. It leaves the stream
disabled for two ranks to produce `EOC` then `OVR`, checks the preserve/overwrite
data result and unchanged `NDTR`, enables the stream while `OVR` remains set,
and verifies that only ISR W1C plus normal DR consumption releases the next
DMA beat. The full ADC qtest is `60/60`; the QEMU system target is rebuilt.

This contract does not claim common CDR/CDR2 request suppression based on ADC
OVR, other dual/DAMDF formats, precise DMA arbitration or physical transfer
timing, or machine-level migration.

## 0.35 ADC12 regular-simultaneous common DMA overrun request gate

This boundary extends the reusable ADC12 common-to-DMA contract only for
regular-simultaneous `CCR.DUAL == 0x6` with `DAMDF == 0x2`. The producer is a
complete master/slave regular pair; the common block updates its read-only CDR
with the packed value, while its `data_ready` callback is the common DMA
request boundary. The status callbacks are the producer-side input and remain
runtime wiring; the common component does not own or cache ADC OVR state.

When either status source reports the ADC `ISR.OVR` bit (bit 4), CDR is still
updated but the common `data_ready` callback is suppressed. Thus a disabled,
unroutable or late DMA consumer cannot observe a new CDR request while the
producer overrun condition is latched. A CDR read still acknowledges both
producer EOC flags through the existing read callback, while only the ADC
producer's ISR write-one-to-clear operation clears OVR. After both status
sources no longer report OVR, the next complete pair may issue one request.

The gate is intentionally not applied to `DAMDF == 0x3`, including its
regular-simultaneous two-pair accumulator: that path has a distinct partial
word contract and must not interpret an asserted EOC during accumulation as a
common DMA stop condition. CDR2 and all other dual/data-management formats are
outside this interface.

The isolated `test-dm-adc-common` gate covers callback suppression while CDR
continues to update, recovery after status clear, and the DAMDF=3 scope
regression. Direct qtest covers both endpoint and CDR-MMIO DMA consumers: an
unserved first result, a second result with both OVR flags and unchanged
`NDTR`, CDR read/EOC acknowledgement, ISR W1C recovery, and the following
successful request. The common unit gate is `15/15`; the two direct gates are
`2/2`. This interface does not claim precise physical DMA arbitration or
machine-level migration.

## 0.36 Reusable STM32H723 SPI data-path VMState contract

`dm_mc02_spi_vmstate()` describes the reusable SPI data-path state needed to
continue a guest-visible transfer after a component restore. The producer
state is:

```text
cr1, cr2, cfg1, cfg2,
transfer_remaining, rx, rx_valid, eot,
dma_tx_next_ns
```

`dma_tx_next_ns` is a monotonic virtual nanosecond deadline for the bounded
deferred TX-DMA continuation; zero means that no SPI-owned continuation is
armed. The component rejects transfer counts that cannot fit the model's
16-bit TSIZE counter and deadlines beyond signed QEMU virtual-clock range
before invoking runtime synchronization.

The target callback table and opaque values, selected chip mask, DMA channel
and DMAMUX pointers, endpoint callbacks, `QEMUTimer` object, recursive request
guard, endpoint mode and DMA batch policy are runtime wiring or configuration;
they are not serialized. `dm_mc02_spi_sync_runtime()` clears the recursive
guard, cancels any destination TX-DMA timer and re-arms the saved deadline
against the destination virtual clock. GPIO/board composition remains
responsible for re-projecting the selected chip after its own input state is
restored.

The isolated gate is `test-dm-spi-vmstate` `4/4`. The direct consumer gates
remain `tools/run-bmi088-smoke.sh` and
`tools/run-spi2-dma-smoke.sh on|off`; they verify that adding the component
contract does not change polled or endpoint/MMIO SPI transfer behavior. This
is a component contract only: the DM-MC02 machine does not register it, so it
does not establish whole-machine snapshot, save/load or migration support.
Exact DMA stream/DMAMUX joint restore ordering, target-device state, GPIO
selection restore ordering, and SPI electrical/bit-level timing remain
separate boundaries.

## 0.37 Reusable USART component VMState contract

`dm_mc02_uart_vmstate()` describes the reusable USART data-path state needed
to continue CPU-visible UART activity after a component restore. The producer
state is:

```text
regs[0x400], rx_fifo[256] + head/length,
rx_wire_fifo[256] + head/length, rx_dropped,
tx_fifo[4096] + head/length, tx_dropped, tx_short_writes,
rx_next_ns, idle_next_ns, tx_next_ns, dma_tx_next_ns
```

The four `*_next_ns` values are absolute monotonic virtual-nanosecond
deadlines. A zero value means that the corresponding continuation is not
armed. `rx_next_ns` is the next wire-to-RX-FIFO delivery, `idle_next_ns` is
the post-frame IDLE indication, `tx_next_ns` is paced host TX, and
`dma_tx_next_ns` is the bounded DMA-TX retry/continuation. The post-load
validator rejects invalid ring cursors, deadlines outside signed QEMU virtual
clock range, and mutually inconsistent RX/IDLE or FIFO/deadline state before
calling `dm_mc02_uart_sync_runtime()`.

The chardev, `Clock`, DMA/DMAMUX pointers, IRQ, timers, endpoint callbacks,
RS485/DE and transceiver power wiring, endpoint-mode configuration, started
markers, and derived baud timing are runtime wiring/configuration. They are
not serialized. Runtime sync preserves saved absolute deadlines, recomputes
baud timing from restored registers and the destination clock, and re-arms
only destination-owned timers; expired deadlines are scheduled at the current
virtual time. No wall-clock delay is introduced.

The isolated gate is `test-dm-uart-vmstate` `5/5`; direct consumer gates are
`run-uart-smoke.sh`, `run-uart-idle-smoke.sh`, `run-uart-timing-smoke.sh`,
`run-uart-rx-timing-smoke.sh`, `run-uart-dma-smoke.sh on|off`, and
`run-uart2-dma-smoke.sh on|off`, all passing. This is a component contract
only: the DM-MC02 machine does not register it, so it does not establish
whole-machine snapshot/save/load or migration support. Chardev reconnect,
slow-backend partial writes, and joint UART/DMA/DMAMUX restore ordering remain
separate boundaries.

## 0.38 Reusable BMI088 SPI framing adapter VMState contract

`dm_mc02_bmi088_spi_vmstate()` describes the in-flight transaction state of
one board-independent BMI088 SPI framing adapter. The producer state is:

```text
command_seen, read_transfer, dummy_pending, reg, read_start_reg
```

`command_seen` marks whether the current chip-select transaction has consumed
its command byte. `read_transfer` records the command direction,
`dummy_pending` records the BMI088 accelerometer read dummy byte, `reg` is the
next register/FIFO address consumed by the framer, and `read_start_reg` keeps
the transaction origin for the register-read completion notification. A
selected-mask transition ends the transaction and resets this state; FIFO
register reads keep their address while the target advances its FIFO.

The BMI088 pointer, consume callback and callback opaque are destination-owned
runtime wiring and are not serialized. The target's register image, FIFO,
sample sequence, signal/noise/bias/filter state and temperature state are
owned by the BMI088 component and are intentionally outside this adapter
contract. The post-load validator rejects versions other than 1, cursor/mode
state without a command, and a dummy phase on a write transaction before any
consumer callback can run.

The isolated gate is `test-dm-bmi088-spi-vmstate` `5/5`, covering legal read
and write round-trips, preservation of destination runtime wiring, unsupported
version, malformed cursor/mode, write-with-dummy, and truncated streams. The
direct consumer gates are `tools/run-bmi088-smoke.sh` and
`tools/run-cosim-link-smoke.sh`; both pass. This is a component contract only:
the DM-MC02 machine does not register it, so it does not establish whole-
machine snapshot/save/load or migration support. BMI088 sensor VMState and
joint SPI/GPIO/DMAMUX restore ordering remain separate boundaries.

## 0.39 Reusable BMI088 sensor component VMState contract

`dm_mc02_bmi088_vmstate()` describes the dynamic state of one board-independent
BMI088 die. Its producer state is:

```text
regs[256], signal dynamic/configuration state,
fifo[1024], fifo_sequence[1024], FIFO cursors and frame state,
sample_sequence, gyro_drdy_clear_time_ns
```

The signal state includes noise, fixed and dynamic bias, temperature
coefficients, temperature, full scale, RNG, bandwidth/filter history, ODR
period and absolute virtual-nanosecond sample/bias timing state. FIFO bytes,
per-byte sample sequence, skipped-frame/sensor-time/overrun state and the
in-progress read frame are serialized so the next register read and sample
remain deterministic. `double` fields use fixed big-endian IEEE-754 bit-pattern
encoding rather than host layout.

`accel` is the die identity selected by destination board composition, and
`signal.kind` is its matching static signal configuration. Neither is in the
state stream. The destination must initialize both before loading; post-load
rejects invalid or mismatched identity/kind, invalid signal values, FIFO bounds
and impossible frame modes before the sensor is used.

The isolated gate is `test-dm-bmi088-vmstate` `5/5`, covering accelerometer and
gyro round-trips, destination static-kind validation, FIFO cursor validation
and truncated streams. Direct consumer gates `run-bmi088-smoke.sh`,
`run-bmi088-fifo-smoke.sh`, `run-bmi088-drift-smoke.sh` and
`run-bmi088-filter-smoke.sh` pass. This component is not registered with the
DM-MC02 machine and therefore does not establish whole-machine migration.
Joint SPI-framer/GPIO/DMA/DMAMUX restore ordering and physical SPI timing remain
separate boundaries.

## 0.40 Reusable BMI088 SPI link composite

`DmMc02Bmi088SpiLink` is the reusable composition boundary for a bus-independent
SPI controller plus the two BMI088 dies used by DM-MC02. It owns embedded
`DmMc02Spi spi`, `DmMc02Bmi088 accel`, `DmMc02Bmi088 gyro`, and matching
`DmMc02Bmi088Spi` framers. `dm_mc02_bmi088_spi_link_state_init()` establishes
the target table and framer-to-die pointers; the board may then attach DMA
channels, callbacks and the SPI `MemoryRegion`. Live GPIO selection must call
`dm_mc02_bmi088_spi_link_select_mask()`, which deliberately uses the normal
SPI target callbacks and records the selection snapshot.

The composite VMState is `dm_mc02_bmi088_spi_link_vmstate()` (version 1). Its
producer fields are the raw SPI data path, both BMI088 component states, both
framer cursors, and a `selected_mask_snapshot`. The live SPI `selected_mask`
is GPIO-derived runtime state and is excluded from the raw SPI description;
the composite `pre_save` copies it into the snapshot. The raw description is
available as `dm_mc02_spi_vmstate_raw()` and has no `post_load`, so a parent
composition can defer scheduler activation. The ordinary
`dm_mc02_spi_vmstate()` retains its existing standalone post-load behavior.

On load, child validators run first. The composite then requires the fixed
accelerometer and gyro slot identities, matching signal kinds, and
destination-owned framer-to-die pointers. It restores the selection through
`dm_mc02_spi_restore_selected_mask()`/the link restore API without invoking
target `select` callbacks; this is required because those callbacks terminate
and reset an in-flight BMI088 transaction. Only after that does it call
`dm_mc02_spi_sync_runtime()` to re-arm the destination SPI DMA timer. A valid
selection may contain zero, one, or multiple target bits; target contention
semantics remain owned by SPI.

The isolated gate is `test-dm-bmi088-spi-link-vmstate` `4/4`. The DM-MC02
machine now consumes the same link object for runtime initialization, reset,
DMA setup and GPIO CS projection, and the existing BMI088/SPI direct gates
remain passing. The composite is not registered with the machine, so it does
not establish whole-machine snapshot, save/load or migration support. GPIO
input restore, DMA/DMAMUX state and their machine-level ordering remain future
boundaries.

# 0.41 Reusable STM32H723 DMA/DMAMUX subsystem state contract

`DmMc02DmaSubsystem` is a reusable chip-layer composition containing two
`DmMc02Dma` stream controllers and two `DmMc02Dmamux` register windows. It does
not imply a one-to-one controller/mux relationship: the current H723 profile
uses DMAMUX1 channels 0..7 for DMA1 and 8..15 for DMA2, while DMAMUX2 remains a
separate window for a future controller.

The producer state is the two DMA register/reload/live-cursor/FIFO sets plus
the two DMAMUX `regs` and `generation` values. The boundary is
`dm_mc02_dma_subsystem_vmstate()` (version 2). Its serialized child order is
fixed identity markers, DMAMUX windows, and raw DMA state. The descriptor
accepts the pre-marker version-1 stream for compatibility. The markers make
the heterogeneous DMA1/2 and DMAMUX1/2 positions self-checking.
`vmstate_dm_mc02_dma_raw` shares the ordinary DMA field table but has no
post-load callback; the parent validates both DMA states with
`dm_mc02_dma_state_valid()` and only then calls
`dm_mc02_dma_sync_runtime()` for each controller.

`MemoryRegion`, channel offsets, stream-enable callbacks, IRQ handles,
request-cache arrays, derived IRQ levels and peripheral endpoint callbacks are
destination-owned runtime wiring or derived state. The destination must
restore that wiring before using the composite; a successful load invalidates
both request caches and reprojects level-sensitive IRQ outputs against the
restored registers and DMAMUX generations.

The isolated gate is `test-dm-dma-subsystem-vmstate` `6/6`, covering dynamic
round-trip, runtime wiring preservation, cache/IRQ reconstruction, identity
rejection, v1 compatibility, invalid FIFO, truncation and unsupported version.
The existing DMA VMState gate is `3/3`; `run-dma-smoke.sh`,
`run-dma-arbitration-smoke.sh` and
`run-dma-batch-smoke.sh` remain passing. This is a component contract only:
it is not registered with the DM-MC02 machine and does not establish
whole-machine snapshot/migration, endpoint state restoration, physical DMA
arbitration or cross-peripheral scheduler ordering.

# 0.42 DMA endpoint non-fatal backpressure boundary

The reusable `DmMc02DmaEndpoint` keeps its legacy bool `read`/`write`
callbacks and adds optional `read_ex`/`write_ex` callbacks returning
`DmMc02DmaEndpointResult`:

```c
DM_MC02_DMA_ENDPOINT_ACCEPTED
DM_MC02_DMA_ENDPOINT_RETRY
DM_MC02_DMA_ENDPOINT_ERROR
```

`ACCEPTED` is the commit boundary for one peripheral beat. `RETRY` means the
endpoint did not consume or publish the beat; the DMA caller may submit the
same request again. The DMA stream retains `NDTR`, live addresses, status
flags and already-prefetched FIFO bytes, and does not disable the stream or
set TEIF. A bounded endpoint batch stops at the first `RETRY` so it cannot
busy-loop in the guest/event producer. `ERROR` retains the existing fatal DMA
path, including TEIF/stream disable where applicable. A legacy callback
returning `false` maps to `ERROR`, preserving existing endpoint consumers.

`dm_mc02_dma_endpoint_transfer_result()` is the result-valued dispatch
boundary; `dm_mc02_dma_endpoint_transfer()` remains a compatibility wrapper
that returns true only for `ACCEPTED`. An extended callback must not mutate
endpoint state before returning `RETRY`; this is a caller-driven retry
contract, not an asynchronous timer or an implicit queue. External async
schedulers and rollback of a beat already consumed by a legacy P2M callback
remain outside this boundary.

The isolated `dm_mc02_dma_endpoint_smoke` checks legacy compatibility and
result propagation. The DMA chip-layer `dm_mc02_dma_fifo_dbm_endpoint_smoke`
checks direct and FIFO M2P retry plus direct and FIFO P2M retry, preserving
`NDTR`/cursor/FIFO/guest-memory/TEIF state and bounded-batch stop/retry
behavior.

## 0.43 Reusable USB control-core VMState invariant boundary

`dm_usb_control_vmstate()` is the component-only state contract for the
board-independent `DmUsbControlDevice`. The producer is the setup packet,
decoded request, control phase, bounded data buffer/cursors, pending address or
configuration status, and the committed address/configuration values. The
consumer is a future DWC2/device composite restore path. The control callbacks,
callback opaque, and `max_packet_size` configuration are destination-owned
runtime wiring/configuration; callbacks are never invoked while ordinary fields
are being loaded.

The version-1 stream keeps the existing fixed fields and encodes `size_t` as
64-bit big-endian. Post-load validates the state before any future consumer can
continue it:

- `data_length` and `data_offset` are within the bounded buffer;
  `DATA_IN` is an IN standard/class request with a non-empty payload or a ZLP,
  and its payload is no longer than setup `wLength`;
- `zero_length_packet` is permitted only in `DATA_IN`, requires
  `data_length < request.length`, and requires the payload to end on a
  `max_packet_size` boundary;
- `DATA_OUT` is an OUT request with non-zero `wLength`, has no committed
  `data_length`, and has a cursor strictly before the final byte while the
  receive phase is active;
- `STATUS_OUT` is an IN request whose data cursor has reached the payload end,
  with no pending address/configuration and no ZLP; `STATUS_IN` is either a
  matching standard `SET_ADDRESS`/`SET_CONFIGURATION` request with the exact
  pending `wValue`, or a completed class OUT transfer;
- address/configuration pending flags are mutually exclusive and cannot be
  attached to an unrelated request. A STALLED state may retain a partial OUT
  cursor from the failing operation, but cannot retain active pending status or
  a ZLP.

IDLE intentionally may retain the last request and completed payload fields,
because the runtime core does not clear that history when status completes;
only active pending/ZLP state and the general buffer bounds are constrained.
Invalid state returns before runtime wiring is synchronized. As with the other
component descriptions, VMState field writes before an error are not a
transactional rollback guarantee.

`test-dm-usb-control-vmstate` is the isolated gate (`6/6`), covering transfer
continuation, pending status callback behavior, cursor/version/phase rejection,
ZLP phase rejection, payload/request mismatch, and unrelated pending state.
This component is not registered with the DM-MC02 machine and does not claim
whole-machine snapshot/migration, DWC2/USB bus restore, PHY/SOF/DMA behavior,
or host enumeration.

## 0.44 QEMU USB host transport borrowed binding

`DmUsbHostQemuTransport` binds the board-independent host transaction API to
QEMU's standard `USBDevice`/`USBPort`/`USBPacket` infrastructure. The binding is
borrowed and exclusive:

```c
void dm_usb_host_qemu_transport_init(DmUsbHostQemuTransport *, USBDevice *);
void dm_usb_host_qemu_transport_init_port(DmUsbHostQemuTransport *, USBPort *);
void dm_usb_host_qemu_transport_clear(DmUsbHostQemuTransport *);
```

An object is valid only when exactly one of `device` and `port` is non-NULL.
`submit()` consumes a direct-device binding; `route()` consumes a port binding
and resolves the address through QEMU `usb_find_device()`. Both callbacks
reject a null transport, null transaction, and the wrong binding kind. The
caller must clear the binding before releasing the borrowed QEMU object or its
bus; the transport does not take a reference and does not unregister a port.
Direct submission additionally requires an attached device in
`USB_STATE_DEFAULT`; a detached or pre-reset device is `NO_DEVICE`.

Each call maps one `DmUsbTransaction` to one stack `USBPacket`. The caller owns
the setup/data buffer for the duration of the synchronous call. If QEMU leaves
the packet queued/asynchronous, the adapter cancels it before returning and
reports `DM_USB_TRANSACTION_DEFERRED`; no QEMU callback may retain the caller
buffer after cancellation. QEMU results map as follows:

| QEMU result | Generic result |
| --- | --- |
| `USB_RET_SUCCESS` | `ACCEPTED` |
| `USB_RET_NAK` | `NAK` |
| `USB_RET_STALL` | `STALL` |
| `USB_RET_NODEV` | `NO_DEVICE` |
| `USB_RET_BABBLE` | `BABBLE` |
| `USB_RET_IOERROR` | `IO_ERROR` |
| `USB_RET_ASYNC` / `USB_RET_ADD_TO_QUEUE` | `DEFERRED` |

The transport unit/bus gate is `test-dm-usb-qemu-adapter` `15/15`; it covers
direct and routed consumers, no-device routing, null/cleared bindings and
deferred cancellation. The generic endpoint consumer currently collapses the
new transport-only terminal results to its existing controller transaction
error where that controller has no richer status. This interface does not claim
USB PHY/VBUS timing, isochronous or stream support, multi-device topology, or
DM-MC02 host-role support.

## 0.45 QEMU USB control/DWC2 composite VMState

`DmUsbDwc2ControlLink` is the reusable component composition for a control
core and one DWC2 device-mode controller:

```c
void dm_usb_dwc2_control_link_init(
    DmUsbDwc2ControlLink *, const DmUsbControlOps *, void *,
    DmUsbDwc2Irq *, void *, uint16_t ep0_max_packet_size);
const VMStateDescription *dm_usb_dwc2_control_link_vmstate(void);
```

The version-1 stream is positional but self-checking: a control marker and
the complete raw control state precede a distinct DWC2 marker and raw DWC2
state. The control child is loaded first because it owns the active EP0
request/data/status phase; DWC2 raw state is loaded second. Neither raw child
runs its standalone post-load. The parent then validates both child states,
the destination-owned
`DmUsbDwc2Device.control == &link->control` relationship and
the control/DWC2 EP0 MPS match, and performs exactly one
`dm_usb_dwc2_sync_runtime()` call.

`dm_usb_control_vmstate_raw()` and `dm_usb_dwc2_vmstate_raw()` are only child
descriptions for this composition; their standalone descriptions retain the
existing validation/synchronization behavior. Control callbacks, opaque
values, IRQ callback/opaque/level, transaction callback graph, QOM ownership,
`MemoryRegion`, USB bus/PHY/SOF, DMA and asynchronous QEMU requests are
destination/runtime state and are not serialized. A failed load may already
have written ordinary fields because VMState is not transactional.

The isolated and direct-consumer gate is
`test-dm-usb-dwc2-control-link-vmstate` (`7/7`). It covers DATA_IN continuation,
pending `SET_ADDRESS`, simultaneous endpoint FIFO state, a synchronous
request-level consumer after restore, invalid phase/FIFO, reversed child
order, truncation and version rejection. This component is not registered
with the DM-MC02 machine and does not establish whole-machine migration.

## 0.50 DM-MC02 board GPIO/power reset composition

The board composition exposes one reset-stage helper:

```c
typedef void DmMc02BoardResetHook(void *opaque);

void dm_mc02_board_reset_gpio_power(
    const DmMc02BoardGpioPowerReset *reset);
```

The caller owns the GPIO array, component objects, hook lifetime and `opaque`;
the helper only borrows them for the duration of the call. A null component or
hook is skipped, and a null descriptor is a no-op. The caller must provide a
valid GPIO array whenever `gpio_count` is non-zero.

The order is part of the board boundary and is not interchangeable:

```text
GPIO bank reset
  -> board SPI-CS inactive hook
  -> SYSCFG reset -> EXTI reset
  -> external GPIO input re-injection
  -> board power model reset
  -> downstream power-consumer projection hook
```

The helper calls existing GPIO, SYSCFG, EXTI and power component APIs; it does
not duplicate their state machines. DM-MC02 supplies hooks for BMI088 CS
projection, GPIO input/EXTI level reconstruction and FDCAN/RS485/MCU power
projection. Other peripheral reset remains in `dm_mc02_machine_reset()`.
The power hook must project consumers without calling a second power setter,
so `dm_mc02_power_reset()` remains the single board-source ADC update in this
stage.

The isolated gate is `test-dm-board-reset` (`1/1`), which asserts the exact
event order. The direct DM-MC02 gate is `dm-mc02-reset-test` (`2/2`), covering
warm-reset RAM/peripheral behavior, CS/input/power re-projection and cold-reset
volatile-memory clearing. This boundary does not add machine-level VMState,
snapshot/migration, or a claim for complete reset behavior of unmodeled
peripherals.

## 0.51 STM32H723 ADC pair composite VMState

The reusable ADC pair boundary is represented by:

```c
typedef struct DmMc02AdcPair {
    DmMc02AdcCommon common;
    DmMc02Adc adc[2];
} DmMc02AdcPair;

bool dm_mc02_adc_pair_state_valid(const DmMc02AdcPair *);
void dm_mc02_adc_pair_sync_runtime(DmMc02AdcPair *);
const VMStateDescription *dm_mc02_adc_pair_vmstate(void);
```

The version-1 positional stream is ordered `common raw -> ADC1 raw -> ADC2 raw`.
`vmstate_dm_mc02_adc_common_raw` and `vmstate_dm_mc02_adc_raw` validate child
state without projecting runtime effects. The parent validates the cross-child
conversion identity, then synchronizes the common clock projection first and
both ADC scheduler/IRQ projections second. Timers, callbacks, DMA/DMAMUX,
IRQ handles, QOM ownership and MemoryRegions remain destination-owned wiring.

The pair validator rejects a common pending master/slave sample whose
conversion ID does not match the corresponding ADC active regular sequence,
and rejects invalid or truncated child state before any runtime projection.
The ordinary ADC validator requires all validity predicates to hold; it must
not return true when any invalidity predicate is true. VMState field writes
before an error remain non-transactional.

The isolated gate is `test-dm-adc-pair-vmstate` (`3/3`), with ADC single-device
(`3/3`) and common (`15/15`) gates retained. Direct ADC qtest is `62/62`, the
ADC smoke set and full QEMU smoke suite are passing. This is a component
contract only; it is not registered with the DM-MC02 machine and does not
claim whole-machine snapshot/migration or complete DMA/CPU/IRQ/RAM restore.

## 0.52 STM32H723 FDCAN/shared Message RAM composite VMState

The reusable FDCAN-to-SoC-RAM boundary is represented by:

```c
typedef struct DmMc02FdcanMsgRamLink {
    DmMc02Fdcan fdcan;
    DmMessageRam *msg_ram;
    uint32_t msg_ram_size;
} DmMc02FdcanMsgRamLink;

bool dm_mc02_fdcan_msg_ram_link_bind(DmMc02FdcanMsgRamLink *, DmMessageRam *);
bool dm_mc02_fdcan_msg_ram_link_state_valid(
    const DmMc02FdcanMsgRamLink *);
void dm_mc02_fdcan_msg_ram_link_sync_runtime(DmMc02FdcanMsgRamLink *);
const VMStateDescription *dm_mc02_fdcan_msg_ram_link_vmstate(void);
```

`DmMessageRam` remains the single owner of the QEMU RAM block and its bytes.
The link borrows that owner and serializes only a 32-bit geometry marker plus
the FDCAN raw component state. The marker is written before the raw child and
must equal the destination RAM size; the destination FDCAN pointers must
already reference that same owner's data and size. RAM bytes are not duplicated
by the link, so multiple FDCAN links can observe one shared SoC RAM block.

The version-1 restore order is fixed:

```text
Message-RAM geometry marker -> FDCAN raw state
  -> RAM identity/geometry and configured element bounds validation
  -> FDCAN timer/IRQ projection
```

`dm_mc02_fdcan_vmstate_raw()` does not project timer, IRQ, CAN-bus or chardev
runtime state. `dm_mc02_fdcan_message_ram_state_valid()` checks the H723
filter, FIFO, dedicated Rx-buffer (64 modelled indices) and Tx element spans
using 64-bit checked arithmetic; unconfigured FIFO/buffer spans are not
accessed by this boundary. RXESC F0DS/F1DS/RBDS and TXESC are decoded from
their actual fields. Callbacks, timer handles, IRQ handles, CAN/chardev
bindings, QOM/MemoryRegion ownership and the RAM contents remain destination
runtime state.

The isolated gate is `test-dm-fdcan-msg-ram-link` (`4/4`), covering round-trip
without RAM-byte overwrite, geometry mismatch before IRQ projection, FIFO and
dedicated-buffer bounds, and two links sharing one owner. Existing FDCAN
VMState (`5/5`), Message RAM owner (`2/2`), FDCAN qtest/smoke and the board
memory path remain separate gates. This is still a component contract: it is
not registered with DM-MC02 machine migration and does not claim whole-machine
RAM/FDCAN migration ordering, exact M_CAN arbitration or physical CAN timing.

## 0.53 STM32H723 GPIO/SYSCFG/EXTI composite VMState

The reusable SoC GPIO/interrupt composition is represented by:

```c
typedef struct DmMc02GpioExti {
    DmMc02GpioBank gpio[8];
    DmMc02Syscfg syscfg;
    DmMc02Exti exti;
    unsigned gpio_count; /* destination-owned profile geometry */
    DmMc02GpioExtiInputSync *input_sync; /* destination-owned board hook */
    void *input_sync_opaque;
} DmMc02GpioExti;

bool dm_mc02_gpio_exti_state_valid(const DmMc02GpioExti *);
void dm_mc02_gpio_exti_sync_runtime(DmMc02GpioExti *);
void dm_mc02_gpio_exti_set_input_sync(
    DmMc02GpioExti *, DmMc02GpioExtiInputSync *, void *);
const VMStateDescription *dm_mc02_gpio_exti_vmstate(void);
```

The version-1 positional stream is ordered `GPIOA..GPIOH raw -> SYSCFG raw ->
EXTI raw`. Standalone GPIO, SYSCFG and EXTI post-load callbacks are deliberately
not run by the raw children. The parent validates the destination bank count
and fixed bank identities, then projects GPIO ODR consumers, invokes the
board-owned input injection hook after the restored SYSCFG EXTICR mapping is
available, and finally rebuilds level-sensitive EXTI IRQ outputs.

`gpio_count`, MemoryRegions, IRQ handles, GPIO ODR callbacks and external input
sources are destination/runtime wiring. This boundary does not guess pull-up,
alternate-function or electrical behavior, and the generic parent does not
serialize board `external_gpio` state. A failed load cannot run projection
hooks, although VMState field writes before an error are not transactional.

The isolated gate is `test-dm-gpio-exti-link-vmstate` (`3/3`), covering child
ordering, restored routing and pending IRQ projection, invalid destination
geometry and truncation. The current DM-MC02 machine uses the composition as
the owner of its GPIO/SYSCFG/EXTI instances; the machine still has no
machine-level VMState, so this does not claim whole-machine snapshot/migration.

## 0.54 STM32H723 EXTI to native NVIC IRQ wiring boundary

The EXTI-to-interrupt-controller edge is represented by the narrow, reusable
route table:

```c
typedef struct DmMc02ExtiIrqRoute {
    unsigned exti_group;
    unsigned controller_input;
    qemu_irq controller_irq; /* borrowed destination input */
} DmMc02ExtiIrqRoute;

bool dm_mc02_exti_connect_nvic(
    DmMc02Exti *, const DmMc02ExtiIrqRoute *, size_t route_count,
    unsigned controller_input_count, Error **errp);
```

The caller obtains `controller_irq` from the already-realized destination
interrupt controller. The reusable EXTI producer validates group bounds,
controller input bounds, non-null targets, and duplicate groups/inputs before
changing any binding. Controller input numbers are not indexed into a local
seven-entry array; high H723 inputs such as 40 remain valid.

The DM-MC02 board profile supplies all seven mappings to QEMU's native
ARMv7-M/NVIC inputs. This API owns no CPU/NVIC state and serializes no IRQ
controller fields. After a component restore has loaded GPIO/SYSCFG/EXTI raw
state and the destination wiring exists, `dm_mc02_gpio_exti_sync_runtime()`
reprojects EXTI levels. An EXTI pending clear does not clear the NVIC pending
bit; firmware must perform the separate NVIC ICPR operation.

The isolated route gate is included in `test-dm-gpio-exti-link-vmstate` and
covers high controller input numbers plus atomic rejection of a later invalid
route. The direct consumer gate is
`dm-mc02-cpu-test` (`3/3`), which drives the board PA15 input through
EXTI15_10 to NVIC input 40 and separately clears EXTI and NVIC pending state.
This remains a wiring/direct-consumer contract, not a local CPU/NVIC VMState or
whole-machine snapshot/migration claim.

## 0.55 STM32H723 DMA/DMAMUX composite-to-machine wiring boundary

`DmMc02DmaSubsystem` is the single reusable chip-layer composition owner for
the current H723 DMA topology:

```c
DmMc02DmaSubsystem {
    DmMc02Dma dma[2];
    DmMc02Dmamux dmamux[2];
}
```

The DM-MC02 machine borrows these children for MMIO mapping, reset, request
routing, SPI/UART/ADC endpoints, and DMA IRQ callbacks. It does not retain
parallel `dma1`/`dma2`/`dmamux1`/`dmamux2` state. The current profile still
maps DMA1 to DMAMUX1 channels 0..7 and DMA2 to channels 8..15; DMAMUX2 is
kept as an independent future-controller window.

The composite VMState is now version 2. Four fixed wire markers identify the
heterogeneous positional children. A marker or destination identity mismatch
is rejected before DMA request-cache invalidation or IRQ projection. Version 1
streams without markers remain load-compatible, but the destination must
initialize the fixed identity before loading. The serialized order is
`markers -> DMAMUX raw children -> DMA raw children`.

`MemoryRegion`, channel offsets, stream-enable callbacks, IRQ handles,
endpoint objects and request caches remain destination wiring or derived
state. This is a chip-composition-to-machine-wiring boundary only; the
composite is not registered as DM-MC02 machine VMState and does not claim
whole-machine migration, RAM ordering, endpoint scheduler restoration or
physical DMA bus timing.

The focused gate is `test-dm-dma-subsystem-vmstate` (`6/6`), including v1
compatibility, identity rejection, truncation and no-projection-on-failure.
Direct gates are `run-dma-smoke.sh`, `run-dma-arbitration-smoke.sh`,
`run-dma-batch-smoke.sh`, `run-dma-fcr-smoke.sh`, `run-dma-irq-smoke.sh` and
`run-uart-dma-smoke.sh`.

## 0.56 DM-MC02 UART TX DMA endpoint backpressure

The UART TX consumer binds its fixed one-byte TDR endpoint to the reusable
`DmMc02DmaEndpoint` result contract. The machine property
`uart-dma-endpoint=on` (the default) selects this path for the board's UART
instances; `off` explicitly retains the legacy MMIO path for compatibility.
The endpoint receives only the configured UART peripheral address and one
byte; DMA continues to own DMAMUX selection, stream arbitration, address
movement, NDTR and status flags.

When the host-facing UART TX FIFO is full, the UART `write_ex` callback returns
`DM_MC02_DMA_ENDPOINT_RETRY`. This is a non-fatal, non-committing result:
TDR, UART FIFO contents, DMA NDTR/live cursor, stream enable and TEIF do not
advance. In FIFO M2P mode, memory words read only to form the rejected beat
are speculative and are rolled back along with the newly staged FIFO bytes;
already committed FIFO bytes remain. A later retry therefore starts from the
same memory and peripheral beat without duplication.

The retry owner is the UART's `QEMU_CLOCK_VIRTUAL` `dma_tx_timer`. A bounded
DMA batch stops at the first no-progress result. UART TX drain, chardev open,
DMA stream enable and endpoint configuration call the explicit DMA kick. A
closed chardev cancels the retry timer; pending UART TX state is resumed by
the open path. There is no wall-clock sleep, polling worker or unbounded
queue in this boundary.

The direct gate is `tests/qtest/dm-mc02-uart-test.c`, registered as
`dm-mc02-uart-test`. It runs with the real DM-MC02 UART1, DMA1 Stream1,
DMAMUX1 channel1 and QEMU `ringbuf` chardev, verifies FIFO-full
NDTR/PAR/M0AR/EN/TEIF preservation, advances controlled virtual time for
drain-driven continuation, and compares the complete wire sequence byte for
byte. The isolated DMA gate also covers speculative FIFO-fill rollback.

This TX boundary does not claim real UART bit/electrical timing, asynchronous
host transport completion, UART/DMA/DMAMUX joint restore, or machine-level
migration. At the time this TX-only boundary was introduced, UART RX used the
legacy synchronous callback. Direct P2M UART RX now has the separate
reservation contract in section 0.69; FIFO P2M and
`uart-dma-endpoint=off` deliberately retain the immediately-consuming
compatibility path. Other peripheral endpoint consumers still require their
own direct backpressure or reservation boundary.

## 0.57 STM32H723 IWDG window-mode boundary

`DmMc02Iwdg` exposes the H723 `WINR` register at offset `0x10` in addition to
the existing `KR`, `PR`, `RLR` and `SR` window. `WINR` is masked to 12 bits and
follows the existing `KR=0x5555` write-unlock gate. The reset value is
`0xfff`; the model treats `WINR >= RLR` as the default/disabled window
configuration, while `WINR < RLR` enables the descending reload window.

The producer owns the absolute virtual timeout deadline. At a reload request,
the model derives the current down-counter from that deadline, the configured
LSI frequency, prescaler and reload value. A started watchdog may reload only
when the derived counter is `<= WINR`; an early `0xaaaa` key increments the
diagnostic `window-violations` counter, cancels the deadline and requests a
guest reset. A valid reload increments `reloads` and re-arms the existing
deadline. A `WINR` write becomes visible only after the status-update boundary
below; its committed update automatically reloads the counter as documented by
the ST HAL. The simulation-only boot grace interval does not suppress this
check when its configured duration is zero.

The direct consumer is `tests/qtest/dm-mc02-iwdg-test.c` and uses QEMU's
controlled virtual clock. It checks the locked/default register, the exact
first-tick boundary, valid reload, early-reset diagnostic and timeout. The
component VMState continues to serialize the register array (including
`WINR`) and rejects reserved PR/RLR/WINR bits before runtime synchronization.
This remains a component/direct-consumer contract: it does not model LSI
frequency drift, per-tick counter MMIO visibility, independent-power-domain
behavior, or machine-level migration.

## 0.58 STM32H723 IWDG deterministic LSI configuration

The reusable timing boundary in `hw/arm/dm_mc02_iwdg_timing.[ch]` converts a
nominal LSI frequency and one fixed signed error in ppm into the IWDG virtual
timeout and the visible descending counter. The accepted configuration is a
non-zero nominal frequency and an error in `-999999..1000000` ppm. Conversion
uses an integer rational scale of `1000000`, `__uint128_t` intermediates, and
ceiling division; invalid or out-of-range timer results saturate at
`INT64_MAX`. The helper has no QEMU timer or board dependency and does not
schedule individual oscillator ticks.

The DM-MC02 machine owns the configuration boundary and exposes string
properties `iwdg-lsi-hz` and `iwdg-lsi-error-ppm`. Defaults are `32000` Hz and
`0` ppm. The settings are applied after the IWDG component is initialized and
before guest execution. They are startup-only while the watchdog is running;
attempts to change them while started are rejected, so an existing absolute
`next_timeout_ns` cannot be silently reinterpreted or extended. The component
setters return `bool` and likewise refuse live changes. The diagnostic string
reports nominal, fixed error, and rounded effective frequency.

The isolated gate is `test-dm-iwdg-timing` (`4/4`), covering configuration
validation, exact rounded-up timeout values, monotonic positive/negative error,
absolute-deadline counter derivation, and extreme inputs. The direct gate is
`dm-mc02-iwdg-test` (`5/5`), which checks positive, nominal, and negative fixed
error timeout thresholds using controlled virtual time and verifies diagnostic
configuration values. This boundary still does not model oscillator startup
settling, temperature/random drift, independent IWDG power, or machine-level
migration.

The timing helper treats `deadline_ns == 0` as the invalid/not-started
sentinel, including when `now_ns == 0`; an elapsed deadline is represented by
`deadline_ns != 0 && now_ns >= deadline_ns` and returns a zero counter. The
component setter follows the same contract and rejects a zero nominal LSI
frequency instead of converting it to the default. This prevents an invalid
configuration or an initial virtual-time sample from being mistaken for a
watchdog expiry.

## 0.59 STM32H723 IWDG configuration-update status boundary

`PR`, `RLR` and `WINR` are not immediately writable IWDG state. After
`KR=0x5555` unlocks the configuration window, an accepted write stores a
masked pending value, sets respectively `SR.PVU`, `SR.RVU` or `SR.WVU`, and
leaves the guest-readable committed register unchanged. While that register's
own flag is set, another write to it is ignored. `SR` is read-only to the
guest. Different configuration registers may have independent pending updates.

`dm_mc02_iwdg_status_update_delay_ns_for_config()` is a board/QEMU-independent
timing helper. It converts the configured nominal LSI plus fixed ppm error into
the ST H7 HAL's published upper bound of five LSI periods, with 128-bit rational
arithmetic and ceiling rounding. `DmMc02Iwdg` schedules only the pending
configuration deadlines—never one event per LSI tick. A completed `PR` or
`RLR` update changes only the committed configuration; it cannot reinterpret
an already armed absolute watchdog deadline. A completed `WINR` update commits
the new window and reloads the counter, matching the direct HAL consumer.

The timing unit (`5/5`) covers nominal and fixed-error delays. The component
VMState gate (`9/9`) covers v2 pending state, invalid SR/deadline pairs and
v1 loading. The direct qtest (`6/6`) checks locked writes, old-value visibility,
same-register suppression, the exact virtual commit deadline, HAL ordering and
automatic `WINR` reload. The model deliberately uses the documented upper
bound rather than modeling a random LSI synchronizer phase; it still does not
claim LSI startup/temperature behavior, an independently powered domain or
machine-level migration.

## 0.63 H723 brownout reset-reason boundary

`DmMc02Power` exposes a board-independent runtime brownout callback. It fires
once only when the discrete power state changes from `NORMAL` to
`UNDERVOLTAGE`, which currently means VIN crosses from `>=12000 mV` to
`1..11999 mV`. Initial low VIN is established before the machine connects this
runtime consumer, so it does not generate an event. `VIN=0`/`OFF`, an unchanged
undervoltage state, and recovery are separate transitions and do not repeat the
callback.

DM-MC02 consumes this callback by latching `RCC_RSR.BORRSTF` at offset `0xd0`,
bit 21, then requesting QEMU's ordinary reset machinery. PWR/RCC retains the
read-only source flag across reset and clears it only through `RCC_RSR.RMVF`.
The callback and its opaque pointer are runtime wiring, not component VMState.

The `12 V` threshold is an explicit discrete DM-MC02 model assumption. It is
not a calibrated STM32H723 BOR voltage and does not model analog ramp,
hysteresis, converter transients, or other reset sources. The isolated power
and PWR/RCC tests plus `dm-mc02-cpu-test` are the lower/direct gates; this does
not establish complete H723 power-domain or machine migration behavior.

## 0.64 H723 external NRST reset-reason boundary

The reusable QOM device dm-mc02-reset-input exposes one named GPIO input,
NRST, on the /machine/reset-input child. The input is active-low. A
high-to-low transition invokes the configured assertion callback exactly once;
repeated low levels are ignored until the input is released high again.

DM-MC02 is the immediate composition consumer of that callback. It first
latches H723 RCC_RSR.PINRSTF at offset 0xd0, bit 22, through
dm_mc02_pwr_rcc_note_pin_reset(), then requests QEMU's ordinary reset
fan-out. PWR/RCC owns the read-only source latch and RMVF clearing; the
input device does not depend on PWR/RCC or board state.

The external level is producer-owned runtime state. The board reset callback
does not force NRST high, so an externally held-low input remains asserted and
does not generate a second event. A later high-to-low transition is required
for another pin reset. The named GPIO, callback and input level are not
machine VMState.

The isolated test-dm-reset-input gate checks edge-only callback behavior and
held-low preservation. The direct dm-mc02-cpu-test gate checks reset fan-out,
PINRSTF preservation across an ordinary reset, low-level non-repetition and
RMVF clearing. This boundary does not claim CPURSTF, domain-specific resets,
low-power/WWDG sources, NRST filtering/debounce, complete reset-domain timing
or machine-level migration.

## 0.65 H723 WWDG1 window-watchdog boundary

`DmMc02Wwdg` is a reusable STM32H7 system-window-watchdog register component.
Its producer-owned state is the `CR`, `CFR`, and `SR` register contract, the
visible counter phase, and one virtual deadline. The component has no PWR/RCC
or DM-MC02 type dependency. Its clock is supplied through
`dm_mc02_wwdg_set_clock_hz()` and its interrupt/reset outputs are borrowed
runtime wiring.

The tick period is the ceiling of `4096 * 2^WDGTB / clock_hz` seconds. The
component schedules only the next observable transition: the first stage holds
visible `CNT` at `0x40`, sets `SR.EWIF`, and asserts IRQ when `CFR.EWI` is set;
the next tick reaches `0x3f` and requests reset. A running `CR.WDGA` is
set-only. A running reload with `T < 0x40`, or with the current visible counter
above `CFR.W`, is a window failure and requests reset. `SR.EWIF` is
write-zero-to-clear and does not automatically disappear on reload.

When `CFR.WDGTB` changes while active, the component first snapshots visible
`CNT` using the old divider and then starts the new phase at the current
virtual timestamp. The DM-MC02 profile maps WWDG1 at `0x50003000`, connects
its IRQ to H723 external vector 0, and supplies the board APB1 timer clock.
Its reset callback is mapped to `RCC_RSR.WWDG1RSTF` before QEMU consumes the
ordinary guest reset request.

The isolated `test-dm-wwdg-timing` gate covers the integer conversion; the
direct `dm-mc02-wwdg-test` gate covers defaults, EWI/IRQ/EWIF, set-only WDGA,
prescaler phase, window failure, timeout, and reset-reason projection. The
current behavioral reference is the local Renode H7 WWDG model. This does
not claim complete H723 reset-domain semantics, machine migration, or
silicon-level clock/electrical precision.

## 0.66 H723 WWDG1 component VMState boundary

`dm_mc02_wwdg_vmstate()` serializes the producer-owned WWDG state: the complete
implemented register window (`CR/CFR/SR` plus zero-valued unimplemented
locations), visible counter, `counter_start_ns`, `next_event_ns`,
`started/reset_stage`, and diagnostic counters. It intentionally excludes the
destination's APB clock configuration, `QEMUTimer`, IRQ handle, reset callback,
and board/PWR/RCC wiring.

The normal description validates the destination clock, implemented register
bits, counter range, active deadline, and EWI/reset-stage relationship before
calling `dm_mc02_wwdg_sync_runtime()`. An elapsed absolute deadline is armed
immediately on the destination rather than restarting a full watchdog window.
The raw description uses the same validation but does not project timer or IRQ
state, allowing a future parent composite to control restore ordering. Both
descriptions are version 1; malformed or truncated streams are rejected before
runtime projection.

The isolated gate is `test-dm-wwdg-vmstate` (`8/8`), covering normal round-trip,
reset-stage state, raw no-projection, invalid deadline/stage/clock/register
state, and truncation. This is a component restore contract only; it does not
register DM-MC02 machine VMState or claim joint CPU/NVIC, PWR/RCC, RAM, bus,
timer, or full reset-domain migration.

## 0.67 H723 DMA P2M FIFO overflow transaction boundary

The reusable DMA FIFO treats one P2M peripheral-width beat as a transaction.
Before the producer is observed, `dm_mc02_dma_request_fifo()` checks that
`fifo_length + PSIZE <= DM_MC02_DMA_FIFO_BYTES`. This check applies equally to
an endpoint callback and to a peripheral MMIO read, so a rejected beat cannot
consume an RX register or mutate an external device.

If the check fails, DMA latches `FEIF`, clears the private FIFO and clears the
stream `EN`. The rejected beat does not change `NDTR`, `PAR`, the active M0/M1
memory cursor or guest memory. `FCR.FEIE` is evaluated only by the
level-sensitive IRQ projection; it does not suppress or create the `FEIF`
status bit. The direct host gate is
`tests/dma_fifo_dbm_endpoint_smoke.c`, which fills the FIFO with both FEIE
settings and verifies callback count, state preservation, and cleanup.

This is a deterministic capacity and transaction boundary, not a claim about
all silicon FIFO-error causes, bus arbitration, burst timing, or recovery
behavior. The ARM guest FCR smoke remains a direct regression for the
guest-visible FCR/DME/FIFO paths, but cannot construct private FIFO occupancy
without adding a test-only device interface, which is intentionally excluded.

## 0.68 DMA direct P2M endpoint reservation

`DmMc02DmaEndpoint` optionally exposes a complete source reservation tuple:

```c
DmMc02DmaEndpointResult read_prepare(void *opaque, uint8_t *data,
                                      unsigned size, uint64_t timestamp_ns);
void read_commit(void *opaque);
void read_abort(void *opaque);
```

The producer owns the source queue. `read_prepare` copies exactly one beat to
the caller's borrowed buffer but does not advance that queue. For a direct P2M
request, the DMA boundary then performs the destination memory transaction.
`MEMTX_OK` selects exactly one `read_commit`; any other result selects exactly
one `read_abort`. The endpoint must be empty of reservation state when the
request returns. `RETRY` from `read_prepare` selects neither callback and
leaves the DMA stream unchanged.

The tuple is all-or-nothing. If any reservation callback is present without
the other two, the direct P2M request returns endpoint `ERROR` before calling
the partial contract. The existing `read/read_ex` callbacks remain a legacy
immediately-consuming path for compatibility; `read_prepare_result()` uses
that path only when no reservation callback is present. The helper is used by
the direct P2M path, while FIFO P2M deliberately remains outside this
single-beat contract.

Reservation callbacks borrow the data buffer only for the synchronous call;
the endpoint owns any source reservation. The DMA interface does not undo
partial destination-side effects made before a QEMU memory transaction returns
non-`MEMTX_OK`; a stronger destination atomicity contract is a separate
lower-layer boundary. At the time of this boundary, the direct consumers were
OCTOSPI and UART RX; their source cursors advance only in `read_commit`. SPI RX
was added by the later 0.70 consumer slice. This interface is a component
boundary, not machine-level VMState or migration support.

## 0.69 UART RX direct P2M source reservation

The UART RX endpoint implements the reusable direct-P2M reservation tuple for
its CPU-visible RX FIFO. `read_prepare` copies the current `rx_fifo_head` byte
and records its queue identity without advancing `rx_fifo_head` or
`rx_fifo_len`. A successful DMA destination write invokes `read_commit`, which
advances that exact head and updates the UART's RDR/RXNE projection. Any
non-`MEMTX_OK` destination result invokes `read_abort`; the source byte and
RXNE remain observable for a later stream reconfiguration. A synchronous
reservation prevents a re-entrant CPU RDR read from stealing the reserved head.

The legacy `read` callback remains present for the FIFO P2M path and for
compatibility when `uart-dma-endpoint=off`; it is immediately consuming and
does not provide rollback. The UART reservation fields are transient runtime
state and are not part of the UART component VMState because the DMA prepare
and commit/abort calls are synchronous and must not span an externally
observable boundary.

`tools/run-uart-rx-reservation-smoke.sh` is the direct consumer gate. It uses
the real UART1, DMA1 Stream0 and DMAMUX1 request 41: a deliberately invalid
destination must set TEIF and disable the stream while retaining RXNE, then a
valid reconfiguration must transfer the same byte exactly once. This gate
does not claim FIFO multi-beat reservation, destination-side atomic rollback,
machine-level migration, or physical UART timing.

## 0.70 SPI RX direct P2M source reservation

The SPI2 RX endpoint implements the same reusable one-beat reservation boundary for
the single CPU-visible `RXDR` result. `read_prepare` copies the pending result and
marks it reserved without clearing `RXDR`/`RXP`. The direct DMA path then performs its
destination memory transaction; `MEMTX_OK` invokes `read_commit`, which clears the
pending result, while any other result invokes `read_abort`, which only releases the
reservation. A synchronous reservation also prevents a re-entrant CPU `RXDR` read
from consuming the result between prepare and commit.

The DM-MC02 board wiring calls `dm_mc02_spi_dma_rx_stream_enabled()` only for the
configured SPI RX DMA controller and stream. This lets a guest re-enable a failed
P2M stream and retry the still-pending result without another SPI clock. The hook
does not own DMA arbitration, addresses, status, or queue state. The direct guest
gate is `tools/run-spi2-rx-reservation-smoke.sh`, using SPI2/DMA1 Stream3/DMAMUX1
request 39: an invalid destination must leave `RXP`, the byte, `NDTR`, and the
source stream state observable; a valid reconfiguration must transfer the same
BMI088 gyro `0x0f` response exactly once.

This remains a synchronous single-slot/single-beat contract. It does not define
SPI RX FIFO or overrun replacement semantics, multi-beat reservations, rollback of
partial destination side effects, SPI/DMA joint migration, or physical SPI timing.

## 0.71 ADC1 `ADC_DR` direct P2M source reservation

ADC1 implements the reusable direct-P2M reservation tuple for its single pending
regular conversion result. `read_prepare` copies `ADC_DR` without clearing
`ISR.EOC`; the direct DMA destination write is the commit boundary. A successful
`MEMTX_OK` invokes `read_commit`, which consumes the result through the ordinary
ADC data-read path and clears `EOC`. Any other destination result invokes
`read_abort`, which only releases the reservation and preserves `ADC_DR` and
`EOC` for a retry. A synchronous reservation also prevents a re-entrant CPU
`ADC_DR` read from consuming the result between prepare and commit.

The DM-MC02 DMA1 board wiring observes each P2M stream-enable edge, while the ADC
consumer itself checks its configured DMA/DMAMUX wiring, endpoint tuple and
`EOC` before issuing one request. This allows a failed ADC1 DMA destination to
be reconfigured and retried without a new ADC conversion. DMA remains the owner
of stream selection, addresses, `NDTR`, `TEIF`/`TCIF` and IRQ state.

The direct guest gate is `tools/run-adc-rx-reservation-smoke.sh`, using ADC1
request 9 on DMA1 Stream2/DMAMUX1. It selects one real SQR rank, verifies that
an invalid destination retains `EOC`, `NDTR`, `ADC_DR` and reports `TEIF`, then
verifies that a valid re-enable writes the same `0x0100` sample exactly once.
The existing `tools/run-adc-dma-smoke.sh` on/off paths remain regression gates
for circular ADC DMA and the legacy address-space path.

This boundary is limited to synchronous direct P2M, one ADC1 `ADC_DR` slot and
the existing regular-DMA status policy. It does not define ADC FIFO or
ADC12_COMMON `CDR/CDR2` multi-beat reservations, destination-side rollback,
ADC/DMA joint migration, or physical ADC/DMA timing.

## 0.72 ADC12 common CDR direct P2M source reservation

`DmMc02AdcCommon` exposes a board-independent, synchronous CDR source
reservation API:

```c
bool dm_mc02_adc_common_cdr_data_pending(const DmMc02AdcCommon *state);
bool dm_mc02_adc_common_cdr_read_prepare(DmMc02AdcCommon *state,
                                         uint8_t *data, unsigned size);
void dm_mc02_adc_common_cdr_read_commit(DmMc02AdcCommon *state);
void dm_mc02_adc_common_cdr_read_abort(DmMc02AdcCommon *state);
```

It applies only to a supported CDR format and one direct P2M beat of 1, 2 or
4 bytes. `prepare` copies the pending CDR word and marks only the transient
reservation; it does not call the CDR read callback or clear either producer
EOC. An adapter maps a successful prepare to the existing complete
`DmMc02DmaEndpoint` tuple. After `dma_memory_write()` succeeds it calls
`commit`, which clears `cdr_valid` and invokes the existing CDR read-ack
consumer exactly once. On every non-success result it calls `abort`, which
only releases the reservation.

`cdr_valid` is component producer state in ADC-common VMState v6. v1–v5 loads
normalize it to false. `cdr_read_reserved` is runtime-only and both normal and
raw component pre-save reject a non-empty reservation; it is never serialized.
The DM-MC02 direct endpoint adapter may resubmit a pending source only from a
P2M stream-enable event and must leave stream selection, DMA/DMAMUX matching,
addresses, NDTR, error/complete flags and IRQs to the DMA layer.

This API deliberately does not retrofit the legacy CDR MMIO path, CDR2,
FIFO P2M, multi-beat buffering, partial target-memory rollback or composite
ADC/DMA/machine migration. Those paths need independently owned source and
acknowledgement contracts.

## 0.73 ADC12 common CDR2 direct P2M source reservation

CDR2 has a separate source reservation API for regular-interleaved
`DUAL=0x7/0x3` with `DAMDF=0x2`:

```c
bool dm_mc02_adc_common_cdr2_data_pending(const DmMc02AdcCommon *state);
bool dm_mc02_adc_common_cdr2_read_prepare(DmMc02AdcCommon *state,
                                          uint8_t *data, unsigned size);
void dm_mc02_adc_common_cdr2_read_commit(DmMc02AdcCommon *state);
void dm_mc02_adc_common_cdr2_read_abort(DmMc02AdcCommon *state);
```

`prepare` copies the current `RDATA_ALT` result and records its producer
source without clearing EOC. A successful direct DMA destination write invokes
`commit`, which acknowledges only that source; any other destination result
invokes `abort` and leaves the result/EOC available for a later stream-enable
retry. The callback tuple is complete or rejected; it is not an alternative
to the legacy immediately-consuming CDR2 MMIO path.

The reservation and saved source are synchronous runtime state and are not
serialized by ADC-common VMState. Normal/raw component saves reject an active
reservation. This interface is limited to one direct-P2M beat and does not
define CDR2 FIFO/multi-beat buffering, destination partial rollback, composite
ADC/DMA/machine migration or physical DMA timing.

The focused gate is `test-dm-adc-common` (`17/17`); the direct consumer gate
is `dm-mc02-adc-test` (`64/64`) for `DUAL=0x7` and `0x3`. The broader QEMU
smoke and Host CTest results are integration evidence only and do not imply
machine-level migration or physical DMA timing support.
# 2026-09-10 CDR DMA admission correction

`dm_mc02_adc_common_cdr_dma_request_pending()` combines unread CDR state
with the existing regular-simultaneous DAMDF=2 OVR request gate. Both
publication and board DMA retry use this chip-owned policy. The generic
`cdr_data_pending()` and CPU CDR acknowledgement remain independent of OVR.
Clearing OVR permits retry of the retained word without another conversion;
DMA stream selection, addresses and status remain DMA-owned. This does not
extend overrun semantics to other common formats or establish migration.

`dm_mc02_adc_common_cdr_read_consuming()` and
`dm_mc02_adc_common_cdr2_read_consuming()` provide the legacy FIFO endpoint
read boundary. They accept 1/2/4 bytes at the start of an unread word, emit
little-endian bytes and acknowledge immediately using the existing register
consumer. CDR acknowledges both producers; CDR2 acknowledges only its saved
source. Invalid size, null output, absent data or active reservation returns
false without consumption. They leave no reservation and provide no rollback
if a later FIFO destination write fails. DMA direct mode selects the complete
reservation tuple instead of these legacy reads. No FIFO multi-beat transaction
or additional VMState is introduced.
# QMP adapter

`tools/dm_mc02_qmp.py` exposes `QmpSession(path, timeout=...)`, `command()`
for decoded return values, `command_raw()` for envelope assertions, and
`close()`. Construction connects and negotiates capabilities exactly once.
It imports the pinned client from `qemu/upstream/python/qemu/qmp`; all smoke,
diagnostic and Release RTF tools use this boundary. A consumer may still own
its UART, CAN, co-sim or qtest socket, but cannot read or frame QMP separately.

Tool-local memory/diagnostic readers return only decoded values. They do not
accept or return a placeholder receive buffer: transport buffering is owned
by the upstream client. The firmware RTF collector's readers return an integer
tick count and an `(integer timeout_count, diagnostics_text)` pair respectively.
