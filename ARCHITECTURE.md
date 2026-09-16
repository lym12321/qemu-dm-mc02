# 0.51 STM32H723 SoC RAM ownership and migration registration

The reusable STM32H723 memory composition owns the backing bytes for the
internal Flash and CPU-visible RAM windows. Dynamic bytes use QEMU's canonical
`memory_region_init_ram()` path with a `NULL` owner, which calls
`vmstate_register_ram_global()` and gives each RAMBlock a stable migration
identity. The public machine-owner parameter is retained for API compatibility,
but is not passed to QEMU because `MachineState` is not a `DeviceState`.

The six dynamic regions are the internal Flash, ITCM, DTCM, AXI SRAM, D2 SRAM
and D3 SRAM. They remain owned by `DmMc02SocMemory`; board code may map and use
them but may not add a second byte store or a SoC-private VMState for the same
bytes. The Flash programming overlay continues to borrow the Flash storage
pointer and does not change this ownership rule.

The UID/factory-calibration window is immutable deterministic profile data and
remains non-migratable ROM, rebuilt at realize time. FDCAN Message RAM remains
the separate `DmMessageRam` owner. No local RAM VMState is introduced and no
machine-level migration is enabled by this change.

Reset semantics are independent of migration registration. Warm reset keeps
ordinary RAM and internal Flash; the existing explicit cold-reset policy clears
ITCM/DTCM/AXI/D2/D3 SRAM only. The focused owner test asserts the registration
choice, while the direct DM-MC02 memory qtest asserts actual address-space and
reset behavior. A future machine composite must restore RAM blocks before
consumers such as CPU execution, DMA, FDCAN and other peripherals can depend on
their contents, and must define the remaining child restore order first.

# 0.50 STM32H723 OCTOSPI/OCTOSPIM state boundary

The external Flash path is layered as `DmMc02Ospi` register semantics ->
`DmMc02SsiNor` adapter -> QEMU's standard `w25q64/m25p80`. The OSPI controller
owns its register mirror, indirect transaction cursors, bounded RX/TX staging,
DLR-derived continuation state and the memory-mapped access gate. The SSI/NOR
device owns NOR command semantics, backing bytes and its native QEMU VMState.
No second production NOR state machine is introduced.

The version-1 OSPI component VMState serializes only controller-owned dynamic
state: registers, RX bytes/cursor, TX bytes/cursor, command address/opcode and
command-valid/started flags. It intentionally excludes `MemoryRegion`, DMA
callbacks, SSI bus/device pointers, Flash storage, and static profile geometry.
The variable RX buffer is length-prefixed by the serialized `rx_size`, bounded
to 1 MiB, and allocated after the destination's old staging buffer is released.
Validation rejects impossible cursor/length and active-command combinations.

Normal post-load derives `memory_mapped` from `CR.FMODE` and restores active-low
SSI CS only when the controller was midway through page program. Raw post-load
performs validation but no projection, allowing a parent composite to restore
the standard Flash child and all wiring before one projection. `OCTOSPIM` is a
separate register-only component. These descriptions remain unregistered from
the DM-MC02 machine, so they do not establish whole-machine snapshot/migration.

The isolated gate is `test-dm-ospi-vmstate` (`6/6`); direct gates are the
existing OSPI and alternate-board profile smokes. The remaining restore risks
are standard Flash child/backing-storage semantics, OSPI/DMA/CPU/IRQ ordering,
board reset composition and full line-mode/DTR/async-WIP timing.

# DM-MC02 QEMU Architecture Contract

## 0.49 DM-MC02 board power state boundary

The board power model is a board-layer producer, not part of the generic
STM32H723 ADC or GPIO implementation. `DmMc02Power` consumes the profile's
static output masks and ADC channel mapping plus dynamic `vin_mv`, GPIO ODR and
the optional electrical-power policy. It projects discrete external states to
24 V/5 V/3.3 V status and to the reusable ADC board-source interface.

Its component VMState deliberately serializes only the three dynamic inputs:
`vin_mv`, `gpio_odr` and `electrical_power`. The ADC pointer and profile wiring
are destination-owned runtime configuration. Rail booleans, ADC raw samples and
`last_update_ns` are derived state; serializing them would allow stale output
or a source timestamp from a different virtual-time epoch to survive restore.
Normal load performs one `dm_mc02_power_sync_runtime()` after the fields are
complete. The raw description performs no callback and is reserved for a
future board composite that restores all children before one projection.

The default VIN is 24 V. The current model has three deterministic VIN states:
0 V/off, 1..11.999 V/undervoltage and >=12 V/normal. This is a functional
board boundary, not an analog power-electronics model; converter transients,
current, ripple and detailed brownout curves remain unsupported.

The isolated gate is `test-dm-power-vmstate` (`4/4`) and the direct gates are
the existing power-boundary and power-runtime smoke scripts. The component is
not registered in DM-MC02 machine-level migration, so this slice does not
claim whole-machine snapshot/migration.

## 0.48 ARMv7-M CPU/NVIC/SysTick reuse and restore boundary

The STM32H723 SoC uses QEMU's native ARMv7-M composition for CPU, exception,
NVIC and SysTick behavior. `ARMv7MState` owns the `nvic` and
`systick-reg-ns` children through `object_initialize_child()`. During
`armv7m_realize()`, the configured Cortex-M7 is created as the `cpu` child
with `object_new_with_props()` and realized through `qdev_realize()`. QEMU
therefore owns the canonical QOM graph and its lifecycle; the board model
only supplies configuration, clock sources, memory and external IRQ routes.

The realize order is part of the boundary: CPU first, NVIC second, then
SysTick after its CPU clock is connected. NVIC GPIO aliases are exposed to
the board only after NVIC realization. This avoids a board profile reaching
into private NVIC arrays or duplicating the CPU/exception implementation.

The full NVIC vector count is `179` (`163` external inputs plus `16` internal
exceptions). A board profile must keep those values distinct. SysTick is an
internal exception and is routed through the existing NVIC SysTick trigger;
its pending state is not an external NVIC `ISPR` bit. DM-MC02's reset clock
fixture is 64 MHz, so the focused virtual-time test can use reload `63` for a
1 µs expiration without introducing a wall-clock dependency.

The native child VMState descriptions are canonical: ARM CPU v22, NVIC v4 and
SysTick v3. QEMU registers them when the realized devices are realized, so a
local DM-MC02 CPU/NVIC/SysTick VMState would be a duplicate implementation and
is prohibited. A future composite restore must first construct the destination
QOM graph, clock roots and IRQ/memory wiring, then load these child states in
the QEMU-owned order. This slice has no machine-level VMState and does not
claim whole-machine snapshot/migration or complete H723 exception/debug/
security semantics.

The focused direct gate is `dm-mc02-cpu-test` (`2/2`), covering QOM child
types, vector-width distinction, external IRQ register/level behavior,
SysTick virtual time, internal pending visibility and reset clearing.

## 0.46 Message-RAM ownership and migration boundary

The shared STM32H723 FDCAN message-RAM window belongs to the reusable SoC
memory composition, not to the DM-MC02 machine or an individual FDCAN
instance. `DmMc02SocMemory.fdcan_msg_ram` owns one `DmMessageRam`; FDCAN1/2/3
receive only a borrowed byte pointer and their configured bounds. The SoC
maps the single region at `0x4000ac00` with the profile's `0x2800` size, so
all FDCAN instances observe the same guest bytes and cannot silently create
per-controller copies.

`DmMessageRam` calls QEMU's standard `memory_region_init_ram()`. The current
composition passes `NULL` as the QEMU memory-region owner, which deliberately
registers a global migratable RAM block. This is the canonical RAM-byte
ownership path; the owner does not add a parallel VMState description. The
embedded `MemoryRegion` and data pointer remain destination runtime wiring for
FDCAN component restore.

Reset is an explicit SoC/board operation. DM-MC02 system reset clears the
message-RAM bytes at the same reset boundary as the previous board-local
region. Initialization itself does not define a universal reset policy, which
keeps the reusable owner suitable for profiles with different power/reset
behavior.

The staged gate is `test-dm-message-ram` (`2/2`) for accessor/reset behavior,
`dm-mc02-memory-test` (`1/1`) for the real system-mode RAM mapping, and the
existing FDCAN standard/medium/bus-off smoke gates for direct consumers. This
boundary only makes the RAM block migration-registered; it does not register
or claim DM-MC02 machine-level snapshot/migration. A future machine composite
must order RAM-byte restoration before FDCAN can consume the borrowed pointer,
and separately define CPU/IRQ/NVIC, other RAM, peripheral, bus/chardev and
co-simulation state restoration.

This document is normative for the QEMU backend. It constrains design choices,
not just the current implementation. `AGENTS.md` defines the development
workflow, `INTERFACES.md` defines public data contracts, `PLAN.md` records
incremental work, and `REVIEW.md` records evidence and residual risk.

## Product Contract

The product is a fast, deterministic DM-MC02 development and test tool. It
executes the real firmware against a layered STM32H723/DM-MC02 model, exposes
stable diagnostics and protocol-neutral co-simulation interfaces, and can
operate in wall-clock real-time when its documented standard profile has the
required execution capacity. A convenient fixture, UI, or external plant must
not alter the modeled board semantics.

### Behavior Evidence

Behavior is decided by the first applicable source in this order:

1. Reproducible observation on the physical board.
2. Vendor reference manual, data sheet, errata, HAL and CMSIS definitions.
3. Observable behavior of the versioned `trobot` firmware.
4. Pinned upstream emulator behavior.
5. Existing local behavior.

Physical observations win conflicts but do not erase the vendor specification.
Each exception needs a short decision record containing the board revision,
firmware identity, instrumentation and stimulus, the vendor reference, the
affected configuration, and a deterministic regression oracle. If evidence is
missing, the feature is *unsupported*, rather than guessed or hidden behind a
board-level fallback.

## Layering and Public Boundaries

Dependencies point only downward through a narrow public interface.

| Layer | Owns | Must not own |
| --- | --- | --- |
| QEMU foundation | ARMv7-M, QOM, memory, IRQ, Clock, virtual timer, chardev, bus and VMState primitives | H723 register policy or board routing |
| STM32H723 SoC | CPU-visible register semantics, clocks, reset, DMA requests, peripheral data paths | DM-MC02 pins, power rails, motor policy or external process state |
| DM-MC02 profile | pin/AF map, rails, peripheral instances, IRQ routes and external-device composition | generic peripheral state machines or protocol codecs |
| Reusable devices/adapters | BMI088, transceiver behavior, protocol codecs and time/command/state adapters | QEMU machine internals or a particular board profile |
| External backend | MuJoCo, Gazebo, ROS 2, SocketCAN, replay and test fixtures | QEMU private state and board-private packet formats |
| User tooling | CLI, stable QMP diagnostics, profiling and launch profiles | modeled peripheral behavior |

Every implementation slice records its owner, producer, boundary, consumer,
clock domain, reset owner, minimum isolated test and immediate integration
gate. Board profiles may pass data and callbacks into a reusable component but
may not inspect or mutate its private state.

## Reuse and Replacement Policy

QEMU is the canonical production implementation for the following generic
facilities: `MemoryRegion`, IRQ/Clock/timer, `CharBackend`, `USBBus`/`USBPort`/
`USBPacket`, `CanBusState`/`CanBusClientState`, SSI devices including `m25p80`,
and VMState. A local model is allowed only for a register-facing STM32H723
wrapper or a behavior QEMU does not supply.

