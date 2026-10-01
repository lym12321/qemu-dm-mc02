# DM-MC02 QEMU Project Constraints

## Scope

- Git delivery uses `main` plus the `dm-mc02/v8.2.2` QEMU branch in the same
  GitHub repository. `qemu/upstream` is a pinned submodule; its gitlink and
  `qemu.lock` fork_commit must agree. Never bootstrap an official tag in its
  place or maintain a parallel patch/source tree. Validate a fresh checkout.

- Pending-commit source delivery explicitly selects `create --allow-worktree`.
  Nested HEAD is the base, not the delivered identity: record tracked diff,
  new/deleted paths and per-file hashes. Default creation rejects dirty QEMU
  sources. Exclude ROM worktrees and reject source changes during packaging.

- This file applies to the whole `dm-mc02-qemu` project.
- Treat this document as a living project contract. When a new layer, reusable
  interface, backend, or verification rule is introduced, update this file in
  the same change so the constraints continue to describe the real project.
- Keep the QEMU project independent from `trobot/`. Do not modify files under
  `trobot/` unless the user explicitly requests a firmware-side change for a
  test. Prefer external adapters, fixtures, and probes.
- Preserve the existing v1 wire interface and documented compatibility unless a
  change is explicitly versioned and tested.
- `ARCHITECTURE.md` is the normative QEMU design contract. Before changing a
  layer, an external adapter, a third-party dependency, a performance gate, or
  a support claim, read the applicable section and update it in the same change
  when the decision changes.

## Truth, Reuse, and Production Boundaries

- The user's 2026-09-29 NOR decision supersedes the previous m25p80 reuse
  decision: use one independent `dm-w25q64` production device, QEMU QOM/SSI,
  and the shared NOR storage core. Keep upstream m25p80.c/flash.h unchanged.
  Prove device isolation and real SSI consumer behavior before OCTOSPI
  integration. Legacy host OSPI fixtures are not production-device evidence.
  Document synchronous completion, quad byte-token approximation, and absent
  protection, physical timing and migration; do not duplicate other stacks.
- The fixed NOR adapter must not grow a type factory or duplicate device
  geometry state. Validate expected configuration before realization; test
  wire identity at the SSI boundary instead of adding initialization probes.

- Resolve behavioral conflicts in this order: reproducible physical-board
  traces, vendor reference material/errata/HAL/CMSIS, observable `trobot`
  behavior, upstream emulator behavior, then this project's previous model.
  A physical-board exception must record its board and firmware revision,
  collection method, affected condition, vendor reference, and regression
  oracle; never silently generalize it into an H723 rule.
- Reuse QEMU's standard infrastructure whenever it owns the behavior: QOM,
  MemoryRegion, IRQ, Clock, timer, CharBackend, USB bus, CAN bus, SSI/NOR and
  VMState. Local code may implement only the missing STM32H723 register-facing
  wrapper, DM-MC02 composition, a board-specific device, or a protocol-neutral
  adapter around that infrastructure.
- One production capability has one implementation. A local transaction stack
  being replaced by an upstream component must be removed after its migration
  gates pass, or be named and built as a test-only fixture. Do not retain a
  runtime switch between equivalent local and upstream production paths.
- All third-party sources have one canonical checked-in origin and pinned
  revision. A build or test must not select a sibling source tree implicitly.
  Record local patches and the intended upstream replacement in
  `ARCHITECTURE.md`.
- Source-delivery snapshots use `tools/dm_mc02_source_package.py` and preserve
  the outer project files, the pinned nested QEMU fork and selected pinned
  Meson wrap sources with per-path hashes. For a wrap with `patch_directory`,
  the QEMU-owned Meson overlay must be materialized into that wrap's build
  source in the package. The read-only firmware ELF is
  identified by hash, not bundled. A verified source restore is only the
  QEMU-06 gate; a fresh configure/build/test in that restore is QEMU-07.
- Keep QEMU build ownership explicit: generic ARMv7-M code cannot depend on
  STM32H723, DM-MC02, or co-simulation sources. STM32H723 sources require a
  dedicated feature symbol, DM-MC02 composition requires a separate one, and
  fixtures build only in test targets.
- Python tools that control QEMU must use `tools/dm_mc02_qmp.py`, backed by the
  pinned `qemu/upstream/python` client. Tool scripts own commands and domain
  assertions only; they must not duplicate greeting negotiation, JSON framing,
  request IDs, event routing, or open a second reader on the QMP connection.

## Time, Performance, and Support Claims

