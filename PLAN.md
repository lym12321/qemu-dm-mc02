# QEMU 工程基线：活动任务与长时交接

更新时间：2026-09-10。当前目标是 QEMU STM32H723 / DM-MC02 工程基线；
里程碑总览见 [工作区 PLAN](../PLAN.md)，审查快照见
[PROGRESS_REPORT](../PROGRESS_REPORT.md)，R-01～R-06 指该次审查的局部编号。
本节取代旧活动 backlog 的优先级；下方保留历史记录。

## 范围与推进规则

- Renode 不进入当前写集、构建、测试、报告聚合或验收依赖；不补其架构契约，
  不要求差分对齐。原目录/资料保留；R-06 的 Renode 专属部分记为范围外。
- 不修改 `trobot/`，不清理未确认过时的文件，不自动提交或发布源码。
- 每次一个层或边界切片，最多一个任务“进行中”。状态固定为待开始、进行中、
  阻断、完成、延后。出现首个错误先隔离根因，不能靠上层 workaround 通过。
- 每次接续先读取适用 AGENTS、本节和对应 REVIEW，核对源码/配置变化；每次结束
  写明实际命令、日期、输入身份、退出状态、日志、限制和唯一下一步。
- 完成须有隔离、直接消费者和受影响回归证据；历史通过不能覆盖当前失败。
  测试计数随清单变化，不以固定数量或跨集合求和充当覆盖率。
- 工程基线不改变设备公共 API、固件或 co-sim wire。复用上游和现有 runner；
  实际新增工具接口/验证规则时同步更新对应 AGENTS、ARCHITECTURE、INTERFACES。

## 当前交接

- 初始证据（2026-09-10）：Meson 65 目标中 runtime-sync 链接失败，其余 64 通过；
  Host CTest 54/54（内含 smoke 92/92），pytest 256 通过。原始日志见
  [review logs](reports/2026-09-10-review/)。Release 三轮结果仍是历史证据。
- 最近完成：**QEMU-01**；仅修改 unit Meson source set 和计划/交付记录。
- 当前结果：ADC runtime-sync 3/3、直接 ADC qtest 75/75、完整 Meson 65/65
  （54 unit、11 qtest）、Host CTest 54/54（67.97 s，含 smoke 92/92）、pytest 256 通过。
  没有排除测试；本轮未执行 Renode 或新的三轮性能门。
- 命令、日志和输入 hash 见 [QEMU-01 交付证据](reports/2026-09-10-qemu-01/README.md)。
- 唯一下一步：**QEMU-02（待开始）**，核对现有 runner 清单与 smoke 的退出码/跳过
  路径，再在工具层实现薄聚合入口。当前没有进行中任务；R-02～R-05 和 R-06 的
  QEMU 部分仍未关闭。

## QEMU-01：恢复 ADC 隔离测试

- 状态：完成（2026-09-10）；依赖：无；关联 R-01 已关闭；所属层：STM32H723 隔离测试构建边界。
- Producer：真实 ADC core 调用 endpoint reservation helper；boundary：Meson
  目标的 source set；consumer：runtime-sync 的 IRQ/timer/restore 行为断言。
- 修改 `qemu/upstream/tests/unit/meson.build`：链接已存在的
  `hw/arm/dm_mc02_dma_endpoint.c`，沿用 DMA 测试的依赖方式，不新增 stub/生产实现。
- 隔离门：`tools/meson test -C build/qemu --num-processes 1 --print-errorlogs test-dm-adc-runtime-sync`。
- 直接消费者门：同入口运行 `qtest-arm/dm-mc02-adc-test`。
- 完整项目门：同入口运行 `'test-dm-*' 'qtest-arm/dm-mc02-*' 'qtest-arm/stm32h723-usb-host-test'`，
  不排除目标；按项目要求补 Host/pytest 验证并保存日志。
- 已知限制：只修复测试链接依赖，不证明新增 ADC 行为、联合迁移或全 H723 时序。
- 交付：现有 helper 直接链接，无新增 stub、生产行为或接口；上述全部门通过。
  输入身份和日志已固化到本节交付证据。review 原失败日志保留，不改写历史。

## QEMU-02：统一现有测试门禁

- 状态：待开始；依赖：QEMU-01；关联 R-04；所属层：验证工具。
- Producer：现有 CTest/pytest/Meson 与 smoke 结果；boundary：薄聚合入口；
  consumer：终端报告与持续验收，不自建测试框架。
- 聚合完整集合，记录 runner 身份、分母、PASS/FAIL/SKIP/BLOCKED 和退出码；
  隔离无关 pytest 自动插件。必需集合未执行不得报通过，可选 backend 缺失注明原因。
- 明确“先构建、后测试”的 owner，统一 QEMU Ninja 来源，消除 smoke 套件中途隐式
  重建。QEMU-01 观察到 `run-cosim-link-smoke.sh` 调用 `build-qemu.sh`，其 venv
  Ninja 与 Meson 测试所用系统 Ninja 版本不同，日志出现旧格式警告且 binary hash
  改变；源码和构建选项 hash 未变。构建前后身份及最终二进制验证见 QEMU-01 证据。
- 最小测试：受控 runner 的成功/失败/跳过/阻断；直接门：实际 QEMU smoke 与三集合。
- 验收：故意失败能传到顶层；缺 backend 不计为 PASS；不调用 Renode。
- 验收还须确认测试阶段无隐式重建，报告绑定实际被测二进制身份；身份漂移不得
  静默算作同一构建的通过证据。
- 限制：该门不包含真实 plant 性能或完整上游 QEMU 测试集。

## QEMU-03：性能采样的复位与回绕判定

- 状态：待开始；依赖：QEMU-02；关联 R-02；所属层：工具采样边界。
- Producer：QMP tick/reset/watchdog 观测；boundary：采样有效性；consumer：RTF 结果。
- baseline/virtual 共用 reset 与合法 wrap 判定，复用现有 QMP 客户端/事件能力；
  不允许非 IWDG reset 形成巨大无符号正增量后通过。
- 隔离：非 IWDG reset、合法 32-bit wrap、watchdog 变化、正常递增；直接门：
  实际 QEMU 控制的 reset 和正常采样；随后工具回归和 QEMU-02 门禁。
- 限制：guest tick 只是当前测量 profile 的时间代理，不推导其它固件或 plant 时序。

## QEMU-04：采样启动期限与清理

- 状态：待开始；依赖：QEMU-03；关联 R-03；所属层：工具进程/采样生命周期。
- Producer：进程状态、QMP 响应与 host deadline；boundary：startup-ready admission；
  consumer：采样开始或有界失败退出。
- 独立 startup-ready 总期限，校验 NaN/Inf 和无效参数，处理停滞/断连/提前退出；
  清理须有界且不留下本任务 QEMU 进程。
- 隔离：QMP 存活但 tick 冻结、异常参数、超时与退出；直接门：正常 QEMU 启动、
  停滞/终止后的退出及清理；随后工具回归和统一门禁。
- 限制：host deadline 只约束工具，不改变 guest virtual-time 语义。

## QEMU-05：当前能力与证据矩阵

- 状态：待开始；依赖：QEMU-04；关联 R-06 的 QEMU 部分；所属层：工程文档。
- Producer：源码、测试和观测；boundary：当前支持矩阵；consumer：README、计划和用户声明。
- 每项列出所属层、公共边界、支持/近似/fixture/未支持/未验证、实现入口、测试证据、
  已知限制与下一门；纠正 ADC ranks 等过时描述，历史记录保留并指向当前状态。
- 验收：链接与命令有效，关键条目核对源码/结果；不按历史勾选率推导完成率。
- 限制：缺少实机证据的项保留“未验证”；Renode 专属问题不伪标已解决。

## QEMU-06：源码与构建输入清单

- 状态：待开始；依赖：QEMU-05；关联 R-05；所属层：源码交付工具。
- Producer：唯一 canonical QEMU 源码、上游版本、本地修改和必要新增源码；
  boundary：本地重建包与 manifest；consumer：隔离目录重建。
- 清单覆盖 tracked patch、必要 untracked 模型/测试、子模块/依赖、构建配置、工具版本、
  ELF 输入身份及获取方式；不把未跟踪源码当垃圾，不复制无关缓存或凭空推定缺文件。
- 最小门：清单路径、内容 hash 与包内源码一致；直接门：干净目录恢复源码并核对身份。
- 限制：记录上游 commit 不足以恢复本地模型；不自动提交/发布或修改固件输入。

## QEMU-07：独立目录重建验证

- 状态：待开始；依赖：QEMU-06；关联 R-05；所属层：构建/验证消费边界。
- Producer：重建包与配置；boundary：独立源码和构建目录；consumer：QEMU/Host 构建及统一门禁。
- 不借用旧 build、隐式 sibling source 或 Renode；缺依赖明确 BLOCKED。使用同一
  固件输入身份，重新构建必需测试 fixture 和可执行文件。
- 验收：还原源码身份一致、构建成功、QEMU-02 必需集合通过；保存命令和日志，
  报告可选 backend skip。不能用旧二进制运行结果证明 clean build。
- 限制：不要求未经定义的二进制逐字节一致，也不据此宣称跨平台可重建。

## QEMU-08：可信 Release 性能基线

- 状态：待开始；依赖：QEMU-07；所属层：性能测量与发布验收。
- Producer：固定源码/ELF/配置下的 Release 固件；boundary：已修正的采样器；
  consumer：既有三轮性能门与报告。
- Profile：DM-MC02 默认外设、无外部 worker，使用 `tools/run-release-rtf-gate.sh`；
  三轮各 60 虚拟秒，沿用目标 1.0x、采样容差 0.999x，不放宽既有标准。
- 验收：逐轮精确 RTF、startup、CPU、RSS、reset/watchdog、源码/ELF/配置身份与
  原始日志齐全；明确 RSS 是采样值还是峰值，失败不能被均值掩盖。
- 限制：不代表无节流容量、worker pacing、真实 plant、多电机或硅级时序。

## 完成标准与后续候选

QEMU-01～08 均通过且 QEMU 范围内审查问题有关闭/拆分证据，才完成工程基线。
当前能力矩阵必须与源码和实际测试一致。每个任务结束在本节和 REVIEW 保存日期、
变更文件、命令、输入身份、结果、日志、限制及下一步；不得只写口头状态。

以下候选均“延后”，基线完成后再选一个主目标：目标固件的芯片缺口 → DM-MC02
业务 ready 与器件恢复 → motor driver 到一个真实单关节 plant。整机恢复独立规划，
不默认阻断电机联调；新 UI、多 plant、完整 USB Host 不提前实施。

---

以下保留历史记录；跨后端任务和旧优先级不再驱动当前实施。

# 2026-09-10 QMP consumer cleanup and performance scope correction

- [x] Removed dead buffer arguments, None placeholders and pass-through QMP
  functions in `tools/collect-firmware-rtf.sh`, `run-adc-input-smoke.sh` and
  `run-adc-calibration-data-smoke.sh`. These tooling consumers call the existing
  QmpSession interface directly; no new transport or device layer is added.
- [x] Both ADC smokes and collector baseline/virtual-window modes pass. Full
  Host CTest passes 54/54 (18.07 s), including 92 smoke scripts. Python unittest
  discovery ran 32 tests: 31 passed and 1 skipped. Every shell script was checked
  individually with bash -n. The prior three-run performance result is historical;
  this cleanup was checked with short samples, not another release gate.
- [x] Corrected ARCHITECTURE/AGENTS: the actual collector launches no NullEngine
  or worker. Previous firmware-only measurements do not validate an external plant.
- [ ] Define and verify separate unpaced-capacity and worker-paced profiles at
  the tooling boundary. Machine migration, physical timing and remaining device
  limitations in earlier sections are still open; QMP completion is not overall
  project completion.

# 2026-09-10 Reuse audit and tooling consolidation (complete)

QMP adapter migration and native-source target cleanup are complete. All QMP
tooling consumers now use the pinned QEMU client through the common adapter.

- [x] Review canonical QEMU ownership and existing test-only alternatives.
- [x] Remove the obsolete same-name machine skeleton; CMake's IDE target now
  points to the actual upstream/hw/arm/dm_mc02.c. Target configuration/build pass.
- [x] Establish a narrow adapter over pinned QEMU's synchronous QMP client;
  isolated fragmented-message/event/error test passes. Upstream owns protocol
  negotiation, framing, IDs and event routing; scripts retain test assertions.
- [x] Migrate all tooling consumers and remove copied QMP JSON-line framing.
- [x] Run full Host/smoke/Python checks: CTest 54/54, QEMU smoke 92/92,
  Python 32 passed/1 skipped, shell syntax and Python compilation passed.
- [x] Run the standard 3x60-second real-time performance gate: RTF
  0.999984..1.000000x, startup 0.522994..0.523777 s, CPU 115.70..115.73%,
  RSS 50264..50520 KiB, and IWDG timeouts=0 in all runs.
- Only tooling/source ownership is changing in this slice. Firmware, H723
  register semantics, USB roles and machine migration remain unchanged.

# 2026-09-06 ADC common / DMA review follow-up (fixed 2026-09-10)

- [x] Reviewed CDR/CDR2 producer, direct/FIFO endpoint dispatch, retry wiring,
  component VMState and current tests. Five virtual-time QEMU probes confirmed
  two open findings; full evidence and reproduction are at the top of REVIEW.md.
- [x] R20260906-1 (P1): fixed common CDR DMA retry admission while OVR is set.
  Owning layer: ADC common request policy; producer is retained CDR/ADC status,
  boundary is DMA request admission, consumer is stream-enable retry. First add
  isolated admission coverage, then direct qtests for master/slave/both OVR;
  preserve CPU read acknowledgement while OVR remains set.
- [x] R20260906-2 (P2), separate subsequent slice: resolved CDR/CDR2 FIFO legacy
  callback compatibility. Default endpoint mode previously TEIFed where the MMIO
  path completed. Explicit consuming component APIs and legacy endpoint reads
  now preserve FIFO compatibility; isolated reads and eight FIFO qtests pass.
  Do not infer multi-beat reservation support from this fix.
- [x] First-slice common unit 18/18 and ADC qtest 67/67 passed before FIFO
  integration. Final common unit 19/19, ADC qtest 75/75, rebuilt Host CTest
  54/54 including serial QEMU smoke 92/92 passed. BMI088/SPI/link VMState:
  5/5, 5/5, 4/4. Machine migration and physical timing remain unverified.
- The original review was read-only; the 2026-09-10 fixes change common/board
  source and tests. Also fixed standalone BMI088/SPI header dependencies exposed
  by full Host rebuilding, with host and three VMState test targets passing.
  No trobot changes. Exact scope and evidence are recorded in REVIEW.md.

# 2026-09-02 STM32H723 SoC RAM ownership and migration registration（已完成）

- [x] 所属层：STM32H723 SoC memory composition。producer 是 SoC 的 Flash、ITCM、DTCM、
  AXI SRAM、D2 SRAM 和 D3 SRAM 字节；boundary 是 `dm_mc02_soc_memory_init()` 对 QEMU
  `MemoryRegion` 的 ownership/migration 注册；consumer 是 CPU 地址空间、Flash program
  overlay、DMA/FDCAN 等直接存取者及未来 machine composite restore。
- [x] 动态字节区域改用 QEMU 标准 `memory_region_init_ram()`，并以 `NULL` 注册 global
  RAMBlock：MachineState 不是 DeviceState，不能把 machine object 误传给 QEMU RAM
  migration owner。Flash 内容和五块片上 RAM 因此进入标准 RAM migration 集合；没有新增
  SoC 私有字节 VMState，也没有改变地址映射或 reset policy。
- [x] 固定 UID/工厂校准区继续使用 `memory_region_init_rom_nomigrate()`，由 profile 在
  realize 时确定性重建，避免把静态 ROM 数据重复放进 RAM stream。
- [x] 新增 `test-dm-soc-memory` 隔离测试 `2/2`，精确断言六个动态区域走 migratable
  初始化、校准 ROM 走 non-migrate 初始化且 owner 为 NULL；扩展
  `dm-mc02-memory-test` 为 `2/2`，覆盖 Flash 正常解锁写入、校准 ROM 写保护、warm
  reset 保留普通 RAM/Flash 以及 cold reset 清除五块片上 RAM。
- [x] 验证：相关 unit/qtest 通过，目标 `qemu-system-arm` 重链通过；`trobot/` 未修改。
- [ ] 限制与下一道门：该切片只注册 RAMBlock，不注册 DM-MC02 machine-level VMState，
  不能宣称整机 snapshot/migration。CPU/NVIC、DMA/DMAMUX、外设 timer/IRQ、Flash 子设备、
  CAN/chardev 和 co-sim queue 的联合恢复顺序仍须按层定义；下一步继续选择一个相邻
  SoC 直接状态边界，不直接开启整机迁移。

# 2026-09-02 STM32H723 OCTOSPI/OCTOSPIM component VMState boundary（已完成）

- [x] 所属层：STM32H723 SoC 外部存储控制器边界。producer 是 `DmMc02Ospi` 的寄存器
  镜像、间接事务游标、RX/TX staging 和 page-program continuation；boundary 是
  `dm_mc02_ospi_vmstate()`/`dm_mc02_ospi_vmstate_raw()`；consumer 是板级 DMA endpoint、
  memory-mapped alias 以及通过 `DmMc02SsiNor` 接入的 QEMU 标准 `w25q64/m25p80`。
- [x] 新增控制器组件 VMState：保存 `regs[0x400]`、有界动态 RX buffer/游标、TX buffer/
  游标、命令地址和命令状态；从 `CR.FMODE` 重建 memory-mapped gate，生产路径只在恢复
  中断 page-program 时重新选择 SSI CS。`MemoryRegion`、DMA callback、SSI bus/device、
  Flash storage/geometry/JEDEC 配置均为目标端 wiring/static config；NOR 命令和 Flash
  状态继续由 QEMU `m25p80` 自己的 VMState 所有。
- [x] `OCTOSPIM` 增加纯寄存器组件描述；新增隔离测试
  `test-dm-ospi-vmstate` `6/6`，覆盖普通 round-trip、raw 无 runtime projection、
  非法游标/长度拒绝、截断流拒绝。
- [x] 直接 consumer 验证：`run-ospi-smoke.sh`、`run-board-profile-smoke.sh`、
  `qemu-system-arm` 重链通过；`trobot/` 未修改。
- [ ] 限制与下一道门：仍未注册 DM-MC02 machine-level VMState；标准 Flash 子设备、
  Flash backing storage、OSPI/DMA/CPU/IRQ/board reset 的联合恢复顺序，以及完整 OCTOSPI
  line/DTR/async WIP 时序仍需分别定义。下一步继续审计相邻 SoC 外设状态边界，不能据此
  宣称整机 snapshot/migration。

# 2026-09-02 DM-MC02 board reset composition contract（已完成）

- [x] 所属层：DM-MC02 board composition。producer 是板级 reset 请求与外部输入保持状态；
  boundary 是公开的 GPIO/SYSCFG/EXTI/power reset 顺序接口；consumer 是现有 machine
  组合和固件可见的 GPIO、电源、IRQ 状态。
- [x] 新增可复用的 `dm_mc02_board_reset_gpio_power()`，固定执行 GPIO bank reset、
  BMI088 CS inactive 投影、SYSCFG/EXTI reset、外部 GPIO 输入重注入、power model reset、
  下游电源消费者重投影。芯片/器件状态机仍由各自组件 API 所有，machine 只提供组合 hooks。
- [x] machine reset 已接入该边界，保留其它外设的既有 reset 顺序；避免重复调用 power
  setter，电源 ADC source 只由 `dm_mc02_power_reset()` 的一次 runtime update 重建。
- [x] 本切片不引入整机 VMState，不保存 QEMU bus/chardev/clock/timer 指针，也不复制
  任何芯片或器件状态机。变更文件为 `qemu/upstream/hw/arm/dm_mc02_board_reset.[ch]`、
  `dm_mc02.c`、对应 unit/qtest 与 Meson 清单。
- [x] 隔离门 `test-dm-board-reset` `1/1`；直接 consumer
  `dm-mc02-reset-test` `2/2`；`qemu-system-arm` 窄重链和 Host CTest `54/54` 通过。
- [ ] 限制与下一道门：当前只验证 warm/cold reset 的板级 GPIO/电源组合，不代表整机
  snapshot/migration。下一步仍须按依赖顺序定义 GPIO、ADC、CPU/IRQ、DMA、外设、总线及
  co-sim 的 machine composite restore；在此之前不得宣称整机迁移支持。

# 2026-09-02 DM-MC02 board power component VMState boundary（已完成）

- [x] 所属层：DM-MC02 board power boundary。producer 是 `DmMc02Power` 的 VIN、GPIO
  ODR 和 electrical-power policy；boundary 是 `dm_mc02_power_vmstate()`；consumer 是
  24 V/5 V/3.3 V rail projection、VIN/key ADC source 以及未来 board/machine restore。
- [x] 新增 normal/raw VMState：只保存动态输入 `vin_mv`、`gpio_odr`、
  `electrical_power`。ADC 指针、profile wiring masks/channels、派生 rail 状态、ADC raw
  值和 `last_update_ns` 都保持 destination/runtime state；normal post-load 在字段完整
  后调用一次 `dm_mc02_power_sync_runtime()`，raw 描述不产生副作用。
- [x] 变更文件：`hw/arm/dm_mc02_power.[ch]`、新增
  `hw/arm/dm_mc02_power_vmstate.c`、ARM/unit Meson 清单和
  `tests/unit/test-dm-power-vmstate.c`；未修改 `trobot/`。
- [x] 隔离门 `test-dm-power-vmstate` `4/4`；覆盖 rail/ADC 重投影、raw 无副作用、未遮罩
  GPIO ODR 拒绝和
  截断状态拒绝。直接 consumer `run-power-boundary-smoke.sh` 与
  `run-power-runtime-smoke.sh` 均通过。
- [x] 收尾验证：QEMU `qemu-system-arm` 全量目标重链通过；相关 VMState 单测均通过；Host
  CTest `54/54` 通过。负向 VMState 用例产生的非法/截断状态诊断均为预期路径。
- [ ] 限制与下一道门：模型仍是离散 rail 边界，不模拟 converter transient、电流、
  纹波或欠压模拟曲线；该 component 尚未注册 DM-MC02 machine-level VMState。下一步
  继续补齐 board-level 动态组合或审计 machine restore 顺序，不能据此宣称整机迁移。

# 2026-09-02 STM32H723 ARMv7-M CPU/NVIC/SysTick boundary（已完成）

- [x] 所属层：STM32H723 SoC 的 CPU、异常/IRQ 和 SysTick 组成边界。producer 是
  QEMU 原生 `ARMv7MState`、Cortex-M7、NVIC 和 SysTick；boundary 是
  `dm-mc02` 对 `ARMv7MState` 的配置、时钟连接和 IRQ wiring；consumer 是板级外部
  IRQ 输入及 guest 可见的 PPB/SCS 寄存器。
- [x] 复用 QEMU 原生实现，不新增 CPU/NVIC/SysTick 状态镜像或第二套调度器：
  `ARMv7MState` 的 child ownership 由 `object_initialize_child()` 建立，CPU 由
  `object_new_with_props(..., "cpu", ...)` 创建并由 `qdev_realize()` realize。
  QEMU 的 CPU VMState、NVIC VMState 和 SysTick VMState 因设备 realize 自动注册。
- [x] 新增 `tests/qtest/dm-mc02-cpu-test.c` 并加入 `tests/qtest/meson.build`。测试
  QOM child 类型、完整 NVIC vector 数 `179`、板级 external IRQ 输入数 `163`、
  external IRQ priority/enable/pending/level、64 MHz 下 SysTick 1 µs 触发、
  `SCB->ICSR.PENDSTSET` 以及 system reset 清零。
- [x] 隔离/直接边界验证：`dm-mc02-cpu-test` `2/2`；命令为
  `QTEST_QEMU_BINARY=./qemu-system-arm tests/qtest/dm-mc02-cpu-test --tap`。
- [ ] 限制与下一道门：本切片只证明 CPU/NVIC/SysTick 的直接 SoC 边界，不代表完整
  H723 CPU 语义、所有异常、调试/安全扩展或 DM-MC02 整机 snapshot/migration。
  未来 composite restore 必须先在目标端建立相同 QOM graph、clock source 和 IRQ
  wiring，再加载 QEMU 原生 child state；CPU/NVIC/SysTick 与 RAM、外设、DMA、总线
  和 co-sim 队列的联合恢复仍需逐层定义和测试。

# 2026-09-02 STM32H723 message-RAM owner boundary（已完成）

- [x] 所属层：STM32H723 SoC memory composition 到 FDCAN 直接 consumer。producer 是
  三个 FDCAN 共享的 message-RAM 字节存储；boundary 是板卡无关 `DmMessageRam`；consumer
  是 SoC 地址空间映射和 FDCAN 借用的 data pointer。
- [x] `DmMessageRam` 通过 QEMU 标准 `memory_region_init_ram()` 建立 RAM block；当前
  SoC 使用 `NULL` owner，走 QEMU global RAM migration registration。owner 不重复实现
  VMState；data/size/region accessor 是借用接口，reset 是调用者显式的整块清零操作。
- [x] 删除 DM-MC02 machine 私有 FDCAN message-RAM region，改由
  `DmMc02SocMemory.fdcan_msg_ram` 统一初始化、映射和 reset；FDCAN 的 RAM 指针仍为
  destination runtime wiring，不进入 FDCAN component VMState。
- [x] 变更文件：`include/hw/misc/dm_message_ram.h`、`hw/misc/dm_message_ram.c`、
  `hw/arm/dm_mc02_soc.[ch]`、`hw/arm/dm_mc02.c`、相关 Meson 清单、
  `tests/unit/test-dm-message-ram[ -stubs].c` 和 `tests/qtest/dm-mc02-memory-test.c`。
- [x] 验证：owner unit `2/2`、真实 machine qtest `1/1`、FDCAN smoke/medium/bus-off
  全部通过；QEMU smoke `89/89`、Host CTest `54/54`、`qemu-system-arm` 重链通过。
- [ ] 限制与下一道门：这只是 RAM owner/component boundary，未注册 DM-MC02 machine
  VMState，不提供整机 snapshot/migration；其它 RAM、CPU/IRQ、FDCAN/外部 CAN 状态及联合
  restore 顺序仍未闭合。下一步继续从 SoC 底层选择单一恢复边界。

# 2026-09-02 FDCAN component VMState contract（已完成）

- [x] 所属层：STM32H723 FDCAN 芯片层。producer 是 FDCAN CPU-visible 寄存器、RX
  wire partial frame、FIFO/Buffer 游标与填充量、TX pending 位图、统计以及有界的
  chardev TX 队列；boundary 是 `dm_mc02_fdcan_vmstate()`；consumer 是未来的
  machine composite、QEMU `CanBusState` 和 84-byte host wire。
- [x] version-2 普通/raw 描述保存寄存器、部分接收帧、FIFO 状态、pending TX、bus-off
  参与门、统计、host-wire 队列及队列偏移；保留 version-1 load compatibility，从
  `PSR.BO` 重建旧流缺少的 bus-off 状态。`MemoryRegion`、message RAM 指针和内容、CAN
  bus、chardev、IRQ handle、电源和 host-ACK 策略保持 destination/board runtime owner。
- [x] 新增 `tx_next_ns` 作为单调虚拟时间的 chardev retry deadline；修复正数短写后
  原实现不重新调度 retry 的首个错误。post-load 在所有字段通过 FIFO/队列/deadline
  校验后才重建 timer 和 level-sensitive IRQ projection；raw 描述只做 legacy `bus_off`
  归一化/校验，不执行 timer/IRQ/bus/chardev 投影。
- [x] 变更文件：`hw/arm/dm_mc02_fdcan.[ch]`、新增
  `hw/arm/dm_mc02_fdcan_vmstate.c`、ARM/unit Meson 清单、FDCAN VMState 单测和窄
  `MemoryRegion` stub；本轮未修改 `trobot/`。
- [x] 隔离门 `test-dm-fdcan-vmstate` `5/5`；FDCAN standard-bus/host-wire、clock、
  extended-filter、Rx-buffer、bus-off 直接 smoke 通过；`qemu-system-arm` 重链通过。
- [ ] 限制与下一道门：该描述尚未注册 DM-MC02 machine-level VMState，不代表整机
  snapshot/migration；message RAM owner、CAN bus 外部状态、真实仲裁/位时序、error
  frame 和 bus-off 精确恢复仍待单独定义。下一步继续按计划选择相邻 H723 动态外设
  边界，再定义 machine composite 的 owner/恢复顺序。

# 2026-09-02 DWC2 FIFO observation API and atomic MMIO rejection（已完成）

- [x] 所属层：可复用 DWC2 device-mode core 到 DM-MC02 USB adapter 的边界。producer 是
  DWC2 端点 IN/OUT FIFO，boundary 是只读 FIFO pending-byte 查询，consumer 是板级
  USB test/status/packet adapter。
- [x] 新增 `dm_usb_dwc2_endpoint_fifo_count()`：按端点和方向返回 pending bytes；空设备、
  越界端点或空输出指针返回 `false` 且无副作用。adapter 不再读取
  `s->dwc2.endpoint[].{in,out}_fifo_count` 私有字段。
- [x] 修复首个边界错误：QEMU AddressSpace 对 USB MMIO 的非对齐访问会拆成自然对齐
  访问，跨 `DIEPTXF` 写入的尾部可能误修改相邻寄存器。DM-MC02 USB MemoryRegion
  现在保持 guest 非对齐访问非法，同时令实现层不拆分该事务；新增 qtest 覆盖字节/半字、
  非对齐和跨寄存器无副作用。
- [x] 隔离门：DWC2 unit `7/7`；直接 DM-MC02 USB qtest `10/10`；生产 QEMU system
  target 重链通过。本轮未修改 `trobot/`。
- [ ] 限制与下一道门：FIFO count 仍是观察接口，不声称实现真实 USB FIFO 仲裁、DMA、
  PHY、SOF、宿主机枚举或跨 machine 的 USB VMState。下一步继续审计 USB transport
  adapter 与 QEMU `USBBus` 的集成边界，先定义独立的 producer/consumer 契约。

# 2026-09-02 DWC2 device-mode component VMState contract（已完成）

- [x] 完成可复用 `DmUsbDwc2Device` 的组件级 VMState：保存 DWC2 全局/设备寄存器、
  `DIEPTXF[0..14]`、16 个端点的寄存器/FIFO/游标、传输 PID/halt/config 状态、DMA 地址
  以及 FIFO overflow 计数；FIFO 游标固定使用 64-bit 大端编码，枚举固定使用 32-bit
  大端编码，不依赖主机 ABI 宽度。
- [x] 恢复前校验版本、EP0 MPS、端点 FIFO 游标/count、端点类型和 DATA0/DATA1 PID。
  callback、control ownership、opaque、IRQ callback/opaque/level 均为目的端 runtime
  wiring；只有全部字段有效后才重绑 transaction callback 并重新投影 level-sensitive IRQ。
  非法状态或版本失败不会触碰目的端 runtime wiring。
- [x] 变更文件：`qemu/upstream/hw/usb/dm_usb_dwc2_device.h`、
  `dm_usb_dwc2_device.c`、`dm_usb_dwc2_device_vmstate.c`、
  `tests/unit/test-dm-usb-dwc2-vmstate.c` 及 USB/unit 构建清单；未修改 `trobot/`。
- [x] 隔离门 `test-dm-usb-dwc2-vmstate` `4/4`；DWC2 core `7/7`、QEMU adapter
  `13/13`、DM-MC02 USB qtest `10/10`，`qemu-system-arm` 重链通过。
- [ ] 限制与下一道门：这只是组件状态契约，尚未注册 DM-MC02 machine-level snapshot/
  migration。DWC2 control core、NVIC、USB PHY、DMA、SOF、真实 USB 枚举，以及 DWC2 与
  control/IRQ/board/co-sim 队列的联合恢复顺序仍未覆盖；下一步继续审计 USB transport
  adapter 与 QEMU `USBBus` 的整机集成边界。

# 2026-09-02 ADC12 regular-simultaneous common DMA overrun request gate（已完成）

- [x] 所属层：STM32H723 ADC12 common 芯片层到既有 DMA consumer 边界。producer 是
  ADC1/ADC2 完成的 regular pair；boundary 是 `ISR.OVR` 对 common CDR DMA request
  的联动；consumer 是 ADC1 request 9 的 CDR endpoint/MMIO 路径。
- [x] 对 `DUAL=0x6 + DAMDF=0x2`，common 继续更新 CDR，但当任一 status source 的
  OVR 置位时不调用 CDR `data_ready` callback；不新增 common/DMA 私有 latch。两路
  OVR 清除后下一 pair 恢复 request。DAMDF=3 明确不经过此 gate。
- [x] 变更：`hw/arm/dm_mc02_adc_common.[ch]`、`tests/unit/test-dm-adc-common.c`、
  `tests/qtest/dm-mc02-adc-test.c`。隔离 common unit `15/15`，endpoint/MMIO 直接
  gate `2/2`；未修改 `trobot/`。
- [ ] 限制与下一道门：其它 dual/DAMDF、CDR2、精确 DMA 仲裁/物理传输时序和
  machine-level migration 仍未支持。继续在 STM32H723 层选择单一独立状态边界，先
  完成隔离测试再接入更高层。

# 2026-09-02 ADC regular DMA overrun request gate（已完成）

- [x] 所属层：STM32H723 ADC regular data/DMA 边界。producer 是 ADC regular rank
  completion；boundary 是 `ISR.EOC/OVR` 与 `CFGR.OVRMOD` 共同决定的 DMA request
  gate；consumer 是 ADC1/ADC2 的既有 DMA request path。
- [x] 实现：`adc_emit_sample()` 记录进入函数前的 `OVR`，并在旧 `EOC` 造成当前
  overrun 时共同禁止该次 ADC-local DMA request；`OVR` 仍只由 ISR W1C 清除，未增加
  DMA 私有门控状态。common CDR/CDR2 的独立 data-ready consumer 不受此本地 gate 误伤，
  因而 DAMDF=3 四值 partial accumulation 仍可继续形成完整 CDR word。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_adc.c` 和
  `qemu/upstream/tests/qtest/dm-mc02-adc-test.c`；未修改 `trobot/`。
- [x] 直接边界测试：新增
  `/dm-mc02/adc/regular-dma-overrun-request-gate`，对 endpoint/MMIO 两条 ADC_DR
  consumer 路径分别覆盖 `OVRMOD=0/1`；验证未服务 DMA 时首个 EOC、第二个 rank 的
  OVR 与保留/覆盖数据、OVR 置位期间 request/NDTR 不变，以及 ISR W1C 和 DR 消费后
  下一 rank 恢复 DMA、NDTR=0、TCIF。完整 ADC qtest `60/60` 通过。
- [ ] 限制与下一道门：该 gate 只约束 ADC-local regular DMA request；common CDR/CDR2
  request 的 ADC OVR 联动、其它 dual/DAMDF、精确 DMA 仲裁/物理传输时序，以及
  common/ADC/DMA 联合 machine-level migration 仍未支持。下一步继续留在 STM32H723
  芯片层选择一个单一未覆盖边界，不向板级/UI 添加 workaround。

# 2026-09-02 ADC12 regular-interleaved DAMDF=3 8-bit packing boundary（已完成）

- [x] 所属层：可复用 STM32H723 ADC12 common 芯片层到 DM-MC02 DMA 直接边界。
  producer 是 ADC1/ADC2 regular rank 完成事件，boundary 是 DAMDF=3 下
  `ADC12_COMMON.CDR` 的四个 8-bit 值累积和 data-ready/read-ack 契约，consumer 是
  ADC1 request 9 对应的 DMA stream；覆盖 regular-interleaved `DUAL=0x7/0x3`。
- [x] 按 RM0468 28.4.32，common 只接受 `master, slave, master, slave` 的连续来源顺序，
  在四个值齐备后按 `CDR[7:0] = M0`、`[15:8] = S0`、`[23:16] = M1`、`[31:24] = S1`
  发布一个完整 word；部分累积不发 DMA request，顺序错误丢弃当前部分 word。
  data-ready timestamp 取四个 producer timestamp 的最大值，避免回调顺序造成时间回退。
- [x] common VMState 从 v4 升至 v5，保存 DAMDF=3 的累积 timestamp；v1–v4 载入时清除
  新字段。修复 pair matcher 在对端先到时误读当前 source timestamp 的根因，并增加完整
  packed word 与非递增 producer timestamp 回归。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_adc_common.h`、
  `qemu/upstream/hw/arm/dm_mc02_adc_common.c`、
  `qemu/upstream/hw/arm/dm_mc02_adc_common_vmstate.c`、
  `qemu/upstream/tests/unit/test-dm-adc-common.c` 和
  `qemu/upstream/tests/qtest/dm-mc02-adc-test.c`；未修改 `trobot/`。
- [x] 隔离门 `test-dm-adc-common` `14/14`；DAMDF=3 endpoint/MMIO qtest 均通过，完整
  ADC qtest `59/59`；QEMU smoke suite `89/89`；Host CTest `54/54`；QEMU system target
  重链通过。
- [ ] 限制与下一道门：CDR2 在 DAMDF=3 下仍未启用；其它 dual/DAMDF、精确 DMA 仲裁/
  物理传输时序、overrun 停止请求和 common/ADC/DMA 联合 machine-level migration 仍未
  支持。下一步继续留在 STM32H723 芯片层选择单一未覆盖边界。

# 2026-09-02 ADC12 CDR2 regular-interleaved DMA boundary（已完成）

- [x] 所属层：可复用 STM32H723 ADC12 common 芯片层到 DM-MC02 直接 DMA 边界。producer
  是 ADC1/ADC2 regular rank 完成事件，boundary 是 `ADC12_COMMON.CDR2.RDATA_ALT`
  的 32-bit multimode data-ready/read-ack 契约，consumer 是 ADC1 request 9 对应的 DMA
  stream。覆盖 `DUAL=0x7/0x3` 与 `DAMDF=0x2`；不扩展其它 dual/DAMDF、8-bit 四值
  packing、物理 DMA 时序或 machine migration。
- [x] CDR2 在每个 ADC1/ADC2 regular EOC 到达时更新为零扩展 16-bit result，并发出
  一次 common data-ready；DMA 成功读取后只确认产生该值的 ADC 的 EOC。endpoint 和
  MemoryRegion 两条 consumer 路径共享同一 read-ack 边界；未消费的旧值在下一事件到来
  时由硬件式 single-register overwrite 语义替换。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_adc_common.h`、
  `qemu/upstream/hw/arm/dm_mc02_adc_common.c`、
  `qemu/upstream/hw/arm/dm_mc02_adc_common_vmstate.c`、
  `qemu/upstream/hw/arm/dm_mc02.c`、`qemu/upstream/tests/unit/test-dm-adc-common.c`
  和 `qemu/upstream/tests/qtest/dm-mc02-adc-test.c`。
- [x] 隔离门 `test-dm-adc-common` `13/13`；直接 endpoint 和 MMIO CDR2 DMA
  qtest 均通过；完整 ADC qtest `57/57`；QEMU system target 已重链。本轮未修改
  `trobot/`。
- [ ] 限制与下一道门：`DAMDF=0x3`/8-bit packing、其它 dual/DAMDF、精确 DMA
  arbitration/physical transfer timing，以及 common/ADC/DMA 联合 machine-level
  migration 仍未支持。下一步继续留在 ADC 芯片层，优先定义其它明确的 data-format
  边界或 DMA 时序契约；不向板级/UI 添加 workaround。

# 2026-09-02 ADC12 regular-interleaved cadence and `CCR.DELAY` boundary（已完成）

- [x] 所属层：可复用 STM32H723 ADC kernel/common 芯片层到 DM-MC02 直接组合边界。
  producer 是 common-owned ADC1 master 的软件启动或 regular 外部触发，boundary 是
  ADC2 peer 的绝对虚拟时间启动契约，consumer 是 ADC2 regular conversion scheduler。
- [x] `DUAL=0x7` 与 `DUAL=0x3` 均采用 ADC1/master-only admission；ADC2 必须已使能并由
  common composition 通过公开 API 启动，不允许 ADC2 自己再次消费同一启动/触发。
  软件启动和 TIM8 regular trigger 都遵循该规则，非 interleaved 的 `DUAL=0x6` 路径保持
  原有 simultaneous 行为。
- [x] interleaved slave 的采样起点为 master 起点之后的 master sampling phase 加
  `CCR.DELAY` 对应的半 ADC-kernel-clock 数；rank 完成时间仍由各 ADC 自己的采样时间、
  当前模型的 17 conversion half-cycles 和有效 kernel clock 调度。`CCR.DELAY` 按 RM0468
  Table 236 的 16/14/12/10-bit 表解释，计算使用整数虚拟纳秒并对每个 deadline 向上取整。
- [x] 新增并保持板卡无关的 `dm_mc02_adc_start_regular_at()`、
  `dm_mc02_adc_external_trigger_with_id_at()` 和
  `dm_mc02_adc_interleaved_slave_start_ns()`；common 对外提供
  `get_dual()`、`get_delay_code()` 和 `regular_interleaved()`，不读取 ADC 私有状态。
- [x] 隔离门 `test-dm-adc-common` `12/12`；直接 ADC qtest `55/55`，包含软件启动、
  TIM8 外部触发、`DELAY=0/2`、默认分辨率以及 `DUAL=0x3` injected simultaneous 的
  同时完成检查；增量 QEMU system target 构建通过。本轮未修改 `trobot/`。
- [ ] 限制与下一道门：CDR2 DMA、其它 dual/DAMDF 格式、精确 event-count 队列、injected
  common data packing、物理 DMA 仲裁/传输时序和 common/ADC/timer/DMA machine-level
  migration 仍未支持。下一步继续在 ADC 芯片层选择一个单一未覆盖边界，不向板级/UI 添加
  workaround。

# 2026-09-02 ADC12 CDR2 regular-interleaved boundary（已完成）

- [x] 所属层：可复用 STM32H723 ADC12 common 芯片层。producer 是 ADC1/ADC2 已完成的
  regular rank 事件；boundary 是 `ADC12_COMMON.CDR2.RDATA_ALT`；consumer 是 common
  的只读 CDR2 数据寄存器。依据本地 STM32H723 CMSIS/HAL 定义，覆盖
  `DUAL=0x7`（regular interleaved）和 `DUAL=0x3`（regular interleaved + injected
  simultaneous）。
- [x] CDR2 每次接受一个已完成 regular rank 并保留其 16-bit regular result value 的零扩展值；
  后到事件覆盖前值，不进入 CDR 的 master/slave pair matcher，也不触发 CDR DMA
  callback。`DAMDF != 0` 在该边界明确拒绝，不把 CDR DMA 格式静默套用到 CDR2。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_adc_common.h`、
  `qemu/upstream/hw/arm/dm_mc02_adc_common.c`、
  `qemu/upstream/tests/unit/test-dm-adc-common.c` 和
  `qemu/upstream/tests/qtest/dm-mc02-adc-test.c`。
- [x] 隔离门 `test-dm-adc-common` `12/12`；直接 qtest
  `/dm-mc02/adc/common-cdr2-regular-interleaved` 通过，验证两个 dual 配置、连续事件
  覆盖、CDR 保持未使用和 reset 清除；本轮未修改 `trobot/`。
- [ ] 限制与下一道门：当前只实现 CDR2 data-register producer/consumer 边界；其
  interleaved master/slave cadence 与 `CCR.DELAY` 已由上方切片覆盖，但 CDR2 DMA（本地
  HAL DMA 地址 API 仍只指向 CDR）、其它 dual/DAMDF 模式和联合 machine-level migration
  仍未支持。下一步继续在 ADC 芯片层选择一个单一边界，不向板级/UI 添加 workaround。

# 2026-09-02 ADC12 CDR read acknowledgement boundary（已完成）

- [x] 所属层：可复用 STM32H723 ADC/common 芯片层到 DM-MC02 直接组合边界。producer 是
  `ADC12_COMMON.CDR` 的 regular simultaneous packed result；boundary 是 common CDR
  read notification；consumer 是 ADC1/ADC2 regular data-consumption path。该切片依据
  RM0468 28.4.32 的 DMA/软件读取确认语义，只覆盖已建模的 `DUAL=0x6` 与
  `DAMDF=0x2/0x3`。
- [x] common 提供 `DmMc02AdcCommonCdrRead` callback、
  `dm_mc02_adc_common_set_cdr_read_callback()` 和
  `dm_mc02_adc_common_notify_cdr_read()`。任何合法、与 CDR 重叠的 MMIO 读取都会在返回
  数据后通知；endpoint DMA 因不经过 common `MemoryRegion`，在成功搬运后显式调用同一
  通知。DM-MC02 consumer 通过 ADC 公共 API 同时清除 ADC1/ADC2 EOC，并复用原有 data
  consumption continuation，因此 `AUTDLY` 会在 CDR 被消费后继续。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_adc_common.h`、
  `qemu/upstream/hw/arm/dm_mc02_adc_common.c`、`qemu/upstream/hw/arm/dm_mc02_adc.h`、
  `qemu/upstream/hw/arm/dm_mc02_adc.c`、`qemu/upstream/hw/arm/dm_mc02.c`、
  `qemu/upstream/tests/unit/test-dm-adc-common.c` 和
  `qemu/upstream/tests/qtest/dm-mc02-adc-test.c`。
- [x] 隔离门 `test-dm-adc-common` `12/12`；直接 qtest
  `/dm-mc02/adc/common-cdr-read-acknowledges-both-eoc` 验证 MMIO 读取和 AUTDLY 继续，
  隔离单测覆盖不支持模式不产生副作用；endpoint 与 MMIO CDR DMA 边界均通过，ADC qtest
  `55/55`，串行 Host CTest `54/54`。QEMU system target 已重链，本轮未修改 `trobot/`。
- [ ] 限制与下一道门：该确认语义仍仅覆盖 `DUAL=0x6`、`DAMDF=0x2/0x3`；其它 dual/
  DAMDF 模式、CDR2 DMA、精确物理 DMA 时序和 common/ADC/DMA 联合 machine-level
  migration 仍未支持。interleaved cadence 与 `CCR.DELAY` 已在上方切片完成；下一道门
  继续留在 ADC 芯片层，优先定义 injected common data packing 或 CDR2 DMA 的单一契约。

# 2026-09-01 架构收敛约束与复用迁移门（进行中）

- [x] 已将真实性裁决、分层边界、复用优先、单一 production 实现、QEMU feature ownership、
  虚拟时间、实时性能和支持声明收敛为 `ARCHITECTURE.md`；项目与工作区 `AGENTS.md` 均将其
  作为实现前必须检查的规范。
- [x] 已固定证据优先级：可复现实机优先，资料与固件行为作为交叉证据；实机偏离资料必须
  可追溯并具有 deterministic regression，不能隐式推广为 H723 通用行为。
- [x] 已固定复用策略：适配 QEMU 的 USB/CAN/SSI/NOR/VMState 等通用能力后淘汰相同的本地
  production 栈，旧代码仅允许以 test-only fixture 存在；不保留长期运行时双轨。
- [x] 已完成第一道迁移门：为 `CONFIG_STM32H723` 与 `CONFIG_DM_MC02` 建立隔离构建。
- [x] 已完成第二道迁移门：DM-MC02 生产路径通过板卡无关的 `DmMc02SsiNor` 适配器，将
  `OCTOSPI2` 接入 QEMU 标准 `w25q64/m25p80`；旧 `DmNorFlash` 仅保留在带
  `DM_MC02_OSPI_TEST_FIXTURE=1` 的 host 隔离测试目标中。
- [x] 已完成第三道迁移门：FDCAN 生产路径通过板卡无关的 `DmCanBusAdapter` 接入 QEMU
  标准 `CanBusState`；旧 virtual-time CAN medium 不再进入 ARM production target，仅保留
  为 test-only fixture 源码。
- [x] 已建立并验证第一批组件级 VMState 契约：CORDIC、CRC、RNG 和 DBGMCU 均拥有独立
  描述与 QEMUFile round-trip 测试；DBGMCU 的 IDCODE 真实性和只读边界另有真实 guest
  smoke。它们仍未注册到 machine，不构成整机迁移支持。
- [x] 已完成 IWDG 动态状态切片：寄存器、启动/解锁/宽限状态和绝对虚拟超时点拥有独立
  VMState 描述；post-load 只在校验成功后重建 QEMUTimer。独立单测 `5/5`、IWDG guest
  smoke 和 QEMU 重链均通过，仍未注册 machine-level VMState。
- [x] 已补齐第二个纯寄存器状态切片：SYSCFG VMState 只保存寄存器镜像，回载后通过运行时
  `changed` 回调重新驱动 EXTI 路由；独立单测覆盖 round-trip、通知和截断流拒绝。
- [x] 已补齐 FMC 纯寄存器状态切片：VMState 只保存 `regs[0x400]`，独立 QEMUFile 单测
  覆盖 round-trip/截断流，`dm-mc02-fmc-test` 覆盖真实 machine MMIO 字节写和 reset。
- [x] 已完成 GPIO 组件级 VMState：仅迁移 8 个 CPU-visible GPIO 寄存器镜像，保留
  `bank_index`、`MemoryRegion` 和 ODR callback 等运行时 wiring；独立单测和真实 GPIO
  A–E 直接消费者 smoke 均通过。该描述已纳入 STM32H723 ARM 生产源列表，但仍未注册
  machine-level VMState。
- [x] 已完成 STM32H723 internal Flash 组件级 VMState：仅迁移 Flash 寄存器镜像和两个
  解锁序列标志；storage、overlay 和运行时 MemoryRegion wiring 由 post-load 根据
  `FLASH_CR1.LOCK/PG` 重建，独立单测和 Flash guest smoke 均通过，仍未注册 machine。
- [x] 已完成 STM32H723 EXTI 组件级 VMState：仅迁移 `regs[0x400]` 和 16 条输入线的
  采样电平；IRQ handle 与派生 IRQ 电平属于运行时 wiring，post-load 强制重建七组 IRQ
  投影，独立单测和 EXTI guest smoke 均通过，仍未注册 machine。
- [x] 已完成 STM32H723 DMAMUX 组件级 VMState：仅迁移 `regs[0x400]` 和 DMA 请求缓存
  使用的 generation；`MemoryRegion` 属于运行时 wiring，独立单测和 DMA 直接消费者
  smoke 均通过，仍未注册 machine。
- [ ] 下一道门（按层逐项执行）：继续审计并补齐其它纯寄存器/动态 H723 外设；之后再处理
  timer、IRQ、DMA、片上 RAM、QEMU 总线和 co-sim 队列，最后才设计 machine-level
  save/load。USB host transaction 的 QEMU core 收敛仍作为独立迁移门。每步需独立差分、
  边界、固件与性能验证，不能并行宣称完成。
- [x] 已实现 60 virtual-second Release RTF gate：从 reset 启动 Release 固件，等待
  `xTickCount >= 250` 的 startup-ready 点，以虚拟 tick 达到目标后停止，检查 IWDG
  diagnostics，并汇总三次 startup latency、RTF、CPU 和 RSS。标准 gate 见
  `tools/run-release-rtf-gate.sh`，底层采集器仍保留旧 wall-clock baseline 模式。
- [x] 标准 gate 的 1.0x 目标使用 `0.999x` 判定容差，以吸收 QMP/wall-clock 采样抖动；
  输出仍保留精确 RTF，不把 `0.999999x` 一类的同步运行误报成失败。三次 60 s 验证为
  RTF `0.999999..1.000012x`、IWDG `timeouts=0`、CPU `115.65..115.69%`、RSS
  `50384..50632 KiB`，startup latency `0.523267..0.523908 s`。
- [x] 该验证首次暴露并修复了真正的底层 USB 缺陷：DWC2 `DIEPTXF[0..14]` 只写入
  legacy compatibility array，guest 通过 DWC2 MMIO 读取到 0，CherryUSB 在
  `usb_dc_init -> dwc2_set_txfifo()` 断言死循环，最终触发 IWDG reset。修复位于可复用
  DWC2 core，补齐上电 FIFO 深度和读写寄存器；新增 `/dm-mc02/usb/power-on-tx-fifo-registers`。
  USB qtest 现为 `10/10`，真实固件 1 s 短测和正式三次 gate 均无 reset。

## 2026-09-02 ADC12 regular-simultaneous multimode DMA boundary（已完成）

- [x] 所属层：STM32H723 ADC/common/DMA 芯片层到 DM-MC02 直接组合边界。producer 是
  ADC1/ADC2 通过 shared `conversion_id` 和 zero-based rank 提交的完整 regular pair；
  boundary 是 `ADC12_COMMON.CDR` 的 packed-data-ready 回调和 DMA request 路由；consumer
  是 DMA1 ADC1 request 9 的 endpoint 或 MMIO 数据路径。
- [x] `DUAL=0x6` 且 `DAMDF=0x2/0x3` 时，每个完整 CDR pair 产生一个 DMA beat；DAMDF=2
  将 ADC1 放入低 half-word、ADC2 放入高 half-word，DAMDF=3 使用低字节。只有 ADC1
  regular DMA data-management 配置非零时才触发 common CDR DMA。
- [x] 修复 DMA request cache 的首个错误：ADC1 `DR` 与 `ADC12_COMMON.CDR` 可以共享
  request 9，缓存现在按 `(request_id, peripheral_addr)` 区分；因此 endpoint 与 MMIO
  consumer 不会被另一种 endpoint 的负缓存结果饿死。
- [x] 隔离门 `test-dm-adc-common` `12/12`；直接 qtest endpoint/MMIO/request-key 三条
  边界和完整 ADC qtest `53/53`；`qemu-system-arm` 重链、QEMU smoke suite `89/89`、Host CTest
  `54/54` 均通过。两个 regular rank 精确断言 `0x01000100`、`0x02000200`、NDTR=0
  和 DMA TC。
- [x] 首个失败状态属于测试 consumer fixture：没有写 `ADC12_COMMON.CCR`，所以 ADC1/2
  没有进入 common pairing；修复配置后又发现 oracle 错把第二个 rank 期待为重复值，已按
  producer 的真实第二 rank 修正。生产路径未添加板级特判或 UI fallback。
- [ ] 限制与下一道门：CDR2、其它 dual/DAMDF 模式、精确 DMA
  arbitration/physical transfer timing，以及 common/ADC/DMA machine-level migration
  仍未实现。继续选择一个 STM32H723 芯片层动态状态切片并先完成隔离/边界门。

## 2026-09-01 STM32H723 internal Flash component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 Flash 寄存器状态层。producer 是 `DmMc02Flash` 的
  CPU-visible `regs[0x400]`、主 Flash 解锁序列状态 `key1_seen` 和 option-byte 解锁
  序列状态 `optkey_seen`；boundary 是 `dm_mc02_flash_vmstate()`；consumer 是未来的
  machine-level migration 组合，以及当前 Flash 控制器的 runtime overlay wiring。
- [x] VMState 版本为 1，只保存上述寄存器镜像和两个解锁状态。`storage` 指针、
  `storage_size`、`storage_region`、`program_window`、`program_enabled` 和 caller-owned
  backing bytes 都排除在状态流之外；成功 post-load 后依据恢复的 `FLASH_CR1.LOCK/PG`
  通过 `dm_mc02_flash_sync_runtime()` 重建编程 overlay。
- [x] `FLASH_CR1` 写路径和 post-load 共用同一个 runtime-sync 边界；warm reset 清除
  控制器状态并关闭 overlay，但保留 caller-owned Flash 内容。截断或版本错误的状态在
  VMState 边界拒绝，且不会提前重建 overlay。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_flash.h`、`dm_mc02_flash.c`、
  `dm_mc02_flash_vmstate.c`、`hw/arm/meson.build`、`tests/unit/meson.build` 和
  `tests/unit/test-dm-flash-vmstate.c`。
- [x] 隔离门 `test-dm-flash-vmstate` TAP `3/3` 通过，覆盖完整 round-trip、锁定状态
  overlay 关闭和截断流拒绝；直接 consumer `tools/run-flash-smoke.sh` 通过，覆盖
  编程、NOR 1->0、sector erase、EOP/错误清除和 warm reset relock。
- [x] 验证：`ninja -C build/qemu qemu-system-arm`、QEMU smoke suite `89/89`、串行
  Host CTest `54/54` 均通过；本轮未修改 `trobot/`，没有活跃子代理或残留构建进程。
- [ ] 限制与下一道门：该描述仍未注册到 DM-MC02 machine，不能宣称整机 snapshot/
  migration；片上 Flash backing RAM 的保存/迁移边界、真实 latency/WIP、ECC、option
  bytes 和掉电语义仍未实现。下一步继续在 STM32H723 层审计下一个独立外设状态边界，
  不用 machine 或 UI workaround 提前开启整机迁移。

## 2026-09-01 GPIO component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 外设状态层。producer 是 GPIO bank 的
  `MODER/OTYPER/OSPEEDR/PUPDR/IDR/ODR/AFR0/AFR1` CPU-visible 状态；boundary 是
  `dm_mc02_gpio_vmstate()`；consumer 是未来的 machine-level save/load 组合以及当前
  DM-MC02 GPIO/EXTI/CS/LED/电源等直接板级使用者。
- [x] VMState 仅保存上述 8 个 `uint32_t` 镜像，不保存 `bank_index`、`MemoryRegion`、
  ODR callback 或 opaque。成功加载后通知目标 callback 一次，使板级派生输出重新计算；
  版本错误或截断流不会触发 callback。
- [x] 隔离门 `test-dm-gpio-vmstate` TAP `2/2` 通过，覆盖完整 round-trip、bank metadata
  保留、post-load 通知和截断流拒绝；直接消费者 `tools/run-gpio-smoke.sh` 通过，覆盖
  GPIO A–E、BSRR、ODR/IDR、AFR、子字节访问和 BMI088 CS 路由。
- [x] 验证：`ninja -C build/qemu qemu-system-arm` 通过；完整 QEMU smoke suite
  `89/89`；串行 host CTest `54/54`；未修改 `trobot/`。
- [ ] 限制与下一道门：GPIO 仍是当前安全的寄存器/派生输入模型，不宣称完整 H723 电气
  争用、模拟输入、锁存/复位细节或 machine-level snapshot/migration；下一步继续审计
  其它底层外设状态，不向板级/UI 添加迁移 workaround。

## 2026-09-01 CORDIC component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 外设状态层。producer 是 CORDIC 的 CSR、参数和结果
  FIFO 状态；boundary 是独立的 `vmstate_dm_mc02_cordic`；consumer 是未来的
  machine-level save/load 组合。VMState 描述不保存 `MemoryRegion`、函数指针、QOM
  owner 或其它运行时连接。
- [x] 新增 `dm_mc02_cordic_vmstate.c`，保存 CSR、`args[2]`、`arg_count`、
  `results[2]` 和 `result_count`，加载后拒绝超过固定数组容量的损坏游标；描述通过
  `dm_mc02_cordic_vmstate()` 以窄公共接口提供。
- [x] 隔离门 `test-dm-cordic-vmstate` 覆盖完整字段 round-trip、QEMUFile EOF 终止标记
  和非法参数计数拒绝；VMState 描述拆成独立源文件，测试不需要链接整套 MMIO memory
  dispatch 实现。
- [x] 验证：独立目标构建和 TAP `2/2` 通过；随后完整 `qemu-system-arm` 重链、独立
  QEMU smoke suite `88/88` 和串行 host CTest `54/54` 均通过；未修改 `trobot/`。
- [ ] 本切片不注册 machine-level VMState，不宣称 snapshot/migration 支持。下一道门
  仍需逐个为本地外设建立独立状态契约，随后才可审计片上 RAM、动态 timer、外部
  `CharBackend/CanBus/USB` 和 co-sim 队列，再设计整机 save/load gate。

## 2026-09-01 CRC component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 外设状态层。producer 是 CRC 的 CPU-visible 寄存器镜像和
  当前累加值；boundary 是独立的 `vmstate_dm_mc02_crc`；consumer 是未来的 machine-level
  save/load 组合。VMState 不保存 `MemoryRegion`、QOM owner 或运行时连接。
- [x] 新增 `dm_mc02_crc_vmstate.c` 和 `dm_mc02_crc_vmstate()`，保存完整 `regs[64]` 与
  `value`；独立 `test-dm-crc-vmstate` 覆盖 QEMUFile round-trip 和 EOF，TAP `1/1` 通过。
- [x] 直接消费者 `run-crc-smoke.sh` 通过；完整 `qemu-system-arm` 重链、QEMU smoke suite
  `88/88` 和串行 host CTest `54/54` 均通过；未修改 `trobot/`。
- [ ] 当前只提供组件契约，未注册 machine-level VMState；仍需逐个补齐带 timer、IRQ、DMA、
  总线或外部连接的设备状态，再审计整机 save/load 边界。

## 2026-09-01 RNG component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 外设状态层。producer 是 RNG 的寄存器镜像、PRNG/FIFO
  状态和 refill marker；boundary 是独立的 `vmstate_dm_mc02_rng`；consumer 是未来的
  machine-level save/load 组合。VMState 不保存 `MemoryRegion`、`qemu_irq` 或其它运行时连接。
- [x] 新增 `dm_mc02_rng_vmstate.c` 和 `dm_mc02_rng_vmstate()`，保存下一次寄存器操作所需的
  完整状态；加载后拒绝超出四字 FIFO 的 count/index 以及矛盾的 refill marker。
- [x] 独立 `test-dm-rng-vmstate` 覆盖完整 round-trip、EOF 和三类非法 FIFO 状态，TAP `2/2`
  通过；直接消费者 `run-rng-smoke.sh`、完整 `qemu-system-arm` 重链、QEMU smoke suite
  `88/88` 和串行 host CTest `54/54` 均通过；未修改 `trobot/`。
- [ ] 当前仍只提供组件契约，未注册 machine-level VMState；IRQ wiring、片上 RAM、定时器、
  DMA、总线和 co-sim 队列仍需分别审计后才能开启整机 save/load。

## 2026-09-01 DBGMCU component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 外设状态层。producer 是 DBGMCU 可变寄存器镜像；boundary
  是独立的 `vmstate_dm_mc02_dbgmcu`；consumer 是未来的 machine-level save/load 组合。
  `MemoryRegion` 和 QOM owner 不属于可迁移状态。
- [x] 修复首个错误：DBGMCU 不再把所有地址当作无语义字节存储；`IDCODE` 固定读取
  `0x20030483`，写入被忽略，其他控制寄存器保留可变镜像。该值与现有 H723 Renode
  register probe 及固件版本探测契约一致。
- [x] 新增 `dm_mc02_dbgmcu_vmstate.c` 和 `dm_mc02_dbgmcu_vmstate()`，保存完整可变镜像；
  独立 `test-dm-dbgmcu-vmstate` 覆盖首尾寄存器 round-trip 和截断流拒绝。
- [x] 直接门 `tools/run-dbgmcu-smoke.sh` 覆盖真实 Cortex-M7 IDCODE、只读写保护和控制
  寄存器读回；本切片只提供组件契约，未注册 machine-level VMState。
- [ ] 限制与下一道门：当前仍未实现 DBGMCU 各控制寄存器的完整位掩码、debug halt/低功耗
  影响和所有 H723 调试功能；下一步继续在同一层审计 SYSCFG/FMC/GPIO 的状态边界。

## 2026-09-01 SYSCFG component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 外设状态层。producer 是 SYSCFG CPU-visible 寄存器镜像；
  boundary 是独立的 `vmstate_dm_mc02_syscfg`；consumer 是未来的 machine-level save/load
  组合以及当前板级 EXTI 路由回调。`MemoryRegion`、函数指针和 opaque 不进入迁移流。
- [x] 新增 `dm_mc02_syscfg_vmstate.c` 和 `dm_mc02_syscfg_vmstate()`，保存完整
  `regs[0x400]`。post-load 只在目标已有 `changed` 回调时通知一次，让 consumer 从恢复的
  `EXTICR1..4` 重新派生路由；没有回调时仍可作为纯组件使用。
- [x] 独立 `test-dm-syscfg-vmstate` 覆盖完整寄存器 round-trip、EXTICR 字节恢复、post-load
  通知以及截断流拒绝；测试不链接整套 SYSCFG MMIO dispatch。
- [x] 验证门：SYSCFG VMState 单测、`run-exti-smoke.sh`、QEMU 增量重链、完整 QEMU smoke
  suite 和串行 Host CTest 均通过；未修改 `trobot/`。
- [ ] 限制与下一道门：SYSCFG 控制寄存器仍是当前模型的安全字节镜像，未补齐所有 H723 位
  掩码/安全域语义；machine-level snapshot/migration 仍未开启。下一步审计 GPIO
  的组件状态边界。

## 2026-09-01 FMC component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 外设状态层。producer 是 FMC CPU-visible 寄存器镜像；
  boundary 是独立的 `vmstate_dm_mc02_fmc`；consumer 是未来的 machine-level save/load
  组合和当前 machine 的 FMC MMIO dispatch。`MemoryRegion` 与 QOM owner 不进入迁移流。
- [x] 新增 `dm_mc02_fmc_vmstate.c` 和 `dm_mc02_fmc_vmstate()`，仅保存完整
  `regs[0x400]`；`test-dm-fmc-vmstate` 覆盖首、中、尾字节 round-trip 和截断流拒绝。
- [x] 直接边界 `dm-mc02-fmc-test` 通过，验证真实 `dm-mc02` machine 的 FMC 地址映射、
  32-bit 读写、byte-lane 写入以及 warm reset 清零；QEMU `qemu-system-arm` 重链、
  board-profile smoke、完整 QEMU smoke suite `89/89` 和串行 host CTest `54/54` 通过。
- [ ] 限制与下一道门：当前 FMC 仍是安全字节镜像，不实现 H723 FMC 的寄存器位掩码、外部
  SRAM/NOR memory window、访问时序、wait-state、ECC 或中断；machine-level
  snapshot/migration 仍未开启。下一步进入 GPIO 组件状态审计。

## 2026-09-01 FDCAN -> QEMU standard CAN bus 迁移（已完成）

- [x] 所属层：可复用 CAN transport adapter 到 STM32H723 FDCAN 的直接边界。producer 是
  FDCAN message-RAM TX path，boundary 是 `DmCanBusAdapter`/QEMU `CanBusState`，consumer
  是其它 CAN bus clients 与 FDCAN RX filter/FIFO/Buffer path；adapter 不读取板级 pin 或
  FDCAN 私有状态。
- [x] FDCAN1/2/3 现在共享标准 QEMU CAN bus；支持外部 `-object can-bus,id=canbus
  -machine dm-mc02,canbus=canbus`，未指定时由 machine 创建板内 bus。标准 bus 的投递是
  即时的、发送者不回环，并以至少一个可接收 peer 的返回值作为 bus-level ACK 结果。
- [x] 固定 84-byte chardev 仍作为独立 legacy host wire，保留其外部 virtual timestamp；
  它不再与生产 CAN bus transport 叠加为第二 medium。`fdcan-host-ack=on` 仍是显式 host
  bridge policy，不等同于物理 ACK。
- [x] 旧 `dm_mc02_can_medium` 不再链接到 ARM Meson production target；源码仅作为带明确
  test-only 边界的未来 virtual-time scheduler fixture 保留。已删除生产路径对
  `fdcan-accurate-timing`、帧持续时间和 CAN-ID 仲裁的依赖。
- [x] 隔离门：`test-dm-can-bus-adapter` 覆盖广播、ACK、无回环、无 peer 和断开生命周期；
  `run-can-medium-smoke.sh`、`run-fdcan-medium-smoke.sh`、FDCAN classic/FD、扩展过滤器、
  dedicated Rx Buffer、BUS-OFF、RCC clock 和 machine smoke 均通过；QEMU 增量重链通过。
- [ ] 限制与下一道门：QEMU standard CAN bus 不提供 CAN-ID 仲裁、帧持续时间、bit stuffing、
  物理 ACK、error frame 或 frame timestamp；standard-bus ingress 的 FDCAN 接收时间戳使用
  当前 QEMU virtual clock。若需要可重复的虚拟帧时序，应在 standard bus 之上另做通用
  scheduler adapter，并先通过独立隔离测试。

## 2026-09-01 W25Q64 -> QEMU m25p80 迁移（已完成）

- [x] 所属层：QEMU 标准 SSI/NOR 器件到 STM32H723 OCTOSPI 芯片层的直接边界。producer
  是 `DmMc02Ospi` 的间接读写/擦除事务，boundary 是 `DmMc02SsiNor` 的 SSI bus、CS
  和字节传输适配，consumer 是 QEMU `w25q64`；m25p80 自己拥有 backing storage。
- [x] 新增 `dm_mc02_ssi_nor.[ch]`，在真实 `armv7m` QOM parent 下创建 SSI bus，实例化并
  校验 `w25q64` 的 8 MiB 几何和 `EF 40 17` JEDEC ID，提供 active-low CS、transfer、reset
  和只读 storage view。OCTOSPI 保留寄存器、DLR、DMA endpoint、memory-mapped alias 和
  persistence 生命周期，不再实现生产 NOR 命令/存储语义。
- [x] 为上游 QEMU v8.2.2 的标准 m25p80 补齐 W25Q 兼容边界：成功 page program/erase
  清除 WEL，4K/32K/64K erase 按包含块地址对齐；新增的窄 storage/geometry/ID accessor
  不暴露命令状态，也不复制 Flash 存储。
- [x] 旧 `DmNorFlash` 明确由 host CMake OSPI fixture 独占；生产 Meson ARM target
  不再链接 `cosim/dm_nor_flash.c`。QEMU 构建失败首先暴露为 SSI adapter 缺少
  `qapi/error.h`，补齐边界依赖后完成重链。
- [x] 隔离/直接门：QEMU `qemu-system-arm` 重链通过；`run-ospi-smoke.sh` 覆盖 JEDEC、
  WEL、page program、普通/quad read、4K/32K/64K/chip erase、非对齐擦除、DLR 边界、
  memory-mapped read、DMA 端点和 raw-image persistence；`run-board-profile-smoke.sh`
  通过；host CTest 串行 `54/54` 通过；最新独立 QEMU smoke suite `88/88` 通过。
- [ ] 限制与下一道门：m25p80 仍是同步 functional model，没有真实 Flash latency、
  async WIP、suspend/resume、ECC、block protection、掉电中止或完整 OCTOSPI line/DTR
  时序；上游本地 patch 需要在未来升级 QEMU 基线时重新审计。下一步进入 FDCAN -> QEMU
  standard CAN bus 迁移，仍需先做独立 adapter/differential gate。

# 2026-09-01 QEMU 构建配置隔离与 ADC fixture 收敛（已完成）

- [x] 所属层：STM32H723/DM-MC02 构建边界与 ADC qtest 直接 consumer fixture。ADC
  producer 是测试 guest 的 RCC/TIM2 配置，boundary 是 reset 后重新建立的 timer clock
  fixture，consumer 是 ADC accurate-timing compare-pulse oracle；没有修改 ADC/TIM 生产模型。
- [x] 修复首个错误：`test_tim2_compare_pulse_only_cc1()` 在 `system_reset` 后没有恢复
  `RCC_D1CFGR/RCC_D2CFGR`，TIM2 从 fixture 的 32 MHz 回到复位时钟，导致 CCR1 compare
  边界提前触发。补回 `configure_timer_fixture_clock()` 后精确时间断言恢复。
- [x] Release 构建由 `tools/build-qemu.sh` 完成并核对为 `arm-softmmu='dm-mc02'`、
  `CONFIG_DM_MC02=y`、`CONFIG_STM32H723=y`、`CONFIG_STM32H723_USB_HOST=y`。
- [x] 新增 `tools/build-qemu-generic.sh`，使用独立 `build/qemu-generic` 和
  `--without-default-devices`；其生成设备配置和构建对象不含 DM-MC02、STM32H723 或
  co-sim 专用内容，`-machine help` 仅显示通用 ARM 机器。
- [x] 隔离/边界门：ADC qtest `40/40`、串行 host CTest `54/54`（含 QEMU smoke
  suite `86/86`）、Release ARM QEMU 构建和 generic ARM QEMU 构建均通过；未修改
  `trobot/`。
- [ ] 限制与下一道门：generic build 只证明构建依赖隔离，不提供 DM-MC02 行为；QEMU
  上游 `vhost-shadow-virtqueue.c` 的既有未初始化 warning 仍存在。下一步进入 W25Q64
  die -> QEMU `m25p80` 迁移，先补隔离 adapter/differential gate，再接 DM-MC02 profile。

# 2026-09-01 Reusable USART RX wire timing and IDLE boundary（已完成）

- [x] 所属层：可复用 UART 器件数据面到 DM-MC02 UART 的直接边界。producer 是 host
  chardev byte stream；boundary 是 UART 自己的有界 wire queue、虚拟帧时钟和 CPU-visible
  RX FIFO；consumer 是 polling `RDR` 或 DMA RX endpoint。板级路由和 DMA 地址策略没有
  下沉到通用 UART。
- [x] 有效 `BRR/PRESC/OVER8` 配置下，输入先进入独立 256-byte wire queue，每个默认
  8N1 frame 交付一个字节到 256-byte RX FIFO；`RXNE/RDR` 和 DMA 只观察/消费已交付的
  FIFO 字节。无效时序配置保留立即兼容路径，队列满时丢弃最新字节并累计计数。
- [x] 最后一个字节交付后再空闲一个 frame duration 才置 `IDLE`；host chunk 不再被当作
  字符边界，新输入取消旧 idle deadline，`IDLE` 仍保持到 `ICR.IDLECF`。reset、chardev
  close 和 transceiver 掉电会清理 RX queue 与两个 RX timers；RCC/baud 变化会重新计算
  时序并保留正在等待的 idle deadline。
- [x] 隔离/边界门：新增 `tools/run-uart-rx-timing-smoke.sh`，用 qtest `clock_step`
  精确验证未到 frame 不交付、连续 `BC` 按帧交付以及最后字节后的 IDLE；UART polling、
  IDLE、UART1/UART2 DMA endpoint on/off 与 TX timing 回归均通过。未修改 `trobot/`。
- [x] 验证：QEMU 增量重链通过；RX timing、UART polling、UART IDLE、UART1/UART2 DMA
  两种 endpoint 路径和 TX timing smoke 全部通过。
- [ ] 限制与下一道门：当前仍使用默认 8N1 frame，未建模 parity/stop-bit、oversampling
  采样、LIN/Smartcard/IrDA、FIFO overrun/error flags 或物理线路电平。下一步应选择
  一个独立 UART 配置语义切片或进入其它直接 kernel-clock consumer，不在 UI/外部 plant
  层补 UART 时序。

# 2026-09-01 RCC unclocked state -> APB timer consumer（已完成）

- [x] 所属层：STM32H723 RCC 到 DM-MC02 直接 timer consumer 的板级边界。producer 是
  RCC 当前 effective source 的 readiness；boundary 是 `dm_mc02_clock_changed()` 对
  APB1/APB2 timer QEMU clocks 的派生和传播；consumer 是 TIM2/TIM1/TIM8 等直接使用
  对应 clock object 的定时器。
- [x] 修复 `dm_mc02_clock_changed()` 在派生频率为 `0 Hz` 时保留旧 APB timer 频率的
  缺陷：APB1/APB2 现在无条件执行 `clock_set_hz()`，并同步更新只读诊断字段。有效源
  失效时不伪造 fallback 频率，也不把 readiness 判断下沉到 timer 热路径。
- [x] 隔离/边界门：`run-clock-zero-smoke.sh` 请求未就绪 PLL1 后关闭当前 HSI，确认
  RCC request 仍可见且 APB1 timer clock 为 `0 Hz`；该脚本已自动纳入全量 QEMU smoke。
  `run-pwr-rcc-smoke.sh`、`run-tim2-clock-smoke.sh` 继续通过。
- [x] 验证：QEMU 增量重链通过，窄 zero-clock smoke 通过，host CTest 串行 `53/53`，
  独立 QEMU smoke suite `85/85`；未修改 `trobot/`，Luna 已复核并关闭。
- [ ] 限制与下一道门：仍未实现 oscillator/PLL settling、CSS、自动 fallback、clock
  gate 和完整 H723 kernel-clock matrix；下一步应继续选择一个直接 kernel-clock
  consumer，保持每层独立测试后再接入上层。

# 2026-09-01 TIM2 clock fixture readiness correction

- [x] 失败首状态定位为测试 guest 的 RCC producer 配置缺失：`run-tim2-clock-smoke.sh`
  只打开 `PLL1ON`，没有打开 `PLL1DIVPEN`，而 H723 RCC 正确拒绝未使能的 PLL1P
  输出，导致 TIM2 继续使用复位 HSI。补齐 `RCC_PLLCFGR.PLL1DIVPEN` 后，测试才真正
  覆盖 HSI -> PLL1 的动态 TIM2 时钟切换；未修改定时器实现或 `trobot/`。
- [x] 验证门：`run-tim2-clock-smoke.sh` 通过，首次 PLL1 切换后的更新延迟为
  `113.906 ms`；完整串行 host CTest `53/53` 通过，独立 QEMU smoke suite `84/84`
  通过。未修改 `trobot/`。

# 2026-09-01 H723 D1 HPRE/HCLK -> ADC synchronous clock slice（已完成）

- [x] 所属层：板卡无关的 STM32H7 D1 clock-tree helper 与 H723 RCC 芯片层。producer
  是 RCC 的有效 SYSCLK 和 `D1CFGR`；boundary 是 `dm_stm32h7_clock_tree` 的 CPU/HCLK
  派生接口及 `DmMc02PwrRcc` 的 `dm_mc02_pwr_rcc_hclk_hz()`；consumer 是 DM-MC02
  ADC12 common `CCR.CKMODE`。
- [x] 提取 `dm_stm32h7_clock_tree.[ch]`，独立实现 H723 `D1CPRE` 与 `HPRE` 编码，
  不依赖 QEMU、DM-MC02 pin map 或 ADC 状态。RCC 只计算有效 SYSCLK，并分别调用
  CPU/HCLK 派生；ADC `CKMODE=01/10/11` 现在使用 `HCLK/{1,2,4}`，不再错误推导
  为 `CPU/2`。
- [x] 隔离门：`dm_stm32h7_clock_tree_smoke` 覆盖所有 HPRE 有效编码，并验证
  D1CPRE 与 HPRE 独立。直接边界门：ADC qtest 覆盖 9 个 HPRE 编码与 3 个 CKMODE，
  以及运行中 HPRE 改变时保留当前 rank 的剩余虚拟时间。
- [x] 验证：纯 helper smoke 通过，QEMU machine 重编译成功，新增两个 ADC qtest 和
  完整 ADC qtest `40/40` 通过；最终 host CTest `53/53`、`run-pwr-rcc-smoke.sh`、
  `run-tim2-clock-smoke.sh` 均通过；未修改 `trobot/`，子代理已复核并关闭。
- [x] 收尾重建时发现三个 Flash smoke target 在 Release 的 `NDEBUG` 下会让 `assert`
  相关变量被优化掉并触发 `-Werror`；为 `dm_spi_nor_flash_smoke`、`dm_nor_flash_smoke`
  和 `dm_nor_flash_persistence_smoke` 补上 `-UNDEBUG`，重新构建和 CTest 均通过。
- [ ] 限制与下一道门：仍未建模完整 APB/D2/D3 分频、kernel source 全矩阵、clock
  security、CSS、PLL/oscillator settling 或低功耗域；下一步应选择一个直接 kernel-clock
  consumer 单独补齐，不把完整 clock tree 塞入 board profile。

# 2026-09-01 H723 RCC source readiness and SW/SWS boundary（已完成）

- [x] 所属层：STM32H723 SoC 时钟层。producer 是 RCC 的振荡器/PLL enable、输入源和
  分频寄存器；boundary 是 `RCC_CFGR.SW/SWS` 的请求与实际生效源契约；consumer 是
  ARMv7-M CPU clock 以及 DM-MC02 的 TIM/ADC clock callback。
- [x] `SW` 现在只保存 guest 请求，RCC 内部单独保存 effective system source；`SWS`
  只报告当前生效源。未就绪 HSI/CSI/HSE/PLL1 请求不会改变 CPU、TIM 或 ADC 下游时钟，
  源变为 ready 后会重新尝试已保存请求。
- [x] PLL ready/output 现在要求 enable、有效 PLL 输入源和非零分频配置；无效 PLL 不会
  伪造 `PLLRDY` 或驱动系统时钟。配置写入仍在寄存器冷路径处理，不增加 timer/ADC 热路径
  解析。
- [x] 最小隔离/边界门：PWR/RCC bare-metal smoke 覆盖未就绪 HSE、HSE 延迟切换、无效
  PLL 和补齐配置后的自动切换；TIM2 动态时钟、ADC trigger、整机 smoke 保持通过；未修改
  `trobot/`。
- [ ] 限制与下一道门：源就绪仍采用立即生效模型，没有启动稳定时间、CSS、PLL lock
  延迟、完整 H7 clock mux/低功耗/备份域语义；下一步应在保持本边界的前提下补其它直接
  kernel-clock consumers 或独立的 virtual-time oscillator settling，而不是在 board/UI
  层修正时钟。

# 2026-09-01 H723 ADC/TIM3 OC4REF trigger slice（已完成）

- [x] 所属层：STM32H723 ADC 芯片层与 DM-MC02 -> trigger bus 的直接边界。producer
  是 TIM3 `OC4REF` master event；boundary 是 board route 按 master-event kind 发布
  `TIM3_CH4` source；consumer 是 ADC regular `EXTSEL=15` 或 injected `JEXTSEL=4`。
- [x] 保持 source identifier 与 ADC 转换逻辑板卡无关；board profile 只提供 event-to-source
  数据，旧的 update route 继续走兼容 fallback。当前切片不引入其它 timer、EXTI、LPTIM
  或 HRTIM producer。
- [x] 隔离门：ADC source matching 与 injected 映射必须分别覆盖；直接边界门：真实 TIM3
  `MMS=7` 的 OC4REF rising event 触发 regular/injected conversion，并确认错误 source
  不触发。下一道门是其它已有 timer channel 的同类映射。
- [x] 验证：ADC qtest `38/38`、TIM qtest `21/21`、现有 ADC trigger ARM smoke 的
  11 个模式（含新增 TIM3_CH4 mode 10）全部通过；QEMU 已重链，未修改 `trobot/`。
- [ ] 限制：当前只把 TIM3_CH4 接入 DM-MC02/EVAL 的 `TRGO` event-specific route；其它
  H723 `EXTSEL/JEXTSEL` source 仍需各自产生者、board route 和边界测试，不能把 0..31
  保留编码默认为可用 trigger。

# 2026-09-01 H723 DMA M2P FIFO burst slice（已完成）

- [x] 所属层：可复用 STM32H723 DMA 芯片层。producer 是 DMAMUX peripheral request
  与 `SxCR.MBURST/PBURST` 配置；boundary 是 FIFO 内存预取/外设提交的 burst 分组以及
  `NDTR`、地址、HT/TC 和错误状态；consumer 是 UART/USART 的板卡无关 DMA endpoint。
- [x] 本切片限定为 `M2P + FIFO`，支持有效 `SINGLE/INCR4/INCR8/INCR16` 编码；
  `MBURST` 限制每次 FIFO 内存预取中的连续 memory beat 分组，`PBURST` 保持外设请求
  的逻辑分组契约。每个 UART TX request 仍只提交一个 byte beat，避免把 UART 的数据
  寄存器语义塞进 DMA；burst 不模拟逐周期总线占用。
- [x] 隔离门：固定 endpoint fixture 覆盖 `MBURST/PBURST` 的 16 种有效组合，验证
  16-bit memory 到 8-bit endpoint 的字节顺序、单 beat callback、FIFO 清空、`NDTR=0`
  和 `TCIF`；既有 DBM/HT/地址/timestamp 断言继续通过。
- [x] 直接集成门：真实 ARM guest 的 UART1/USART2 DMA TX smoke 在 endpoint `on/off`
  两种路径下均显式使用 `MBURST=INCR4`、`PBURST=SINGLE`，保持输出字节、DMA 状态和
  UART `TDR` side effect 正确；未修改 `trobot/`。
- [x] 验证：DMA FIFO host fixture、ASan fixture、DMA/FIFO/batch/仲裁/DBM/TIM8/SPI2
  回归及 UART1/USART2 四个 guest smoke 均通过，QEMU 已重编译；完整 host CTest
  `45/45` 通过。
- [ ] 限制：本切片不宣称真实 DMA 总线时钟、MBURST/PBURST 仲裁、FIFO FEIF 触发条件或
  硬件 burst 延迟；这些仍需后续芯片层切片和独立证据。

# 2026-09-01 H723 USB host-channel multi-packet PIO/DMA slice（已完成）

- [x] 所属层：可复用 STM32H723 USB host-controller 芯片层。producer 是 guest 对
  `HCCHAR/HCTSIZ/HCDMA/HCFIFO` 的编程及每个 accepted packet 的 completion；boundary 是
  host channel 的 `PKTCNT/XFERSIZE` 生命周期、私有 `next_pid` 和 PIO/DMA data-path；consumer
  是板卡无关的 host-channel transport。
- [x] `HCCHAR.CHENA` 上升沿从 `HCTSIZ.DPID` 初始化私有 DATA toggle；每个 accepted packet
  切换 `DATA0/DATA1`，而 `HCTSIZ.DPID` 保持本次 channel operation 的初始编程值。PIO 由
  `HCFIFO` 提供 packet bytes，DMA 由 `GAHBCFG.DMAEN` 和当前 `HCDMA` 提供 packet bytes；
  两条路径共享相同的 packet completion、短包停止和剩余 transfer state 语义。
- [x] 隔离和边界覆盖 PIO 与 DMA 的连续 `64+64+2` OUT、accepted short packet 立即停止，
  并断言 DATA PID、`HCTSIZ`、`HCDMA`、FIFO 内容和最终 `HCINT` 状态。测试 fixture 的
  submit callback 使用 `TransportFixture`，PIO callbacks 使用 host 对象，已修正 opaque
  ownership 不一致。
- [x] 已有验证记录：host-controller unit `16/16`，host transport unit `14/14`，data-path
  `3/3`，channel-control `4/4`，completion scheduler `6/6`，QEMU adapter `13/13`，H723
  qtest `4/4`；此前相关 ARM guest smoke 通过。未修改 `trobot/`。
- [ ] 限制与下一道门：当前仍不是完整 USB bus/PHY 模型，没有真实 SOF 驱动的 retry/timeout、
  完整 DMA/FIFO arbitration、descriptor DMA、ISO/split、hub/topology、宿主机 passthrough
  或 DM-MC02 Host board wiring。下一步应在同一 H723 芯片层先定义并测试 retry/SOF 与
  DMA/FIFO 边界，再向 host-role board profile 集成。

# 2026-09-01 QEMU 构建、回归与实时性基线（已完成）

- [x] 当前 `build/qemu/qemu-system-arm` 已从 host-channel 最新源码重建成功；窄验证包括
  H723 host controller `16/16`、host transport `14/14`、data-path `3/3`、channel-control
  `4/4`、completion scheduler `6/6`、QEMU adapter `13/13`，项目 CTest 串行 `44/44`。
- [x] Release `trobot.elf` 从复位运行的 1 秒基线为 `1.000165x` RTF（`1002` FreeRTOS
  ticks、宿主采样 `1.001834 s`），QEMU CPU 约 `101.8%`、RSS 约 `50 MiB`，IWDG
  `timeouts=0`；NullEngine 吞吐约 `199884 IMU frames/s`，DM-MC02 startup smoke
  约 `50.021 ms`。
- [x] 同一固件的 TCG `thread=multi` 为 `0.999815x`、`thread=single` 为 `0.998829x`，
  均约占用一个宿主 CPU；当前路径没有可观测的多核加速收益，仍以单 vCPU/event loop
  为主要性能边界。
- [ ] `perf stat` 受当前主机 `perf_event_paranoid=4` 拒绝，硬件 cycles/cache counters
  尚未采集；不影响已完成的 wall-clock/`/proc` 基线，但后续 profiler 仍需具备相应主机
  权限。未修改 `trobot/`。

# 2026-09-01 H723 host SOF periodic scheduling slice（已完成）

- [x] 所属层：可复用 STM32H723/DWC2 host-controller 芯片层。SOF producer 是 host
  virtual-time frame counter，boundary 是 `HFNUM` 帧奇偶与 `HCCHAR.ODDFRM`，consumer
  是 host-channel service callback；不依赖 DM-MC02 board wiring 或外部 plant。
- [x] SOF 路径对 ISO/interrupt（`EPTYPE=1/3`）按 `ODDFRM` 只在匹配奇偶帧 service；
  control/bulk 不受该门控并保留 control/bulk NAK 的自动下一 SOF retry。CHENA 上升沿的
  首包和直接 `service_channel()` API 保持立即/调用者驱动语义。
- [x] periodic NAK 恢复为 `NAK|CHHLTD`，不推进 PID、`HCTSIZ` 或 DMA；软件重新使能
  channel 后才能再次提交。纯虚拟时间测试覆盖 odd/even SOF、多包延续和 periodic NAK
  halt，H723 host-controller unit 当前 `18/18`。
- [ ] 限制与下一道门：当前没有 per-endpoint interval、完整 microframe bandwidth/global
  FIFO arbitration、split/ISO payload、descriptor DMA、PHY 或 electrical timing；下一步
  仍应在 H723 芯片层处理 DMA/FIFO boundary，再进入 host-role board composition。

# 2026-09-01 H723 async PIO completion/cancel adapter（已完成）

- [x] 所属层：可复用 STM32H7 host-controller adapter。producer 是 H723 PIO packet
  start，boundary 是 `DmUsbHostChannelCompletion` 的固定容量 pending/terminal record
  和 H723 `HCCHAR/HCTSIZ/HCINT/HCFIFO` 观察，consumer 是 caller 驱动的 IRQ/event-loop。
  该层不依赖 DM-MC02 board wiring、QEMU event API、线程或 wall-clock。
- [x] completion record 使用 opaque fixed storage；`begin()` 保持 channel lease，并为每次
  成功开始递增独立于 allocator channel generation 的 token generation（跳过 0）。旧 token
  即使新 operation 迁移到另一个 channel 也会被拒绝；active lease 只能拷贝到 caller storage，
  不暴露可写内部状态。
- [x] 新增 H723 async PIO adapter：`start()` 校验并编程一个 packet 后返回 `STARTED`，资源
  忙时返回 `DEFERRED`；`poll()` 无终端 controller event 时返回 `PENDING`，检测 `XFRC/NAK/
  STALL/XACTERR` 后提交 completion；`cancel()` 先停 channel 再提交取消。IN buffer 在 terminal
  event 前必须保持有效，async 对象和 allocator 也必须保持稳定地址。
- [x] 隔离门：`dm_usb_host_channel_completion_smoke` 增加跨 channel migration stale-token
  回归；`dm_stm32h7_usb_host_pipe_async_smoke` 覆盖 start、lease retention、pending poll、
  accepted length/FIFO read、NAK、cancel、IN buffer validation 和 stale token。
- [x] 集成门：host CTest `41/41` 通过；两个 `stm32h723-usb-host` ARM/QEMU guest smoke
  通过，修复了两个 freestanding guest script 漏链 `dm_usb_host_channel_completion.c` 以及
  completion 大结构体隐式依赖 libc `memset` 的问题；未修改 `trobot/`。
- [ ] 限制与下一道门：这是 caller-driven 单 packet PIO adapter，不是完整 IRQ handler；仍没有
  IRQ locking/callback dispatch、HCDMA/FIFO arbitration、SOF retry scheduler、ISO、多设备
  topology、PHY/VBUS、passthrough 或 DM-MC02 Host board wiring。下一步应先定义 H723 IRQ
  locking/event dispatch 边界，再扩大到 DMA/FIFO 或 host-role board 组合。

# 2026-08-31 DWC2 -> DM-MC02 -> NVIC 边界接线（已完成）

- [x] 所属层：DM-MC02 板级组合层。producer 是可复用 `DmUsbDwc2Device` 的全局
  device-mode IRQ level；boundary 是 `dm_mc02_usb_set_irq()` 和 board profile 的
  `irqs.usb`；consumer 是 ARMv7M/NVIC external IRQ input。DWC2 core 仍不依赖板级
  pin map，板级只负责地址和 IRQ 组合。
- [x] DM-MC02 USB adapter 已将 DWC2 register/FIFO window 转发到通用 core，USB
  IRQ 77 已由 DM-MC02 profile 接入 ARMv7M/NVIC；`SET_ADDRESS` 同步更新 core 的
  `DCFG`，EP1..EP15 不再保留旧的重复 endpoint data path。
- [x] 保持 legacy controller-ready 和 FIFO0 CDC 兼容：通用 core 的同步 `CSFTRST`
  会报告 `CSRSTDONE`，adapter 读取 `GINTSTS` 时合并 raw FIFO0 的 `RXFLVL`，不把
  旧 byte pipe 混入通用 endpoint 数据面。
- [x] 直接边界测试 `/dm-mc02/usb/irq-reaches-nvic` 验证 `GINTSTS.OEPINT`、NVIC
  `ISPR2` bit 13、清除 endpoint interrupt 和 `ICPR2`；之前失败的测试是读取了
  `0xE000E108`（`ISER2`），已修正为 NVIC `ISPR2 = 0xE000E208`。
- [x] 验证：`dm-mc02-usb-test` `9/9`；DWC2/control/transaction/host 隔离测试
  分别为 `5/5`、`5/5`、`5/5`、`4/4`；controller-ready 与 FIFO0 CDC smoke 通过，
  host CTest `22/22`；未修改 `trobot/`。
- [ ] 限制与下一道门：仍没有 DMA 地址搬运、SOF/RXFLVL、PHY、电气时序、QEMU USB
  bus attachment、宿主机枚举或异步 completion/cancel；下一步应把已验证的
  transaction/host callback 接到明确的 transport adapter，再做 USB bus 集成。

# 当前有效基线（2026-08-31）

本文件下方的条目按时间追加，属于各次切片完成时的历史记录；其中的测试数量和
“当前仍待实现”只描述当时状态，不覆盖后续条目。以下内容是继续开发时应采用的
当前基线。

- 分层边界：`STM32H723` 芯片模型 -> `DM-MC02` board profile/组合 -> 可复用器件与
  驱动接口 -> 外部 plant/backend -> CLI/工具。上层不得绕过下层私有状态补行为。
- 已形成的芯片/板级基础包括 CPU/时钟/复位、GPIO/EXTI、DMA/DMAMUX、Timer、ADC
  regular/injected 基础数据面、SPI/BMI088、UART/RS485、FDCAN、Flash、RNG、CRC、
  CORDIC、OCTOSPI、电源和 USB controller-ready 窗口；USB 另有板卡无关的最小
  DWC2 device-mode FIFO/endpoint/IRQ 核心，已接入 DM-MC02 地址映射和 IRQ 77/NVIC。
- 已形成的工具边界包括 QEMU-native co-sim、v1/v2 frame codec、MuJoCo/ROS2/Null
  worker 和 SocketCAN bridge；当前 QEMU 主路径不提供 Web 面板。
- 当前验证基线：QEMU ADC qtest `37/37`，TIM qtest `19/19`，USB qtest `9/9`，
  USB control/transaction unit `6/6`、`5/5`，host CTest `36/36`，Python `255 passed`；整机
  QEMU smoke 当前为 `79/79`，由 smoke suite 串行执行。
- 当前明确未完成：ADC 完整触发矩阵，完整 USB 枚举和端点，DMA FIFO 的完整
  总线时序、silicon FEIF/burst 语义，完整 CAN 物理层，严格跨进程锁步与混合 STEP 事务，以及具体
  Gazebo 机器人模型联调。这些必须继续从所属底层切片并通过边界测试后再向上接入。
- 本轮已完成的芯片层切片：regular `DISCEN/DISCNUM` 和 injected `JDISCEN` 外部触发
  subgroup；ADC qtest 覆盖两组 rank 游标、regular/injected 软件完整 sequence、忙时不
  排队和 `AUTDLY` 组合边界。当前 qtest 计数以实际构建后的 TAP 输出为准。
- 后续每个切片都要补充所属层、producer/boundary/consumer 契约、最小隔离测试和
  下一道集成门；若历史条目与当前代码冲突，以当前源码、接口文档和本节验证结果为准。

## 2026-08-31 STM32H7 OTG host-port 生命周期芯片层切片（已完成）

- [x] 所属层：STM32H723 USB host-controller 层。producer 是 guest 对 `HPRT0` 的
  reset assert/deassert，或外部 transport 的 connect/disconnect；boundary 是
  `DmStm32H7OtgHost` 内嵌的板卡无关 `DmUsbHostPort`；consumer 是 USB device 的
  bus-reset callback。模型不包含 DM-MC02 地址、pin map、NVIC、QEMU USB bus 或 PHY。
- [x] 以真实 DWC2 `0x400/0x440` host-register layout 实现 `GAHBCFG`、
  `GINTSTS/GINTMSK`、`HCFG`、`HFIR` 与单端口 `HPRT0`。port change W1C、global
  interrupt gate、PWR、connect speed 及 reset release 后的 `ENA/ENACHG` 都在芯片层
  状态机内完成，不由 device core 或 board adapter 伪造。
- [x] 最小隔离门：`test-dm-stm32h7-otg-host` `5/5`，精确验证 defaults、connect
  interrupt/W1C、RST edge、虚拟 timestamp 和空 port。直接消费者门把 callback 接到
  `dm_usb_dwc2_bus_reset()`，验证 address/configuration、`DCFG.DAD` 和 endpoint
  runtime state 清理；不是 `GRSTCTL.CSFTRST` core reset。
- [x] 兼容回归：重链 `qemu-system-arm`；DM-MC02 USB qtest `9/9`；controller-ready
  与 CDC byte-pipe bare-metal smoke 通过；完整 QEMU smoke suite `79/79` 通过。未修改
  `trobot/`；完成只读固件核对后已关闭 Luna 子代理。
- [ ] 下一道门：先从同一芯片层单独实现一个最小 host-channel transaction producer
  （`HCCHAR/HCTSIZ/HCINT`）及其隔离测试，再考虑 SOF。只有通过该门后，才为一个明确
  的 host-role board profile 添加 machine 组合；当前 DM-MC02 profile 保持 USB Device
  mode，不能声称 USB host、枚举、PHY、OTG 或 passthrough 支持。

## 2026-08-31 USB host-port lifecycle 分层切片（已完成）

- [x] 所属层：可复用 USB transport/lifecycle 层。producer 是 host controller 或
  generic transport 发出的 port reset；boundary 是板卡、QEMU、DWC2 无关的
  `DmUsbHostPortReset` callback；consumer 是未来的 USB device bus-reset handler。
  `DmUsbHost` 继续只生产 SETUP/IN/OUT transaction，不拥有连接或 port 生命周期。
- [x] 新增 `DmUsbHostPort`：它只保存 callback 和 opaque，并同步透传调用者给出的虚拟
  时间戳；没有 callback 或空 port 是显式 no-op，不创建 token、结果或隐式时间推进。
- [x] 最小隔离门：`test-dm-usb-host-port` `2/2` 精确覆盖 callback 次数、opaque、时间戳
  以及 no-op；相邻回归为 host `4/4`、DWC2 `5/5`、QEMU adapter `7/7`，并已重建
  `qemu-system-arm`。未修改 `trobot/`。
- [x] 直接消费者门：单独定义 `DmUsbDwc2Device` 的 bus reset。它清除 control
  transfer、地址/配置、PID/halt 和待处理端点数据，同时明确哪些 DWC2 MMIO 状态保留；
  不得把它别名为 `GRSTCTL.CSFTRST` core reset，也不得在这一层加入 host 枚举或 DM-MC02
  machine 组合。（已由下列切片完成。）

## 2026-08-31 DWC2 bus-reset boundary 与 DCFG.DAD 修正（已完成）

- [x] 所属层：可复用 USB control/device/transport 边界。producer 是 QEMU 或未来 host
  controller 的带虚拟时间的 port reset；control boundary 是
  `dm_usb_control_bus_reset()`；device consumer 是 `dm_usb_dwc2_bus_reset()`；QEMU
  adapter 只把 QEMU virtual clock 事件交给 `DmUsbHostPort`，不依赖 DM-MC02 pin map。
- [x] 修复首个底层寄存器偏差：真实 DWC2 `DCFG.DAD` 是 bit `4..10`，不再把地址错误
  写入 bit `0..6`。DM-MC02 `SET_ADDRESS` qtest 直接验证 bit field；该修正依据实际
  `trobot` DWC2 driver 的 `addr << 4` 写法，但没有修改固件。
- [x] control bus reset 清空进行中的 transfer、address/configuration 并通知 consumer。
  DWC2 bus reset 清空 DAD、PID/halt、FIFO、transfer size、endpoint interrupt、`EPENA`
  与 `STALL`；保留 DCFG 非 DAD 位、global config/mask、FIFO sizing、DMA 地址与 endpoint
  `USBAEP`/MPS/type。它与 `GRSTCTL.CSFTRST` core reset 保持不同的公共语义。
- [x] QEMU adapter 的 real-`USBBus` reset 使用 `DmUsbHostPort` 同步传递 QEMU virtual
  timestamp，并在 DWC2 fixture 调用 `dm_usb_dwc2_bus_reset()`；没有实例化 DM-MC02 host
  controller 或宣称 USB passthrough。
- [x] 验证：host-port/control/transaction/host/DWC2/adapter unit 为
  `2/2`、`6/6`、`5/5`、`4/4`、`6/6`、`7/7`；DM-MC02 USB qtest `9/9`；controller-ready
  与 CDC pipe smoke 通过；系统重链；host CTest `22/22`，整机 QEMU smoke `79/79`。
- [ ] 下一道门：若要实现板级 USB host，先实现 H723 host-controller 的 port state、SOF 和
  reset event producer，再接此公共 port boundary；当前 machine 仍没有 host controller、
  枚举、async completion/cancel、DMA、PHY、OTG 或宿主机 USB passthrough。

## 2026-08-31 synthetic upstream host 分层切片（已完成）

- [x] 所属层：可复用 USB transport/producer 层。producer 是 host 侧控制传输或
  bulk 调度；boundary 是 `DmUsbHostSubmitTransaction` 回调和 `DmUsbHostResult`，
  consumer 是任意实现 `DmUsbTransaction` 提交契约的 device/transport。host 不依赖
  QEMU USB bus、QOM、DM-MC02 寄存器、板级队列或 PHY。
- [x] 新增 `DmUsbHost`：控制传输按 setup、DATA、status 阶段推进，按 EP0 MPS
  分包，支持 control IN/OUT、无 data-stage 请求、累计长度和事务计数；bulk IN/OUT
  按调用者提供的 MPS 分包，并把 NAK/STALL/INVALID 原样返回给调用者，未在 host 内
  使用 wall-clock 重试或隐藏设备背压。
- [x] 最小隔离门：`test-dm-usb-host` `4/4`，覆盖 descriptor control IN、control
  OUT 分包、无 data-stage `SET_ADDRESS`、bulk NAK 后 retry、时间戳传递和参数边界。
  直接边界门继续为 transaction unit `5/5` 与 DM-MC02 USB qtest `8/8`。
- [ ] 限制与下一道门：当前 host 是同步的进程内 producer，没有 USB bus 拓扑、SOF、
  地址/枚举调度、async completion/cancel、真实 DWC2 FIFO/DMA/IRQ、PHY 或宿主机
  枚举；下一步应把同一 host submit callback 接到明确的 transport adapter，再单独
  实现 DWC2 device-mode 状态机，不能把私有 MMIO harness 当成 USB 总线。

## 2026-08-31 USB transaction/token 分层切片（已完成）

- [x] 所属层：可复用 USB 器件/驱动层。producer 是带虚拟时间戳的 SETUP/IN/OUT
  transaction；boundary 是不依赖 QEMU bus、QOM、板级寄存器或 PHY 的
  `DmUsbTransactionDevice`；consumer 是 `DmUsbControlDevice` 和 endpoint IN/OUT
  callback。
- [x] 新增 transaction dispatcher：端点配置、方向、MPS、`PID_AUTO/DATA0/DATA1`、
  每方向 toggle、NAK、STALL、halt/clear-halt、reset 和精确 `actual_length`；EP0
  复用 control core，成功 setup 后从 DATA1 开始，callback 保留 producer timestamp。
- [x] DM-MC02 packet harness 已通过 dispatcher 接入 EP0 和 EP1..EP5；空 IN 返回
  NAK，不再伪造成功 zero-length 或错误置 endpoint completion interrupt；原有 64-byte
  分包和 FIFO 行为保持。
- [x] 隔离门：`test-dm-usb-transaction` `5/5`；直接边界门：DM-MC02 USB qtest
  `8/8`；`run-usb-smoke.sh` 和 `run-usb-pipe-smoke.sh` 通过；未修改 `trobot/`。
- [ ] 下一道门：把同一 transaction 接口适配到 synthetic upstream host，再实现
  DWC2 device-mode FIFO/DMA/IRQ 状态机；最后才接 QEMU USB bus、真实 PHY 和宿主机枚举。

## 2026-08-31 USB EP0 control-transfer core（已完成）

- [x] 所属层：可复用 USB 器件/驱动层。producer 是控制器的 setup、IN、OUT packet；
  boundary 是 `hw/usb/dm_usb_control.[ch]`；consumer 是 DM-MC02 USB adapter 的
  EP0 和 CDC class callbacks。接口不依赖 QEMU USB bus、DM-MC02 wiring 或 PHY。
- [x] core 支持 `GET_STATUS`、`SET_ADDRESS`、`GET_DESCRIPTOR`、`GET_CONFIGURATION`、
  `SET_CONFIGURATION`，class callback，IN packetization/ZLP、OUT 数据累积和明确
  STALL。标准 address/configuration 仅在 status-IN 阶段提交。
- [x] `dm_mc02_usb.c` 不再重复解析 EP0 请求；descriptor、CDC line coding 和板级
  DCFG/configured 状态通过 callback 接入。现有 FIFO0 raw pipe、EP1..EP5 queue 和
  寄存器窗口保持不变。
- [x] 隔离门：`test-dm-usb-control --tap` `5/5`；直接边界门：
  `dm-mc02-usb-test --tap` `7/7`，覆盖 CDC `SET_LINE_CODING` 的 setup/data/status、
  3+4-byte OUT 分包和 board-visible STALL。
- [ ] 限制与下一道门：当前仍是 EP0 packet harness，不实现 token/PID/toggle、NAK、
  宿主机枚举、真实 QEMU USB bus attachment、DMA、PHY 或电气时序；下一步先定义
  USB transaction/state contract，再接真实 transport。

## 2026-08-31 shared v2 wire codec/section parity（已完成）

- [x] 所属层：协议中立的 co-sim framing 层。producer 是 host 或 QEMU link 的完整
  v2 body；boundary 是 `cosim/dm_mc02_v2_wire.[ch]`；consumer 是 QEMU v2 link、
  Python step protocol 及后续外部 worker。codec 不依赖 board profile、外设寄存器、
  plant 或 transport 队列。
- [x] 共享 36-byte v2 header、little-endian 编解码、kind/payload 长度限制、section
  payload header/iterator 和 compact 60-byte IMU discriminator。4-byte outer body
  length 仍由 stream transport 所有；typed payload 语义和 session 状态继续留在
  consumer 层。
- [x] 最小隔离门：`v2_wire_smoke`、`v2_wire_vectors` 和 Python golden-vector
  parity 覆盖 reset、session reset、compact IMU、单 section IMU、混合 ADC sections，
  验证 C/Python 对同一完整字节序列进行 decode/re-encode 后保持字节一致。
- [x] 直接边界门：QEMU v2 STEP、RESET、motor callback、STEP_DONE 和 full smoke
  suite 通过；host CTest `19/19`，Python `248 passed`，整机 QEMU smoke `79/79`。
- [x] 当前性能复测：NullEngine `194659 IMU frames/s`，worker 启动到 RESET
  `62.819 ms`，DM-MC02 启动 smoke `50.081 ms`；真实 Release 固件 RTF
  `1.000005x`、约 `1000.005 tick/s`、QEMU CPU `117.8%`、RSS `50316 KiB`，
  IWDG `timeouts=0`。
- [ ] 限制与下一道门：typed RESET/STEP ACK、telemetry 和 MOTOR_STATE payload
  仍由消费者各自校验；跨进程可靠传输、多步窗口、严格锁步和外部 plant 不属于本切片。

## 2026-08-31 shared v2 fixed payload codec（已完成）

- [x] 所属层：协议中立的 v2 typed response payload 层。producer 是 QEMU board/link
  状态快照；boundary 是 `cosim/dm_mc02_v2_payload.[ch]`；consumer 是 QEMU link、
  host validators 和后续 board/backend 工具。该层不依赖 QEMU timers、chardev、
  session validator 或 DM-MC02 pin map。
- [x] 抽取 RESET_ACK、STEP_ACK、DIAGNOSTICS、STEP_DONE 和 board TELEMETRY 的固定
  长度 little-endian encode/decode/validation；统一 status、capability、reserved、
  consumed/missing mask 约束，输出接口只返回完整 payload 长度或失败。
- [x] 最小隔离门：`dm_mc02_v2_payload_smoke` 覆盖五类 payload 的精确长度、字段 round
  trip、reserved/mask 拒绝；直接边界门覆盖 QEMU v2 STEP、RESET、motor callback、
  STEP_DONE、telemetry 和 backpressure smoke。
- [x] 验证：QEMU 重链、host CTest `20/20`、Python `248 passed`，v2 定向 QEMU smoke
  全部通过。
- [x] 增加 7 个固定 payload golden vectors；C/Python 都对完整 payload 做
  decode/re-encode 字节一致检查，并覆盖 RESET_ACK reserved 与 STEP_DONE mask 非法边界。
- [x] Python step protocol 增加对称的 RESET_ACK、STEP_ACK、DIAGNOSTICS、STEP_DONE
  encode API，向量测试不再复制固定 payload 的 struct packing。
- [ ] 限制与下一道门：IMU/MotorCommand/MotorState 等可变 section payload 仍由
  QEMU/Python consumer 分别验证；本切片不实现跨语言自动生成 schema、可靠传输、多步
  窗口或外部 plant 锁步。

## 2026-08-31 shared v2 ADC section payload（已完成）

- [x] 所属层：协议中立的 ADC input section 层。producer 是 host/plant 的 ADC 输入；
  boundary 是 `cosim/dm_mc02_v2_payload.[ch]`；consumer 是 QEMU co-sim link 和后续
  board/backend 工具。codec 不知道 ADC 实例、board pin map 或 QEMU queue。
- [x] 抽取 `ADC_INPUT`（channel/raw/reserved）和 `ADC_PIN_VOLTAGE`
  （channel/flags/voltage/reserved）的固定长度 encode/decode/validation，统一 channel
  范围、PIN_OVERRIDE flags、3.3 V 上限和保留字段语义。
- [x] 最小隔离门：`dm_mc02_v2_adc_payload_smoke` 覆盖边界值、round trip、reserved、
  flags 和电压范围；直接边界门覆盖 ADC-only、混合 STEP、v2 RESET 和 QEMU 重链。
- [x] 验证：host CTest `22/22`、Python `255 passed`，ADC-only/STEP/motor/reset v2
  smoke 通过。
- [ ] 限制与下一道门：IMU 与 MotorCommand/MotorState 仍是消费者侧变长 schema；完整
  ADC trigger matrix 和严格跨进程锁步不属于本切片。

## 2026-08-31 shared v1 wire codec 回归（已完成）

- [x] 共享 codec 接入后的直接边界回归：native co-sim link、虚拟时间调度和 QEMU
  external worker smoke 全部通过；host CTest `17/17`，Python `243 passed`。
- [x] 脚本和环境检查通过：`bash -n tools/*.sh`、`uv lock --check`、QEMU
  `git diff --check`；ROS 全局 pytest 插件缺少 `yaml` 时使用项目声明之外的
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 运行，项目自身依赖未被静默扩展。
- [x] 当前性能复测：NullEngine `195701 frame/s`，worker 启动到 RESET
  `64.083 ms`，DM-MC02 启动 smoke `49.733 ms`；真实 Release 固件 RTF
  `0.999700x`、约 `999.700 tick/s`、QEMU CPU `113.7%`、RSS `50304 KiB`，
  IWDG `timeouts=0`。
- [x] 下一道门已在后续 v2 framing/section parity 切片完成；typed payload validator、
  session state 和 transport ownership 仍保持在各自 consumer 层。

## 2026-08-31 shared v1 wire codec boundary（已完成）

- [x] 所属层：可复用 co-sim 协议层。producer 是 host transport 或 QEMU
  chardev 的完整 frame buffer；boundary 是 `cosim/dm_mc02_wire.[ch]`；consumer
  是 host API、QEMU link 和外部 worker。共享 codec 只依赖 C 标准库，不知道
  DM-MC02 pin map、外设或 transport 队列。
- [x] `dm_mc02_wire_encode/decode()` 统一 v1 header、little-endian 字段、精确
  payload 长度和 outer frame 之外的 body 布局；`dm_mc02_wire_payload_valid()`
  单独统一 IMU finite、reserved、ADC channel/voltage 语义校验，保留原 API 的
  “先编解码、后显式验证”时机。
- [x] 最小隔离门：`dm_mc02_wire_smoke` 只编译共享 C codec，覆盖 endian、header、
  精确长度、结构/语义校验分离、IMU finite 和 ADC 边界；直接边界门为 host
  protocol/transport smoke、QEMU-native co-sim
  link smoke、timing smoke 和 external worker smoke 均通过；QEMU 与 host 均编译
  同一 wire source。未改变 v2 step section 语义。
- [ ] 限制与下一道门：v2 header/section codec 仍在 QEMU link 与 Python 中分别
  实现；下一步应单独定义 v2 protocol-neutral codec，再通过 parity fixture 接入，
  不应在本切片中混入 plant 或 USB 总线行为。

## 2026-08-31 USB endpoint packet queue boundary（已完成）

- [x] 所属层：可复用 USB 器件/驱动层。producer 是控制器 adapter 的 endpoint
  packet 提交；boundary 是板卡无关的 `DmUsbEndpointQueue`；consumer 是 USB
  controller 的 endpoint FIFO/完成状态。queue 保留 packet 边界、方向、类型和
  virtual timestamp，不依赖 DM-MC02 pin map、USB bus 或 PHY。
- [x] 最小隔离门：固定容量 FIFO 覆盖空/满/超长/非法输入、FIFO 顺序、时间戳、
  IN/OUT 独立队列、reset 和 zero-length packet，单测为 `5/5`。
- [x] 直接边界门：EP1..EP5 adapter 使用 64-byte MPS 分包；RX 按 packet queue
  入队后由 endpoint FIFO 按边界消费，TX 通过 pending buffer 在 IN 请求时分包；
  qtest 覆盖描述符端点、EP1 RX 4 KiB 边界和 130-byte TX 的 `64/64/2` 分包，
  USB qtest 为 `5/5`，legacy DWC2/CDC 两个 smoke 均通过。
- [x] 构建门：queue 源码加入 QEMU USB system source，隔离单测加入 QEMU unit
  target；QEMU `qemu-system-arm` 和 USB qtest 成功重链，host CTest `16/16` 通过，
  `trobot/` 保持原有改动数量。
- [x] 错误边界：endpoint pending buffer 和 RX queue 拒绝的字节会累计到 USB
  状态的内部丢弃计数；chardev 初始化失败会释放 timer 和 endpoint queues。
- [ ] 限制与下一道门：这只是 packet storage/adapter 切片，仍不实现 USB token/data
  toggle、NAK/STALL、设备状态机的完整请求语义、DMA、PHY、总线仲裁或宿主机枚举；
  下一步应在 USB 芯片层独立定义 transaction/state contract，再连接真实 QEMU USB
  bus，而不是把当前私有 qtest window 扩展成宿主机设备接口。

## 2026-08-31 ADC low-power guest boundary（已完成）

- [x] 所属层：STM32H723 ADC 芯片层。Producer 是 guest 对 `CR.DEEPPWD`/
  `CR.ADVREGEN`/`CR.ADEN`/`CR.ADSTART` 的 MMIO 操作；boundary 是 ADC 芯片层的
  virtual-clock regulator-ready 状态；consumer 是 `CR`、`ISR.ADRDY/EOC/EOS`、
  `ADC_DR` 和最终 deep-power-down 状态。
- [x] 最小隔离门：ADC qtest 覆盖复位状态、清除 `DEEPPWD` 后的 regulator 启动、
  `ADEN/ADSTART` ready gate、10 us virtual-time 边界和重新进入 deep-power-down。
  当前 ADC qtest 为 `37/37`。
- [x] 直接集成门：`tools/run-adc-power-smoke.sh` 编译真实 ARM guest，通过 QMP
  启动 `-machine dm-mc02,adc-power-model=on`，精确检查 reset -> `ADRDY` ->
  `EOC/DR` -> `DEEPPWD` 的 MMIO 状态，脚本结束自动回收临时目录。
- [x] 验证：guest ADC power MMIO smoke 通过，guest 使用 `-Wall -Wextra -Werror`
  编译，shell 语法检查通过；未修改 `trobot/`。
- [ ] 限制与下一道门：模型不模拟模拟电源纹波、欠压曲线或 ADC enable 的模拟起振；
  其它 H723 外部触发源和完整 ADC trigger matrix 仍需在芯片层分别实现并测试，之后
  才接入更高层 board/worker 路径。

## 2026-08-31 DMA FIFO + DBM + endpoint 组合切片（已完成）

- [x] 所属层：STM32H723 DMA 芯片层。Producer 是 DMAMUX 选中的 peripheral request
  和 DMA 双缓冲/FIFO 状态机；boundary 是 `DmMc02DmaEndpoint` 的单 beat callback；
  consumer 是 endpoint 端的字节序列及 DMA 的 `NDTR/CT/M0AR/M1AR/HT/TC` 可见状态。
- [x] 最小隔离门：用独立 host fixture 同时启用 FIFO、DBM 和 endpoint，覆盖 M2P 与
  P2M、双缓冲切换、FIFO 暂存/宽度转换、endpoint 字节和 timestamp；不修改板级模型。
- [x] 验证：`cmake --build build/host -j2`、该 fixture 在 ASan/UBSan 下运行、host
  CTest `16/16`、`run-dma-fcr-smoke.sh`、`run-dma-dbm-reconfigure-smoke.sh` 和
  `run-tim8-dbm-smoke.sh` 均通过；QEMU 主体由 `tools/build-qemu.sh` 重编译。
- [x] 下一道集成门：组合状态机已经在芯片层闭合；UART/ADC/SPI 等现有消费者继续使用
  各自已验证的窄 endpoint 配置，不在本切片中强行扩大其 FIFO/DBM 组合。
- [ ] 后续仍需从芯片层独立实现或验证真实 `MBURST/PBURST`、总线仲裁、FEIF 触发、异步
  endpoint backpressure 和 per-beat rollback；在此之前不把该同步模型宣称为完整 DMA。

## 2026-08-31 DMA DBM inactive-buffer reconfiguration

- [x] 所属层：STM32H723 DMA 芯片层。Producer 是 guest 在 DBM/EN 运行期间写入非
  活动 `M0AR/M1AR`；boundary 只更新对应的 reload base，不改活动 buffer cursor、
  `NDTR`、`CT`、HT/TC 或 endpoint callback；consumer 继续只接收 DMA 已提交的
  peripheral beat。
- [x] 最小/直接边界门：`run-dma-dbm-reconfigure-smoke.sh` 使用通用 DMA DBM
  peripheral-request 数据面和 TIM8 作为唯一请求时钟，覆盖 M0/M1 -> 新 inactive
  地址切换，精确断言配置基址、`CT`、`NDTR`、HT/TC 和新 buffer 首个 beat；既有
  `run-tim8-dbm-smoke.sh` 继续覆盖固定双缓冲顺序。
- [x] FIFO + DBM 的 endpoint 暂存字节边界已由本节 host fixture 覆盖；真实板级外设的
  异步 backpressure 和 per-beat rollback 仍未实现。
- [ ] 本切片不实现 `MBURST/PBURST`、真实总线仲裁、FEIF 产生条件或异步 endpoint
  backpressure；这些保持独立 DMA 芯片层任务。

## 2026-08-31 UART DMA endpoint integration boundary

- [x] UART1、USART2 的 DMA RX/TX 已接入可复用 `DmMc02DmaEndpoint`；默认由
  `uart-dma-endpoint=on` 使用同步、direct、8-bit endpoint，`off` 显式保留旧的
  MMIO 兼容路径。
- [x] UART endpoint 只负责 `RDR` 消费和 `TDR` 写入副作用；DMA 继续负责 DMAMUX
  请求路由、stream 仲裁、地址/NDTR、HT/TC、TE 和 IRQ，UART TX 使用有界 batch。
- [x] 保留 `dm_mc02_dma_endpoint_smoke` 作为板卡无关隔离契约测试；UART1/USART2
  的真实 guest/DMA 边界由 `run-uart-dma-smoke.sh on|off` 和
  `run-uart2-dma-smoke.sh on|off` 覆盖。没有为 DMA engine 强行链接 QEMU 私有静态库。
- [x] 同步 endpoint 已接入 DMA FIFO；FIFO 的 packing/unpacking、阈值、NDTR/地址和
  HT/TC 仍由 DMA 芯片层负责，UART callback 只收到一个外设宽度的 beat。UART1/USART2
  的真实 guest/DMA 边界由 `run-uart-dma-smoke.sh on|off` 和
  `run-uart2-dma-smoke.sh on|off` 覆盖。
- [ ] endpoint 异步 backpressure、per-beat rollback 和完整 `ReceiveToIdle_DMA`
  event-size 语义仍未实现；DBM 基础运行时改址已完成，更复杂 FIFO/DBM 组合仍需要
  独立边界测试，再迁移更多 UART 或其它外设。

## 2026-08-31 ADC1 DMA endpoint integration

- [x] 在 STM32H723 ADC 芯片层把 ADC1 regular `ADC_DR` 接入板卡无关的 DMA
  endpoint。P2M endpoint callback 复用 guest 读取 `ADC_DR` 的 EOC 清除、AUTDLY
  继续和 IRQ 更新副作用；DMA controller 继续负责 DMAMUX、stream 选择、NDTR、
  地址、HT/TC 和 circular 状态。
- [x] `dm-mc02,adc-dma-endpoint=on` 为默认路径；`off` 显式保留原来的
  `address_space_memory` MMIO 读取兼容路径。machine 初始化后重新应用属性，避免
  QOM 属性写入早于 ADC peripheral 初始化时被 reset 清掉。
- [x] ADC DMA bare-metal smoke 支持 `[on|off]` 参数，整机 smoke 同时覆盖 endpoint
  和 legacy 两种路径；两者验证同一 16-bit circular buffer、NDTR/地址回绕以及
  HT/TC 状态。ADC qtest `36/36`、host CTest `15/15` 通过。
- [x] 下一道 DMA 芯片层切片已迁移 SPI2：独立 `dm_mc02_spi` 模块保留 BMI088
  framing/片选/消费 token，同时通过通用 `DmMc02DmaEndpoint` 接收 TX beat、提交
  RX beat；`spi-dma-endpoint=on` 为默认路径，`off` 显式走旧 MMIO 兼容路径。
- [x] SPI2 DMA smoke 已覆盖两种路径、固定 request endpoint、PINC live `PAR` 和
  BMI088 精确字节结果；PINC 递增到非 endpoint 地址时仍完成 DMA 地址/NDTR 状态，
  但不会误调用固定器件 callback。QEMU 增量 build、`run-spi2-dma-smoke.sh on|off`
  均通过。
- [x] endpoint FIFO、DBM 和宽度转换已经沿 DMA callback 路径实现；新的 DMA DBM
  运行时改址 smoke 验证了非活动 buffer 的 reload base、CT、NDTR、HT/TC 和直接
  TIM8 请求边界。异步 backpressure、per-beat rollback 仍未实现。

## 2026-08-31 SPI2 DMA endpoint integration

- [x] 所属层为 STM32H723 DMA 芯片层到可复用 SPI 器件数据面的边界；DMA producer
  负责 DMAMUX/stream 仲裁、宽度检查、NDTR、地址、TC/HT 和 IRQ，SPI consumer
  通过 `DmMc02DmaEndpoint` 负责一字节 BMI088 wire transaction，板级组合只提供
  DMA channel/request/address 和 GPIO 片选。
- [x] 原来嵌在 `dm_mc02.c` 的 SPI2 实现已抽到 `dm_mc02_spi.[ch]`，SPI1 复用同一
  模块；模块可单独初始化、reset、清理和配置 DMA/cosim link，降低板级 composition
  root 对器件私有状态的依赖。
- [x] endpoint callback 只在固定 `PAR` endpoint 基址上生效。DMA `PINC` 仍以捕获的
  `reload_par` 参与 request 路由，实际 beat 使用 live `SxPAR`；递增后的地址回到
  MMIO 路径，不会被错误当作同一个 SPI 数据寄存器。该规则已由 `{0x10, 0x55}`
  fixture 验证，BMI088 寄存器保持 `0xA5`，`PAR` 增加 2。
- [x] `tools/run-spi2-dma-smoke.sh [on|off]` 和
  `tools/run-spi2-dma-legacy-smoke.sh` 分别覆盖 endpoint/兼容路径；两者必须保持
  相同的 guest 字节和 DMA 状态结果。
- [x] 增量 QEMU build、ADC qtest `36/36`、TIM qtest `19/19`、host CTest `15/15`
  （含自动发现的 SPI endpoint/legacy smoke）、Python `243 passed` 和全部 shell
  语法检查通过；`trobot/` 未修改。
- [x] endpoint 当前支持同步 direct 和 FIFO 路径；FIFO 可做受支持的宽度转换，基础
  DBM reload 也可复用该路径。运行时 inactive `M0AR/M1AR` 改址由
  `run-dma-dbm-reconfigure-smoke.sh` 覆盖。
- [ ] endpoint 异步 backpressure、callback 失败后的 per-beat rollback，以及 FIFO+
  DBM 的独立 endpoint 组合回归仍是 DMA 层缺口。

## 2026-08-31 DMA endpoint contract

- [x] 在 STM32H723 DMA 芯片层新增板卡无关的 `DmMc02DmaEndpoint` 回调契约。
  DMA 是 producer/consumer 边界，P2M 通过 `read` 获取外设数据，M2P 通过 `write`
  提交内存数据；每次 callback 透传 beat 字节数和 virtual timestamp。旧的
  `hwaddr + address_space_memory` 路径保持兼容。
- [x] 首阶段只允许 direct mode、等宽的同步传输；endpoint FIFO/DBM/宽度转换尚未
  接入，失败会回到 DMA 错误状态路径或由调用方使用旧接口。未迁移任何真实外设，
  因此不会改变 ADC/UART/SPI/TIM8 现有副作用。
- [x] 新增 host 隔离 smoke，覆盖 P2M/M2P 的 8/16/32-bit beat、timestamp 透传、
  缺失 callback、空数据/零长度以及 callback 失败；QEMU endpoint 源同时保持
  可独立编译，不依赖 QEMU 私有初始化头。
- [x] 验证：`dm_mc02_dma_endpoint_smoke` 通过，QEMU `qemu-system-arm` 成功重链，
  `trobot/` 未修改。
- [x] 下一道 DMA 芯片层集成门已由 ADC1 完成：保留旧 MMIO 路径，并用同一
  bare-metal fixture 对比 endpoint/legacy 的数据、地址和状态结果。

## 2026-08-31 ADC JAUTO automatic injected conversion

- [x] 在 STM32H723 ADC 芯片层实现 `CFGR.JAUTO`：regular sequence 完成时，若
  `JEXTEN=0` 且存在有效 injected context，则自动启动 injected sequence，并由
  `JADSTART` 反映活动状态；直接写入的 `JADSTART` 在 JAUTO 下不重复启动。
- [x] 明确组合边界：`JAUTO` 不接受 `DISCEN/JDISCEN`，只使用软件 injected
  trigger context；非连续 regular 只完成一次自动 injected，连续 regular 在注入组
  完成前暂停，完成后恢复下一 regular sequence。`JQM` context snapshot 仍由已有
  queue admission 逻辑管理。
- [x] 修复 `JADSTP` 中止边界：中止 JAUTO 注入时释放 regular 等待状态；如果 regular
  仍由 `AUTDLY` 卡在 `ADC_DR` 消费边界，则继续等待 guest 读 DR。新增 qtest 覆盖
  regular->injected、JAUTO 关闭、以及连续模式 `JADSTP` 恢复。
- [x] 验证：ADC 芯片 qtest `36/36`（含本切片）；本切片不改变 DM-MC02 board
  route、DMA 或上层 worker。
- [x] 后续芯片层切片已补 `JAUTO+JQM` 的真实 guest/DMA 组合回归；结果记录在
  `2026-09-01 H723 ADC JAUTO/JQM regular DMA 边界`。仍待实现 regular low-power
  auto-power-off 和其它 H723 injected trigger source；这些通过隔离测试后才进入
  board boundary。

## 2026-08-31 ADC injected context queue

- [x] 在 STM32H723 ADC 芯片层实现 queue-enabled injected context admission：固定
  一个 active context 和一个 pending context，完整 `JSQR` 写入按 FIFO 顺序快照；
  队列已满时保持已有快照不变并锁存 `ISR.JQOVF`。
- [x] 实现 `JQOVFIE` 的共享 ADC IRQ level gate、`ISR` W1C，以及 `JQM=0` 保留最后
  context、`JQM=1` 完成后清空 `JSQR` 并等待新 context 的边界。活动 rank 始终使用
  快照，不会被后续 guest 的 `JSQR` 写入改变。
- [x] 新增 ADC qtest 覆盖 A/B/C FIFO 顺序、满队列拒绝与 IRQ/W1C、JQM end-empty
  和无 reset 恢复；stateful OCREF 的单 context helper 显式设置 `JQDIS`。
- [x] 验证：ADC qtest `33/33`；本切片仍只属于芯片层，不改变 DM-MC02 board route
  或上层 worker。
- [ ] 下一道芯片层切片：`JAUTO`、regular low-power auto-power-off、其它 `JEXTSEL`
  source 以及完整 H723 trigger/source matrix；继续通过隔离测试后再向上接入。

## 2026-08-31 ADC JDISCEN software-start boundary

- [x] 修复 STM32H723 ADC 芯片层对 `JDISCEN` 的判定：只有多-rank 且配置了
  `JSQR.JEXTEN` 的外部 injected sequence 才按每个边沿一个 rank 分组；
  `JEXTEN=0` 的软件 `JADSTART` 保持完整 sequence 语义。
- [x] 新增 ADC qtest 覆盖 `JQDIS=1 + JDISCEN=1 + JEXTEN=0` 的三-rank 软件启动，
  验证每个 rank 的 `JEOC`、最终 `JEOS` 和 `JADSTART` 清除；第三 rank 使用既有
  无配置输入回退值 `0`，不引入新的 board-level source。
- [x] 验证：QEMU ADC qtest `32/32`；本切片只修改 ADC 芯片判定和隔离 qtest，未改变
  board trigger route 或上层 worker。
- [x] 后续芯片层切片已实现 injected `JQM`/context queue/`JQOVF`；`JAUTO` 和其它
  H723 ADC trigger source 仍待继续独立实现。

## 2026-08-31 ADC AUTDLY data-consumption boundary

- [x] 在可复用 STM32H723 ADC 芯片层加入 `CFGR.AUTDLY` 的 regular 数据消费状态：
  `EOC` 未被 guest 读取时停止下一 rank/sequence 的 virtual timer，读取 `ADC_DR`
  后按当前 virtual timestamp 恢复；DMA 同步读取仍可立即释放等待，默认关闭时保持
  原有 cadence 和性能路径。
- [x] 明确 trigger bus 的 `event_count` 契约：ADC sink 对压缩批次最多接受一个启动，
  忙时忽略整批，不伪造无界 ADC trigger FIFO；该策略属于 ADC 芯片层，不由 board 或
  timer callback workaround。新增 ADC qtest 覆盖 AUTDLY 保持、DR 读取释放和停止。
- [x] 加入 regular `DISCEN/DISCNUM` 外部触发 subgroup：每个匹配边沿最多推进
  `DISCNUM+1` 个 rank，subgroup 间保留 rank 游标和 `ADSTART`，软件启动仍为完整
  sequence；`AUTDLY`、忙时不排队和 sequence 尾部状态均有 qtest 覆盖。
- [ ] 仍待实现：regular low-power auto-power-off、injected `JQM`/context queue/
  `JQOVF`、`JAUTO` 和其它 H723 ADC trigger source；这些继续在芯片层独立切片后
  再接入 board route。

- [x] 在 ADC 芯片层加入 injected `JDISCEN`：外部触发的多-rank JSQR 每个边沿推进
  一个 rank，subgroup 间保留 rank 游标和 `JADSTART`，软件/board route 不负责拼接
  数据；单 rank 配置忽略该位。qtest 覆盖三次 TIM8 trigger 的 rank 顺序和最终
  `JEOS/JADSTART` 边界。

## 2026-08-31 center-aligned timer phase

- [x] 在通用 STM32H723 timer 芯片层加入中心对齐 `0..ARR..0` 三角 phase；`CNT`、
  update period、compare deadline、PWM 观察和时钟/寄存器重锚定共用同一 phase 计算，
  不为每个 PWM 边沿创建宿主定时器。
- [x] 支持 `CMS=01/10/11` 对 compare flag/DMA match 的下数/上数/双向过滤；master
  OCREF route 保留双向内部参考边沿。`ARR=0` 退化为静态计数，避免零周期事件。
- [x] 新增 TIM2 芯片层 qtest，覆盖上数、到顶、下数、双向 compare、完整周期 UIF 和
  CMS 方向过滤；定向 timer qtest `13/13` 通过，未修改 `trobot/`。
- [ ] 当前仍未实现中心对齐的完整 UEV 选择、组合/边沿模式、dead-time 和高级
  TIM1/TIM8 OCxM；Break/MOE 的最小输出级语义另见后续切片。

## 2026-08-31 stateful OCREF compare backlog

- [x] 在通用 timer 芯片层把 active-on-match、inactive-on-match 和
  toggle-on-match 的 compare 积压按原始 virtual deadline 逐事件重放；每个事件
  独立更新 `SR.CCxIF`、OCREF 状态、master-event 和下游 trigger，避免 toggle 在
  `event_count > 1` 时只翻转一次却报告多个同方向事件。
- [x] 保留普通 PWM/无状态 compare 的批处理与绝对 phase 调度；frozen 模式不再被
  误判为需要逐边沿重放，TIM2 qtest `10/10`、ADC qtest `25/25` 通过。
- [x] 最终回归：QEMU 增量构建、host CTest `14/14`（含整机 QEMU smoke suite）、
  Python `243 passed`、全部 shell 语法检查和 `uv lock --check` 通过。
- [ ] 当前没有可控宿主停顿注入的 timer qtest；真实延迟恢复按到期事件数线性执行，
  极端高频长时间停顿仍可能形成恢复开销。后续如需要更高吞吐，应先扩展事件 ABI
  表达 alternating edge batch，再由 trigger sink 明确定义丢弃/排队语义，不能直接
  把 stateful 事件重新压成无方向批次。

## 2026-08-31 TIM2 CC3/CC4 compare 芯片层语义

- [x] 在通用 timer 芯片模型中补齐 CC3/CC4 的 compare match、`SR.CC3IF/CC4IF`、
  `DIER.CC3IE/CC4IE` 共享 IRQ 门控和 `SR` W0C；CCR1..4 的 deadline 统一按虚拟
  时间选择，多个通道同一时刻会合并置位。
- [x] compare flag 与 IRQ enable 解耦：写入 CCR 后激活对应通道，flag 可被 guest
  轮询观察，IRQ 仍只由对应 `CCxIE` 控制；开启任一 compare IRQ 时强制单事件调度，
  避免 compare batch 延迟中断。
- [x] `dm-mc02-tim2-test` 扩展为 `6/6`，覆盖 CC3/CC4 独立 deadline、屏蔽 IRQ
  仍锁存 flag、同相位合并和 W0C；CC1/CC2 原有测试保持通过。
- [ ] 本切片仍不把 CC2..CC4 接入 board route、ADC trigger bus 或 master-event
  callback；CC3/CC4 的 master trigger、OCxREF、组合/边沿模式、预装载/UG transfer
  和完整 timer source matrix 仍待后续按层实现。

## 2026-08-31 TIM2 CC2 compare 芯片层语义

- [x] 在通用 TIM2 复用芯片模型中补齐 CC2 compare 的最小寄存器语义：`DIER.CC2IE`
  门控共享 timer IRQ，compare match 独立锁存 `SR.CC2IF`，并沿用 `SR` 写零清除；
  CC1/CC2 同一虚拟时刻会同时置位，下一次 deadline 按各自 CCR 阈值和原有 virtual
  phase 推进。
- [x] 新增 `dm-mc02-tim2-test` 芯片层 qtest，覆盖 CC2 单独 flag/IRQ、W0C、CC1/CC2
  同相位及不同相位；修正 compare 调度入口遗漏 `CC2IE` 的缺陷。定向测试 `5/5`，
  ADC 定向测试 `22/22` 通过。
- [ ] 本切片没有把 CC2 接入 board route、ADC trigger bus 或 master-event callback；
  CC3/CC4 的 master trigger、OCxREF、组合/边沿模式、预装载/UG transfer 和完整
  timer source matrix 仍待后续按层实现。

## 2026-08-31 通用 timer master route 与 TIM3 TRGO ADC 映射

- [x] 将 timer master 输出的 source 配置收敛到通用 board route 数据：每个 timer
  route 独立声明 `TRGO`/`TRGO2` 的 source ID；未接出的输出使用无效 source，timer
  芯片模型不依赖 ADC 或具体开发板，板级组合层负责把抽象输出接入 trigger bus。
- [x] 接入 H723 TIM3 `TRGO` 的 ADC 触发映射：regular group 使用
  `EXTSEL=4`，injected group 使用 `JEXTSEL=12`。trigger bus 的 source ID 继续采用
  regular `EXTSEL` 编码，ADC 芯片层在 injected group 内执行独立的 `JEXTSEL` 映射。
- [x] 增加芯片层和板级边界测试，覆盖 TIM3 `CR2.MMS=2`、`EGR.UG`、ADC regular
  `EXTSEL=4` 以及 injected `JEXTSEL=12` 的上升沿触发，并确认未选择 TIM3 的 ADC
  不会响应该事件。
- [x] TIM3 route wiring、上述映射及对应测试已完成；source 未匹配的 ADC 负路径也已
  覆盖。当前仍只承诺表中列出的 source、同步 trigger bus 语义和已有 timer master
  event 类型，不扩张为完整 H723 触发矩阵。

## 2026-08-31 TIM2 TRGO ADC 映射

- [x] 在 DM-MC02 与 STM32H723-EVAL board profile 中加入统一的 TIM2 route，保留独立
  的 TIM2 base/IRQ/source 描述；machine 通过与 TIM1/TIM3/TIM8 相同的 route-based
  master-event callback 接入 trigger bus。
- [x] 接入 H723 TIM2 `TRGO` 的 regular `EXTSEL=11` 与 injected `JEXTSEL=2` 映射，
  新增 regular/injected 正向测试，并确认未选择 TIM2 source 时 ADC 不响应。
- [x] TIM2 定向与完整 ADC qtest 已通过（TIM2 qtest `5/5`、ADC qtest `22/22`）；
  当前板级仍只承诺 TIM2 update/TRGO 路径，不扩张为 TIM2 `TRGO2`、compare master
  输出或完整触发矩阵。

## 2026-08-31 TIM1 trigger routing vertical slice

- [x] 在 H723 board profile 中加入可复用的 TIM1 timer instance（`0x40010000`，
  update IRQ 25），与 TIM3/TIM8/TIM12/TIM24 共享同一 timer 芯片模型；machine
  的 timer storage 改为由 profile capacity 定义，TIM8/蜂鸣器索引保持正确。
- [x] 将 TIM1 `CR2.MMS/MMS2=2` 的 update 事件接入 board trigger bus，使用稳定的
  regular ADC source identifiers `TIM1_TRGO=9`、`TIM1_TRGO2=10`；TIM1/TIM8
  都显式获得 H723 `MMS2` 能力，timer model 本身仍不依赖 ADC 或 board pins。
- [x] 在 ADC 芯片层按物理 source 做 regular/injected mux 映射：TIM1 TRGO 为
  `EXTSEL=9/JEXTSEL=0`，TIM1 TRGO2 为 `EXTSEL=10/JEXTSEL=8`；同时修正 TIM8
  的 injected 映射为 `EXTSEL=7/JEXTSEL=9`、`EXTSEL=8/JEXTSEL=10`。
- [x] ADC qtest 新增 TIM1 `TRGO` regular 与 `TRGO2` injected 的完整板级边界测试，
  并保留 TIM8 injected 回归。
- [ ] 当前仍只实现 update master event；TIM1/TIM8 的 OCxREF、组合/边沿模式、
  CC2..CC4、预装载/UG transfer、其它 timer source 及硬件级跨总线延迟仍未实现。

## 2026-08-31 ADC calibration data path

- [x] 在 STM32H723 ADC 芯片层把 regular `ADC_DR` 与 injected `JDR1..4` 统一接入
  校准转换函数；按 `DIFSEL` 为 channel 0..19 选择 `CALFACT_S` 或 `CALFACT_D`，
  offset 结果在无符号 16-bit 范围内饱和。
- [x] 实现 `ADC_CALFACT2` 的六个 `LINCALRDYW1..6` 窗口保存、读回和写入提交语义；
  word 6 只暴露 10 个有效位，其他 word 暴露 30 位，reset 清除全部窗口。
- [x] 增加校准数据 bare-metal smoke，覆盖 regular/injected、offset、linearity、
  组合转换、有效位边界、禁用态写入拒绝和 reset 清零；ADC 芯片 qtest 继续覆盖
  寄存器约束。
- [ ] H723 的真实模拟 SAR 电容线性补偿仍未建模；当前无模拟电路模型，因此将
  `LINCALFACT` word 1 解释为 signed Q0.30 gain delta，word 2..6 仅保留并可经
  窗口协议访问。该近似接口已明确隔离，后续替换模拟核心不需改变 guest API。

## 2026-08-31 DMA coalesced request verification

- [x] 为默认近似模式新增独立 `TIM8_CH1 -> DMA2 Stream6` guest smoke；它使用
  默认 `dm-mc02`，验证固定 `TIM8_CCR1` 端点只保留批次最后一个 waveform 值，
  同时完整保留 circular `NDTR` 重装、M0AR 和 HT/TC 状态。
- [x] 主线程复核并运行精确模式 `run-tim8-dma-smoke.sh`、DBM
  `run-tim8-dbm-smoke.sh`、UART batch `run-dma-batch-smoke.sh` 和新的
  `run-tim8-approx-dma-smoke.sh`；四项均通过。
- [x] 串行真实 Release 固件基准保持实时量级：默认构建预热后 5 s 为
  `0.999760x`，native/LTO 隔离构建无预热样本为 `0.995963x`；后者包含启动阶段，
  不能作为 native 优于默认构建的结论。
- [ ] 合并接口仍是明确的近似边界：不适用于需要每个外设 MMIO 写入、FIFO、DBM
  或逐事件中断观察的消费者；这些调用方必须保留逐 item 或普通 batch 路径。

## 2026-08-31 ADC injected conversion vertical slice

- [x] 在 STM32H723 ADC 芯片层加入真实 `JSQR`/`JDR1..4` 地址和注入组独立
  virtual timer；regular/injected conversion 可并行运行，rank 时序使用同一
  ADC channel source、board source 和 external raw/pin-voltage override 接口。
- [x] 支持 `JL=1..4` 的 injected rank sequence、软件 `JADSTART`（要求
  `CFGR.JQDIS`）、`JADSTP`、`JEOC/JEOS` 状态和中断、JDR 读取清 `JEOC`、ISR
  W1C 保留 `JEOS`，并按 `TIM8_TRGO` 的 source/edge 配置接入通用 trigger bus。
- [x] 支持 injected conversion 的 ADC kernel clock 变化后按剩余 half-cycle
  重排 deadline；system reset、`ADDIS`、`JADSTP` 和 clock=0 都会停止/暂停
  注入转换，不影响 regular group。
- [x] 新增并通过 ADC 芯片层 qtest：17/17，覆盖软件序列、TIM8 rising/falling
  edge、状态读取副作用、停止命令、regular/injected 并行、时钟切换和 reset。
- [ ] 仍待实现：`JQM`/context queue、`JAUTO`、`JDISCEN`/`JQOVF`、完整 injected
  calibration/data-management 语义、其它 `JEXTSEL` 外部触发源和完整 H723
  kernel clock/source matrix；当前 trigger bus 仍是同步 peripheral-event
  接口，不宣称完整硬件级队列时序。

## 2026-08-31 ADC timing follow-up and verification

- [x] 在 ADC 芯片层补齐 `adc_sequence_channel()` 的前置声明，修复严格编译
  选项下的函数隐式声明错误；未改变运行时接口或 `trobot/`。
- [x] 重新构建 `qemu-system-arm` 与 ADC qtest；ADC qtest `15/15` 通过，覆盖
  重复 `ADSTART`、有效 ADC clock 下的校准时长和活动 rank 改频后的剩余时间。
- [x] 串行 host CTest `13/13` 通过，其中 QEMU smoke suite `68/68`、`68.04 s`；
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --project . pytest` `243 passed`。
- [x] `bash -n tools/*.sh`、Python `compileall`、`uv lock --check` 和
  `trobot/` 写入保护检查通过。直接运行 pytest 时会被宿主 ROS 的全局插件
  自动加载阻断（缺少 `yaml`），项目测试使用上述环境变量隔离外部插件。
- [ ] ADC 校准因子尚未作用于采样数据；`LINCALRDYW1..6` 完整逐 word handshake、
  injected conversion 数据面、其它外部触发源和完整 kernel clock matrix 仍待
  后续芯片层切片。

## 2026-08-31 ADC calibration control and factor boundary

- [x] 在 STM32H723 ADC 芯片层加入 `CALFACT_RES13`（offset `0xc4`，单端/差分
  各 11 bit）和 `CALFACT2_RES14`（offset `0xc8`，30 bit 线性因子）的有效位
  语义；软件覆写仅在 ADC 已使能、无 regular conversion 且无校准进行时接受，
  reset 清零。
- [x] 完善 `ADC_CR.ADCALLIN`、`ADCALDIF` 与 `ADCAL` 的组合和互斥：校准只可在
  ADC 禁止且 regular group 空闲时启动，`ADCAL` 在 virtual time 延迟后自清除；
  校准进行中写入启动/模式命令不会启用或启动转换，也不能取消校准。线性校准
  完成后暴露六个 `LINCALRDYW` ready-word 状态。
- [x] 新增 ADC qtest 覆盖模式命令、活动期间写入、非法启动、两个校准因子掩码、
  禁止态写入和 system reset；QEMU ADC qtest `9/9`、全量 QEMU smoke `73/73`、
  host CTest `13/13`、Python `243 passed` 均通过。
- [ ] 当前校准因子是可复现的寄存器数据，不从模拟输入自动估计，也未将 offset/
  linearity 因子施加到 ADC 采样结果；ADC 电压 regulator、注入组校准、完整
  linear-factor word handshake 和真实器件校准统计仍待后续芯片层切片。

## 2026-08-31 H723 timer master trigger and ADC trigger bus

- [x] 修正 ADC 时钟配置状态的芯片层边界：仅写 CPU/PLL1 时钟树不再把
  默认 ADC 时钟误切换为未启用 PLL2P 的 0 Hz；写入 ADCSEL 或启用 PLL2 后才
  进入真实 source 计算。新增 qtest 覆盖 `PLL2P`、`PLL3R`、`CLKP` 和 CCR
  `/1`、`/256`，ADC qtest `5/5`、ADC 定向 smoke 及串行 QEMU smoke `73/73`
  通过。该切片仍只承诺当前 H723 kernel source，不代表完整 APB 分频树。
- [x] 在 TIM2 芯片层新增 QTest 回归：验证 CEN=0 时 CNT 随 virtual time
  冻结、PSC/ARR 周期与 UIF 更新语义，以及运行中重写 PSC/ARR 不回退当前 CNT；
  注册为 `dm-mc02-tim2-test`，主线程构建并运行 `3/3` 通过。QTest 没有公开
  board trigger sink，因此没有伪造 CR2.MMS callback 的断言；该边界继续由
  `run-tim2-event-smoke.sh` 覆盖。
- [x] 在不依赖 ADC、timer 或 DM-MC02 pin wiring 的芯片层新增固定容量
  `DmMc02TriggerBus`；事件包含 `source_id`、边沿方向和 virtual-time 时间戳，
  注册完成后同步按稳定顺序 fan-out，不分配内存、不创建 host 线程或事件队列。
- [x] 按 STM32H723 `ADC_CFGR.EXTSEL/EXTEN` 接入真实 `TIM8_TRGO=7` source；
  ADC1/ADC2 作为 bus sink 自行过滤 source 和 rising/falling/both 语义，旧的
  `dm_mc02_adc_external_trigger()` 保留为兼容包装。
- [x] 通用 timer 芯片层解码 `CR2.MMS`，并对 H723 TIM8 显式开放 `MMS2`；支持
  `EGR.UG` reset、`CEN` enable、update、CC1 compare pulse，TIM8 board profile
  将 TRGO/TRGO2 分别映射为 source 7/8，普通 timer 不会误获 MMS2 能力。
- [x] TIM8 board callback 只发布 trigger event，WS2812 DMA 的既有 request/
  batch 路径保持独立；新增 host trigger smoke 和 ADC bare-metal source/edge
  rejection 及 master-mode smoke。host CTest `13/13`、QEMU smoke `71/71` 和
  QEMU 构建通过。
- [x] 真实 Release `trobot.elf` RTF 回归：`1.001000 s` virtual time /
  `1.001061 s` wall time，`0.999939x`、`999.939 tick/s`、QEMU CPU `102.9%`、
  RSS `50156 KiB`、IWDG timeouts `0`；trigger bus 接入未破坏当前实时基线。
- [x] 2026-08-31 收尾检查：`bash -n tools/*.sh`、Python `compileall`、
  `uv lock --check` 和最新 Release QEMU 全量 CTest 均通过；`trobot/` 本轮无新增
  文件写入。
- [ ] 仍待实现：其它未接出 timer 的真实 TRGO/TRGO2 source wiring、OCxREF
  电平/组合/边沿模式、CC2..CC4 触发、EXTI/HRTIM/LPTIM source，以及完整 ADC
  kernel clock/source matrix。当前 bus 是同步 peripheral-event 分发，不是硬件级
  触发总线时序模型。

## 2026-08-31 ADC overrun data-management boundary

- [x] 在 ADC 芯片层验证 `CFGR.OVRMOD=1`：当前一个 `EOC` 尚未被 guest 消费时，
  新转换会置位 `OVR` 并把最新 half-word 写入 `ADC_DR`；读取 `ADC_DR` 只清
  `EOC`，`OVR` 仍由 `ISR` W1C 独立清除。新增真实 IRQ18 裸机 smoke，覆盖
  `EOC+OVR`、最新值 `0x0200`、DR 读取后的 OVR 保留和最终 W1C 清除。
- [x] 验证范围限定在 ADC 芯片/guest MMIO 边界，不改变默认 `OVRMOD=0` 行为；新
  runner 会被 QEMU smoke suite 自动发现。
- [ ] 仍待实现：ADC 其它外部触发源、完整 kernel clock/source matrix、校准系数，
  以及多 ADC 共享数据管理的完整硬件时序。

## 2026-08-31 timer master-event reentrancy and backlog bound

- [x] 在 TIM2 复用芯片层对 `TRGO/TRGO2` 路由做事件时刻快照；update/compare
  callback 修改 `CR2` 不会改变已经产生的事件，也不会因为第一个输出的 sink
  修改配置而跳过第二个输出。UG 同时产生 reset/update 时，两类路由也分别在
  callback 前快照。
- [x] master-event callback 增加 `event_count`，连续 compare 事件合并为一次同步
  callback，trigger bus 提供对应的 `publish_batch()`；避免极端宿主停顿下按最多
  `UINT_MAX` 次逐事件调用。不可表示的时间线积压收敛为一个当前时间代表事件，
  并统一清理 compare/update deadline 溢出后的旧状态。
- [x] host trigger batch、ADC 各触发模式、TIM8 DMA circular/DBM、TIM2 时钟和
  最新 QEMU 全量 smoke `73/73`、CTest `13/13`、Python `243 passed` 已通过；
  本轮没有修改 `trobot/`。
- [ ] 仍待实现：使用受控虚拟时间/宿主停顿直接断言 `events`、最后事件时间戳及
  下一 deadline 的专门 timer unit/qtest；当前正常 QEMU smoke 未覆盖任意时间点
  的 callback 人为延迟注入。

## 2026-08-31 TIM8 compare DMA endpoint and absolute phase

- [x] 修复 TIM8 `CC1DE` compare callback 误用 state-only
  `dm_mc02_dma_advance_stream()` 的缺陷；现在通过 DMA2/DMAMUX1 request 47 的
  `dm_mc02_dma_request_batch()` 为每个事件保留 `TIM8_CCR1` MMIO 写入，避免只更新
  `NDTR`/指针而没有实际 PWM 值落到外设寄存器。
- [x] live `CCR1` 写入不再重置 TIM2 复用模型的 CNT virtual phase；compare callback
  按单个 timer period 统计绝对 deadline 之后已到期的事件，维护原相位并避免重新从
  callback 当前时刻起算。master compare 仍强制单事件 batch；极端不可表示的事件数
  按 callback ABI 饱和到 `UINT_MAX`，完整积压恢复仍不是本层承诺。
- [x] 修正 TIM8 DMA smoke 的异步观察竞态，并覆盖 compare DMA 的 circular 与 DBM
  端点副作用；QEMU Release build、QEMU smoke `72/72`、host CTest `13/13`、
  `uv run --project . pytest` `243 passed`、shell/compileall/`uv lock --check` 均通过。
- [ ] 仍待实现：TIM12/TIM24 及其它未接出 timer 的真实 TRGO/TRGO2 source wiring、OCxREF
  电平/组合/边沿模式、CC2..CC4 触发、预装载/UG transfer、完整 timer source
  matrix，以及极端宿主停顿下的 compare 积压统计专门测试。

## 2026-08-30 DMA stream request arbitration

- [x] 在 DMA 芯片层增加 `SxCR.PL[17:16]` 仲裁：同一 DMAMUX request 和
  peripheral address 的多个候选 stream 中，高优先级先服务，同优先级按低
  stream 编号稳定选择；一个 request 不再广播到多个 stream。
- [x] `dm_mc02_dma_request()` 保留单匹配 stream 的缓存快速路径；多候选请求
  在每个事件选择一个当前启用且仍有 `NDTR` 的 stream。
- [x] `dm_mc02_dma_request_batch()` 对每个 item 独立仲裁，保留每 item 的
  address-space/MMIO side effect 和批次末 IRQ fan-out。
- [x] 新增 `run-dma-arbitration-smoke.sh`：UART TX batch 覆盖高优先级和
  完成后低优先级接管，UART RX 单事件覆盖同一选择规则；DMA arbitration、FCR、
  batch 定向 smoke 和全量 QEMU smoke `69/69` 通过。
- [ ] 仍待实现：FEIF 真实产生条件、MBURST/PBURST 精确总线占用、DMA 时钟级
  仲裁和完整错误恢复；当前仲裁是同步 peripheral-request 级别。

## 2026-08-30 BMI088 dynamic temperature and bias random walk

- [x] 在与 QEMU 无关的 `DmMc02Bmi088Signal` 层加入每轴温度系数和 bias
  random walk；温漂以 25°C 为参考，random walk 按接受样本的 virtual-time
  `sqrt(dt)` 推进，默认参数为零，不改变既有默认热路径。
- [x] `DmMc02Bmi088` 暴露对应窄 API，DM-MC02 machine 增加四个三轴 QOM
  字符串属性；板级温度输入同时更新 accel/gyro 两个 die，避免只更新 accel
  导致 gyro 温漂配置失效。
- [x] 新增 `dm_mc02_bmi088_drift_smoke`，覆盖每轴温漂、同 seed 确定性、ODR
  拒绝不推进漂移、reset 清动态 bias/留配置；`run-bmi088-drift-smoke.sh`
  覆盖 machine 命令行和运行时 QMP 属性。
- [x] 定向验证：drift host smoke、QMP property smoke、QEMU build 均通过。
- [x] 全量回归：QEMU smoke `68/68`（CTest `11/11`）、Python `243 passed`、
  shell/compileall/uv 检查通过；Release 固件 RTF `0.999561x`、CPU `103.9%`、
  IWDG timeouts `0`。
- [ ] 仍待实现：基于真实 BMI088 记录的温度系数、噪声谱和 bias random walk
  统计标定，以及动态温度物理源；当前实现是可复现的参数化信号模型。

## 2026-08-30 DMA FIFO control and direct-mode error slice

- [x] 在 DMA 芯片层实现 `SxFCR` 的寄存器语义：`FTH`、`DMDIS`、`FEIE`
  可写，`FS` 按 4-word FIFO 的实际字节占用合成，保留位不会被锁存。
- [x] 在 DMA 芯片层实现 direct mode 的 `PSIZE/MSIZE` 宽度不匹配检测：置位
  `DMEIF`、清除 `EN`，并由 `DMEIE` 门控 level-sensitive IRQ；`LIFCR/HIFCR`
  的 W1C 清除保持有效。
- [x] FIFO mode 使用 4-word byte FIFO 实现 M2P/P2M 宽度打包/拆包、阈值排空、
  动态 `FS`、循环重载和 M2P 无外设副作用状态推进；新增
  `run-dma-fcr-smoke.sh`，覆盖 FCR 掩码、DMEIF/IRQ/W1C、FIFO 16-bit 到 8-bit
  UART TDR、UART RX 8-bit 到 16-bit 内存拼包、FS、HTIF/TCIF 和 circular reload。
- [x] 定向及全量验证：QEMU build、DMA 相关 smoke、QEMU smoke `68/68`、host CTest
  `11/11`、Python `243 passed`、shell/compileall/uv 检查均通过；真实 Release
  固件 RTF `0.999561x`、CPU `103.9%`、IWDG timeouts `0`。
- [ ] 仍待实现：真实硬件 FEIF 产生条件、FIFO burst/threshold 的精确总线时序、
  DMA 请求仲裁以及完整错误恢复；当前实现不宣称位级 DMA 总线模型。

## 2026-08-30 DMA peripheral-request batching

- [x] 在 DMA 芯片层新增兼容的 `dm_mc02_dma_request_batch()`，一次解析匹配的
  DMAMUX stream，在批次内仍逐 item 执行 address-space 读写和外设 MMIO 副作用，
  并在批次边界统一更新 level-sensitive IRQ；原 `dm_mc02_dma_request()` 保持单事件
  语义并继续可用于需要逐事件可见边界的调用方。
- [x] UART TX timer 接入 DMA batch API；新增 `run-dma-batch-smoke.sh` 和对应
  bare-metal guest，验证真实 UART 字节顺序、circular NDTR/M0AR/PAR 重装以及 HT/TC
  状态。该 runner 被现有 QEMU smoke suite 自动发现。
- [x] 定向验证：QEMU Release build、UART DMA、DMA memory/HT、TIM8 DBM 和 batch
  smoke 均通过；DMA FCR/DME 最小语义已由独立 smoke 覆盖，真实 FIFO/FEIF 和
  完整仲裁仍待后续分层实现。

## 2026-08-30 SoC/profile 复用验收与启动回归

- [x] 新增第二个同 STM32H723 SoC 的 `STM32H723-EVAL` board profile；它复用
  `dm_mc02_stm32h723_soc`、芯片模型和 machine composition root，只替换 GPIO/pin
  wiring、UART/FDCAN 数量及 DMA route 数据。
- [x] 新增 host `dm_mc02_board_profile_smoke` 和 QMP
  `run-board-profile-smoke.sh`，覆盖 profile lookup、静态 profile 校验、GPIO/AF
  解码和备用 profile 的真实 QEMU 初始化。
- [x] 修复 SoC profile 的 CPU QOM 类型契约：`ARMv7M.cpu-type` 使用完整的
  `cortex-m7-arm-cpu` 类型名；不再把命令行 `-cpu cortex-m7` 的简写规则错误地
  应用于设备属性。
- [x] 验证：QEMU 构建、host CTest `10/10`、全量 QEMU smoke `65/65`，备用 profile
  QMP 启动 smoke 通过；现有 Python/uv 和 shell 回归保持可用。
- [ ] 仍待实现：更多真实 H723 外设语义、完整 USB/FIFO/中断模型、Gazebo 具体机构
  验收，以及跨进程严格锁步、ACK/重传和断线无损恢复。

## 2026-08-30 BMI088 采样信号层抽取

- [x] 新增不依赖 QEMU 对象的 `DmMc02Bmi088Signal` 模块，统一承载 accel/gyro
  的 bias、noise、RNG、量程、ODR 接受窗口、一阶带宽滤波、温漂系数、bias
  random walk 和 16-bit raw 饱和量化。
- [x] `dm_mc02.c` 的 co-sim IMU 输入、寄存器量程/ODR 配置、QOM 噪声/偏置/温度
  属性和 reset 状态均通过该模块访问；SPI 命令、DMA request、FIFO 字节格式和
  guest-consumption token 保留在 DM-MC02 glue 层。
- [x] host CTest 直接编译真实信号层实现；覆盖 ODR 丢样、确定性噪声、bias、滤波
  阶跃和 raw 饱和，并通过 ASan/UBSan smoke。
- [x] 验证：QEMU 构建、BMI088 直读/滤波/FIFO/chip smoke、CTest `10/10`、Python `243 passed`。
- [x] BMI088 寄存器状态、采样状态和 FIFO 数据面已抽到独立的
  `DmMc02Bmi088` 芯片对象；`dm_mc02.c` 仅保留 SPI 片选/命令、dummy byte、地址递增、
  DMA 和 co-sim 消费通知。芯片对象通过 `DmMc02Bmi088ReadEvent` 报告完整 FIFO
  数据帧完成及其 `fifo_sample_sequence`，不依赖板级 `step_id`。
- [ ] 后续补齐 BMI088 accel FIFO INT tag、sample-drop frame、FIFO interrupt/真实
  DRDY 引脚映射、gyro 外部 tag 与精确 watermark/full 时序；继续校准完整器件滤波
  离散响应/群延迟和真实温度/噪声统计。当前已验证的 bias/noise、温漂系数、
  bias random walk、
  一阶带宽滤波、ODR 窗口和 raw 饱和不能代表这些完整器件模型能力。

## 2026-08-30 BMI088 电源状态语义

- [x] 芯片对象按真实 `ACC_PWR_CONF/ACC_PWR_CTRL` 和 `GYRO_LPM1` 判断 die 是否上电；
  断电或 suspend 时拒绝新的物理样本，不更新 raw/FIFO，并清除对应 DRDY 状态。
- [x] `dm_mc02_bmi088_is_powered()` 提供板级 adapter 可复用的只读状态查询，电源判断
  保留在 BMI088 芯片层，不把寄存器位复制到 DM-MC02 machine。
- [x] FIFO guest smoke 增加断电注入回归，并显式执行真实上电序列；验证断电时允许保留
  既有 config frame，但不得出现传感器 data frame，上电后原有 FIFO/sensor-time 断言继续通过。
- [ ] 仍未模拟上电稳定延迟、完整 power-domain 电流/电气行为和中断引脚映射；这些不应
  被当前 `is_powered` 布尔语义误认为完整器件电源模型。

## 2026-08-30 BMI088 芯片级 smoke 与构建边界

- [x] 新增 `tests/bmi088_chip_smoke.c`，直接验证芯片对象的 accel/gyro 电源门控、温度
  极值与 NaN、非法寄存器访问、FIFO partial read，以及只有真实 `0x84` accel 数据帧
  才产生 `fifo_frame_complete` 事件。
- [x] host CMake 只为该复用 QEMU 内部头文件的 smoke 目标启用 GNU C 扩展并撤销
  `NDEBUG`；其它 host 目标仍保持项目的严格 C11 配置。
- [x] 通过 Release host build、CTest `10/10`、QEMU build、QEMU smoke `65/65`、shell
  语法、Python compileall、`uv lock --check` 和
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --project . pytest -q`（`243 passed`）。
- [ ] QEMU 内部头文件在该独立 smoke 下仍会触发 `-Wpedantic` 的 GNU 扩展告警；这些
  告警来自 QEMU 宏，不代表 BMI088 行为失败，后续可在不降低正式 QEMU 构建诊断的
  前提下单独收敛测试目标的告警策略。

## 2026-08-30 worker Python/uv 入口统一

- [x] `tools/run-worker.sh` 成为 Null、MuJoCo 和 ROS2 worker 的统一启动入口；Null
  使用项目 uv 环境，MuJoCo 自动启用 `mujoco` extra，ROS2 保留已 source 的系统 Python。
- [x] `run-sim-worker-smoke.sh`、`run-mujoco-worker-smoke.sh`、`run-ros2-worker-smoke.sh`、
  `run-qemu-worker-smoke.sh` 和 `run-dm-motor-smoke.sh` 均通过统一入口启动 worker，
  移除重复的解释器/worker 路径传递。
- [x] 验证：五个 worker 相关 smoke 全部通过（Null、MuJoCo、ROS2、QEMU-native、DM-MIT），
  入口脚本语法检查通过；
  性能基线 NullEngine `202044 IMU frames/s`、worker 到 RESET `52.628 ms`、DM-MC02
  启动 smoke `49.679 ms`。
- [x] 新增协议中立的 `BackendRegistry`：内建 Null/MuJoCo/ROS2 通过隔离 factory 表
  创建；`--backend MODULE:FACTORY` 支持直接加载自定义 plant，
  `--backend-registry MODULE:INITIALIZER --engine NAME` 支持注册后选择自定义名称。
  registry 单测、worker 选择契约、五类既有 worker smoke 和独立 plugin smoke 均通过。
- [ ] 仍待实现：Gazebo 非 ROS 原生 plant adapter，以及跨进程严格锁步。

## 2026-08-30 v2 typed STEP 契约回归

- [x] QEMU C 端 `v2_step_payload_valid()` 对 `ADC_INPUT` 和 `ADC_VOLTAGE` 分别维护
  32 位通道位图，拒绝同一 section 类型内的重复 channel；两种不同类型仍可在同一
  STEP 中更新同一通道，和 Python validator 保持一致。
- [x] `run-qemu-v2-adc-only-smoke.sh` 增加重复 ADC channel 回归：非法 STEP 不推进
  validator，随后使用相同 step/time 的合法 STEP 成功通过。
- [x] 修正 `run-qemu-v2-motor-smoke.sh` 的测试时序：短 timeout 检查后恢复正常
  timeout，并根据非法 STEP 不提交的语义计算后续 `dt_ns`。
- [x] 验证：QEMU 增量构建、host CMake 构建、CTest `7/7`（QEMU smoke `63/63`）、
  Python `227 passed`、全部 shell 语法检查和 `uv lock --check`。
- [ ] 仍待实现：真实外部 motor plant callback 端到端绑定、完整混合 STEP 可回滚事务、
  断线无损恢复、多步发送窗口、完整 H723/USB/FIFO 语义。

## 2026-08-30 v2 motor endpoint 缺陷修复与回归

- [x] 修复默认无 motor handler 时绕过完整 STEP payload 校验的问题；非法
  `MotorCommand`、`MOTOR_STATE` 输入和非零 section flags 不再以 `UNSUPPORTED`
  推进 session。
- [x] 修复带 `MotorState` 的 STEP 只预留一个控制响应槽位的问题；解析阶段会为
  `STEP_ACK` 和 `MOTOR_STATE` 一起预留控制队列容量，避免 ACK 成功而反馈丢失。
- [x] 明确 callback 成功后返回非法状态是 `PROTOCOL`，不进入可重复执行的 queue retry；
  正常 callback 成功但后续 IMU/ADC 背压时继续复用缓存状态。
- [x] Python codec/validator 与 C 端统一 section flags 为保留值 0；
  `MotorCommand.flags` 仍允许 endpoint-defined 非零 metadata。
- [x] 增加默认关闭的 `cosim-motor-loopback` QEMU fixture，覆盖合法命令、反馈、重复
  STEP、非法 payload 不推进 session；新增 `run-qemu-v2-motor-smoke.sh`。
- [x] 验证：QEMU incremental build、motor smoke、CTest `7/7`、Python `209 passed`。
- [ ] 仍待实现：真实外部 motor plant adapter 的 callback 端到端接入、完整混合 STEP
  事务回滚、跨断线无损恢复和多步发送窗口。

## 2026-08-30 v2 motor endpoint 一致性与重试收敛

- [x] Python codec/validator 增加 `MOTOR_STATE` 和 `CAP_MOTOR`，并补齐消息类型、能力位、
  session 与 step 时间匹配测试。
- [x] QEMU motor command 校验允许 endpoint-defined 的非零 `MotorCommand.flags`，与 Python
  codec 保持兼容；runtime smoke 覆盖非零 flags。
- [x] QEMU link 保存最近一次成功 STEP 的 `MotorState`，重复 STEP 时重发 ACK 和状态；
  callback 已成功但混合 section 后续提交失败时，重试复用同一状态，避免 motor plant 重复调用。
- [x] 完成 Python `204 passed`、QEMU build、host CMake build 和基础脚本检查。
- [ ] 默认 `dm-mc02` machine 仍不绑定具体 motor plant；worker v2 尚未使用该 endpoint，
  默认电机闭环继续走 FDCAN。需要后续提供独立 plant adapter 和端到端 callback smoke。
- [ ] 混合 IMU/ADC/motor STEP 的整体事务提交仍需进一步收敛；当前只保证 motor callback
  在重试中不重复执行，不把所有 board side effect 变成可回滚事务。

## 2026-08-30 co-sim 时间基准与重连收敛

- [x] 为 QEMU 保存最近一次已协商的 v1/v2 接收协议；chardev 断开时只清理当前
  validator，不丢失重连握手所需的 wire version。
- [x] 增加 v2 chardev 真实断开、重连、RESET/RESET_ACK 和首个 STEP 的端到端 smoke，
  覆盖新 session ID 与 telemetry。
- [x] v2 worker 从 RESET_ACK 的 QEMU virtual clock 建立 `QemuTimestampMapper`，将
  QEMU FDCAN timestamp 转换到 worker step epoch；运行中 QEMU reset 重新建立 identity
  epoch，v1 保持原有共同时间基准。
- [x] `--ros-wait-imu` 在没有历史样本时等待第一条带有效时间戳的 IMU，超时后保持
  有界等待行为；补充 worker 单测和 timestamp mapper 契约测试。
- [x] 验证：QEMU 构建、v2 reconnect/reset smoke、host CTest `7/7`、uv pytest
  `193 passed`。
- [x] v2 STEP ACK 增加有界超时重发；QEMU 缓存最后一个已成功 STEP 的完整指纹，重复
  STEP 只重发 ACK，不重复注入 IMU/ADC；重连和重复 STEP 均有 smoke 覆盖。
- [x] 严格 `STEP_DONE` 支持同一 session 内恢复：QEMU 缓存最近完成的消费确认，worker
  在 DONE 超时后重发同一 STEP，validator 不重复创建 outstanding 消费事务。
- [x] 性能回归：NullEngine `184950 IMU frames/s`，Release 固件 RTF `0.999771x`，
  QEMU CPU 约 `102.9%`；协议修复未破坏当前实时能力。
- [ ] 仍待实现：断线后的无损恢复、多步发送窗口和跨进程持久化状态；统一 QEMU、worker、
  MuJoCo/ROS2 的 step coordinator 接口，并继续补齐 board profile 与 SoC 外设边界。

## 2026-08-30 v2 session 与 ADC-only STEP 修复

- [x] v2 保留 36-byte header，将原 `flags:u32` 复用为可选非零 session ID；QEMU、worker
  和 Python validator 在 RESET 后严格回显并拒绝旧 session，v1 wire 保持兼容。
- [x] 修复运行中 QMP `system_reset` 的 v2 RESET/RESET_ACK session 同步，并修正 reset
  smoke 末尾错误发送 `flags=0` v2 STEP 的测试缺陷；旧 session 拒绝和正确 session 恢复均有
  端到端回归。
- [x] 修复 v2 section payload 强制要求 IMU 的校验缺陷；ADC_INPUT/ADC_VOLTAGE 可以单独
  组成 STEP，新增 QEMU ADC-only smoke。
- [x] 定向验证：runtime reset、ADC-only、STEP、STEP_DONE、控制队列背压和 telemetry
  重试 smoke 全部通过。
- [x] 完整验证：QEMU 构建、host CTest `7/7`（含全量 QEMU smoke）、uv pytest `186 passed`、
  `bash -n tools/*.sh` 和 `uv lock --check` 均通过。
- [ ] 仍待实现：ACK 超时/重传与无损恢复、v2 电机 section endpoint 消费、完整 H723/USB/
  FIFO 语义，以及跨 Gazebo/MuJoCo backend 的严格锁步。

## 2026-08-30 功能缺陷修复与板级输出解码

- [x] 修复 BMI088 gyro FIFO 在 stop-at-full 满 100 帧后切换 stream 模式时的
  `uint16_t` 容量下溢；切换时按完整帧淘汰，FIFO 长度保持在有效上限内。
- [x] 提取可复用的 FIFO 容量/空间判断接口，并增加 stop-to-stream 专项回归。
- [x] 新增纯数据型 `dm_mc02_board_decode_gpio_outputs()`，由 board profile 统一
  解码电源、BMI088 CS、LED、蜂鸣器 GPIO/AF 和 RS485 DE；machine 只消费解码结果。
- [x] 将蜂鸣器 GPIO alternate function 从 machine 硬编码移入 profile，并增加 profile
  校验；初始化和运行时 GPIO 变更共用同一输出传播路径。
- [x] 验证：QEMU 构建通过；CTest `7/7`（含 QEMU suite 内部 `61/61`）；uv pytest
  `180 passed`；`bash -n tools/*.sh`、`uv lock --check`、BMI088 FIFO、RS485、PWM、GPIO
  定向 smoke 全部通过；未修改 `trobot/`。
- [ ] 剩余高价值功能：ACK/重传，v2 Motor section 的 endpoint 消费，
  完整 H723/USB/FIFO 中断语义，以及真实外部总线的无损锁步。

## 2026-08-30 GPIO 回调解耦

- [x] GPIO ODR changed callback 现在携带 `bank_index`，公共 GPIO 模型保存自身 bank
  索引并在通知时传回；`dm_mc02.c` 删除 8 个 bank-specific wrapper，所有 GPIO bank
  共用一个机器层回调。GPIO ODR/IDR、BMI088 CS、电源、RS485 DE、LED dirty 和 telemetry
  行为保持不变。
- [x] QEMU 构建、GPIO、RS485、电源/VIN、BMI088 直读/FIFO、v2 reset 定向 smoke 通过；
  host CTest `6/6`（含 QEMU smoke `61/61`）通过。
- [x] 后续已补充纯数据型 board GPIO output decoder；profile 一致性校验和 machine
  侧统一输出传播路径也已完成。

## 2026-08-30 board profile 初始化校验

- [x] 增加 `dm_mc02_board_validate()`，在 machine realize 前校验 SoC GPIO/IRQ 容量、
  timer/UART/FDCAN 数量、serial slot 冲突、器件 pin、BMI088 DMA、RS485 DE 和 DMA UART
  route 索引；错误配置在初始化阶段失败，不进入仿真热路径。
- [x] 将 machine 的 GPIO bank 容量常量统一到 board 公共头文件，并在 `dm_mc02_init()`
  中调用 profile 校验。
- [x] 当前 DM-MC02 profile 初始化、关键外设 smoke、QEMU 构建和完整回归通过。
- [x] 将 CPU reset/ref 时钟的所有权明确留在 SoC profile，移除 board profile 中未被
  machine 使用的重复字段；本轮校验不改变现有 profile 的运行时行为。
- [x] 纯 GPIO 输出解码已整理为 board profile 公共 API；其它重复地址字段仍可在后续
  profile 扩展时继续清理，但不影响当前功能。

## 2026-08-30 telemetry 背压修复

- [x] 修复 co-sim telemetry 在 v2 控制响应占满 TX 队列时只记录失败、但没有自动重试的问题；
  保存最新待发送状态，并在控制队列排空后由 TX timer 重试。若状态回到最近一次已发送快照，
  pending 状态会正确清除。
- [x] 增加 `run-qemu-v2-telemetry-backpressure-smoke.sh`：用 QMP 确认 8 个控制槽已满，
  qtest 写入 PC13，验证控制响应完整保序且最终 `board_flags` telemetry 到达。
- [ ] 仍待实现：session ID、ACK/重传、v2 电机 section、完整 H723/USB/FIFO 语义和
  跨 Gazebo/MuJoCo backend 的严格锁步。

## 2026-08-30 v2 异步 reset 与 telemetry 背压修复

- [x] 修复 worker 收到 QEMU 运行中 `RESET/RESET_ACK` 后误报
  `unexpected v2 response kind` 并退出的问题；现在会重置 host validator、plant、
  `StepCoordinator`、步计数和时间 epoch，从新 session 继续运行。
- [x] 为 Null/MuJoCo/ROS2 backend 增加保留实例配置的 reset 操作；ROS2 不重建 node，
  MuJoCo 不重载 model，减少 reset 对外部连接的影响。
- [x] 修复 QEMU telemetry 在 TX 控制队列满时仍更新 `last_telemetry` 的问题；只有
  telemetry 成功进入队列后才标记为已发送。
- [x] 增加异步 reset worker 回归；验证 QEMU 构建、v2 reset/STEP_DONE、BMI088 FIFO、
  CTest `6/6`、uv pytest `148 passed`、shell 语法和 `uv lock --check`。
- [x] 补充 telemetry 满控制队列的独立端到端 oracle；控制队列满载时改变 PC13，验证
  延迟 telemetry 在控制响应排空后自动重发。
- [ ] 仍待实现：session ID、ACK/重传、完整 v2 电机 section、完整 H723/USB/FIFO
  语义和跨进程严格锁步。

## 2026-08-30 当前功能修复

- [x] 完成 v2 guest-consumption `STEP_DONE`：QEMU 将 IMU step 绑定到消费 token，
  在 BMI088 accel/gyro 完整 direct raw burst 或 FIFO data frame 读完后发送二阶段确认；
  reset/reconnect 会清理旧 token。
- [x] Python worker 增加方向化 v2 session validator 和可选 `--wait-step-done`，
  支持同一 step 的 `STEP_ACK -> STEP_DONE`，默认保持旧 QEMU 兼容。
- [x] 增加真实 guest BMI088 消费 smoke，并修复 co-sim 控制帧不应被 telemetry
  淘汰、BMI088 sensor-time 长时间乘法溢出的问题。
- [x] 验证：QEMU build、严格 worker/STEP_DONE smoke、QEMU smoke `59/59`、
  CTest `6/6`、uv pytest `147 passed`、shell 语法检查和 `uv lock --check`。
- [ ] 后续仍需实现 session ID、ACK/重传和更完整的 FIFO 中断/丢帧语义。
- [x] BMI088 FIFO data frame 增加与 FIFO 字节环并行的 sample sequence 元数据；
  多样本 FIFO 消费确认按实际帧序号发送，v2 token 队列满时返回 `QUEUE_FULL` 并暴露
  `consume_dropped` 诊断。

## 2026-08-30 当前复核结果

- [x] 修复运行中 QEMU reset 后 v2 session 未同步的问题，并用
  `run-qemu-v2-reset-smoke.sh` 验证 reset 后第一条 STEP 可被接受。
- [x] 修复 v2 `RESET_ACK` 未声明 ADC capability，以及 ADC 队列溢出误计入
  `imu_dropped` 的诊断问题。
- [x] 回归通过：QEMU build、v2 STEP/RESET smoke、CTest `6/6`、uv pytest
  `128 passed`。
- [x] 增加并接入 host-side `DmMotorBusAdapter`，保留 guest FDCAN wire-faithful
  默认路径，并增加独立 MIT/float/反馈映射测试。
- [x] 增加 `StepCoordinator`，集中管理带虚拟时间戳的 CAN 命令排序、backend
  应用和 plant step 边界，并通过单测及 worker smoke 验证。
- [x] guest-consumption `STEP_DONE` 已完成；session ID/可靠传输和跨
  Gazebo/MuJoCo backend 的统一 step coordinator 仍待实现。

## 2026-08-30（v2 step section 输入扩展）

- [x] QEMU co-sim link 增加 v1/v2 版本分流；v2 支持 `RESET`、严格递增的 `STEP`、
  `RESET_ACK`、`STEP_ACK` 和 diagnostics，STEP 的 60-byte `ImuSampleV2` 及 section
  payload 接入已有 BMI088/ADC 路径；v1 默认 wire 和实时路径保持兼容。
- [x] worker 增加 `--protocol v2`，以 plant virtual time 生成 step，等待对应 ACK 后
  再进入下一步；ADC raw/voltage 作为 v2 section 发送，不再混用 v1 frame。
- [x] 增加 section/ADC codec 单测：25 条 payload 测试通过。
- [x] 增加 QEMU/worker v2 端到端 smoke；串行全量 QEMU smoke `56/56`、host CTest
  `6/6`、uv pytest `88 passed`（section 测试加入后待最终回归更新）。
- [x] 重新采集 v1 性能基线：NullEngine `199604 IMU frames/s`、worker 启动到 RESET
  `46.248 ms`、DM-MC02 启动 smoke `53.811 ms`；连续运行 v2 smoke 两次通过。
- [ ] 继续补齐 v2 的 MotorCommand/MotorState、session ID、消费确认、ACK/重传和跨
  Gazebo/MuJoCo backend 的统一 adapter；ADC input 已完成 QEMU 队列化交付。

## 2026-08-30（架构复用与实时路径收敛）

- [x] 修复板级 IRQ profile 将 TIM2 与 TIM3 隐式共用 `timer[0]` 的缺陷：TIM2
  使用独立的 `irqs.tim2` 字段，TIM3 使用 IRQ29；新增 TIM3 裸机 IRQ/UIF/W0C smoke，
  避免只验证 TIM2 就误判所有定时器中断正确。
- [x] 将 BMI088 active-low CS 的启动和 reset 复位逻辑统一为 profile-aware helper，
  按 GPIO bank 合并 ODR 更新；当前 DM-MC02 的 PC0/PC3 行为保持不变，未来 profile
  可将两个 die 放到不同 GPIO bank。
- [x] 优化 QEMU co-sim RX 缓冲：增加消费游标，普通逐帧解析不再执行 `memmove`，
  仅在尾部空间不足时压缩；保持 v1 wire、有限缓冲和非阻塞语义不变。
- [x] 补充 ADC ADRDY 生命周期和 ADC12 common CCR 时序 qtest，确保寄存器读回不
  等同于错误的 ready 状态。
- [x] 将 timer/UART/FDCAN 的 IRQ 绑定放回各自 board route，profile 重排外设时不会
  因独立 IRQ 数组索引错配；SoC 级 EXTI/ADC/DMA IRQ 仍由公共 wiring map 提供。
- [x] 增加初始化前 `board-profile` machine 属性并在初始化后锁定，默认选择
  `DM-MC02`；当前注册表保留独立 lookup，后续同 SoC 开发板可只新增 profile 数据。
- [x] 局部验证：QEMU 构建、TIM2/TIM3 IRQ、BMI088、peripheral-reset、co-sim
  link/timing/readback 和 host codec smoke 通过。
- [ ] 下一步：增加第二个同 SoC board profile 作为复用验收，并把外部 backend 注册接口
  与 v2 STEP/ACK 控制面接入；继续保持 v1 wire 和实时默认路径兼容。

## 2026-08-30（供电路径回归修复）

- [x] 修复 GPIOC PC15 运行时切换没有传播到板级收发器的问题：GPIOC ODR 回调现在
  会立即更新 FDCAN 和 USART2/USART3 RS485 的 switched-5V 供电状态；新增 FDCAN
  端到端 smoke 验证收发器断电期间丢帧、恢复后继续收发且 MCU 不复位。
- [x] 验证：QEMU smoke `55/55`，host CTest `6/6`，uv pytest `24 passed`。

## 2026-08-30（QEMU 工具化收敛：时间、芯片层时序与链路诊断）

- [x] 修复 VIN=0 仍允许 Cortex-M7 执行的电源边界缺陷：VIN=0 现在会复位并保持
  MCU halted，恢复有效 VIN 后从复位入口重新启动；新增启动时掉电、运行中掉电和恢复
  的端到端 guest/QMP smoke，并暴露只读 `mcu-power-good`。
- [x] 修复 ROS2/Gazebo 外部时间戳回退后的映射恢复：新 epoch 以单调输出为锚点，
  后续样本恢复使用外部时间增量；新增 worker 回归覆盖 reset、fallback 和重复样本。
- [x] 将 BMI088 ODR 从整数 Hz 改为可复用的纳秒周期表，修正 accel 12.5 Hz 为 80 ms；
  独立芯片层头文件和 host CTest 覆盖全部 accel/gyro 编码及无效编码。
- [x] 增加 QMP `qom-get /machine cosim-diagnostics`，导出 link 开关、连接状态、收发、
  短写、丢帧和队列占用计数；co-sim v1 仍不提供 ACK/重传。
- [x] 验证：host CTest `6/6`、uv pytest `24 passed`、QEMU smoke `55/55`；
  `trobot/` 未修改。
- [ ] 下一步：按 SoC 公共模块 / board profile / 外部 backend 三层继续拆分，随后实现
  co-sim v2 session、ACK、STEP 和消费确认；保留实时默认路径和 v1 wire 兼容。

## 2026-08-30（功能缺陷审计与接口边界收敛）

- [x] 修复 board profile wiring 的 reset 边界：BMI088 两个 active-low CS
  即使位于不同 GPIO bank 也会分别恢复为高电平；RS485 DE 状态刷新统一由 board
  GPIO 变更入口触发，profile 不再隐含依赖 GPIOB/GPIOD 回调。
- [x] 修复 RS485 供电仍依赖 UART 数组下标的问题：现在按 `route.rs485` 选择
  外部收发器，UART route 重排不会给错误串口断电或上电。
- [x] 验证：QEMU 构建通过，独立 QEMU smoke `55/55`，host CTest `6/6`，
  `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q` 为 `25 passed`。

- [x] FDCAN host wire 严格拒绝 DLC 高四位脏数据；新增 classic CAN `DLC=0x10`
  回归，避免它被误当作零长度帧。
- [x] SocketCAN bridge 对 CAN-FD ESI 和未知 native flag 显式报错；v1 固定 wire
  暂无 ESI 表达位，不再静默丢失状态。
- [x] QEMU worker smoke 等待 guest BMI088 配置完成，使用 `--realtime` 发送有限样本，
  并通过 guest RAM sensor-time 断言数据实际被 guest 消费。
- [x] ROS2 smoke 增加 `rclpy` 预检和 5 秒连接超时，依赖缺失时 skip，启动异常不会挂死。
- [x] 验证：QEMU smoke `55/55`，串行 CTest `6/6`，uv pytest `24 passed`。
- [x] 修复 co-sim 断连时提前清理 future IMU 队列的问题：已接收样本继续按虚拟时间
  交付，新连接或 machine reset 才清理旧 session。有限非实时 burst 不再因正常断连
  直接丢失；v1 仍无 ACK/重传保证。SocketCAN 外部节点 ACK 尚不能反馈到 QEMU；ROS2
  不按 IMU header 时间戳配对。
- [x] 增加显式 `fdcan-host-ack` machine/QOM 策略：连接的 host backend 可作为外部
  ACK 参与者，默认关闭以保留无 ACK 故障注入；FDCAN host chardev smoke 已验证 TEC 不增长。

# 2026-08-29 已确认缺陷收敛

- [x] 修复 USB 描述符端点与内部测试队列不一致：内部 packet harness 现在覆盖真实
  描述符使用的 EP1..EP5，IN/OUT 完成状态和 `DAINT` 端点位按实际 endpoint 编号更新，
  复位 `DAINTMSK` 覆盖 EP0..EP5；qtest 扩展为 4/4。该修复不等同于真实 USB 枚举、
  PHY 或宿主机 USB 挂载。

- [x] 修复 USB EP1 OUT 默认中断摘要缺陷：复位 `DAINTMSK` 由 `0x00010003` 修正为
  `0x00030003`，EP1 OUT 的 `DOEPINT1` 现在可通过 `GINTSTS.OEPINT` 正确上报；新增
  qtest 全局中断断言。QEMU 构建、USB qtest、CTest 和 pytest 已通过。USB 完整枚举、
  端点和 PHY 仍明确属于未实现能力。

- [x] 接入 BMI088 双 die FIFO 的基础真实 SPI 数据面：accelerometer `FIFO_LENGTH`/
  `FIFO_DATA`、1024-byte header FIFO、`FIFO_DOWNS` 与 stream/stop-at-full；gyro
  `FIFO_STATUS`/`FIFO_DATA`、100-frame/8-byte frame FIFO 与 stream/stop-at-full。FIFO 只在已接受的
  co-sim 样本写入，未引入默认高频 QEMU timer。新增 `run-bmi088-fifo-smoke.sh`：覆盖三
  个按虚拟时间注入样本、accel 21-byte/gyro 3-frame fill level、accel partial-frame
  repeat、FIFO drain 与 overread。全量 `run-*-smoke.sh`、CTest、pytest 及 Release
  RTF 回归均通过；后续仍需 FIFO INT tag/sample-drop frame 和真实 FIFO interrupt
  pin。

- [x] 补齐 FDCAN 粗粒度 BUS-OFF：无 ACK 按帧级 TEC 增量，达到阈值后设置 `PSR.BO`/
  `IR.BO`、进入 `CCCR.INIT`、停止 medium 参与并完成 pending TX；显式 `INIT -> normal`
  重初始化清除 BO/TEC。新增 BUS-OFF 与恢复 smoke；完整 error frame、位级重试和物理层
  recovery 仍 pending。
- [x] 性能采集工具固定使用当前 `build/qemu/qemu-system-arm`，移除历史
  `build/qemu-release` 自动回退；实测 `tcg thread=multi`/不同 TB cache 未显示稳定收益，
  不改变默认启动参数。

- [x] 修复 EXTI 输入 IRQ 重入时序：先锁存 `line_level` 再分发 IRQ，SYSCFG
  `EXTICR1..4` 运行时改写立即传播当前 GPIO 输入；新增 GPIOA→GPIOB 动态复用 smoke。

- [x] 补齐 EXTI0--15 的常用功能路径：RTSR1/FTSR1 边沿选择、SWIER1 软件触发、
  PR1 写 1 清除、C1IMR1 屏蔽和 EXTI0--4/9_5/15_10 到 NVIC 的 level IRQ；新增
  bare-metal smoke 验证 IRQ40 和 pending 生命周期。全量回归达到 52/52 smoke、
  CTest 4/4、pytest 16/16；EXTICR 动态复用变更传播已补齐并有专项覆盖。
- [x] 接入 DM-MC02 PA15 active-low 用户键：默认松开为上拉高电平，支持
  `user-key` machine/QMP 属性，按键边沿经 GPIO 输入和 EXTI15_10 触发；新增测试覆盖
  运行时按下和 GPIOA IDR 读回。
- [x] 增加通用 GPIO 外部输入注入：支持 `gpio-input=PORTPIN=0|1` 的启动/QMP 接口，
  输入值写入对应 GPIO IDR，并按当前 SYSCFG EXTICR 端口选择驱动 EXTI0--15；新增
  A14 共享 EXTI15_10 的运行时输入 smoke。

- [x] 修复 FDCAN smoke 的验收用例缺陷：priority-only filter 使用 `0x321` 后，
  电源恢复步骤改用 FIFO filter 的 `0x456`，避免把合法 HPMS 帧误当作 FIFO 恢复失败。
  修复后全量 51 个 QEMU smoke、CTest 4/4、pytest 16/16 通过。

- [x] 完成当前 Release 固件热点采样：稳定运行阶段主要是单 Cortex-M7
  TCG 执行、FreeRTOS busy idle loop、SysTick/TIM2 周期中断和少量 DMA 请求，
  没有证据表明增加 host 多核可以线性提速。
- [x] 对 DMA、ADC、UART、FDCAN 和 TIM 的 level-sensitive IRQ 输出增加电平缓存；
  电平未变化时不重复进入 QEMU IRQ fan-out，同时保留状态位、IRQ 边界和 reset
  初始电平语义。全量 smoke、CTest、pytest 回归通过。
- [x] 本轮构建与验证：`tools/build-qemu.sh`、QEMU smoke `51/51`、CTest `4/4`、
  pytest `16 passed`；真实 Release 固件 3 s 样本 RTF `1.000004x`，CPU `101.6%`，
  IWDG timeout `0`。该结果说明当前环境达到实时，但尚未形成稳定性能余量。

- [x] 修正 FDCAN `IR` 的 H723 位定义：RF0F/RF1N/RF1F 从旧的错误偏移改为
  bit2/bit4/bit6，并补齐 FIFO 高优先级过滤器的 `HPMS`/`IR.HPM` 状态；扩展
  FIFO1 smoke 现按 CMSIS 真实值验收。
- [x] 修正 FDCAN dedicated Rx Buffer 已有 `NDAT` 时的过滤器路径：命中过滤器但
  目标 buffer 忙的帧现在丢弃，不再错误回退到 GFC 的 FIFO 路由；专项 smoke 新增
  重复帧、NDAT 释放后重收和扩展 ID 回归。
- [x] 修复 `run-dm-motor-smoke.sh` 和 `run-peripheral-reset-smoke.sh` 默认优先
  选择过期 `build/qemu-release/qemu-system-arm` 的工具缺陷；现在默认使用当前
  源码构建 `build/qemu/qemu-system-arm`，仍可用 `QEMU_SYSTEM_ARM` 显式覆盖。
  重新构建后电机闭环、外设 warm-reset、全量 QEMU smoke 均通过。
- [x] 本次复核：`tools/build-qemu.sh` 通过；QEMU smoke `51/51`，host CTest
  `4/4`，uv pytest `16 passed`；Release 固件 RTF `0.999810x`、CPU `101.9%`、
  IWDG timeout `0`；NullEngine `162804 frame/s`。
- [x] 补齐 FDCAN dedicated Rx Buffer：按 H723/M_CAN 的 `SFEC/EFEC=7` 精确 ID
  过滤、`ID2[5:0]` buffer index、`RXBC/RXESC` 地址与元素大小、`NDAT1/2`
  W1C 和 `IR.DRX`；同时修正 `SIDFC/XIDFC.FLSSA` 的 word-address 解释。新增
  `run-fdcan-rx-buffer-smoke.sh`，全量 QEMU smoke 达到 `51/51`。
- [x] 进一步收敛 FDCAN Message RAM 地址字段：`TXBC/RXF0C/RXF1C/RXBC` 和
  `SIDFC/XIDFC` 均忽略硬件保留低位，扩展 dedicated Rx Buffer smoke 覆盖
  `NDAT1` buffer 5 与 `NDAT2` buffer 32；最新全量回归 `51/51`。

- [x] 修复 FDCAN TX 状态观测：`TXFQS.TFFL/TFGI/TFQPI/TFQF` 按尚未完成的
  virtual-time 发送更新，新增 `TXBRP` pending 位；断电时 pending 请求以粗粒度
  no-ACK 完成，避免 guest 永久等待。扩展 FDCAN medium smoke 已覆盖发送前/完成后
  状态。
- [x] 本轮验证：全量 `tools/run-*-smoke.sh` 通过；FDCAN 定向状态 smoke、CTest
  4/4、uv pytest 16 passed；Release 固件 RTF `0.999606x`，NullEngine
  `205625 frame/s`，QEMU 启动 smoke `51.984 ms`。

- [x] 补齐 FDCAN 扩展过滤器和 FIFO1：支持扩展 range/dual/mask 过滤、EFEC
  路由、全局非匹配策略、FIFO1 状态/释放和 RF1N/RF1F；新增扩展 ID + FIFO1
  端到端 smoke，标准 FIFO0/CAN-FD 路径保持通过。
- [x] uv dev 环境显式锁定 pytest；`env -u PYTHONPATH uv run --group dev
  python -m pytest -q` 可稳定运行，避免 ROS2 全局 pytest 插件污染项目测试。
- [x] 实现 FDCAN 标准过滤器和全局接收策略：支持标准 ID 的 range/dual/mask
  匹配、FIFO0 路由、非匹配帧拒绝、远程帧拒绝和 Rx element FilterIndex/ANMF
  字段；新增有效非匹配帧专项覆盖。全量 smoke 仍为 49/49，Release RTF 约
  `0.999854x`。
- [x] 新增 TIM2 动态 RCC 改频专项 smoke：验证改频时 CNT 连续，并按新
  `TIMERCLK` 重新计算首个 update 的剩余时间；修复了 deadline 被重置为完整
  新周期的时序缺陷。

- [x] 修复 RCC 动态改频时定时器首个周期仍使用旧 TIMERCLK 的时序缺陷；保持 CNT 连续后按新时钟重排绝对 deadline。
- [x] 为 TIM2/TIM3/TIM8/TIM12/TIM24 缓存有效 update interval，配置/时钟/batch 变化时失效，减少定时器虚拟事件的重复计算。
- [x] 将 CAN medium 的 pending 调度由固定数组 O(N²) 扫描改为固定容量最小堆，保留时间戳、CAN ID、注册顺序和 sequence 仲裁顺序。
- [x] 为 Python 项目设置 pytest testpaths，避免 ROS2 launch_testing 误扫描 QEMU 上游测试；worker 发送队列减少重复 flush 和不必要的数据复制，并为整数采样率使用精确整数时间路径。
- [x] 修正 PLL1M1 六位 divider 的掩码；原四位掩码只在当前 M=2 板级配置下正确。
- [x] 修复 FDCAN host ingress 丢失外部 virtual timestamp 的问题。
- [x] 修复 FDCAN TX DLC 超过 message element 容量时越界读取后续元素的问题。
- [x] 修复 UART `TC` 提前置位，令其与 host-facing TX queue 清空状态一致。
- [x] 统一 Null/MuJoCo/ROS2 legacy float 电机命令的 enable gate 与 torque limit。
- [x] 处理 FDCAN medium 满队列时的 TX completion，并补充 transport pending-send 误用测试。
- [x] 修复 smoke 入口权限并完成全量回归与 Release RTF 复测。

# 2026-08-30 架构解耦与外部时间映射

- [x] 将 EXTI/ADC/Timer/FDCAN/UART/DMA 的 IRQ vector wiring 从 machine 代码移入
  `DmMc02BoardProfile.irqs`；芯片模型保持 board-independent，新增 profile 即可复用
  组装流程。
- [x] ROS2 `sensor_msgs/Imu.header.stamp` 建立 external-time origin，并在重复、回退或
  缺失时间戳时使用严格单调的固定步长 fallback；新增 worker 单测。
- [x] 验证：QEMU 构建通过，CTest `5/5`，uv pytest `23 passed`，Release 固件 RTF
  `0.999827x`、有效 tick/s `999.827`、IWDG timeouts `0`。
- [ ] 下一步优先实现 co-sim v2 的 step/ACK/diagnostics 契约，以及进一步拆出通用
  H723 SoC wiring，避免把当前单板 GPIO/电源行为继续扩大到芯片模型。

# DM-MC02 QEMU 独立后端计划（M0-M3）

## 2026-08-29 BMI088 动态滤波

- [x] 按 BMI088 寄存器配置识别加速度计 OSR4/OSR2/Normal 与陀螺仪 8 档带宽。
- [x] 在虚拟时间上对三轴物理输入施加稳定的一阶低通响应，再量化到 SPI raw 寄存器；首样本直接初始化，避免启动瞬态污染既有兼容路径。
- [x] 增加 116 Hz/23 Hz 阶跃响应专项 smoke，验证 raw 数据收敛和相同物理时间内低带宽衰减。
- [ ] 继续补齐 BMI088 FIFO 的 INT tag、sample-drop frame、FIFO interrupt/真实
  DRDY GPIO 映射、gyro 外部 tag 与精确 watermark/full 时序；config/skip/sensortime
  基础 frame 已由芯片对象实现。动态温漂和 bias random walk 仍待实现。

## 2026-08-29 本轮功能修复

- [x] FDCAN host TX 在 chardev 尚未连接时先进入已有有界队列，连接建立后续传，修复 worker 启动竞态造成的 DM 首帧丢失。
- [x] DM-MIT worker 闭环 smoke 收尾逻辑修复；Null/MuJoCo/ROS2 engine 统一 enable gate，Null/MuJoCo 遵守配置扭矩上限，MuJoCo 执行位置/速度/Kp/Kd 控制项。
- [x] 新增 DM-MIT engine 回归断言；uv 环境下 Python 电机测试 9/9、QEMU smoke 49/49、host CTest 4/4 通过。
- [x] 最新回归性能：NullEngine `210041 frame/s`、worker 启动到 RESET `41.758 ms`、QEMU 启动 `52.138 ms`；真实 Release 固件 3 s RTF `0.999647x`、约 999.647 tick/s、CPU 102.0%、IWDG timeouts=0。
- [x] ROS2 worker 增加可配置 `sensor_msgs/JointState` 输入（默认 `/dm_mc02/joint_states`），按索引提供电机 position/velocity/effort；ROS2 DM-MIT 控制、反馈和 enable gate 已通过运行时检查。
- [x] 新增 `tools/run-worker.sh` 统一入口：Null/MuJoCo 自动使用 uv 项目环境，ROS2 使用已 source 的系统 ROS Python，避免 uv 隔离环境找不到 `rclpy`。
- [x] FDCAN 84-byte host wire 接收端严格校验 reserved、flags、CAN ID、DLC 和非法 CAN-FD 组合。
- [x] worker `--realtime` 使用初始 virtual-time 相对原点，避免非零 QEMU 时间基准造成启动延迟。
- [x] 针对上述边界增加 FDCAN integration smoke 和 worker 单元回归。
- [x] FDCAN strict electrical-power smoke 覆盖 VIN 掉电丢帧与恢复收发。

## 2026-08-29 当前回归状态

- [x] 本轮优化后 Release QEMU 重新构建；定向 TIM/CAN smoke、CTest 4/4、Python 15 passed/1 skipped 和全量板级 smoke 回归通过（49/49），Release 固件 RTF 实测 `0.999912x`，IWDG timeouts=0。
- [x] TIM12_CH2/PB15 PWM 输出采用虚拟时间惰性计算，蜂鸣器 telemetry 改为检查真实 GPIO 复用/PWM 有效状态，并新增 QMP QOM 观察属性和独立 PWM smoke；未创建逐边沿 host timer。
- [x] worker co-sim/FDCAN/SocketCAN 发送改为有界非阻塞队列；部分写入不会阻塞仿真主循环，正常结束会限定时间排空。
- [x] 修复 `tools/build-smoke.sh` 执行权限，并新增 worker host-queue CTest；host CTest 现为 3/3。
- [x] 定时器 update 事件补齐 UIF、UIE 电平 IRQ 和写零清除语义，并接入 TIM2/TIM8/TIM12/TIM24 的 H723 IRQ；新增 TIM2 HAL timebase smoke。
- [x] 修复 MuJoCo smoke 对启动目录的隐式依赖：使用 `uv run --project`，现在从项目外目录执行也能正确加载 MuJoCo extra。
- [x] 修复真实 H7 SPI `SR.EOT`/`IFCR.EOTC` 缺失及 TXDR 打包写语义；真实 Release `trobot.elf` 已越过 BMI088 初始化并持续运行。
- [x] 回归通过：44/44 QEMU smoke、host CTest 3/3、`bash -n`、Python `compileall`、`uv lock --check`。
- [x] 性能基线：NullEngine `331499 frame/s`，worker 到 RESET `47.615 ms`，QEMU 启动约 `51.744 ms`；Release 固件 1 秒样本 `0.999979x`、`999.979 tick/s`、CPU 约 `101.9%`、RSS `50600 KiB`，IWDG `timeouts=0`。
- [x] 补齐 CRC 常用计算子集，并加入 CRC-32/CRC-8/CRC-16 向量与 reset smoke；RNG 复位也纳入 machine reset。
- [x] VIN 已支持 `vin-mv` machine/QMP 属性，运行时修改会更新电源策略，reset 保留外部供电值；新增 VIN 配置 smoke。
- [x] 完善 warm reset：CORDIC、USB 控制器寄存器、EXTI、SYSCFG、DBGMCU、FMC 均恢复模型复位状态；新增跨复位保留未消费 CORDIC 结果和修改后 USB FIFO 配置的独立 smoke。
- [x] 修复 co-sim `rx_dropped_bytes` 将正常消费帧误计为丢弃字节的问题。
- [x] 修复 DM 多电机有效控制/反馈 ID 冲突，及 ROS2 `JointState` 缺失字段沿用旧反馈的问题。
- [x] 新增 `run-peripheral-reset-smoke.sh`，验证 warm reset 后 CORDIC 未消费结果和 USB `GRXFSIZ` 修改不会泄漏；最新全量回归为 49/49 smoke、4/4 CTest。
- [x] DMA 基础 peripheral-request 路径增加 DBM/M1AR/CT 双缓冲交替、每 buffer NDTR 重装和 TC/HT 状态保持；新增 `run-tim8-dbm-smoke.sh`，验证 TIM8→DMA2 Stream6 的 M0→M1 顺序和 CT/指针状态。
- [ ] 仍需处理：完整 H723 外设语义、USB 枚举/端点、真实 RNG、DMA 完整 FIFO/FE/DME/仲裁、ADC 完整时钟/触发矩阵、BMI088 FIFO INT tag/sample-drop/真实 DRDY/动态温漂与 bias random walk、TIM 逐边沿 PWM、FDCAN error frame/物理层 recovery、具体 Gazebo 机构验收，以及 co-sim ACK/重传与无损诊断。

## 状态

- M0：基础目录、版本锁定格式、工具链探测、host probe：已实现
- M1：QEMU machine 注册、源码构建入口和 `-machine none` smoke：已实现
- M1：Cortex-M7、Flash/SRAM 内存映射、向量表加载和独立 guest 启动：已实现并通过 QMP smoke
- M1：STM32H723 外设：进行中（已接入 GPIO、SPI2、PWR/RCC、DMA/DMAMUX、定时器窗口、UART/FDCAN/ADC/OCTOSPI2/CORDIC 及启动兼容子集；仍有其它 DMA 外设和更多精确数据面 pending）
- M1：PWR/RCC 最小模型和 Release 固件初始化回归：已实现并通过 bare-metal smoke；Release 固件已越过 `ExitRun0Mode`
- M2：SPI2/GPIOC 片选、双 BMI088 ID/静止 raw 数据、加速度计 dummy byte、EOT/W0C 和 TXDR 打包写：已实现并通过 QMP smoke；真实 `trobot.elf` 已越过 BMI088 初始化
- M2：DMA/DMAMUX 及真实 `trobot.elf` 启动：DMA/DMAMUX、ADC1 按真实 H723 `SQR1` channel/rank 采样、SMPR1/SMPR2 rank-level 采样时间、软件启动和 TIM8 更新外部触发、ADCAL 虚拟完成、raw/ADC-pin-voltage 外部输入、默认 24 V/VIN 分压与 LCD NONE 源、连续/circular 数据面、基础 peripheral-request DBM/M1AR/CT 双缓冲、SPI2 byte TX/RX request、UART1/2/3/5/7/10 的板级 DMA 映射、UART1 TX/RX 双向 byte request、OCTOSPI2/W25Q64 最小数据路径、CORDIC Q1.31 sine/cosine 子集和 QEMU standard CAN bus 已接入；DMA FCR/DME 最小语义及 P2M FIFO 容量溢出事务边界已接入；完整 SPI/DMA 状态机、真实 DMA FIFO 触发条件/总线仲裁仍 pending
- M3：定长 little-endian 二进制 codec、时间/序号校验、IMU/telemetry/ADC_INPUT/ADC_VOLTAGE payload 和 1000 帧确定性 smoke：已实现
- M3：IWDG1 key/预分频/重装值、QEMU virtual timer 和 guest reset：已实现并通过独立超时 smoke；LSI 仍为可配置的理想 32 kHz 源
- M3：协议显式 wire size、RESET session 起点、telemetry reserved 校验：已实现并通过 CTest
- M3：Unix socket/TCP adapter：已实现并通过 CTest；QEMU-native chardev IMU/ADC、板级输出 telemetry、电源/模拟源、UART/FDCAN 数据通道、SocketCAN host bridge、Null/MuJoCo/ROS2 worker 和可加载 backend registry 已实现并通过 smoke/单测；DM-MIT Null/MuJoCo/ROS2 控制反馈链路已接通；Gazebo 实际模型验收、非 ROS 原生 adapter 和完整多电机映射仍 pending

## 当前可验证内容

1. 编译器和 CMake 可以构建独立 probe。
2. 可以发现 `qemu-system-arm` 是否存在，并报告版本、machine 和 CPU 列表。
3. 可以发现 QEMU 源码目录是否为 Git 仓库及其 revision。
4. 在 QEMU 已安装时，可以启动 `-machine none` 做无破坏 smoke。
5. 自编译 QEMU 已注册并启动 `dm-mc02`，独立 guest 已从 Flash 向量表进入 Reset_Handler。
6. 独立 BMI088 guest 已通过 GPIOC 片选和 SPI2 寄存器窗口读到两个 die 的 ID 与 raw 数据。
7. co-sim codec 已通过 1000 个连续 1 ms 帧、坏 magic、NaN 和严格单调性校验，固定 hash 为 `0x541eda0e854b8d4c`。
8. Unix-domain/TCP stream adapter 已实现 4-byte little-endian 长度前缀、partial I/O、阻塞/非阻塞结果和协议边界检查，并通过 transport smoke。
9. QEMU-native chardev link 已使用相同 framing；host IMU frame 可通过原有 SPI2 路径读回 BMI088 raw，量程灵敏度、ACC_CONF/gyro BANDWIDTH ODR 下采样、DRDY 和 accel 25.6 kHz sensor-time 已通过 readback smoke；连接建立和 GPIO/TIM 输出变化会推送 telemetry，重复状态会去重。
10. PWR/RCC 最小模型已通过 bare-metal smoke；PWR 电压 ready、RCC HSIRDY/PLL 状态和时钟使能寄存器路径可用。Release 固件已越过 `ExitRun0Mode`、完成 BMI088 初始化并持续运行 FreeRTOS tick；这不代表完整 H723 外设模型已经完成。
11. 可选 UART chardev 已接入 USART1/2/3、UART5、UART7、USART10；TDR TX、RX FIFO、RXNE/RDR、UART1 DMA1 Stream0/1 双向 byte request、USART2 DMA1 RX + DMA2 TX 跨控制器路径和 USART1 IDLE IRQ/IDLECF 已通过独立 guest smoke，测试也覆盖 DMA 配置前 FIFO 中的 pending RX 字节。IDLE 边界采用 chardev chunk，不等同于真实 bit timing 或完整 HAL ReceiveToIdle event size。
12. FDCAN1 已接入真实 message RAM TX/RX FIFO、64 个 dedicated Rx Buffer 和固定 84 字节 chardev 帧，经典 CAN 及 CAN-FD DLC 映射、虚拟时间戳、RXF0A/RXF1A 释放、标准/扩展过滤器、FIFO0/FIFO1、NDAT/DRX、全局接收策略和 `ILS/ILE` 双中断线已通过独立 guest smoke；FDCAN2/3 复用同一模型和独立后端槽位。生产收发通过 `DmCanBusAdapter` 接入 QEMU standard `CanBusState`，发送即时广播、发送者不回环，并由可接收 peer 返回值作为 ACK 结果；固定 84-byte chardev 是独立 legacy host wire。QEMU bus 不提供 CAN-ID 仲裁、帧持续时间、物理 ACK、error frame 或完整 bus-off recovery；旧 virtual-time medium 仅为 test-only fixture。
13. `dm_mc02_sim_worker.py` 已提供 NullEngine 和可选 MuJoCo engine，固定步长 IMU、DM-MIT FDCAN 命令/反馈、RESET/序号/虚拟时间发送已通过 worker protocol smoke；QEMU 与 worker 联合闭环 smoke 也已通过。FDCAN host TX 在 worker 晚连接时由 QEMU 有界队列暂存。
14. worker 已提供 ROS2 engine：订阅标准 `sensor_msgs/Imu`、发布 `Float64MultiArray` 电机命令，可连接 Gazebo ROS2 插件；ROS2 不进入 uv 基础依赖。
15. MuJoCo 最小 XML、DM-MIT enable/控制项/反馈和 ROS2 JointState 电机状态输入 smoke 均已通过；当前还没有针对具体 Gazebo 机器人模型的动力学和控制器验收测试。
16. 当前性能基线：`collect-performance-baseline.sh` 最近一次测得 NullEngine 约 331,499 IMU 帧/s（10,000 帧）；Release 固件在修正板级 480 MHz CPU 时钟和 SPI EOT 语义后，QEMU TCG 下 1 秒样本保持约 1 kHz FreeRTOS tick，RTF `0.999979`。
17. worker 初始 telemetry 等待已由 200 ms 缩短到 20 ms；启动阶段仍需继续分析 QEMU TCG、固件初始化和 MMIO 访问开销。
18. DMA1/2 的 8 个 stream 已支持最小同步 memory-to-memory 搬运，包含 `PSIZE/MSIZE`、`PINC/MINC`、`NDTR`、TC/HT/TE 状态标志、HTIE/TCIE/TEIE IRQ 门控、LIFCR/HIFCR 清除、`CIRC` 完成后 NDTR/地址回绕，以及基础 peripheral-request DBM 的 `M1AR/CT` 交替和每 buffer reload；`run-dma-ht-smoke.sh`、DMA、SPI2、ADC、TIM8 和 `run-tim8-dbm-smoke.sh` 已覆盖对应路径。DMA FCR 的 `FTH/DMDIS/FEIE` 写语义、direct-mode 宽度错误 `DMEIF`、`DMEIE` 门控、FIFO-mode 最小宽度转换及 P2M FIFO 容量不足前不消费外设数据的事务边界已由 host fixture/`run-dma-fcr-smoke.sh` 覆盖；真实 FIFO error 触发条件、总线仲裁仍 pending。
19. DMA2 Stream6 的 D2 SRAM WS2812 buffer observer 已接入：按 168/84 PWM 值解码两颗灯的 GRB，第一颗映射为 RGB/max-channel brightness，并通过 10 ms virtual timer telemetry 更新；独立 WS2812 smoke 已通过。它不等同于真实 DMA 外设 request/定时器波形模型。
20. SPI2 的 DMA1 Stream4 TX / Stream3 RX 已支持按 H723 DMAMUX1 request ID 40/39 的单字节外设 request，TX 写入 SPI2 后驱动 BMI088 返回，再由 RX DMA 写回 guest buffer；Stream EN 启动回调、NDTR、TC/HISR/LISR 和地址递增已通过独立 smoke。BMI088 已按 ACC_CONF/BANDWIDTH 配置施加虚拟时间一阶带宽滤波；其它 DMA 外设和 FIFO 仍 pending。
21. DMA1/2 各 Stream 已接入 Cortex-M NVIC 的 level-sensitive TC/HT/TE IRQ（DMA1 为 `11..17,47`，DMA2 为 `56..60,68..70`，对应 H723 非连续向量）；TCIE/HTIE/TEIE、LIFCR/HIFCR 清除、HT 状态锁存和 DMA1 Stream0 handler 已通过 bare-metal smoke。DMEIF/DMEIE、FCR 最小寄存器语义以及 FEIF/FEIE 状态与 IRQ 分离已通过独立 DMA smoke；完整 silicon FEIF 触发条件、FIFO 水位/仲裁仍 pending；DBM 的基本 TC/HT 状态和 CT 翻转已由 TIM8 双缓冲 smoke 覆盖。
22. ADC1 已绑定 DMAMUX1 request 9 的可选 DMA 数据面；根据真实 H723 `SQR1` 的 6-bit rank stride 解析 1--4 个 regular rank，按 `SMPR1/SMPR2` 的采样时间逐 rank 推进。默认模式保留 1 ms 的连续序列节拍上限以控制 QEMU 事件量；`accurate-timing=on` 时去掉该策略性下限，按有效 ADC 时钟调度实际转换 cadence。当前板级有效 ADC 时钟来自独立 PLL2P，并通过 ADC12 common `CCR.CKMODE/PRESC` 选择同步/异步分频（默认正常配置为 96 MHz/64=1.5 MHz）；RCC 写入 PLL2 配置或 common CCR 后会重新计算，未配置 PLL2 时保留兼容默认值。支持软件启动和 TIM8 更新 rising-edge 外部触发（按 `EXTSEL` 匹配当前模型源）、按 channel 注入 16-bit raw 值和 ADC pin 电压（四舍五入、3.3 V 饱和），以及旧的双样本兼容 API。支持 `ADEN/ADSTART/ADDIS/ADSTP` 和 ADCAL 的确定性虚拟完成、连续/单次模式；ADC1→DMA1 Stream2 的 circular buffer、NDTR 回绕、固定 `ADC_DR` 端点和 TC flag 已通过独立 smoke，ADC1 direct P2M reservation 也已保证非法目标不会消费 `ADC_DR/EOC`，重新 enable stream 可在无新转换时提交同一个结果；QEMU-native co-sim 的 ADC_INPUT/ADC_VOLTAGE frame 和 channel 4/19 顺序已通过端到端 smoke。ADC1/ADC2 已通过 OR 接入真实 H723 ADC_IRQn 18；`IER.EOCIE/EOSIE/OVRIE` 电平中断、`ADC_DR` 读清 EOC、ISR 写 1 清标志、按 `CFGR.OVRMOD` 保持/覆盖数据和未读数据 OVR 已实现并由独立 bare-metal smoke 验证。ADC 校准数据路径已应用于 regular/injected 结果并通过 `DIFSEL` 选择单端/差分 offset；完整 ADC kernel source 选择、APB 分频、其它外部触发选择矩阵、真实校准统计和 SAR 电容级 linearity 补偿仍 pending。
23. 板级电源模型已接入 GPIOC PC13/PC14/PC15：默认 VIN=24 V，支持 `vin-mv` machine/QMP 属性和运行时电源状态更新；正常/欠压/断电状态可确定性派生，channel 4 按 VIN/11 产生 ADC 源，channel 19 默认产生 LCD NONE 源；外部 raw/pin-voltage override 优先于板级源。独立 host/QMP harness 已覆盖 VIN=0/1/11999/12000/18000/24000/36300/40000 mV 的状态、系统电源、reset 保留和 ADC4 分压/饱和边界。当前仍未实现转换器瞬态、过流跳闸和收发器实际断电门控。
24. TIM8 已增加按 `PSC/ARR` 的 QEMU virtual update event；TIM8 `CC1DE` 事件可通过 DMAMUX1 request 47 驱动 DMA2 Stream6，将 D2 SRAM 中的 half-word PWM 序列写入 `TIM8_CCR1`，并支持 circular 回绕。TIM8→DMA2 的端到端 smoke 已通过；当前仍是更新频率近似，不是完整 compare-match/PWM 波形模型。timer update 已改为绝对 virtual-time deadline，延迟时跳过错过的周期，避免长期相位漂移和追赶风暴。
25. OCTOSPI2 已接入 8 MiB W25Q64 backing store 和 gated memory-mapped window；支持 JEDEC ID、读状态/WEL、WREN、quad page program、sector erase、普通/quad read 及 FCR/TCF/TEF 最小状态路径。独立 bare-metal smoke 已验证编程、memory window 读回、擦除和未 WREN 保护；当前未实现真实线模式、DTR、DMA、延时和非易失持久化。
26. CORDIC 已接入 `0x58004400`，支持 H723 HAL 编码的 `FUNC=0` cosine、`FUNC=1` sine，32-bit Q1.31 输入输出和 `NRES=1` 配对结果；不支持的 16-bit/双参数配置置模型诊断位并不产生结果。独立 smoke 已验证 `RRDY` 生命周期、pi/4 结果和诊断清除；当前未实现硬件精度/缩放、DMA、中断和其它函数。
27. USB OTG HS 已有 DWC2 controller-ready 寄存器 smoke，验证 `GSNPSID`、`GRXFSIZ`、`AHBIDL` 和 `CSRST/CSRSTDONE`；当前仅用于固件初始化兼容性，不代表 USB 枚举、端点传输或 USB 总线已实现。
28. FDCAN1/2/3 已通过 `DmCanBusAdapter` 共享 QEMU standard `CanBusState`；默认 machine 自动创建内部 bus，也支持 `-object can-bus,id=canbus -machine dm-mc02,canbus=canbus`。标准 bus 即时广播、发送者不回环，并用可接收 peer 的返回值作为 bus-level ACK；固定 84-byte chardev 仍是独立 legacy host wire。QEMU bus 不提供 CAN-ID 仲裁、帧持续时间、物理 ACK、error frame 或完整 bus-off recovery；旧 virtual-time medium 仅保留为 test-only fixture。
29. ADC 触发/校准 smoke 已覆盖真实 `SQR1` rank stride、软件单次/连续启动、TIM8 更新 rising-edge 外部触发、rank-level sampling-time 调度、ADCAL 虚拟完成和 regular/injected 校准数据变换；ADC IRQ/OVR smoke 另行覆盖 IRQ18 向量、中断使能/屏蔽、EOC/EOS/OVR 生命周期、DR 读清、ISR W1C、默认 `OVRMOD=0` 数据保持及读取 DR 后 OVR 保持。当前仍不覆盖其它 EXTSEL 源、`OVRMOD=1` 覆盖分支、真实校准统计和 SAR 电容级 linearity。
30. `tools/collect-performance-baseline.sh` 已提供短时、确定性的 worker/QEMU smoke 基线：固定 10000 帧 NullEngine 吞吐、worker 启动到 RESET 延迟和 `dm-mc02` 启动 smoke wall-clock；最近实测为 331,499 IMU 帧/s、47.615 ms、51.744 ms。该脚本不测真实固件 RTF。
31. `tools/collect-firmware-rtf.sh` 直接启动只读的 Release `trobot.elf`，从 ELF 符号自动定位 `xTickCount`，按 FreeRTOS 1 ms tick 计算真实固件 RTF，并通过 machine QOM 的 IWDG diagnostics 检测 guest reset；默认优先选择 Release QEMU。最近 1 s 样本推进 1001 tick，RTF 为 **0.999912x**，有效约 999.912 tick/s，QEMU 进程 CPU 约 100.9%，RSS 约 49.5 MiB。
32. 性能差距的主要原因已定位并修复：QEMU 板级模型此前把 ARMv7M `cpuclk` 错设为 24 MHz，而固件运行时 `SystemCoreClock` 为 480 MHz；QEMU SysTick 按错误的 24 MHz 周期运行，造成约 20 倍的假性变慢。现在复位从 64 MHz HSI 开始，RCC 的 SW/PLL/D1CPRE 写入会动态传播到 `cpuclk` 和 SysTick；QEMU 真实固件 RTF 与 Renode 记录的约 0.95x 已同量级。34 个 QEMU smoke、host CTest、worker/QEMU 基线均通过。
33. 在错误 24 MHz 时钟下的 `tcg,tb-size=16/64/256 MiB`、`-icount shift=auto` 和 50 ms `-d exec` trace 对照不再作为性能结论；后续 profiling 应在动态时钟正确的版本上重新进行，并用 guest PC/TCG trace 归因。
32. USART2/USART3 RS485 DE 已支持板级方向选择：PD4/PB14 普通 GPIO ODR 手动门控，AF7 + `CR3.DEM` USART 自动门控，断开/错误复用时禁止 TX；polling TX、DMA TX、`CR3.DEP` 极性和 DMA 交互已由独立 smoke 验证。当前仍不模拟位级 DE 时序、发送延迟、回显、总线冲突和收发器电气断电。
33. worker 侧 SocketCAN bridge 已完成：固定 84-byte FDCAN frame 与 Linux `can_frame`/`canfd_frame` 双向转换，支持标准/扩展 ID、RTR、CAN-FD/BRS、DLC 映射；QEMU→SocketCAN 和 SocketCAN→QEMU 已接入 worker 非阻塞事件循环。socketpair smoke、worker 全部模式和全量 QEMU smoke 已通过。当前不支持 CAN error frame、Linux CAN 物理层/bit timing，也未创建 `vcan` 做特权环境测试。
34. Flash warm reset 已补齐控制器复位语义：`FLASH_CR1` 恢复 LOCK、解锁 key 状态清除、NOR program overlay 关闭，同时保留 Flash backing 内容；GPIO 关键寄存器已支持 8/16/32-bit lane merge，新增 Flash reset 和 GPIO sub-word smoke。
35. UART host TX 已改为 4096-byte 有界队列；chardev 短写/暂时 backpressure 在 QEMU virtual time 中重试，队列满时丢弃最新字节并累计内部诊断计数；UART、DMA UART、RS485 smoke 已回归通过。协议层 ACK/重传和计数导出仍 pending。
36. 增加 `cold-reset=on` 工具选项：默认保持真机式 warm reset；开启后 `system_reset` 清空 ITCM/DTCM/AXI/D2/D3 SRAM，保留片上/外部 Flash，并由独立 cold-reset smoke 验证。
37. IWDG1 已实现 `KR=0x5555/0xAAAA/0xCCCC`、`PR=/4..../256`、12-bit `RLR`、默认 32 kHz LSI、virtual timer 和超时 guest reset；新增 `run-iwdg-smoke.sh` 覆盖锁定写入、解锁配置、reload/start 和实际复位。当前不模拟 IWDG window mode、LSI 温漂或独立电源域细节。
38. 定时器更新事件改为基于绝对 deadline 调度；QEMU 落后时跳过已错过的抽象事件，不将 host 调度延迟累计到后续周期，也不形成高频 callback 追赶。Debug/Release 构建、44 个 QEMU smoke 和 10 s Release RTF 回归通过。
39. DMA DMAMUX request 缓存改为记录同一 request 的全部匹配 stream；正常单 stream 保持快速路径，多个 stream 共享 request 时不再静默漏传输，CR/PAR 改写会使缓存失效。DMA 族 smoke 与 Release RTF 回归通过。
40. 仿真 worker 的 IMU 时间戳改为按样本序号计算理想 `round(n*1e9/rate)`，避免非整数采样频率逐帧累加舍入误差；worker smoke 改用 333 Hz 精确时间断言，MuJoCo smoke 通过。
41. CRC 从寄存器安全窗口提升为常用 polynomial/INIT/RESET/bit-reversal 计算子集，RNG/CRC 状态纳入 QMP system reset；新增 `run-crc-smoke.sh`，标准 CRC-32/CRC-8/CRC-16 向量和 reset 通过。
42. 电源输入已从仅内部 helper 提升为 `-machine dm-mc02,vin-mv=<mV>`；QMP `qom-set /machine vin-mv` 可运行时改变 VIN，外部 VIN 跨 `system_reset` 保留而 MCU GPIO 供电开关按 reset 复位；`run-vin-config-smoke.sh` 与电源边界 smoke 已通过。
43. 真实固件回归发现 SPI2 模型缺少 H723 `SR.EOT`，`HAL_SPI_Transmit()` 会永久等待并最终触发 IWDG；现已实现 `CR2.TSIZE` 基础计数、`SR.EOT`、`IFCR.EOTC` 和 8/16/32-bit TXDR 打包写，BMI088 smoke 覆盖 EOT 清除，真实 Release 固件 10 s 无复位运行。

## 后续里程碑

- M1a：确认上游 QEMU 的 ARMv7-M/M7 CPU 实际支持和地址空间接口。
- M1b：实现 Flash/SRAM/向量表、CPU reset、NVIC/SysTick 基础路径：内存/复位/向量表已完成，NVIC/SysTick 由 QEMU ARMv7M 容器提供，尚未加入 H723 校准回归。
- M1c：实现 PWR/RCC 最小初始化路径：已完成，并通过 PWR/RCC bare-metal smoke；Release 固件已越过 `ExitRun0Mode`，启动兼容窗口已覆盖 `FLASH_R`（`0x52002000`）。
- M2：实现 SPI/DMA/BMI088 raw 数据链路：最小 DMA memory-to-memory 搬运、DMA circular、ADC1 channel/rank request、TIM8_CH1→DMA2 Stream6 request、SPI2 byte TX/RX request、UART1/2/3/5/7/10 request 映射、DMA TC/TE IRQ、WS2812 buffer observer、OCTOSPI2/W25Q64 最小读写路径、CORDIC Q1.31 sine/cosine 子集、QEMU standard CAN bus 和 USART2/USART3 RS485 DE 已完成并通过 smoke；完整 SPI/DMA 状态机和真实定时器波形仍 pending。
- M3：实现外部二进制协同接口和虚拟时间：codec/validator、Unix/TCP transport、QEMU IMU 接线、输出 telemetry、UART/FDCAN 通道、SocketCAN host bridge 和 Null/MuJoCo/ROS2 worker 基础已完成；Gazebo 实际模型联调、完整电机协议映射和回放仍待实现。

## 2026-08-30 DMA PINC endpoint 修复

- [x] 修复 peripheral-request 单 request 路径对 `PINC` 的实际地址访问：路由匹配继续使用捕获的 `reload_par`，实际传输改用 live `SxPAR`；普通 stream 仍使用固定 peripheral endpoint。
- [x] 扩展 `run-spi2-dma-smoke.sh` 的真实 SPI2/BMI088 回归：连续两个单 item request 启用 `PINC` 时，第二项不应重复写入 `SPI2_TXDR`；测试同时检查 live `PAR` 增量和 BMI088 寄存器保持值。
- [x] 定向 DMA arbitration/FCR/batch、QEMU build、CTest `11/11`、QEMU smoke `69/69`、Python `243 passed`、shell/compileall/`uv lock --check` 均通过。
- [ ] 仍待实现：真实 DMA FIFO/FEIF 触发条件、MBURST/PBURST 总线占用、时钟级仲裁和完整错误恢复；本修复只收敛 `PINC` 的 endpoint 路由与实际地址一致性。

## 2026-08-30 SoC calibration ROM 只读语义

- [x] 在可复用 `STM32H723` SoC memory layer 中将 UID/ADC factory calibration
  region 从普通 RAM 改为 `memory_region_init_rom_nomigrate()`，保留初始化 backing
  数据但拒绝 guest 写入。
- [x] 新增 `run-calibration-rom-smoke.sh` 及 bare-metal guest，覆盖 32/16 位读取、
  固定 UID/校准值和写入后内容保持；该 runner 自动加入全量 QEMU smoke。
- [ ] 仍待实现：真实不同芯片的 UID/calibration 数据来源及 option-byte/ECC 语义；
  当前固定数据只用于可复现的 H723 profile 测试。

## 下一阶段设计决策

QEMU 数据面采用 QEMU 原生 chardev，避免把 POSIX socket、阻塞 I/O
和连接生命周期带入 machine/MMIO 热路径。现有 `cosim/dm_mc02_transport.c`
保留为 host-side worker 和测试使用；QMP 只承担暂停/恢复、诊断和故障控制，
不承担高频 IMU/telemetry 数据交换。

垂直切片按以下顺序推进：

1. 固化协议的显式 wire size、双向 session/RESET 语义和 reserved 校验：已完成。
2. 新增 QEMU-native `dm_mc02_cosim_link`，使用固定缓冲 parser、QEMU chardev、
   独立 RX/TX sequence 和 QEMU virtual clock；已支持初始及变化时 telemetry，
   相同状态去重。
3. 将 link 接到真实板级 endpoint：GPIO/LED、PWM/buzzer 的最小路径已完成，
   当前 LED 是 PA7 活动投影、蜂鸣器是 TIM12_CH2/PB15 状态投影；真实 WS2812
   当前 WS2812 是从真实 firmware DMA buffer 观察解码，不增加绕过固件外设路径的 mailbox；ADC1 已可接收带时间序列的 channel/raw 或 pin-voltage 输入，并支持最小采样时间、软件触发和 TIM8 外部触发，但完整触发矩阵和电气 ADC 行为仍待实现。
4. 为 BMI088 增加 IMU sample provider：host frame 经过默认量程单位、饱和、raw
   register 转换后，再由 guest 原有 SPI2 路径读取；已通过 paused-guest SPI readback。
5. 增加 chardev Unix/TCP、断连/重连、坏帧、迟到 frame 和 guest 不阻塞的 integration smoke：基础 IMU/输出链路已完成，ADC raw/analog 输入也已通过端到端 smoke。
6. SocketCAN host bridge 已完成；后续可在具备权限的机器上增加 `vcan`/真实 CAN 联调，并补充 CAN error frame 和物理层 bus-off 恢复模型。

第一阶段限制为单 QEMU 进程、单 chardev client，不承诺 migration/snapshot 下的
link 状态复现；TCP 默认 loopback，除非明确配置绑定地址。

## 已确认的板级映射（只读来源：`trobot/trobot.ioc`）

- WS2812：`PA7 / TIM8_CH1N`，DMA2 Stream6。
- 蜂鸣器：`PB15 / TIM12_CH2`。
- 24V 使能：`PC13 / POWER_24V_2`；5V 使能：`PC15 / POWER_5V`。
- RS485：`PD4 / USART2_DE`、`PB14 / USART3_DE`。
- UART：USART1 `PA9/PA10`，USART2 `PD5/PD6`，USART3 `PD8/PD9`，
  UART5 `PC12/PD2`，UART7 `PE8/PE7`，USART10 `PE3/PE2`。
- UART DMA：H723 使用 DMAMUX1；DMA1 Stream N 对应 channel N，DMA2 Stream N 对应
  channel N+8。request ID 为 USART1 RX/TX `41/42`、USART2 `43/44`、USART3 `45/46`、
  UART5 RX `65`、UART7 RX/TX `79/80`、USART10 RX/TX `118/119`。
- FDCAN：FDCAN1 `PD0/PD1`，FDCAN2 `PB5/PB6`，FDCAN3 `PD12/PD13`。

这些映射用于 QEMU 模型实现和测试；UART/FDCAN 的主机通道已经有独立 smoke，QEMU standard CAN bus 也已通过节点间 smoke，
UART DMA 和最小 USART IDLE IRQ 已接入上述路径，RS485 DE 已支持手动 GPIO/AF 自动两种方向控制，SocketCAN host bridge 已在 worker 侧接入，但电气层、SocketCAN 原生 QEMU 后端、
完整 HAL ReceiveToIdle event-size 回调和完整 DMA 语义仍不应标记为完成。

当前 Release 固件启动回归已越过 `ExitRun0Mode` 并持续运行；这不代表完整 H723
外设模型或完整业务启动已经完成。

## 约束

- 本项目所有写入仅发生在 `dm-mc02-qemu/`。
- 不修改固件或现有 Renode 仿真。
- 未实现功能必须显式报告。
- 不使用删除、覆盖用户目录或回滚仓库的命令。

## 最近执行记录

- 2026-08-29：修正 FDCAN dedicated Rx Buffer 忙状态的错误 GFC 回退，并纠正 H723
  `IR` 的 RF0F/RF1N/RF1F 位定义；补齐 FIFO 高优先级过滤器的 `HPMS/IR.HPM`。
  专项及全量 51/51 smoke、host CTest 4/4、uv pytest 16 passed；真实 Release
  固件 1 s RTF `0.998739x`，CPU `100.9%`，IWDG timeouts=0。
- 2026-08-29：补齐 FDCAN 粗粒度 BUS-OFF 与显式 INIT 重初始化恢复；无 ACK 按帧级
  TEC +8，达到阈值后设置 BO 状态并取消该节点 medium pending。新增 `run-fdcan-busoff-smoke.sh`，
  构建及专项 smoke 通过；完整 CAN error frame/物理层 recovery 仍 pending。
- 2026-08-29：DMA 基础 peripheral-request 路径增加 DBM/M1AR/CT 双缓冲状态机；M0/M1 各自按 `NDTR` 重装，TC 后切换 CT，HT/TC 状态保持。DMA 内部新增 current cursor，M0AR/M1AR 保持真机式配置基地址，不再把寄存器暴露为逐项运行指针。新增 TIM8→DMA2 Stream6 双缓冲 smoke；49/49 smoke、CTest 4/4、Python 15 passed/1 skipped、Release RTF `0.999912x`，IWDG timeouts=0。
- 2026-08-28：修复 VIN 只能在内部电源 helper 中设置、无法通过 QEMU 使用的问题。新增 `vin-mv` machine/QMP 字符属性，默认 24000 mV；运行时更新会同步 5V/CAN/RS485 供电策略，system reset 保留外部 VIN 但复位 GPIO 供电开关。新增 `run-vin-config-smoke.sh`，QEMU 重编译、VIN、电源边界和全量 39 个 smoke 通过；Release 固件 RTF 约 `0.99986x`。
- 2026-08-28：修复外部 worker 在 co-sim/FDCAN/SocketCAN host socket 背压时等待 1 秒的问题。三类发送端现在都使用 256 帧有界、保序、非阻塞队列；队列持续满时显式失败，有限帧运行结束时按 1 秒上限排空。新增短写/背压/队列上限单元测试；host CTest 3/3，worker/QEMU/FDCAN smoke 通过。`tools/build-smoke.sh` 已恢复可执行权限。
- 2026-08-28：补齐通用定时器 update 的 UIF/UIE/W0C 和 level-sensitive IRQ，接入 TIM2 IRQ28、TIM8 update IRQ44、TIM12 IRQ43、TIM24 IRQ162；修正 ARMv7M 外部 IRQ 数量覆盖到 163。新增 TIM2 HAL timebase 裸机 smoke；现有 Release 固件仍约 `0.9997x` 实时系数。
- 2026-08-28：保存 review 修复计划。按 P1 顺序修复 Flash 编程门控、USB FIFO 短写/flush、DMA HTIF 语义；补充专项 smoke，并以现有 36 个 smoke、CTest、Release 固件 RTF≈1.0 为回归门槛。P2 继续保留 BMI088 ODR/温漂/随机游走、完整 USB 枚举和完整 Flash ECC/编程粒度为后续项。
- 2026-08-28：完成 BMI088 量程/ODR/DRDY/sensor-time 和 co-sim 时间调度切片。host IMU 按已配置 accel/gyro ODR 的 frame timestamp 锁存，采样不启动额外 QEMU 高频 timer；对应样本更新 raw、DRDY 和 accel 24-bit 25.6 kHz sensor-time。RESET 建立 host/QEMU virtual-time 原点，未来 IMU frame 在对应 QEMU 时刻由有界队列交付，迟到 frame 不阻塞地立即生效。readback smoke 覆盖 ±1000 dps/±6 g、100 Hz 边界内帧丢弃、DRDY 生命周期和 11 ms sensor-time；新增 timing smoke 覆盖暂停时注入 t=1 ms、恢复后先旧后新数据。38 个 integration smoke、CTest 2/2、uv/Python/Shell 检查和 Release 固件 RTF=0.999808x 通过。滤波、温漂、random walk、FIFO 和 DRDY 引脚仍 pending。
- 2026-08-28：核对真实 `bsp_imu_init()` 后修复 BMI088 gyro soft-reset：gyro 采用 `0x14=0xB6`，不再错误沿用 accel `0x7E`；加速度计继续采用 `0x7E=0xB6`。同时修正 six-uniform 噪声近似的方差缩放，使 machine noise 参数是实际 one-sigma 值。BMI088 smoke 现覆盖两个 die 的真实 reset 序列；runner 会在 smoke 源或 linker script 更新后自动重编译。38 个 integration smoke、CTest 2/2、uv/Python/Shell 检查和 Release 固件 RTF=0.999859x 通过。
- 2026-08-28：BMI088 温度数据面改为真实 11-bit 编码（0.125°C/LSB、23°C offset），新增 `imu-temperature-c` machine 参数，默认 25°C；SPI smoke 覆盖 accel `TEMP_M/TEMP_L`。温漂仍是后续的动态物理模型，不与该静态环境温度参数混淆。
- 2026-08-28：补齐 ADC 的板级时钟连接和时序策略：按 DM-MC02 实际独立 PLL2P 路径及 ADC12 common `CCR.CKMODE/PRESC` 计算有效 ADC 时钟，默认保持 1 ms 连续序列限流以保证实时性，`accurate-timing=on` 时按转换周期调度；ADC trigger、DMA、IRQ、analog 和 Release RTF 回归通过。ADC 完整 kernel source/APB 分频树仍 pending。
- 2026-08-28：完成 DMA HTIF 语义修复：半传输达到边界时无条件锁存 HTIF，HTIE 仅门控中断输出；更新 memory-to-memory、SPI2、ADC、TIM8 期望值，新增 `run-dma-ht-smoke.sh` 专项状态回归。真实半传输事件仍是当前最小 request 模型的一次 item 边界，不含 FIFO/DBM/FE/DME。
- 2026-08-28：DMA 修复后的验收通过：QEMU 重编译、`-machine none` smoke、31 个板级/host smoke（含新增 HT smoke）、host CTest 2/2、`uv lock --check`、Python compileall 和全部 shell 语法检查均通过；`trobot/` 未修改。
- 2026-08-28：补齐 IWDG1 最小真实语义和定时器绝对 deadline。IWDG 支持 key 解锁/reload/start、PR/RLR 配置、32 kHz LSI virtual timer，超时通过 QEMU guest reset；TIM update 延迟不再累积相位，错过周期不追赶。40/40 QEMU smoke、host CTest 2/2、uv/Python/Shell 检查通过；Release 固件 RTF `0.999845x`，约 999.85 tick/s，CPU 约 102.9%。
- 2026-08-28：修复 DMA request 缓存只保留单 stream 的语义缺陷；共享 request 的多个 stream 现在均可在同一 peripheral event 上推进，单 stream 仍走快速路径。DMA、ADC、UART、SPI、TIM8 smoke 通过；本次 Release RTF `0.999568x`，约 999.57 tick/s，CPU 约 102.9%。
- 2026-08-28：修复 worker 非整数 rate 的累计时间舍入漂移，并用 333 Hz worker smoke 验证按样本序号生成的时间戳；MuJoCo worker smoke 通过。

- 2026-08-28：按当前 QEMU 计划完成一组高价值功能切片：内部 Flash 改为 1 MiB 可写 backing，支持 FLASH_R 解锁后的 sector erase；BMI088 增加可复现 gyro/accel 噪声、三轴零偏和 seed 配置；增加 `accurate-timing` 与 `dma-batch-limit` 性能控制；USB OTG HS 增加可选 serial slot 10 的 FIFO0 虚拟 CDC 双向字节管道。新增 Flash 和 USB pipe bare-metal smoke，现有 QEMU/host smoke 继续通过。完整 USB 枚举、端点、Flash ECC/持久化和传感器 ODR/漂移模型仍 pending。

- 2026-08-28：完成一次性能回归后实现低风险优化：DMA DMAMUX request->stream 缓存（按 generation 和 request 独立失效）、UART/SPI DMA 每次 timer callback 最多批量处理 32 个 item、WS2812 telemetry dirty flag 以避免无关 GPIO 变化重复解码；同时修复并覆盖 SPI1 reset 状态清理。优化后全量 QEMU smoke、host CTest、uv/Python 检查均通过。当前 Release 固件 RTF 仍约 0.166x，确认主瓶颈是单核 TCG guest 执行，不是这些外设路径；后续需用 guest PC/TCG profiling 定位，不能仅靠增加 host 并行度。
- 2026-08-27：完成 ADC 采样/触发切片。修正 H723 `SQR1` 的真实 6-bit rank stride 和 `CFGR.DMA[1:0]` 语义；加入按 `SMPR1/SMPR2` 的 rank-level 虚拟采样时间、软件启动、`EXTSEL` 匹配的 TIM8 更新 rising-edge 外部触发，以及 ADCAL 的虚拟完成。ADC DMA/raw/analog/trigger smoke 和全量串行外设、co-sim、MuJoCo、ROS2 回归通过。
- 2026-08-28：完成 ADC 中断/溢出切片。ADC1/ADC2 通过 OR 接入 ADC_IRQn 18，实现 EOC/EOS/OVR 电平中断、DR 读清 EOC、ISR W1C、OVRMOD 数据保持/覆盖和未读数据 OVR；新增独立 bare-metal IRQ/OVR smoke。随后串行重跑全部 32 个 smoke，全部通过。下一步优先补齐其它 EXTSEL 源和校准系数语义，再进入性能 profiling。
- 2026-08-28：完成 USART2/USART3 RS485 DE 切片。PD4/PB14 在普通 GPIO 输出模式下支持 ODR 手动方向控制，在 AF7 + `CR3.DEM` 下支持 USART 自动方向控制；polling TX、DMA TX、极性和复用切换由独立 smoke 验证。随后重跑全部 33 个 QEMU smoke、host CTest、Python/Shell/uv 检查，全部通过（电源边界 host harness 仅有 QEMU 公共头既有 warning）。下一步实现 worker 侧可选 SocketCAN bridge。
- 2026-08-28：完成 USART2/USART3 RS485 DE 切片。PD4/PB14 在普通 GPIO 输出模式下支持 ODR 手动方向控制，在 AF7 + `CR3.DEM` 下支持 USART 自动方向控制；polling TX、DMA TX、极性和复用切换由独立 smoke 验证。随后重跑全量 QEMU smoke、host CTest、Python/Shell/uv 检查，全部通过（电源边界 host harness 仅有 QEMU 公共头既有 warning）。
- 2026-08-28：完成 worker 侧 SocketCAN bridge。加入固定 FDCAN wire 与 Linux classic CAN/CAN-FD 转换、QEMU chardev 双向事件循环、参数保护和无特权 socketpair smoke；重跑 34 个 QEMU smoke 结果全部通过。尚未在 root/vcan 或真实总线上联调。
- 2026-08-28：修复 Flash warm reset 状态泄漏：QMP `system_reset` 现在会重新锁定 `FLASH_CR1`、清除 key sequence 并关闭编程 overlay，但保留片上 Flash 内容；Flash smoke 新增复位后禁止未解锁编程的回归。GPIO smoke 新增 ODR/BSRR 的 8/16-bit lane 访问覆盖。
- 2026-08-28：修复 UART chardev TX 短写丢字节：新增 4096-byte 有界 TX 队列、虚拟时间重试和 short-write/drop 计数；正常 UART、DMA UART、RS485 相关 smoke 通过。
- 2026-08-28：将板级 CPU 时钟改为动态 RCC 驱动：复位 HSI 64 MHz，按 SW/PLL1/D1CPRE 计算并通过 QEMU clock propagation 更新 ARMv7M/SysTick；修复 `SW/SWS` 合成寄存器读取导致的动态计算错误。1.0 s Release 固件样本为 1000 tick、0.99898x、998.98 tick/s、115.9% CPU、约 46.9 MiB RSS；34 个 QEMU smoke、host CTest、worker/QEMU 基线均通过。未修改 `trobot/`。
- 2026-08-28：修复 review 中的高优先级问题：新增 machine 统一 reset handler，清理 GPIO/UART/FDCAN/ADC/DMA/CAN medium/BMI088/timer/co-sim 状态并恢复 BMI088 CS；TIM2/TIM3/TIM8/TIM12/TIM24 改由共享动态 `TIMERCLK`（SYSCLK/2）驱动；修正 PLLSRC 编码、PLL1 FRACN 和整数精度；新增 `-machine dm-mc02,electrical-power=true` 供电策略，VIN/PC15 可门控 CAN 与 USART2/3 RS485；co-sim telemetry 和 FDCAN 增加有界非阻塞发送队列。QMP reset 回归、34 个 QEMU smoke、host CTest、`uv lock --check` 和 Python compile 全部通过。UART 完整 TX ring、ADC/APB 全时钟树、BMI088 噪声零漂和 v2 diagnostics 仍 pending。
- 2026-08-29：完成 TIM12_CH2/PB15 PWM 惰性观察：按 GPIO 模式/AF2 复核实际板级引脚，按当前虚拟时间计算 PWM 电平、频率和占空比，不为输出边沿创建 host timer；v1 telemetry 的 `buzzer` 改为非零输出使能语义，并新增 `buzzer-enabled`、`buzzer-level`、`buzzer-frequency-hz`、`buzzer-duty-permille` 只读 QOM 属性。新增 PWM bare-metal/QMP smoke，TIM12 1 kHz/25% 和 OC2PE 配置通过；44 个 QEMU/worker smoke、host CTest 3/3、Release RTF 约 `0.998958x` 回归通过。
## 2026-08-30 v2 协议版本固定与全量复核

- [x] 修复同一 chardev v2 session 接受 v1 `RESET` 后降级的问题；v1 `RESET` 在已建立
  v2 session 中被拒绝，后续 v2 `STEP` 仍可继续处理。
- [x] 在 `run-qemu-v2-reset-smoke.sh` 增加协议降级回归，并确认 v2 reset telemetry 使用
  24-byte board telemetry payload。
- [x] 已完成 Python pytest、shell/字节码、`uv lock --check`、QEMU build、CTest 和
  QEMU smoke 回归：pytest `180 passed`、CTest `6/6`、QEMU smoke `61/61`。
- [ ] 仍待实现：session ID、ACK/重传、v2 电机 section、完整 H723/USB/FIFO 语义和
  跨 Gazebo/MuJoCo backend 的严格锁步。
## 2026-08-30 RNG continuous output refill

- [x] 在 STM32H723 RNG 芯片层保留四字输出缓冲耗尽后的一个可观察
  `DRDY=0` 轮询边界；下一次状态轮询确定性补充四字，使连续调用
  `HAL_RNG_GenerateRandomNumber()` 不会在首次四字后永久超时。
- [x] host smoke 覆盖四字边界和补充后的第五字，QEMU bare-metal smoke
  覆盖真实 guest `SR -> DR` 轮询路径以及 IRQ/完成状态。
- [x] 重新使能 `IE` 且输出缓冲已耗尽时由 RNG 芯片层补充数据并重新评估
  IRQ 电平，覆盖 HAL 中断模式的重复调用边界。
- [ ] 仍待实现：真实熵源、健康测试、错误注入、RNG 时钟分频和精确硬件生成
  延迟；当前输出仍是可复现的 xorshift32 序列，补充动作不创建逐字 virtual timer。

## 2026-08-30 RNG conditioning/configuration contract

- [x] 在 RNG 芯片层区分当前错误与中断锁存：`CECS/SECS` 阻止新输出生成，
  `CEIS/SEIS` 保留中断原因；clock error 发生前的 FIFO 数据继续可读，当前
  `SECS` 置位时屏蔽不可用输出。
- [x] 实现 H723 v3.2 `RNG_CONFIG1/2/3`、`NISTC`、`CLKDIV` 的 `CR` 读写，
  以及 `CONFIGLOCK` 对这些配置字段和 `HTCR` 的一次性锁定；普通 reset 清除锁。
- [x] 实现可观察的 `CONDRST`：置位时保留 CR 状态、清空 FIFO/错误并隐藏
  `DRDY`，写回 0 后在 `RNGEN` 有效时重新填充；host smoke 覆盖 PRNG 重播、
  错误门控、IRQ 电平和锁后写保护，bare-metal smoke 覆盖第二次 IRQ。
- [x] 验证：QEMU 增量构建、CTest `12/12`、QEMU smoke suite、Python `243
  passed`、shell/compileall/`uv lock --check` 均通过。
- [ ] 仍待实现：真实熵源、健康测试、外部错误注入、RNG 时钟分频对生成延迟
  的影响、完整 HTCR 健康测试语义；当前配置字段只保证寄存器可观察性，不
  宣称改变 deterministic xorshift32 输出模型。

## 2026-08-30 RNG review follow-up

- [x] 修正真实 guest smoke 的完成判据：runner 等待 guest 最终 `DONE` 提交标记，
  不会在启动阶段的早期 marker 尚未完成时读取结果；同时校验首批四字、refill
  后第五字和后续第六字的确定性序列。
- [x] 按 H723 HAL 区分 RNG 当前错误与中断锁存：`CECS/SECS` 门控生成，
  `CEIS/SEIS` 仅保留中断原因；`SECS=0` 且 `SEIS=1` 时有效 FIFO 数据仍可读，
  可由 `RNG_RecoverSeedError()` 的 SR 清除路径恢复。
- [x] host smoke 覆盖 SEIS 锁存、数据继续可读和清除路径；QEMU 增量构建及
  真实 MMIO RNG smoke 通过。
- [ ] CR 配置字段、`CONFIGLOCK`、`CONDRST` 和错误注入仍未全部走真实 guest
  MMIO 流程；当前对应行为由芯片层 host smoke 覆盖，后续补充 guest 回归。
## 2026-08-31 Timer active/shadow 与 OCxREF 芯片层切片

- [x] 在通用 STM32H723 timer 模型中分离 CPU-visible shadow 配置和虚拟计数使用的
  active `PSC/ARR/CCR1..4`。支持 `PSC` 延迟到 update/UG、`CR1.ARPE` 控制 ARR、
  对应 `OCxPE` 控制 CCR，并在 active 周期变化时保持 CNT 的 update 边界和虚拟时间
  相位一致；寄存器读回仍保留 guest 写入值。
- [x] 补齐 `CR2.MMS/MMS2=4..7` 的 edge-aligned up-counting PWM1/PWM2
  `OC1REF..OC4REF` 边沿事件，复用已有同步 master-event ABI；forced inactive/active
  作为恒定电平可观察，未为每个 PWM 边沿创建 host timer。
- [x] 修复 timer 芯片层的事件边界：MMS=3 仅由 CC1 命中发布 compare pulse，输入捕获
  `CCxS!=0` 不调度 output compare，寄存器高字节读取不再依赖有符号整数移位，EGR
  非起始 byte lane 不会误触发 UG。
- [x] 新增/更新 timer 与 board boundary qtest：timer qtest `9/9`，覆盖 active/shadow
  transfer、输入捕获过滤和 EGR lane；ADC qtest 增加 TIM8 OC1REF 上升/下降触发及
  TIM2 CC1-only compare pulse 负路径/正路径。当前定向构建和测试通过。
- [ ] 当前仍待实现：冻结/active/inactive-on-match/toggle OCxM 状态机、中心对齐和
  组合/边沿模式、TIM1/TIM8 高级 OCxM 扩展、其它 timer master source、完整 DMA
  与硬件级跨总线延迟；这些必须继续按芯片层、板级边界、外部设备顺序推进。
## 2026-08-31 advanced timer RCR 芯片层切片

- [x] 在可复用 timer 芯片模型中加入可选的 `TIMx_RCR` 能力。`TIM1/TIM8` 由
  board profile 数据标记支持，`TIM2/TIM3/TIM12/TIM24` 保持保留区行为；芯片模型
  不依赖 DM-MC02 的具体引脚或外设。
- [x] 分离 RCR shadow、active REP 和当前 repetition counter。`REP=N` 时 update/
  preload/UEV/TRGO 在 `N+1` 个自然计数周期后产生，CC1..4 compare 仍按每个周期
  匹配；直接按虚拟时间安排 qualified deadline，不为被抑制的周期创建 host timer。
- [x] 新增 TIM8 qtest，覆盖 `REP=2` 的前两次 UIF 抑制、第三次 UIF、compare 独立
  产生和 system reset 清零；原有 TIM2 timer qtest 保持通过，当前定向结果 `14/14`。
- [x] 同一通用模型已加入高级 timer `BDTR` 的 `BKE/BKP/AOE/MOE` 最小输出级语义，
  并由 board profile 只为 TIM1/TIM8 开启 capability；内部 OCREF/compare/DMA/TRGO
  不被输出级 fault 门控。
- [ ] 当前仍未建模高级 timer 的完整 CMS-specific UEV 选择、COM/边沿模式、dead-time、
  第二 Break 输入、LOCK 写保护、互补输出和重复计数器逐半周期硬件细节；本切片只承诺
  现有 `0..ARR..0`/update 抽象边界上的 REP 语义。

## 2026-08-31 advanced timer output gate

- [x] 在可复用 timer 芯片层加入可选 `BDTR` capability。TIM1/TIM8 的 `MOE` 复位为
  0，`get_pwm_state()` 将 `MOE` 和 Break 状态纳入实际主 PWM 输出级；普通 timer
  对 BDTR 保持保留区行为。
- [x] 加入板卡无关的 `dm_mc02_tim2_set_break_input()`，按 `BKE/BKP` 解释物理 BKIN
  电平；Break 激活时清除 MOE，释放后由 `AOE` 在下一个 qualified update 恢复。
  OCREF、compare flag、DMA request 和 TRGO/TRGO2 保持内部信号语义。
- [x] 新增 timer qtest：普通 TIM2 BDTR 忽略、TIM8 active-high Break 清除 MOE、
  active-low + AOE 在 UG 后恢复 MOE；定向 qtest `16/16` 通过，QEMU 主体重新链接成功。
- [x] 真实 Release 固件 3 s RTF 回归：`3001` tick、RTF `0.999928x`、约 `999.928`
  tick/s、IWDG `timeouts=0`；本切片未引入可观测的实时性回退。
- [x] 增加 `DTG` 分段解码、CKD 到 timer-cycle 的转换和基于虚拟相位的主/互补门控观察；
  TIM1/TIM8 的 CH1..CH3 支持 CCxE/CCxNE、CCxP/CCxNP 与死区期间双路关闭，forced
  inactive/active 也可观察。新增 TIM1 forced 输出、死区和边界回归，当前定向 qtest
  `21/21` 通过。
- [x] 修正 H723 高级定时器分裂字段 `OCxM[3]`（CH1/3 bit16、CH2/4 bit24）的解码；
  未实现的扩展模式现在明确返回 `unsupported`，不再错误别名为低三位模式。
- [ ] 仍未实现 BKIN2、LOCK 写保护、完整 OSSI/OSSR/OIS off-state、精确逐边沿
  dead-time 事件、COM/UEV 细节和 OCxM 组合/边沿扩展；本切片不能替代完整半桥安全时序验证。

## 2026-08-31 DWC2 device-mode core 分层切片（已完成）

- [x] 所属层：可复用 USB 器件/驱动层。producer 是 `DmUsbHost` 产生的
  SETUP/IN/OUT token 或未来 transport token；boundary 是
  `DmUsbDwc2Device` 的 endpoint register/FIFO/transaction/IRQ API；consumer 是
  板级 MMIO/IRQ adapter。核心不依赖 `hcd-dwc2.c`、DM-MC02 pin map、DMA 控制器、
  QEMU USB bus 或 `trobot/`。
- [x] 实现 EP0 control dispatcher 接入、EP1..EP15 的基础 IN/OUT endpoint register
  语义、little-endian guest FIFO 读写、`DIEPTSIZ/DOEPTSIZ` transfer/packet count
  递减、`XFRC/STUP`、`DAINT`、`GINTSTS.IEPINT/OEPINT`、`GINTMSK/GAHBCFG/DAINTMSK`
  level-sensitive IRQ，以及 endpoint STALL/clear-halt 的 PID 重置边界。
- [x] 修正底层实现中的首个编译错误（混用的 DWC2 宏前缀）、寄存器跨界/非法 3-byte
  访问和 reset 时 IRQ 未撤销；FIFO-consuming read API 改为明确的可变设备参数。
- [x] 最小隔离门：`test-dm-usb-dwc2-device` `5/5`，覆盖 reset defaults、EP1 bulk
  IN/OUT FIFO、计数清零、EP0 descriptor/control status、IRQ mask/W1C、非法 MMIO
  边界和 reset deassert。直接边界门：control/transaction/host unit
  `5/5`/`5/5`/`4/4`、DM-MC02 USB qtest `8/8`。
- [ ] 限制与下一道门：当前没有 DMA 地址搬运、`RXFLVL`/SOF、PHY/电气时序、USB bus
  attachment、host 枚举或异步 completion/cancel；EP0 与 EP1..EP15 仍通过进程内
  transaction callback 使用。下一步先实现 DM-MC02 的地址映射和真实 NVIC IRQ 接线，
  再用板级 guest smoke 验证，不直接宣称宿主机 USB 设备可挂载。

## 2026-08-31 QEMU USB device adapter 分层切片（已完成）

- [x] 所属层：可复用 USB transport adapter。producer 是 QEMU 8.2.2 的
  `USBDeviceClass`/`USBPacket` 回调，boundary 是 `DmUsbQemuAdapter`，consumer 是
  带虚拟时间戳的 `DmUsbTransaction` submit/reset callback；adapter 不依赖 DM-MC02
  pin map、DWC2 私有寄存器或外部 plant。
- [x] `USBPacket` data path 已转换为 transaction。control IN 在 QEMU generic core
  的 SETUP 回调中准备底层 SETUP、分包 IN 和 status OUT；control OUT 在 generic core
  的 status-IN 回调中提交 SETUP、完整 OUT data 和 status IN。该同步折叠与 QEMU
  `core.c` 的实际 callback 生命周期由 `usb_handle_packet()` harness 覆盖。
- [x] 当前 adapter 能力明确收窄为 Full-Speed、64-byte EP0/data packet；不再声明
  High-Speed 但仍使用 64-byte bulk MPS。QEMU 共用 `data_buf` 只在同步回调期间使用，
  重入 submit/handler 会返回 `USB_RET_IOERROR`，避免嵌套调用覆盖外层数据。
- [x] 隔离门：`test-dm-usb-qemu-adapter` `7/7`，覆盖 control IN 多包、control OUT
  data/status、bulk IN/OUT 多段 iov、NAK 映射、reset callback、realize 配置和重入
  防护；测试通过 QEMU generic `usb_handle_packet()`，不是直接调用 class handler。
- [x] 首错修复：setup 数组参数上的 `sizeof(*setup)` 曾生成 1-byte transaction，已
  改为固定 8-byte USB SETUP；unit harness 显式调用 `MODULE_INIT_QOM`，并只 stub
  monitor/pcap/migration 元数据依赖，不把完整系统依赖混入隔离目标。
- [x] QEMU bus 边界：unit fixture 使用具体 QOM host、真实 `usb_bus_new()`、单个
  Full-Speed `USBPort` 和 `usb_realize_and_unref()` 挂接 adapter；port reset 后以
  `usb_find_device()` 解析地址 0 并传输 transaction。teardown 按 device unparent、port
  unregister、bus release、bus unparent 的顺序验证 port ownership 和 `nfree/nused`，不再
  手工伪造 `parent_bus` 或 `USBPort.dev`。
- [x] 验证：adapter unit `7/7`；`qemu-system-arm` 重链；DWC2 controller-ready 和
  legacy FIFO0 CDC byte-pipe smoke 通过；host CTest 串行 `22/22`，其中整机 QEMU smoke
  `79/79`。未修改 `trobot/`。
- [x] DWC2 consumer 边界：同一 real-`USBBus` fixture 的 submit/reset callback 直接调用
  `dm_usb_dwc2_submit()`/`dm_usb_dwc2_reset()`；QEMU EP1 IN packet 按公共 transaction
  API 消费预配置的 DWC2 FIFO，并精确验证 payload、`DIEPTSIZ=0`、`DIEPINT.XFRC` 与
  port reset 后 endpoint state 清零。adapter 没有读取 DWC2 私有字段、DM-MC02 MMIO 或
  board pin map。
- [x] 直接边界验证：adapter unit `7/7`、DWC2 unit `5/5`。这只增加 test target 的
  DWC2 source dependency，不改变 system source 或 machine composition。
- [ ] 限制与下一道集成门：adapter 尚未由 DM-MC02 machine 实例化，也没有 QEMU host
  controller、枚举、SOF、异步 completion/cancel、PHY 或宿主机设备挂载。真实 `USBBus`
  目前只存在于 test-only fixture；下一道门应先定义并验证一个明确的 host-controller
  transport 边界，不能把这个 fixture 或当前 transaction callback 当成 DM-MC02 宿主机
  USB 支持。

## 2026-08-31 STM32H7 DWC2 host channel 到 transaction 边界（已完成）

- [x] 所属层：host channel 属于 STM32H723 USB host-controller 层；producer 是 guest
  对 `HCCHAR/HCTSIZ` 的编程，boundary 是带虚拟时间戳的
  `DmStm32H7OtgHostChannelRequest`/completion，直接 consumer 是板卡无关的
  `DmUsbHostChannelTransport`，它再通过 `DmUsbTransaction` 调用任意 USB device 或
  transport。三层均不依赖 DM-MC02 地址、NVIC、QOM、`USBPort`、trobot 或外部 plant。
- [x] 实现十二个 DWC2 host channel 的 `HCCHAR`、`HCTSIZ`、`HCINT/HCINTMSK` 与
  `HAINT/HAINTMSK`，并派生 `GINTSTS.HCINT`。单包 producer 按 port 的 PWR/connect/ENA/
  reset 状态门控；accepted 会准确递减 transfer/packet count，NAK、STALL、transaction
  error 和 CHDIS 有明确中断/停止语义。多包后的下一 packet 仍须显式
  `service_channel()`，不在本切片伪造 SOF 或 wall-clock 重试。
- [x] `DmUsbHostChannelTransport` 使用注入的 OUT reader/IN writer 表达未实现的
  PIO/DMA 数据面，以单个 2047-byte 同步 scratch buffer 转换 SETUP/IN/OUT 和
  ACCEPTED/NAK/STALL/INVALID。它没有板级代码、线程、队列或异步状态，未来能分别接
  FIFO、DMA 或 test fixture；DATA2/MDATA/isochronous 语义尚未承诺。
- [x] 隔离门：`test-dm-stm32h7-otg-host` `9/9`，覆盖 register 默认值、port gate、完整
  request 字段/时间戳、W1C/IRQ、NAK 与多包显式服务；直接边界门：
  `test-dm-usb-host-channel-transport` `3/3`，覆盖精确 OUT 字节、短包 IN 写回和
  NAK-to-STALL。相邻 host-port/DWC2/transaction unit 分别 `2/2`、`6/6`、`5/5`。
- [x] 兼容验证：重建 Release `qemu-system-arm`；DM-MC02 USB qtest `9/9`；DWC2
  controller-ready、CDC byte-pipe smoke 通过；完整串行 QEMU smoke suite `79/79`。
  未修改 `trobot/`。
- [x] 下一道门已在下列 SOF/frame 切片完成；DM-MC02 仍不组合 host-role machine。

## 2026-08-31 STM32H7 DWC2 host SOF/frame 调度（已完成）

- [x] 所属层：STM32H723 host-controller timing。producer 是 board adapter 将 QEMU
  virtual clock 的单调时间交给 `dm_stm32h7_otg_host_advance_time()`；boundary 是 SOF、
  `HFNUM.FRNUM` 与既有 `service_channel()`；consumer 是未完成的 host channel/transport。
  该层不依赖 DM-MC02 地址、NVIC、QOM timer、USBPort、DMA 或 device model。
- [x] reset-release 后启动 frame clock：Full/Low-Speed 为 1 ms，High-Speed 为 125 us。
  每个 frame 精确递增 `HFNUM`、置位 W1C `GINTSTS.SOF` 并服务 eligible channel；reset
  assert、disconnect 与 PWR-off 停止时钟，PWR restore 后必须重新 port reset release。
- [x] 隔离门：`test-dm-stm32h7-otg-host` `12/12`，新增 Full/High-Speed 间隔、SOF IRQ
  W1C、多包下一 frame dispatch 与端口掉电/复位重启覆盖。host-channel transport
  `3/3`、host-port `2/2`、DWC2 `6/6`、transaction `5/5` 作为相邻回归。
- [ ] 下一道门：保持在同一 host-controller 层实现 host PIO FIFO 的最小数据面，并让
  `DmUsbHostChannelTransport` 的 reader/writer 由 FIFO consumer/producer 替代；先做
  FIFO packet 边界和 HCTSIZ direct test，再讨论 HCDMA/DMA 或 host-role board profile。

## 2026-08-31 STM32H7 DWC2 host PIO FIFO 数据面（已完成）

- [x] 所属层：STM32H723 host-controller 数据面。producer 是 guest 对
  `HCFIFO(n)=0x1000+0x1000*n` 的 32-bit PIO 访问；boundary 是板卡无关的
  `DmUsbHostPioFifo` 与 `DmStm32H7OtgHost` 的 OUT reader/IN writer callback；consumer
  是既有同步 `DmUsbHostChannelTransport`。它们都不依赖 DM-MC02 地址、NVIC、QOM、
  `USBPort`、DMA、外部设备或 `trobot`。
- [x] 每个十二个 host channel 都拥有独立的 2048-byte IN/OUT 环形 FIFO（每个 host
  实例共 48 KiB）。MMIO word 以 little-endian 字节顺序写入 OUT、从 IN 读出；controller
  reset 清空所有 FIFO。OUT 只在请求所需字节完整存在时消费，因而缺少 PIO 数据不会产生
  部分 packet。IN FIFO 容量不足经 writer callback 映射为 `HCINT.XACTERR|CHHLTD`。
- [x] `DmUsbHostChannelTransport` 已直接配对 host FIFO callback。同步 submit 若错误地
  报告超过当前 `HCTSIZ` packet 的 accepted 长度，会在触碰 IN FIFO 前停止 channel 并报告
  `XACTERR|CHHLTD`，不会暴露 scratch buffer 未定义范围。
- [x] 隔离和直接边界门：`test-dm-usb-host-pio-fifo` `3/3` 覆盖顺序、exact-read 不消费
  和环绕/容量；`test-dm-stm32h7-otg-host` `13/13` 覆盖 word packing 与 controller-reset
  FIFO 清理；`test-dm-usb-host-channel-transport` `6/6` 覆盖精确 OUT、短 IN、FIFO 满与
  oversized accepted-IN 拒绝。
- [x] 最终回归门：Release `qemu-system-arm` 已重链；host-port `2/2`、DWC2 `6/6`、
  transaction `5/5` 和 DM-MC02 USB qtest `9/9` 通过；完整串行 QEMU smoke suite
  `79/79` 通过。未修改 `trobot/`。
- [ ] 限制与下一道门：这只是固定容量、每 channel 的同步 packet stream，不是完整的
  DWC2 全局 RX status queue 或动态 TX FIFO 分配；没有 `GRXFSIZ/GNPTXFSIZ/HPTXFSIZ`、
  `GRSTCTL` FIFO flush、RX status、FIFO arbitration、HCDMA 地址搬运、cache coherency、
  async completion/cancel、枚举、PHY 或 host-role board。下一切片必须先为 `HCDMA` 定义
  SoC memory boundary 和完成/错误时序，再考虑接入现有 DMA 或 QEMU USB bus。

## 2026-08-31 STM32H7 DWC2 HCDMA 与可复用数据路径（已完成）

- [x] 所属层：STM32H723 host-controller DMA register/data boundary。producer 是 guest
  对 `GAHBCFG.DMAEN`、`HCDMA(n)=0x514+0x20*n`、`HCCHAR/HCTSIZ` 的编程；boundary 是
  `DmUsbHostChannelDataPath` 的无 QEMU memory read/write callback；consumer 是既有同步
  `DmUsbHostChannelTransport`。没有 DM-MC02 地址、QOM、`AddressSpace`、`USBPort` 或
  外部设备策略进入芯片层。
- [x] accepted completion 仅在 `DMAEN=1` 时按 `actual_length` 推进 `HCDMA`；PIO、NAK、
  STALL 和 transaction error 不推进。实现中发现 controller reset 原先遗漏清除 host
  channel registers/active state，已在同一层修复；reset 现在也清 `HCDMA`。
- [x] `DmUsbHostChannelDataPath` 以 `DMAEN` 选择 host PIO FIFO 或注入的物理地址
  memory callback。DMA OUT 从本次 `HCDMA` 读，DMA IN 写入本次 `HCDMA`，随后由 controller
  统一推进地址；未安装 callback、地址空间拒绝、FIFO 不足或 FIFO 满均通过原有 transport
  completion 映射为 `XACTERR|CHHLTD`。
- [x] 隔离门：host controller `14/14` 覆盖 HCDMA progress/NAK/reset；
  `test-dm-usb-host-channel-data-path` `3/3` 覆盖 PIO/DMA 选择、memory failure 与缺失
  binding；channel transport `7/7` 验证 DMA OUT/IN 绕过 PIO 并精确推进地址。
- [x] transport 现在允许 submit、OUT reader 和 IN writer 各有独立 opaque context；旧
  `init(..., opaque)` 保持为三者共用同一 context 的兼容包装。这样 transaction consumer
  与 data-path selector 不会再因错误的 context 转换相互耦合。
- [x] 最终回归门：Release `qemu-system-arm` 已重链；host PIO `3/3`、host controller
  `14/14`、data path `3/3`、channel transport `7/7`、host-port `2/2`、DWC2 `6/6`、
  transaction `5/5`、DM-MC02 USB qtest `9/9` 和完整 QEMU smoke suite `79/79` 均通过；
  未修改 `trobot/`。
- [ ] 限制与下一道门：memory callback 仍是板卡无关同步边界，尚未绑定 QEMU
  `AddressSpace`，没有 H723 cache coherency、AHB burst/错误 IRQ、descriptor DMA、
  `HCDMAB`、异步 completion、FIFO arbitration、枚举、PHY 或 host-role board。下一切片
  必须把这组 callback 以独立 QEMU adapter 绑定到 system memory，随后才评估 test-only
  `USBBus` 接入；不得把它接入当前 DM-MC02 device-mode 窗口。

## 2026-08-31 DWC2 host DMA 的 QEMU AddressSpace 适配（已完成）

- [x] 所属层：QEMU system-memory adapter，而非 STM32H723 host-controller 或 DM-MC02
  board。producer 是 `DmUsbHostChannelDataPath` 的同步 32-bit physical-address
  read/write callback；boundary 是 `DmUsbHostQemuMemory` 持有的调用方
  `AddressSpace *`；consumer 是 QEMU `dma_memory_read()`/`dma_memory_write()`。
  通用芯片层因此仍不包含 QOM、`AddressSpace`、machine RAM map 或 board host-role 策略。
- [x] 适配器将 `MEMTX_OK` 映射为 callback success，任何 QEMU memory transaction
  失败映射为 `false`，由既有 transport 完成为 `HCINT.XACTERR|CHHLTD`。HCDMA 的 32-bit
  byte address 仍只由 controller 在 accepted completion 后推进；adapter 不推进地址、
  不分配 buffer，也不引入线程、队列或重试。
- [x] 最小隔离门：`test-dm-usb-host-qemu-memory` `1/1` 以受控的
  `address_space_rw()` stub 断言地址、长度、读写方向、字节流和 `MEMTX_ERROR -> false`。
  `test-dm-usb-host-channel-transport` 新增 QEMU-memory data-path integration case，
  使其当前为 `8/8`；它验证 DMA OUT/IN 经 `transport -> data path -> QemuMemory`
  到 `dma_memory_*` 的完整 callback 边界。
- [x] 最终回归门：Release `qemu-system-arm` 已重链；host PIO `3/3`、host controller
  `14/14`、data path `3/3`、QEMU-memory adapter `1/1`、channel transport `8/8`、
  host-port `2/2`、DWC2 `6/6`、transaction `5/5` 和 DM-MC02 USB qtest `9/9` 全部通过；
  完整串行 QEMU smoke suite 为 `79/79`。未修改 `trobot/`。
- [ ] 限制与下一道门：没有 cache coherency、IOMMU、AHB burst/fault IRQ、`HCDMAB`、
  async completion、global FIFO arbitration、enumeration、PHY 或 board host-role wiring。
  下一步只能评估 test-only QEMU USB-host composition；不得接入当前 DM-MC02 device-mode
  controller window。

## 2026-08-31 QEMU USB host-device transport 组合边界（已完成）

- [x] 所属层：QEMU USB composition adapter，而非 STM32H723 或 DM-MC02 board。producer
  是既有 `DmUsbHostChannelTransport` 的同步 `DmUsbTransaction` submit callback；boundary
  是只持有一个调用方管理的 `USBDevice *` 的 `DmUsbHostQemuTransport`；consumer 是 QEMU
  `usb_ep_get()`、`usb_handle_packet()` 和 `USBPacket` lifecycle。controller 保留 HCDMA、
  PIO、channel completion 和 timestamp ownership，board 没有 host MMIO、IRQ 或 pin route。
- [x] `SETUP/IN/OUT` 映射为对应 QEMU token，`USB_RET_SUCCESS/NAK/STALL` 映射为
  `ACCEPTED/NAK/STALL`，其它 result 为 `INVALID`。目标 device 必须已 attached 且处于
  `USB_STATE_DEFAULT`；未 ready port 直接返回 `INVALID`，不触发 QEMU assertion。若 QEMU
  device 返回 async/queued packet，adapter 立即 cancel 并返回 `INVALID`，不会让 stack packet
  越过本次 synchronous submit。
- [x] 最小隔离门：真实 QOM `USBBus` fixture 的 host transport case 覆盖 SETUP、OUT、短
  IN、NAK、STALL 和 port 尚未 reset 的拒绝；异步 fixture 验证 queue 已清空且 cancel callback
  恰好一次。direct consumer 门将 host controller 的 port reset 通过 `usb_port_reset()` 接入，
  并验证 `HCFIFO -> host channel -> QEMU device -> generic transaction device` 的 exact OUT/IN
  字节与 channel completion。`test-dm-usb-qemu-adapter` 当前 `10/10`。
- [x] 最终回归门：Release `qemu-system-arm` 已重链；QEMU adapter `10/10`、host PIO
  `3/3`、host controller `14/14`、data path `3/3`、QEMU-memory adapter `1/1`、channel
  transport `8/8`、host-port `2/2`、DWC2 `6/6`、transaction `5/5` 和 DM-MC02 USB qtest
  `9/9` 全部通过；完整串行 QEMU smoke suite 为 `79/79`。未修改 `trobot/`。
- [ ] 限制与下一道门：adapter 只路由一个已选 `USBDevice`，不读取 host request 的 device
  address，也不支持 hub/topology、多设备枚举、PID/toggle 传播、async completion/cancel
  delivery、isochronous、PHY/VBUS/passthrough 或 guest-visible host-role board。下一层必须先
  在可复用 host transport boundary 定义并测试 address-routing contract，再评估多 device 的
  QEMU bus lookup；不得用这个 fixture 直接宣称 DM-MC02 USB host 支持。

## 2026-08-31 USB host device-address routing boundary（已完成）

- [x] 所属层：可复用 host-channel transport 到 QEMU composition adapter。producer 是
  `DmStm32H7OtgHostChannelRequest.device_address`（精确来自 `HCCHAR.DEVADDR`）；boundary
  是可选 `DmUsbHostChannelTransportRoute(device_address, transaction)`；consumer 可以是任何
  协议/板卡无关 router，或 QEMU 专属的 `DmUsbHostQemuTransport` port router。地址没有写入
  `DmUsbTransaction`，因此 transaction 仍只描述 token、endpoint、PID、数据和虚拟时间。
- [x] 旧 `submit(transaction)` 初始化器保持单目标兼容；仅当 caller 显式配置 route callback
  时，transport 才将精确 device address 和同一次同步 transaction 交给 router。unit `10/10`
  覆盖 legacy target 在 nonzero address 下不变，以及 route 收到 `HCCHAR.DEVADDR=37`。
- [x] `DmUsbHostQemuTransport` 新增 caller-owned `USBPort` 模式，route 只调用
  `usb_find_device(port, address)` 并复用既有 packet lifecycle。`DmUsbQemuAdapter` 在底层
  标准 `SET_ADDRESS` 成功完成 status-IN 后才提交 QEMU `USBDevice.addr`；reset 仍由 QEMU core
  清回 0。修复了 adapter 先前把无数据 OUT control request 伪造成零长度 data OUT 的顺序错误。
- [x] 验证：Release `qemu-system-arm` 重链；QEMU adapter `11/11`、channel transport `10/10`、
  host controller `14/14`、host/data-path/QEMU-memory/PIO/port/control/transaction/DWC2 unit
  分别 `4/4`、`3/3`、`1/1`、`3/3`、`2/2`、`6/6`、`5/5`、`6/6`；DM-MC02 USB qtest `9/9`；
  完整串行 QEMU smoke `79/79`。未修改 `trobot/`。
- [ ] 限制与下一道门：port router 只使用 QEMU 已连接 device tree 的既有地址查找，不实现
  hub 枚举、地址分配策略、PID/toggle、async completion delivery、isochronous、PHY/VBUS、
  passthrough 或 DM-MC02 host-role board。下一道门必须先在 reusable host-controller/transport
  层定义并验证完整 control-enumeration scheduler，再由单独 host-role board profile 组合；当前
  DM-MC02 `USB1_OTG_HS` 继续只表示 Device mode。

## 2026-08-31 QEMU USBPort host lifecycle adapter（已完成）

- [x] 所属层：QEMU USB composition adapter。producer 是 QEMU `USBPortOps` 的真实 device
  attach/detach 与已存在 H723 host-controller 的 reset callback；boundary 是
  `DmUsbHostQemuPort`；consumer 是 `DmStm32H7OtgHost.HPRT0` connection state 与 QEMU
  `usb_port_reset()`。它不属于 STM32H723 芯片模型、DM-MC02 board 或 generic transaction
  protocol。
- [x] adapter 自己注册一个 caller-owned `USBBus` 上的 root `USBPort`，仅持有 bus、host 与
  port 的借用关系。真实 attach 按 QEMU negotiated Low/Full/High-Speed 设置
  `CONNSTS|CONNDET|SPD`；真实 detach 清除 `CONNSTS/ENA` 并锁存 `CONNDET|ENACHG`。
  H723 `HPRT0.RST` 回调调用 QEMU `usb_port_reset()`，在其内部 detach/attach 期间临时抑制
  host connection 更新，从而不把 electrical reset 误报为物理拔插。
- [x] 最小/直接消费者门：真实 QOM bus fixture 验证 attach、`CONNDET` W1C、host reset
  release 后 `CONNSTS/ENA` 保持且 QEMU address 清零、真实 detach/re-attach 的 HPRT0
  变化。adapter unit 已为 `12/12`；host controller `14/14`、channel transport `10/10`、
  DM-MC02 USB qtest `9/9` 通过；最终回归见本次 smoke `79/79`。未修改 `trobot/`。
- [ ] 限制与下一道门：当前只有单 root port lifecycle，不提供 host machine、Hub 类请求、
  topology/address allocation、PID/toggle、async completion delivery、isochronous、PHY/VBUS
  或 passthrough。下一道门是 reusable host-controller 的真实 control-enumeration scheduler
  与完成时序，之后才能设计独立的 host-role board profile；DM-MC02 USB Device-mode 映射不变。

## 2026-08-31 STM32H7 host control-enumeration scheduler（已完成）

- [x] 所属层：STM32H723 host-controller fixture/client 层。producer 是调用者的 control
  request 与虚拟时间；boundary 是 `DmUsbHostChannelControl` 对既有
  `HCCHAR/HCTSIZ/HCINT/HCFIFO` register/data path 的同步编排；consumer 是
  `DmUsbHostChannelTransport` 及其可选 device-address router。该 helper 不依赖
  DM-MC02 地址、NVIC、QOM、`USBBus` 或 QEMU device ownership。
- [x] 每个 control stage 都通过真实 channel state 提交：SETUP、按 EP0 MPS 分包的 IN/OUT
  data、反向 zero-length status。它从 `HCINT` 解码 completion，不绕过 controller；输入地址
  在写入 `HCCHAR.DEVADDR` 前限制为 `0..127`，避免破坏相邻 bit。标准 `SET_ADDRESS` 的
  setup/status 都在旧地址执行，只有成功 status-IN 后的下一次 request 才能路由到新地址。
- [x] 最小隔离门：`test-dm-usb-host-channel-control` `4/4` 覆盖 80-byte 分包 descriptor
  IN、class control OUT/status、address 0 到 13 的准确 stage route 与超范围地址拒绝。直接
  QEMU consumer 门：`test-dm-usb-qemu-adapter` `13/13` 在实际
  `DmUsbHostQemuPort` lifecycle 上执行 `GET_DESCRIPTOR -> SET_ADDRESS -> GET_STATUS`，验证
  reset 后 port 仍连接、QEMU address 仅在 status-IN 后变为 13、router 能继续找到该 device。
- [x] 当前定向回归：Release `qemu-system-arm` 已重链；host controller `14/14`、control
  scheduler `4/4`、data-path `3/3`、channel transport `10/10`、QEMU-memory `1/1`、PIO
  `3/3`、port `2/2`、QEMU adapter `13/13`、control `6/6`、transaction `5/5`、DWC2 `6/6`
  均通过；DM-MC02 USB qtest `9/9` 与完整串行 QEMU smoke suite `79/79` 通过。未修改
  `trobot/`。
- [ ] 限制与下一道门：这是同步、单 channel 的 test/integration helper，不是可由真实 guest
  firmware 替代的 host driver。没有 descriptor parser/endpoint configuration、address
  allocation policy、hub/topology、多 device 并发、NAK retry/timeout、async completion/cancel、
  isochronous、PID/toggle fidelity、global FIFO/HCDMA descriptor、PHY/VBUS/passthrough 或
  host-role board。下一切片应先选择一个独立 host-role board profile 的最小 machine
  composition 契约；当前 DM-MC02 `USB1_OTG_HS` 必须继续保持 Device mode。

## 2026-08-31 STM32H723 host-role reference profile（已完成）

- [x] 所属层：QEMU composition 与独立 H723 board-profile 层。producer 是已验证的
  `DmStm32H7OtgHost`、channel transport/data path、QEMU memory/port/route adapters；
  boundary 是可复用 `dm-stm32h7-otg-host-qemu` SysBus wrapper；consumer 是
  `stm32h723-usb-host` profile 选择的 canonical MMIO address 和 IRQ 77/NVIC route。
  `dm-mc02` machine 的 USB Device-mode mapping 完全未修改。
- [x] wrapper 拥有 controller MMIO、sysbus IRQ、QEMU virtual-time SOF timer、`USBBus`、
  一个 root `USBPort` 与 router。FIFO 访问保持 1/2/4-byte 宽度，HCDMA 通过既有
  `DmUsbHostQemuMemory` 使用 machine system `AddressSpace`；wrapper 不拥有 descriptor
  parsing、board pin、NVIC vector 或 firmware enumeration policy。
- [x] 新 machine `stm32h723-usb-host` 只组合 H723 memory/ARMv7-M 和 wrapper，在
  `0x40040000` 映射 host MMIO、在 external IRQ 77 连接 IRQ。用户可通过
  `-device usb-kbd` 等 QEMU virtual USB device 使用唯一 root port；这不是 DM-MC02
  host-mode feature。
- [x] 最小 profile gate：新 qtest `stm32h723-usb-host-test` `2/2` 以真实 QEMU `usb-kbd`
  验证 attach、HPRT0 reset、NVIC pending、QEMU virtual SOF 和 EP0 device descriptor。
  定向相邻回归：host controller `14/14`、control scheduler `4/4`、channel transport
  `10/10`、QEMU adapter `13/13`、DM-MC02 USB qtest `9/9`，Release `qemu-system-arm`
  已重链；未修改 `trobot/`。
- [x] 最终回归门：完整串行 QEMU smoke suite `79/79` 通过。power-boundary smoke 仍会输出
  既有 QEMU header 的 `-Wpedantic` warnings，未由本切片引入。
- [ ] 限制与下一道门：仅支持同步单-root-port QEMU device route；不实现 guest-side
  descriptor parser、address allocation、hub/topology、多设备并发、NAK retry/timeout、
  async completion/cancel、PID/toggle、全局 FIFO/HCDMA descriptor、PHY/VBUS、电气时序或
  USB passthrough。任何完整 host firmware/demo 都必须先按 controller/driver 边界补齐这些
  所需下层语义，不能把 reference profile 当作 DM-MC02 USB host 支持。

## 2026-08-31 STM32H723 guest EP0 control-transfer smoke（已完成）

- [x] 所属层：可复用 STM32H723 host-controller client/driver 层。producer 是 caller
  提供的 EP0 setup/data、channel、MPS 与 device address；boundary 是
  `dm_stm32h7_usb_host_control.[ch]` 对既有
  `HPRT0/HCCHAR/HCTSIZ/HCINT/HCFIFO` 寄存器语义的 polling 使用；consumer 是独立
  `stm32h723-usb-host` reference profile。该 driver 不依赖 DM-MC02、QOM、`USBBus`、
  QEMU address space、descriptor parser 或地址分配策略。
- [x] `port_reset()` 使用 HPRT0 reset assert/release 确认已上电、连接且 enabled；
  `control_transfer()` 经真实 PIO FIFO 顺序执行 SETUP、按 EP0 MPS 分包的 IN/OUT data 和
  反向 zero-length status。返回值只映射同步 `HCINT` completion；caller 仍拥有 address，
  因此只有 `SET_ADDRESS` 成功返回后才更新下一 request 的 address。
- [x] 直接 consumer gate：`tools/run-stm32h723-usb-host-smoke.sh` 编译真实 Cortex-M7
  guest，经真实 QEMU `usb-kbd` 完成
  `GET_DESCRIPTOR -> SET_ADDRESS(5) -> GET_STATUS`。`GET_STATUS=0x0001` 是该 QEMU
  device 的 self-powered status，不是 NAK/transaction error。最初 smoke 将此 payload
  误当作错误码；已在 caller result 与 DTCM 逐字检查后修正断言。
- [x] 定向回归：guest smoke 通过；host profile qtest `2/2`、host controller `14/14`、
  control scheduler `4/4`、channel transport `10/10`、QEMU adapter `13/13` 和
  DM-MC02 USB qtest `9/9` 均通过。最终串行 QEMU smoke suite 为 `80/80`；未修改
  `trobot/`。
- [ ] 限制与下一道门：这是同步单-channel EP0 client，不实现 descriptor parsing、endpoint
  configuration、address allocation、hub/topology、多设备、NAK retry/timeout、async
  completion/cancel、PID/toggle、global FIFO/HCDMA descriptor、PHY/VBUS、passthrough 或
  DM-MC02 USB Host board。下一切片应从独立 host profile 的 descriptor/endpoint boundary
  开始，不能把此 smoke 扩展为 DM-MC02 Host 支持。

## 2026-08-31 USB configuration descriptor boundary 与直接 root-port 验证（已完成）

- [x] 所属层：可复用 USB firmware driver 层。producer 是已经由任意 EP0 client 取得的
  configuration descriptor byte span；boundary 是 freestanding
  `DmUsbHostConfiguration`/`DmUsbHostInterface`/`DmUsbHostEndpoint` 及其 parser；consumer
  是独立 H723 reference-profile 的 Cortex-M7 smoke。parser 不依赖 MMIO、QEMU、DM-MC02、
  endpoint scheduler 或 address policy，因此可由后续 host firmware/client 重用。
- [x] `dm_usb_host_descriptor_parse_configuration_header()` 只接受固定 9-byte configuration
  header；full parse 以 `wTotalLength` 验证整个 descriptor 序列，拒绝截断、零长度和不足长度的
  interface/endpoint descriptor。interface lookup 以 number/alternate setting 定位，endpoint
  lookup 以该 interface 内的 ordinal 定位；它不选择 configuration、不安排 endpoint transfer。
- [x] 直接 consumer gate：Cortex-M7 smoke 完成
  `GET_DEVICE_DESCRIPTOR -> SET_ADDRESS(5) -> GET_STATUS -> GET_CONFIGURATION(header/full) ->
  parse -> SET_CONFIGURATION(1)`。直接 root-port `usb-kbd` 的实际 configuration 长度为 34、
  value 为 1、HID interface 为 `0/0`、interrupt-IN endpoint 为 `0x81`/attributes 3/MPS 8；
  `GET_STATUS=0x0000`。
- [x] 首错修复：先前 bare `-device usb-kbd` 触发 QEMU 在空 bus 上自动插入 `usb-hub`，旧 smoke
  仅检查 device descriptor 的前两个 byte，因而把 hub 误判为 keyboard。所有项目调用现显式使用
  `-device usb-kbd,bus=usb-bus.0,port=1`；qtest 同时按 `HPRT0.SPD` 选择 SOF 周期，High-Speed
  为 125 us，Full/Low-Speed 为 1 ms，避免 1 ms 推进时错误地观察到 `HFNUM + 8`。
- [x] 验证：descriptor CTest `1/1`、完整 CTest `23/23`；host-controller `14/14`、channel-control
  `4/4`、QEMU adapter `13/13`、host profile qtest `2/2`、DM-MC02 Device-mode USB qtest `9/9`
  与 Cortex-M7 guest smoke 均通过；完整串行 QEMU smoke suite 为 `80/80`。未修改 `trobot/`。
- [ ] 限制与下一道门：当前只验证单个直接 root-port、同步 EP0 configuration discovery；没有
  generic endpoint transfer/scheduling、configuration selection policy、hub/topology、多设备、
  NAK retry/timeout、async completion/cancel、interrupt/bulk data path、PID/toggle、全局 FIFO/
  descriptor DMA、PHY/VBUS/passthrough 或 DM-MC02 Host capability。下一切片应先定义可复用
  endpoint/pipe configuration boundary，再添加其隔离与直接 consumer 测试。

## 2026-08-31 USB endpoint pipe configuration boundary（已完成）

- [x] 所属层：可复用 USB firmware driver 层。producer 是已验证的
  `DmUsbHostEndpoint` descriptor metadata；boundary 是 `DmUsbHostPipe`；consumer 是独立 H723
  guest smoke 和未来任意 host endpoint driver。pipe 不依赖 H723 register/QEMU/DM-MC02/port speed/
  scheduler，后续 controller adapter 只能消费 pipe，不能重解 descriptor bytes。
- [x] `dm_usb_host_pipe_from_endpoint()` 规范化 7-bit device address、endpoint number/direction、
  raw attributes、transfer type、effective MPS、interval 与 `wMaxPacketSize[12:11]+1` 的
  transactions-per-microframe。它拒绝 address 越界、endpoint zero/reserved address bit、zero MPS、
  reserved MPS bit，及 control/bulk 的 multi-transaction encoding；失败不修改 caller 的 pipe。
- [x] 隔离门：`dm_usb_host_pipe_smoke` 覆盖 HID interrupt-IN、三 transaction isochronous pipe、
  所有 public validation boundary 与 no-mutation failure。直接 consumer gate：Cortex-M7 smoke
  将 `usb-kbd` endpoint `0x81` 配置成 address 5、endpoint 1、IN、interrupt、MPS 8、one-transaction
  pipe 后才提交 `SET_CONFIGURATION(1)`。
- [x] 验证：pipe/descriptor CTest `2/2`，完整 CTest `24/24`；Cortex-M7 host smoke、host profile
  qtest `2/2`、host-channel-control `4/4` 和完整 QEMU smoke suite `80/80` 通过。未修改
  `trobot/`。
- [ ] 限制与下一道门：pipe 只表示静态 endpoint configuration，不检查当前 port speed、不会 program
  `HCCHAR`、不拥有 channel/PID/toggle/NAK retry/timeout/queue，也不传输 interrupt/bulk data。下一切片
  应在 H723 host-controller client 层定义一个仅执行单 packet、由 pipe 驱动的 synchronous endpoint
  transaction boundary；先验证 PIO/HCINT byte/completion，再考虑 periodic scheduling。

## 2026-08-31 H723 pipe 到 single-packet PIO boundary（已完成）

- [x] 所属层：STM32H723 host-controller client 层。producer 是已验证的 `DmUsbHostPipe`、显式
  `DATA0/DATA1` 与单 packet payload；boundary 是
  `dm_stm32h7_usb_host_pipe_encode()` 和 `dm_stm32h7_usb_host_pipe_transfer()`；consumer 是独立
  H723 guest smoke。该 client 不依赖 QEMU/DM-MC02/board profile，只有 caller 提供的 MMIO base 和
  channel 进入实现。
- [x] pure encoder 以 `PKTCNT=1` 生成 H723 `HCCHAR/HCTSIZ`，包含 device address、endpoint number、
  transfer type、direction、MPS、length 和 PID。它只接受 one-transaction pipe、DATA0/DATA1 及
  `length <= MPS`，不会在 validation 失败时访问 MMIO。PIO transfer 通过真实 FIFO/`HCINT.CHHLTD`
  同步完成，并将 `XFRC/NAK/STALL/XACTERR` 映射为公共 result；不猜测 toggle 或 retry。
- [x] 隔离门：`dm_stm32h7_usb_host_pipe_smoke` 覆盖 interrupt-IN/bulk-OUT 的精确 register word、
  zero-length、multi-transaction、PID/type/MPS invalid boundary。直接 consumer gate：guest 在
  `SET_CONFIGURATION(1)` 后以 endpoint 1 的 DATA0 interrupt-IN 发起一 packet；idle QEMU keyboard
  返回 `NAK`，验证真实 `pipe -> HCCHAR/HCTSIZ/HCFIFO/HCINT -> QEMU` 路径而非伪造 zero-length success。
- [x] 验证：pipe codec/client CTest `3/3`，完整 CTest `25/25`；host profile qtest `2/2`、host
  controller `14/14`、host-channel-control `4/4`、DM-MC02 Device-mode USB qtest `9/9` 与完整
  QEMU smoke suite `80/80` 通过。未修改 `trobot/`。
- [ ] 限制与下一道门：目前只支持 PIO、同步 one-packet、one transaction/microframe；没有 channel
  allocator、endpoint toggle state、short-packet policy、interrupt period/SOF schedule、NAK retry/
  timeout、multi-packet bulk、ISO multi-transaction、async completion、DMA/FIFO arbitration、hub/
  topology、PHY/VBUS/passthrough 或 DM-MC02 Host feature。下一切片应先定义 board-independent
  endpoint state (`DATA0/DATA1`、halt/reset/short packet) 并做 isolated test，之后再接 periodic
  scheduling。

## 2026-08-31 USB endpoint data-toggle/halt state（已完成）

- [x] 所属层：可复用 USB endpoint driver 状态层。producer 是 caller 的 one-packet completion；
  boundary 是 `DmUsbHostEndpointState`；consumer 是 H723 single-packet PIO adapter。state 只保存
  copied pipe、next DATA PID 和 halt state，不依赖 MMIO/QEMU/DM-MC02/channel/timer，因此可由未来
  其他 controller 或 scheduler 重用。
- [x] state 从 DATA0 开始；成功 Bulk/Interrupt packet（包括 short packet）切换 DATA0/DATA1，
  NAK 和 transaction error 保持 PID，STALL 锁定 endpoint，clear-halt/reset 恢复 DATA0。ISO
  completion 不修改 PID；state 只验证 accepted `actual_length <= requested_length`，不自行决定
  short packet 完成 policy、CLEAR_FEATURE、retry 或时序。
- [x] H723 adapter `dm_stm32h7_usb_host_endpoint_transfer()` 只做
  `state.prepare -> existing PIO transfer -> state.complete` 映射。guest 经 keyboard endpoint 1
  发出的 idle interrupt-IN 收到 NAK，且记录的 state 仍是 DATA0，证明 transport completion 未被
  adapter 隐藏或错误推进 toggle。
- [x] 隔离门：endpoint-state smoke 覆盖 toggle/short/NAK/error/STALL/clear-halt/reset/ISO/invalid
  length；直接 consumer gate 为 Cortex-M7 guest NAK + DATA0 assertion。验证：完整 CTest `26/26`、
  host profile qtest `2/2`、host controller `14/14`、host-channel-control `4/4`、DM-MC02 USB qtest
  `9/9` 与完整 QEMU smoke suite `80/80` 通过。未修改 `trobot/`。
- [ ] 限制与下一道门：仍无 endpoint channel allocator、per-pipe SOF period、NAK retry budget、
  multi-packet request/short-packet completion policy、ISO multi-transaction、async completion、DMA、
  FIFO arbitration、hub/topology/PHY/VBUS/passthrough 或 DM-MC02 Host support。下一切片先定义
  protocol-neutral periodic schedule eligibility/time contract，在 host controller timer 前作 isolated
  test；不得把时钟策略写入 endpoint state 或 board profile。

## 2026-08-31 USB periodic endpoint eligibility/time contract（已完成）

- [x] 所属层：可复用 USB firmware driver 时间边界。producer 是已验证的 periodic
  `DmUsbHostPipe` 与 caller 给出的单调 virtual timestamp；boundary 是
  `DmUsbHostPeriodicSchedule`；consumer 将是 H723 host wrapper 的 SOF dispatch。该模块不依赖
  MMIO、QEMU timer、board profile、PID/halt state、channel allocation 或 retry policy。
- [x] `init()` 复制 pipe 并以 caller 的 `origin_ns` 建立第一个时隙。High-Speed interrupt/ISO 按
  `125 us * 2^(bInterval - 1)` 接受 `1..16`；Full-Speed interrupt 为 `1..255 ms`，Low-Speed
  interrupt 为 `10..255 ms`，Full-Speed ISO 固定 `1 ms`。Low-Speed ISO 与 Full/Low-Speed multi-
  transaction pipe 被拒绝，保持 pipe parser 的 speed-neutral 职责。
- [x] `eligible()` 无副作用；实际 poll 完成后才调用 `advance()`。当 virtual time 越过多个
  slot，后者直接前进到严格晚于当前 timestamp 的第一个 slot，避免在一个 SOF callback 中回放
  过期 polling，从而保持实时执行的有界工作量。
- [x] 隔离门：`dm_usb_host_periodic_schedule_smoke` 覆盖 HS/FS/LS exact interval、ISO 规则、
  multi-transaction speed boundary、early poll no-mutation 与 late-poll skip。定向 CTest 与
  descriptor/pipe/PIO/PID 相邻回归均通过；未修改 QEMU controller/adapter、DM-MC02 board 或
  `trobot/`。
- [ ] 限制与下一道门：尚未将 eligibility 接到 H723 SOF，也不发起 transfer、分配 channel、
  重试 NAK、处理 timeout、短包/multi-packet/ISO multi-transaction、DMA/FIFO arbitration、
  async completion、hub/PHY/VBUS/passthrough。下一切片只让 H723 host wrapper 在虚拟 SOF
  调用这一 schedule，再为 active channel 加 direct consumer test；不得把 schedule 逻辑写进
  DM-MC02 Device-mode board。

## 2026-08-31 USB periodic poller 与 H723 PIO adapter（已完成）

- [x] 所属层：可复用 USB driver + STM32H723 controller-client adapter。producer 是 caller 的
  virtual timestamp 和周期 endpoint state；通用 boundary 是 `DmUsbHostPeriodicPoller` 的
  `pipe/PID/bytes -> completion/actual_length` submit callback；consumer 是极薄的
  `DmStm32H7UsbHostPeriodic` PIO callback。通用 poller 不依赖 H723/QEMU/board，H723 adapter
  不拥有 SOF/timer/descriptor parsing/retry 或 DM-MC02 routing。
- [x] poller 执行 `eligible -> one submit -> endpoint completion -> advance`。not-due 不调用
  submitter；NAK 消费一个时隙但保留 PID，accepted 复用 endpoint state 的 toggle，STALL 消费时隙
  并 halt；halted 和 invalid submit 都不推进 schedule。virtual time 跳跃仍只发起一个 poll。
- [x] H723 adapter 仅映射既有 PIO `OK/NAK/STALL/XACTERR`，没有复制 HCCHAR/FIFO/HCINT 实现。
  Cortex-M7/QEMU `usb-kbd` smoke 以真实 parsed pipe 完成 first periodic poll 的 NAK，随后以相同
  timestamp 调用得到 `NOT_DUE`，证明同一 slot 不会重复写 channel；DATA0 保持不变。64-bit 的
  jump calculation 在 freestanding ARM link 显式使用 `-lgcc` 提供 `__aeabi_uldivmod`。
- [x] 隔离门：`dm_usb_host_periodic_poller_smoke` 覆盖 early/no-submit、NAK、accepted PID、
  late skip、STALL/halt 和 invalid submit；直接 consumer 门为 `run-stm32h723-usb-host-smoke.sh`。
  未修改 QEMU controller、DM-MC02 board 或 `trobot/`。
- [ ] 限制与下一道门：caller 目前仍手工提供 timestamp，未读取 H723 `HFNUM` 或订阅 SOF；没有
  actual SOF-to-ns mapping、channel allocator、NAK retry budget、multi-packet/short policy、ISO
  multi-transaction、DMA/FIFO arbitration、async completion、hub/PHY/VBUS/passthrough。下一切片先
  定义一个 H723 driver-owned SOF timestamp source，再将其传入此 adapter；不得让 controller
  解析 descriptor `bInterval` 或把 driver schedule 写进 board profile。

## 2026-08-31 USB SOF virtual timestamp source（已完成）

- [x] 所属层：可复用 USB time driver + STM32H723 register adapter。producer 是 16-bit SOF
  frame/microframe counter；boundary 是 `DmUsbHostSofClock`；consumer 是
  `DmStm32H7UsbHostSof` 与 periodic poller caller。通用 clock 不依赖 H723 MMIO/QEMU/board/
  endpoint，H723 source 仅在初始化读取 `HPRT0.SPD` 和 `HFNUM`，之后只读取 `HFNUM`。
- [x] High-Speed 每 `HFNUM.FRNUM` tick 为 125 us，Full/Low-Speed 为 1 ms。clock 使用 unsigned
  16-bit delta 重建正常 counter wrap 后的单调 virtual ns；port reset 后由 caller 重新 init，
  不把 reset 猜测混入时间转换。
- [x] 隔离门：`dm_usb_host_sof_clock_smoke` 精确覆盖 HS microframe、FS/LS frame、16-bit wrap 和
  unknown-speed rejection。直接 consumer 门：Cortex-M7 guest 从实际 H723 `HFNUM` 取 timestamp，
  first poll NAK 后同 timestamp 返回 `NOT_DUE`，再等待 `next_slot_ns` 后第二次真实 keyboard NAK。
  未修改 QEMU controller、DM-MC02 board 或 `trobot/`。
- [ ] 限制与下一道门：当前 SOF source 是 polling client，尚不通过 SOF IRQ/event callback 唤醒
  scheduler；没有 channel allocator、NAK retry budget、timeout、multi-packet/short policy、ISO
  multi-transaction、DMA/FIFO arbitration、async completion、hub/PHY/VBUS/passthrough。下一切片应
  先定义单 channel 的 driver-owned event-loop adapter，在每个 SOF 只检查 due endpoint，保持
  controller/board 不解析 descriptor metadata。

## 2026-08-31 H723 single-channel periodic SOF-event adapter（已完成）

- [x] 所属层：STM32H723 host driver event-loop adapter。producer 是 caller 每次观测到的 SOF
  event；boundary 是 `DmStm32H7UsbHostPeriodicSof`；consumer 是既有 protocol-neutral poller。
  adapter 只借用 H723 SOF source 和 poller，不拥有 PIO client、endpoint state、descriptor、IRQ、
  QEMU timer 或 DM-MC02 board state。
- [x] `on_event()` 读取当前 `HFNUM` timestamp 后至多调用一次 poller；not-due SOF 不触发底层
  transfer。init 检查 source 与 poller 的 USB speed 一致，防止 125-us microframe 与 1-ms frame
  被混用。它适用于 polling loop 或未来 SOF IRQ handler，二者复用同一 adapter。
- [x] 隔离门：`dm_stm32h7_usb_host_periodic_sof_smoke` 以伪 MMIO frame counter 与通用 submit
  callback 验证 first due submit、中间 SOF no-submit、later due submit、PID 保持以及 speed mismatch
  rejection。直接 consumer 门：Cortex-M7 guest W1C `GINTSTS.SOF` 后调用 adapter，实际 QEMU
  keyboard 在第一 NAK、same-slot defer 后只在到期 SOF 得到第二 NAK。未修改 QEMU controller、
  DM-MC02 board 或 `trobot/`。
- [ ] 限制与下一道门：当前是 single-channel polling event loop，尚未绑定 NVIC SOF IRQ 或维护多个
  endpoint registry；没有 NAK retry budget/timeout、channel allocator、多包/short policy、ISO
  multi-transaction、DMA/FIFO arbitration、async completion、hub/PHY/VBUS/passthrough。下一切片应
  定义 controller-independent 的小型 periodic endpoint registry，再接 H723 IRQ/event loop；避免
  在 board 或 controller 中保存 descriptor scheduling metadata。

## 2026-08-31 controller-independent periodic endpoint registry（已完成）

- [x] 所属层：可复用 USB driver registry + STM32H723 SOF adapter。producer 是同一 link speed
  下的多个 `DmUsbHostPeriodicPoller`；boundary 是固定容量
  `DmUsbHostPeriodicRegistry`；consumer 是 H723 `HFNUM` SOF adapter。registry 不依赖 H723/QEMU/
  board/descriptor/PIO，adapter 只借用 source 和 registry。
- [x] entry 复制 poller、caller-owned IN/OUT buffer span 与可选 completion callback。add 拒绝
  duplicate、speed mismatch 与容量耗尽；dispatch 在一个 timestamp 对每个 active endpoint 最多调用
  一次，not-due 不进入 submitter。submitted STALL、halted 或 invalid submit 自动移出 active set；
  disconnect/reconfigure 可显式 remove。内部使用 unordered compact array，未引入分配、队列或锁。
- [x] 首错修复：大 registry 的 aggregate zero-init 在 `-nostdlib` ARM smoke 中被编译器降为 libc
  `memset`。unused slots 由 `count` 隔离，init 改为只写 `speed/count`，消除 libc 依赖并缩短初始化。
- [x] 隔离门：`dm_usb_host_periodic_registry_smoke` 覆盖多 endpoint shared timestamp、different
  bInterval、duplicate/speed rejection、STALL auto-remove 与 explicit remove；
  `dm_stm32h7_usb_host_periodic_registry_sof_smoke` 覆盖 fake HPRT0/HFNUM 的 due/no-due/due 和
  source-registry mismatch。直接 consumer 门：Cortex-M7 guest 以真实 keyboard pipe 注册 registry，
  由 W1C `GINTSTS.SOF` 驱动第二次到期 NAK。未修改 QEMU controller、DM-MC02 board 或 `trobot/`。
- [ ] 限制与下一道门：guest 尚用 polling 检测 SOF，未接 NVIC IRQ；没有 endpoint channel allocator、
  NAK retry budget/timeout、多包/short policy、ISO multi-transaction、DMA/FIFO arbitration、async
  completion、hub/PHY/VBUS/passthrough。下一切片应单独建立 H723 SOF IRQ handler boundary，使现有
  registry 可从 IRQ 调用而不改变 controller 的 descriptor-free contract。

## 2026-08-31 H723 SOF IRQ to registry boundary（已完成）

- [x] 所属层：STM32H723 host driver IRQ adapter。producer 是 controller 的 `GINTSTS.SOF`；
  boundary 是 `DmStm32H7UsbHostSofIrq`；consumer 是既有 H723 source/registry adapter。它只读
  status、SOF W1C 和 dispatch；不拥有 `GAHBCFG/GINTMSK/NVIC`、channel IRQ、DMA、retry、
  descriptor 或 board route。
- [x] guest consumer 独立提供 IRQ policy：vector index `16+77` 指向 SOF handler，配置
  `GINTMSK.SOF`、`GAHBCFG.GINT`、`NVIC_ICPR2/ISER2` bit 13 后用 `wfi` 等待。IRQ handler 调用
  adapter，只有 due SOF 才经 registry/PID/PIO 发起 packet；这保持 controller 和 board 不保存
  endpoint scheduling metadata。
- [x] 隔离门：`dm_stm32h7_usb_host_sof_irq_smoke` 用 fake status/HFNUM 验证 non-SOF no-dispatch、
  SOF due/no-due/due dispatch。直接 consumer 门：Cortex-M7/QEMU guest 通过真实 IRQ 77 唤醒，
  在到期 SOF 得到第二次 keyboard NAK；未修改 QEMU controller、DM-MC02 board 或 `trobot/`。
- [ ] 限制与下一道门：当前只验证一个 host IRQ consumer 和单 endpoint registry item；没有
  channel allocator、NAK retry budget/timeout、多包/short policy、ISO multi-transaction、DMA/FIFO
  arbitration、async completion、hub/PHY/VBUS/passthrough。下一切片应先定义通用 host channel
  allocator，再将 registry entry 的 PIO client 绑定为显式 channel lease。

## 2026-08-31 USB host channel lease allocator（已完成）

- [x] 所属层：可复用 USB host driver resource 层。producer 是上层 scheduler 给出的 non-null
  owner identity 和 controller-defined channel ID 集合；boundary 是
  `DmUsbHostChannelAllocator`/`DmUsbHostChannelLease`；consumer 将是任意 host controller
  adapter。模块不依赖 H723 MMIO、DWC2 register、QEMU、DM-MC02、descriptor、pipe、SOF 或
  retry policy。
- [x] 固定上限为 16。`init()` 复制任意唯一 channel ID，接受 empty set，拒绝缺失/重复/超限的
  非法配置并保持现有 allocator 不变；`acquire()` 按初始化顺序选择第一个空闲 ID，满载时不改
  caller lease。lease 同时记录 channel、owner 和 generation；release 必须三者匹配，成功后清空
  entry 并使 caller lease 失效，因此 stale copy 不能释放同一 allocator lifetime 内重新分配后的
  channel。成功 reinit 是调用者丢弃旧 lease、重新绑定 consumer 的生命周期边界。
- [x] 隔离门：`dm_usb_host_channel_allocator_smoke` 覆盖 init 无 mutation、任意 ID/顺序、容量耗尽、
  owner forgery、release invalidation 和同一 channel reuse 后 stale lease 拒绝。定向 CTest `1/1`
  通过；未修改 H723 controller、DM-MC02 board、QEMU upstream 或 `trobot/`。
- [ ] 限制与下一道门：allocator 只处理 caller-serialized lease 生命周期，不含 IRQ locking、channel
  register program、transfer cancel、retry/timeout、FIFO/DMA 或 endpoint scheduling。下一切片才让
  H723 PIO client 以显式 active lease 代替裸 `channel`，并先添加 fake-MMIO unit、再以 reference
  host guest 验证分配的 channel 真正到达 `HCCHAR/HCTSIZ`；仍不引入 DM-MC02 Host mode。

## 2026-08-31 H723 PIO client channel-lease consumer（已完成）

- [x] 所属层：STM32H723 host-controller client 层。producer 是可复用 allocator 产生的 active
  `DmUsbHostChannelLease`；boundary 是 H723 PIO client 的 init/transfer；consumer 是已存在的
  periodic poller 和 reference host guest。client 借用 allocator/lease，不拥有 allocation、descriptor、
  SOF、retry、QEMU 或 DM-MC02 board state。
- [x] 移除 client 的裸 `uint8_t channel`。init 只接受 active lease 且在该 H723 client 层限制 channel
  为 `0..11`；transfer 每次重验 active lease 后才计算 `HCINT/HCTSIZ/HCCHAR/HCFIFO` MMIO 地址。释放
  lease 后的 transfer 在 volatile access 前返回 `INVALID`。allocator reinit 是 caller 丢弃旧 lease 并
  重建 client 的生命周期边界，不在此层推断 reset。
- [x] 隔离门：扩展 `dm_stm32h7_usb_host_pipe_smoke`，覆盖 active lease bind、H723 range reject 和
  released lease 不触碰 PIO。直接 consumer 门：Cortex-M7/QEMU guest 将 endpoint PIO 分配为 channel 1
  （EP0 control 已结束于 channel 0），完成 periodic NAK、same-slot defer 和 SOF-IRQ registry NAK；结果
  额外精确断言 channel 1 的 `HCCHAR` address/type/direction/endpoint/MPS fields。host USB CTest `12/12`
  与 standalone guest smoke 通过；未修改 DM-MC02、QEMU upstream 或 `trobot/`。
- [ ] 限制与下一道门：EP0 control client 仍是此 reference profile 的单 channel fixture，尚未消费 lease；
  没有 concurrent allocation、IRQ locking、completion-driven release、cancel、retry/timeout、multi-packet、
  DMA/FIFO 或 DM-MC02 Host mode。下一切片应先为 generic scheduler 定义 completion-driven lease release
  contract，再让 multi-endpoint registry 使用它；不能由 board 或 controller 持有 endpoint policy。

## 2026-08-31 H723 periodic dynamic lease/release boundary（已完成）

- [x] 所属层：STM32H723 host driver adapter 与 generic poller boundary。producer 是 due periodic
  poller callback；boundary 是 `DM_USB_HOST_PERIODIC_SUBMIT_DEFERRED` / `POLL_DEFERRED` 以及 H723
  periodic adapter 的 allocator lease；consumer 是现有 periodic registry/SOF IRQ 和 Cortex-M7 guest。
  generic poller 不依赖 allocator，H723 adapter 不修改 board 或 QEMU controller。
- [x] `DEFERRED` 表示没有发出 packet，poller 不修改 endpoint PID、不推进 `next_slot_ns`，registry 不移除
  endpoint。H723 adapter 每次实际 due submit 才 acquire 一个临时 lease，以 periodic object 作为 owner，
  用短生命周期 client 执行同步 PIO，并在 `OK/NAK/STALL/XACTERR` 返回后 release；无空闲 channel 会在
  下一个事件重试，非法 H723 channel 或 release failure 才是 `INVALID`。
- [x] 隔离门：`dm_usb_host_periodic_poller_smoke` 新增 deferred 不推进状态/时序断言；相邻 poller、
  registry、SOF/IRQ 回归通过。直接 consumer guest 使用 channel 1，断言 channel-1 `HCCHAR` 字段以及
  第二次 SOF PIO 后 allocator entry 已为空。全量 CTest `34/34`、bare-metal guest smoke 通过；未修改
  `trobot/`。
- [ ] 限制与下一道门：当前 lease 仍由同步 H723 PIO adapter 临时持有；没有异步 completion/cancel、
  IRQ 并发保护、DMA/FIFO、multi-packet 或完整多端点公平调度。下一切片应在保持 `DEFERRED` 语义的前提下，
  用两个独立 periodic adapter/真实端点验证 shared allocator 的多端点行为，再考虑异步 controller。

## 2026-09-01 H723 periodic shared allocator multi-adapter boundary（已完成）

- [x] 所属层：STM32H723 host driver adapter 与 generic poller boundary。producer 是两个独立 periodic
  adapter 的 due callback；boundary 是显式 H723 transfer callback、temporary channel lease 和
  `DEFERRED` poll result；consumer 是 allocator、endpoint state 和现有 poller。默认真实 PIO 路径保持不变，
  测试 fixture 只替换 transfer callback，不接触 QEMU 私有状态。
- [x] 新增 `dm_stm32h7_usb_host_periodic_init_with_transfer()`，默认 initializer 仍绑定真实 PIO。
  两个 adapter 嵌套提交时分别取得 channel 2/3，callback 内确认 lease active；同步返回后两个 entry
  均释放。唯一 channel 被外部 owner 占用时，adapter 返回 `POLL_DEFERRED`，endpoint PID 和 schedule
  保持不变。
- [x] 隔离门：`dm_stm32h7_usb_host_periodic_smoke` 覆盖 callback injection、两个 adapter 的同时 lease、
  completion 后 release 和资源耗尽 defer；generic poller/registry 相邻回归、Cortex-M7/QEMU guest smoke
  通过。全量 CTest `36/36` 通过；未修改 `trobot/`。
- [ ] 限制与下一道门：仍是同步 PIO；没有异步 completion/cancel、IRQ locking、DMA/FIFO、multi-packet、
  公平仲裁或真实多设备 USB topology。下一切片应先在 QEMU/transport 无关的多 packet bulk 状态边界定义
  short-packet、PID 和 channel 持有关系，再接 H723 controller；不要借由 fixture 宣称真实多设备枚举。

## 2026-09-01 controller-independent USB bulk composition（已完成）

- [x] 所属层：可复用 USB host driver 数据组合层。producer 是已配置的 bulk
  `DmUsbHostEndpointState` 和 controller-neutral single-packet submit callback；boundary 是
  `DmUsbHostBulkTransfer`；consumer 将是后续 H723 PIO/channel adapter。该层不依赖 H723 MMIO、QEMU、
  DM-MC02、descriptor、SOF、DMA 或外部设备。
- [x] OUT 按 endpoint MPS 分包；IN 按 MPS 取包，在 short packet 或 caller buffer 耗尽时结束；OUT
  可对精确 MPS 的 payload 显式追加一个 zero-length packet。每次 accepted packet 通过 endpoint state
  更新 DATA0/DATA1，返回值累计本次调用成功传输的字节数。
- [x] `NAK` 返回累计长度并保留当前待重试 packet 的 PID；`DEFERRED` 表示没有提交 packet，既不改变
  PID 也不推进组合进度；`STALL` 锁定 endpoint。部分调用的重试由 caller 使用返回长度裁剪剩余 buffer，
  该 API 本身不保存异步事务上下文，也不重复提交已经 accepted 的 packet。
- [x] 隔离门：`dm_usb_host_bulk_smoke` 覆盖 `64/64/2` OUT 分包、IN short、精确 MPS 的 ZLP、
  NAK 累计长度、DEFERRED 状态保持、STALL halt 和非 bulk 拒绝；定向构建与执行通过。
- [ ] 限制与下一道门：当前仍是同步单调用组合器，没有 H723 多包 PIO 接入、每 packet channel lease
  生命周期、async completion/cancel、retry budget/timeout、DMA/FIFO arbitration、ISO multi-
  transaction、hub/拓扑、PHY/VBUS、宿主机 passthrough 或 DM-MC02 Host board wiring。下一切片只应
  以既有 H723 single-packet PIO callback 接入一个 bulk packet，并单独验证 channel acquire/release、
  short/NAK/STALL 和剩余长度，不得直接声称真实 bulk 枚举。

## 2026-09-01 STM32H723 bulk packet adapter（已完成）

- [x] 所属层：STM32H723 host-controller client adapter。producer 是通用
  `DmUsbHostBulkTransfer` 的 single-packet callback；boundary 是
  `DmStm32H7UsbHostBulk`；consumer 是已有 H723 PIO pipe client。adapter 不修改 DWC2 core、QEMU
  USB bus 或 DM-MC02 Device-mode board mapping。
- [x] 每个 bulk packet 进入 H723 adapter 时临时 acquire 一个 active channel lease，调用既有
  `dm_stm32h7_usb_host_pipe_transfer()`（或注入的同型 packet callback），在同步 completion 后
  release。channel 不跨 packet 持有，资源耗尽映射为通用 `DEFERRED`，不改变 endpoint PID 或
  bulk 组合进度。
- [x] `dm_stm32h7_usb_host_bulk_smoke` 覆盖 H723 bulk OUT 分包、IN packet 映射、部分 OUT 后
  `NAK` 与 caller 剩余 span 重试、每 packet lease active/release、资源耗尽 DEFERRED、STALL
  halt 和资源释放。窄测试通过，完整 host CTest `37/37` 通过，QEMU smoke suite 通过。
- [ ] 限制与下一道门：当前默认 adapter 仍是同步 PIO，直接测试使用 callback 注入，尚未让
  H723 reference guest 经真实 QEMU USB device 完成 bulk endpoint configuration/data transfer。
  仍无 async completion/cancel、channel IRQ locking、retry budget/timeout、DMA/FIFO arbitration、
  ISO multi-transaction、hub/拓扑、PHY/VBUS、passthrough 或 DM-MC02 Host board wiring。下一切片
  应在 reference host profile 中配置一个明确的 bulk device/endpoint，先验证单 packet 的
  `HCCHAR/HCTSIZ/HCFIFO/HCINT` 和 QEMU packet length，再扩展到 guest 多包回归。

## 2026-09-01 STM32H723/QEMU bulk direct consumer（已完成）

- [x] 所属层：reference H723 host profile 的直接 consumer 验证。producer 是 QEMU `usb-serial`
  的真实 configuration/bulk endpoint；boundary 是 guest 的 descriptor parser、endpoint state、
  H723 PIO bulk adapter 与 QEMU USB transport；consumer 是 QEMU chardev 文件。该 smoke 不接
  DM-MC02 machine，也不改变现有 USB Device-mode profile。
- [x] Cortex-M7 guest 完成 `GET_DESCRIPTOR -> SET_ADDRESS(5) -> GET_CONFIGURATION(header/full) ->
  parse interface/endpoints -> SET_CONFIGURATION(1)`，从 USB serial interface 0 的 endpoint 2
  构造 64-byte bulk OUT pipe，并通过默认 H723 adapter 发送 130 bytes。QEMU chardev 收到完整且
  精确的 `0..129` payload；guest 同时检查最终 channel-1 `HCCHAR/HCTSIZ`、DATA PID 和 lease 已释放。
- [x] `tools/run-stm32h723-usb-host-bulk-smoke.sh` 独立通过，并由自动发现 `run-*-smoke.sh` 的
  QEMU smoke suite 执行；此前的 oracle 错误（最终 packet 为 DATA0，不是 SETUP PID）已修正。
- [ ] 限制与下一道门：当前仍是单 root-port、同步 PIO、bulk OUT 直接 consumer；没有 per-packet
  QEMU trace oracle、bulk IN 数据注入、NAK retry budget/timeout、async completion/cancel、DMA/FIFO
  arbitration、hub/拓扑、PHY/VBUS、passthrough 或 DM-MC02 Host board wiring。下一切片应先为
  H723 channel completion/NAK retry 定义独立状态边界，再扩展 IN/OUT 设备行为，不要把 chardev
fixture 当作完整 USB host 实现。

## 2026-09-01 H723 USB bulk NAK completion/channel lease slice（已完成）

- [x] 所属层：STM32H723 host-controller PIO adapter 与 reference host direct-consumer 边界。
  producer 是 QEMU `usb-serial` 在无输入时产生的 bulk `NAK`，boundary 是 H723
  `HCINT/HCCHAR` channel 生命周期，consumer 是同步 PIO bulk adapter、endpoint state 和
  caller retry。
- [x] H723 PIO transfer 现在同时等待 `HCINT.CHHLTD | HCINT.NAK`；bulk/control NAK 返回前
  写入 `HCCHAR.CHDIS`，确保仍 active 的 channel 在同步 adapter 释放 lease 前停止。NAK 不推进
  `HCTSIZ`、actual length 或 endpoint DATA PID；重试策略仍由上层调用者负责。
- [x] guest direct-consumer smoke 在成功 OUT/IN 后再次执行真实 idle bulk IN，断言返回 NAK、
  actual length 为 0 且 channel `CHENA` 已清除。测试夹具补齐 IN endpoint state 初始化，并将
  READY 标记保持在有限 100 ms 虚拟 SysTick 窗口内，避免 host 在 marker 发布前后错过 chardev
  注入边界；该同步修正不改变运行时 USB 模型。
- [x] 隔离与集成门：`dm_stm32h7_usb_host_pipe_smoke`、`dm_stm32h7_usb_host_bulk_smoke`、
  独立 `tools/run-stm32h723-usb-host-bulk-smoke.sh` 通过；host CMake 构建和完整 CTest
  `38/38` 通过（含 QEMU smoke suite）。`trobot/` 未修改。
- [ ] 限制与下一道门：当前仍是单 root-port、同步 PIO、临时 channel lease；没有 NAK retry
  budget/timeout、async completion/cancel、IRQ 并发保护、DMA/FIFO arbitration、ISO
  multi-transaction、hub/topology、PHY/VBUS、passthrough 或 DM-MC02 Host board wiring。
  下一切片应单独定义有限 retry/SOF 预算，不能在 H723 PIO 层隐式忙等。

## 2026-09-01 USB bulk direct consumer 修正与双向验证（已完成）

- [x] 所属层：通用 bulk composer、STM32H723 guest smoke 链接边界和 reference host profile 的
  直接 consumer。首个 producer/boundary 错误在 `dm_usb_host_bulk_transfer_run()`：IN packet
  调用 submitter 时错误地同时传入了 `out_length=requested_length` 和 `in_capacity`，被 H723
  pipe 的参数校验拒绝。修复后 IN 始终传 `out_length=0`，OUT 仍传实际 packet 长度。
- [x] 增加隔离回归断言：`dm_usb_host_bulk_smoke` 检查 OUT/IN 的方向参数分别为
  `out_length=64,in_capacity=0` 与 `out_length=0,in_capacity=64`；避免 callback 只检查合并后
  的 requested length 而漏掉方向契约。
- [x] 修复 guest smoke 链接脚本：未声明的静态 allocator `.bss` 原先落在 Flash orphan section，
  现保留 `0x20000000..0x200000ff` 结果区并将 `.data/.bss` 放到 DTCM `0x20000100` 起始地址。
  同时修正 USB descriptor 校验对 IN endpoint 地址的预期为带方向位的 `0x81`。
- [x] 真实双向边界已通过：Cortex-M7 guest 经 `GET_DESCRIPTOR -> SET_ADDRESS -> GET_CONFIGURATION ->
  SET_CONFIGURATION` 发现 `usb-serial` 的 endpoint 1 IN/endpoint 2 OUT；OUT 发送精确 `0..129`，
  IN 接收 12 bytes（FTDI 默认状态头 `b1 00` 加 `00..09`）。IN 最终 `HCCHAR` 保留 bulk 类型、
  IN 方向和 endpoint 1，`HCTSIZ` 剩余 52 bytes，OUT/IN lease 均释放。
- [x] chardev 输入同步使用 guest SysTick/WFI 边界：guest 发布 READY 后进入 WFI，脚本在 QEMU 主
  循环可运行时注入输入，SysTick 唤醒 guest 后再执行 IN；不依赖不可用的 `system_wakeup`，也不
  使用无界 NAK 忙等作为测试同步机制。
- [x] 验证：`dm_usb_host_bulk_smoke`、`dm_stm32h7_usb_host_bulk_smoke`、独立
  `tools/run-stm32h723-usb-host-bulk-smoke.sh` 和完整 host CTest `37/37` 通过。
- [ ] 限制与下一道门：当前仍是单 root-port、同步 PIO、临时 channel lease；没有 NAK retry
  budget/timeout、async completion/cancel、IRQ 并发保护、DMA/FIFO arbitration、hub/拓扑、
  PHY/VBUS、passthrough 或 DM-MC02 Host board wiring。chardev IN 只验证了 QEMU `usb-serial`
  的一个确定性设备行为，不等于完整 USB Host 支持。

## 2026-09-01 STM32H723/QEMU bulk direct consumer（已完成）

- [x] 所属层：reference H723 host profile 的直接 consumer 验证。producer 是 QEMU `usb-serial`
  的真实 configuration/bulk endpoint；boundary 是 guest 的 descriptor parser、endpoint state、
  H723 PIO bulk adapter 与 QEMU USB transport；consumer 是 QEMU chardev 文件。该 smoke 不接
  DM-MC02 machine，也不改变现有 USB Device-mode profile。
- [x] Cortex-M7 guest 完成 `GET_DESCRIPTOR -> SET_ADDRESS(5) -> GET_CONFIGURATION(header/full) ->
  parse interface/endpoints -> SET_CONFIGURATION(1)`，从 USB serial interface 0 的 endpoint 2
  构造 64-byte bulk OUT pipe，并通过默认 H723 adapter 发送 130 bytes。QEMU chardev 收到完整且
  精确的 `0..129` payload；guest 同时检查最终 channel-1 `HCCHAR/HCTSIZ`、DATA PID 和 lease 已释放。
- [x] `tools/run-stm32h723-usb-host-bulk-smoke.sh` 独立通过，并由自动发现 `run-*-smoke.sh` 的
  QEMU smoke suite 执行；此前的 oracle 错误（最终 packet 为 DATA0，不是 SETUP PID）已修正。
- [ ] 限制与下一道门：当前仍是单 root-port、同步 PIO、bulk OUT 直接 consumer；没有 per-packet
  QEMU trace oracle、bulk IN 数据注入、NAK retry budget/timeout、async completion/cancel、DMA/FIFO
  arbitration、hub/拓扑、PHY/VBUS、passthrough 或 DM-MC02 Host board wiring。下一切片应先为
  H723 channel completion/NAK retry 定义独立状态边界，再扩展 IN/OUT 设备行为，不要把 chardev
  fixture 当作完整 USB host 实现。

## 2026-09-01 USB host retry policy boundary（已完成）

- [x] 所属层：板卡无关的 USB host transfer 状态边界。producer 是单包 controller
  submitter 的 `NAK`/`DEFERRED`/accepted completion；boundary 是
  `DmUsbHostRetryPolicy`、endpoint DATA PID 和 bulk/periodic composition；consumer 是
  H723 PIO adapter 及 reference host guest。retry policy 不依赖 QEMU、DM-MC02、SOF
  寄存器或具体 transport。
- [x] `dm_usb_host_retry` 增加虚拟时间、单调性（含 uint64 wrap）检查、有限 NAK retry
  budget 和 timeout，并区分成功 `complete`、终止 `finish` 与调用者取消 `reset`。
  首次提交不消费预算；NAK 只在实际返回 `RETRY` 时消费一次预算，超限和超时均为终态。
- [x] endpoint state 统一校验单包方向、MPS、completion 实际长度和非 accepted completion
  的零字节约束。bulk 提供 `run_with_retry()`；调用者在 NAK 后必须在后续虚拟时间以
  `actual_length` 对应的剩余 buffer span 重新调用，避免重放已成功 packet。
- [x] periodic poller 支持可选 retry policy：NAK retry 不推进 DATA PID 或 schedule，
  `DEFERRED` 不消费预算；`RETRY_EXHAUSTED`/`RETRY_TIMEOUT` 才结束本次 due slot。retry
  pending 期间保存调用者 buffer 指针，因此调用者必须保证其生命周期。H723 periodic/bulk
  adapter 将真实 PIO completion 接入同一边界。
- [x] 隔离门：retry、endpoint state、bulk、periodic poller/registry、SOF 和 H723 adapter
  smoke 全部通过；两个独立 H723/QEMU guest smoke 通过；host CTest `38/38` 通过，QEMU
  suite `81/81`，Python `255 passed`，shell/compileall/`uv lock --check` 通过。
- [ ] 限制与下一道门：retry 仍由 caller 驱动，不在 H723 PIO 热路径忙等；bulk/periodic
  adapter 仍为同步、单包 PIO 加多包组合，H723 channel lease 是每 packet 临时持有。尚无
  async completion/cancel、IRQ locking、DMA/FIFO arbitration、SOF retry scheduler、ISO
  multi-transaction、hub/topology、PHY/VBUS、passthrough 或 DM-MC02 Host board wiring。
  下一切片应先定义 completion-driven channel ownership，再分别接异步 controller 和真实
  host topology。

## 2026-09-01 controller-independent completion-driven channel operation（已完成）

- [x] 所属层：可复用 USB host resource 生命周期层。producer 是上层 adapter 发起的 channel
  request；boundary 是 `DmUsbHostChannelOperation` 对 allocator lease 的 pending/complete/
  cancel 管理；consumer 是 H723 bulk/periodic synchronous adapter。该层不依赖 H723 MMIO、
  DWC2、QEMU、DM-MC02、descriptor、SOF、DMA 或具体 transport。
- [x] 新增 `dm_usb_host_channel_operation.[ch]`：`init()` 后由 `begin()` 获取一个 generation-
  protected lease；operation 在 controller completion 前保持 `PENDING`，仅由
  `complete()` 或 `cancel()` 释放并进入终态。重复 begin、非 pending 终结和 allocator 资源耗尽
  不会改变已有 operation/allocator 状态；终态 operation 可复用。operation 自身作为 owner，
  因而不会把 adapter 的临时生命周期误当作异步 completion 的 owner。
- [x] H723 bulk/periodic adapter 已复用 operation 作为同步 packet 的 lease 生命周期；callback
  期间 lease 保持 active，返回后由 operation completion 释放。未来异步 controller 可将 operation
  作为稳定对象跨 event loop 持有，但本切片没有伪造异步 completion，也没有加入锁或线程。
- [x] 隔离门：`dm_usb_host_channel_operation_smoke` 覆盖 pending、双 operation 独立占用、
  资源耗尽、complete/cancel、重复终结和终态复用；bulk/periodic adapter smoke 与两条 H723/QEMU
  guest smoke 均通过。首次编译发现 operation 源文件缺少 `<stddef.h>` 导致 `NULL` 未定义，已在
  operation 层修复后重新构建。
- [x] 验证：host build 通过，完整 CTest `39/39`，QEMU smoke suite `81/81`，Python `255 passed`，
  `bash -n tools/*.sh`、Python compileall 和 `uv lock --check` 通过；`trobot/` 未修改。
- [ ] 限制与下一道门：operation 只定义 ownership 生命周期，不实现异步 controller、IRQ locking、
  callback dispatch、DMA/FIFO、SOF scheduler、hub/topology、PHY/VBUS 或 DM-MC02 Host wiring。
  下一切片应在该契约之上实现 controller-independent async completion/cancel 测试 fixture，
  再由 H723/QEMU adapter 消费，不应直接把线程或 QEMU event loop 塞进 allocator。

## 2026-09-01 H723 async channel IRQ dispatcher（已完成）

- [x] 所属层：STM32H723 host-controller IRQ/event adapter。producer 是 H723
  `GAHBCFG/GINTSTS/GINTMSK/HAINT/HAINTMSK` 事件汇总；boundary 是固定 12-channel
  `DmStm32H7UsbHostPipeAsyncDispatch`；consumer 是既有 async PIO
  `DmStm32H7UsbHostPipeAsync` completion/cancel record。实现不依赖 DM-MC02 board、QEMU、
  RTOS、NVIC 或外部 transport。
- [x] `dispatch_start()` 在同一锁边界内完成 PIO start 和 channel-to-async/token 注册，
  `dispatch_handle()` 按 global gate 与 channel mask snapshot 路由每个 pending channel，
  terminal poll 后清理 slot；`dispatch_cancel()` 同步取消并清理 slot。外部先完成/取消的
  record 会被 token 校验识别并回收 stale slot，不会释放新 generation 的 lease。
- [x] 隔离门：`dm_stm32h7_usb_host_pipe_async_dispatch_smoke` 覆盖 global/channel mask、
  pending 无 mutation、channel 1/3/7 多路 dispatch、accepted/NAK、显式 cancel、外部取消
  后 slot cleanup 和 enter/leave 配对。首次窄链接发现新 target 漏列公共
  `dm_usb_host_endpoint_state.c`，补齐后通过；复核又补充了活动 slot 禁止重初始化和
  cancel 在锁内读取 lease 的边界。
- [x] 验证：新 dispatcher smoke 通过，`CMakeLists.txt` 已注册 CTest；根目录与 QEMU 子项目
  `AGENTS.md` 已保留逐层开发、首错定位、接口复用和子代理及时清理约束。本切片未修改
  `trobot/`。
- [ ] 限制与下一道门：当前 `handle()` 是可被 IRQ 调用的 caller-provided adapter，不安装
  NVIC/vector，也不清理 QEMU `GINTSTS`；真实任务上下文和 IRQ 必须共享同一锁。尚无真实
  deferred QEMU producer、IRQ line/W1C 集成、DMA/FIFO arbitration、SOF retry scheduler、
  ISO、多设备 topology、PHY/VBUS、passthrough 或 DM-MC02 Host-role wiring。下一切片应先
  用独立 deferred controller fixture 验证 IRQ 线与多个 HCINT 的保持/下降，再考虑 guest 集成。

## 2026-09-01 QEMU host-channel deferred completion boundary（已完成）

- [x] 所属层：板卡无关的 QEMU host-channel transport boundary。producer 是已完成的
  `DmUsbTransaction` submit 和 IN data copy；boundary 是可选的
  `DmUsbHostChannelTransportScheduleCompletion`；consumer 是
  `dm_stm32h7_otg_host_complete_channel()` 及其 HCINT/HAINT/global IRQ 汇总。回调只接收
  稳定的 host、channel、非零 completion token、completion 和 actual length，不保存栈 request、
  transaction 或 reusable packet buffer。host 会拒绝 reset/halt/reuse 后迟到的旧 token。
- [x] 未安装 scheduler 时保持既有同步 completion；安装后 transport 只延迟寄存器终态提交，
  不改变 transaction 结果、IN 数据搬运或 packet 生命周期。callback 必须负责恰好一次
  completion delivery，因而可由后续 QEMU virtual timer 使用，但本切片没有引入 timer、线程、
  锁、队列或时间推进。
- [x] 隔离/边界门：`test-dm-usb-host-channel-transport --tap` `12/12`，覆盖真实 host
  `waiting_completion`、无 HCINT/IRQ 的 pending 状态，以及后续 delivery 后的
  `XFRC|CHHLTD -> HAINT -> GINTSTS.HCINT -> IRQ` 链；原有同步、PIO、DMA、QEMU-memory
  和 routing cases 保持通过。
- [ ] 下一道集成门：增加 QEMU virtual-timer-backed scheduler fixture，先验证 timer
  cancellation/reset 和多个 channel 的完成顺序，再考虑真实 guest async path。仍不得把
  scheduler 直接接到当前 DM-MC02 USB Device profile，也不应在此层加入 DMA/FIFO、SOF retry、
  ISO、hub/topology、PHY/VBUS 或 host passthrough。

## 2026-09-01 QEMU virtual-clock completion scheduler fixture（已完成）

- [x] 所属层：板卡无关的 QEMU transport fixture。producer 是
  `DmUsbHostChannelTransport` 已确定的 completion，boundary 是固定容量的
  `QEMU_CLOCK_VIRTUAL` timer scheduler，consumer 是带 token 校验的 H723 host channel
  completion。scheduler 不依赖 DM-MC02 board wiring，也不持有 transaction、request 或
  packet buffer。
- [x] 新增 `DmUsbHostQemuCompletionScheduler`：支持默认 delay、显式虚拟 delay、同一
  host/channel 的 deadline 替换、reset 取消以及 destroy teardown；timer 到期只调用
  `dm_stm32h7_otg_host_complete_channel_with_token()`，保持既有 IRQ/HCINT 汇总路径。
- [x] 隔离门：`test-dm-usb-host-qemu-completion-scheduler` `5/5` 覆盖零延迟、正延迟
  reset、多个 channel 的完成顺序、重复 channel 替换和 destroy 取消。测试使用受控
  `cpu_get_clock()` 与 deadline delta，不依赖 wall-clock sleep 或 CPU timer 初始化。
- [x] 本轮发现并修复测试夹具的两个错误：unit target 不应链接整套
  `cpu_timers_init()`/`cpu_enable_ticks()`；`qemu_clock_deadline_ns_all()` 返回相对当前
  时间的 delta 而非绝对时间。修复后 scheduler 目标重新构建并通过。
- [x] 受影响的 `test-dm-usb-host-channel-transport` 与
  `test-dm-usb-host-qemu-completion-scheduler` 通过（`12+5` 个子测试）；完整 QEMU
  Meson 回归观测为 `332` 通过、`9` 跳过、`1` 个既有
  `qtest-arm/test-hmp` SIGSEGV，失败位于 `stm32h723-usb-host` 的 HMP `mouse_button 0`
  输入设备路径，不归因于 scheduler，已记录到 `REVIEW.md`。
- [ ] 限制与下一道门：固定容量为 12 个 channel，超出 fixture 契约的独立
  host/channel 请求不提供排队扩容；scheduler 仍是 test-only QEMU adapter，不负责
  cancel notification、IRQ locking、SOF retry、DMA/FIFO、USB topology、PHY/VBUS、宿主机
  passthrough 或 DM-MC02 USB Host board wiring。下一步应把它接入独立 reference host
  composition，并先验证 reset/teardown 与真实 deferred guest event 的边界。

## 2026-09-01 QEMU reference host deferred-completion composition（已完成）

- [x] 所属层：独立 STM32H723/QEMU host composition。producer 是 QEMU USB device
  transaction 的已确定结果，boundary 是 QEMU host-controller device 上可选的
  `DmUsbHostQemuCompletionScheduler`，consumer 是 H723 MMIO/NVIC channel completion。
  通用 STM32H7 core、transport 和当前 DM-MC02 USB Device profile 均未依赖该 scheduler。
- [x] `dm-stm32h7-otg-host-qemu` 增加初始化属性
  `completion-scheduler`（默认 `false`）和 `completion-delay-ns`（默认 `0`）。
  `stm32h723-usb-host` reference profile 显式开启 scheduler；通用 QEMU host device
  保持同步兼容默认值。零延迟仍经 `QEMU_CLOCK_VIRTUAL` timer，不伪造同步 completion。
- [x] 生命周期边界已闭合：device reset 先取消 scheduler timer 再 reset host；unrealize
  先清空 transport callback，再销毁 scheduler，随后释放 SOF timer、USB port 和 bus。
- [x] reference qtest 以 `completion-delay-ns=1000` 验证 deadline 前 `HCINT=0`、推进
  虚拟时钟后完成并进入 IRQ 汇总路径；新增 pending completion 在 `system_reset` 后不会
  产生旧 channel completion。
- [x] 验证：QEMU 主体、`stm32h723-usb-host-test`（3 个 qtest）和已有 H723 host/bulk
  guest smoke 通过；scheduler unit `5/5` 与 transport `12/12` 保持通过。
- [x] 限制与下一道门：scheduler 仍固定 12-entry、单 host/channel pending，且是 QEMU
  composition fixture；本切片之后仍没有 firmware async dispatcher 的直接 guest 集成、IRQ
  locking、SOF retry scheduler、DMA/FIFO arbitration、USB topology、PHY/VBUS、passthrough
  或 DM-MC02 Host board wiring。多 channel deferred completion/cancel 已在保持 host-role
  profile 与 DM-MC02 device-role profile 分离的前提下通过；下一切片应先做真实 guest async
  IRQ consumer 的最小边界测试。

## 2026-09-01 QEMU reference host multi-channel deferred completion/cancel（已完成）

- [x] 所属层：独立 STM32H723/QEMU host composition 的 channel lifecycle 边界。producer
  是两个真实 QEMU USB control transaction，boundary 是 H723 `HCCHAR/HCTSIZ/HCFIFO`、
  virtual completion scheduler 和 host channel cancel callback，consumer 是
  `HCINT/HAINT/GINTSTS.HCINT/NVIC`。
- [x] H723 host 新增板卡无关的 `DmStm32H7OtgHostChannelCancel` callback。guest 写入
  `HCCHAR.CHDIS` 或 host reset 时，只有仍在等待 completion 的 channel 才通知 owner；
  callback 以 `host + channel + completion_token` 精确匹配，随后 host 产生正常
  `CHHLTD`，不会伪造 USB completion。
- [x] QEMU scheduler 新增 per-channel cancel：移除匹配 timer、减少 pending 数并保持其它
  host/channel entry 不变；reference composition 在 realize 时绑定，unrealize 时先解绑。
  reset 先取消 scheduler timer，再 reset H723 host。
- [x] 隔离门：`test-dm-usb-host-qemu-completion-scheduler --tap` `6/6`，覆盖错误 token
  不取消、`CHDIS` 取消 timer；`stm32h723-usb-host-test --tap` `4/4`，覆盖两个 channel
  同时 pending、deadline 前无中断、共同 `HAINT/GINTSTS/NVIC` 汇总，以及取消 channel 0
  后 channel 1 独立完成。
- [x] 集成回归：H723 control/bulk guest smoke、host transport unit `12/12` 和 host CTest
  `42/42` 通过；`trobot/` 未修改。脚本通过 `bash tools/run-stm32h723-usb-host-smoke.sh`
  执行，直接执行因既有 executable bit 缺失而拒绝，未修改权限元数据。
- [ ] 限制与下一道门：QEMU scheduler 仍是固定 12-entry composition fixture，不提供
  firmware async dispatcher 的 guest IRQ consumer、并发锁、SOF retry、DMA/FIFO、ISO、多
  设备 topology、PHY/VBUS、passthrough 或 DM-MC02 Host-role board wiring。下一步应只建立
  一个真实 guest IRQ handler 消费该 H723 channel completion 的最小 async smoke。

## 2026-09-01 真实 guest async IRQ consumer（已完成）

- [x] 所属层：独立 STM32H723 host-role reference profile 的 guest 集成门。producer 是
  QEMU USB keyboard 的 interrupt-IN NAK，经 virtual completion scheduler 产生 H723
  `HCINT/HAINT/GINTSTS`；boundary 是 IRQ 77 vector/NVIC 与
  `DmStm32H7UsbHostPipeAsyncDispatch`；consumer 是两个稳定地址的 async PIO operation。
  该切片不接 DM-MC02 当前 USB Device profile，也不把 QEMU 类型带入可复用 firmware 层。
- [x] 新增 `smoke/stm32h723_usb_host_async_smoke.c`：先用同步 EP0 完成 port reset、
  keyboard descriptor/configuration discovery 和 `SET_CONFIGURATION`，随后用 allocator
  channel `{0, 1}` 通过 `dispatch_start()` 同时提交两个 interrupt-IN packet；channel 0
  通过 `HCCHAR.CHDIS` 取消，channel 1 保留并由 IRQ 77 handler 调用 dispatcher 消费 NAK。
- [x] guest 在提交前配置两个 `HCINTMSK`、`HAINTMSK`，在取消后开启 `GINTMSK/GINT/NVIC`，
  结果区逐字段验证两个 start、token generation、cancelled/completed state、NAK result、
  IRQ 次数，以及两个 allocator lease 和 dispatcher slot 均已释放。取消事件不作为 USB
  completion 再次上报。
- [x] 新增 `tools/run-stm32h723-usb-host-async-smoke.sh`；脚本复用现有 linker script，
  通过 `run-qemu-smoke-suite.sh` 的文件名自动发现，不修改 QEMU machine 或 `trobot/`。
- [x] 隔离门：allocator、H723 async pipe、async dispatcher 三个 CTest 均通过；直接
  consumer 门 `bash tools/run-stm32h723-usb-host-async-smoke.sh` 通过；本切片未引入
  wall-clock completion 等待，脚本中的 socket ready polling 仅用于进程启动同步。
- [x] 受影响回归：`ctest --test-dir build/host --output-on-failure` 为 `42/42`，
  `stm32h723-usb-host-test --tap` 为 `4/4`，现有 control/bulk guest smoke 通过，
  `run-qemu-smoke-suite.sh` 为 `82/82`，`PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q`
  为 `255 passed`，`bash -n tools/*.sh` 和 QEMU `git diff --check` 通过。
- [ ] 限制与下一道门：当前仍是单 root-port、单 `usb-kbd`、单包 PIO、单核 PRIMASK 锁的 guest
  fixture；没有实际键盘报告数据、多设备 topology、SOF retry、DMA/FIFO arbitration、
  ISO、PHY/VBUS、passthrough 或 DM-MC02 Host-role board wiring。下一步应在同一 H723
  host-role 层定义 IRQ locking 与多包/重试边界，再考虑 DMA/FIFO 或更上层外设组合。

## 2026-09-01 H723 PRIMASK IRQ lock adapter（已完成）

- [x] 所属层：可复用 STM32H7/Cortex-M7 芯片层同步边界。producer 是主上下文或 IRQ
  handler 对共享 allocator/operation/dispatch 状态的访问；boundary 是
  `firmware/dm_stm32h7_irq_lock.[ch]` 的 `PRIMASK` 保存、禁中断、恢复回调；consumer
  是 `DmStm32H7UsbHostPipeAsyncDispatch` 的 `enter/leave` 接口。该模块不依赖
  DM-MC02 pin map、QEMU event loop、RTOS 或设备策略。
- [x] 接口契约要求 `opaque` 非空且同一 lock 对象使用成对、嵌套的
  `enter/leave`；`PRIMASK` 只屏蔽可配置异常，不宣称 RTOS `BASEPRI`、SMP 或 DMA
  cache/barrier 语义。
- [x] 锁对象支持嵌套深度：最外层保存原始 `PRIMASK` 并执行 `cpsid i`，最后一层按原值
  恢复 `cpsie i`/`cpsid i`；async guest smoke 增加二层嵌套深度断言，并将同一锁接入
  `dispatch_start/cancel/handle`。
- [x] 验证：带真实 ARM 指令的 `run-stm32h723-usb-host-async-smoke.sh` 通过，既有
  dispatcher 锁回调配对隔离测试继续通过；本切片未修改 QEMU host core、DM-MC02
  board profile 或 `trobot/`。
- [x] 受影响回归：host CTest 串行为 `42/42`，H723 host qtest 为 `4/4`，QEMU smoke
  suite 为 `82/82`，Python 为 `255 passed`，`bash -n tools/*.sh`、`uv lock --check`
  和 QEMU `git diff --check` 通过。
- [ ] 限制与下一道门：这是单核 Cortex-M7 的 PRIMASK 临界区，不是多核锁、RTOS mutex、
  DMA cache barrier 或可抢占 SMP 同步原语；下一步应在 H723 host 层定义多包 PIO/NAK
  retry 的 operation 生命周期，再决定是否需要更细的 FIFO/DMA 锁边界。

## 2026-09-01 H723 completion-driven asynchronous bulk composition（已完成）

- [x] 所属层：可复用 H723 PIO bulk composition 层。producer 是 H723 async dispatcher
  报告的单 packet completion；boundary 是稳定的
  `DmStm32H7UsbHostBulkAsync`、dispatcher slot 和 completion token；consumer 是
  caller 的 IRQ/event loop。该层复用 endpoint state、channel allocator、async pipe 和
  dispatcher，不依赖 DM-MC02 pin map、QEMU event API、DMA 或外部 plant。
- [x] 新增 `firmware/dm_stm32h7_usb_host_bulk_async.[ch]`：整笔请求保持 caller buffer
  指针和累计进度稳定；accepted full packet 自动通过同一 dispatcher 启动下一 packet；
  accepted short packet 结束；精确 OUT multiple 可追加一个 ZLP。只有 accepted bytes 推进
  endpoint DATA PID，NAK 保留当前 packet/PID 并进入 `READY`，由 `resume()` 在后续事件中
  显式重试；channel exhaustion 同样返回 `DEFERRED/READY`；cancel 通过 dispatcher 停止
  pending channel 并释放 lease。
- [x] 隔离门：新增 `dm_stm32h7_usb_host_bulk_async_smoke`，覆盖 `64+64+2` packet
  progress、token generation、channel exhaustion defer/resume、NAK 后 PID/offset 保留和
  cancellation；相关 CTest `4/4` 通过，新模块和测试各自的 `-Wall -Wextra -Werror`
  freestanding ARM 对象编译 `2/2` 通过。
- [x] 修复前置编译缺陷：`firmware/dm_usb_host_endpoint_state.c` 对零值 enum 做恒假
  下界比较，严格 ARM `-Werror=type-limits` 会失败；移除恒假判断后 endpoint、async
  pipe/dispatcher smoke 与 `11` 个实际 guest 源文件的严格 ARM 编译均通过。
- [ ] 限制与下一道门：该组合层每个 packet 仍使用 `PKTCNT=1` 的 PIO async operation，
  不实现 controller 内部 `PKTCNT/XFERSIZE/HCDMA` continuation、DMA/FIFO arbitration、
  SOF retry budget、ISO、多设备 topology、PHY/VBUS、passthrough 或 DM-MC02 Host wiring。
  下一步应先在 QEMU H723 host-controller 层明确并测试多包 PIO completion 的寄存器和
  transport 生命周期，再添加真实 guest consumer；不得用该 firmware composition 替代
  芯片层语义。

## 2026-09-01 H723 DWC2 host lifecycle and packet accounting（已完成）

- [x] 所属层：可复用 STM32H723/DWC2 host-controller 芯片层及其 USB transport 边界。
  producer 是端口、channel 和 controller 生命周期事件以及 transport 返回的实际包长度；
  boundary 是 host channel 的 active/waiting/token 状态和 `HCTSIZ` accounting；consumer
  是异步 completion scheduler 或同步 transport。该切片不依赖 DM-MC02 board wiring。
- [x] 端口断连、`HPRT0.PWR` 关闭和 controller reset 统一取消仍在等待的 channel，并通过
  `DmStm32H7OtgHostChannelCancel(host, channel, token)` 通知外部 owner；取消使 channel
  失活、清除 `CHENA` 和 token，不伪造 `CHHLTD`、completion 或 IRQ。旧 token 的迟到
  completion 不得修改 `HCTSIZ`、`HCDMA`、PID 或 `HCINT`。
- [x] accepted completion 按 `ceil(actual_length / max_packet_size)` 消费 packet count，
  只对实际消费的 packet 切换 DATA PID；零长度 accepted packet 不递减 `PKTCNT`、不切换
  PID，并保留正常终止/继续传输判断。transport 对 SETUP 的识别显式要求
  `endpoint_type == CONTROL && pid == SETUP`，其它 endpoint 始终按 `HCCHAR.EPDIR`
  选择 IN/OUT。
- [x] 隔离门：H723 host-controller `22/22`、host transport `15/15`，覆盖零长度、短包、
  disconnect、port power-off、controller reset、stale completion、packet accounting 及
  非 control endpoint 携带 SETUP PID；受影响 host CTest `44/44`，未修改 `trobot/`。
- [ ] 限制与下一道门：仍没有完整 USB bus/PHY、DMA/FIFO arbitration、SOF retry budget、
  ISO/split、hub/topology、passthrough 或 DM-MC02 Host-role wiring。下一步应在同一 H723
  层定义 NAK 的 caller retry 与 virtual-time controller retry 责任，再扩展 `bInterval` 和
  periodic bandwidth；不得由 board 或 transport workaround 掩盖该边界。

## 2026-09-01 H723 async completion ownership correction（已完成）

- [x] 所属层：STM32H723 host async event boundary。修复 `dispatch_handle()` 对共享
  dispatcher 可能消费其它 channel completion 的问题，新增按 channel/token 的
  `dispatch_handle_channel()`；bulk `poll()` 只消费自己的 lease，且能读取 IRQ handler
  已先提交的 terminal record。
- [x] `POLL_INVALID` 不再被静默忽略：dispatcher 取消当前 operation、清除 slot 并释放
  channel；bulk 在 dispatcher 异常而 completion 仍 pending 时执行一次 abort，避免永久
  `PENDING`。`dispatch_start()` 在写 H723 MMIO 前校验 `base == dispatch->base`。
- [x] 新增共享 dispatcher、malformed completion、错误 base 隔离 smoke，并增加 bulk IRQ-first
  consumer 回归；独立测试使用 `-Wall -Wextra -Wpedantic -Werror`，不接板卡 profile。
- [x] 验证：相关 CTest `3/3`、host CTest `44/44`、ASan/UBSan 相关 CTest `3/3`、严格
  ARM 对象编译 `2/2`、H723 async guest smoke 和 bulk guest smoke 均通过；未修改
  `trobot/`。
- [ ] 限制与下一道门：锁仍是调用者提供的单核临界区，dispatcher 只汇总 HCINT，不安装
  NVIC/vector；仍没有 DMA/FIFO arbitration、SOF retry scheduler、ISO、多设备 topology、
  PHY/VBUS、passthrough 或 DM-MC02 Host-role wiring。下一步继续在 QEMU H723 controller 层
  验证多包 PIO continuation 的 `PKTCNT/XFERSIZE/HCDMA` 生命周期。

## 2026-09-01 H723 USB host control NAK 边界（已完成）

- [x] 所属层：可复用 STM32H723/DWC2 host-controller 的同步 control PIO 边界。producer 是
  H723 host channel/USB transport 产生的 `HCINT.NAK`；boundary 是
  `dm_stm32h7_usb_host_control.c` 对 `HCINT.CHHLTD | HCINT.NAK` 的等待以及 NAK 时写入
  `HCCHAR.CHDIS` 并等待 halt；consumer 是 control transfer caller 和其上层的显式重试策略。可选的
  `DmStm32H7UsbHostControlPoll` 只推进 caller 提供的虚拟 controller，不拥有 request 或
  board state。该切片不依赖 DM-MC02 pin map、QEMU board wiring 或 `trobot/`。
- [x] control helper 将 NAK 视为当前同步 transfer attempt 的 caller-visible terminal result，
  返回 `DM_STM32H7_USB_HOST_CONTROL_NAK`，不推进 control stage、actual length 或隐含重试。
  caller 必须在后续调度点按自己的策略重试整笔 control transfer；driver 不在等待循环中进行
  无界 busy retry。
- [x] control transfer 在确认 `actual_length` 指针有效后先将其置零，因此 NAK、STALL 和
  transaction error 不会把调用者传入的旧值当作本次 transfer 的长度。
- [x] control client 在触碰 MMIO 前校验 H723 的 12-channel 范围、`HCCHAR.DEVADDR`/MPS
  字段范围和 IN/OUT 数据指针；accepted completion 拒绝大于本阶段长度的
  `HCTSIZ.XFERSIZE`，避免无符号下溢或越界 FIFO 访问。
- [x] 确定性测试夹具已闭合：初版 `dm_stm32h7_usb_host_control_nak_smoke` 使用共享
  MMIO，但没有模拟 H723 `HCINT` 的 W1C 语义。driver 先写 `HCINT_ALL` 清 pending 位后，
  夹具仍保留该掩码，导致伪造的 NAK/CHHLTD 状态，测试失败为
  `NAK worker did not observe channel disable`。这是测试 producer/fixture 的错误，不能据此
  修改生产逻辑；修正为简单数组 MMIO 配合一次性的 poll callback，在读取 `HCINT` 前注入
  NAK。该 fixture 只验证 control client 的阶段终止和寄存器写入，不重复声称模拟完整 W1C。
- [x] 窄验证：`dm_stm32h7_usb_host_control_nak_smoke` 通过；受影响 H723
  control/pipe/bulk/periodic/retry CTest `16/16` 通过；真实
  `run-stm32h723-usb-host-smoke.sh`、`run-stm32h723-usb-host-bulk-smoke.sh` 和
  `run-stm32h723-usb-host-async-smoke.sh` 均通过；完整 CTest `45/45` 通过。
- [ ] 限制与下一道门：本切片只定义一次同步 attempt 的 NAK 终止和 caller ownership，不实现
  retry budget/timeout、SOF/virtual-time scheduler、异步 completion/cancel、DMA/FIFO
  arbitration、ISO、hub/topology、PHY/VBUS、passthrough 或 DM-MC02 Host-role wiring。

## 2026-09-01 SPI target boundary 与 BMI088 adapter（已完成）

- [x] 所属层：可复用器件/驱动层及 DM-MC02 board composition 边界。producer 是 H723 SPI2
  寄存器/DMA byte request 和 GPIO active-low CS；boundary 是板卡无关的
  `DmMc02SpiTarget` callback、CS mask 和单字节虚拟时间；consumer 是 BMI088 SPI adapter
  或未来的 Flash/其它 SPI target。SPI core 不再持有 BMI088 或 co-sim link。
- [x] `dm_mc02_spi` 负责 SPI register/DMA state、固定容量 target table、CS mask 路由和
  TX/RX endpoint；`dm_mc02_bmi088_spi` 负责 BMI088 command/read/write framing、accel
  dummy byte、地址递增、FIFO streaming 和 frame-consumed callback；`dm_mc02.c` 只负责
  board pin/DMA tuple/outer co-sim token 的组合。
- [x] 复位契约显式化：SPI core 清理自身状态并取消 TX timer，board reset 依次复位 SPI core、
  BMI088 chip 和 adapter framing；CS mask 变化结束未完成 FIFO read，避免半帧跨 transaction
  传播。多选 CS 会被 adapter 观察并终止当前 framing，但 functional core 不模拟电气争用。
- [x] 隔离门：`dm_mc02_bmi088_spi_smoke` 覆盖 gyro/accel ID framing、accel dummy byte、
  地址递增、direct burst 消费通知和 CS 断帧；SPI2 polling 以及 DMA endpoint on/off 的
  直接 H723/QEMU 边界 smoke 通过。完整 host CTest 为 `46/46`，QEMU smoke suite 为
  `82/82`。
- [x] 未修改 `trobot/`；本切片没有复制 SPI core 到 fixture，也没有扩展到完整 SPI mode、
  electrical timing、DMA FIFO arbitration 或多器件总线争用。
- [ ] 限制与下一道门：SPI target table 当前固定为 4 项，只有单选 target 有响应；DMA TX
  deferred timer 仍由初始化者显式启用，SPI1 仍是无 DMA 的寄存器安全模型。下一步若接入
  Flash，应先增加独立 adapter/CS 测试，再接 board profile，不把 Flash framing 写回 SPI core。

## 2026-09-01 SPI core dispatch 与 OSPI page boundary（已完成）

- [x] 所属层：可复用 SPI 芯片数据面隔离测试，以及 STM32H723 OCTOSPI2/W25Q64
  芯片层提交边界。SPI producer 是 SPI MMIO TXDR/CS，boundary 是
  `DmMc02SpiTarget` target table，consumer 是 fake target 与 BMI088 adapter；OSPI
  producer 是 guest 的 `DLR/IR/AR/DR` transaction，boundary 是 256-byte staging buffer，
  consumer 是 W25Q64 backing array。
- [x] 新增 `dm_mc02_spi_target_smoke`，不复制 SPI 实现，覆盖 target 注册、单选、无选、
  多选、CS 广播、虚拟时间戳、BMI088 adapter 接入和 CS 断帧；CTest 独立门通过。
- [x] 修复 OSPI page-program 根因：`execute()` 报告 `DLR + 1 > 256` 后，commit 边界
  重新检查原始长度，防止 staging buffer 填满时仍部分写 Flash；原始 DLR 在加一前检查，
  避免 `0xffffffff` 回绕为零。新增 WREN-enabled 257-byte 和最大 DLR bare-metal regression，
  确认 `TEF`、Flash 未改变且 WEL 未被错误清除；合法 256-byte program 行为保持不变。
- [x] 验证：SPI target smoke 通过，`bash tools/run-ospi-smoke.sh` 通过，QEMU 增量构建通过，
  host `ctest --test-dir build/host --output-on-failure` 为 `47/47`（含 QEMU suite）。
- [ ] 限制与下一道门：SPI 仍是 functional byte model；OSPI 仍不实现真实 line mode、
  DTR、DMA、总线延迟或 Flash 持久化。下一步若接入 Flash 到 generic SPI，先实现并隔离
  验证板卡无关的 Flash framing adapter，再进行 board composition。

## 2026-09-01 可复用 NOR Flash 存储核心（已完成）

- [x] 所属层：可复用器件/驱动层。producer 是 OCTOSPI 或未来 SPI Flash framing
  adapter；boundary 是板卡无关的 `DmNorFlash` geometry/status/operation API；consumer
  是调用者拥有的 backing storage。核心不依赖 QEMU、DM-MC02 pin map 或总线线模式。
- [x] 新增 `cosim/dm_nor_flash.[ch]`，将 storage ownership、WEL/WIP、NOR 1->0
  page program、页内回绕、sector erase、reset 保留和结果分类从 `dm_mc02_ospi.c`
  移出。OSPI 现在只负责寄存器事务、数据暂存和状态映射；OSPI 的 declared DLR length
  也成为实际提交长度，额外 DR 写入不再越过事务边界。
- [x] 隔离门：新增 `dm_nor_flash_smoke`，覆盖几何绑定、WEL/WIP 与 busy、无 WREN
  保护、1->0 编程、页边界回绕、sector erase、非法长度/越界原子拒绝及 reset 后存储
  保留。窄 CTest 通过。
- [x] 直接消费者门：`bash tools/run-ospi-smoke.sh` 通过；`ninja -C build/qemu
  qemu-system-arm` 通过；完整 host CTest `48/48` 通过，其中 QEMU smoke suite `82/82`。
  未修改 `trobot/`，没有保留活跃子代理。
- [ ] 限制与下一道门：核心是同步 storage model，WIP 没有可观察的真实 wall/virtual
  latency；OSPI 仍不实现 line mode、DTR、DMA、bus delay、persistence 或完整 W25Q
  status/config registers。下一步应先为 generic SPI 定义并隔离验证 Flash command
  framing adapter，再接入 SPI target table；不得把 framing 逻辑重新写入 SPI core。

## 2026-09-01 通用 SPI NOR command framing adapter（已完成）

- [x] 所属层：可复用器件/驱动层。producer 是板卡无关的
  `DmMc02SpiTarget` 单字节/CS callback；boundary 是
  `dm_mc02_spi_nor.[ch]` 的命令 framing、三字节地址、CS 事务和
  `DmNorFlash` 调用；consumer 是 caller-owned NOR core。该切片不拥有 backing
  storage，也不依赖 DM-MC02 pin map 或 SPI core 私有状态。
- [x] 支持 `WREN`、`RDSR`、`JEDEC ID`、normal/fast read、page program 和
  4 KiB sector erase。program/erase 在 CS release 时原子提交；普通读取线性递增，
  页内回绕只由 NOR storage core 的 page-program 契约提供。
- [x] 非法或多选 CS mask 在命令解码前进入 ignore，不能改变 WEL；257 字节及以上
  page program 由 257-byte staging buffer 交给 NOR core 拒绝，Flash 和 WEL 保持不变。
  `last_result` 显式初始化为 `DM_NOR_FLASH_INVALID`，CS 中断会丢弃未完成 framing。
- [x] 隔离门：新增 `dm_spi_nor_flash_smoke`，覆盖 JEDEC/status/WEL、page-wrap
  program、normal/fast read、NOR program、oversized program 原子拒绝、sector erase、
  invalid CS 和半事务；窄测试通过。
- [x] 直接构建门：QEMU `qemu-system-arm` 重新配置并构建通过，日志确认新 adapter
  已编译进 ARM target；OSPI 裸机 smoke 通过；完整 host CTest 为 `49/49`，其中 QEMU
  smoke suite 通过且耗时约 `66.7s`。
- [x] 本切片未修改 `trobot/`，没有活跃子代理；根目录和 `dm-mc02-qemu/` 的
  `AGENTS.md` 继续要求逐级开发、隔离验证、根因修复和及时释放子代理。
- [ ] 限制与下一道门：当前仍是 functional SPI byte model，不实现 quad/DTR/bit-level
  timing、Flash persistence、完整 W25Q status/config、异步 WIP 或真实 bus latency。
  下一步应把该 adapter 接入 SPI core target table 做直接组合测试，确认 MMIO/DMA/CS
  boundary 后再修改 DM-MC02 board profile。

## 2026-09-01 SPI core 与 NOR adapter 直接组合门（已完成）

- [x] 所属层：STM32H723 SPI 数据面到可复用 NOR 器件的直接边界。producer 是
  `DmMc02Spi` 的 `TXDR/RXDR` MMIO 和 `dm_mc02_spi_select_mask()`；boundary 是
  target table 与 `DmMc02SpiTarget` callback；consumer 是
  `DmSpiNorFlash -> DmNorFlash`。测试没有复制 SPI core 或访问 DM-MC02 私有状态。
- [x] 扩展 `dm_mc02_spi_target_smoke`，通过真实 SPI core 注册 NOR adapter，覆盖
  JEDEC/status/WEL、page program 与 linear readback、非法多选 CS 不执行 WREN、以及
  未完成地址事务在 CS 释放后被丢弃。
- [x] 窄门：`cmake --build build/host --target dm_mc02_spi_target_smoke -j2` 和
  对应 CTest 通过；完整 host CTest 为 `49/49`；`bash tools/run-ospi-smoke.sh`
  通过；`ninja -C build/qemu qemu-system-arm` 通过。
- [x] 本切片没有修改 `trobot/`，没有活跃子代理；既有 QEMU 上游
  `vhost_svq_poll()` 未初始化 warning 仍不属于本切片。
- [ ] 限制与下一道门：尚未把 NOR adapter 接入 DM-MC02 GPIO active-low CS、板级
  profile 或实际 machine 属性。下一步应先定义并隔离验证 GPIO/AF 到 SPI CS 的最小
  board route，再做板级 Flash composition；不在 SPI core 中加入板卡策略。

## 2026-09-01 DM-MC02 GPIO SPI CS board route（已完成）

- [x] 所属层：DM-MC02 板级组合层。producer 是 GPIO `MODER/ODR` 输出快照；boundary
  是 `DmMc02BoardSpiCsRoute` 与 `dm_mc02_board_decode_spi_selected_mask()`；consumer
  是可复用 SPI core 的 `selected_mask`。SPI core、BMI088 和 NOR adapter 不再拥有板级
  pin/polarity 策略。
- [x] route table 成为 DM-MC02 与 STM32H723-EVAL profile 的 CS source of truth：按
  controller、target bit、GPIO pin 和 active-low 极性解码；未配置为 GPIO output 的 CS
  不选中；target capacity 由直接 consumer 提供，避免 board 层复制 SPI table 容量。
- [x] `dm_mc02_board_profile_smoke` 覆盖输入态不选中、active-low 单选/多选、CS
  释放、active-high 可复用分支、target capacity 越界和重复 route 拒绝。此前真实
  BMI088 smoke 暴露 fixture 未配置 PC0/PC3 `MODER`，已补齐所有相关 SPI/BMI088
  smoke 的 GPIO output 初始化；该修复保持 production route 的真机语义。
- [x] 直接边界：BMI088 polling、SPI2 DMA endpoint `on/off`、FIFO、滤波、cosim
  readback/timing 均通过；QEMU `qemu-system-arm` 重建成功；host CTest `49/49`，其中
  QEMU smoke suite `82/82` 通过。未修改 `trobot/`。
- [ ] 限制与下一道门：当前 route 只支持软件 GPIO 控制的 CS，未实现硬件 NSS/AF CS、
  电气争用或 SPI bus timing；机器仍只组合 BMI088 target 0/1。下一步应在本 route 门
  之后，将独立 NOR adapter 绑定到明确的 DM-MC02 Flash profile/target bit，再做实际
  Flash machine 属性和读写 smoke。

## 2026-09-01 DM-MC02 OCTOSPI Flash profile composition（已完成）

- [x] 所属层：DM-MC02 板级组合层到可复用 OCTOSPI/NOR 器件层。producer 是 board
  profile 的 Flash 几何和 OSPI 实例声明；boundary 是
  `DmMc02BoardFlashProfile -> DmMc02OspiFlashConfig`；consumer 是 OSPI 寄存器窗口、
  W25Q64 backing storage 和可选 memory-mapped window。Flash 命令和存储语义没有复制
  到 board 层。
- [x] DM-MC02 profile 声明真实 `OCTOSPI2 -> W25Q64JV`：8 MiB、256-byte page、
  4 KiB sector、JEDEC `EF 40 17`，并打开 `0x70000000` memory-mapped capability。
  `STM32H723-EVAL` 明确声明无外部 Flash；其 OSPI 寄存器窗口仍存在，但不会添加未
  初始化的 Flash memory region。
- [x] profile 初始化校验拒绝非法 OSPI 实例、几何、JEDEC ID、重复实例和不具备
  memory-mapped 地址的配置。OSPI 实例拥有 backing storage，board profile 只提供
  数据和组合；外部 Flash persistence 尚未实现。
- [x] 修复 OSPI 芯片层 DLR 边界：`DLR` 的原始值在 `+1` 前检查，`0xffffffff` 不会
  回绕为零长度；非法读和 page program 设置 `SR.TEF`，不修改 Flash 或错误清除 WEL。
  page program 仍在命令接受和最终提交两个边界检查长度。
- [x] 隔离/直接边界：board profile smoke 覆盖 W25Q64/EVAL 几何和非法 profile；
  `run-ospi-smoke.sh` 覆盖 JEDEC、读写擦除、257-byte page program 和最大 DLR 读；
  `run-board-profile-smoke.sh` 覆盖无 Flash profile 的 QEMU machine 组合。
- [x] 验证：`bash tools/run-ospi-smoke.sh`、`bash tools/run-board-profile-smoke.sh`、
  `ninja -C build/qemu qemu-system-arm` 和 host `ctest --test-dir build/host
  --output-on-failure`（49/49）通过；未修改 `trobot/`。
- [ ] 限制与下一道门：OCTOSPI 仍是同步 functional model，不实现真实 line mode、DTR、
  DMA、bus latency、异步 WIP、ECC、option bytes 或 Flash persistence。下一步应在
  保持 profile/OSPI ownership 边界的前提下，先补 OSPI DMA 数据面或显式 persistence
  adapter 的独立契约，不把这些策略写进 board profile。

## 2026-09-01 W25Q64 擦除粒度与 OSPI 命令映射（已完成）

- [x] 所属层：可复用 NOR 器件层到 STM32H723 OCTOSPI 芯片层。producer 是调用者设置
  WEL 后发出的擦除请求；boundary 是 `dm_nor_flash_erase()` 的对齐/几何契约；consumer
  是 OSPI 的 W25Q64 `0x20/0x52/0xd8/0xc7` 命令映射。没有把 W25Q64 特定擦除规则写入
  board profile。
- [x] `DmNorFlash` 现在支持按 sector-size 整数倍的 block erase，并提供独立的
  `sector_erase()` 和 `chip_erase()` 入口。擦除地址向对应粒度对齐，非法粒度返回
  `LENGTH_INVALID` 并保留 WEL；成功擦除后设置/清除 WIP 的同步生命周期与 page
  program 一致，并消费 WEL。
- [x] OSPI 增加 W25Q64 32 KiB block erase、64 KiB block erase 和 chip erase；带地址的
  两种 block 命令在 AR 写入时执行，chip erase 在 IR 写入时执行。命令映射仍调用
  `DmNorFlash`，不复制存储实现。
- [x] 隔离门：`dm_nor_flash_smoke` 覆盖 32K/64K 非对齐地址、非法擦除粒度、WEL 保持
  和整片擦除；直接 ARM 门 `run-ospi-smoke.sh` 覆盖三种 W25Q 命令和 memory-mapped
  读回，验证擦除前写入值、擦除后 `0xffffffff` 及最终状态。
- [x] 验证：NOR/SPI NOR smoke 通过，`bash tools/run-ospi-smoke.sh` 通过，
  `ninja -C build/qemu qemu-system-arm` 通过，host CTest `49/49` 通过；未修改
  `trobot/`，子代理已关闭并释放。
- [ ] 限制与下一道门：当前擦除仍是同步 `memset`，没有 4 KiB/32 KiB/64 KiB/整片真实
  wall-clock 或 virtual-time busy 窗口、suspend/resume、掉电中止、ECC、块保护和
  persistence。下一步应先选择一个独立的 Flash timing/persistence adapter 契约，不能
  在 OSPI 命令分支中直接加入 sleep 或宿主文件 I/O。

## 2026-09-01 H723 DMA -> OCTOSPI endpoint 直接边界（已完成）

- [x] 所属层：STM32H723 可复用 DMA 芯片层到 OCTOSPI 芯片层 endpoint 的直接边界。
  producer 是 DMA/DMAMUX stream request；boundary 是 `DmMc02DmaEndpoint` 的逐 beat
  callback、guest RAM 地址推进、`NDTR`、TC/TE 状态；consumer 是 `DmMc02Ospi` 的
  indirect read/page-program 数据路径。DMA 仍拥有 stream 状态，OSPI 不访问 DMA
  private cursor 或寄存器。
- [x] `dm_mc02_ospi_dma_endpoint()` 提供 P2M RX queue read 和 M2P page-program
  staging；P2M 不会读出 RX queue 剩余长度，M2P 不会越过 DLR/page buffer 边界，失败
  不能把未接受的 beat 伪造成 DMA 成功。timestamp 目前只透传到 endpoint，不产生额外
  时延。
- [x] 隔离门 `dm_mc02_ospi_dma_endpoint_smoke` 覆盖 JEDEC P2M、page-program M2P、
  oversized beat、reset 清理和 memory-mapped window 语义。直接边界门新增
  `dm_mc02_ospi_dma_integration_smoke`，通过真实 DMA/DMAMUX 搬运 guest RAM 与 OSPI
  数据，并断言地址/`NDTR`/TC，另覆盖 endpoint rejection -> TEIF 且 RAM 不被提交。
- [x] 验证：两个 OSPI smoke 通过；完整 host CTest `51/51`，其中 QEMU smoke suite
  `82/82`；`bash tools/run-ospi-smoke.sh`、`bash tools/run-board-profile-smoke.sh`
  和 `ninja -C build/qemu qemu-system-arm` 通过。QEMU 上游已有的
  `vhost_svq_poll()` 未初始化 warning 仍不属于本切片；未修改 `trobot/`，没有活跃
  子代理。
- [ ] 限制与下一道门：当前直接测试使用 synthetic request ID 和 endpoint address，
  因为只读核对的实际 `trobot` 配置没有启用 OSPI DMA；尚未猜测 DMAMUX request ID、
  OCTOSPIM 路由或修改 board profile。下一步只有在获得真实板级配置证据后，才接入
  DM-MC02 profile；另外仍未实现 OSPI line mode、DTR、真实 DMA bus timing、async
  backpressure、Flash latency/persistence。

## 2026-09-01 可复用 NOR Flash raw-image persistence adapter（已完成）

- [x] 所属层：可复用 NOR 器件/驱动层到 DM-MC02 QEMU machine 边界。producer 是
  `DmNorFlash` caller-owned storage；boundary 是
  `cosim/dm_nor_flash_persistence.[ch]` 的 raw-image load/save API；consumer 是
  `DmMc02Ospi` backing storage 及 machine 生命周期。adapter 不解析 OSPI/QEMU 私有
  状态，也不把板级策略写入 NOR core。
- [x] 公共接口和结果语义：`dm_nor_flash_persistence_load()` / `_save()` 传入路径、
  byte storage 和精确尺寸，结果区分 `OK`、`DISABLED`、`NOT_FOUND`、`IO_ERROR`、
  `SIZE_MISMATCH` 和 `INVALID`。文件是完整、精确几何的原始镜像，不带 header；缺失
  文件保持调用者预先设置的擦除态，尺寸不匹配拒绝加载且不修改 storage；空路径直接
  返回 `DISABLED`，不做磁盘 I/O。
- [x] `DmMc02Ospi` 只提供 load/save persistence 薄封装，转交 `state->flash.storage`
  和 `state->flash_size`。machine 属性 `ospi2-flash-file` 只能在初始化锁定前配置；
  OSPI2 初始化后加载文件，加载失败（缺失除外）告警并使用擦除镜像；仅在正常退出
  notifier 中保存，保存失败告警，不在 Flash 命令热路径写盘。
- [x] 隔离门 `dm_nor_flash_persistence_smoke`、OSPI 直接边界测试及
  `bash tools/run-ospi-smoke.sh` 通过。脚本验证 8 MiB W25Q64 raw image 的启动加载、
  guest 读回和正常 `quit` 后保存；host CTest 记录为 `52/52`，其中 persistence smoke
  通过。未修改 `trobot/`。
- [ ] 限制与下一道门：该切片不模拟真实 Flash latency、ECC、掉电原子性或异步保存；
  只保证正常退出路径的完整 raw-image 写入。异常终止、崩溃和断电恢复仍未定义。

## 2026-09-01 H723 ADC JAUTO/JQM regular DMA 边界（已完成）

- [x] 所属层：STM32H723 ADC 芯片层到 DMA 数据面的直接边界。producer 是 ADC1
  regular EOS 自动提交 injected context；boundary 是 `CFGR.JAUTO/JQM`、`JSQR`、
  `JDR1/JEOC/JEOS` 与 ADC1 request 9 的 regular DMA 状态；consumer 是 guest
  circular DMA buffer 和 injected result/status。
- [x] 新增 `smoke/dm_mc02_adc_jauto_dma_smoke.c`、对应 linker script 以及
  `tools/run-adc-jauto-dma-smoke.sh`。guest 配置一个 regular rank、一个 injected
  context、`JAUTO=1`、`JQM=1`、regular circular half-word DMA，验证 regular 两 rank
  的 EOS 自动启动 injected 两 rank sequence，sequence 完成后清空 `JSQR`，并确认
  regular DMA 按 rank 顺序继续完成 HT/TC、buffer 样本保持正确。
- [x] 隔离/边界门：ADC qtest、DMA 相关 CTest 和该 bare-metal smoke 分别覆盖
  芯片寄存器语义、DMA 状态面以及真实 guest/QEMU 组合；测试失败时先区分 ADC
  EOS/自动注入、DMA request/状态和 QMP 观测 consumer，未用测试脚本 workaround
  修改生产模型。
- [x] 本切片未修改 `trobot/`，未改变 v1 wire 或 board profile；没有活跃子代理。
- [ ] 限制与下一道门：当前只覆盖 regular/injected 各两个 rank 的成功组合，
  不覆盖 `JAUTO` 与 `DISCEN/JDISCEN` 非法组合、injected 外部 trigger 完整矩阵、
  ADC 低功耗自动断电或任意深度 injected FIFO。下一步应先补多 rank/中止和错误路径
  的 ADC 芯片层回归，再向 DM-MC02 板级采样配置集成。

## 2026-09-01 VIN smoke 临时目录清理（已完成）

- [x] 所属层：用户工具层。producer 是 `run-vin-config-smoke.sh` 创建的精确临时
  运行目录；boundary 是 `EXIT` cleanup trap；consumer 是 QEMU stderr/QMP socket
  临时文件和工作区目录。
- [x] cleanup 在终止 QEMU 后删除 `qemu.stderr` 与 QMP socket，再移除本次运行目录；
  不改变 VIN、QMP、reset 或供电模型语义。
- [x] 隔离验证：`bash -n tools/run-vin-config-smoke.sh` 与
  `bash tools/run-vin-config-smoke.sh` 通过；运行前后精确匹配的
  `output.dm-mc02-vin.*` 目录数量保持不变（`407 -> 407`）。
- [x] 本切片没有修改 `trobot/`，没有活跃子代理。
- [ ] 限制与下一道门：修复只防止后续泄漏，不删除此前遗留的临时目录；若要清理历史
  生成物，应另行执行已核对目标的维护操作，不在测试脚本中隐式删除。

## 2026-09-01 H723 D2 APB1/APB2 -> timer kernel clocks（已完成）

- [x] 所属层：板卡无关的 STM32H7 clock-tree helper、H723 RCC 芯片层和
  DM-MC02 定时器时钟边界。producer 是有效 SYSCLK/HCLK、`RCC_D2CFGR` 与
  `RCC_CFGR.TIMPRE`；boundary 是 APB1/APB2 PCLK 和 timer kernel clock 查询接口；
  consumer 是 DM-MC02 的 TIM2/3/12/24 与 TIM1/8 实例。
- [x] `dm_stm32h7_clock_tree.[ch]` 新增 D2 `D2PPRE1/D2PPRE2` 解码、APB 频率和
  H7 `TIMPRE` 规则：`TIMPRE=0` 时 APB `/1` 使用 PCLK，其它分频使用 `2*PCLK`；
  `TIMPRE=1` 时 APB `/1,/2,/4` 使用 HCLK，较大分频使用 `4*PCLK`。helper 不依赖
  QEMU、DM-MC02 或 board profile。
- [x] RCC 新增 APB1/APB2 PCLK 与 timer kernel clock API；`RCC_D2CFGR` 和
  `RCC_CFGR.TIMPRE` 写入进入已有时钟通知边界。DM-MC02 用两个独立 QEMU `Clock`，
  profile 只通过 `DmMc02BoardTimerClockDomain` 声明每个 timer 属于 APB1 或 APB2，
  不再共享错误的 `CPU/2` 常量。
- [x] 隔离门 `dm_stm32h7_clock_tree_smoke` 覆盖 APB 编码和两种 TIMPRE 规则；直接
  QEMU 门 `dm-mc02-tim2-test` 现在为 `22/22`，新增运行中 APB1 改频且 TIM8/APB2
  不受影响的相位连续性测试。`run-tim2-clock-smoke.sh`、`run-pwm-smoke.sh` 和
  完整 host CTest `53/53` 通过，QEMU 已重链。
- [x] 复位语义同步：D2CFGR 复位为 APB `/1`，因此裸机复位时 H723 timer clock
  为 64 MHz；定时器 qtest 使用显式 HCLK/APB `/2` fixture 保留历史 32 MHz 测试
  基线，PWM/TIM IRQ smoke 已改为真实 64 MHz 复位假设。未修改 `trobot/`。
- [ ] 限制与下一道门：本切片只连接 timer APB1/APB2 kernel clock；FDCAN、UART、SPI、
  其它 kernel source、APB3/APB4/D3、clock settling、CSS、低功耗和完整 TIMPRE/clock
  mux 仍未完成。下一步应选择一个直接 APB/kernel-clock consumer（优先 FDCAN 或 UART）
  单独补齐，继续保持底层隔离测试后再进入更高层组合。
# 2026-09-01 H723 FDCAN kernel-clock source boundary（已完成）

- [x] 所属层：STM32H723 RCC 芯片时钟层到可复用 FDCAN consumer 的直接边界。
  producer 是 `D2CCIP1R.FDCANSEL`、HSE/PLL enable、PLLCKSELR、PLL1/2DIVR、
  PLLFRACR 和 `DIVQEN`；boundary 是
  `dm_mc02_pwr_rcc_fdcan_kernel_clock_hz()`；consumer 是 DM-MC02 的三个 FDCAN
  timing instances。
- [x] 实现准确 source mapping：`00=HSE`、`01=PLL1Q`、`10=PLL2Q`、`11=0`；PLL
  Q 输出要求对应 Q divider enable，RCC 的相关寄存器写入会进入时钟通知边界。
- [x] 移除 DM-MC02/EVAL board profile 的固定 `120 MHz` 字段；machine 通过 RCC
  查询更新 FDCAN。增加只读 `/machine` `fdcan-kernel-clock-hz` 诊断值，既不替代
  RCC producer，也不让测试访问私有 FDCAN 状态。
- [x] 隔离/直接边界门：`run-fdcan-clock-smoke.sh` 验证四种 source 的
  `24/120/96/0 MHz`，`run-fdcan-medium-smoke.sh` 验证显式 PLL1Q 下的准确时序；
  PWR/RCC、machine、Release 固件 RTF 回归均通过，未修改 `trobot/`。
- [ ] 限制与下一道门：当前 source readiness 立即生效，未模拟 PLL lock/oscillator
  settling、CSS、低功耗和完整 kernel source matrix；FDCAN 的零时钟门控和动态改频
  中正在传输帧的行为也未定义。下一步应继续选择一个独立 kernel-clock consumer，或
  先为 FDCAN zero-clock semantics 建立芯片层契约，不能在 board profile 恢复常量。

# 2026-09-01 H723 USART kernel-clock source boundary（已完成）

- [x] 所属层：STM32H723 RCC 芯片层到可复用 USART consumer 的直接边界。producer 是
  `RCC_D2CCIP2R`、APB1/APB2 分频、HSI/CSI/LSE readiness 和 PLL2/PLL3 Q 输出；boundary
  是 `dm_mc02_pwr_rcc_usart16_kernel_clock_hz()` 与
  `dm_mc02_pwr_rcc_usart234578_kernel_clock_hz()`；consumer 是 DM-MC02/EVAL profile
  中各 routed UART 的 kernel `Clock`。
- [x] 实现两个 H723 USART mux group：USART16 使用 bits `5:3` 和 APB2，USART234578
  使用 bits `2:0` 和 APB1；支持 APB、PLL2Q、PLL3Q、HSI/HSIDIV、CSI、LSE，保留 source
  返回 `0 Hz`，PLL Q output 要求对应 `DIVQEN` 和有效 PLL 输入/分频。
- [x] machine 创建每个 group 的 root clock 及每个 UART 的 child clock，UART 通过
  `dm_mc02_uart_set_kernel_clock()` 只消费缓存频率；新增只读诊断属性
  `usart16-kernel-clock-hz` 与 `usart234578-kernel-clock-hz`，没有把 RCC 或 board
  profile 依赖写入可复用 UART 模型。UART cleanup 会清除 clock callback，避免析构后的
  回调访问已释放 UART。
- [x] 隔离/直接边界门：`tools/run-uart-clock-smoke.sh` 为每个 RCC source 独立启动
  guest，覆盖 9 个配置 case（含不同 APB1/APB2 分频），并通过 QMP 读取两个只读属性；
  完整 QEMU smoke `86/86`、host CTest `53/53` 通过，未修改 `trobot/`。
- [ ] 限制与下一道门：当前测试因 QEMU HMP 不提供 `mw` 而采用独立启动，尚未覆盖运行中
  RCC 改写后的 UART consumer 回调；UART 仍未实现 `BRR/PRESC -> baud` 位级时序、FIFO
 传输延迟、oversampling、LIN/Smartcard/irDA、完整 kernel source matrix 或真实 PLL/LSE
  settling。下一步应先在 UART 可复用器件层定义 BRR/PRESC baud timing 契约和隔离测试，
  再由 DM-MC02 接入，不能由 board 层恢复固定 baud 常量。

# 2026-09-01 H723 USART BRR/PRESC -> virtual-time TX pacing（已完成）

- [x] 所属层：可复用 UART 器件时序层到 DM-MC02 UART 的直接边界。producer 是缓存的
  H723 USART kernel clock 与 `BRR/PRESC/CR1.OVER8` 寄存器值；boundary 是板卡无关的
  `dm_uart_timing_calculate()`；consumer 是 DM-MC02 UART 的 host-facing TX queue 和
  QEMU virtual timer。
- [x] 新增 helper，集中实现 H7 PRESC 表、16x/8x BRR 解码、有效性边界和默认 8N1
  10-bit frame duration；helper 不依赖 RCC、board profile、chardev 或外部 plant，使用
  `__uint128_t` 做整数纳秒计算。
- [x] UART 在 kernel clock、BRR、PRESC 或 OVER8 改变后重新计算；有效配置逐帧发送一个
  queued byte，背压仍按非阻塞虚拟时间重试，最终字节发送完成后才置 `TC`。无效配置保留
  复位/兼容路径的立即发送行为；RX、校验位、停止位和物理位级线路仍不在本切片。
- [x] 隔离门 `dm_uart_timing_smoke`、QEMU 边界门 `tools/run-uart-timing-smoke.sh`，以及
  既有 UART polling/IDLE/DMA（endpoint on/off）smoke 均通过；QEMU 已重链，未修改
  `trobot/`。
- [x] 性能门：真实 Release `trobot.elf` 采样为 `1001` tick / `1.001201 s`，RTF
  `0.999800x`，QEMU CPU `102.9%`、RSS `50296 KiB`、IWDG `timeouts=0`；NullEngine
  `184376 IMU frames/s`，worker 启动到 RESET `64.953 ms`，DM-MC02 startup smoke
  `49.847 ms`。与既有约 `1x` 基线一致，未观察到 UART 时序形成新的性能门槛。
- [ ] 限制与下一道门：当前只对 TX 使用默认 8N1 帧时长，RX 仍按 chardev chunk 产生
  字节/IDLE；尚未实现 parity/stop-bit 配置、RX sampling、LIN/Smartcard/IrDA 或运行中
  RCC 改频时保留已发送帧的精确相位。下一步应先补 UART 的 RX/IDLE 时序边界，再接更高层
  串口工具或外部 plant，不要在 UI 层补时序。

## 2026-09-01 EXTI component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 EXTI 状态层。producer 是 EXTI 的 CPU-visible
  `regs[0x400]` 镜像和 16 条输入线的 `line_level` 采样状态；boundary 是
  `dm_mc02_exti_vmstate()`；consumer 是未来的 machine-level migration 组合以及当前
  EXTI/NVIC IRQ 投影。
- [x] VMState 版本为 1，只保存 `regs` 和 `line_level`。`MemoryRegion`、七个 IRQ handle
  和派生的 `irq_level` 不进入状态流；成功 post-load 后通过
  `dm_mc02_exti_sync_runtime()` 按恢复的 `PR1/C1IMR1` 强制重建全部 IRQ 投影，避免目标
  连接仍保留旧电平。
- [x] 状态边界保持原子：版本错误或截断流在回载完成前拒绝，不调用 runtime-sync；warm
  reset 清空寄存器、输入采样和 IRQ 输出。该切片没有注册 machine-level VMState。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_exti.h`、`dm_mc02_exti.c`、
  `dm_mc02_exti_vmstate.c`、`hw/arm/meson.build`、`tests/unit/meson.build` 和
  `tests/unit/test-dm-exti-vmstate.c`。
- [x] 隔离门 `test-dm-exti-vmstate` TAP `2/2` 通过，覆盖寄存器/采样电平 round-trip、
  IRQ 重投影和截断流拒绝；直接 consumer `tools/run-exti-smoke.sh` 通过，覆盖软件触发、
  GPIO 输入边沿、W1C 清 pending 和运行中 SYSCFG 重映射。
- [x] 验证：`ninja -C build/qemu qemu-system-arm`、独立 QEMU smoke suite `89/89`、
  串行 Host CTest `54/54` 均通过；本轮未修改 `trobot/`，没有活跃子代理或残留任务。
- [ ] 限制与下一道门：该描述仍未注册到 DM-MC02 machine，不能宣称整机 snapshot/
  migration；EXTI 的完整 H723 line 触发矩阵、安全域、event-only/CPU2 投影和所有 reset
  细节仍未实现。下一步继续在 STM32H723 层审计下一个独立外设状态边界，不向板级或 UI
  添加迁移 workaround。

## 2026-09-01 DMAMUX component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 DMAMUX 状态层。producer 是 DMAMUX 的 CPU-visible
  `regs[0x400]` 镜像和每次配置写入/复位递增的 `generation`；boundary 是
  `dm_mc02_dmamux_vmstate()`；consumer 是 DMA 请求选择与未来的 machine-level migration
  组合。
- [x] VMState 版本为 1，只保存 `regs` 和 `generation`。`MemoryRegion` 不进入状态流；
  generation 保留 DMA request-stream cache 的配置代际，使恢复后的 DMA consumer 能够
  判定缓存是否仍对应当前 DMAMUX 配置。该组件没有额外派生 callback 或 timer，因此不
  需要 post-load side effect。
- [x] 状态边界保持原子：截断或版本错误的流在字段恢复完成前拒绝；DMAMUX warm reset
  清空寄存器并递增 generation。该切片没有注册 machine-level VMState。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_dma.h`、
  `dm_mc02_dmamux_vmstate.c`、`hw/arm/meson.build`、`tests/unit/meson.build` 和
  `tests/unit/test-dm-dmamux-vmstate.c`。
- [x] 隔离门 `test-dm-dmamux-vmstate` TAP `2/2` 通过，覆盖首尾寄存器、generation
  round-trip 和截断流拒绝；直接 DMA consumer 的 `run-dma-smoke.sh`、
  `run-dma-arbitration-smoke.sh`、`run-dma-batch-smoke.sh` 均通过。
- [x] 验证：`ninja -C build/qemu qemu-system-arm` 通过；本切片未修改 `trobot/`，没有
  活跃子代理或残留构建任务。
- [ ] 限制与下一道门：该描述仍未注册到 DM-MC02 machine，不能宣称整机 snapshot/
  migration；DMA stream live/reload address、FIFO、IRQ、endpoint callback 和
  DMAMUX/DMA 的联合状态仍需在 DMA 芯片层单独审计。下一步继续留在 STM32H723 层，先
  设计 DMA component state contract，再进入定时器/ADC 等带动态时序外设。

## 2026-09-01 DMA stream component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 DMA stream 状态层。producer 是 DMA 寄存器镜像、循环/
  双缓冲 reload tuple、两个 live memory cursor 和每个 stream 的 FIFO 字节状态；boundary
  是 `dm_mc02_dma_vmstate()`；consumer 是未来 machine-level migration 组合及 DMA 的
  peripheral-request/endpoint 数据面。
- [x] VMState 版本为 1，保存 `regs[0x400]`、8 个 stream 的 reload/count/address、live
  cursor、16-byte FIFO、head 和 length。`MemoryRegion`、DMAMUX channel offset、stream
  enable callback、IRQ handles、request cache 和派生 IRQ 电平不进入状态流。
- [x] `dm_mc02_dma_post_load()` 在 runtime sync 前拒绝超过 16-bit NDTR 的 reload count、
  越界 FIFO head/length 及空 FIFO 的非零 head；成功回载通过
  `dm_mc02_dma_sync_runtime()` 使 request cache 失效并重投影 level-sensitive IRQ。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_dma.h`、`dm_mc02_dma.c`、
  `dm_mc02_dma_vmstate.c`、`hw/arm/meson.build`、`tests/unit/meson.build`、
  `tests/unit/test-dm-dma-vmstate.c` 和 test-only memory stub。
- [x] 隔离门 `test-dm-dma-vmstate` TAP `3/3` 通过，覆盖完整 live state round-trip、
  runtime cache/IRQ 重建、截断流及非法 FIFO 拒绝；直接 DMA consumer 的
  `run-dma-smoke.sh`、`run-dma-arbitration-smoke.sh`、`run-dma-batch-smoke.sh` 均通过。
- [x] `ninja -C build/qemu qemu-system-arm` 通过，串行 Host CTest `54/54` 通过；本轮未修改
  `trobot/`，没有活跃子代理或残留构建任务。
- [ ] 限制与下一道门：该描述仍未注册到 DM-MC02 machine，不能宣称整机 snapshot/
  migration；DMA/DMAMUX 联合回载顺序、endpoint 外部状态、异步总线时序和 timer-owned
  scheduler 仍未覆盖。下一步继续留在 STM32H723 层，审计带动态时间状态的 timer/ADC
  外设，之后再设计 machine-level save/load。

## 2026-09-01 STM32H723 timer component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 timer 状态层。producer 是共享 TIM2/辅助 timer 模型的寄存器
  镜像、活动 shadow 配置、计数相位锚点、更新/比较虚拟 deadline、重复计数、OCREF/Break
  状态和 batching policy；boundary 是 `dm_mc02_tim2_vmstate()`；consumer 是未来
  machine-level migration 组合及当前 timer/ADC/DMA 触发消费者。
- [x] VMState 版本为 1，保存 `regs`、active PSC/ARR/CCR/RCR、repetition 状态、虚拟时间
  锚点和 deadline、compare/OCREF/Break 状态以及 update/compare batch。QEMUTimer、Clock、IRQ、
  callback、缓存 interval、派生 IRQ validity 和静态 profile capability 保持运行时 wiring。
- [x] `dm_mc02_tim2_post_load()` 在 runtime sync 前拒绝越界 PSC/RCR、非法 repetition、
  compare/OCREF mask、不可表示 timestamp、deadline 早于 phase anchor、与当前活动输出
  配置不一致的 compare channel、禁用计时器携带 armed deadline，以及无 channel mask 的
  compare deadline；成功回载通过 `dm_mc02_tim2_sync_runtime()` 重挂虚拟 timer 并重投影
  level-sensitive IRQ，随后通知板级 `changed` consumer。已过期 deadline 只在目标当前
  虚拟时间重挂，不重放 wall-clock 事件循环。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_tim2.h`、`dm_mc02_tim2.c`、
  `dm_mc02_tim2_vmstate.c`、`hw/arm/meson.build`、`tests/unit/meson.build`、
  `tests/unit/test-dm-tim2-vmstate.c` 和 test-only runtime-sync stub。
- [x] 隔离门 `test-dm-tim2-vmstate` TAP `5/5` 通过，覆盖 round-trip、截断流、deadline
  顺序、非活动 compare channel 拒绝和 post-load consumer 通知；直接 consumer
  `dm-mc02-tim2-test` 22/22 通过，覆盖 TIM2/TIM8/TIM1 计数、更新、比较、中心对齐、预装载、
  Break、重复计数和互补输出行为。
- [x] 验证：`ninja -C build/qemu qemu-system-arm` 通过；本轮未修改 `trobot/`，审查代理已
  复核并关闭，无残留构建或测试进程。
- [ ] 限制与下一道门：Clock source/virtual-clock epoch 的完整迁移边界、timer/ADC/DMA
  联合回载顺序、外部 trigger consumer 状态和 machine-level save/load 仍未覆盖。当前
  timestamp 仍要求迁移组合提供一致的时钟语义；不能据此宣称跨时钟 epoch 的整机迁移。
  下一步继续在 STM32H723 层审计 ADC 动态转换/校准状态，不向板级或 UI 添加迁移 workaround。

## 2026-09-01 STM32H723 ADC dynamic VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 ADC 芯片状态层。producer 是 ADC CPU-visible register
  mirror、regular/injected 转换进度、校准/稳压器状态、JAUTO/JQM injected context
  snapshots、板级/外部 channel source；boundary 是 `dm_mc02_adc_vmstate()`；consumer
  是未来 machine-level migration 组合以及当前 ADC timer/IRQ/DMA/trigger 数据面。
- [x] VMState 版本为 1，保存 regular/injected rank cursor、剩余 half-cycle、绝对虚拟
  deadline、校准剩余周期、linear calibration window、regulator 状态、active/pending
  JSQR context、寄存器镜像和 source override 位图。`QEMUTimer`、DMA/DMAMUX 与 endpoint
  callback、IRQ handle、common-clock callback、当前 `clock_hz`、`power_model` 和
  `accurate_timing` 保持 runtime wiring/configuration，不进入状态流。
- [x] `dm_mc02_adc_post_load()` 在 runtime sync 前拒绝 regular/injected rank 越界、
  ADEN/ADSTART/JADSTART/ADCAL 不一致、AUTDLY/DISCEN/JDISCEN/JAUTO 非法等待态、
  active/pending JSQR 不一致、校准窗口越界、deadline/phase 倒序、不可表示时间和
  source override 位图不一致；成功回载通过 `dm_mc02_adc_sync_runtime()` 只重挂 ADC
  自有 timer 并重投影 level-sensitive IRQ。停止 kernel clock 时保留转换/校准剩余进度，
  不伪造 timer deadline；不调用 common-clock callback，也不重建 DMA/trigger wiring。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_adc.h`、`dm_mc02_adc.c`、
  `dm_mc02_adc_vmstate.c`、`hw/arm/meson.build`、`tests/unit/meson.build`、
  `tests/unit/test-dm-adc-vmstate.c`、`tests/unit/test-dm-adc-vmstate-stubs.c`。
  单测编译中的 `QEMU_VM_EOF` 头文件缺失和 32-bit `CFGR.CONT` 截断已在测试边界修复，
  没有改变 ADC production 行为。
- [x] 隔离门 `test-dm-adc-vmstate` TAP `3/3` 通过，覆盖完整动态状态 round-trip、
  不一致活动状态拒绝和截断流拒绝；直接 consumer `dm-mc02-adc-test` 为 `40/40`，
  覆盖 ADC1 地址窗口、电源、校准、时钟 source/改频、regular/injected、JAUTO/JQM、
  TIM1/2/3/8 trigger、JDR/DR 状态、队列和 IRQ。ADC analog/input/IRQ/DMA/power/
  trigger/JAUTO-DMA bare-metal smokes 也通过。
- [x] 验证：`ninja -C build/qemu qemu-system-arm`、QEMU smoke suite `89/89`、串行
  Host CTest `54/54` 均通过；QEMU 重链保留一个既有的上游
  `vhost_svq_poll()` `-Wmaybe-uninitialized` warning，不属于 ADC 变更。`trobot/` 未修改，
  无残留 QEMU/Ninja/CTest 进程。
- [ ] 限制与下一道门：该描述仍未注册到 DM-MC02 machine，不能宣称整机 snapshot/
  migration；ADC2/common shared model、模拟电气行为、深度
  injected FIFO 和 timer/ADC/DMA 联合回载顺序仍未覆盖。下一步继续在 STM32H723 层审计
  下一个独立动态外设或设计 machine-level save/load，不向板级或 UI 添加迁移 workaround。

## 2026-09-01 ADC paused-progress restore correction（已完成）

- [x] 所属层：可复用 STM32H723 ADC 芯片运行时恢复边界。producer 是停止 kernel clock
  时保留的 regular/injected half-cycle 或 calibration cycle 进度；boundary 是
  `dm_mc02_adc_sync_runtime()`；consumer 是 ADC 自有 virtual timer。
- [x] 修复首个错误：恢复状态的 deadline 为 0 时，原实现只删除 timer，未根据剩余进度
  重建 deadline；恢复到有效 ADC clock 后 active regular、injected 或 calibration 状态
  会永久停住。现在有效目标时钟下按 phase anchor 与剩余工作重建 deadline；目标仍为 0 Hz
  时保持暂停并刷新 phase anchor，避免把无时钟期间错误计入后续转换。
- [x] 新增 `test-dm-adc-runtime-sync`，实际执行 ADC VMState round-trip 和受控虚拟时钟：
  regular EOC `1667 ns`、injected JEOC `1667 ns`、calibration 完成 `3334 ns`；原有
  `test-dm-adc-vmstate` 继续覆盖非法/截断流和 post-load 副作用门。
- [x] 验证：两个 ADC VMState 单测目标通过；未修改 `trobot/`。
- [ ] 限制与下一道门：组件仍未注册 machine-level migration；绝对虚拟时间要求组合提供
  一致 Clock/epoch，DMA、trigger、common-clock wiring 和跨时钟 epoch 迁移仍未验证。

## 2026-09-01 STM32H723 ADC regular SQR2/SQR3/SQR4 sequence boundary（已完成）

- [x] 所属层：STM32H723 ADC 芯片层 regular sequence。producer 是 guest 对 SQR1/SQR2/
  SQR3/SQR4 的 register writes；boundary 是板卡无关的 sequence rank decoder；consumer 是
  ADC conversion timer、DR/EOC/EOS 以及已有 DMA/IRQ 数据面。
- [x] SQR1 解码 rank 1–4，SQR2 解码 rank 5–9，SQR3 解码 rank 10–14，SQR4 解码
  rank 15–16；字段保持 H723 的 6-bit stride 和 5-bit channel 值。四个 SQR 寄存器的
  full/half-word/byte MMIO 写都会选择真实 regular sequence，序列上限为 16 rank。
- [x] 原有 qtest `/dm-mc02/adc/regular-sequence-sqr2-sqr3` 继续验证 14 rank；新增
  `/dm-mc02/adc/regular-sequence-sqr4` 使用 16 rank 的确定性 channel sequence 验证
  SQR4 lane 写入、每个 DR 样本顺序和最终 EOS/ADSTART 状态。ADC qtest 为 `42/42`；
  ADC VMState `3/3`、runtime-sync `3/3`、QEMU smoke `89/89`、Host CTest `54/54`，
  QEMU 重链均通过。
- [x] 首次手动 qtest 失败是运行入口未设置 `QTEST_QEMU_BINARY`，不是模型失败；设置为
  当前 `build/qemu/qemu-system-arm` 后全量 qtest 通过。完整 smoke 中的 QEMU 重编译和
  上游通用头文件 warning 也未产生 ADC 相关错误。
- [ ] 限制与下一道门：ADC2/common shared state、完整模拟电气行为、深度 injected FIFO
  以及 timer/ADC/DMA 联合迁移顺序仍未验证。下一切片继续在 ADC 芯片层建立 ADC2/common
  的单一边界，不向 board/UI 添加 workaround。
## 2026-09-02 STM32H723 ADC12 common register boundary（已完成）

- [x] 所属层：可复用 STM32H723 ADC 芯片层。`DmMc02AdcCommon` 独立拥有 `CSR`、`CCR`、
  `CDR`、`CDR2` register block；ADC1/ADC2 通过 status callback 作为 producer，board
  clock policy 通过 CCR callback 作为直接 consumer。
- [x] CCR 合法位掩码为 DUAL/DELAY/DAMDF/CKMODE/PRESC/VREFEN/TSEN/VBATEN；full、half
  和 byte lane 写按 little-endian 合并，任意 CCR lane 写都会触发一次 clock callback。
  CSR 只读地组合 ADC1 status 的低半部和 ADC2 status 的高半部；CDR/CDR2 只读并提供
  `dm_mc02_adc_common_set_data()` staging API，尚未赋予 dual-mode 计算语义。
- [x] 机器只映射一个 common MMIO/状态实例，移除了 ADC 单体中旧的 offset-0x08 common
  兼容处理和 callback 字段，消除了 ADCx.CR 与 common CCR 的重复状态边界。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_adc_common.[ch]`、
  `dm_mc02_adc_common_vmstate.c`、`dm_mc02_adc.[ch]`、`dm_mc02.c`、
  `hw/arm/meson.build`、`tests/unit/meson.build`、`tests/unit/test-dm-adc-common.c`、
  `tests/unit/test-dm-adc-common-stubs.c` 和 `tests/qtest/dm-mc02-adc-test.c`。
- [x] 验证：`test-dm-adc-common` `4/4`、ADC VMState `3/3`、ADC runtime-sync `3/3`、
  `dm-mc02-adc-test` `45/45`，以及 `ninja ... qemu-system-arm` 通过。
- [ ] 限制与下一道门：dual-mode 同步转换、CDR/CDR2 packing、multimode DMA、common
  machine-level migration 和 timer/ADC/DMA 联合回载仍未支持；不得把当前 staging
  API 或 CSR 投影误报为完整 ADC12 multimode。

## 2026-09-02 ADC12 common regular-simultaneous CDR boundary（已完成）

- [x] 所属层：可复用 STM32H723 ADC 芯片层。producer 是 ADC1/ADC2 已完成的
  regular rank 样本事件；boundary 是 `DmMc02AdcCommon` 的有界样本配对器；consumer
  是 `ADC12_COMMON.CDR` 只读数据寄存器。板级 clock callback、ADC timer、DMA 和
  外部设备不进入该切片。
- [x] 公共事件携带 `sequence`、零基 `rank`、`value` 和单调虚拟
  `timestamp_ns`。common 只接受 `DUAL=0x6` regular simultaneous，并实现
  `DAMDF=2`（master `CDR[15:0]`、slave `CDR[31:16]`）和 `DAMDF=3`（两路低 8 位）；
  `CDR2` 与其它 DUAL/DAMDF 组合保持未支持并返回明确结果。
- [x] 每个 ADC 只有一个 pending slot。相同 sequence/rank/timestamp 的两路样本才
  更新 CDR；重复 producer 事件返回 `DUPLICATE` 并保留最新事件，失配返回
  `MISMATCH`、丢弃旧 peer 并保留新事件，避免无界队列或合成错误样本。DUAL/DAMDF
  改写和 reset 会清空 pending 状态。
- [x] common VMState 版本 1 保存 CDR、CCR 和两个 pending slot；post-load 拒绝非法
  source 状态、reserved CCR 位和 unsupported pending mode，再调用既有 clock consumer
  的 runtime sync。callback、owner、MemoryRegion 和 machine-level migration 仍不序列化。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_adc_common.[ch]`、
  `dm_mc02_adc_common_vmstate.c`、`dm_mc02_adc.[ch]`、`dm_mc02_adc.c`、`dm_mc02.c`、
  `tests/unit/test-dm-adc-common.c`、`tests/qtest/dm-mc02-adc-test.c` 及对应构建清单。
- [x] 隔离/边界门：`test-dm-adc-common` `7/7`，含 32/10-bit、8-bit、顺序无关配对、
  失配/重复/非法模式和 pending VMState；直接 qtest
  `/dm-mc02/adc/common-cdr-regular-simultaneous` 覆盖 ADC2 enable、ADC1 master 启动、
  CDR 读回和 reset。ADC qtest 全量 `47/47`，ADC VMState `3/3`，runtime-sync `3/3`。
- [x] 验证期间发现的首个错误是 `active-clock-change-remaining-rank` fixture 仍按
  1 MHz 计算复位时钟；producer 实际为 1.5 MHz。已将测试 oracle 改为 3000 ns 消耗
  9 个 half-cycle、切换后剩余 11 个 half-cycle/1375 ns，未改变生产模型。
- [ ] 限制与下一道门：尚未实现 ADC1/ADC2 外部触发的 master-only 同步语义、共享
  conversion ID、CDR2 alternate
  regular data、其它 dual modes、multimode DMA、CDR 读取对 EOC/overrun 的完整耦合，
  以及 common/ADC/timer/DMA 联合 machine-level migration。下一步仍先留在 ADC
  芯片层，逐项定义这些边界并补隔离测试，不向板级或 UI 添加 workaround。

## 2026-09-02 ADC12 regular-simultaneous master/slave software-start boundary（已完成）

- [x] 所属层：可复用 STM32H723 ADC 芯片层的 ADC1/ADC2 common start boundary。
  producer 是 ADCx.CR 的 regular `ADSTART` 请求；boundary 是
  `DmMc02AdcCommonRegularStartPeer` admission callback；consumer 是 ADC1 master、ADC2
  slave 的 regular conversion kernels。
- [x] 依据 RM0468 与 STM32H7 HAL：在 `CCR.DUAL=0x6` regular simultaneous 下，ADC1
  master 的 software `ADSTART` 通过 common callback 启动已启用的 ADC2；ADC2 的独立
  `ADSTART` 被拒绝并清零。其它 DUAL 值保留独立 ADC 的原有准入行为。该边界不把
  board wiring、ADC timers 或 DMA 状态放入 common 组件。
- [x] reset 保留 common-to-peer、ADC regular-sample 和 clock runtime wiring；测试明确
  覆盖 reset 后再次执行 HAL 风格“先 enable ADC2、只启动 ADC1”。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_adc_common.[ch]`、`dm_mc02_adc.[ch]`、
  `dm_mc02.c`、`tests/unit/test-dm-adc-common.c` 和 `tests/qtest/dm-mc02-adc-test.c`。
- [x] 隔离/边界门：`test-dm-adc-common` `7/7`；
  `/dm-mc02/adc/common-master-start-drives-slave`；ADC qtest 全量 `47/47`；本轮窄目标
  `ninja` 重链通过。负向 VMState 用例会按预期输出 malformed-stream load error，进程
  退出码为 0。
- [ ] 限制与下一道门：当前只覆盖 software master start。外部 timer trigger 仍会由
  两个 ADC 各自消费，尚未收敛为 master-only 触发；样本配对仍依赖完全相等的虚拟时间，
  下一步应先在 common 层引入共享 `conversion_id`，再处理外部触发和 multimode DMA。

## 2026-09-02 ADC12 shared conversion ID pairing（已完成）

- [x] 所属层：STM32H723 ADC common/ADC regular data boundary。producer 是 common 的
  software master-start admission，boundary 是 `DmMc02AdcRegularSample.conversion_id`
  和 `DmMc02AdcCommon` 的 bounded matcher，consumer 是 ADC12 `CDR` packing。独立 ADC
  与外部触发样本使用 `conversion_id == 0`，不会误入 common matcher。
- [x] common 每次 master 软件启动分配非零、递增的 `next_conversion_id`，通过公共
  `DmMc02AdcCommonRegularStartPeer` callback 传给 DM-MC02 composition；composition 使用
  公共 ADC setter 将同一个 ID seed 到 ADC1/ADC2，连续模式从同一 seed 继续递增。
- [x] matcher 现在按 `conversion_id + rank` 配对，不再要求两个 ADC 的虚拟时间戳完全相等；
  timestamp 仍保留在 pending state 作为诊断字段。ID 为零、rank 越界和不支持的
  DUAL/DAMDF 组合均在边界返回明确结果。
- [x] common VMState 升级到 v2，保存 `next_conversion_id`；ADC VMState 升级到 v2，保存
  `regular_shared_conversion`。v1 common/ADC streams 仍可加载；common v1 回载会从 legacy
  pending ID 重建 generator frontier，避免新旧 sequence 重号。
  common reset 清除 ID generator 和 pending pair，但保留所有 runtime wiring。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_adc_common.[ch]`、
  `dm_mc02_adc_common_vmstate.c`、`dm_mc02_adc.[ch]`、`dm_mc02_adc.c`、`dm_mc02_adc_vmstate.c`、
  `dm_mc02.c`、`tests/unit/test-dm-adc-common.c` 和 `tests/unit/test-dm-adc-vmstate.c`。
- [x] 隔离门：`test-dm-adc-common` `8/8`（含 ID 递增、zero-ID 拒绝、不同 timestamp
  配对、reset 和显式 v1 stream）；`test-dm-adc-vmstate` `3/3`。
- [x] 直接边界门：`QTEST_QEMU_BINARY=build/qemu/qemu-system-arm`
  `dm-mc02-adc-test` `47/47`；最窄 QEMU targets（两个 unit、ADC qtest 和
  `qemu-system-arm`）均完成重链。未修改 `trobot/`。
- [ ] 限制与下一道门：尚未实现外部 trigger 的 master-only common admission、CDR2 alternate
  data、其它 dual modes、multimode DMA、CDR read side effects 或 machine-level migration。
  下一道门仍留在 STM32H723 ADC 层，先定义外部触发共享 ID/事件所有权和对应隔离测试。

## 2026-09-02 ADC12 regular-simultaneous external-trigger ownership（已完成）

- [x] 所属层：STM32H723 ADC common/regular-trigger 数据边界。producer 是共享 trigger
  bus 的带 source、edge、event-count 和虚拟时间戳事件；boundary 是 common 的
  `DmMc02AdcCommonExternalTriggerPeer` admission 与非零 `conversion_id` 分配；consumer
  是 ADC1 master、ADC2 slave 的 regular conversion kernels 和 CDR matcher。
- [x] 在 `CCR.DUAL=0x6` 下只有 ADC1/master 可以接收 regular 外部触发；ADC2 的独立
  trigger sink 被拒绝。common 检查并透传触发源、边沿、事件数量和虚拟时间戳，确认
  composition 能接收已布防的 ADC2 后分配同一共享 ID，并通过显式 peer API 启动 ADC2。
- [x] 在非 simultaneous DUAL 模式下保留 ADC-local 外部触发路径，ID 保持为零；injected
  外部触发不进入 common regular admission。批量边沿在当前 compact ADC 模型中折叠为一次
  regular sequence，精确逐边沿队列仍是后续时序边界。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_adc_common.[ch]`、`dm_mc02_adc.[ch]`、
  `dm_mc02.c`、`tests/unit/test-dm-adc-common.c` 和 `tests/qtest/dm-mc02-adc-test.c`。
- [x] 隔离门：`test-dm-adc-common` `9/9`，覆盖 source/edge/event-count/timestamp
  透传、master-only admission、peer 失败和 ID 不泄漏；直接边界
  `/dm-mc02/adc/common-external-trigger-drives-slave` 覆盖 TIM8 触发、双 ADC EOC、
  CDR 读回、master-only 负向路径和 reset 后状态。
- [x] 验证：`test-dm-adc-vmstate` `3/3`、`test-dm-adc-runtime-sync` `3/3`、ADC qtest
  `48/48`，QEMU smoke suite `89/89`，串行 Host CTest `54/54`；
  本轮未修改 `trobot/`，未留下活跃子代理或运行进程。
- [ ] 限制与下一道门：CDR2 alternate regular data、其它 dual modes、multimode DMA、
  CDR read side effects，以及 common/ADC/timer/DMA 联合 machine-level migration 仍未支持。
  下一步继续留在 ADC 芯片层，先定义并隔离 CDR2 或 multimode DMA 的单一 producer/consumer
  边界，不能用 board/UI workaround 代替。
# 2026-09-02 STM32H723 SPI component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 SPI 数据层。producer 是 SPI 配置寄存器、当前帧进度、
  RX 单槽、EOT 和 deferred TX-DMA 的绝对虚拟 deadline；boundary 是
  `dm_mc02_spi_vmstate()`；consumer 是 SPI 自身 TX-DMA scheduler 与未来的
  machine-level migration 组合。
- [x] 新增 `dma_tx_next_ns`，所有 SPI TX-DMA 延期路径都通过统一 helper 维护该
  `QEMU_CLOCK_VIRTUAL` deadline；`dm_mc02_spi_sync_runtime()` 在回载后清除递归保护，
  取消目标 timer，并按目标虚拟时间重挂未过期/已过期 deadline。
- [x] VMState 版本 1 只保存 `CR1/CR2/CFG1/CFG2`、传输剩余量、RX 数据/有效位、EOT
  和 deadline。target callback、GPIO-derived selected mask、DMA/DMAMUX/endpoint
  wiring、timer identity、endpoint mode、batch policy 和递归 guard 均明确排除。
- [x] 隔离门 `test-dm-spi-vmstate` `4/4`：动态状态 round-trip、16-bit transfer
  count 边界、不可表示 virtual deadline 和截断流均通过，拒绝路径不触发 sync。
- [x] 直接 consumer 门 `run-bmi088-smoke.sh`、`run-spi2-dma-smoke.sh on`、
  `run-spi2-dma-smoke.sh off` 通过；`ninja -C build/qemu qemu-system-arm` 重链通过。
  本轮未修改 `trobot/`。
- [ ] 限制与下一道门：SPI target（BMI088）内部状态、DMA/DMAMUX 联合回载顺序、GPIO
  片选回载顺序、真实 SPI bit-level/electrical timing 和 machine-level migration 仍未
  支持。下一步继续在 STM32H723 层选择另一个明确动态外设边界，不向板级/UI 添加 workaround。

# 2026-09-02 STM32H723 USART component VMState contract（已完成）

- [x] 所属层：可复用 STM32H723 USART 数据层。producer 是 USART 寄存器镜像、RX
  wire/CPU FIFO、TX FIFO 与统计计数；boundary 是 `dm_mc02_uart_vmstate()`；consumer
  是 USART 自身的虚拟时间 RX/IDLE、paced TX、DMA-TX continuation，以及未来的
  machine-level migration 组合。
- [x] 保存四个绝对 `QEMU_CLOCK_VIRTUAL` deadline：RX wire delivery、post-frame IDLE、
  paced host TX、bounded DMA TX retry；保存前述 FIFO/cursor 和计数器。post-load 在
  触发 runtime sync 前拒绝非法 cursor、超出 `INT64_MAX` 的 deadline 以及 deadline/FIFO
  不一致，防止坏状态污染 timer/wiring。
- [x] chardev、Clock、DMA/DMAMUX、IRQ、QEMUTimer、endpoint callback、RS485/DE/供电
  状态、endpoint mode、DMA started marker 和派生 baud timing 均保持 runtime-only。
  `dm_mc02_uart_sync_runtime()` 重新计算 baud timing，按保存的绝对 deadline 重建目标
  timer；过期 deadline 以当前虚拟时间继续执行，不使用 wall-clock。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_uart.h`、`dm_mc02_uart.c`、
  `dm_mc02_uart_vmstate.c`、`hw/arm/meson.build`、`tests/unit/meson.build`、
  `tests/unit/test-dm-uart-vmstate.c` 和 `test-dm-uart-vmstate-stubs.c`；未修改
  `trobot/`。
- [x] 隔离门 `test-dm-uart-vmstate` `5/5`；直接 UART polling、IDLE、TX/RX virtual-time、
  USART1/USART2 DMA endpoint/MMIO on/off 共 `8/8`；`qemu-system-arm` 增量重链通过。
- [ ] 限制与下一道门：该描述仍未注册到 DM-MC02 machine，不能宣称整机 snapshot/
  migration；chardev reconnect/慢后端 partial write、UART/DMA/DMAMUX 联合回载顺序、
  真实串行位级/电气时序仍未覆盖。下一步继续留在 STM32H723 层，选择另一个独立动态
  状态边界，先完成隔离与直接 consumer 门。

# 2026-09-02 BMI088 SPI framing adapter VMState contract（已完成）

- [x] 所属层：可复用器件/适配器层。producer 是单颗 BMI088 SPI framer 的命令方向、
  dummy 阶段和寄存器/FIFO 游标；boundary 是 `dm_mc02_bmi088_spi_vmstate()`；consumer
  是 SPI target callback 与 BMI088 读写数据面。
- [x] VMState v1 只保存 `command_seen`、`read_transfer`、`dummy_pending`、当前
  `reg` 和 `read_start_reg`。BMI088 指针、消费 callback 及 opaque 是目的端 runtime
  wiring，不进入状态流；传感器寄存器、FIFO、噪声/零漂/filter 状态不在本切片伪装成
  已覆盖的迁移状态。
- [x] post-load 在任何 runtime wiring 使用前拒绝版本错误、无 command 却带游标/模式、
  以及写事务携带 dummy 状态；合法读/写事务保留 framing 状态和目的端 runtime sentinel。
- [x] 隔离门 `test-dm-bmi088-spi-vmstate` `5/5`；直接 `run-bmi088-smoke.sh` 和
  `run-cosim-link-smoke.sh` 通过；`ninja -C build/qemu qemu-system-arm` 重链通过。
  本切片未修改 `trobot/`，已完成的子代理已复核并关闭。
- [ ] 限制与下一道门：这只是 SPI 事务适配器状态契约，不是 BMI088 传感器或整机
  snapshot/migration 支持。下一步应单独审计 BMI088 寄存器、FIFO、采样序列、噪声/零漂
  与温度状态的 VMState 及其直接读取回归，再处理 SPI target 与 GPIO/DMAMUX 联合回载顺序。

# 2026-09-02 BMI088 sensor component VMState contract（已完成）

- [x] 所属层：可复用器件层。producer 是 BMI088 寄存器镜像、信号噪声/零偏/温度/滤波
  状态、采样 ODR 时间状态、FIFO 字节/序列和陀螺 DRDY deadline；boundary 是
  `dm_mc02_bmi088_vmstate()`；consumer 是未来 machine-level migration 组合和当前
  BMI088 SPI target 数据面。
- [x] VMState v1 保存完整动态传感器状态：256 字节寄存器、信号模型参数与历史、RNG、
  ODR/采样时间、FIFO 数据和 sequence、FIFO 读游标/帧状态、sensor-time/overrun、sample
  sequence 以及 gyro DRDY 清除 deadline。`double` 以固定大端 IEEE-754 bit pattern 序列化。
- [x] `accel` die identity 与 `signal.kind` 是目的端静态配置，不进入状态流；post-load
  校验目的端 identity/kind 一致，并在 runtime wiring 使用前拒绝非法 signal、FIFO 游标、
  frame 状态或数值状态。该策略避免把目标板上的加速度计/陀螺仪身份伪装成可迁移数据。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_bmi088.h`、
  `qemu/upstream/hw/arm/dm_mc02_bmi088_vmstate.c`、
  `qemu/upstream/hw/arm/meson.build`、`qemu/upstream/tests/unit/meson.build` 和
  `qemu/upstream/tests/unit/test-dm-bmi088-vmstate.c`；未修改 `trobot/`。
- [x] 隔离门 `test-dm-bmi088-vmstate` `5/5`，覆盖加速度计/陀螺仪动态状态 round-trip、
  目的端非法/不匹配 kind、非法 FIFO cursor 和截断流。直接 `run-bmi088-smoke.sh`、
  `run-bmi088-fifo-smoke.sh`、`run-bmi088-drift-smoke.sh`、`run-bmi088-filter-smoke.sh`
  通过；`ninja -C build/qemu qemu-system-arm` 通过。
- [ ] 限制与下一道门：该描述仍未注册到 DM-MC02 machine，不能宣称整机 snapshot/
  migration；SPI framer、GPIO 片选、DMA/DMAMUX 和 BMI088 联合回载顺序仍未验证，
  也未覆盖真实 SPI 位级/电气时序。下一步继续定义 SPI target 与传感器组件的联合状态边界，
  先完成隔离与直接消费者门，再考虑板级组合。

# 2026-09-02 BMI088 SPI link composite state boundary（已完成）

- [x] 所属层：可复用器件/STM32H723 SPI 组合层到 DM-MC02 board consumer。新增
  `DmMc02Bmi088SpiLink`，统一拥有 SPI2、加速度计/陀螺仪 die 和两个 SPI framer；
  machine 的初始化、复位、BMI088 访问、DMA 配置和 GPIO 片选投影均通过该组合对象，
  消除了 board 层重复拼装。
- [x] 新增 `dm_mc02_spi_vmstate_raw()` 和无回调
  `dm_mc02_spi_restore_selected_mask()`。组合 VMState v1 保存 SPI 原始动态字段、两颗
  BMI088、两个 framer 和 GPIO 派生片选快照；raw SPI 不在子状态加载时重挂 timer，父级
  先完成 die/framer 校验，再无副作用恢复片选，最后激活 SPI DMA continuation。
- [x] 组合层额外校验固定 accel/gyro slot、`signal.kind` 和 framer->die 目的端指针，避免
  单颗 BMI088 VMState 的静态身份约束在组合边界丢失。片选转换仍通过正常回调结束事务，
  只有 snapshot restore 使用无回调路径。
- [x] 变更文件：`hw/arm/dm_mc02_spi.[ch]`、`dm_mc02_spi_vmstate.c`、
  `dm_mc02_bmi088_spi_link.[ch]`、`dm_mc02_bmi088_spi_link_vmstate.c`、
  `dm_mc02.c`、构建登记和 `test-dm-bmi088-spi-link-vmstate*`；未修改 `trobot/`。
- [x] 隔离门 `test-dm-bmi088-spi-link-vmstate` `4/4`；SPI/BMI088 既有 VMState
  `4/4 + 5/5 + 5/5`，BMI088 polled/FIFO/drift/filter 和 SPI DMA endpoint on/off
  直接门均通过，`qemu-system-arm` 重链通过。
- [ ] 限制与下一道门：组合 VMState 尚未注册到 DM-MC02 machine，不能宣称整机
  snapshot/migration；GPIO 输入、DMA/DMAMUX、CPU/IRQ、片上 RAM、co-sim 队列和
  machine-level 的完整回载顺序仍需逐项建立状态契约。真实 SPI 位级/电气时序也未覆盖。

## 2026-09-02 STM32H723 DMA/DMAMUX subsystem state boundary（已完成）

- [x] 所属层：可复用 STM32H723 芯片层。producer 是两个 DMA stream controller 的寄存器、
  reload/live cursor/FIFO 状态和两个 DMAMUX 窗口的寄存器/generation；boundary 是
  `dm_mc02_dma_subsystem_vmstate()`；consumer 是 DMA request lookup、stream arbitration、
  FIFO 和 level-sensitive IRQ projection。
- [x] 新增 `DmMc02DmaSubsystem`，组合两个 DMA 与两个 DMAMUX；当前 H723 profile 中 DMA1/2
  共同消费 DMAMUX1 的 0/8 通道偏移，DMAMUX2 作为独立窗口保留。raw DMA VMState 与普通
  描述共享字段表，组合恢复不会提前重建 request cache。
- [x] 组合 post-load 在所有子字段装载后复用 `dm_mc02_dma_state_valid()`，验证两个 DMA
  的 FIFO/count，再按顺序重建两路 DMA request cache 和 IRQ projection。MemoryRegion、
  channel offset、callback、IRQ handle、request cache 和 endpoint wiring 都是 runtime-only。
- [x] 隔离门 `test-dm-dma-subsystem-vmstate` `4/4`；既有 DMA VMState `3/3`；DMA 三个
  直接 smoke 与 `qemu-system-arm` 重链通过。首次链接失败是测试 target 同时包含两个
  `main`，已删除错误的重复源文件。
- [ ] 限制与下一道门：未注册 machine-level VMState，不能宣称整机 snapshot/migration；
  endpoint、SPI/UART/ADC timer、CPU/IRQ、RAM、co-sim 队列及其联合恢复顺序仍未完成，真实
  DMA 总线仲裁/物理传输时序也未建模。

# 2026-09-02 DMA endpoint non-fatal backpressure boundary（已完成）

- [x] 所属层：可复用 STM32H723 DMA 芯片层到板卡无关 endpoint 边界。producer 是外设
  endpoint 的单 beat callback；boundary 是 `DmMc02DmaEndpointResult`；consumer 是
  DMA direct/FIFO stream 的 `NDTR`、live cursor、FIFO 和错误/完成状态。
- [x] 新增可选 `read_ex`/`write_ex` callback。`ACCEPTED` 提交一个 beat，`RETRY` 表示
  endpoint 尚未接受且不得改变 endpoint 状态，DMA 保留 stream、地址、NDTR、FIFO 和
  TEIF，调用方负责稍后重试；`ERROR` 延续原有 TEIF/停 stream 路径。旧 bool callback
  保持兼容，`false` 映射为 `ERROR`。
- [x] bounded endpoint batch 在第一个 `RETRY` 处停止，避免在一个虚拟事件中忙循环。该
  结果是 caller-driven backpressure，不创建隐藏 timer/worker/无界队列，也不声称已经
  支持异步 transport 或 legacy P2M callback 的消费回滚。
- [x] 变更文件：`hw/arm/dm_mc02_dma_endpoint.[ch]`、`hw/arm/dm_mc02_dma.c`、
  `tests/dma_endpoint_smoke.c`、`tests/dma_fifo_dbm_endpoint_smoke.c` 以及本项目契约文档；
  未修改 `trobot/`。
- [x] 隔离/芯片层门：`dm_mc02_dma_endpoint_smoke`、
  `dm_mc02_dma_fifo_dbm_endpoint_smoke` 均通过；后者覆盖 direct/FIFO 的 M2P 与 P2M
  retry，验证 `NDTR`、live cursor、FIFO、guest memory、EN/TEIF 和后续重试提交；QEMU
  DMA/DMAMUX VMState 单测和 `qemu-system-arm` 重链通过。
- [ ] 历史限制与下一道门：在该 `read_ex/write_ex` 背压切片完成时，ADC/SPI/OCTOSPI
  和 UART RX 仍使用同步 legacy callback（历史状态；后续 ADC1、OCTOSPI 与 UART RX
  已分别完成 direct P2M reservation，SPI RX 也已完成该切片）。其余 endpoint 的 backpressure virtual-time owner、peripheral
  transaction rollback、精确 DMA 仲裁和 machine-level migration 仍未实现，必须继续逐个
  建立边界，不能把 retry 结果直接扩展为上层 UI 或整机迁移支持。

# 2026-09-02 UART TX DMA backpressure direct boundary（已完成）

- [x] 所属层：STM32H723 UART 数据面到可复用 DMA endpoint 的直接 consumer 边界。
  producer 是 UART TX/TDR endpoint，boundary 是 `write_ex` 返回的
  `DM_MC02_DMA_ENDPOINT_RETRY`，consumer 是 UART TX FIFO、DMA stream cursor/NDTR 和
  host-facing chardev。
- [x] UART TX FIFO 满时只返回 `RETRY`，不写 TDR、不丢字节、不推进 DMA、不置 TEIF；
  DMA FIFO M2P 在 callback 前的新增内存预取也在 retry 时回滚，避免 FIFO 与内存游标
  不一致导致后续重复字节。UART 自有 virtual timer 在 DMA 有进展时分批续传；FIFO
  drain、chardev reopen 和 DMA 配置路径显式 kick pending stream，FIFO 满时不忙轮询。
- [x] 新增直接 QEMU qtest `tests/qtest/dm-mc02-uart-test.c`，使用真实 UART1、DMA1
  Stream1、DMAMUX1 channel1 和 ringbuf chardev，断言 FIFO 满后的 NDTR/PAR/M0AR/EN/TEIF
  保持，并校验 drain 后完整字节序列无丢失；加入 `tests/qtest/meson.build`。
- [x] 修复并扩展 `tests/dma_fifo_dbm_endpoint_smoke.c`，覆盖已有 FIFO 内容与新预取
  同时存在时的 retry/恢复序列；普通和 ASan host smoke 均通过。
- [x] 验证：`test-dm-uart-vmstate` `5/5`、`dm-mc02-uart-test` `1/1`、
  `run-uart-dma-smoke.sh on|off`、`run-uart2-dma-smoke.sh on|off` 通过；
  `qemu-system-arm` 增量重链通过。
- [ ] 限制与下一道门：当前仍不提供整机 migration、真实串行位级/电气时序或完整异步
  chardev 恢复；UART RX endpoint、ADC/SPI/OCTOSPI 的异步 backpressure 和跨外设联合
  restore 仍需各自的下层边界，不能由本 qtest 推广为整机支持。

# 2026-09-02 STM32H723 PWR/RCC component VMState contract（已完成）

- [x] This slice stays at the STM32H723 chip layer. The producer is the PWR/RCC
  register mirrors plus the effective system-clock source and ADC clock phase;
  the boundary is `dm_mc02_pwr_rcc_vmstate()`; the consumer is a future clock/
  peripheral composition restore path.
- [x] Version 1 serializes `pwr_regs[0x400]`, `rcc_regs[0x400]`,
  `system_clock_source`, and `adc_clock_configured`. `MemoryRegion`, clock
  callback, callback opaque, and board/QOM wiring remain destination-owned
  runtime state.
- [x] The ordinary description validates the complete restored state and calls
  `dm_mc02_pwr_rcc_sync_runtime()` exactly once. The raw description has no
  side effect and is available for a parent composite with an explicit restore
  order. Invalid source and truncated streams do not notify the consumer.
- [x] Changed `hw/arm/dm_mc02_pwr_rcc.[ch]`,
  `dm_mc02_pwr_rcc_vmstate.c`, ARM/unit build lists, the focused VMState test,
  and its narrow-link MemoryRegion stub. `trobot/` was not modified.
- [x] The isolated gate `test-dm-pwr-rcc-vmstate` is `4/4`. PWR/RCC, zero
  effective-clock, TIM2 dynamic-clock, USART/FDCAN kernel-clock, alternate
  profile, QEMU smoke `89/89`, Host CTest `54/54`, and the `qemu-system-arm`
  relink all pass.
- [ ] This remains a component contract and is not registered with the
  DM-MC02 machine, so it does not establish whole-machine snapshot/migration.
  PLL/oscillator settling, complete H723 PWR/RCC bit semantics, and joint
  PWR/RCC restore ordering with DMA, timers, ADC, UART and FDCAN remain future
  lower-layer boundaries.

# 2026-09-02 USB control-core VMState invariant tightening（已完成）

- [x] 所属层：可复用 USB control-transfer core。producer 是
  `DmUsbControlDevice` 的 setup、数据和 status 状态；boundary 是
  `dm_usb_control_vmstate()`；consumer 是未来 DWC2/USB composite restore。
- [x] post-load 现在拒绝 `DATA_IN` 中超过 setup `wLength` 的 payload、无数据且无
  ZLP 的不可继续状态，以及 phase 不匹配或条件不成立的 ZLP。ZLP 仅允许在
  `DATA_IN`，并要求 `data_length < request.length` 且按 `max_packet_size` 整包结束。
- [x] `DATA_OUT` 必须是 OUT request、非零 `wLength`、接收期间 `data_length == 0`；
  `STATUS_OUT` 必须来自已消费完的 IN transfer；`STATUS_IN` 必须是合法 pending
  standard request 或已完成的 class OUT transfer。
- [x] pending address/configuration 分别绑定 `SET_ADDRESS`/
  `SET_CONFIGURATION` 和 request `wValue`，并要求 status payload 为空；STALLED
  明确保留错误前的部分 OUT cursor，但不保留活动 pending/ZLP。
- [x] 变更文件：`qemu/upstream/hw/usb/dm_usb_control_vmstate.c`、
  `qemu/upstream/tests/unit/test-dm-usb-control-vmstate.c`；未修改 `trobot/`。
- [x] 隔离验证：`test-dm-usb-control-vmstate` `6/6`；受影响 control/DWC2/adapter
  unit、DM-MC02 USB qtest 和 `qemu-system-arm` 目标重链通过。
- [ ] 限制与下一道门：组件状态尚未注册到 DM-MC02 machine，不能宣称整机
  snapshot/migration；DWC2 control 联合回载、USB bus/PHY/DMA/SOF 和跨组件顺序仍待
  单独定义和验证。

# 2026-09-02 QEMU USB host transport binding boundary（已完成）

- [x] 所属层：可复用 USB host transport 到 QEMU `USBBus`/`USBPort`/`USBPacket`
  边界。producer 是 generic `DmUsbTransaction`，boundary 是 QEMU 标准 USB 对象的
  同步 packet 映射，consumer 是 H723 host-channel transport；当前 DM-MC02 仍保持
  USB device role，不接入该 host fixture。
- [x] 明确 borrowed ownership：direct `USBDevice` 与 routed `USBPort` 绑定互斥；新增
  `dm_usb_host_qemu_transport_clear()`，host composition 在释放 port/bus 前断开 route
  并清除借用指针；port cleanup 后清空 host/bus，未注册或无设备 reset 不触发 QEMU assert。
- [x] transport 保留 `NODEV`、`BABBLE`、`IOERROR` 和 `ASYNC/ADD_TO_QUEUE` 的结果语义；
  deferred packet 会在栈 packet/caller buffer 离开前取消，映射为
  `DM_USB_TRANSACTION_DEFERRED`。新增 binding/null/clear 回归，直接 QEMU bus consumer
  测试共 `15/15`。
- [x] 验证：`ninja -C build/qemu tests/unit/test-dm-usb-qemu-adapter`、对应 `--tap`
  `15/15`、`ninja -C build/qemu qemu-system-arm` 均通过；未修改 `trobot/`。
- [ ] 限制与下一道门：QEMU `USBDeviceClass.handle_control` 只有 request-level callback，
  不能无依据宣称 device-side adapter 已逐 status packet 映射；control 双状态机仍需新的
  request boundary。错误细分向 H723 controller 的完整传播、USB PHY/VBUS、拓扑、
  isochronous/streams 和 DM-MC02 host profile 仍待独立切片。

# 2026-09-02 QEMU request-level control bridge（已完成）

- [x] 所属层：可复用 USB control core 到 QEMU `USBDeviceClass.handle_control`
  的直接边界。producer 是 QEMU 提供的完整 request-level 参数和 data buffer；boundary
  是 `DmUsbQemuControlSubmit`；consumer 是板卡无关的 `DmUsbControlDevice` 或其它同步
  control request backend。
- [x] 新增 `dm_usb_control_execute_request()`，以一个完整 request 驱动既有 control
  state machine 的 SETUP、数据和 status 阶段；IN 聚合 payload，OUT 要求完整 data
  stage，成功后回到 IDLE。该 helper 不增加第二套 control state machine。
- [x] `DmUsbQemuAdapter` 新增必需的 request-level control callback。QEMU adapter 不再
  从 `handle_control` 伪造下层 SETUP/DATA/STATUS transaction；bulk/data 仍使用原有
  `DmUsbTransaction` 边界。回调只同步借用 QEMU data buffer，重入和方向/长度违规被拒绝。
- [x] 变更文件：`qemu/upstream/hw/usb/dm_usb_control.[ch]`、
  `dm_usb_qemu_adapter.[ch]`、`tests/unit/test-dm-usb-control.c`、
  `tests/unit/test-dm-usb-qemu-adapter.c` 以及 `INTERFACES.md`/`ARCHITECTURE.md`。
  未修改 `trobot/`。
- [x] 隔离门：control core `7/7`；直接 QEMU bus consumer `15/15`，覆盖多包 IN、class
  OUT、零长度 `SET_ADDRESS`、地址路由、DWC2 bulk bridge 和 callback 重入保护。
- [x] 限制与下一道门：该边界仍是同步 request-level 适配器，不提供 QEMU async control
  completion、逐 token lower transport、USB PHY/SOF 或 DM-MC02 machine-level migration。

# 2026-09-02 DWC2/control composite VMState boundary（已完成）

- [x] 所属层：可复用 USB control core 与 DWC2 device-mode 芯片层的联合边界。producer
  分别是 control core 的活动 request/data/status 游标和 DWC2 的寄存器、端点 FIFO、传输
  计数及 PID 状态；boundary 是 `DmUsbDwc2ControlLink` 与
  `dm_usb_dwc2_control_link_vmstate()`；consumer 是 DWC2 token 数据面及同步 QEMU
  request-level control consumer。
- [x] 新增 raw child 描述和无副作用校验入口：`dm_usb_control_vmstate_raw()`、
  `dm_usb_dwc2_vmstate_raw()`、`dm_usb_control_state_valid()`、
  `dm_usb_dwc2_state_valid()`。联合流按 `control -> DWC2 raw` 加载，parent 在所有字段
  有效后校验 EP0/MPS 和目的端 control 指针，再只调用一次
  `dm_usb_dwc2_sync_runtime()`；callback、opaque、IRQ、QOM/MemoryRegion 均不进入流。
- [x] 联合 wire 增加不同的固定 child marker，交换子状态、截断流、非法 control phase
  或非法 FIFO 在 runtime sync 前拒绝。VMState 普通字段在错误返回前可能已经写入，仍不
  提供事务级 rollback。
- [x] 变更文件：`hw/usb/dm_usb_control.[ch]`、
  `dm_usb_control_vmstate.c`、`dm_usb_dwc2_device.[ch]`、
  `dm_usb_dwc2_device_vmstate.c`、新增 `dm_usb_dwc2_control.[ch]`、联合 VMState 源、
  USB/unit 构建清单和联合测试；未修改 `trobot/`。
- [x] 隔离及直接消费者门：`test-dm-usb-dwc2-control-link-vmstate` `7/7`，覆盖
  DATA_IN 中途继续、SET_ADDRESS pending、端点 FIFO 同时恢复、request-level consumer、
  非法 phase/FIFO、错误 child 顺序、截断和版本；原有 control `6/6 + 7/7`、DWC2
  `4/4 + 7/7` 通过。
- [x] 完整验证：16 个 USB unit、DM-MC02 USB qtest `10/10`、H723 USB host qtest
  `4/4`、Host CTest `54/54` 和 `qemu-system-arm` 重链均通过；负向 VMState 用例中的
  版本/marker/FIFO 错误输出均为预期拒绝路径。
- [ ] 限制与下一道门：该 composite 仍是可复用 component contract，尚未注册到
  DM-MC02 machine；USB bus/PHY/SOF、DMA、CPU/IRQ/NVIC、RAM、QEMU adapter 对象生命周期
  和 co-sim 队列的联合恢复仍未实现。下一步继续选择一个相邻的底层联合状态边界，不能
  由此宣称整机 snapshot/migration。

# 2026-09-02 STM32H723 ADC1/ADC2 + ADC12_COMMON composite VMState boundary（已完成）

- [x] 所属层：STM32H723 ADC 芯片层。producer 是 ADC12 common 的共享时钟/模式、CDR
  pending 数据和 ADC1/ADC2 的寄存器及 regular scheduler 状态；boundary 是新增的
  `DmMc02AdcPair` 与 `dm_mc02_adc_pair_vmstate()`；consumer 是未来的 SoC/board
  composite restore。未注册 machine-level migration，未修改 `trobot/`。
- [x] 联合流固定为 `ADC12_COMMON raw -> ADC1 raw -> ADC2 raw`。raw child 只验证字段，
  不重建 timer、clock callback、DMA、IRQ 或其它 runtime wiring；parent 在所有字段有效后
  按 common -> ADC1 -> ADC2 顺序执行一次 common clock projection 和两次 ADC scheduler/
  IRQ projection。
- [x] parent 验证 common pending sample 的 conversion ID 与对应 ADC active sequence ID
  一致，并拒绝不支持的 common/ADC 状态或截断流；普通字段在错误返回前可能已写入，仍不
  提供事务级 rollback。
- [x] 修复 ADC VMState validator 的根因错误：此前将非法条件以逻辑或返回，合法状态会被
  判为无效；现在要求 calibration window、输入 override、injected context、sequence
  timing 等约束全部满足。该修复同时恢复 ADC 单体和 pair 的合法 round-trip。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_adc_vmstate.c`、ADC/common 头与 VMState
  文件、新增 `dm_mc02_adc_pair.[ch]` VMState、ARM/unit Meson 清单和 pair focused test
  及 test-only stubs；未修改 `trobot/`。
- [x] 隔离门：`test-dm-adc-vmstate` `3/3`、`test-dm-adc-common` `15/15`、
  `test-dm-adc-pair-vmstate` `3/3`；直接 `dm-mc02-adc-test` `62/62`，ADC 相关 smoke
  全部通过。完整 QEMU smoke suite `89/89`、Host CTest `54/54` 和
  `qemu-system-arm` 重链通过。
- [ ] 限制与下一道门：pair composite 尚未接入 DM-MC02 machine-level VMState；DMA/
  DMAMUX、CPU/IRQ/NVIC、RAM、board power、trigger bus、external bus/co-sim queue 及
  machine-level 的联合恢复顺序仍需逐项建立。当前结果不代表整机 snapshot/migration、
  精确 DMA 仲裁或所有 ADC multimode 已完整支持。

# 2026-09-02 STM32H723 FDCAN + shared Message RAM composite VMState（已完成）

- [x] 所属层：STM32H723 SoC 芯片层。producer 是 SoC-owned `DmMessageRam` 和单个
  `DmMc02Fdcan` raw state；boundary 是 `DmMc02FdcanMsgRamLink`；consumer 是未来
  SoC/board restore。没有接 UI、CAN 外部后端或 machine-level migration，未修改
  `trobot/`。
- [x] 新增 `dm_mc02_fdcan_msg_ram_link_bind()`、state validator、runtime sync 和
  version-1 VMState。流顺序固定为“Message-RAM geometry marker → FDCAN raw →
  pointer/geometry/element bounds validation → timer/IRQ projection”。RAM bytes 不在
  link 中重复序列化，继续由 QEMU `MemoryRegion`/RAM block owner 负责。
- [x] FDCAN 新增统一 Message RAM span validator，覆盖 128 standard/64 extended filter、
  configured FIFO、64 dedicated Rx-buffer indices 和 Tx FIFO，并修正 RXESC FIFO1 的
  F1DS 解码。所有 span 使用 checked 64-bit arithmetic；非法 state 不触发 runtime sync。
- [x] 新增 `test-dm-fdcan-msg-ram-link` `4/4`：round-trip 不覆盖目的 RAM bytes、geometry
  mismatch、FIFO/Buffer 越界、两个 link 共享一块 RAM。FDCAN VMState `5/5`、Message RAM
  owner `2/2`、FDCAN qtest/smoke 和 `qemu-system-arm` 重链通过。
- [x] 变更文件：`hw/arm/dm_mc02_fdcan.[ch]`、新增
  `hw/arm/dm_mc02_fdcan_msg_ram_link.[h]`/VMState、ARM/unit Meson 清单和 focused test；
  同步 `INTERFACES.md`、`ARCHITECTURE.md`、`REVIEW.md`；未修改 `trobot/`。
- [ ] 限制与下一道门：link 尚未接入 DM-MC02 machine-level VMState；RAM block 与多个
  FDCAN 的迁移顺序、CPU/IRQ/NVIC、DMA/DMAMUX、CAN queue/physical timing、section overlap
  诊断和整机 snapshot/migration 仍待独立下层边界。下一步继续选择相邻 SoC owner，不能
  由该 component contract 宣称整机迁移已支持。

# 2026-09-02 STM32H723 GPIO/SYSCFG/EXTI composite VMState boundary（已完成）

- [x] 所属层：STM32H723 SoC 外部中断数据面。producer 是 GPIOA..GPIOH 寄存器/采样输入、
  SYSCFG EXTICR 路由和 EXTI pending/line 状态；boundary 是 `DmMc02GpioExti`；consumer
  是板级输入注入、GPIO ODR 路由和 NVIC level-sensitive IRQ。
- [x] 为 GPIO、SYSCFG、EXTI 增加无副作用 raw VMState 描述，组合流固定为
  `GPIO -> SYSCFG -> EXTI`。parent 在所有 child 字段加载并验证 bank count/identity 后，
  依次执行 ODR consumer、板级 input-sync hook 和 EXTI IRQ projection，避免 standalone
  post-load 在路由尚未完整时产生错误副作用。
- [x] DM-MC02 machine 改用组合对象承载现有 GPIO/SYSCFG/EXTI 实例；board electrical
  input 仍由 runtime hook 所有，通用 SoC 层不猜测 pull-up/AF，也不复制 `external_gpio`。
- [x] 隔离门 `test-dm-gpio-exti-link-vmstate` `3/3`；GPIO/SYSCFG/EXTI 既有 VMState
  `2/2 + 2/2 + 2/2`，`dm-mc02-reset-test` `2/2`、`dm-mc02-cpu-test` `2/2`，
  `qemu-system-arm` 重链通过。
- [ ] 限制与下一道门：组合仍未注册 DM-MC02 machine-level VMState，不包含 machine
  `external_gpio`、CPU/NVIC、RAM、DMA/DMAMUX、外设/总线/co-sim 队列；完整 GPIO AF/电气
  规则、EXTI 安全域/event-only 和整机 snapshot/migration 仍未支持。下一步继续闭合相邻
  SoC 状态 owner 后再设计 machine composite。

# 2026-09-02 STM32H723 EXTI -> native CPU/NVIC IRQ wiring boundary（已完成）

- [x] 所属层：STM32H723 SoC GPIO/EXTI 到 QEMU 原生 ARMv7-M/NVIC 的直接边界。producer
  是七组 EXTI level 输出；boundary 是 `dm_mc02_exti_connect_nvic()` 的 borrowed
  `qemu_irq` route table；consumer 是已 realize 的 NVIC external input。
- [x] 路由表先完整校验 EXTI group、controller input geometry、非空 IRQ target 和重复
  group/input，再修改任何绑定；duplicate input 使用 pairwise 检查，不假设 NVIC 输入号
  小于 EXTI group 数，覆盖 DM-MC02 的 input 40。
- [x] DM-MC02 board profile 改为通过该 SoC API 连接全部 EXTI route；没有复制 CPU/NVIC
  vector/active/pending/priority 状态，QEMU native child VMState 仍是唯一 owner。
- [x] 隔离门 `test-dm-gpio-exti-link-vmstate` `4/4`；直接门
  `dm-mc02-cpu-test` `3/3`，覆盖 PA15 falling edge -> EXTI15_10 -> NVIC input 40，及
  EXTI pending 与 NVIC pending 的独立清除。
- [x] 同步 `AGENTS.md`、`INTERFACES.md`、`ARCHITECTURE.md`、`REVIEW.md`；未修改
  `trobot/`。
- [ ] 限制与下一道门：这只是接线/direct-consumer contract，不是 CPU/NVIC VMState 或
  machine-level migration；EXTI event-only/security、完整 GPIO 电气语义、其它外设 IRQ
  和整机 restore 顺序仍待单独底层门。

# 2026-09-02 STM32H723 DMA/DMAMUX composite-to-machine wiring boundary（已完成）

- [x] 所属层：STM32H723 芯片组合到 DM-MC02 machine wiring。producer 是已验证的
  `DmMc02DmaSubsystem`；boundary 是 machine 对 `dma[0..1]`/`dmamux[0..1]` 的唯一借用入口；
  consumer 是 DMA MMIO、reset、DMA IRQ、SPI/UART/ADC/WS2812 request 路由。
- [x] machine 删除平行的 `dma1`、`dma2`、`dmamux1`、`dmamux2` 成员，统一嵌入
  `DmMc02DmaSubsystem`，保持现有 H723 地址和 DMAMUX1 0/8 channel offset 不变；未注册
  machine-level VMState。
- [x] composite VMState 升为 v2，加入 DMA1/2、DMAMUX1/2 固定身份 marker；marker 或
  目的端 identity 不匹配在 request-cache/IRQ projection 前拒绝；旧的无 marker v1 流仍可加载。
- [x] 变更文件：`hw/arm/dm_mc02_dma.[ch]`、`dm_mc02_dma_subsystem_vmstate.c`、
  `dm_mc02.c`、DMA subsystem focused test，以及工作区/QEMU 约束和契约文档；未修改
  `trobot/`。
- [x] 隔离门 `test-dm-dma-subsystem-vmstate` `6/6`；覆盖 round-trip、v1 兼容、身份拒绝、
  FIFO/截断/版本失败前无 runtime projection。
- [x] 直接门 `run-dma-smoke.sh`、`run-dma-arbitration-smoke.sh`、`run-dma-batch-smoke.sh`、
  `run-dma-fcr-smoke.sh`、`run-dma-irq-smoke.sh` 和 `run-uart-dma-smoke.sh` 全部通过；目标
  `qemu-system-arm` 窄重链通过。
- [ ] 限制与下一道门：该切片仍不是 machine-level migration；RAM、CPU/NVIC、外设 scheduler、
  endpoint backpressure、DMA/外设联合恢复顺序和真实 DMA 总线时序仍需单独下层门。

# 2026-09-02 STM32H723 IWDG window-mode boundary（已完成）

- [x] 所属层：可复用 STM32H723 IWDG 芯片层。producer 是 IWDG 的 `PR/RLR/WINR`、
  reload/start key、绝对虚拟超时点和派生的下降计数；boundary 是 reload 时的窗口判定；
  consumer 是 QEMU guest reset 与 IWDG diagnostics。
- [x] 新增 `WINR` 寄存器（12-bit，默认 `0xfff`），仅在 `KR=0x5555` 解锁后可写；
  `WINR >= RLR` 保持硬件默认的关闭状态。窗口开启时，当前倒计数大于 `WINR` 的
  `0xaaaa` reload 立即请求 guest reset，合法窗口内 reload 重装原有 virtual timer。
  `WINR` 不重新启动 timer；`boot-grace-ms=0` 不再错误地跳过窗口检查。
- [x] 变更文件：`hw/arm/dm_mc02_iwdg.[ch]`、IWDG VMState validator、machine
  diagnostics、`tests/unit/test-dm-iwdg-vmstate.c`、新增
  `tests/qtest/dm-mc02-iwdg-test.c` 和 qtest 构建清单；未修改 `trobot/`。
- [x] 隔离门 `test-dm-iwdg-vmstate` `6/6`；直接 qtest `dm-mc02-iwdg-test` `4/4`，
  使用受控 virtual clock 覆盖默认关闭、WINR 锁定、窗口边界合法 reload、过早 reload
  reset 和完整超时；既有 `tools/run-iwdg-smoke.sh` 通过，`qemu-system-arm` 重链通过。
- [ ] 限制与下一道门：窗口计数由绝对 deadline 推导，未模拟 LSI 每 tick 的独立寄存器
  进展、LSI 温漂或完整独立电源域；IWDG component 仍未注册到 DM-MC02 machine-level
  VMState，不能据此宣称整机 snapshot/migration。下一步继续按层选择相邻动态外设边界。

# 2026-09-05 STM32H723 IWDG LSI 配置与固定误差（已完成）

- [x] 所属层：可复用 STM32H723 IWDG 芯片层。producer 是名义 LSI 频率、确定性固定
  ppm 误差、PR/RLR 和绝对 virtual deadline；boundary 是独立 timing helper 的整数
  有理数换算；consumer 是 IWDG timeout/window counter、guest reset 和 diagnostics。
- [x] 新增板卡无关的 `dm_mc02_iwdg_timing.[ch]`，以 `1,000,000 + ppm` 比例、
  `__uint128_t` 中间值和向上取整计算 timeout/剩余计数；不创建每个 LSI tick 的 QEMU
  事件。修复并由单测捕获了重复乘 prescaler 的首个公式错误。
- [x] DM-MC02 暴露并校验 `iwdg-lsi-hz`、`iwdg-lsi-error-ppm`，默认 `32000/0`，
  ppm 范围 `-999999..1000000`。运行中的 watchdog 拒绝 LSI 配置修改，不会重新解释或
  延长已有绝对 deadline；诊断包含有效频率。
- [x] 隔离门 `test-dm-iwdg-timing` `4/4`；直接门 `dm-mc02-iwdg-test` `5/5`，覆盖
  正/零/负误差的单调 timeout 阈值和配置诊断；相关 `qemu-system-arm` 增量重链通过。
- [ ] 限制与下一道门：固定 ppm 不是温漂或启动 settling 模型，尚未增加每 tick 可读
  倒计数、独立电源域或 machine-level VMState。下一步继续按依赖顺序审计相邻 SoC 外设
  边界，不能据此宣称真实 LSI 物理精度或整机迁移。

# 2026-09-05 IWDG timing sentinel and configuration-boundary correction（已完成）

- [x] 所属层：STM32H723 IWDG 可复用 timing/component 边界。修复 `deadline_ns == 0`
  在虚拟时间 0 被错误解释为已到期的问题；无效/未启动哨兵返回完整 `reload`，只有
  非零 deadline 到期才返回 0。
- [x] 收敛配置输入域：`dm_mc02_iwdg_set_lsi_hz()` 拒绝零频率，与 machine property
  和 timing helper 的非零契约一致；reset 的默认值仍由显式初始化路径设置，不再由 setter
  静默兜底。
- [x] 变更文件：IWDG timing/component/header、timing unit test，以及
  `INTERFACES.md`、`ARCHITECTURE.md`、`REVIEW.md`；未修改 `trobot/`。
- [x] 验证：`test-dm-iwdg-timing` `4/4`、`dm-mc02-iwdg-test` `5/5`、
  `tools/run-iwdg-smoke.sh` 和 `qemu-system-arm` 增量重链通过。
- [ ] 限制与下一道门：LSI settling/漂移、独立电源域、每 tick 可读倒计时和 machine-level
  VMState 仍未支持。下一切片继续选择相邻 SoC owner，并先定义 producer/boundary/consumer
  契约和隔离门。

# 2026-09-05 STM32H723 IWDG configuration-update status boundary（已完成）

- [x] 所属层：可复用 STM32H723 IWDG 芯片层。producer 是已提交的 `PR/RLR/WINR`、各自
  的 pending 值、`SR.PVU/RVU/WVU` 和绝对 virtual commit deadline；boundary 是寄存器写入
  到 LSI 域提交的同步握手；consumer 是 ST HAL 的 `HAL_IWDG_Init()` 等待循环、IWDG reload
  和 watchdog deadline。
- [x] 契约：解锁后对未 pending 的 `PR/RLR/WINR` 写入先只设置对应 SR 位，读取继续返回
  已提交值；同一寄存器在其 update flag 置位时被抑制。以 ST HAL 所述“最多 5 个 LSI clock
  periods”为确定性上界，为每个 pending register 建立独立的有效 LSI-derived virtual deadline。
  `PR/RLR` 提交不重解释已运行的 deadline，下一次合法 reload 使用新值；`WINR` 提交按 HAL
  注释自动 reload。SR、pending 值及 deadline 是 IWDG component state，必须随 component
  VMState 一起验证和恢复；timer/MemoryRegion/LSI 配置仍由目的端持有。
- [x] 隔离门：`test-dm-iwdg-timing` `5/5` 精确断言 nominal/正负 ppm 下的 status-update
  delay；`test-dm-iwdg-vmstate` `9/9` 覆盖 pending round-trip、不一致状态在 runtime sync
  前拒绝和 v1 流兼容加载。
- [x] 直接门：`dm-mc02-iwdg-test` `6/6` 以受控 virtual time 覆盖 locked write、SR flags、
  旧值可见、exact deadline commit、同寄存器写抑制、HAL 顺序和 `WINR` 自动 reload；随后运行
  `run-iwdg-smoke.sh`、真实 Release `trobot` 1 s 回归、`qemu-system-arm` 重链、89 项 QEMU
  smoke 和 Host CTest `54/54` 全部通过。
- [ ] 限制与下一道门：本切片采用资料公开的五周期上界，不模拟随机 synchronizer phase、LSI
  startup/温漂、独立电源域或整机 migration；完成后再选择相邻 SoC owner，不能用板级 grace
  或固件特判掩盖寄存器同步语义。

# 2026-09-05 STM32H723 IWDG reset reason → RCC_RSR（已完成）

- [x] 所属层：STM32H723 IWDG 到 PWR/RCC 的芯片层直接边界。IWDG producer 只发出
  board-independent reset-request callback；DM-MC02 consumer wiring 将其投影到实际 H723
  `RCC_RSR` offset `0xd0` 的 `IWDG1RSTF` bit 26。
- [x] `RCC_RSR.RMVF` bit 16 采用 write-one-to-clear，`IWDG1RSTF` 为只读来源位；PWR/RCC
  reset 保留已建模 reset flags，保证 watchdog reset 后 guest 可以在下一次启动观察原因。
  IWDG core 不包含板级 PWR/RCC 私有定义。
- [x] 变更文件：`hw/arm/dm_mc02_iwdg.[ch]`、`hw/arm/dm_mc02_pwr_rcc.[ch]`、
  `hw/arm/dm_mc02.c`、PWR/RCC unit、IWDG qtest 以及 AGENTS/INTERFACES/ARCHITECTURE/REVIEW；
  未修改 `trobot/`。
- [x] 隔离门：PWR/RCC unit `5/5`；直接 `dm-mc02-iwdg-test` `7/7`；受影响
  `qemu-system-arm` 重链通过。
- [x] bare-metal IWDG smoke 的第二次 guest boot 已实际读取 `RCC_RSR.IWDG1RSTF`，写
  `RMVF` 后确认清零；结果为 `0x04000000 → 0x00000000`。
- [ ] 限制与下一道门：仅支持 IWDG1 reset reason，不涵盖其它 H723 reset source、完整电源域
  复位原因、所有 RCC 位语义或 machine-level migration。

# 2026-09-05 STM32H723 SYSRESETREQ software reset reason → RCC_RSR（已完成）

- [x] 所属层：STM32H723 ARMv7-M/NVIC 到 PWR/RCC 的芯片层直接边界。producer 是 QEMU
  原生 `SYSRESETREQ` named output；consumer 是 `RCC_RSR.SFTRSTF`，machine 只负责借用
  runtime `qemu_irq` wiring。
- [x] 仅 asserted edge 投影 `SFTRSTF`（offset `0xd0`, bit 24），忽略 pulse deasserted edge；
  不从任意 QEMU/host reset request 猜测软件来源。`RMVF` 继续 W1C，普通 `system_reset`
  保留已建模来源 flags。
- [x] 变更文件：`hw/arm/dm_mc02_pwr_rcc.[ch]`、`hw/arm/dm_mc02.c`、PWR/RCC unit 和
  `tests/qtest/dm-mc02-cpu-test.c`，以及对应 AGENTS/INTERFACES/ARCHITECTURE/REVIEW；
  未修改 `trobot/`。
- [x] 隔离门：PWR/RCC unit `6/6`；直接 `dm-mc02-cpu-test` `4/4`，覆盖 AIRCR 写入、真实
  reset 完成、普通 reset 保留和 RMVF 清除；QEMU system target 增量重链通过。
- [ ] 限制与下一道门：只支持 SYSRESETREQ→SFTRSTF，不涵盖 CPURSTF、D1/D2、BOR/POR、pin、
  low-power、WWDG 或 machine-level reset-cause migration。下一步仍须逐项定义剩余 reset
  source，或在所有下层边界闭合后再设计 machine composite restore。

# 2026-09-05 STM32H723 power-on reset reason → RCC_RSR（已完成）

- [x] 所属层：STM32H723 芯片层 PWR/RCC reset-cause boundary。producer 是 DM-MC02
  machine construction 的一次显式初始 power-on event；boundary 是
  `dm_mc02_pwr_rcc_note_power_on_reset()`；consumer 是 guest 读取
  `RCC_RSR.PORRSTF`（bit 23）。
- [x] hook 与普通 reset 分离：machine 初始化后、guest 执行前只调用一次；QMP/guest
  system reset 不重复产生 POR，PWR/RCC reset 保留已建模 flags，RMVF 继续 W1C 清除。
  不把 `cold-reset`、任意 `cpu_reset()` 或普通 reset request 猜测成 POR。
- [x] 隔离门：PWR/RCC unit `7/7`，覆盖显式通知、幂等、reset 保留和 RMVF；直接门：
  `dm-mc02-cpu-test` `5/5`，覆盖初始 guest-visible flag、普通 reset 保留和清除。
  `qemu-system-arm` 增量重链通过，未修改 `trobot/`。
- [ ] 限制与下一道门：本切片只覆盖显式启动事件和 PORRSTF，不涵盖 BOR/PIN、D1/D2、
  CPU/低功耗/WWDG 来源、完整 reset-domain 语义或 machine-level migration；下一步先
  取得明确 producer 后再实现另一个 source，不能由 POR hook 推断其它来源。

# 2026-09-05 STM32H723 discrete brownout reset reason → RCC_RSR（已完成）

- [x] 所属层：STM32H723/DM-MC02 电源到 PWR/RCC 的单一直接边界。producer 是
  `DmMc02Power` 的离散 `NORMAL→UNDERVOLTAGE` 下降沿；boundary 是 brownout callback
  与 DM-MC02 reset wiring；consumer 是 `RCC_RSR.BORRSTF`（bit 21）和 QEMU 正常 reset
  fan-out。
- [x] 电源组件只通知运行时下降沿：启动时低 VIN、`VIN=0` 的 OFF、欠压保持和恢复均不
  生成 BOR；回调属于目标端 runtime wiring，`DmMc02Power` 不依赖 PWR/RCC 类型。
- [x] PWR/RCC 新增 BOR source latch，普通 reset 保留已建模来源，`RMVF` 继续 W1C 清除；
  machine 在初始 VIN/电源配置完成后才绑定 callback，避免启动低 VIN 伪造 BOR。
- [x] 变更文件：`dm_mc02_power.[ch]`、`dm_mc02_pwr_rcc.[ch]`、`dm_mc02.c`、power
  与 PWR/RCC unit、CPU qtest，以及工作区/项目约束和契约文档；未修改 `trobot/`。
- [x] 隔离门：power unit `5/5`、PWR/RCC unit `8/8`；直接门：`dm-mc02-cpu-test`
  `6/6`；`qemu-system-arm` 目标重链通过。
- [ ] 限制与下一道门：`12000 mV` 是当前离散 DM-MC02 模型阈值，不是已校准的 H723
  silicon BOR 电压；未实现模拟斜率/滞回、converter transient、其它 reset source、
  完整电源域或 machine-level migration。下一步仍需逐项建立 PIN/domain/CPU 等来源，
  或在 reset-cause 下层门闭合后再设计 machine composite。

# 2026-09-05 STM32H723 external NRST reset reason → RCC_RSR（已完成）

- [x] 所属层：STM32H723/DM-MC02 reset-input 直接边界。producer 是可复用
  dm-mc02-reset-input QOM GPIO 输入；boundary 是 machine runtime wiring 和
  dm_mc02_pwr_rcc_note_pin_reset()；consumer 是 RCC_RSR.PINRSTF（bit 22）及 QEMU
  普通 reset fan-out。
- [x] NRST 为 active-low，仅高到低边沿产生一次事件；低电平保持、普通 machine reset
  和重复 low input 都不重复排队。machine reset 保留外部输入电平，后续 high→low 才能
  产生新的 pin event。
- [x] 新增 reset-input 隔离单测 test-dm-reset-input，直接 dm-mc02-cpu-test 覆盖
  reset 后寄存器清零、PINRSTF 保留、低电平保持和 RMVF 清除；变更未触及 trobot。
- [x] 隔离门、直接 qtest 和 qemu-system-arm 增量重链均已通过。
- [ ] 限制与下一道门：/machine/reset-input 和 callback 属于 runtime wiring，未纳入
  machine-level VMState；本切片不涵盖 CPURSTF、D1/D2、低功耗、WWDG、其它 pin/reset
  domain、完整 reset-domain 时序或物理 NRST 去抖。后续 source 必须分别建立 producer、
  隔离门和直接 consumer 门。

# 2026-09-05 STM32H723 WWDG1 window-watchdog boundary（已完成）

- [x] 所属层：STM32H723 可复用 WWDG 芯片层到 DM-MC02 PWR/RCC 直接边界。WWDG 组件
  拥有寄存器、虚拟时间计数和下一个可观察事件；DM-MC02 只提供 APB1 timer clock、
  IRQ 和 reset-source wiring。
- [x] tick 使用 `ceil(4096 * 2^WDGTB * 1e9 / clock_hz)`，只调度 EWI 和最终 reset；
  `CNT=0x40` 产生 EWI，下一 tick 产生 reset。WDGA 运行后 set-only，低 T/窗口外
  reload 请求 reset，EWIF 为写零清除。
- [x] CFR.WDGTB 改频先按旧 divider 快照 CNT，再提交新 divider，避免计数相位被追溯
  重算。新增 `dm-mc02-wwdg-test` 覆盖 EWI、IRQ、EWIF、WDGA、分频切换、窗口违规、
  timeout 和 `RCC_RSR.WWDG1RSTF`。
- [x] 隔离 timing `3/3`、直接 qtest `3/3`、PWR/RCC、IWDG、CPU/reset 回归通过；
  QEMU smoke suite `89/89`、Host CTest `54/54`、目标重链通过。
- [ ] 限制：Renode 是当前行为参考，官方资料尚未独立核验全部 WWDG 细节；组件未注册
  machine-level VMState，不支持完整 reset-domain、migration 或硅级时钟/电气精度。

# 2026-09-05 STM32H723 WWDG1 component VMState boundary（已完成）

- [x] 所属层：STM32H723 可复用 WWDG 芯片层。producer 是 WWDG 寄存器镜像、可见计数、
  计数相位、EWI/reset 阶段和诊断计数；boundary 是 component-only normal/raw VMState；
  consumer 是目的端 WWDG timer/IRQ runtime projection。未注册 machine-level migration，
  未修改 `trobot/`。
- [x] 新增 `dm_mc02_wwdg_sync_runtime()`、状态校验和
  `dm_mc02_wwdg_vmstate()`/`dm_mc02_wwdg_vmstate_raw()`。保存 `CR/CFR/SR`、可见 counter、
  absolute virtual deadline、阶段与统计；`clock_hz`、QEMUTimer、IRQ、reset callback 和
  board/PWR/RCC wiring 保持目的端所有权。
- [x] normal restore 在保留位、计数、阶段/deadline 和目的端时钟全部通过后重建 timer/IRQ；
  raw restore 只验证/载入 producer state，不产生 runtime 副作用。过期 deadline 立即重臂，
  不重启新的 watchdog window。
- [x] 变更文件：`hw/arm/dm_mc02_wwdg.[ch]`、新增 `dm_mc02_wwdg_vmstate.c`、ARM/unit
  Meson 清单、`tests/unit/test-dm-wwdg-vmstate.c` 和 stub，以及约束/接口/架构/review 文档。
- [x] 隔离门 `test-dm-wwdg-vmstate` `8/8`；直接 `dm-mc02-wwdg-test` `3/3`；增量
  `qemu-system-arm` 重链通过。本轮未修改 `trobot/`。
- [ ] 限制与下一道门：该切片不是 machine-level snapshot/migration；CPU/NVIC、PWR/RCC、
  RAM、DMA、CAN/USB、总线和 reset-domain 联合恢复顺序仍需按层建立。下一步继续选择一个
  相邻下层动态状态边界，不能由组件 VMState 宣称整机迁移。

# 2026-09-05 STM32H723 DMA P2M FIFO overflow transaction boundary（已完成）

- [x] 所属层：可复用 STM32H723 DMA 芯片层。producer 是 P2M peripheral request 与 FIFO
  occupancy；boundary 是完整 `PSIZE` beat 的容量检查到 endpoint/MMIO 读取的提交顺序；
  consumer 是外设数据源和 DMA stream 状态。
- [x] 修复首个错误状态：P2M FIFO 在 endpoint/MMIO 读取前检查
  `fifo_length + PSIZE <= 16`。空间不足时不消费外设数据，锁存 `FEIF`、清空 FIFO、清除
  `EN`，并保持该失败 beat 的 `NDTR`、`PAR`、活动 memory cursor 和 guest memory 不变。
  `FEIE` 只门控 IRQ 投影，不门控 FEIF 锁存。
- [x] 变更文件：`qemu/upstream/hw/arm/dm_mc02_dma.c`、
  `tests/dma_fifo_dbm_endpoint_smoke.c`，以及 root/QEMU `AGENTS.md`、
  `INTERFACES.md`、`ARCHITECTURE.md`、`REVIEW.md`；未修改 `trobot/`。
- [x] 隔离门：`dm_mc02_dma_fifo_dbm_endpoint_smoke` 普通构建/运行、ASan 构建/运行和
  定向 CTest 通过；新增 FEIE 开/关已满 FIFO 场景，覆盖 callback 无副作用、状态保持、
  FIFO 清空和 FEIF/IRQ 分离。
- [x] 直接回归：`tools/run-dma-fcr-smoke.sh` 通过，`qemu-system-arm` 增量重链通过。
- [ ] 限制与下一道门：guest 正常 FIFO threshold 路径会主动排空 FIFO，本轮没有通过测试
  专用接口注入私有 occupancy；因此不声称完整硅级 FEIF 触发条件、DMA 总线仲裁、burst
  时序、完整错误恢复或 machine-level migration。下一步仍在 DMA 芯片层选择相邻边界。

# 2026-09-05 H723 DMA direct P2M endpoint reservation boundary（已完成）

- [x] producer/boundary/consumer：DMA direct P2M producer 读取 endpoint；
  `read_prepare` 暂存一个 source beat，DMA 完成 guest-memory write 后调用
  `read_commit`，写失败调用 `read_abort`；stream 的 NDTR/cursor 仍由 DMA 持有。
- [x] `DmMc02DmaEndpoint` 新增 `read_prepare/read_commit/read_abort`；完整 tuple
  才启用 reservation，partial tuple 返回 ERROR。旧 `read/read_ex` 保持 v1
  immediately-consuming compatibility path。`request_endpoint_batch()` 也接受该 tuple，
  每个 direct beat 独立配对 commit/abort。
- [x] OCTOSPI fixture/production wrapper 接入 reservation；隔离 endpoint smoke、DMA
  direct state smoke、OCTOSPI endpoint smoke、OCTOSPI-DMA integration smoke 共 `4/4`
  通过；生产 `ninja -C build/qemu qemu-system-arm` 通过。
- [ ] 限制与下一道门：在该历史切片完成时，它不是 FIFO 多 beat reservation、machine migration 或
  atomic-memory guarantee；legacy UART/SPI/ADC consumers 当时尚未迁移。下一步以 UART RX
  queue 为独立 consumer slice，不能用本切片结果宣称全部 P2M endpoint 可回滚。

# 2026-09-05 H723 UART RX direct P2M endpoint reservation（已完成）

- [x] 所属层：STM32H723 UART 数据面到 DMA direct P2M endpoint 的直接 consumer 边界。
  producer 是 UART CPU-visible RX FIFO；boundary 是队首 `read_prepare`、DMA 目标写入和
  `read_commit/read_abort`；consumer 是 RDR/RXNE 与 DMA stream 状态。
- [x] UART prepare 只复制并记录队首身份，不推进 FIFO；目标写返回 `MEMTX_OK` 后才推进
  队首、更新 RDR/RXNE，失败只释放 reservation 并保留原字节。legacy `read` 仍用于 FIFO
  P2M 和兼容路径，明确保持立即消费语义；同步 reservation 阻止重入 CPU RDR 偷取队首。
- [x] 新增 `smoke/dm_mc02_uart_rx_reservation_smoke.c` 和
  `tools/run-uart-rx-reservation-smoke.sh`，使用 UART1/DMA1 Stream0/DMAMUX1 request 41
  验证非法目标的 TEIF、EN、NDTR、RXNE 保持，以及重新配置后的同字节单次提交。
- [x] 验证：UART RX reservation smoke、UART TX qtest `1/1`、`qemu-system-arm` 增量重链，
  完整 QEMU smoke suite `90/90` 与 Host CTest `54/54` 均通过；`trobot/` 未修改。
- [ ] 限制与下一道门：仍只覆盖同步单 beat，不覆盖 FIFO 多 beat reservation、目标内存部分
  副作用回滚、UART/DMA 联合迁移或物理串口位级时序；下一步继续选择相邻单一
  外设 endpoint consumer，不能把该结果扩展为整机迁移能力。

# 2026-09-05 H723 SPI RX direct P2M endpoint reservation（已完成）

- [x] 所属层：STM32H723 SPI 数据面到 DMA direct P2M endpoint 的直接 consumer 边界。
  producer 是 SPI2 单槽 RXDR/RXP；boundary 是 `read_prepare`、DMA 目标写入和
  `read_commit/read_abort`；consumer 是 RXDR/RXP 以及 DMA stream 状态。
- [x] SPI prepare 只复制 pending RXDR 并建立同步 reservation，不清除 RXP；目标写返回
  `MEMTX_OK` 后才 commit 清除 RXDR，失败只 abort 并保留同一字节。reservation 期间同步
  CPU RXDR 读取不会偷走该字节。
- [x] DM-MC02 DMA1/DMA2 stream-enable wiring 仅匹配板级配置的 SPI RX controller/stream，
  重新 enable 后重试 pending RXDR，无需额外 SPI clock；DMA 仍拥有地址、仲裁和错误状态。
- [x] 新增 `smoke/dm_mc02_spi_rx_reservation_smoke.c` 和
  `tools/run-spi2-rx-reservation-smoke.sh`，使用 SPI2/DMA1 Stream3/DMAMUX1 request 39，
  验证非法目标的 TEIF/EN/NDTR 与 RXP/字节保持，以及有效重试后 BMI088 gyro `0x0f`
  单次提交。
- [x] 验证：SPI RX reservation smoke、`run-spi2-dma-smoke.sh` on/off 和
  `ninja -C build/qemu qemu-system-arm` 均通过；本轮未修改 `trobot/`。
- [ ] 限制与下一道门：仍只覆盖同步单 beat/单 RXDR 槽，不覆盖 FIFO 多 beat reservation、
  overrun replacement、目标内存部分副作用回滚、SPI/DMA 联合迁移或物理 SPI 位级时序；
  下一步继续选择相邻单一外设 endpoint consumer，不能把该结果扩展为整机迁移能力。

# 2026-09-05 H723 ADC1 `ADC_DR` direct P2M endpoint reservation（已完成）

- [x] 所属层：STM32H723 ADC regular data path 到 DMA direct P2M endpoint 的直接
  consumer 边界。producer 是 ADC1 regular result/`ISR.EOC`；boundary 是
  `read_prepare`、DMA 目标写入和 `read_commit/read_abort`；consumer 是 `ADC_DR`、
  `EOC` 和 DMA stream 状态。
- [x] ADC prepare 只复制一个 `ADC_DR` beat 并建立同步 reservation，不消费结果；目标
  写返回 `MEMTX_OK` 后 commit 复用普通 ADC data-read side effects 清除 `EOC`，失败只
  abort 并保留 `ADC_DR`/`EOC`。reservation 期间同步 CPU 读取不会偷走该结果。
- [x] DM-MC02 在 DMA1 P2M stream-enable 边界调用 ADC retry hook。ADC hook 先检查
  DMA/DMAMUX wiring、endpoint 完整 tuple、DMA 配置和 `EOC`，再让 DMA 执行一次请求；
  DMA 继续拥有 stream 选择、地址、`NDTR`、TEIF/TCIF 和 IRQ，重试不产生第二次 ADC 转换。
- [x] 新增 `smoke/dm_mc02_adc_rx_reservation_smoke.c` 和
  `tools/run-adc-rx-reservation-smoke.sh`，使用 ADC1 request 9、DMA1 Stream2、
  DMAMUX1；显式写入 `SQR1` 选择一个真实 rank，验证非法目标保留 ADC 结果/EOC 和
  NDTR、置 TEIF/停 stream，重新配置有效目标后同一个 `0x0100` 只提交一次。
- [x] 验证：ADC reservation smoke、`run-adc-dma-smoke.sh on|off`、
  `ninja -C build/qemu qemu-system-arm`、串行 QEMU smoke suite `92/92` 和 Host CTest
  `54/54` 均通过；本轮未修改 `trobot/`。
- [ ] 限制与下一道门：只覆盖同步 direct P2M 的单一 ADC1 `ADC_DR` slot；ADC FIFO、
  ADC12_COMMON `CDR/CDR2` 多 beat reservation、目标内存部分副作用回滚、ADC/DMA 联合
  migration 和物理 ADC/DMA 时序仍未支持；下一步继续选择一个相邻的单一底层数据边界。

# 2026-09-05 H723 ADC12 common CDR direct P2M reservation（已完成）

- [x] 所属层：STM32H723 ADC12 common 数据面到 DMA direct endpoint 的单一直接
  consumer 边界。producer 是 supported regular-simultaneous/interleaved CDR word；
  boundary 是 common-owned `cdr_read_prepare/commit/abort`；consumer 是 common CDR
  read acknowledgement、ADC1/ADC2 `EOC` 与 DMA stream 状态。
- [x] `cdr_valid` 保留一个 pending word；prepare 仅复制该 word 并阻止同步 CDR MMIO
  read 偷取它，`MEMTX_OK` 后 commit 才清除 latch 并复用 existing CDR read callback
  acknowledgement both EOC，error 后 abort 只释放 reservation。machine 不再根据
  `dm_mc02_dma_request_endpoint()` 的“request processed”返回值猜测传输成功。
- [x] DMA1 P2M stream-enable wiring 在 direct endpoint mode 检查 pending CDR 后提交一次
  existing request 9；DMA/DMAMUX matcher 仍唯一选择 stream，DMA 持有 address/cursor、NDTR、
  TEIF/TCIF 和 IRQ。合法重配因此重试原 CDR word，不制造 conversion。
- [x] ADC common component VMState v6 保存 producer `cdr_valid`，v1–v5 load 清除该新
  latch；`cdr_read_reserved` 是 transient runtime state，normal/raw pre-save 都拒绝
  non-empty reservation。该变化没有注册 machine migration。
- [x] 隔离门 `test-dm-adc-common` `16/16`，覆盖 prepare/abort/commit、CPU read exclusion、
  v6 state round trip 与 normal/raw save rejection；直接 `dm-mc02-adc-test` `63/63` 新增
  invalid CDR destination → TEIF/EOC/NDTR retention → valid re-enable exact retry。ADC
  DMA on/off、ADC reservation smoke、pair/ADC VMState 相关回归均通过；最终
  `qemu-system-arm` 重链、串行 QEMU smoke suite `92/92` 与 Host CTest `54/54` 通过；
  本轮未修改 `trobot/`。
- [ ] 限制与下一道门：只覆盖 synchronous direct P2M 的单一 CDR word；legacy CDR MMIO
  consumer 仍会在 target error 前读取/ack，CDR2 仍使用 legacy endpoint，ADC FIFO/multi-beat
  reservation、partial destination rollback、ADC/common/DMA composite migration 和 physical
  timing 未支持。下一步可单独定义 CDR2 的 producer/reservation/ack contract，不能复用本
  CDR pair acknowledgement 假设。

# 2026-09-05 H723 ADC12 common CDR2 direct P2M reservation（已完成）

- [x] 所属层：STM32H723 ADC12 common regular-interleaved CDR2 数据面到 DMA direct
  endpoint 的单一 consumer 边界。producer 是 `RDATA_ALT` 与 source；boundary 是
  common-owned `cdr2_read_prepare/commit/abort`；consumer 是 source-specific EOC ack、
  DMA stream 状态和目标内存。
- [x] CDR2 prepare 只复制当前结果并记录 source，目标写返回 `MEMTX_OK` 后才 commit
  并 ack 对应 ADC；非法目标 abort 保留 CDR2/EOC/NDTR，匹配 stream 重新 enable 后可
  重试同一结果。没有把 CDR 的双 EOC acknowledgement 规则外推到 CDR2。
- [x] `cdr2_read_reserved` 与 reserved source 为同步 runtime 状态；ADC common component
  VMState 不保存它，normal/raw save 在 reservation 活跃时拒绝。
- [x] 隔离 `test-dm-adc-common` `17/17`，直接 `dm-mc02-adc-test` `64/64`；最终
  `qemu-system-arm` 重链、串行 QEMU smoke suite `92/92` 与 Host CTest `54/54` 通过；
  本切片未修改 `trobot/`。
- [ ] 限制与下一道门：仅覆盖 DUAL=0x7/0x3、DAMDF=0x2 的同步单 beat；legacy CDR2 MMIO
  仍立即消费，FIFO/multi-beat reservation、目标端 partial rollback、ADC/common/DMA
  composite migration 和 physical timing 仍未支持。下一步继续选择一个独立的底层状态
  边界，不能由该切片宣称整机迁移。