| Capability | Production direction | Local code retained |
| --- | --- | --- |
| USB host transactions | QEMU USB core and host-controller integration | H723 DWC2 register wrapper; existing host stack only as a named fixture until removed |
| USB device role on DM-MC02 | H723 device controller and endpoint wrapper | DM-MC02 remains device-only; no host bus or passthrough is mapped into its device MMIO window |
| W25Q64/NOR die | QEMU `m25p80` behind SSI | H723 OCTOSPI/OCTOSPIM controller and board routing |
| CAN transport | QEMU standard CAN bus and optional SocketCAN backend | H723 FDCAN registers/message RAM; local medium only as deterministic test fixture |
| External physics | MuJoCo/Gazebo/ROS 2 implementations | protocol-neutral time/command/state adapter and DM-MC02 mapping |
| Renode infrastructure | the pinned Renode submodule | project-specific H723/board adapters only; no second canonical Infrastructure tree |

### CAN transport boundary (2026-09-01)

The production CAN transport is QEMU's standard `CanBusState`. The reusable
`DmCanBusAdapter` translates a controller endpoint to
`CanBusClientState`; FDCAN remains responsible for M_CAN registers, message
RAM, acceptance filters, FIFO/Buffer state, IRQs and coarse controller error
state. The DM-MC02 machine creates one internal bus by default and accepts an
explicit `can-bus` object for external QEMU clients.

QEMU standard CAN provides immediate peer delivery, sender exclusion and a
peer-acceptance result. It does not provide CAN-ID arbitration, frame
duration, bit stuffing, physical ACK, error frames, or frame timestamps. The
FDCAN standard-bus ingress therefore stamps received frames with the current
virtual clock. The fixed 84-byte chardev remains a separate legacy host wire;
it preserves its supplied timestamp and is not routed through `CanBusState`.
`fdcan-host-ack` is an explicit host-wire policy and is not a physical ACK
claim. A future accurate virtual-time scheduler must be a separate reusable
adapter above the standard bus, with its own isolated and boundary tests.

`dm_mc02_can_medium.[ch]` is retained only as a clearly marked test fixture
for that future scheduler work. It is not listed in the ARM Meson production
sources and must not become a runtime alternative to `CanBusState`.

### FDCAN component VMState (2026-09-02)

The reusable FDCAN state producer includes the register mirror, host-wire
partial RX frame, guest-visible FIFO/Buffer state, TX pending mask, diagnostic
counters, and the bounded host-wire TX queue. The queue's absolute retry
deadline is part of the component because a short or blocked chardev write
must resume in the same virtual-time phase. A queue can remain pending without
a deadline while the peer is disconnected; a deadline cannot exist without a
pending queue.

The current stream version is 2. Version 1 remains load-compatible; its missing
internal `bus_off` participation gate is reconstructed from `PSR.BO` before
validation. The stream excludes message RAM and its pointer because the current board
composition owns that backing memory and its ownership has not yet been
defined for migration. It also excludes `MemoryRegion`, `CanBusState`,
`CharBackend`, IRQ handles, transceiver power and host-ACK policy. The ordinary
post-load validates all bounded state first, then rebuilds only derived FIFO
sizes, the destination retry timer and the level-sensitive IRQ projection.
The raw description only normalizes/validates legacy `bus_off`; it does not
project timer, IRQ, bus or chardev state and is reserved for a future machine
composite with an explicit message-RAM and bus-state ordering. Rebuilding a
timer never flushes the chardev; host output resumes only through the normal
runtime event path.

For each replacement, first add an isolated adapter test and a differential
test against the old behavior where it represents a supported contract. Then
run the direct board/firmware gate and the standard performance profile. Once
they pass, delete the old production path or move it into a test-only target.
There is no long-lived runtime selection between two equivalent production
implementations.

### DWC2 device observation and MMIO boundary (2026-09-02)

The reusable DWC2 device core owns endpoint FIFO state. Board adapters observe
pending bytes only through `dm_usb_dwc2_endpoint_fifo_count()`; they must not
reach into `DmUsbDwc2Endpoint` fields. The query is read-only, bounded to the
16 modeled endpoints, and reports invalid arguments without side effects.

The DM-MC02 USB `MemoryRegion` keeps guest unaligned accesses invalid while
preventing QEMU's internal access-size adjustment from splitting one invalid
cross-register transaction into valid writes to adjacent `DIEPTXF` registers.
This is an MMIO boundary rule, not a second DWC2 register implementation. The
core still owns `DIEPTXF[0..14]` state and its direct register access contract.

### DWC2 device-mode component VMState (2026-09-02)

`dm_usb_dwc2_vmstate()` is a reusable component contract, not a machine
migration registration. It serializes the DWC2 device-mode register state,
`DIEPTXF[0..14]`, endpoint IN/OUT FIFO bytes and cursors, transfer counters,
DMA addresses, transaction endpoint configuration/PID/halt state, and FIFO
overflow count. The in-process FIFO cursors are `size_t`, but their wire
representation is fixed 64-bit big-endian; enum values use fixed 32-bit
big-endian encoding. This keeps the stream independent of host ABI widths.

The destination's `ep0_max_packet_size` is static configuration and is checked
with an equal field. Post-load validates the version, EP0 control invariant,
FIFO bounds, endpoint type and DATA0/DATA1 PID before calling
`dm_usb_dwc2_sync_runtime()`. That function restores the destination-owned
transaction callback graph and reprojects the level-sensitive IRQ. Control
state, `MemoryRegion`, QOM ownership, IRQ callback pointers/opaque, current
IRQ level, USB PHY, DMA, SOF and asynchronous USB transactions are not in this
component stream. Invalid state/version returns before runtime synchronization;
the VMState mechanism may still have written ordinary fields before reporting
the error, so this is not a transactional rollback guarantee.

The isolated contract test is `4/4`, with DWC2 core `7/7`, QEMU USB adapter
`13/13`, DM-MC02 USB qtest `10/10`, and the ARM system target relink passing.
The component is deliberately not registered with the DM-MC02 machine. A
whole-machine snapshot claim remains blocked on the control core, CPU/IRQ/NVIC,
RAM, DMA/DMAMUX, USB bus/PHY, scheduler and co-simulation queue restore order.

### DWC2/control composite restore ordering (2026-09-02)

The reusable `DmUsbDwc2ControlLink` is the next lower-layer composition. It
embeds one `DmUsbControlDevice` and one `DmUsbDwc2Device`; the
`DmUsbDwc2Device.control` pointer is destination wiring and must point to the
embedded control object. Its version-1 stream has fixed child markers and
loads:

```text
control marker -> control raw state -> DWC2 marker -> DWC2 raw state
```

The raw descriptions deliberately omit child post-load side effects. The
parent first checks both child validators, then checks the pointer and EP0 MPS
composition invariant, and finally invokes `dm_usb_dwc2_sync_runtime()` once.
This prevents DWC2 from projecting an IRQ or rebuilding a transaction callback
graph while control state is only partially restored. Swapped child streams
are rejected by the distinct markers, rather than being allowed to depend on
accidental field values.

The direct request-level consumer test drives the restored control object
through the same complete-request contract used by `DmUsbQemuAdapter`, while
the token continuation test drives the same control core through DWC2. This
proves the two consumers can share the restored lower state; it does not claim
that QEMU's USB adapter object, bus topology, PHY/SOF, DMA or asynchronous
control completion is migratable. The composite remains unregistered from the
DM-MC02 machine; CPU/IRQ/NVIC, RAM, board peripherals and co-simulation queue
ordering still need separate lower-layer boundaries.

### STM32H723 PWR/RCC component VMState (2026-09-02)

The reusable `DmMc02PwrRcc` state producer is the PWR/RCC register mirrors,
the effective RCC system-clock source, and the ADC compatibility/configuration
phase. Its version-1 stream contains `pwr_regs[0x400]`, `rcc_regs[0x400]`,
`system_clock_source`, and `adc_clock_configured`. The effective source is
serialized separately from the guest's `RCC_CFGR.SW` request because an
unready request must not retime consumers or change `SWS` prematurely.

`MemoryRegion`, clock callback/opaque, QOM owner and board wiring are
destination-owned runtime state. The ordinary description validates the
effective source after all fields are loaded, then calls
`dm_mc02_pwr_rcc_sync_runtime()` exactly once so the DM-MC02 clock root can
reproject CPU, timer, ADC and peripheral kernel clocks. The raw description
has the same field table without post-load side effects and is reserved for a
future parent composite that defines its own ordering and final sync.

The isolated gate is `test-dm-pwr-rcc-vmstate` (`4/4`), including raw no-sync
and malformed-stream cases. PWR/RCC, zero-clock, dynamic TIM2, USART/FDCAN
clock, alternate-profile and full QEMU/Host regression gates pass. The
component remains unregistered from the machine-level VMState; complete H723
PWR/RCC semantics, oscillator settling and joint restore ordering with
DMA/timers/ADC/UART/FDCAN are not implied.

Generic ARMv7-M builds must never pull in DM-MC02, H723, or co-simulation
sources. STM32H723 and DM-MC02 are separate QEMU feature symbols; fixture-only
sources belong to test targets. Third-party code has one canonical pinned
source. Local modifications state their upstream base and exit condition in
the associated decision record.

The current QEMU upstream base is v8.2.2. The DM-MC02 NOR migration carries a
small local patch in `hw/block/m25p80.c` for W25Q-compatible WEL consumption
after successful program/erase and address alignment for block erase, plus
narrow realized-device accessors in `include/hw/block/flash.h`. These changes
are generic m25p80 behavior, are covered by the DM-MC02 OSPI boundary smoke,
and must be re-audited against upstream before changing the QEMU baseline.

The reproducible build profiles are explicit: `tools/build-qemu.sh` owns the
DM-MC02 Release profile (`arm-softmmu` with `--with-devices-arm=dm-mc02`),
while `tools/build-qemu-generic.sh` owns the isolated generic ARM profile in
`build/qemu-generic` (`arm-softmmu` with `--without-default-devices`). The
generic profile may contain QEMU's dependency-selected ARM/Virt support, but
must not contain the project feature symbols or their source objects. The
profiles use separate build directories and are never selected implicitly at
runtime.

## Time, Reset, Snapshot, and Real-Time Performance

The sole modeled timebase is monotonic virtual nanoseconds. External timestamps
are mapped into that domain by a protocol-neutral adapter. A reset starts a new
time/session epoch; replay, qtest and integration tests use virtual time, not
wall-clock sleeps. Wall-clock is reserved for explicit user pacing, host-I/O
deadlines and measurement, and cannot affect a deterministic result.

The implemented release gate starts Release `trobot` from reset in QEMU with
default DM-MC02 peripherals, `-nodefaults`, and `-serial none`. The collector
does not launch a worker or NullEngine and does not connect an external plant.
It measures a continuous 60 virtual-second sample after `xTickCount >= 250`:

- `1.0x` is the RTF target. The release script accepts `0.999x` as a measurement
  tolerance for QMP/wall-clock sampling jitter; it still reports the exact factor
  and a sustained result materially below `1.0x` fails.
- A user-facing paced run must track `1.0x` without watchdog timeout.
- This collector does not provide separate unpaced-capacity and worker-paced
  measurements. Those, including a connected NullEngine or physical plant
  profile, remain separate validation gates; the firmware-only result cannot
  establish them.
- Report three runs' startup latency, RTF, CPU and RSS; a profile with a
  debugger, trace, non-default firmware, Gazebo or MuJoCo is measured and
  reported separately.

The collector launches with `-S` and admits an epoch only after QMP reports
`prelaunch` with `running=false`. While stopped it records the initial uint32
FreeRTOS tick and IWDG timeout count, drains startup events, then issues
`cont`. Baseline, startup-ready and virtual-window observations use the same
`TickEpochTracker`: any QMP `RESET` in the complete event batch, any IWDG
timeout-count change, or any tick delta at least `0x80000000` invalidates the
sample before progress is committed. The modulo delta admits a real
`0xffffffff -> 0` wrap. At the end, QMP `stop` must produce the `paused`
runstate before the final tick/watchdog snapshot; a second status/event barrier
closes the last reset race. This is measurement validity for the named
firmware profile, not a new guest timebase or reset-domain support claim.