- `CAPABILITIES.md` is the canonical current capability/evidence matrix. Every
  row must name exactly one bounded status (`supported`, `approximation`,
  `fixture`, `unsupported`, or `unverified`), its public boundary,
  implementation, current evidence, limits, and next gate. `README.md` may
  summarize and link to it but must not maintain a second rolling feature list.
  The README is the project entry point; `docs/USER_AND_DEVELOPMENT_GUIDE.md`
  owns the detailed usage and development procedures.
- A historical checklist, old test count, component VMState result, short smoke,
  or adapter-compatible external API must not silently promote a matrix status.
  Update the matrix in the same change whenever production behavior, evidence,
  or a stated limitation changes.
- All deterministic behavior uses monotonic virtual nanoseconds. Wall-clock is
  allowed only for explicit pacing, host I/O deadlines, startup measurement and
  performance reporting; it must never prove guest timing or alter a replay.
- The implemented release gate measures Release `trobot` with default DM-MC02
  peripherals and no external worker over a continuous 60 virtual-second
  window. It does not launch NullEngine. Measure unpaced capacity and paced user
  operation separately, including startup time, RTF, CPU, RSS and watchdog
  status. `1.0x` is the target; the standard release script uses `0.999x` only
  as a documented QMP/wall-clock sampling tolerance and must still print the
  exact measured RTF for all three runs. A different firmware, external plant,
  trace mode, or debugger is a different performance profile.
- The firmware RTF collector starts QEMU with `-S`, requires the initial
  `prelaunch` non-running state, captures tick/watchdog state, and clears old
  QMP events before `cont`. A sample belongs to one epoch only: every event
  batch is scanned for `RESET`, the IWDG timeout count must remain unchanged,
  and each uint32 tick delta must be below the half range. Natural wrap is
  allowed; backward or ambiguous progress rejects the sample.
- A firmware RTF sample ends by stopping QEMU and proving `paused` before the
  final tick/watchdog observation. Reset rejection is a tooling validity rule,
  not evidence for another firmware, external plant, or complete reset-domain
  behavior.
- Firmware RTF startup has one finite host-monotonic deadline, defaulting to
  10 seconds, covering QMP socket creation, connect/greeting/capabilities,
  stopped-state queries, `cont` and the virtual-mode ready tick. QMP commands
  and disconnect also retain bounded per-operation timeouts.
- Collector numeric wall-clock inputs must be finite and a virtual window must
  select at least one FreeRTOS tick. QEMU stderr uses a temporary file. Cleanup
  is bounded TERM, KILL/reap, QMP disconnect, file close and directory removal;
  lifecycle tests identify a process with both PID and Linux start time.
- The simulation worker rejects non-finite or non-positive `rate` and
  `connect-timeout` values before opening sockets. It validates a backend
  factory result before sending RESET or stepping: `step`, `set_motor`,
  `reset_motor`, `set_motor_enabled`, `set_motor_dm`, and `motor_feedback` must
  be callable. Optional `reset` and `close` members must also be callable when
  present. Rejected instances are closed when possible, and worker startup
  admission closes any sockets it opened before rejecting the backend.
- A feature is supported only after its isolated lower-layer test, direct
  consumer boundary test, and relevant firmware integration test pass. Snapshot
  or migration support additionally requires complete VMState coverage. Never
  infer USB host, passthrough, physical timing, complete DMA arbitration, or
  migration support from a fixture or a smoke test.

## External Worker Backend Admission

- The worker owns motor-backend contract validation. `BackendRegistry` remains
  protocol-neutral and only owns backend names and factories.
- CLI parsing and direct `run()` calls both reject non-finite or non-positive
  `rate` and `connect-timeout` before any socket connection or backend factory
  call.
- Direct factories and registry factories share the same instance check. The
  required callable method set is `step`, `set_motor`, `reset_motor`,
  `set_motor_enabled`, `set_motor_dm`, and `motor_feedback`. Optional `reset`
  and `close` must be callable when exposed; optional `imu_timestamp_ns` opts
  into external timestamp mapping.
- Admission checks method callability only; they do not introspect signatures
  or prove returned values and plant behavior. A rejected instance is closed if
  its `close` member is callable, and sockets acquired by worker startup are
  closed before the error reaches the caller. Validation must finish before
  v1/v2 RESET is sent or `step()` is invoked.

## USB Host Role Boundary

- The current `dm-mc02` machine retains the real firmware's USB Device role at
  `USB1_OTG_HS`. Never map a host controller, QEMU USB bus, or passthrough
  adapter into that same board window.
- STM32H7 host-controller and QEMU host-device adapters may be developed only
  as reusable chip/QEMU layers and test fixtures until a separate host-role
  board profile has passed its lower-layer gates. A fixture is not a board
  feature or host-support claim.
- QEMU host-device transports must preserve QEMU object ownership and expose
  their routing limits explicitly. Do not turn a single attached-device
  fixture into implicit multi-device enumeration; first define and test a
  board-independent address-routing contract at the host transport boundary.

## USB Device Control Request Boundary

- QEMU `USBDeviceClass.handle_control` is a complete request-level callback;
  it must not be modeled as a per-SETUP/DATA/STATUS packet callback.
- `DmUsbQemuAdapter` consumes control through its synchronous
  `DmUsbQemuControlSubmit` callback. The callback borrows the request and data
  buffer only for the duration of the call; IN returns one complete payload,
  while OUT consumes one complete data stage and reports zero payload length.
- A lower `DmUsbControlDevice` consumer should use
  `dm_usb_control_execute_request()` to drive the existing control state
  machine. Do not add a second request/control state machine or synthesize
  lower SETUP/DATA/STATUS transactions in the QEMU adapter.
- This boundary does not provide asynchronous control completion, USB PHY/SOF
  behavior, or machine-level migration. Such behavior requires a separately
  owned request and a separately tested composite restore contract.
- The reusable `DmUsbDwc2ControlLink` composite serializes the control core
  before DWC2 raw state. Child post-load is intentionally suppressed; the
  parent validates both children, their EP0/MPS and destination-owned control
  pointer, then performs one `dm_usb_dwc2_sync_runtime()` projection. Fixed
  child markers reject swapped or incompatible positional streams.

## Virtual-Time Completion Fixture Boundary

- `DmUsbHostQemuCompletionScheduler` is a test-only transport consumer. It may
  defer an already-decoded host-channel completion with `QEMU_CLOCK_VIRTUAL`,
  but it must not retain a request, transaction, packet buffer, or board
  private state.
- Its fixed 12-entry capacity and one pending entry per host/channel are an
  explicit fixture contract. A larger topology requires a separately defined
  capacity and routing boundary; do not silently turn this fixture into a
  general host scheduler.
- Reset and teardown must cancel timers before their host, transport, or
  channel owner is released. Tests must advance controlled virtual time and
  must not use wall-clock sleeps to prove completion ordering.
- Keep the fixture disconnected from the current DM-MC02 USB Device profile
  until a reference host composition has passed its lower-layer gates.

## Flash Persistence Boundary

- `cosim/dm_nor_flash_persistence.[ch]` is a host-side lifecycle adapter for
  caller-owned Flash bytes. It must remain independent of QEMU registers,
  board profiles, NOR command framing, and external plant state.
- Persistence files are exact-size raw images for one configured device
  geometry. A missing file leaves the caller's erased image intact; a size
  mismatch must be rejected before loading bytes. Do not add headers or
  silently truncate/pad images in this boundary.
- The default empty path performs no disk I/O. Configured images may be loaded
  during device/machine initialization and saved during an explicit normal
  shutdown hook; Flash command, reset, DMA, and telemetry hot paths must not
  perform file I/O.
- DM-MC02 internal `flash-file` borrows the SoC-owned Flash RAM through this
  adapter. A missing image keeps the erased bytes; a size or I/O error rejects
  machine initialization before guest execution. Lock this path after machine
  initialization so the shutdown save cannot silently target a different file.
- This adapter is functional lifecycle persistence, not a power-fail-safe
  durability protocol. Do not claim crash recovery, atomic power-loss commit,
  real Flash latency, or ECC behavior without a separate contract and tests.

## FDCAN Shared Message RAM Boundary

- `DmMessageRam` is the sole owner of the QEMU RAM block and Message RAM bytes;
  FDCAN must borrow that owner through `DmMc02FdcanMsgRamLink` and must not
  duplicate RAM bytes in controller VMState or board composition.
- A destination link must be bound to its `DmMessageRam` before restore. The
  fixed order is geometry marker, FDCAN raw state, pointer/size and element
  bounds validation, then timer/IRQ projection. Invalid geometry must be
  rejected before timer, IRQ, CAN or chardev side effects.
- Multiple FDCAN links may point to one shared owner, but that is not a
  machine-migration claim. RAM-block, IRQ, DMA, CAN-queue and machine-level
  restore ordering require separate lower-layer gates.

## GPIO/EXTI to native CPU/NVIC boundary

- `DmMc02Exti` owns only its register/line/pending state. CPU and NVIC state
  remains QEMU's native `ARMv7MState`/`NVICState` child state; never mirror
  vector, active, pending, or priority arrays in DM-MC02 code.