The startup lifecycle uses one host-monotonic deadline from immediately before
QEMU launch through socket creation, the complete upstream QMP
connect/greeting/capabilities coroutine, initial status/watchdog/tick commands,
`cont`, and the virtual-mode ready tick. The default is 10 seconds; each QMP
command is additionally bounded to at most 2 seconds. All wall-clock numeric
inputs must be finite, and a virtual window that rounds below one FreeRTOS tick
is invalid rather than an empty passing sample.

QEMU stderr is written to a per-run temporary file so a full pipe cannot stop
guest progress. Every success or failure path performs bounded cleanup in this
order: TERM and a 2-second wait, KILL and a 2-second wait when required, QMP
disconnect with a 2-second adapter timeout, stderr close, then temporary-tree
removal. The adapter applies connect/disconnect deadlines around the pinned
upstream coroutines; it does not duplicate QMP framing or negotiation. These
host deadlines constrain the measurement tool only and do not alter virtual
time or guest behavior.

`tools/run-release-rtf-gate.sh` enforces this profile, the 60 *virtual*-second
target, the `0.999x` measurement threshold and the three-run report. The hot path
may not allocate, write files, poll external
processes, or add speculative recovery work. Check real external input at the
public boundary; do not add error handling for impossible internal states
without an inexpensive invariant or evidence.

Snapshot, save/load, or migration is unsupported until every local device and
RAM region in the composition has an audited VMState contract and save/load
regression. Using `memory_region_init_ram_nomigrate()`, ordinary heap state, or
an upstream device's VMState does not establish whole-machine support.

The reusable VMState component contracts currently cover the STM32H723 PWR/RCC,
GPIO,
internal Flash, EXTI, DMAMUX, DMA stream, TIM2, ADC, IWDG, CORDIC, CRC, RNG,
DBGMCU, SYSCFG, FMC, SPI and USART models, plus the BMI088 SPI framing
adapter.
`dm_mc02_gpio_vmstate()`
serializes only its eight CPU-visible register mirrors and invokes the
destination ODR callback after load; `dm_mc02_cordic_vmstate()` serializes only
its CSR, bounded argument/result arrays and queue cursors;
`dm_mc02_crc_vmstate()` serializes only its register mirror and accumulator;
`dm_mc02_rng_vmstate()` serializes its register mirror, PRNG/FIFO state and
refill marker; `dm_mc02_dbgmcu_vmstate()` serializes its mutable register
mirror while deriving the fixed read-only IDCODE; and
`dm_mc02_syscfg_vmstate()` serializes its register mirror and invokes the
destination routing callback after load; `dm_mc02_fmc_vmstate()` serializes
only its register mirror; and `dm_mc02_flash_vmstate()` serializes its
register mirror and unlock-sequence markers, rebuilding the programming
overlay after load; `dm_mc02_exti_vmstate()` serializes its register mirror
and sampled input levels, rebuilding all seven IRQ projections after load;
`dm_mc02_dmamux_vmstate()` serializes its register mirror and DMA cache
generation without a post-load side effect; `dm_mc02_dma_vmstate()` serializes
DMA stream live/reload/FIFO state and rebuilds request/IRQ runtime projections;
and `dm_mc02_tim2_vmstate()` serializes timer shadow configuration, virtual
phase anchors, active deadlines, repetition/OCREF/Break state and batching
policy, rebuilding QEMUTimer scheduling and IRQ projection after load;
`dm_mc02_adc_vmstate()` serializes ADC register mirrors, regular/injected
conversion progress, calibration/regulator state, injected context snapshots,
source overrides and virtual deadlines, rebuilding ADC-owned timers and IRQ
projection after load. A zero regular/injected deadline is the explicit
stopped-kernel-clock marker; when the destination already has a valid ADC
clock, the remaining half-cycles/cycles and phase anchor rebuild the deadline
instead of leaving an active operation disarmed.
The ADC regular data path decodes SQR1/SQR2/SQR3/SQR4 ranks 1..16 using the
H723 six-bit rank fields. ADC1/ADC2 regular simultaneous pairing is owned by
the separate common component through its shared conversion-ID boundary for
both software starts and regular external-trigger edges. External edges are
master-owned by ADC1; the composition starts an enabled ADC2 peer through a
narrow callback and the explicit ADC API, while the common component does not
inspect ADC private state. A completed pair invokes a runtime packed-data
callback; the DM-MC02 composition may consume it through the board's DMA1
request 9 using either the endpoint or address-space path. For regular
interleaved `DUAL=0x7` and regular-interleaved-plus-injected-simultaneous
`DUAL=0x3`, each completed rank updates the zero-extended single-result
`CDR2.RDATA_ALT` register. With `DAMDF=0` this is polling-only; with
`DAMDF=0x2` the common block also emits one 32-bit CDR2 DMA event per rank.
The DM-MC02 composition routes it through ADC1 request 9 to the CDR2 address
and source-specific read acknowledgement. ADC1/master-only admission and the
delayed ADC2 peer start are defined by the separate cadence boundary below;
other dual/DAMDF modes remain outside this component boundary.
The common component's version-5 VMState also preserves bounded multimode
pairing/packing state, including the aggregate timestamp for a partially
accumulated DAMDF=3 word; callbacks, MemoryRegion and QOM ownership remain
runtime wiring.
`dm_mc02_iwdg_vmstate()` serializes the IWDG register mirror, lock/start/grace
state, its absolute virtual timeout deadline, and deferred PR/RLR/WINR update
values/deadlines. It rebuilds the watchdog and configuration-update timers only
after their SR/deadline invariants pass; LSI frequency, grace configuration and
diagnostic counters remain runtime/configuration state.
`dm_mc02_spi_vmstate()` serializes SPI configuration, transfer/RX state, EOT
and the deferred TX-DMA virtual deadline; `dm_mc02_uart_vmstate()` serializes
USART register/FIFO state, counters and four absolute virtual deadlines for
RX delivery, IDLE, paced TX and DMA-TX continuation. The separate
`dm_mc02_bmi088_spi_vmstate()` serializes only one BMI088 SPI framer's command,
direction, dummy and register cursors; its target pointer/callback wiring and
the BMI088 sensor state remain outside this contract. The separate
`dm_usb_dwc2_vmstate()` serializes DWC2 registers, `DIEPTXF`, endpoint FIFO
bytes/cursors, transfer state, DMA addresses and transaction endpoint config;
its control device, callback graph, IRQ wiring and USB transport remain
destination-owned.
All exclude `MemoryRegion`, QOM
ownership and runtime callbacks from the stream and are independently
round-tripped. CORDIC and RNG reject malformed queue state, while DBGMCU,
SYSCFG, FMC, Flash, EXTI, DMAMUX, DMA, TIM2 and ADC reject malformed or truncated
state streams at the VMState boundary. GPIO, DBGMCU, SYSCFG, FMC, Flash,
EXTI and DMAMUX are pure
register/connection contracts;
none of these descriptions is
registered with the DM-MC02 machine yet: component coverage must be completed
for local peripherals, RAM and external connection boundaries before any
machine-level snapshot claim is enabled.

## Validation and User-Facing Compatibility

Every support claim requires all of the following evidence:

1. An isolated SoC/device test with exact register, byte, timestamp or IRQ
   assertions.
2. A direct producer-to-consumer boundary test.
3. A firmware integration test when the real firmware consumes the behavior.
4. Registration in the authoritative test runner; an unregistered script is
   not a regression gate.

`tools/dm_mc02_test_gate.py` owns the authoritative project gate. It builds
before taking binary identities, then runs four disjoint inventories: selected
DM Meson unit/qtests, native Host CTest entries selected with
`-LE gate-external`, the complete pytest inventory with host plugin autoload
disabled, and the stable shell-smoke inventory. A test belongs to exactly one
inventory. The historical CTest Python and shell delegators remain available
for direct use but carry the `gate-external` label and are not counted by the
authoritative Host collection. Smoke scripts consume prebuilt production QEMU
and Host binaries; they may still build an individual guest fixture.

The gate records each dynamic inventory, command, result, raw exit status and
QEMU/Host SHA-256 identity in
`build/test-results/qemu-gate/<run>/summary.json`. PASS, FAIL and command-line
errors return 0, 1 and 2. Exit 77 means SKIP only for the explicitly optional
ROS2 and MuJoCo backends; a required dependency or result that is absent is
BLOCKED. A run containing only BLOCKED failures returns 78, while FAIL takes
precedence over BLOCKED. Binary identity drift during testing is a failure.
This gate covers the project inventories, not all upstream QEMU tests or a
real external plant performance profile.

Public user interfaces are versioned wire protocols, documented launch
profiles, and read-only QMP diagnostics. External backends must use those
interfaces and cannot depend on QOM paths, board-private structs, or a CAN/USB
fixture. Compatibility changes require a versioned interface and a migration
test; `v1` stays available until its documented deprecation decision.

Remaining migration candidates are tracked in `PLAN.md`: custom USB host
transaction layers and whole-machine VMState. The NOR die and CAN transport
migrations are complete; their residual limitations remain documented above
and in `INTERFACES.md`. These entries are not evidence that the corresponding
complete physical or host-passthrough capability already exists.
The ADC1/ADC2 common window is a separate `DmMc02AdcCommon` component rather
than a third ADC instance or duplicated machine fields. Its CSR is a live
projection of the two ADC status producers, and its CCR is the sole common
clock-configuration producer. The CDR data boundary accepts completed
regular-rank events from ADC1/ADC2 and packs only regular simultaneous
`DUAL=0x6` with `DAMDF=2` or `3`; matching is by a common-generated non-zero
`conversion_id` and zero-based rank, while timestamps remain diagnostic and
need not be equal. Independent/external-trigger samples use ID zero and are
rejected by this matcher. One pending slot per source is the fixed capacity.
In that mode, software `ADSTART` is master-owned: the common admission
boundary allocates the ID, asks the DM-MC02 composition to start an enabled
ADC2 peer, and rejects a slave-owned start. The callback is runtime wiring and
survives reset. Multimode CDR DMA is supported for this same `DUAL=0x6`,
`DAMDF=2/3` boundary when ADC1 regular DMA is enabled: one completed pair
invokes one packed-data callback and the DM-MC02 composition routes it to
DMA1 request 9 through either a reusable endpoint or the CDR MMIO address.
The common component has an independent VMState v5
description (v1 streams remain
loadable without the generator field; the generator frontier is reconstructed
from legacy pending IDs), but it is not registered with the
machine-level migration composition; its pending pair state and generator are
component state and its callbacks/MemoryRegion are runtime wiring. Regular
external-trigger admission follows the same boundary: ADC1 owns the edge in
`DUAL=0x6`, the common block allocates the shared ID, and the board composition
delivers the event to ADC2 without a second common admission. Coalesced edge
batches are currently one sequence, and exact per-edge queueing is not a
support claim. CDR2 DMA is limited to the separate `DUAL=0x7/0x3`,
`DAMDF=0x2` boundary below; other dual/DAMDF modes and exact physical DMA
timing remain unsupported.

## 2026-09-02 ADC12 common CDR DMA decision

The common block is the producer of a packed CDR word only after its bounded
master/slave matcher has received the same non-zero `conversion_id` and
zero-based rank from both ADCs. Its runtime callback is:

```c
typedef void (*DmMc02AdcCommonDataReady)(void *, uint32_t data,
                                         uint64_t timestamp_ns);
void dm_mc02_adc_common_set_data_ready_callback(
    DmMc02AdcCommon *, DmMc02AdcCommonDataReady, void *opaque);
```

The board composition is the consumer. It checks the public
`dm_mc02_adc_regular_dma_enabled()` predicate and raises the board's ADC1
request through `dm_mc02_dma_request_endpoint()` when endpoint mode is selected,
or `dm_mc02_dma_request()` with `ADC12_COMMON.CDR` when the MMIO comparison path
is selected. The callback carries monotonic QEMU virtual time; it does not
perform host I/O or become VMState.

The supported data formats are regular simultaneous `CCR.DUAL=0x6` with
`DAMDF=0x2` (ADC1 low 16 bits, ADC2 high 16 bits), or `DAMDF=0x3` (two
matching pairs form one CDR word in successive byte lanes). Regular
interleaved `CCR.DUAL=0x7` and regular-interleaved-plus-injected-simultaneous
`CCR.DUAL=0x3` also support `DAMDF=0x3`: four producer values in strict
master/slave/master/slave order form one CDR word. Each complete format emits
one DMA beat; partial 8-bit accumulation emits none. The DMA request cache key
includes both the masked request ID and the peripheral endpoint address because
ADC1 `DR` and `ADC12_COMMON.CDR` may share request 9. This key is routing state,
not guest-visible VMState.

The direct gates are `/dm-mc02/adc/common-multimode-dma-endpoint` and its
MMIO counterpart, plus the endpoint/MMIO interleaved DAMDF=3 gates. They use
the HAL ordering (configure common CCR, configure DMA, enable ADC2, start
ADC1) and verify complete words, partial-word suppression and consecutive
beats. This does not establish other multimode formats, physical DMA
arbitration/timing, or machine-level migration support. CDR2 DMA is defined by
the separate CDR2 boundary below. The interleaved trigger
cadence is defined by the separate ADC cadence boundary below. CDR read
acknowledgement is defined by the separate common/ADC consumer boundary below.

## 2026-09-02 ADC12 regular-interleaved CDR2 decision

The local STM32H723 CMSIS header defines `CDR2.RDATA_ALT` as regular data from
the master/slave alternated ADCs, while the H723 LL definitions identify
`DUAL=0x7` as regular interleaved and `DUAL=0x3` as regular interleaved plus
injected simultaneous. The reusable common block therefore has a narrow data
boundary for those modes: every completed regular-rank producer event writes
the zero-extended 16-bit value to CDR2, and a later event replaces it.

The original polling-only decision is superseded by the evidence-backed
`DAMDF=0x2` CDR2 DMA boundary below. The local HAL's
`LL_ADC_DMA_GetRegAddr()` still exposes only `DR` and the multimode `CDR`
address, so this project models CDR2 DMA through the common block's explicit
request-9 composition boundary rather than silently changing that HAL helper.
`DAMDF=0` remains polling/read-only state, while `DAMDF=0x3` remains
unsupported for CDR2 specifically; its regular-interleaved CDR packing is
defined by the separate boundary below. Other formats remain unsupported. The
direct qtest starts the two compact producers
explicitly to verify both the data register and DMA boundaries; the exact
modeled interleaved trigger cadence and `CCR.DELAY` phase are recorded in the
cadence decision below.

## 2026-09-02 ADC12 CDR read acknowledgement decision

The common CDR read side effect is a narrow runtime boundary, not common-owned
ADC status. For the supported regular-simultaneous `DUAL=0x6` with
`DAMDF=0x2/0x3`, and the regular-interleaved `DUAL=0x7/0x3` DAMDF=0x3 path, a
legal MMIO access overlapping CDR invokes the
`DmMc02AdcCommonCdrRead` callback after assembling the returned bytes. The
callback is not invoked for CDR2 or unsupported CDR formats, and it is runtime
wiring excluded from VMState.

The DM-MC02 composition consumes this notification by calling the public
`dm_mc02_adc_acknowledge_regular_data()` API for both ADCs. That API shares the
normal ADC data-consumption continuation, so it clears both EOC flags and
releases `AUTDLY` at the same virtual event. Endpoint DMA bypasses the common
MemoryRegion and therefore explicitly calls
`dm_mc02_adc_common_notify_cdr_read()` only after successful transfer; MMIO DMA
reaches the same notification through the common read callback. This keeps
producer, transport boundary and consumer semantics aligned without exposing
board state in the reusable common component.

The direct qtest is `/dm-mc02/adc/common-cdr-read-acknowledges-both-eoc`, with
the endpoint and MMIO CDR DMA gates covering their respective transport
boundaries. The isolated common gate is `test-dm-adc-common` `14/14`, and the
full ADC qtest is `59/59`. CDR2 acknowledgement is covered by the separate
CDR2 DMA boundary below. This does not claim other dual/DAMDF acknowledgement
semantics, physical DMA timing, or machine-level migration.

## 2026-09-02 ADC12 software master/slave start decision

RM0468 and the STM32H7 HAL establish ADC1 as the regular-simultaneous master:
`HAL_ADCEx_MultiModeStart_DMA()` enables the slave and issues the regular start
on the master. The QEMU boundary mirrors that ownership without embedding ADC
private state in the common register component. The same master-only admission
rule is used for regular-interleaved `DUAL=0x7` and
regular-interleaved-plus-injected-simultaneous `DUAL=0x3`; a direct slave
request or second trigger is rejected. For interleaved operation, the
composition starts the enabled ADC2 peer at an absolute virtual timestamp
resolved from the master's sampling phase and `CCR.DELAY`. The peer admission
carries the trigger source, edge, event count, timestamp and common-generated
conversion ID. Exact per-edge event-count queueing and injected common-data
packing remain outside the support claim.

## 2026-09-02 ADC12 regular-interleaved cadence decision

The reusable ADC kernel exposes `dm_mc02_adc_start_regular_at()`,
`dm_mc02_adc_external_trigger_with_id_at()` and
`dm_mc02_adc_interleaved_slave_start_ns()` so the board composition does not
duplicate ADC timing logic. The last API resolves the slave sampling start as
the master start plus the master sampling phase plus the resolution-dependent
`CCR.DELAY` entry from RM0468 Table 236. The kernel then schedules the peer's
normal rank using its own sampling time, conversion cycles and effective ADC
kernel clock. Virtual deadlines use integer nanoseconds and round up; a stopped
kernel clock is represented explicitly by `UINT64_MAX` and leaves the peer
paused when the clock callback runs.

The isolated common gate is `test-dm-adc-common` `14/14`. The direct gates are
`/dm-mc02/adc/common-interleaved-external-trigger-cadence` and
`/dm-mc02/adc/common-interleaved-injected-simultaneous`; the full ADC qtest is
`59/59`. `DUAL=0x6` remains on the regular-simultaneous path. CDR2 DMA is
defined by the separate boundary below; other dual/DAMDF formats, exact
physical DMA timing and machine-level migration are not implied by this
boundary.

## 2026-09-02 ADC12 regular-interleaved CDR2 DMA decision

This slice remains at the STM32H723 ADC/common-to-DMA boundary. The common
component is the producer of one CDR2 event per completed regular rank; the
DM-MC02 composition is the direct consumer and owns request routing. Supported
configurations are only `DUAL=0x7` or `DUAL=0x3` with `DAMDF=0x2`.

`CDR2.RDATA_ALT` is a single 32-bit register containing the zero-extended
16-bit result from the most recently completed ADC1/ADC2 regular rank. The
common callback carries the source and monotonic virtual timestamp. A later
rank overwrites an unconsumed value; no queue is introduced. A successful
CDR2 read acknowledges only that source's EOC through the public ADC data
consumption API. Endpoint DMA uses an explicit read notification because it
does not traverse the common `MemoryRegion`; MMIO DMA obtains the same
notification from the CDR2 read hook.

The board consumer requires ADC1 regular DMA to be enabled and routes the
event through DMA request 9 to the CDR2 address. This keeps the common model
board-independent and keeps the existing CDR and CDR2 consumers on distinct
address-keyed DMA paths. `DAMDF=0` remains the polling-only CDR2 data-register
mode; `DAMDF=0x3` CDR2 is not supported here, while regular-interleaved
DAMDF=3 CDR packing is defined by the separate boundary below. Injected
common-data packing, other dual modes and precise physical DMA
arbitration/transfer timing remain unsupported.

The component VMState is version 5 and includes the CDR2 value, validity and
source metadata plus DAMDF=3 partial-packing state and its aggregate timestamp
in addition to the existing common state. Callbacks,
`MemoryRegion` and machine ownership remain runtime wiring, and the DM-MC02
machine still does not register this component for whole-machine migration.
The isolated unit gate is `test-dm-adc-common` `14/14`; the endpoint and MMIO
direct gates both pass, and the full ADC qtest is `59/59`.

## 2026-09-02 ADC12 regular-interleaved DAMDF=3 decision

RM0468 28.4.32 defines `DAMDF=0b11` as the 8-bit multimode format. For
regular-interleaved `DUAL=0x7` and regular-interleaved-plus-injected-
simultaneous `DUAL=0x3`, the common producer sequence is master, slave, master,
slave. Four consecutive low bytes form one CDR word:

```text
CDR[7:0]   = master(t0)
CDR[15:8]  = slave(t0)
CDR[23:16] = master(t1)
CDR[31:24] = slave(t1)
```

`DmMc02AdcCommon` owns a bounded four-value accumulator and publishes one
data-ready event only after all four sources arrive. A source-order violation
drops the partial word and the violating event; it does not synthesize a
cadence from unrelated ADC results. For the resulting word, the timestamp is
the maximum of all four producer timestamps, so the common-to-DMA event cannot
move virtual time backwards when callbacks are delivered out of order.

The DM-MC02 consumer routes the event through ADC1 request 9 to the CDR
address. Correct firmware configuration uses a half-word peripheral and
memory transfer, so each event transfers `CDR[15:0]` (the two bytes for the
first pair) while the complete packed word remains visible in CDR. Endpoint
and MMIO DMA share the existing read acknowledgement callback. This slice
does not add a separate DMA queue or claim physical DMA arbitration, and it
does not enable DAMDF=3 on CDR2; `CDR2` remains the DAMDF=2 interleaved path.

The common component VMState is version 5. It persists the packed data,
source cursor, count and aggregate timestamp so a component restore cannot
reorder or retimestamp a partial word. v1-v4 streams clear the newly added
timestamp field during post-load. The isolated gate is
`test-dm-adc-common` `14/14`; direct endpoint/MMIO gates are
`/dm-mc02/adc/common-interleaved-damdf8-dma-endpoint` and
`/dm-mc02/adc/common-interleaved-damdf8-dma-mmio`; the full ADC qtest is
`59/59`, QEMU smoke is `89/89`, and Host CTest is `54/54`.

## 2026-09-02 ADC regular DMA overrun request-gate decision

The ADC-local regular DMA request boundary now follows the H723 overrun
contract. `adc_emit_sample()` captures `ISR.OVR` on entry and treats an
already-pending `EOC` as the current conversion's overrun. Either condition
suppresses that ADC's `DR` DMA request. `CFGR.OVRMOD` remains only a data
register policy: it preserves the previous `DR` value when clear and exposes
the new value when set. `OVR` remains a guest-visible ISR W1C flag and is the
only persistent request-gate state; no DMA-private latch was added.

This gate is deliberately limited to the ADC-local `DR` producer. The
ADC12-common CDR/CDR2 callbacks are separate consumers with their own
data-ready and acknowledgement contracts. In particular, regular-interleaved
DAMDF=3 may hold ADC `EOC` while collecting a four-value CDR word, so routing
the ADC-local OVR gate into common CDR would drop a valid partial accumulation.