- EXTI output groups are connected through the validated borrowed-IRQ route
  table `dm_mc02_exti_connect_nvic()`. The route table must be fully checked
  before any existing binding changes, and controller input numbers are
  validated against the caller's external-input geometry rather than a local
  fixed-size array.
- Restore/projection order is GPIO/SYSCFG/EXTI raw state, destination IRQ
  wiring, then EXTI level projection. Clearing EXTI pending and clearing NVIC
  pending are independent guest-visible operations and require separate
  assertions in direct-consumer tests.
- This is a wiring and direct-consumer boundary only. It does not add a local
  CPU/NVIC VMState and does not establish machine-level snapshot/migration.

## WWDG component VMState boundary

- `DmMc02Wwdg` owns only its register image, visible counter phase, absolute
  virtual deadline, EWI/reset stage and diagnostics. The destination's
  `clock_hz`, timer, IRQ, reset callback and board/PWR/RCC wiring remain
  runtime/static configuration and are not serialized.
- Normal WWDG VMState must validate reserved bits, counter/stage/deadline
  consistency and a non-zero destination clock before calling the public
  runtime synchronizer. Raw WWDG VMState may validate and load producer state
  but must not project timer or IRQ side effects; malformed or truncated input
  is rejected before any runtime projection.
- This component gate is not machine migration. RAM, CPU/NVIC, PWR/RCC, DMA,
  bus state and machine restore order still require their own lower-layer
  contracts and tests.

## DMA/DMAMUX Composition Ownership

- `DmMc02DmaSubsystem` is the sole chip-layer composition owner for the two
  DMA controllers and two DMAMUX windows used by the DM-MC02 machine. Machine
  wiring must borrow `dma[0..1]` and `dmamux[0..1]` from that object rather
  than retain parallel controller fields.
- Composite positional children are heterogeneous. Their fixed identity
  markers must be initialized before save/load; a marker or geometry mismatch
  must be rejected before DMA request-cache or IRQ projection. The v1 stream
  remains load-compatible; v2 adds wire identity markers.
- Channel offsets, callbacks, IRQ handles, endpoint objects and request
  caches remain destination wiring/derived state. This boundary does not
  register machine VMState or imply complete DMA migration.

## Layered Development

Develop from the bottom up. Do not implement a complete board or tool path by
skipping lower layers.

1. `STM32H723` SoC layer: CPU-visible address space, reset, clocks, IRQs,
   register semantics, DMA request wiring, and generic peripheral data paths.
2. `DM-MC02` board layer: pin map, power rails, external devices, board routes,
   GPIO/alternate-function decoding, and board-specific composition.
3. Reusable device and driver layer: BMI088, flash, UART/RS485, CAN/FDCAN, USB,
   motor-driver mappings, and co-simulation transports. These components must
   expose board-independent interfaces wherever the behavior is not inherently
   board-specific.
   Protocol codecs such as `cosim/dm_mc02_v2_wire.[ch]` and
   `cosim/dm_mc02_v2_payload.[ch]` belong to this reusable boundary: framing and
   fixed payload layout must not depend on QEMU link state or board wiring. Typed
   ADC sections follow the same rule; QEMU consumers receive decoded values and
   retain queue/timing policy locally.
4. External plant/backend layer: motor plants, MuJoCo, Gazebo/ROS2, replay, and
   test fixtures.
5. User tools: command-line runners, diagnostics, performance reports, and
   optional UI integrations.

Dependencies must point downward through narrow interfaces. A reusable SoC
model must not depend on DM-MC02 pins or motor policy. A board profile should
provide data and composition, not duplicate peripheral implementations. A motor
backend should consume a protocol-neutral command/state interface rather than
parse QEMU or CAN details directly.

The expected progression is explicit and incremental: `stm32h723` ->
`dm-mc02` -> reusable device/driver (for example, a motor driver) -> external
plant -> user-facing tooling. Do not try to implement the whole system in one
pass or jump directly from a board symptom to an upper-layer workaround. First
identify which layer owns the behavior, make that layer correct and reusable,
verify its interface, and only then integrate it with the next layer. Upper
layers must not reach into private state or duplicate logic that belongs to a
lower layer.

Treat every arrow in this progression as a review gate. An upper-layer feature
is not considered supported until the lower layer has a focused isolated test
and the boundary has an integration check. A complete end-to-end demo may be
used to validate composition, but it must not replace the staged implementation
or hide missing lower-layer semantics.

## Long-Term Development Loop

For every change, record the owning layer, the producer/boundary/consumer
contract, the smallest useful test, and the next integration gate before coding.
Implement and verify that slice first; only then expose it to the next layer.
If the slice fails, keep the failure local until its root cause is understood,
and document the diagnosis and any remaining limitation before continuing.