The direct boundary qtest
`/dm-mc02/adc/regular-dma-overrun-request-gate` covers endpoint and MMIO
consumers with both `OVRMOD` settings. It verifies that an unserved stream
produces `EOC` then `OVR`, leaves `NDTR` unchanged while `OVR` is set, and
resumes only after ISR W1C plus normal DR consumption. The full ADC qtest is
`60/60`. This decision does not claim common CDR/CDR2 suppression based on
ADC OVR, other dual/DAMDF modes, precise physical DMA arbitration or transfer
timing, or machine-level migration.

## 2026-09-02 ADC12 common CDR DMA overrun request-gate decision

The common-to-DMA overrun gate is a separate, deliberately narrower boundary
than the ADC-local `DR` gate above. It applies only to regular-simultaneous
`CCR.DUAL=0x6` and `DAMDF=0x2`, where a complete ADC1/ADC2 pair updates CDR and
would otherwise invoke the common `data_ready` request callback. The common
status callbacks inspect the producer ADCs' guest-visible OVR bit; if either is
set, CDR is updated but the callback is suppressed. No common or DMA-private
latched state is introduced. A later pair can request DMA after both ADCs have
had OVR cleared by their ISR W1C paths.

The gate is not generalized to DAMDF=3. Its regular-simultaneous accumulator
has a valid partial-word state in which producer EOC may remain asserted before
the second pair completes; applying the common gate there would lose a valid
CDR word. CDR2 and other dual/data-management modes remain unsupported by this
decision. The unit and direct endpoint/MMIO gates establish this contract, but
do not claim exact physical DMA arbitration, transfer timing, or machine-level
migration.
## 2026-09-02 STM32H723 SPI component VMState decision

This slice remains in the reusable STM32H723 chip/device layer. The SPI data
path producer state needed to continue a guest-visible transfer is the four
CPU-visible configuration mirrors (`CR1`, `CR2`, `CFG1`, `CFG2`), the current
transfer count, the one-entry RX data state, EOT, and the absolute virtual
deadline of a deferred TX-DMA continuation. The deadline is zero when no
SPI-owned continuation is armed; values above the signed QEMU virtual-clock
range are rejected before restore side effects.

Target callbacks and their opaque values, GPIO-derived chip selection, DMA and
DMAMUX channel pointers, endpoint callbacks, QEMUTimer identity, recursive
request protection, endpoint mode, and DMA batch size are runtime wiring or
configuration. They are not serialized by the component. The post-load
boundary invokes `dm_mc02_spi_sync_runtime()` only after validation; that
function clears the recursive guard, cancels the destination timer, and
re-arms the saved deadline against the destination `QEMU_CLOCK_VIRTUAL`.
Board composition must re-project chip selection after restoring its GPIO
inputs; this component does not reach upward into board state.

The isolated gate is `test-dm-spi-vmstate` `4/4`. Existing direct consumer
gates `run-bmi088-smoke.sh` and `run-spi2-dma-smoke.sh on|off` passed after the
change. This is a component contract only: the DM-MC02 machine does not
register it and no whole-machine migration claim follows. DMA/DMAMUX joint
restore ordering, target-device state, GPIO projection ordering, and
SPI electrical/bit-level timing remain separate future boundaries.

## 2026-09-02 STM32H723 USART component VMState decision

This slice remains in the reusable STM32H723 USART data layer. The state
needed to continue a guest-visible serial stream is the CPU-visible register
mirror, the CPU RX FIFO, the chardev wire FIFO, the host-facing TX FIFO and
their loss/short-write counters. Four absolute `QEMU_CLOCK_VIRTUAL`
nanosecond deadlines preserve pending RX delivery, post-frame IDLE, paced TX,
and bounded TX-DMA continuation. The VMState boundary validates ring cursor
invariants and deadline/FIFO combinations before invoking runtime sync.

Chardev, clock, DMA/DMAMUX, IRQ, timer objects, endpoint callbacks, RS485/DE
and transceiver wiring, endpoint mode, DMA-start markers, and derived baud
timing remain destination-owned runtime state. `dm_mc02_uart_sync_runtime()`
recomputes baud timing from restored register state and the destination clock,
then re-arms the saved absolute deadlines, clamping an already expired
deadline to the current virtual time. This avoids reconstructing timing from
wall-clock or silently discarding a pending byte.

The isolated `test-dm-uart-vmstate` gate is `5/5`. The direct UART polling,
IDLE, TX/RX virtual-time and USART1/USART2 DMA endpoint/MMIO gates pass in
both endpoint modes. The description is not registered with the DM-MC02
machine and therefore does not establish whole-machine snapshot or migration
support. Chardev reconnect/partial-write recovery and joint UART/DMA/DMAMUX
restore ordering remain future boundaries.

## 0.39 BMI088 sensor component state boundary

The BMI088 sensor is a reusable device-layer component below the SPI framer
and above the board-independent signal model. Its VMState producer is the
register image plus all state that changes the next sensor-visible result:
signal noise/bias/temperature/filter history, RNG and ODR timing, FIFO bytes
and sample-sequence metadata, read-frame cursor state, overrun/sensor-time
state, and the gyro DRDY clear deadline. The state uses version 1 and encodes
`double` values as fixed big-endian IEEE-754 bit patterns.

The accelerometer/gyro die identity (`accel`) and signal identity
(`signal.kind`) are destination-owned static configuration. They are excluded
from VMState, and post-load requires the destination values to match before
any runtime consumer can use the restored component. The validator also
rejects malformed numeric signal state and invalid FIFO/frame cursors. This
keeps die selection in board composition and the sensor implementation
reusable across boards.

The isolated gate is `test-dm-bmi088-vmstate` `5/5`; direct BMI088 polled,
FIFO, drift and filter smoke gates pass, and the `qemu-system-arm` target
rebuilds. The description is deliberately not registered with the DM-MC02
machine: this slice does not claim machine-level snapshot/migration. The next
restore boundary is the joint SPI framer/target state and its ordering with
GPIO chip-select and DMA/DMAMUX runtime wiring; physical SPI bit timing is
also outside this contract.

## 0.40 BMI088 SPI link composition and restore ordering

The DM-MC02 runtime composition uses the reusable `DmMc02Bmi088SpiLink` for
SPI2 and its two BMI088 targets. This object is below the board policy layer:
it wires a generic SPI data path to generic BMI088/framer components, while
the board still owns GPIO pin decoding, DMA routing and the external
consumer callbacks. The machine does not duplicate the die/framer
initialization or target table.

The composite state boundary is intentionally separate from machine migration.
`dm_mc02_spi_vmstate_raw()` serializes SPI data and continuation fields without
running `post_load`; `dm_mc02_bmi088_spi_link_vmstate()` serializes that raw
state, both sensor states, both framers and a pre-save copy of the
GPIO-derived CS mask. On restore the order is:

```text
raw SPI fields -> BMI088 die validators/state -> framer validators/state
-> fixed accel/gyro slot and pointer checks -> no-callback CS projection
-> SPI runtime/timer synchronization
```

The last step is the only step that activates a saved SPI DMA continuation.
Normal live CS transitions continue to use `dm_mc02_spi_select_mask()` through
the link wrapper, because a live transition must terminate the old target
transaction. Restore must not use that path: BMI088 target `select` callbacks
call `dm_mc02_bmi088_end_read()` and reset framer cursors, which would erase a
serialized half-transaction.

The fixed link slot identity is a composite invariant. A standalone BMI088
VMState still treats `accel` and `signal.kind` as destination static config;
the composite additionally requires `accel` in the accelerometer slot,
`gyro` in the gyro slot, matching signal kinds, and framer pointers into the
same embedded dies. This prevents a valid standalone child stream from being
accepted into the wrong board slot.

This link VMState is a reusable component contract and remains unregistered
from the DM-MC02 machine. Therefore no whole-machine snapshot/migration claim
is made. GPIO input state, DMA/DMAMUX state, CPU/IRQ state, RAM, co-sim queues
and their cross-component restore ordering must pass their own lower-layer
and direct-consumer gates before a machine-level migration description is
considered.

### DMA/DMAMUX composition boundary (2026-09-02)

The reusable `DmMc02DmaSubsystem` is the chip-layer boundary for the two H723
DMA controllers and two DMAMUX windows. It preserves the actual current
topology—DMA1 and DMA2 consume DMAMUX1 channels with 0/8 offsets—without
embedding that board route in the DMA algorithm or claiming a one-to-one mux
ownership model. DMAMUX2 remains an independently serialized window.

The composite loads DMAMUX and raw DMA producer fields before rebuilding DMA
request caches and IRQ projections. Runtime `MemoryRegion`, channel offsets,
callbacks, IRQ handles, endpoint objects and derived caches remain destination
wiring. The ordinary component VMState retains its standalone post-load; the
raw description is only for an enclosing composition. This boundary is not
machine-registered until CPU/IRQ, endpoint, peripheral scheduler, RAM and
co-simulation state contracts define their own restore order.

### DMA endpoint non-fatal backpressure boundary (2026-09-02)

The board-independent DMA endpoint adapter has one explicit non-fatal result:
`DM_MC02_DMA_ENDPOINT_RETRY`. It is caller-driven backpressure, not a hidden
worker queue. A retry is observable as no committed DMA beat: `NDTR`, live
cursor, status flags, stream enable and FIFO contents remain unchanged. The
bounded batch API terminates at the first retry and leaves resubmission to the
producer. Existing bool endpoint callbacks are source-compatible and map
`false` to the fatal `ERROR` path. No asynchronous callback is serialized or
implicitly scheduled here; a future external transport must define its own
virtual-time retry owner and lifecycle contract before integration.

### USB control-core state boundary (2026-09-02)

The reusable control-transfer core remains below board composition. Its
VMState contract serializes only the state needed to continue a decoded
control transfer; callbacks, opaque pointers, packet-size configuration and
all DWC2/QEMU object ownership remain destination wiring. The boundary rejects
states that could cause a restored transfer to emit an impossible payload or
commit a pending address/configuration for an unrelated setup request. In
particular, a ZLP is a DATA-IN-only state and must describe a short transfer
ending on a packet boundary.

The contract deliberately preserves the core's existing IDLE history and
STALLED partial-OUT diagnostics rather than inventing a cleanup transition at
restore time. It is still component-only. DWC2, USB bus, CPU/IRQ/NVIC, RAM,
DMA/PHY/SOF and external transaction state require separate lower-layer
contracts and an explicit composite restore order before machine migration can
be considered.

## 0.44 QEMU USB host transport binding boundary

`DmUsbHostQemuTransport` is a reusable, synchronous adapter from the generic
host transaction interface to QEMU's `USBDevice`, `USBPort` and `USBPacket`
objects. It does not own any QEMU object. A binding is exactly one borrowed
direct device or one borrowed root port; direct submission and address routing
cannot be mixed, and `dm_usb_host_qemu_transport_clear()` must run before the
owner releases the device, port or bus. The host-channel composition now breaks
its route callback and clears this borrowed binding before port/bus teardown.
Direct submission requires the borrowed device to be attached and in QEMU's
`USB_STATE_DEFAULT`; an unattached or pre-reset object is reported as no device.

The adapter creates a stack `USBPacket` and caller-owned I/O vector for one
synchronous attempt. QEMU `NAK` is returned as `NAK`; `NODEV`, `BABBLE`, and
`IOERROR` retain distinct transport results. A QEMU async/queued result is
cancelled before the stack packet and caller buffer leave scope and is reported
as `DEFERRED`. This is intentionally not an asynchronous transport contract;
such a contract would need a separately owned request object and completion
boundary.