Do not let a temporary workaround become a lower-layer contract. Any adapter,
fixture, compatibility path, or performance shortcut must identify the layer it
belongs to and have a regression test for the behavior it intentionally changes.

## Required Slice Checklist

Every implementation slice must state its owning layer and the reusable
interface it changes or introduces. Before integrating with the next layer:

1. Verify the lower-layer behavior in isolation with a focused test.
2. Verify the boundary with the immediate consumer using exact values, bytes,
   timestamps, or register effects as appropriate.
3. Record unsupported behavior and follow-up work before moving upward.

If a failure appears, stop at the first incorrect transition and trace the
producer, boundary, and consumer independently. Do not hide an unresolved
lower-layer error with a board-level constant, firmware special case, or UI
fallback; otherwise the error will propagate into every dependent layer.

## Incremental Delivery

- Make one layer or one interface change at a time.
- Before moving upward, add a focused unit, qtest, bare-metal smoke, or replay
  test for the layer just changed.
- Keep the first slice of each layer deliberately small and reusable; expand
  behavior only after its contract and failure boundaries are understood.
- Keep compatibility paths explicit. Do not silently broaden a model by
  accepting malformed input or by returning a fixed value for an unsupported
  feature.
- Record supported behavior and deliberate limitations in `PLAN.md`,
  `REVIEW.md`, `README.md`, or `INTERFACES.md` as appropriate.

## Debugging and Verification

- When a test fails, first reproduce it in isolation and identify the first
  incorrect state transition, timestamp, register value, or wire frame.
- Check the producer, transport, consumer, and test expectation separately;
  do not patch the first component that reports the symptom.
- Prefer deterministic fixtures, exact byte-level assertions, and virtual-time
  checks over wall-clock sleeps.
- Run the narrowest relevant test after each edit, then run the affected layer's
  regression, and finally the full applicable suite serially when shared QEMU
  sockets or build outputs are involved.
- Treat passing tests as evidence only for the behavior they cover. Keep known
  approximation boundaries visible, especially for incomplete H723, USB,
  timing, FIFO, and physical-layer models.

## Canonical Test Gate

- Linux smoke Unix sockets use private `mktemp -d /tmp/dm-qemu.*.XXXXXX`
  directories, not paths under the source/build root. Keep per-script traps
  and process cleanup ownership. Deep source restores must not exceed
  sockaddr_un path limits before the guest runs; test at the deep root too.

- `tools/dm_mc02_test_gate.py` is the authoritative aggregate entry. New tests
  must belong to exactly one of its Meson, native Host CTest, complete pytest,
  or shell-smoke inventories.
- CTest entries that delegate to pytest or the shell suite must carry the
  `gate-external` label. The authoritative Host collection uses
  `-LE gate-external`; `run-qemu-smoke-suite.sh` remains a compatibility wrapper
  around `--no-build --smoke-only`.
- Build all production QEMU and Host binaries before testing. Smoke scripts
  consume those binaries and must not configure or rebuild them. Guest ELF
  fixture compilation remains local to the smoke that owns the fixture.
- Reports must preserve runner paths/versions, commands, dynamic denominators,
  PASS/FAIL/SKIP/BLOCKED, raw exits and QEMU/Host SHA-256 before and after the
  test phase. Identity drift fails the gate.
- Exit 77 is an optional SKIP only for the named ROS2 and MuJoCo smoke scripts.
  Missing required inputs are BLOCKED; FAIL takes precedence over BLOCKED.

## Error Handling and Performance

- Handle realistic runtime failures at public boundaries: malformed input,
  queue exhaustion, disconnects, invalid configuration, and externally visible
  time/order violations.
- Do not add defensive branches for impossible states without evidence or a
  cheap invariant check. Keep hot paths small and move profile validation and
  static consistency checks to initialization.
- Prefer bounded queues, non-blocking host I/O, direct callbacks, and virtual
  timers. Measure startup latency, throughput, real-time factor, CPU, and RSS
  before and after performance changes.
- Never claim real-time capability from a narrow benchmark alone; include the
  actual firmware path and the relevant board peripherals.

## Subagents

- Split work into small, independent tasks with disjoint write sets.
- Assign simple, mechanical implementation or test updates to Luna; use Terra
  for broader implementation review, integration reasoning, and final test
  review. Use Sol only when the task genuinely needs frontier-level analysis or
  a difficult design decision.
- Tell each subagent its exact scope, files, constraints, and required checks.
- Do not duplicate an active subagent's work in the main thread.
- After a subagent reports completion or becomes unnecessary, review its result
  and close it promptly. Immediately clear its context/session and release its
  concurrency slot as part of finishing that task; do not leave completed or
  idle agents occupying slots across unrelated tasks.
- A subagent's test report is not a substitute for reviewing the changed code
  or running the authoritative project-level checks.
- At the end of each delegated task, confirm the agent is closed before starting
  unrelated work. Reuse a context only for a directly dependent follow-up;
  otherwise start a fresh, narrowly scoped task after the previous context has
  been cleared.

## Change Hygiene

- Use `apply_patch` for manual edits and keep changes focused.
- Do not revert unrelated user changes or clean broad generated directories.
- Before handoff, report changed files, test commands and results, remaining
  limitations, and any unverified assumptions.

## UART DMA Backpressure Boundary

- The UART TX endpoint is a consumer of the reusable DMA endpoint contract. A
  full host-facing TX FIFO must return `DM_MC02_DMA_ENDPOINT_RETRY` without
  writing TDR, dropping a byte, advancing DMA state, or setting TEIF.
- UART-owned virtual timers are the retry owner: DMA retry stops when no
  progress is possible, and UART TX drain/reconnect/reset paths must explicitly
  kick a pending DMA stream. Do not add a polling loop, wall-clock sleep, or
  unbounded queue to hide backpressure.
- The direct gate must cover FIFO-full NDTR/address/status preservation, drain-
  driven continuation, and byte-for-byte wire ordering. The ringbuf chardev
  fixture is test infrastructure only; it does not imply host transport or
  physical UART timing completeness.
- In FIFO M2P endpoint mode, memory bytes prefetched while forming a beat are
  speculative until the endpoint returns `ACCEPTED`; a `RETRY` must roll back
  those new FIFO bytes and leave the committed FIFO/cursor unchanged. Do not
  advance only one side of that pair, or a later retry can duplicate bytes.

## IWDG LSI timing boundary

- The board-independent IWDG timing helper owns integer conversion from a
  nominal LSI frequency plus one deterministic fixed error in ppm to virtual
  deadlines and visible counter values. It must use monotonic virtual time,
  checked wide arithmetic, and ceiling conversion; it must not schedule one
  QEMU event per LSI tick.
- The accepted fixed-error range is `-999999..1000000` ppm and the nominal
  frequency must be non-zero. The DM-MC02 machine exposes these settings as
  `iwdg-lsi-hz` and `iwdg-lsi-error-ppm`, defaulting to 32000 Hz and zero
  error. Invalid values are rejected at the machine configuration boundary.
- LSI configuration is startup-only while the watchdog is running. Changing
  it must not re-arm or extend an existing absolute deadline; a new setting is
  used by the next valid start/reload after the watchdog is stopped or reset.
- This timing helper does not establish silicon LSI temperature drift, startup
  settling, independent-power-domain behavior, or whole-machine migration.

## IWDG reset-reason boundary

- The reusable IWDG core emits only a board-independent reset-request callback;
  it must not include or depend on PWR/RCC private state. DM-MC02 wiring maps
  that event to `RCC_RSR.IWDG1RSTF`.
- `IWDG1RSTF` is read-only reset-source state. `RCC_RSR.RMVF` is write-one-to-
  clear for the currently modelled source, and an ordinary system reset must
  preserve the flag until the guest explicitly clears it.
- This slice supports only the IWDG1-to-`RCC_RSR` projection. It does not
  claim other H723 reset sources, full reset-domain semantics, reset-cause
  migration, or complete RCC bit semantics without separate lower-layer and
  direct-consumer tests.

## H723 software-reset reason boundary

- The software-reset producer is QEMU's native ARMv7-M/NVIC `SYSRESETREQ`
  output. Do not duplicate AIRCR/NVIC state in the board or PWR/RCC model, and
  do not infer a software cause from an arbitrary `qemu_system_reset_request()`
  call.
- DM-MC02 projects only the asserted edge of that pulse to
  `RCC_RSR.SFTRSTF` (bit 24); the deasserted edge is ignored. An ordinary QMP
  or host `system_reset` does not create this source, and preserves already
  modelled flags according to the reset contract.
- The SYSRESETREQ connection is destination runtime `qemu_irq` wiring, not
  PWR/RCC or machine VMState. CPU, domain, pin, BOR/POR, and other watchdog
  sources require their own producer/boundary/consumer contract and tests.

## H723 power-on reset reason boundary

- Initial DM-MC02 machine construction is the sole explicit power-on producer.
  It may call `dm_mc02_pwr_rcc_note_power_on_reset()` once to project
  `RCC_RSR.PORRSTF` (bit 23). Do not infer POR from QMP/system reset,
  `cold-reset`, or an arbitrary `cpu_reset()` call.