The isolated direct-consumer gate is
`test-dm-usb-qemu-adapter` (`15/15`), including null input, exclusive binding,
explicit clear, no-device routing and deferred cancellation. This boundary
does not add USB topology, PHY/VBUS, isochronous/streams, or DM-MC02 host-role
wiring. QEMU's `USBDeviceClass.handle_control` callback is consumed as the
explicit request-level boundary below; it is not treated as a per-status-packet
callback. The adapter's control callback returns a complete IN payload or
consumes a complete OUT data stage synchronously. `dm_usb_control_execute_request()`
drives the existing board-independent control core for consumers that use that
model, while QEMU's own generic control phase remains QEMU-owned. The isolated
control-core gate is `test-dm-usb-control` (`7/7`) and the direct adapter gate
remains `15/15`. No asynchronous control completion, USB PHY/VBUS, SOF, or
machine-level USB migration claim follows from this boundary.

## 0.50 DM-MC02 board GPIO/power reset composition

Board reset ordering is owned by a small composition boundary rather than by
any generic GPIO, SYSCFG, EXTI or power implementation. The reusable API is
`dm_mc02_board_reset_gpio_power()` with a borrowed
`DmMc02BoardGpioPowerReset` descriptor. It accepts an array of GPIO banks,
component pointers and three board-owned hooks; the helper has no QOM, bus,
chardev, clock or timer ownership.

The producer is the board reset request plus the persistent external input
conditions. The boundary performs this exact sequence:

```text
GPIO reset -> BMI088 CS inactive projection -> SYSCFG/EXTI reset
-> external input re-injection -> power model reset
-> FDCAN/RS485/MCU downstream power projection
```

Each component reset is delegated to its existing public API. The CS and input
hooks are composition-owned because they depend on board routes; the power
hook only applies the already-reset model to consumers and must not invoke a
second power setter. This keeps board policy out of reusable peripheral code
and prevents a duplicate ADC source update during reset. The machine continues
to reset UART, FDCAN, ADC, DMA, SPI/BMI088, Flash, USB, timers and co-simulation
state in its own surrounding sequence.

The focused unit gate is `test-dm-board-reset` (`1/1`), asserting the exact
component/hook event order. The direct consumer gate is
`dm-mc02-reset-test` (`2/2`), covering warm reset retention/clearing and cold
volatile-memory clearing. This slice remains below machine-level migration:
it does not serialize state or claim complete reset or snapshot/migration
support for CPU/IRQ, RAM, buses, chardevs, clocks, DMA or co-simulation.

### STM32H723 ADC12 composite VMState boundary (2026-09-02)

The ADC pair is the next chip-layer restore boundary below board composition.
Its canonical state owner is `DmMc02AdcPair`, which embeds one
`DmMc02AdcCommon` producer and the two ADC consumers. The stream order is
explicitly:

```text
ADC12_COMMON raw fields -> ADC1 raw fields -> ADC2 raw fields
  -> common clock projection -> ADC1 scheduler/IRQ projection
  -> ADC2 scheduler/IRQ projection
```

The raw child descriptions are validation-only for this parent. This prevents
an ADC child from arming a timer against a stale common clock and prevents
common post-load from calling a board callback before both ADC states are
available. The pair owns only ordering and cross-child invariants; it does not
duplicate ADC or common register behavior.

The parent requires common pending sample IDs to agree with the matching ADC
active regular sequence IDs. Runtime callbacks, timer objects, DMA/DMAMUX,
IRQ handles, QOM ownership and MemoryRegions remain destination wiring. The
component is intentionally not registered with the DM-MC02 machine, so this
boundary does not establish snapshot/migration support for the board.

During this slice the ADC validator's invalidity expression was corrected:
valid state now requires every calibration, input-override, injected-context
and timing predicate to pass. This was a lower-layer producer/validator bug,
not a pair-level fallback. The regression gates are ADC VMState `3/3`, common
VMState `15/15`, pair VMState `3/3`, direct ADC qtest `62/62`, full smoke
`89/89`, and Host CTest `54/54`.

Remaining unsupported restore edges include CPU/IRQ/NVIC, RAM, DMA/DMAMUX,
trigger bus, board power and co-simulation queues. These must obtain their own
isolated and direct-consumer contracts before a machine-level composite is
considered.

## 0.52 STM32H723 FDCAN and shared Message RAM boundary

The next chip-layer boundary keeps the existing QEMU RAM owner as the sole
producer/owner of FDCAN Message RAM bytes and adds a narrow composition around
one reusable `DmMc02Fdcan` instance. `DmMc02FdcanMsgRamLink` borrows a
`DmMessageRam`; it does not allocate a second buffer, copy bytes into VMState,
or make FDCAN parse QEMU MemoryRegion internals. The current DM-MC02 machine
already wires FDCAN1/2/3 to the same SoC-owned RAM bytes; this slice makes that
identity explicit for a future composite restore without registering one yet.

The producer/boundary/consumer contract is:

```text
DmMessageRam RAM block + FDCAN register/raw state
  -> DmMc02FdcanMsgRamLink
  -> future SoC/board restore composition
```

The serialized stream begins with the RAM-size marker and then uses the
side-effect-free FDCAN raw description. On the destination, the link must be
bound to the existing RAM owner before load. Its post-load first validates
marker, pointer identity, size and all configured filter/FIFO/Rx-buffer/Tx
spans, then invokes the existing FDCAN runtime synchronization once. A failed
load therefore cannot reproject IRQ/timer state. RAM bytes are deliberately
outside this link's stream because QEMU's RAM block owns their persistence and
migration; a future machine contract must order that RAM block before any
FDCAN consumer that can interpret it.

The FDCAN model now centralizes Message RAM span checking and decodes RXESC's
separate FIFO0/FIFO1 data-size fields. This is a chip-layer producer fix, not
a board-specific fallback. The validator limits the H723 model to 128 standard
and 64 extended filter elements and 64 dedicated Rx-buffer indices, uses
checked virtual byte ranges, and leaves section overlap policy outside this
slice. Physical CAN arbitration, exact M_CAN error timing, and bus/chardev
runtime ownership remain outside the VMState boundary.

The focused gate is `test-dm-fdcan-msg-ram-link` (`4/4`), with existing FDCAN
VMState, Message RAM owner, FDCAN qtest and CAN smoke gates retained. The link
is not registered with the DM-MC02 machine; CPU/IRQ/NVIC, DMA/DMAMUX, RAM
migration ordering, CAN bus queues and machine-level snapshot/migration still
require separate lower-layer contracts.

## 0.53 STM32H723 GPIO/SYSCFG/EXTI composite VMState

The SoC-level external-interrupt path is composed as one reusable
`DmMc02GpioExti` object containing the fixed H723 GPIO bank slots, SYSCFG and
EXTI. The producer chain is:

```text
GPIO register/input state -> SYSCFG EXTICR route -> EXTI pending/line state
  -> destination NVIC IRQ wiring
```

The composite stream is `GPIOA..GPIOH raw -> SYSCFG raw -> EXTI raw`. The raw
child descriptions contain fields only and do not run their standalone
post-load callbacks. This matters because standalone GPIO/SYSCFG callbacks
would otherwise project board outputs or re-inject external inputs before the
full routing state was present. After all fields are loaded, the parent checks
the destination profile geometry and bank identities, calls each GPIO ODR
consumer, invokes an optional board input-sync callback, and only then
reprojects all EXTI IRQ levels.

The board callback owns external electrical input state. The generic
composition therefore does not infer pull-up, alternate-function or pin
electrical behavior from restored register bytes, and it does not serialize
the board's `external_gpio` array. MemoryRegions, IRQ handles, callbacks,
profile geometry and board input sources remain destination-owned runtime
wiring. The parent is component-only and is not registered with the DM-MC02
machine.

The isolated gate is `test-dm-gpio-exti-link-vmstate` (`3/3`); the existing
`dm-mc02-reset-test` and `dm-mc02-cpu-test` remain the direct board IRQ/reset
consumer gates. This slice does not establish complete GPIO electrical
semantics, full EXTI trigger/security behavior, or machine-level
snapshot/migration.

## 0.54 EXTI to native CPU/NVIC wiring boundary

The GPIO/SYSCFG/EXTI composite now closes its direct interrupt-controller
edge through `dm_mc02_exti_connect_nvic()`. Its input is a board-supplied
array of borrowed `qemu_irq` targets and its output is the existing EXTI level
projection. The function checks every group and controller input number before
mutating any binding; it uses pairwise duplicate detection so the controller's
external-input geometry can be larger than the seven EXTI groups.

This boundary intentionally reuses the native QEMU `ARMv7MState` and NVIC.
No vector, active, pending, priority, or CPU state is mirrored locally. The
destination graph and route wiring must exist before projection, and the
projection order is:

```text
GPIO/SYSCFG/EXTI raw -> destination NVIC IRQ wiring -> EXTI level projection
```

The board profile remains the owner of the vector map. The isolated route test
and the direct DM-MC02 qtest verify both high-numbered input wiring and the
fact that EXTI pending clear is distinct from NVIC pending clear. This is not
a machine-level migration boundary; native CPU/NVIC child state and all other
machine owners still need their own restore gates.

## 0.55 DMA/DMAMUX composition ownership and machine wiring

The DM-MC02 machine now embeds one `DmMc02DmaSubsystem` and obtains all DMA
and DMAMUX pointers from that composition. This closes an ownership problem
in which the machine had four parallel fields even though the chip-layer
composite already described the same producer state. Mapping, reset, stream
callbacks and peripheral endpoint setup all use the embedded children.

The composition is heterogeneous by position: DMA1 and DMA2 have different
DMAMUX channel offsets, and DMAMUX1 and DMAMUX2 are not interchangeable.
Version 2 of `dm_mc02_dma_subsystem_vmstate()` serializes fixed identity
markers before the raw DMAMUX/DMA children. A destination initializes those
markers as part of its static wiring; a mismatch is rejected before cache or
IRQ projection. The descriptor still accepts the pre-marker version-1 stream
for compatibility.

Only producer state is serialized. MemoryRegions, offsets, callbacks, IRQs,
endpoint objects and request caches stay destination-owned runtime wiring or
derived state. This change does not register the composite in machine VMState,
and therefore does not establish whole-machine snapshot/migration or define
DMA/RAM/peripheral scheduler restore ordering. The focused composite test is
`test-dm-dma-subsystem-vmstate` (`6/6`); direct DMA and UART-DMA smoke gates
remain required.

## 0.56 UART TX DMA backpressure and FIFO transaction boundary

The UART is the consumer of the board-independent DMA endpoint result
interface. Its TX callback is intentionally narrow: one request corresponds
to one byte written to the UART TDR side effect. UART does not implement DMA
arbitration, request lookup, cursor movement or status bookkeeping; it only
accepts the byte, reports temporary capacity exhaustion, or reports an
invalid endpoint request.

`DM_MC02_DMA_ENDPOINT_RETRY` is a no-commit result when the host-facing TX
FIFO is full. The DMA stream therefore retains its committed NDTR, live
cursor, status, enable state and any previously committed FIFO bytes. The
DMA FIFO path treats memory reads used to form the current endpoint beat as
speculative: on retry it restores the FIFO head/length from the beginning of
that request. This keeps the FIFO and memory cursor as one transaction and
prevents a later retry from duplicating a prefetched word. `ACCEPTED` is the
only point at which the request's staged beat and cursor update become
visible; `ERROR` keeps the existing fatal TEIF path.