- `PORRSTF` is read-only reset-source state. Ordinary reset preserves it and
  `RMVF` clears it together with the other modelled source flags. The hook must
  run before guest execution and must not be called from the ordinary reset
  callback.
- This slice covers only the explicit startup event. BOR, PIN, D1/D2, CPU,
  low-power and other watchdog sources require separate producer, boundary,
  isolated-test and direct-consumer contracts.

## H723 Brownout Reset-Reason Boundary

- `DmMc02Power` emits one runtime brownout event only for the
  `NORMAL -> UNDERVOLTAGE` transition (`1..11,999 mV`). Initial low VIN,
  `VIN=0`/OFF, a held undervoltage state, and recovery are distinct boundaries
  and must not produce `BORRSTF`.
- DM-MC02 owns the composition wiring: it maps that callback to
  `RCC_RSR.BORRSTF` (bit 21) and submits the reset to QEMU's normal reset
  machinery. PWR/RCC owns only the read-only source latch and `RMVF` clearing;
  the reusable power producer must not depend on PWR/RCC types.
- The 12 V threshold is an explicit discrete DM-MC02 model assumption, not a
  claim about the silicon BOR voltage or analog brownout curve. This slice
  does not establish startup power, other reset domains/sources, or complete
  power-reset timing without separate lower-layer and direct-consumer gates.

## H723 External NRST Reset-Reason Boundary

- External NRST is represented by the reusable QOM GPIO input
  dm-mc02-reset-input. It is active-low and emits one event only on a
  high-to-low edge; a held-low line must not enqueue repeated resets. The
  board reset path must preserve the external level and must not release it.
- DM-MC02 maps the input event to RCC_RSR.PINRSTF (bit 22) and then submits
  QEMU's ordinary reset request. PWR/RCC owns the read-only source latch and
  RMVF clearing; an arbitrary QMP/host reset is not a pin-reset producer.
- The public runtime wiring is /machine/reset-input with the named GPIO
  NRST. The QOM GPIO and callback are not machine VMState. This slice does
  not claim CPU/domain/BOR/POR/low-power/WWDG sources or complete reset-domain
  timing without separate lower-layer and direct-consumer gates.

## H723 WWDG1 Reset and Window Boundary

- The reusable WWDG producer owns only `CR/CFR/SR`, virtual-time counter state,
  and the next observable event. Its tick is the deterministic
  `4096 * 2^WDGTB / clock_hz`; schedule EWI and final reset directly, never one
  QEMU event per watchdog tick.
- `CR.WDGA` is set-only once running. A running write with `T < 0x40`, or a
  reload while `CNT > CFR.W`, requests reset. EWI first sets `SR.EWIF` and
  asserts the wired IRQ; the next tick requests reset. `SR.EWIF` is cleared
  only by a write-zero operation.
- A live `CFR.WDGTB` change must snapshot the visible counter using the old
  divider before committing the new divider. DM-MC02 supplies APB1 timer clock
  through the public setter; WWDG must not depend on PWR/RCC private types.
- The reset callback is board-independent and DM-MC02 maps it to
  `RCC_RSR.WWDG1RSTF`. The timing unit, direct qtest and component VMState are
  independent gates; passing the component state contract does not claim
  complete reset domains or silicon-level timing.

## H723 ADC Conversion Deadline Boundary

- ADC1/2 regular and injected rank timing is chip-owned: `CFGR.RES` selects
  16.5, 14.5, 12.5 or 10.5 processing clocks for 16, 14, 12 or 10 bits;
  add the channel's SMPR sampling clocks, then ceil the integer half-cycle
  duration to virtual nanoseconds. The board and DMA consumer must not
  substitute a fixed conversion period.
- EOC/JEOC and direct DMA memory writes must remain pending until that rank
  deadline. Verify the ADC-only producer before the ADC-to-DMA consumer;
  this digital deadline does not establish analog acquisition, SAR physics or
  DMA bus-cycle fidelity.

## H723 DMA FIFO Overflow Transaction Boundary

- A P2M FIFO request must check capacity for the complete `PSIZE` beat before
  reading a peripheral MMIO register or invoking a peripheral endpoint. A
  full/insufficient FIFO is a DMA producer/boundary failure; the peripheral
  must not be consumed before DMA reports `FEIF`.
- The overflow path latches `FEIF`, clears the FIFO and clears stream `EN`,
  while leaving the rejected beat's `NDTR`, `PAR`, active memory cursor and
  guest memory unchanged. `FCR.FEIE` gates only the IRQ projection and never
  the `FEIF` status latch.