UART owns the retry schedule because it owns both the TX capacity and the
virtual-time TX service. A bounded batch invokes at most its configured item
limit and stops on the first retry. The UART DMA timer is armed only for
bounded continuation or configuration readiness; TX drain, chardev reopen,
stream enable and endpoint-mode changes explicitly kick it. Chardev close
cancels the DMA timer before the backend is released, and reopen resumes
pending TX through normal UART service. This composition contains no hidden
worker, wall-clock wait or unbounded queue.

The QEMU `ringbuf` backend in `dm-mc02-uart-test` is a deterministic test
consumer, not a claim about host transport behavior. The direct test uses the
real DM-MC02 UART1/DMA1 Stream1/DMAMUX1 channel1 route and controlled virtual
time to prove state preservation, drain-driven continuation and byte order.
The composition remains outside machine-level VMState; real UART bit timing,
electrical behavior, asynchronous host completion, and joint UART/DMA/DMAMUX
restore ordering need separate contracts.

## 0.57 IWDG window-mode ownership

The reusable H723 IWDG component now owns the `WINR` reload-window policy. Its
state producer remains the register mirror and absolute `QEMU_CLOCK_VIRTUAL`
deadline; the window decision is made at the IWDG `KR=0xaaaa` reload boundary.
The component derives a hardware-like descending counter from the remaining
deadline rather than adding a per-LSI-tick QEMU event source, preserving the
existing low event overhead while making the first-tick/window boundary
deterministic.

`WINR >= RLR` is the reset/default disabled configuration. For `WINR < RLR`,
reload is accepted only at a counter value no greater than `WINR`; an early
reload is a watchdog failure and requests guest reset. The IWDG owns the
failure counter and timer cancellation, while the DM-MC02 machine exposes only
the diagnostic string. Board policy such as boot grace remains machine-owned;
zero grace is treated as no grace, so it cannot accidentally bypass hardware
window semantics.

The focused unit VMState test and direct qtest use controlled virtual time and
keep the boundary below the board/tool layer. LSI drift, independent power
domain behavior, exact per-tick counter visibility, and machine-level VMState
remain outside this slice.

## 0.58 IWDG LSI configuration and timing ownership

IWDG oscillator timing is kept below the DM-MC02 board layer. The reusable
`dm_mc02_iwdg_timing` component owns only the protocol-neutral conversion from
nominal LSI frequency, fixed signed ppm error, PR and RLR to virtual
nanoseconds and counter values. It has no dependency on the board profile,
guest firmware, wall-clock pacing, or external simulation. `DmMc02Iwdg` owns
the register/key state and absolute virtual deadline; the machine only supplies
validated startup configuration and exposes diagnostics.

The conversion is exact with respect to the configured rational ppm model:
the scale is `1,000,000 + error_ppm`, all products use checked 128-bit
intermediates, and timeout/counter conversion rounds upward where a partial
LSI interval must remain observable. The fixed error is deterministic and
reproducible, not a claim that silicon temperature drift or oscillator startup
settling has been emulated. A `QEMU_CLOCK_VIRTUAL` event remains the only
watchdog scheduler; no per-LSI-tick event is permitted.

The machine properties are `iwdg-lsi-hz` (default `32000`) and
`iwdg-lsi-error-ppm` (default `0`, accepted range `-999999..1000000`). They are
validated before component configuration and are immutable while the IWDG is
started. Component setters return failure for a live change and never re-arm
or rewrite an existing absolute deadline. The nominal/fixed-error fields are
configuration, not guest VMState; a future composite migration contract must
require matching destination configuration before restoring the deadline.

The lower-layer gate is `test-dm-iwdg-timing`; the direct machine gate is
`dm-mc02-iwdg-test`. This slice does not change the machine-level migration
status and does not establish physical LSI accuracy, independent-power-domain
behavior, or per-tick MMIO visibility.

The timing API reserves `deadline_ns == 0` for the invalid/not-started state;
it is not an elapsed deadline even when the virtual clock is also zero. The
component LSI setter rejects zero nominal frequency, matching the machine
property validator and the timing helper's accepted domain. Neither boundary
silently normalizes invalid input into a runnable watchdog configuration.

## 0.59 IWDG configuration-update status ownership

The reusable IWDG component owns the `SR.PVU/RVU/WVU` configuration handshake,
not the DM-MC02 board or firmware. An unlocked `PR`, `RLR` or `WINR` write
first becomes a per-register pending value and exposes the matching read-only
SR flag; the existing committed register remains guest-visible and a second
write to that same pending register is inhibited. The component schedules the
published ST HAL upper bound of five effective LSI periods as an absolute
virtual deadline with the existing rational LSI helper. It uses at most one
QEMU timer for the outstanding configuration deadlines, not a periodic LSI
timer.

When a pending PR/RLR deadline completes, only the subsequent legal reload may
use that newly committed configuration; an existing watchdog deadline is never
retimed implicitly. A committed WINR update reloads the counter, which is the
behavior expected by the ST HAL initialization path. VMState version 2 contains
the SR flags, pending values and per-register deadlines; version 1 load
normalizes the absent deferred state. This remains a component contract, not a
machine migration claim. The intentionally fixed upper-bound delay does not
model synchronizer phase, LSI settling/temperature variation, independent power
or complete silicon reset semantics.

## 0.60 IWDG reset-reason projection ownership

Reset-cause ownership follows the existing layered boundary. The reusable IWDG
component is the producer of a board-independent reset-request event; it does
not include PWR/RCC headers, inspect RCC state, or encode a DM-MC02 reset
policy. The DM-MC02 composition binds that event to the PWR/RCC consumer, which
sets the STM32H723 `RCC_RSR.IWDG1RSTF` bit (offset `0xd0`, bit 26) before the
global guest reset request is processed.

`RCC_RSR.RMVF` (bit 16) is implemented as write-one-to-clear for the currently
modelled source. `IWDG1RSTF` is read-only, and the PWR/RCC reset routine carries
the source flags through an ordinary system reset. This ordering is required
because the IWDG reset callback runs before the board reset callback; clearing
the whole RCC mirror during board reset would lose the first observable reset
reason.

The PWR/RCC unit and IWDG direct qtest are the lower and immediate-consumer
gates. This does not establish other reset sources, power-domain distinctions,
complete H723 RCC semantics, or machine-level migration. Any future reset
source must add its own producer, projection and reset-preservation tests.

## 0.61 SYSRESETREQ software-reset reason ownership

Software reset cause follows the native CPU boundary. QEMU's realized
ARMv7-M/NVIC child is the sole producer of the AIRCR `SYSRESETREQ` event; the
DM-MC02 machine does not inspect or mirror AIRCR state. The board connects the
named `SYSRESETREQ` output to a runtime `qemu_irq` handler owned by the PWR/RCC
consumer. On the asserted edge, PWR/RCC latches H723 `RCC_RSR.SFTRSTF` (bit 24)
before QEMU consumes the pending guest reset. The deasserted edge is ignored.

The source flag is read-only. `RMVF` clears all currently modelled source flags,
while an ordinary host/QMP reset preserves them and does not invent a software
reset source. The IRQ object is destination wiring and is deliberately outside
VMState; this slice therefore does not expand machine migration support.

The focused PWR/RCC unit covers asserted/deasserted callback behavior and
preservation/clearing. The direct `dm-mc02-cpu-test` drives the guest-visible
AIRCR write, lets the real runstate reset complete, checks `SFTRSTF` after the
reset and after a subsequent ordinary reset, and clears it through `RMVF`.
CPURSTF, domain/pin/BOR/POR/low-power/WWDG causes and full reset-domain timing
remain separate future boundaries.

## 0.62 Power-on reset reason ownership

Power-on cause is represented only by an explicit initialization event. After
the DM-MC02 PWR/RCC object is initialized, the machine calls
`dm_mc02_pwr_rcc_note_power_on_reset()`, which latches H723
`RCC_RSR.PORRSTF` (bit 23). This call is not part of the ordinary reset
callback, so a QMP/guest system reset cannot be mislabeled as POR and the
existing source flags remain observable until `RMVF` is written.

The producer is the machine's initial power-on boundary; PWR/RCC owns the
guest-visible latch; the CPU/guest is the direct consumer. The focused unit and
CPU qtest cover the complete boundary. The local Renode H7 RCC model is used as
a cross-check for the H7 bit position, not as a substitute for vendor
documentation or a physical-board trace. BOR, PIN, CPU/domain, low-power and
other watchdog causes remain unsupported and need independent producers and
tests.

### STM32H723 brownout reset-cause boundary (2026-09-05)

The board power producer has one deliberately narrow event boundary. After a
runtime consumer is wired, `DmMc02Power` emits a brownout callback only for a
`NORMAL -> UNDERVOLTAGE` transition in the current discrete VIN model. Machine
construction may start with low VIN without creating a BOR event; `VIN=0` is
the existing OFF/power-halt path and is not relabeled as BOR. The current
threshold is `12000 mV`, selected as a board-model contract rather than a
silicon-voltage claim.

The DM-MC02 composition callback latches `RCC_RSR.BORRSTF` (bit 21) through the
PWR/RCC public source hook and submits a normal QEMU reset request. QEMU owns
reset fan-out; PWR/RCC owns only the persistent read-only cause latch and
`RMVF` clearing. The reusable power component does not include PWR/RCC types,
and its callback wiring is destination runtime state rather than VMState.

This boundary is validated in isolation and through the real machine qtest.
It does not model analog brownout slope/hysteresis, converter behavior,
startup power sequencing, other H723 reset domains/sources, or whole-machine
migration.

## 0.64 External NRST reset-cause ownership

External pin reset has an explicit producer and is not inferred from a
generic QEMU reset request. The reusable dm-mc02-reset-input QOM child owns
the active-low NRST input state and emits one callback only for a
high-to-low edge. A held-low level is stable state, not a periodic event.
The machine exposes this runtime wiring at /machine/reset-input and does
not encode it as a second board-private reset state.

The DM-MC02 composition is the boundary adapter: it connects the callback to
dm_mc02_pwr_rcc_note_pin_reset(), which latches H723 RCC_RSR.PINRSTF
(offset 0xd0, bit 22), and then submits QEMU's normal reset request. PWR/RCC
owns only the guest-visible source latch and RMVF semantics. The input
component remains board-independent and has no dependency on PWR/RCC
internals.

Reset fan-out does not rewrite the external input. Consequently a reset
while NRST is low preserves the low level and cannot retrigger until the
producer supplies a release followed by a new assertion. The lower gate is
test-dm-reset-input; the direct gate is dm-mc02-cpu-test. The connection is
runtime wiring and is outside machine-level VMState. CPURSTF, domain,
low-power, WWDG, debounce/filtering and complete reset-domain timing remain
separate unsupported boundaries.

## H723 WWDG1 composition

The WWDG implementation is kept at the reusable STM32H723 peripheral boundary.
`DmMc02Wwdg` owns the register semantics and virtual-time event state; the
DM-MC02 machine owns only profile address mapping, APB1 timer-clock selection,
NVIC IRQ wiring, diagnostics, and reset-cause projection. The reusable
component does not inspect PWR/RCC state or board pins.

The event model follows the local Renode `STM32H7_SystemWindowWatchdog`
implementation: `CNT` descends to `0x40` for EWI and the following watchdog
tick requests reset at `0x3f`. The QEMU model keeps the hot path bounded by
scheduling only this next transition. Tick conversion is integer, uses
128-bit intermediates and ceiling rounding, and is tested independently.

`WDGA` is set-only once active. Low-counter writes and reloads outside the
configured window are producer failures, not board workarounds. A live WDGTB
change snapshots the visible counter before committing the new divider so the
old phase is not reinterpreted. The callback to PWR/RCC is a narrow
board-independent reset-request event; PWR/RCC owns the `RCC_RSR.WWDG1RSTF`
latch and QEMU owns reset fan-out.

The direct qtest is the integration gate for the runtime behavior. The
component-only `dm_mc02_wwdg_vmstate()`/`dm_mc02_wwdg_vmstate_raw()` contract
now separately covers the register image, visible counter phase, absolute
deadline, EWI/reset stage and diagnostics. Normal restore validates all
producer fields before rebuilding destination timer/IRQ projection; raw
restore has no runtime side effects for use by a future parent composite.
This component contract still does not register DM-MC02 machine VMState or
claim complete reset-domain migration, and it does not add a silicon-level
clock/electrical claim.

## 0.67 H723 DMA P2M FIFO overflow transaction boundary

The DMA FIFO request path owns the commit boundary for one P2M peripheral
beat. It checks `fifo_length + PSIZE` against the fixed 16-byte FIFO before
performing either a peripheral MMIO read or an endpoint callback. This keeps
the producer side effect behind the DMA capacity check and prevents a failed
transfer from consuming an RX register or external queue item.

Insufficient capacity is a modeled FIFO error: `FEIF` is latched, FIFO state
is discarded, and stream `EN` is cleared. `NDTR`, `PAR`, the active M0/M1
cursor and guest memory remain at their pre-request values. `FEIE` only
projects the already-latched status into the level-sensitive IRQ; it is not a
status latch enable. Existing M2P RETRY rollback remains a separate endpoint
backpressure boundary.

The isolated gate is `dm_mc02_dma_fifo_dbm_endpoint_smoke`, with FEIE enabled
and disabled cases. The existing ARM guest `run-dma-fcr-smoke.sh` remains a
direct FCR/DME/FIFO regression. It intentionally does not inject private FIFO
occupancy, so this slice does not claim a machine-level FEIF trigger test,
silicon FIFO-error coverage, physical bus arbitration, burst timing or full
error recovery.

## 0.68 H723 DMA direct P2M source reservation

The reusable DMA endpoint layer now has an optional one-beat transactional
source boundary. A source that provides `read_prepare`, `read_commit`, and
`read_abort` is borrowed by the direct P2M DMA path as follows:

```text
source prepare (no consume) -> DMA destination write
                              ├─ MEMTX_OK       -> source commit
                              └─ other result   -> source abort
```

This keeps producer consumption aligned with DMA's committed stream state on
the observed invalid-destination failure. The complete tuple is required and
partial tuples are rejected. Legacy `read/read_ex` sources remain an explicit
compatibility path with immediate consumption, so their later memory failure
cannot be rolled back. OCTOSPI implements the tuple in both the standalone
fixture and production wrapper; its reservation fields are synchronous
runtime state and are not serialized in the OCTOSPI component VMState.

The boundary uses QEMU's existing `dma_memory_write()` result and does not
invent a second memory transaction layer. It therefore does not promise to
undo partial side effects from a destination that returns non-`MEMTX_OK`.
At the time of this 0.68 boundary, FIFO P2M needed a separate bounded
multi-beat reservation design, and UART, SPI, ADC, and ADC-common legacy
endpoint consumers remained outside that slice (historical status). UART RX,
SPI RX and ADC1 were added by the subsequent consumer slices below. The isolated gates are the endpoint
contract smoke and DMA direct failure smoke; the direct consumer gate is the
OCTOSPI-DMA integration smoke. This is not machine-level migration support.

## 0.69 UART RX reservation consumer

The UART data path is the next direct consumer of the reusable DMA source
reservation boundary. The producer is the UART CPU-visible RX FIFO; the
boundary is `read_prepare` -> direct DMA destination write -> `read_commit` or
`read_abort`; the consumer is UART RDR/RXNE plus the DMA stream state.

UART owns the queue-head reservation identity and does not advance the source
until the DMA destination `dma_memory_write()` returns `MEMTX_OK`. Abort keeps
the same byte pending, while commit advances the exact head and updates the
CPU-visible RDR/RXNE state. The legacy consuming callback remains explicitly
available for FIFO P2M and the `uart-dma-endpoint=off` compatibility path.
This is a synchronous one-beat boundary; reservation state is transient
runtime wiring and is not serialized in component VMState.

The direct gate is `tools/run-uart-rx-reservation-smoke.sh`, which runs a real
guest against UART1/DMA1 Stream0/DMAMUX1 request 41 and verifies invalid-target
TEIF preservation followed by a valid retry. This does not extend reservation
semantics to FIFO multi-beat transfers or imply machine-level migration,
destination atomicity, or physical serial timing.

## 0.70 SPI RX direct P2M reservation

SPI2 is the next direct consumer of the reusable DMA source-reservation boundary.
The SPI peripheral owns one pending `RXDR` byte and its `RXP` projection. Its
`read_prepare` callback copies that byte and records a synchronous reservation but
does not consume it. The DMA destination write is the commit boundary: a
`MEMTX_OK` result calls `read_commit` and clears the pending RXDR result; every
other result calls `read_abort` and leaves the byte/RXP available for retry. The
reservation blocks a synchronous re-entrant CPU RXDR read from stealing the
reserved result.

DM-MC02 owns only the board route from the configured SPI RX DMA controller/stream
enable event to `dm_mc02_spi_dma_rx_stream_enabled()`. The SPI component does not
perform DMA arbitration or own stream addresses/status. This route allows a valid
stream reconfiguration to retry a failed destination without generating a second
SPI transfer. The direct gate uses the real SPI2/DMA1 Stream3/DMAMUX1 request 39
path and a BMI088 gyro response.

The model is intentionally a single-slot, synchronous data path. RX FIFO/multi-beat
reservation, overrun replacement, destination-side atomicity, joint SPI/DMA
migration, and physical SPI bit/electrical timing remain separate boundaries.

## 0.71 ADC1 `ADC_DR` direct P2M reservation

ADC1 is the next direct consumer of the reusable DMA source-reservation boundary.
The producer is the regular ADC result and `ISR.EOC`; the boundary is
`read_prepare` -> direct DMA destination write -> `read_commit` or `read_abort`;
the consumer is the ADC data-read path and DMA stream state. `read_prepare`
borrows one `ADC_DR` beat without clearing `EOC`. Only a successful destination
write commits the ordinary ADC data-read side effects. A failed destination
releases the reservation and leaves the same result and `EOC` pending.

The machine owns only the DMA1 stream-enable notification route. The ADC component
checks that its DMA, DMAMUX and complete reservation tuple are wired and that
`EOC` is still asserted before asking the reusable DMA boundary to retry. DMA
continues to own request arbitration, stream addresses/cursors, `NDTR`, status
flags and IRQ projection. No second conversion is scheduled for a retry.

The direct gate is `tools/run-adc-rx-reservation-smoke.sh`, which uses ADC1
request 9 on DMA1 Stream2/DMAMUX1 and selects one real SQR rank so the
compatibility two-sample fixture mode is not mistaken for an additional retry.
The existing ADC circular smoke runs on both endpoint and legacy paths. This
slice is synchronous and one-beat only; ADC FIFO, ADC12_COMMON `CDR/CDR2`,
destination atomicity, joint ADC/DMA migration and physical ADC/DMA timing
remain separate boundaries.

## 0.72 ADC12 common CDR direct P2M reservation

ADC12 common now owns a one-word CDR producer latch (`cdr_valid`) in addition
to the read-only register value. The lower boundary is deliberately
board/DMA-neutral:

```text
CDR prepare (copy, no EOC acknowledgement) -> direct DMA destination write
                                             ├─ MEMTX_OK -> CDR commit + CDR read acknowledgement
                                             └─ error    -> CDR abort, word/EOCs remain pending
```

The CDR read acknowledgement remains the sole consumer that projects a
supported common read to both ADC regular EOC flags. This avoids interpreting
the DMA request helper's "request processed" result as a successful memory
write: the endpoint commit callback is the only direct-DMA acknowledgement
path. A CPU CDR read during the synchronous reservation returns the register
value but does not steal the reserved producer word.

DM-MC02 retains only a P2M stream-enable retry route. In direct endpoint mode
it submits one pending CDR request through the existing DMA1/DMAMUX request-9
matcher; DMA continues to own stream selection, PAR/M0 cursor, NDTR, TEIF,
TCIF and IRQ. A valid re-enable after an invalid destination retries the same
CDR word and does not start another conversion.

`cdr_valid` is ADC-common component state and VMState v6 serializes it; v1–v5
loads normalize it to false. `cdr_read_reserved` is transient runtime state:
normal and raw component saves reject it while active, and it is cleared before
post-load validation. This is not ADC/common/DMA composite or machine
migration support. The legacy CDR MMIO path is still immediately consuming,
and CDR2, FIFO multi-beat reservation, partial destination rollback and
physical DMA timing remain separate boundaries.

## 0.73 ADC12 common CDR2 direct P2M reservation

CDR2 is a separate regular-interleaved producer and must not inherit the CDR
pair acknowledgement rule. For `DUAL=0x7` or `0x3` with `DAMDF=0x2`,
`DmMc02AdcCommon` owns one pending `RDATA_ALT` result and its producer source.
The board-independent reservation boundary is:

```text
CDR2 prepare (copy result/source, no EOC ack) -> direct DMA destination write
                                                ├─ MEMTX_OK -> CDR2 commit + source EOC ack
                                                └─ error    -> CDR2 abort, result/EOC remain pending
```

The reservation callback records the source selected at prepare time. A direct
DMA commit is the only endpoint acknowledgement path; a failed destination
does not consume the result. DM-MC02 may retry the level-like result only at a
matching DMA1 request-9 stream-enable edge. DMA retains matcher, address,
NDTR, status and IRQ ownership.

`cdr2_read_reserved` and its saved source are synchronous runtime wiring and
are not part of ADC-common VMState. Normal and raw component saves reject an
active reservation. This is a one-beat component boundary only: legacy CDR2
MMIO remains immediately consuming, and FIFO/multi-beat reservation, partial
destination rollback, composite ADC/DMA/machine restore and physical DMA
timing require independent contracts and gates.

The isolated common gate is `test-dm-adc-common` (`17/17`) and the direct
consumer gate is `dm-mc02-adc-test` (`64/64`), including both supported dual
modes and invalid-destination retry. The QEMU target relink, serial smoke
suite (`92/92`) and Host CTest (`54/54`) also pass; these results do not expand
the component boundary into machine-level migration.
# 2026-09-10 ADC common DMA request admission

Unread CDR state and DMA request permission are separate chip-layer concepts.
The public `dm_mc02_adc_common_cdr_dma_request_pending()` includes the existing
regular-simultaneous DAMDF=2 OVR gate and is used for publication and retry;
board wiring must not reimplement that policy. CPU reads still acknowledge
CDR while OVR is set. No new format or machine migration is implied.

CDR/CDR2 FIFO endpoint compatibility borrows explicit immediately consuming
component reads, reusing the common register acknowledgement semantics. FIFO
dispatch uses the legacy read callback while direct dispatch uses the complete
reservation tuple on the same endpoint. This restores valid FIFO transfers
without duplicating DMA stream selection or inventing a multi-beat reservation.
An invalid FIFO destination still leaves the source consumed and EOC cleared;
that behavior is explicitly covered by direct-consumer tests. Transactional
FIFO rollback remains unsupported and requires a separate boundary design.
# QMP tooling ownership

Smoke and diagnostic tools use the pinned QEMU Python QMP client through
`tools/dm_mc02_qmp.py`. The adapter owns only session setup and the small
return-value API; QMP framing, capabilities negotiation, request IDs, event
dispatch, and error handling remain QEMU-owned. Tools must not open a second
socket reader or reimplement JSON line framing.

This ownership also applies to the Release RTF collector; performance sampling
may issue domain commands and read host process metrics, but it does not own a
parallel QMP transport implementation.