- This contract covers only the deterministic capacity boundary of the
  reusable model. Silicon-specific FIFO-error conditions, bus arbitration,
  clock-level recovery and physical timing require separate evidence and
  producer/consumer gates; the host fixture must not be used to claim them.

## DMA direct P2M endpoint reservation boundary

- The optional `DmMc02DmaEndpoint` P2M reservation tuple consists of
  `read_prepare`, `read_commit`, and `read_abort`. `read_prepare` must copy
  one beat without consuming it; the DMA direct path invokes exactly one
  commit after a successful `dma_memory_write()` or abort after an error.
  Partial tuples are rejected rather than silently using a consuming read.
- Existing `read/read_ex` callbacks remain the v1 compatibility path and are
  immediately consuming, so they cannot roll back a later target-memory
  failure. The direct consumer slice covers one beat for OCTOSPI, UART RX, SPI
  RX, and ADC1 `ADC_DR`; FIFO P2M needs a separately designed bounded multi-beat
  reservation layer.
- UART RX reservations identify and copy the current queue head without
  advancing it. Only a successful destination write consumes that head and
  updates RDR/RXNE; an invalid destination preserves the byte for a later DMA
  configuration retry. SPI RX reservations apply the same commit boundary to
  the single pending RXDR result and preserve RXP on a failed destination.
  ADC1 reservations apply the same commit boundary to the single pending
  ADC_DR result and preserve EOC/result on a failed destination; a stream-enable
  event may retry it without a new conversion. These are synchronous data-path
  rules, not UART/SPI/ADC migration guarantees.
- Reservation state is synchronous endpoint runtime state, not VMState. The
  contract does not claim rollback of partial destination-side effects from a
  non-`MEMTX_OK` QEMU memory transaction; that requires a separate lower-layer
  atomic-memory boundary.

## ADC12 common CDR direct P2M reservation boundary

- Publication and retry must use the chip-owned CDR DMA admission predicate,
  including regular-simultaneous DAMDF=2 OVR gating. Keep CPU acknowledgement
  separate so software may read CDR while OVR remains set.
- CDR/CDR2 FIFO endpoint compatibility uses the explicit immediately consuming
  component reads; it cannot restore data/EOC on a destination-write error.
  Direct P2M retains the reservation tuple. No multi-beat rollback or migration
  support follows from the FIFO compatibility path.

- `DmMc02AdcCommon` owns one pending CDR producer word (`cdr_valid`) and the
  board-independent `cdr_read_prepare`, `cdr_read_commit`, and
  `cdr_read_abort` interface. Prepare copies one supported CDR beat without
  acknowledging either producer EOC; only a post-`MEMTX_OK` commit may invoke
  the common CDR read acknowledgement. Abort retains the same word and both
  producer EOC flags for a later direct-DMA retry.
- `cdr_valid` is component producer state and is serialized in ADC-common
  VMState v6. v1–v5 streams normalize it to false. `cdr_read_reserved` is
  synchronous runtime state, is not serialized, and normal/raw component saves
  must reject a non-empty reservation. This does not establish ADC/common/DMA
  composite or machine migration.
- DM-MC02 may retry this level-like source only at a direct-endpoint P2M
  stream-enable edge and only through the existing DMA/DMAMUX matcher; DMA
  retains stream selection, addresses/cursors, NDTR, TEIF/TCIF and IRQ
  ownership. The CDR MMIO compatibility consumer remains immediately
  consuming. CDR2, FIFO multi-beat reservation and partial destination-write
  rollback require distinct producer/boundary/consumer slices.

## ADC12 common CDR2 direct P2M reservation boundary

- `DmMc02AdcCommon` exposes a separate one-result CDR2 reservation tuple for
  regular-interleaved `DUAL=0x7/0x3` with `DAMDF=0x2`. `read_prepare` copies
  `RDATA_ALT` and records its producer source without acknowledging EOC;
  `read_commit` is reached only after a successful direct DMA destination
  write and performs the source-specific CDR2 read acknowledgement. `read_abort`
  leaves the result and its producer EOC pending.
- The CDR2 reservation and its saved source are synchronous runtime state,
  not VMState. Normal and raw ADC-common saves reject an active reservation.
  This source-specific boundary must not reuse CDR's dual-EOC acknowledgement
  assumption; DM-MC02 only retries it at the matching DMA1 request-9 stream
  enable edge.
- The boundary is limited to one synchronous direct-P2M beat. Legacy CDR2
  MMIO remains immediately consuming, and FIFO/multi-beat reservation,
  destination partial rollback, composite ADC/DMA migration and physical
  timing require separate contracts and gates.
