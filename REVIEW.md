# 2026-09-16 QEMU-02 统一测试门禁交付

- **R-04 已关闭。** 所属层是验证工具；producer 是 Meson、原生 Host CTest、完整
  pytest 和 shell smoke，boundary 是 `tools/dm_mc02_test_gate.py` 的清单、状态与
  身份聚合，consumer 是终端退出码和结构化 `summary.json`。
- 正式入口先构建并冻结身份，再运行四个不重复集合。历史 CTest Python/shell
  委托项标为 `gate-external`；两个 co-sim smoke 只消费预构建 production binary；
  旧 shell suite 已收敛为同一入口的兼容包装。ROS2/MuJoCo 缺依赖统一返回 77，
  只有这两个显式可选项可成为 SKIP。
- 隔离测试覆盖 PASS、FAIL、可选 77、必需 77、FAIL/BLOCKED 优先级、稳定发现、
  CLI 错误、pytest 插件隔离和受控 SHA 漂移，共 9/9 通过。实际默认门退出 0：
  Meson 65/65、Host 51/51、pytest 265/265、smoke 92/92；ROS2/MuJoCo 实际 PASS。
- QEMU 测试前后 SHA-256 均为
  `29175e5f691cd73864f88b1765f7e1859281af4db32c95952e28d1f1d60dc248`；
  51 个 Host 测试可执行文件的身份映射前后相同。测试阶段没有 production rebuild，
  未调用 Renode，也未修改 `trobot/`。
- 变更文件：统一入口及测试、CMake 标签、构建/兼容 runner、ROS2/MuJoCo 跳过语义、
  AGENTS/ARCHITECTURE/PLAN/README/REVIEW 和交付记录。设备、板级、固件与 wire API
  未变，`INTERFACES.md` 无需更新。
- 证据见 [QEMU-02 交付记录](reports/2026-09-16-qemu-02/README.md)。该门不覆盖完整
  上游 QEMU、真实 plant 性能或硅级时序；唯一下一步是 QEMU-03 的 reset/wrap
  采样边界。

# 2026-09-10 QEMU-01 交付与审查状态

- 当前推进范围仅 QEMU；Renode 专属审计项为范围外，不要求修复、构建或验证。
  新的长期计划已写入工作区/项目 PLAN 顶部，唯一下一步为 QEMU-02。
- **R-01 已关闭。** 首个错误是 runtime-sync 测试的 Meson source set 漏列现有
  `hw/arm/dm_mc02_dma_endpoint.c`，使真实 ADC core 的 reservation helper 调用
  无法链接。本次只为该目标增加一条源文件依赖，复用现有实现，不新增 stub。
- 所属层：STM32H723 隔离测试构建边界；producer 是 ADC core/helper 调用，
  boundary 是 Meson 依赖，consumer 是 runtime-sync 的 timer/IRQ/restore 断言。
- 验证：隔离 3/3；直接 ADC qtest 75/75；完整项目 Meson 65/65（54 unit、11 qtest），
  无排除项；Host 构建与 CTest 54/54（67.97 s，含 smoke 92/92）；pytest 256 通过，
  仅既有上游 event-loop deprecation warning。MuJoCo/ROS2 smoke 本轮实际 PASS。
- [交付日志、命令与输入身份](reports/2026-09-10-qemu-01/README.md) 已保存。
  生产模型、固件、接口、时序和性能配置未修改；没有新增测试方法或层，检查现有
  AGENTS/ARCHITECTURE/INTERFACES 后无需变更其契约。本次没有子代理。
- 修改文件：unit Meson、工作区/项目 PLAN、REVIEW、PROGRESS_REPORT 的快照说明；
  新增交付证据及构建产物。未清理/覆盖其它源码或历史产物。
- R-02～R-05 和 R-06 的 QEMU 部分仍待后续切片；本次通过不声明整机迁移、
  完整 H723/DMA 时序或真实 plant 闭环。原始失败及审查记录保留在下文。
- QEMU-02 补充证据：Host smoke 中 `run-cosim-link-smoke.sh:21` 再次调用
  `build-qemu.sh`，使用 venv Ninja 1.13.0，而 Meson 测试入口当前解析到系统
  Ninja 1.11.1。smoke 日志出现 `build log version is too old`；结束后二进制 hash
  改变，已记录的源码/构建选项/ELF hash 不变。不能将整轮日志绑定到一个不变的
  binary hash；交付目录分别保留 Host 前后身份。应在 QEMU-02 明确构建 owner 和
  Ninja 来源，避免测试中途重建；不在本次芯片测试依赖切片中跨层修改 runner。
  对最终二进制用 `--no-rebuild` 重新运行完整 Meson 集合，65/65 通过。

# 2026-09-10 全项目 review：审查快照与后续关闭状态

详细实现与验收矩阵见 [PROGRESS_REPORT.md](../PROGRESS_REPORT.md)。以下 R-01–R-06
为本次报告局部编号。此次是审查/规划交付，未修改生产代码；修复次序见 PLAN 顶部。

1. **R-01 / P1：ADC runtime-sync 隔离目标无法链接（后续已由 QEMU-01 关闭）。**
   `qemu/upstream/tests/unit/meson.build:370` 编译真实 ADC core，却没有提供
   `dm_mc02_dma_endpoint_read_reservation_supported` 的定义。新增调用位于
   `hw/arm/dm_mc02_adc.c:2484`，该测试的 stubs 也未提供 helper。首个错误是
   source set 缺失依赖导致 undefined reference，不是 ADC qtest 断言失败。
   完整 65 目标构建被阻断；应修复依赖、先运行隔离目标再全组回归。
2. **R-02 / P2：baseline RTF 可将复位算成极高性能。**
   `tools/collect-firmware-rtf.sh:222–256` 无 reset 检查，直接无符号相减。
   抽取实际 `run_one()`，用受控 QMP/process/clock 给出采样 tick 10000→100、
   IWDG timeout 不变，函数接受 `tick_delta=4294957396`、`rtf=4294957.396`。
   producer 的非 IWDG 复位被 consumer 误判为 wrap；需共享 reset/epoch admission
   并分别测试合法 wrap。此为脚本诊断复现，不是实际固件复位证据，也不推翻
   历史 virtual-window 三轮结果。
3. **R-03 / P2：startup-ready 等待无总期限。**
   同脚本 `190–196` 在 QMP 存活但 tick 低于 ready 阈值时可无限等待。受控时钟
   超过 100 s 仍循环，由诊断 fixture 主动中止。socket/单命令 timeout 不能约束
   整个启动阶段；需独立 deadline、停滞诊断和有界清理，并校验 NaN/Inf 参数。
4. **R-04 / P2：测试聚合遗漏，skip 可记为 pass（后续已由 QEMU-02 关闭）。**
   `CMakeLists.txt:715–723,778–782` 未聚合完整 pytest/Meson；unittest discovery
   漏掉函数式和参数化测试。`run-ros2-worker-smoke.sh:26–31` 缺依赖时 exit 0，
   `run-qemu-smoke-suite.sh:35` 按退出码计 pass。本轮 ROS2 实际 PASS，问题是
   其它环境下的统计契约。应复用现有 runner 聚合并保留显式 skip/blocked。
5. **R-05 / P2 风险：干净重建和源码交付未验证。**
   QEMU 上游工作树固定 v8.2.2，同时存在 tracked 修改及 248 个 untracked 路径；
   项目外层不是 Git 工作树。`apply-qemu-patch.sh:8–20` 只校验存在/注册，不应用
   补丁。仅上游 commit 不足以复现当前模型；需 manifest/checkpoint 和 clean
   rebuild 证据。这不是丢失证据，不得清理这些源码。
6. **R-06 / P2 文档与验收债务：历史清单不能代表当前能力。**
   PLAN/REVIEW/INTERFACES 有已被后续实现解决的旧限制；README 的 ADC 1–4 rank
   已落后于源码的 1–16 rank，温漂/random walk 等早期未完成项也需核对。Renode
   缺独立 ARCHITECTURE。应保留历史并建立当前支持/近似/fixture/未支持矩阵，
   不按旧勾选数宣称完成率。

审查时实测（修复后的结果见本文顶部）：pytest **256 passed**；重建 Host CTest **54/54**，包含 smoke **92/92**。
Meson 项目集 **65 目标中 1 个链接失败**；排除该目标后 **64/64 通过**（53 unit、
11 qtest；ADC common 19/19、ADC qtest 75/75）。不能称整体门禁通过。Renode 平台
检查通过、既有 C++ 二进制 CTest 1/1、Python 2/2；未重跑完整固件矩阵或三轮性能。
原始构建失败和运行日志保存在 [审查日志](reports/2026-09-10-review/)。

残余范围：完整 H723 时序/复位域、DMA FIFO 多 beat reservation/部分目的写回滚、
USB 物理/枚举、板级业务 readiness、真实 plant 长时闭环、硬件差分校准及整机恢复
仍需各自验收。component、fixture 与短 smoke 不替代这些门。

本轮修改文件：工作区 PROGRESS_REPORT.md/PLAN.md、项目 PLAN.md/REVIEW.md；
新增审查日志，构建刷新产物。没有生产或固件改动、没有子代理；未引入新接口或
验证规则实现，现有 AGENTS/INTERFACES/ARCHITECTURE 的契约无需变化。

# 2026-09-10 QMP consumer follow-up review

Two findings addressed within the tooling slice:

1. The ADC calibration/input scripts and firmware RTF collector still passed
   an always-None receive buffer through local QMP wrappers after migration.
   Removed those arguments, return tuple elements, assignments and wrappers;
   QmpSession remains the only command/transport boundary.
2. ARCHITECTURE and AGENTS described the release gate as running NullEngine.
   `collect-firmware-rtf.sh` launches only QEMU, using `-serial none` and no
   worker/chardev plant connection. Corrected the profile and recorded separate
   unpaced-capacity and worker-paced validation as open. No simulator behavior
   was changed to make the implementation match the inaccurate claim.

Validation: direct ADC input/calibration smokes pass; collector baseline and
one-virtual-second modes pass with zero IWDG timeouts. Short samples report
RTF 0.999845/0.999139, startup 0.022079/0.523688 s, CPU 114.9/114.7%, RSS
50116/50272 KiB respectively. Baseline startup measures initial QMP observation;
the virtual-window mode additionally waits for tick 250. These short samples
verify consumer integration, not release performance. No new three-run gate
was run. CTest 54/54 (18.07 s), including QEMU smoke 92/92; Python 32 run,
31 passed/1 skipped; all shell files checked individually with bash -n.

Changed files: the three tools named above, workspace/project PLAN.md, project
REVIEW.md, INTERFACES.md, ARCHITECTURE.md and AGENTS.md. No firmware, device
state or QMP public API changes; no subagents used. The remaining project scope
is broader than QMP and includes migration, physical timing and external backend
validation. Earlier 'only remaining work' summaries were too broad.

# 2026-09-10 ADC common / DMA fixes

R20260906-1 and R20260906-2 are resolved in source. The first slice adds the
chip-owned `cdr_dma_request_pending()` admission predicate, shared by publication
and retry, while retaining OVR-independent CPU reads. Unit coverage checks
master/slave/both OVR and CPU consumption; three direct qtests prove OVR blocks
stream-enable retry and that clearing it allows the exact retained word to
transfer without another conversion. This slice passed common unit 18/18 and
ADC qtest 67/67 before starting the FIFO slice.

The second slice adds explicit immediately consuming CDR/CDR2 component reads,
with little-endian copy and existing register acknowledgement. The board exposes
these as legacy endpoint reads for FIFO, alongside the direct reservation tuple.
No DMA private state is accessed and no FIFO reservation is introduced. The new
FIFO endpoint test was run before wiring the callbacks and failed with EOC still
set; after the fix all eight CDR/CDR2 × endpoint/MMIO × valid/invalid destination
cases pass. Invalid FIFO destinations intentionally retain the existing consuming
semantics, while direct destination failures still preserve their source.

Full Host rebuilding exposed a pre-existing independent header dependency:
`dm_mc02_bmi088.h` and `dm_mc02_bmi088_spi.h` included migration/vmstate.h
from standalone consumers that had not included qemu/osdep.h. The first errors
were undefined Error and GLib macros in QAPI headers. These public headers now
forward-declare VMStateDescription; actual VMState implementation files already
include the definition. Both host smoke targets and full Host rebuild pass;
BMI088 VMState, SPI VMState and SPI-link VMState pass 5/5, 5/5 and 4/4.
This changes no sensor behavior or serialized format.

Changed production files: `hw/arm/dm_mc02_adc_common.[ch]`, `dm_mc02.c`,
`dm_mc02_bmi088.h`, `dm_mc02_bmi088_spi.h` under canonical `qemu/upstream/`.
Tests: `tests/unit/test-dm-adc-common.c`, `tests/qtest/dm-mc02-adc-test.c`.
Contracts and plans: workspace/project AGENTS.md and PLAN.md, project
INTERFACES.md, ARCHITECTURE.md and this review. No trobot changes or subagents.

Validation: rebuilt QEMU and all Host targets; common unit 19/19; ADC qtest
75/75; eight new FIFO direct cases pass. Final `ctest --test-dir build/host
--output-on-failure` passes 54/54 (64.92 s), including serial QEMU smoke
92/92. No performance gate was rerun. FIFO multi-beat reservation, target
partial-write rollback, other-format OVR policy and machine migration remain
unsupported; the old reproductions below describe the pre-fix state.

# 2026-09-06 ADC common / DMA current-code review (resolved 2026-09-10)

Scope: latest ADC CDR/CDR2 producer, DMA direct/FIFO dispatch, board retry
wiring, common component VMState and corresponding unit/qtest coverage. This
is a bounded source review, not a whole-workspace correctness audit. Production
code and firmware were not changed. Workspace and project AGENTS.md were
reviewed; no new interface, architecture rule or verification gate is introduced.

## R20260906-1 [P1] Stream-enable retry bypasses CDR overrun gating

- Location: `qemu/upstream/hw/arm/dm_mc02.c:2035` and `:2115`;
  `dm_mc02_adc_common.c:162` and `:630`.
- Producer: regular-simultaneous DAMDF=2 publishes a retained CDR word but
  suppresses its data-ready callback when either ADC has OVR. Boundary:
  stream-enable retry only checks DMA enable and `cdr_data_pending()`, whose
  meaning is unread data, not permission to issue a DMA request. Consumer:
  DMA therefore writes the word and commit acknowledges both EOC flags even
  though OVR remains set. The first wrong event is the retry request admission.
- Reproduced with the existing QEMU binary and virtual-clock qtest commands:
  leave stream 0 disabled through two conversions, obtaining ADC1/ADC2 ISR
  `0x1d`, NDTR=1 and destination=0. Write S0CR=`0x5001` without clearing OVR
  or advancing time: both ISR become `0x19`, NDTR=0, LISR=`0x20` (TCIF), and
  destination=`0x02000200`. Expected: no DMA transfer while the documented
  common OVR request gate is closed; pending EOC/data and NDTR stay unchanged.
- Existing `test_common_multimode_dma_overrun_request_gate()` reads CDR and
  clears OVR before enabling DMA, so it misses this transition.
- Required fix gate: centralize the reusable common DMA request-admission rule
  and apply it to publication and retry. Keep CPU CDR read acknowledgement
  valid while OVR is set; do not add OVR to the generic unread-data predicate
  without separating CPU and DMA semantics. Add isolated admission tests and
  a direct qtest enabling DMA with master-only, slave-only and both OVR flags.

## R20260906-2 [P2] CDR/CDR2 endpoint wiring rejects FIFO transfers

- Location: `qemu/upstream/hw/arm/dm_mc02.c:2042` and `:2082`;
  `dm_mc02_dma.c:556`; `dm_mc02_dma_endpoint.c:26`.
- Board endpoints expose only prepare/commit/abort. DMA routes DMDIS=1 to
  its FIFO consumer, which calls the legacy transfer dispatcher; neither
  `read` nor `read_ex` exists, so the first beat returns ERROR and sets TEIF.
  Default `adc-dma-endpoint=on` therefore rejects even a one-word, aligned,
  equal-width FIFO transfer to valid SRAM. This is a callback compatibility
  gap, independent of implementing transactional multi-beat rollback.
- Reproduced for CDR (DUAL=6) and CDR2 (DUAL=7), DAMDF=2, request 9,
  PSIZE=MSIZE=32, NDTR=1, FCR=`0x4`. Endpoint on: LISR=`0x8` (TEIF),
  EN cleared, NDTR=1, destination=0. Endpoint off with identical registers:
  LISR=`0x20`, NDTR=0; destination is `0x01000100` for CDR and `0x00000100`
  for CDR2. Both paths use the same DMA FIFO implementation and memory map.
- No earlier binary was compared, so the introducing revision is not proven.
  Documentation excludes FIFO reservation but does not explain that the new
  default endpoints reject FIFO outright. Existing reservation qtests only
  exercise direct mode.
- Required fix gate: explicitly define and test the legacy consuming FIFO
  compatibility boundary (or explicitly reject/document unsupported FIFO
  configuration at admission). Do not reuse the single-beat reservation tuple
  as an implicit multi-beat transaction. Add CDR/CDR2 FIFO on/off direct qtests.

## Reproduction and verification

Use `build/qemu/qemu-system-arm -machine
dm-mc02,adc-accurate-timing=on,adc-dma-endpoint=on -accel qtest -display none
-qtest stdio` (machine argument on one line). Each case starts a fresh process.
The following are qtest `writel ADDRESS VALUE` pairs, in order:

```text
0x58024428 0x2002
0x58024438 0xee27
0x5802442c 0x80000
0x58024400 0x4010001
0x58024458 0
0x40022308 0x8006
0x40020800 9
0x40020018 0x4002230c
0x4002001c 0x20000100
0x40020014 1
0x20000100 0
```

For R1, follow with ADC1_CFGR (`0x4002200c`)=`0x2003`, ADC2_CFGR
(`0x4002210c`)=`0x2000`, ADC2_CR (`0x40022108`)=1, ADC1_CR
(`0x40022008`)=5, then `clock_step 5000`. Read both ISR (`0x40022000`,
`0x40022100`), NDTR (`0x40020014`) and memory. Write S0CR
(`0x40020010`)=`0x5001` and re-read without stepping time.

For R2, insert FCR (`0x40020024`)=4 and S0CR=`0x5001` before the ADC
configuration/start writes, then `clock_step 2500`. Repeat with endpoint off;
repeat both modes with CCR=`0x8007` and PAR=`0x40022310` for CDR2.

All five cases were executed successfully as probes; their observed states
demonstrate the two defects, not passing correctness assertions. Processes were
terminated and waited after each case. The prior progress-check run in this
session passed common unit 17/17, ADC qtest 64/64 and Host CTest 54/54 including
QEMU smoke 92/92; no production edits occurred between those checks and this
review. Those existing tests do not cover the failing transitions above.
No new performance, Renode, firmware or whole-machine migration claim is made.

# 2026-09-02 STM32H723 SoC RAM ownership and migration registration review

本轮继续停留在 STM32H723 SoC memory composition 层，未注册 DM-MC02 machine-level
migration，也未修改 `trobot/`。producer 是六个内部动态存储窗口；boundary 是
`memory_region_init_ram()` 的 QEMU RAMBlock 注册；consumer 是真实 system-mode 地址空间、
Flash program overlay 和现有 cold-reset helper。

审计确认首个问题是所有权语义，而不是 reset：`memory_region_init_ram_nomigrate()` 会创建
RAM 但不会调用 `vmstate_register_ram*()`，导致 Flash 和五块片上 RAM 不在标准迁移集合。
同时，`MachineState` 不能作为 `DeviceState` owner 传给标准 initializer。修复是在 SoC 层
统一使用 `memory_region_init_ram(..., NULL, ...)`；不复制字节、不增加第二套 VMState，
QEMU global RAMBlock 继续作为唯一迁移 owner。固定 UID/校准 ROM 仍是 realize 时可重建的
non-migrate 数据。

验证：

- `tests/unit/test-dm-soc-memory`：`2/2`，六个动态区全部走 migratable 初始化，ROM 走
  non-migrate 初始化，且没有错误 owner。
- `tests/qtest/dm-mc02-memory-test`：`2/2`，真实寄存器解锁/PG 后 Flash 写入、校准 ROM
  写保护、warm reset 保留全部普通 RAM、cold reset 清除五块片上 RAM。
- `git diff --check`：通过；相关 QEMU target 可重链。

期间直接 qtest 的第一次失败是测试未执行 Flash unlock/PG 前置条件，读取到 `0xff` 是
模型正确拒绝写入；补齐真实 FLASH_KEYR1/CR1 路径后通过。第一次构建调用使用了不存在的
裸 target 名称，改为 `tests/unit/...` 和 `tests/qtest/...` 路径后通过；两者均不是源码
故障。

残余风险：该改动只让 RAMBlock 具备标准 migration registration，不能证明整机 snapshot/
migration。machine-level VMState、CPU/NVIC、DMA/DMAMUX、外设状态、Flash 子设备、总线和
co-sim 队列的顺序及跨块一致性仍待下层门逐项闭合。隔离 stub 只验证 SoC 的初始化调用
契约，标准 QEMU RAM migration 实际传输仍须在未来 machine composite 门中验证。

# 2026-09-02 STM32H723 OCTOSPI/OCTOSPIM component VMState review

本轮所属层是 STM32H723 SoC 外部存储控制器边界，未注册 DM-MC02
machine-level migration，也未修改 `trobot/`。producer 是 `DmMc02Ospi` 的寄存器镜像、
间接事务游标和 DMA/PIO 共用 staging；boundary 是 version-1 normal/raw VMState；consumer
是 OSPI memory-mapped alias、DMA endpoint 以及 `DmMc02SsiNor` 后面的 QEMU 标准
`w25q64/m25p80`。

复用审计确认 NOR 命令、Flash backing storage 和 Flash 子设备状态不能在 OSPI 中重复实现。
因此组件只保存控制器下一次可见行为所需的 `regs[0x400]`、RX buffer/游标、TX staging/
游标、command address/opcode 和 command flags。Flash geometry/JEDEC、`MemoryRegion`、
SSI bus/device、DMA callbacks 都是目标端静态配置或 runtime wiring；标准 m25p80 的
VMState 仍由 QEMU 子设备自身负责。

首个边界风险是动态 RX 指针：直接把指针当固定 buffer 会在恢复时写入目标旧容量。实现
改用 `rx_size` 前置的 bounded `VMSTATE_VBUFFER_ALLOC_UINT32`，加载前释放目标旧 staging，
并把最大长度限制在 1 MiB。validator 拒绝 `rx_pos > rx_size`、超出 256-byte TX staging、
无效 page-program sentinel、未开始命令却残留 active cursor，以及未知 active command；
非法状态不会调用 runtime sync。normal post-load 从 `CR.FMODE` 重建 memory-mapped gate，
只对中断 page-program 恢复 SSI CS；raw 描述验证但不产生这些副作用。

`OCTOSPIM` 作为独立纯寄存器组件提供 version-1 描述。隔离
`test-dm-ospi-vmstate` 为 `6/6`；直接 `run-ospi-smoke.sh` 和
`run-board-profile-smoke.sh` 通过，`qemu-system-arm` 重链通过。截断负向测试产生的
VMState load error 是预期诊断。

残余风险：组件状态没有接入 machine-level VMState；标准 Flash 子设备/backing storage、
OSPI 与 DMA/CPU/IRQ/board reset 的联合恢复顺序、Flash asynchronous WIP、line mode/DTR、
掉电中止和完整 OCTOSPI 语义仍未验证。当前结果只证明组件边界和既有直接 consumer 不回归，
不能宣称整机 snapshot/migration。

# 2026-09-02 DM-MC02 board power component VMState review

本轮所属层是 DM-MC02 board power boundary，未注册 machine-level migration，也未修改
`trobot/`。producer 是 `DmMc02Power` 的动态 VIN/GPIO ODR/electrical-power 输入；boundary
是 `dm_mc02_power_vmstate()`；consumer 是离散 24 V/5 V/3.3 V rail 投影和 ADC board-source
回调。

审查将状态分为动态输入、静态 wiring 和派生输出三类。VMState 只保存
`vin_mv`、`gpio_odr`、`electrical_power`；ADC 指针、输出 mask、ADC channel 属于目标端
profile/runtime wiring；`state`、各路 good/enabled、ADC raw 和 `last_update_ns` 在目标端
由一次 `dm_mc02_power_sync_runtime()` 重建。特别没有保存 `last_update_ns`，避免把源端
虚拟时间 epoch 的诊断时间戳伪装成目标端当前事件。

隔离测试覆盖 normal post-load 的导轨和 ADC 重投影、raw 描述不触发 ADC callback、未遮罩
GPIO ODR 的非法状态，以及截断状态在 runtime projection 前拒绝。直接 smoke 覆盖
VIN=0、欠压、正常、电源 GPIO 开关和 VIN restore/reset。实现范围保持为离散功能模型，
不扩展到 converter transient、
电流、纹波或模拟欠压曲线。

变更文件：`qemu/upstream/hw/arm/dm_mc02_power.[ch]`、新增
`dm_mc02_power_vmstate.c`、ARM/unit Meson 清单和
`tests/unit/test-dm-power-vmstate.c`，以及同步的 `PLAN.md`、`INTERFACES.md`、
`ARCHITECTURE.md`；本轮未修改 `trobot/`。

验证：

- `ninja -C build/qemu tests/unit/test-dm-power-vmstate` 通过。
- `tests/unit/test-dm-power-vmstate --tap`：`4/4`。
- `bash tools/run-power-boundary-smoke.sh`：通过。
- `bash tools/run-power-runtime-smoke.sh`：通过。
- `qemu-system-arm` 全量目标重链通过；相关 VMState 单测均通过；Host CTest `54/54` 通过。
  VMState 单测中的非法/截断状态日志是预期的负向验证输出，测试结果仍为通过。

独立 smoke 编译输出的 QEMU 头文件 `-Wpedantic`/unused-parameter 警告属于现有
standalone harness 与 QEMU GNU 扩展，不影响功能结果，也未修改上游头文件。

残余风险：这是组件级 board power 状态契约，尚未接入 machine-level VMState；完整
board composite 仍需定义 power、GPIO、ADC、CPU/IRQ 和其它外设的 restore 顺序。当前
离散 rail 模型不代表真实电源瞬态或硬件电气时序。

# 2026-09-02 STM32H723 ARMv7-M CPU/NVIC/SysTick boundary review

本轮所属层是 STM32H723 SoC 的 CPU、异常/IRQ 和 SysTick 组成边界，未注册
DM-MC02 machine-level migration，也未修改 `trobot/`。producer 是 QEMU 原生
`ARMv7MState`、Cortex-M7、NVIC 和 SysTick；boundary 是 `armv7m_realize()` 的 QOM
组合、时钟连接和 IRQ wiring；consumer 是 DM-MC02 的 163 个板级 external IRQ 输入
以及 guest 的 PPB/SCS 寄存器。

复用审计确认不应在 DM-MC02 增加 CPU/NVIC/SysTick 镜像。`object_initialize_child()`
建立 `nvic`/`systick-reg-ns` 的 child ownership，`object_new_with_props()` 创建
`cpu` child，`qdev_realize()` 完成 CPU realize；QEMU 的 realized-device 路径随后
自动注册各自 class VMState。对应的 canonical native descriptions 是 ARM CPU v22、
NVIC v4 和 SysTick v3。板级代码只配置 Cortex-M7、clock、memory 和输入线路。

首个测试错误是契约认知错误：将 `ARMv7MState.num-irq` 当成 external IRQ 数，并将
SysTick pending 错误地寻找在外部 `NVIC_ISPR`。复核 QEMU ARMv7-M composition 后修正
为完整 vector 数 `179 = 16 internal exceptions + 163 external inputs`，并通过
`SCB->ICSR.PENDSTSET` 检查 SysTick。另一个测试边界错误是 qtest QMP 返回类型不同：
`qom-list` 返回 `QList`、`qom-get` 返回 `QNum`，不能统一使用只接受 `QDict` 的
`qtest_qmp_assert_success_ref()`；现已按实际 QMP 类型读取并释放对象。

变更文件：`qemu/upstream/tests/qtest/dm-mc02-cpu-test.c`、
`qemu/upstream/tests/qtest/meson.build`，以及同步的 `PLAN.md`、`INTERFACES.md`、
`ARCHITECTURE.md`。测试覆盖 QOM child 类型、179/163 数量区分、外部 IRQ priority/
enable/pending/level、64 MHz 下 SysTick 1 µs virtual-time expiration、内部 pending
和 system reset 清零。

验证：

- `ninja -C build/qemu tests/qtest/dm-mc02-cpu-test qemu-system-arm` 通过。
- `QTEST_QEMU_BINARY=./qemu-system-arm tests/qtest/dm-mc02-cpu-test --tap`：`2/2`。

残余风险：该测试只验证 CPU/NVIC/SysTick 的直接 QEMU/SoC 边界，不证明完整 H723
异常、调试、安全扩展、所有 reset 细节或整机 snapshot/migration。未来 machine
composite 必须在目标端先完成相同 QOM graph、clock source、memory map 和 IRQ wiring，
再加载原生 child VMState；CPU/IRQ 与 RAM、外设、DMA、总线和 co-sim 队列的联合恢复
仍需独立切片和直接 consumer 测试。

# 2026-09-02 STM32H723 message-RAM owner boundary review

本轮所属层是 STM32H723 SoC memory composition 到 FDCAN 的直接边界，未注册
DM-MC02 machine-level migration，也未修改 `trobot/`。producer 是 FDCAN1/2/3
共享的 message-RAM 字节区域；boundary 是 `DmMessageRam`；consumer 是 SoC
`MemoryRegion`/guest 地址空间和 FDCAN 借用的 data pointer。

首个错误出现在隔离构建边界：`memory_region_init_ram()` 与
`memory_region_get_ram_ptr()` 属于 QEMU system-mode memory 实现，而原 unit target
只链接 QOM/HW core，导致链接失败。根因不是 owner API 或 SoC 映射。修复是在 unit
target 中加入仅测试用的窄 MemoryRegion stub；真实 QEMU RAM 行为没有由 stub 冒充，改由
`dm-mc02-memory-test` 在完整 system target 中验证。另将 owner 初始化改为本地
`Error *` 接收再传播，避免调用者传入 NULL `Error **` 时在分配失败后继续访问无效
`ram_block`。

实现将 FDCAN message RAM 从 machine 私有 `memory_region_init_ram_nomigrate()` 区域迁移到
板卡无关的 `DmMessageRam`，并嵌入 `DmMc02SocMemory`。当前 SoC 传入 `NULL` QEMU owner，
因此使用标准 global RAM block migration registration；FDCAN VMState 仍排除
`MemoryRegion`、message RAM pointer/bytes、CAN bus、chardev 和 IRQ wiring。DM-MC02 reset
通过 SoC API 显式清零整块 message RAM，保持原有 reset 观察结果。

验证：`test-dm-message-ram` `2/2`、`dm-mc02-memory-test` `1/1`；
`run-fdcan-smoke.sh`、`run-fdcan-medium-smoke.sh`、`run-fdcan-busoff-smoke.sh` 均通过；
QEMU smoke suite `89/89`、Host CTest `54/54`、`qemu-system-arm` 重链通过。构建过程中
出现的上游 `vhost-shadow-virtqueue.c` 未初始化变量 warning 与本切片无关，未修改其
源码。

残余风险：标准 RAM block 已注册迁移，但整机仍没有 machine-level VMState；不能据此宣称
snapshot/migration。其它片上 RAM、CPU/IRQ/NVIC、FDCAN 组件与 message RAM 的联合 restore
顺序、外部 CAN/chardev peer 状态和真实 CAN 物理时序仍未覆盖。下一道门应继续先定义一个
相邻的 SoC/外设恢复边界，并保持 message-RAM owner 只负责字节存储和显式 reset。

# 2026-09-02 STM32H723 FDCAN component VMState review

本轮所属层是 STM32H723 FDCAN 芯片层，未注册 DM-MC02 machine-level migration，也未修改
`trobot/`。producer/boundary/consumer 分别是 FDCAN 寄存器与内部 FIFO/host-wire 状态、
`dm_mc02_fdcan_vmstate()`、未来 machine composite/QEMU CAN bus/84-byte host wire。

审查发现并修复的首个实际行为错误在已有 host-wire producer：`qemu_chr_fe_write()` 返回
正数短写后，原代码只更新 offset 就返回，没有设置下一次虚拟 retry，因此队列可能永久
停留。新增 `tx_next_ns`，所有 blocked/partial retry 都使用同一单调虚拟 deadline；队列
排空时取消 timer。该修复没有把 wall-clock 或隐式线程引入 CAN 热路径。

当前 version-2 状态保存寄存器、partial RX wire、FIFO/Buffer cursors、TX pending、bus-off
参与门、计数器和有界 TX queue；message RAM、CAN/chardev、IRQ、MemoryRegion、供电和
host-ACK 是目的端或板级 owner。validator 拒绝 FIFO 越界、pending 位图超出配置容量、
环形队列无效 offset、截断 deadline、无队列却存在 deadline 以及 `PSR.BO` 与 bus-off
参与门不一致。普通 post-load 只在完整验证后重投影 timer/IRQ；raw 描述保留 legacy
归一化但不能绕过其 message-RAM owner 契约。

复核发现原 version-1 字段集漏保存 `bus_off` 内部参与门；只恢复 `PSR.BO` 会让目标端
仍可能参加 QEMU CAN bus。现将组件描述升为 version 2，并保留 v1 load compatibility：
旧流在校验前从 `PSR.BO` 重建该门；validator 同时拒绝两者不一致。raw 描述只做该状态
归一化和验证，不重建 timer/IRQ/总线等运行时投影。

验证：`test-dm-fdcan-vmstate` `5/5`；`run-fdcan-smoke.sh`、`run-fdcan-medium-smoke.sh`、
`run-fdcan-busoff-smoke.sh`、`run-fdcan-ext-smoke.sh`、`run-fdcan-rx-buffer-smoke.sh`
均通过；`qemu-system-arm` 重链通过。组件测试覆盖跨环尾 TX queue、IRQ projection、非法
pending mask、截断流和 raw 无 projection。截断负向用例输出预期的 VMState load error，
不是测试失败。

残余风险：当前未迁移 message RAM 内容、外部 CAN bus/chardev peer 状态或 machine-level
CPU/IRQ/RAM/外设联合恢复；QEMU 标准 CAN 仍不提供仲裁、bit timing、error frame 和真实
bus-off recovery。下一道门应先定义 message RAM owner 与 FDCAN/board composite 的恢复
顺序，再接入 direct machine snapshot 测试。

# 2026-09-02 STM32H723 PWR/RCC component VMState review

本轮所属层是 STM32H723 PWR/RCC 芯片层，未向 DM-MC02 machine 注册整机迁移，也未修改
`trobot/`。producer 是 PWR/RCC 的两个 CPU-visible 寄存器镜像、RCC readiness 边界
之后的 effective `system_clock_source` 和 ADC compatibility/configuration phase；
boundary 是 `dm_mc02_pwr_rcc_vmstate()`；consumer 是未来的时钟/外设联合恢复流程。

首个实际错误出现在隔离构建边界而非 VMState 字段：单测链接了完整 PWR/RCC MMIO
producer，却没有提供 `memory_region_init_io`。同时测试缺少 `migration/vmstate.h`，导致
save/load API 被隐式声明。修复是增加窄链接桩并补齐公开声明；没有把完整 QEMU memory
实现错误地拉入组件测试。

当前普通 version-1 描述保存 `pwr_regs[0x400]`、`rcc_regs[0x400]`、
`system_clock_source` 和 `adc_clock_configured`。`MemoryRegion`、clock callback/opaque、
QOM owner 和板级 wiring 均是 runtime-only。post-load 在完整字段恢复且 source `0..3`
通过校验后，只调用一次 `dm_mc02_pwr_rcc_sync_runtime()`；raw 描述共享字段但不执行
副作用，供未来 parent composite 按明确顺序使用。普通状态的失败仍可能已经写入字段，
不宣称事务级 rollback。

验证结果：隔离 VMState `4/4`（含 raw 无通知、非法 source 和截断流）；PWR/RCC、零
有效时钟、TIM2 动态改频、USART/FDCAN kernel-clock 和备用 profile smoke 通过；完整
QEMU smoke `89/89`、串行 Host CTest `54/54`、`qemu-system-arm` 重链和 `git diff --check`
通过。截断负向用例的 VMState load error 日志是预期诊断，不代表测试失败。

残余风险：当前 validator 只约束 effective source 枚举，不验证全部 H723 保留位/寄存器
组合，也没有模拟 PLL/振荡器 settling、CSS、低功耗或整机 snapshot。后续必须先分别
建立 DMA/定时器/ADC/UART/FDCAN 与 PWR/RCC 的联合恢复顺序和直接边界测试，再考虑
machine-level VMState；不能由板级或 UI fallback 掩盖这些缺口。

# 2026-09-02 ADC12 common CDR DMA overrun request-gate review

本轮继续留在 STM32H723 芯片层，完成 ADC12 common 到 DMA 的单一边界切片。
producer 是 ADC1/ADC2 regular pair；boundary 是 `DUAL=0x6 + DAMDF=0x2` 下
common CDR 的 data-ready request；consumer 是已有 ADC1 request 9 的 endpoint 或
CDR MMIO DMA 路径。

首个错误状态来自缺失的 common gate：ADC-local `adc_emit_sample()` 已能在 OVR 时
停止 ADC_DR request，但 common pair completion 仍会无条件调用 CDR data-ready，因而
在 ADC OVR 后可能继续推进 CDR DMA。修复位于 common producer/boundary：CDR 仍写入
最新 packed pair；任一 status source 的 OVR bit 置位时仅抑制 data-ready callback。
common 不缓存 OVR，也不修改 ADC ISR；CDR read 继续通过既有 callback 清两路 EOC，
ISR W1C 由 guest 清除 OVR，随后下一 pair 恢复 request。

审查特别保留 DAMDF=3 的独立语义。其 common accumulator 在第二个 pair 前可能看到
ADC EOC 仍置位；本 gate 若无条件覆盖该路径会把合法 partial CDR word 误判为停止，
因此实现只接受精确的 `DUAL=0x6/DAMDF=0x2`。

变更文件：`hw/arm/dm_mc02_adc_common.[ch]`、`tests/unit/test-dm-adc-common.c`、
`tests/qtest/dm-mc02-adc-test.c` 及同步的 `PLAN.md`、`INTERFACES.md`、
`ARCHITECTURE.md`；未修改 `trobot/`。

验证：common unit `15/15`；endpoint/MMIO direct qtest `2/2`。完整 ADC qtest、
QEMU smoke、Host CTest 和最终 system target 重链将在本切片收尾时运行；当前不据此
宣称其它 dual/DAMDF、精确 DMA 物理仲裁或 machine-level migration 已支持。

# 2026-09-02 ADC12 CDR2 regular-interleaved DMA boundary review

本轮所属层是 STM32H723 ADC12 common 芯片层到 DM-MC02 DMA 直接组合边界，未向
外部仿真器、Web/UI 或 `trobot/` 扩展。审查对象的 producer/boundary/consumer
契约如下：

- producer：ADC1/ADC2 完成 regular rank 后，通过现有的板卡无关 regular-sample
  回调提交 `source`、16-bit result、rank 和单调虚拟时间；common 不读取 ADC 私有状态。
- boundary：`DmMc02AdcCommon` 在 `DUAL=0x7` 或 `DUAL=0x3` 且 `DAMDF=0x2` 时更新
  单一 `CDR2.RDATA_ALT`，发出 `DmMc02AdcCommonCdr2DataReady`，并保留
  `cdr2_valid/cdr2_source` 供成功读取确认。后到事件覆盖前值，不建立无界队列。
- consumer：DM-MC02 仅在 ADC1 regular DMA 已启用时，将 request 9 路由到
  `ADC12_COMMON.CDR2`。endpoint DMA 成功后显式调用
  `dm_mc02_adc_common_notify_cdr2_read()`；MMIO DMA 通过 common 的 CDR2 read hook
  走同一边界。确认只清除产生当前 CDR2 值的 ADC 的 EOC。

VMState 版本从 2 升到 3，新增 CDR2 validity/source 字段；v1/v2 兼容路径保留，
回调、`MemoryRegion` 和 QOM ownership 仍是运行时 wiring，没有错误地注册为整机
migration。`INTERFACES.md`、`ARCHITECTURE.md` 和两个 `PLAN.md` 已同步该契约及
限制。

验证结果：

- `test-dm-adc-common`: `13/13`；覆盖 data-ready/read-ack、单寄存器覆盖、源特定
  确认和 VMState CDR2 source round-trip。
- `dm-mc02-adc-test`: `57/57`；新增 endpoint 与 MMIO CDR2 DMA gate 均通过，覆盖
  `DUAL=0x7/0x3`、连续两个 32-bit beat、NDTR 清零和 DMA TC。
- `tools/run-qemu-smoke-suite.sh`: `89/89`；串行 `ctest --test-dir build/host
  --output-on-failure -j1`: `54/54`。
- `ninja -C build/qemu tests/unit/test-dm-adc-common tests/qtest/dm-mc02-adc-test
  qemu-system-arm`：通过；本轮 `trobot/` 无新增修改。

残余风险和下一道门：`DAMDF=0x3`/8-bit packing、其它 dual/DAMDF 模式、injected
common-data packing、精确 DMA arbitration/physical transfer timing，以及
common/ADC/DMA 联合 machine-level migration 仍未实现。CDR2 是 single-register
语义，失败 DMA 不确认旧值，后续 producer 事件会覆盖它；这不是可回放的 DMA FIFO。
下一步仍留在 ADC 芯片层，先为另一个明确 data-format 或时序边界建立隔离契约，
再进入更高层。

# 2026-09-01 DMA stream VMState component review

本轮继续留在 STM32H723 芯片层，选择 DMA stream 作为 DMAMUX 之后的状态
producer。DMA 下一次寄存器可见行为不只依赖 `regs[0x400]`：循环/双缓冲
需要 reload tuple 和两个 live memory cursor，FIFO 模式还需要每个 stream 的
字节缓存、head 和 length。现已由 `dm_mc02_dma_vmstate()` 统一保存这些字段。

边界审查将 `MemoryRegion`、板级 `dmamux_channel_offset`、stream enable
callback、IRQ handle、request-stream cache 和派生 IRQ 电平排除在状态流之外。
回载先验证 16-bit NDTR shadow、FIFO head/length 不变量，再通过
`dm_mc02_dma_sync_runtime()` 使 request cache 失效并按目标 IRQ wiring 重建
电平投影；截断或非法 FIFO 流不会触发该副作用。

隔离 `test-dm-dma-vmstate` 通过 TAP `3/3`，覆盖寄存器/reload/cursor/FIFO
完整 round-trip、runtime cache/IRQ 重投影、截断流和非法 FIFO 拒绝。直接
consumer 的 `run-dma-smoke.sh`、`run-dma-arbitration-smoke.sh`、
`run-dma-batch-smoke.sh` 均通过，生产 `qemu-system-arm` 重链和串行 Host CTest
`54/54` 也通过。

该切片没有注册 machine-level VMState，不能宣称整机 snapshot/migration。
DMA/DMAMUX 联合回载顺序、endpoint 外部状态、异步总线时序和未来 timer-owned
调度器仍需单独定义；本轮未修改 `trobot/`，没有活跃子代理或残留构建任务。

# 2026-09-01 DMAMUX VMState component review

本轮继续留在 STM32H723 芯片层，选择 DMAMUX 作为下一个纯寄存器状态 producer。其下一次
DMA 请求选择所需的组件状态是完整 `regs[0x400]` 镜像和用于 DMA request-stream cache
失效的 `generation`；`MemoryRegion` 是运行时 wiring，不进入状态流。generation 是本地
缓存一致性代际，不伪装成 H723 硬件寄存器。

边界审查确认 DMAMUX 写入和 reset 都会推进 generation。如果只恢复寄存器而丢失该代际，
DMA consumer 可能继续使用对应旧配置的 request cache。现已新增独立的
`dm_mc02_dmamux_vmstate()`，保存寄存器和 generation；组件没有 callback、timer 或 IRQ
派生状态，因此 post-load 不执行副作用。截断状态在完成字段恢复前拒绝。

隔离 `test-dm-dmamux-vmstate` 通过 TAP `2/2`，覆盖首尾寄存器、generation round-trip
和截断流拒绝。`run-dma-smoke.sh`、`run-dma-arbitration-smoke.sh`、
`run-dma-batch-smoke.sh` 作为直接 DMA consumer 边界均通过；QEMU `qemu-system-arm`
重链通过。

该切片仍未注册 machine-level VMState，不能宣称整机 snapshot/migration。DMA stream 的
live/reload address、FIFO、IRQ、endpoint callback 及 DMAMUX/DMA 联合回载顺序仍未覆盖，
下一道门继续在 STM32H723 芯片层审计 DMA component state contract。本轮未修改
`trobot/`，没有活跃子代理或残留构建任务。

# 2026-09-01 EXTI VMState component review

本轮继续留在 STM32H723 芯片层，选择 EXTI 作为带输入历史和 IRQ 派生状态的最小
VMState producer。其下一次寄存器可见行为依赖完整 `regs[0x400]` 镜像和 16 条输入线的
`line_level` 采样；`MemoryRegion`、七个 IRQ handle 以及由 `PR1 & C1IMR1` 派生的
`irq_level[7]` 是运行时 wiring/派生状态，不进入迁移流。

首个边界检查确认回载后不能沿用目标端旧 IRQ 电平：如果 pending 或 mask 已改变，目标
NVIC 连接可能保持错误投影。现已让 `dm_mc02_exti_post_load()` 调用
`dm_mc02_exti_sync_runtime()`，按恢复的寄存器状态重算七组 IRQ，并强制驱动已连接的
handle；采样电平被保存，以便回载后的下一次 GPIO 变化仍能正确识别上升/下降边沿。截断
或版本错误的流在字段完成前拒绝，因此不会执行该 runtime-sync。

隔离 `test-dm-exti-vmstate` 通过 TAP `2/2`，覆盖寄存器与采样电平 round-trip、IRQ 重投影
以及截断流拒绝。`tools/run-exti-smoke.sh` 通过真实 Cortex-M7 guest，覆盖软件触发、
GPIO 输入边沿、PR W1C 清除和运行中 SYSCFG 重映射。QEMU `qemu-system-arm` 重链通过；
独立 QEMU smoke suite `89/89`、串行 Host CTest `54/54` 均通过。

该切片未注册 machine-level VMState，不能宣称整机 snapshot/migration；完整 H723 EXTI
line/event、安全域、CPU2 投影和所有 reset 细节仍未完成。QEMU 上游已有的 warning 不在
本切片修改路径。本轮未修改 `trobot/`，没有活跃子代理或残留构建任务。

# 2026-09-01 STM32H723 internal Flash VMState component review

本轮继续在 STM32H723 芯片层选择一个可独立验证的状态 producer。`DmMc02Flash` 的
迁移状态只有 `regs[0x400]`、主 Flash 解锁序列 `key1_seen` 和 option-byte 解锁序列
`optkey_seen`；`MemoryRegion`、QOM/owner wiring、`storage` 指针、storage bytes、
`program_window` 和派生字段 `program_enabled` 均不属于该组件的 VMState。

首个边界检查确认编程 overlay 不能作为独立持久状态保存：它是由 `FLASH_CR1.LOCK/PG`
决定的运行时映射。现已将 `FLASH_CR1` 写路径和 VMState post-load 都收敛到
`dm_mc02_flash_sync_runtime()`；post-load 只有在完整字段加载成功后才重建 overlay，
截断流不会触发 callback 或改变目标运行时连接。warm reset 保留 caller-owned Flash
内容，但重置控制器寄存器、解锁标志并关闭 overlay。

隔离 `test-dm-flash-vmstate` 通过 TAP `3/3`，覆盖寄存器/解锁状态 round-trip、锁定
状态关闭 overlay 和截断流拒绝。截断用例会按 QEMU VMState 约定输出预期的 load error，
但不会使测试失败。直接 `tools/run-flash-smoke.sh` 通过，验证编程的 NOR 1->0 规则、
sector erase、EOP/错误清除、warm reset relock 和 overlay 关闭；`qemu-system-arm`
重链、QEMU smoke suite `89/89`、串行 Host CTest `54/54` 均通过。

本切片仍未注册到 DM-MC02 machine，不能宣称整机 snapshot/migration。片上 Flash
backing RAM 的 machine-level 状态边界、真实 latency/WIP、ECC、option bytes 和掉电
语义仍未实现。`DmNorFlash` 生产路径的旧文档也已明确标为 host test fixture，生产
OCTOSPI 继续使用 QEMU `m25p80`/`DmMc02SsiNor`。本轮未修改 `trobot/`。

# 2026-09-01 GPIO VMState component review

本轮继续在 STM32H723 芯片层选择无动态队列的 GPIO producer。`DmMc02GpioBank` 的可迁移
状态是 8 个 CPU-visible 寄存器镜像：`MODER`、`OTYPER`、`OSPEEDR`、`PUPDR`、`IDR`、
`ODR`、`AFR0` 和 `AFR1`。`bank_index` 是板级/profile 元数据；`MemoryRegion`、ODR
callback 和 opaque 是运行时 wiring，均未进入迁移流。

独立 `dm_mc02_gpio_vmstate()` 使用 version 1。成功回载后在寄存器全部恢复的前提下调用
目标 callback 一次，让 GPIO 直接消费者重新派生 LED、蜂鸣器、电源、片选和收发器状态；
版本错误或截断流在边界处拒绝且不触发 callback。首次收尾审查发现 VMState 源文件未被
ARM 生产源列表编译，根因是构建边界遗漏而非 VMState 字段错误，已补入
`hw/arm/meson.build` 并重新重链。

验证：`test-dm-gpio-vmstate` TAP `2/2`、`tools/run-gpio-smoke.sh`、
`ninja -C build/qemu qemu-system-arm`、完整 QEMU smoke suite `89/89`、串行 host CTest
`54/54` 均通过。未修改 `trobot/`。

该切片仍只提供 component contract，不注册 machine-level VMState；不能据此宣称整机
snapshot/migration，也不能宣称完整 H723 pad 电气争用、模拟输入、锁存/复位细节已实现。
下一道门继续留在芯片层，逐项审计其它外设动态状态。

# 2026-09-01 FMC VMState component review

本轮继续在 STM32H723 芯片层选择无动态连接的纯寄存器 producer。`DmMc02Fmc` 当前模型
只有 `regs[0x400]` 和运行时 `MemoryRegion`；FMC 外部 memory window、访问时序和中断
尚未建模，因此没有其它状态可以合法进入迁移流。独立的
`dm_mc02_fmc_vmstate()` 只序列化寄存器镜像，不保存 `MemoryRegion` 或 QOM ownership。

验证分为两道边界：`test-dm-fmc-vmstate` TAP `2/2` 通过，覆盖首/中/尾字节 round-trip
和截断 QEMUFile 拒绝；`dm-mc02-fmc-test` 通过真实 machine MMIO，覆盖 32-bit 读写、
byte-lane 写入和 warm reset。`qemu-system-arm` 重链、board-profile smoke、完整 QEMU
smoke suite `89/89` 和串行 host CTest `54/54` 通过。未修改 `trobot/`。

该切片仍只提供 component contract，不注册 machine-level VMState；不能据此宣称整机
snapshot/migration，也不能宣称 FMC 外部存储、总线时序或完整 H723 位语义已经支持。
下一道门是 GPIO 组件状态审计。

# 2026-09-01 SYSCFG VMState component review

本轮继续在 STM32H723 芯片层选择纯寄存器 producer。`DmMc02Syscfg` 的可迁移状态只有
`regs[0x400]`；`MemoryRegion`、`changed` 函数指针和 opaque 是运行时 wiring。首次窄构建
发现单测错误地调用了 SYSCFG MMIO 实现中的 helper，导致隔离目标链接失败；根因是测试
越过 VMState 边界而不是模型行为错误，已改为直接验证公开的 post-load 通知契约，未把整套
MMIO dispatch 拉入单测。

`dm_mc02_syscfg_post_load()` 在版本有效且寄存器已恢复后调用目标 callback 一次，使板级
consumer 可以重新读取 `EXTICR1..4` 并重建 EXTI 路由；回调不进入迁移流。独立
`test-dm-syscfg-vmstate` TAP `2/2` 通过，覆盖完整 round-trip、EXTICR 字节、通知和截断流
拒绝；直接 consumer `run-exti-smoke.sh`、QEMU smoke suite `89/89`、串行 Host CTest
`54/54` 通过。QEMU/upstream 既有 warning 仍与本切片无关，未修改 `trobot/`。

该切片仍只提供 component contract，不注册 machine-level VMState；SYSCFG 的完整 H723
位掩码、安全域和其它控制语义，以及整机迁移所需的 RAM/IRQ/定时器/总线状态仍未宣称支持。

# 2026-09-01 DBGMCU VMState component review

本轮先审计下一个最小状态 producer。首次审查发现 DBGMCU 只是通用字节窗口，复位后
`IDCODE` 返回 0，与 H723 Renode register probe 和固件 ADC 版本探测所需的
`0x20030483` 不一致。根因位于 DBGMCU 芯片层寄存器 consumer，而不是 VMState 或测试层。

现已将 `IDCODE` 固定为只读硅片身份，保留其它控制寄存器的可变镜像；VMState 只保存
该镜像，排除 `MemoryRegion` 与 QOM owner。`test-dm-dbgmcu-vmstate` 通过完整 round-trip
和截断流拒绝，`tools/run-dbgmcu-smoke.sh` 通过真实 Cortex-M7 IDCODE、写保护和控制
寄存器读回。该切片仍未注册 machine-level VMState，不宣称整机 snapshot/migration。

# 2026-09-01 RNG VMState component review

本轮选择 RNG 作为带有界运行态的底层 producer。下一次寄存器可见行为依赖寄存器镜像、
`prng`/`seed`、四字 FIFO、`status`、FIFO 游标和空轮询 refill marker；`MemoryRegion` 与
`qemu_irq` 只是运行时 wiring，未进入 VMState。`dm_mc02_rng_post_load()` 只检查固定容量
和 marker/count 的可观测不变量，没有把 IRQ 或其它外部连接伪装成可迁移字段。

验证：`test-dm-rng-vmstate` TAP `2/2`；`tools/run-rng-smoke.sh` 通过；完整
`qemu-system-arm` 重链、独立 QEMU smoke suite `88/88`、串行 host CTest `54/54` 均通过，
且 `qemu/upstream` 的 `git diff --check` 通过。未修改 `trobot/`。

该证据只覆盖 RNG component contract 和现有寄存器数据面；machine-level save/load 仍被
禁止，后续必须继续覆盖动态 timer、IRQ、DMA、总线和 co-sim 状态。

# 2026-09-01 CRC VMState component review

本轮选择无动态连接的 CRC producer：`DmMc02Crc` 的下一次寄存器可见行为只依赖完整的
`regs[64]` 镜像和 `value` 累加器。独立的 `dm_mc02_crc_vmstate.c` 只暴露这两个状态字段，
排除 `MemoryRegion` 与其它运行时 wiring；该设备没有 timer、IRQ callback、DMA 指针或外部
总线连接，因此不需要在组件契约中引入生命周期或取消语义。

验证：`test-dm-crc-vmstate` TAP `1/1`；`tools/run-crc-smoke.sh` 通过；
`ninja -C build/qemu qemu-system-arm` 完整重链通过；独立 QEMU smoke suite `88/88`、串行
host CTest `54/54` 均通过，且 `qemu/upstream` 的 `git diff --check` 通过。未修改
`trobot/`。

该证据只覆盖 CRC component contract 和现有寄存器数据面；machine-level save/load 仍被
禁止，后续必须继续覆盖动态 timer、IRQ、DMA、总线和 co-sim 状态后才能设计整机迁移门。

# 2026-09-01 CORDIC VMState component review

本轮选择最小的 VMState producer：`DmMc02Cordic` 只有 CSR、两个参数、参数计数、两个
结果和结果计数，没有动态 timer、外部连接、DMA 指针或板级状态。序列化边界由独立的
`dm_mc02_cordic_vmstate.c` 提供，避免为了测试该状态契约而链接完整 system MMIO
dispatch；`MemoryRegion` 和 QOM owner 明确排除在 wire 之外。

加载后的首个错误边界是固定数组游标：`arg_count`/`result_count` 超出数组容量时返回
错误，不能让损坏快照形成越界队列状态。测试使用 QEMUFile，保存流包含
`QEMU_VM_EOF`，覆盖完整字段 round-trip 和非法计数拒绝；此前窄测失败是测试流缺少 EOF
导致的读取错误，已在测试 producer 侧修复。

验证：`test-dm-cordic-vmstate` TAP `2/2`；`ninja -C build/qemu qemu-system-arm` 完整
重链通过；独立 QEMU smoke suite `88/88`、串行 host CTest `54/54` 均通过，且
`qemu/upstream` 的 `git diff --check` 通过。未修改 `trobot/`。
该证据只覆盖 CORDIC component contract；machine-level save/load 仍被禁止，片上 RAM、
其它外设、动态 timer、CanBus/USB/CharBackend 和 co-sim 队列尚未具备完整迁移语义。

# 2026-09-01 W25Q64 -> QEMU m25p80 migration review

本轮完成 `OCTOSPI2 -> SSI -> w25q64/m25p80` 的生产迁移。producer 是
`DmMc02Ospi` 的寄存器事务和控制器侧 staging/DLR 边界；boundary 是板卡无关的
`DmMc02SsiNor`（SSI bus、active-low CS、字节 transfer、reset 和 realized geometry/ID
校验）；consumer 是 QEMU v8.2.2 标准 `m25p80`，由其拥有 Flash backing storage。
memory-mapped alias 和 raw-image persistence 只使用窄的 realized storage view，未复制
NOR command/state machine。

首次 QEMU 重链失败定位为新 SSI adapter 漏含 `qapi/error.h`，表现为 `error_setg`
链接未定义；补齐 producer/boundary 的错误接口依赖后重链通过。随后 OSPI 首次直接 smoke
暴露两个 m25p80 consumer 与 W25Q64 不一致：成功 erase 没有清 WEL，且非对齐 block
erase 从传入字节开始而不是擦除包含该地址的整块。已在通用 m25p80 操作完成边界修复：
成功 page program/erase 清 WEL，4K/32K/64K（以及已有 die）擦除按粒度向下对齐；被
OCTOSPI DLR 检查拒绝的事务不会到达 die，仍保留 WEL 和 storage。

生产 Meson ARM target 已移除 `cosim/dm_nor_flash.c` 依赖；旧实现只在显式
`DM_MC02_OSPI_TEST_FIXTURE=1` 的两个 host OSPI 测试目标中编译，不能作为运行时 fallback。
`include/hw/block/flash.h` 新增的 accessor 仅返回已 realize 的 storage、size 和 JEDEC
identity，用于 board alias/persistence，不暴露 m25p80 私有命令状态。

验证证据：`ninja -C build/qemu qemu-system-arm` 通过；`run-ospi-smoke.sh` 通过并明确
断言 page-program WEL 清除、W25Q64 JEDEC/8 MiB 几何、读写/擦除、memory map、DLR
拒绝、DMA endpoint 和 persistence；`run-board-profile-smoke.sh` 通过；独立 QEMU smoke
suite `88/88` 通过；host CTest 串行 `54/54` 通过。`git diff --check` 通过，`trobot/`
无改动。

残余风险：m25p80 和 OCTOSPI 仍是同步 functional model，没有真实 Flash latency、异步
WIP、suspend/resume、ECC、block protection、掉电中止或完整 line/DTR 时序；QEMU 上游
`vhost_svq_poll()` 的既有未初始化 warning 仍存在且不属于本切片。v8.2.2 本地 m25p80
补丁在未来升级 QEMU 时必须重新对照上游审计。下一道门是 FDCAN 接入 QEMU standard CAN
bus，仍需先做独立 adapter/differential gate。

# 2026-09-01 RCC unclocked state propagation review

首个错误状态位于 RCC -> board clock boundary：`dm_mc02_pwr_rcc_*_timer_clock_hz()`
在当前 effective source 失效时正确返回 `0 Hz`，但 `dm_mc02_clock_changed()` 只在旧
逻辑下更新非零 APB timer clock，可能让 TIM consumer 继续使用上一频率。producer 是
RCC readiness，boundary 是 board callback，consumer 是 TIM clock object；问题不在
timer 调度器或 `trobot` 固件。

修复后 APB1/APB2 timer clock 无条件写入派生值，`0 Hz` 作为有效的 clock-readiness
状态传播到对应 timer。新增的只读 `/machine` QOM 属性
`apb1-timer-clock-hz`/`apb2-timer-clock-hz` 只用于诊断和 deterministic test oracle，
不改变运行时数据面。zero-clock fixture 请求未就绪 PLL1、关闭当前 HSI，并在 board
边界确认 APB1 timer clock 为 `0 Hz`，因此实际覆盖了本轮修复而不是仅覆盖 FDCAN 的
独立 kernel-clock 路径。

验证：QEMU 增量重链通过；`run-clock-zero-smoke.sh`、`run-pwr-rcc-smoke.sh`、
`run-tim2-clock-smoke.sh` 通过；host CTest 串行 `53/53` 和独立 QEMU smoke suite
`85/85` 通过。未修改 `trobot/`，Luna 的 smoke 改动已主代理复核并关闭。

残余风险：当前仍采用立即 readiness，不表示完整 H723 RCC；没有 oscillator/PLL lock
延迟、CSS、自动 fallback、clock gating 或全量 kernel source matrix。后续应继续在直接
consumer 层逐项补齐，不用 board-level fallback 掩盖这些缺口。

# 2026-09-01 TIM2 clock smoke producer configuration review

全量 CTest 首先失败在 `run-tim2-clock-smoke.sh` 的时间 oracle，而不是 TIM2 consumer：
RCC 已按当前契约要求 `PLL1DIVPEN`，fixture 只写 `PLL1ON`，因此 `SW=PLL1` 请求未就绪，
系统时钟保持 64 MHz HSI，更新事件约 1 s 后才出现。补齐 guest 的 `RCC_PLLCFGR` P 输出
使能位后，fixture 才验证目标边界 HSI -> PLL1，而不是依赖过期的宽松 RCC 行为。

修复后 `run-tim2-clock-smoke.sh` 通过，首次 PLL1 切换后的更新延迟为 `113.906 ms`；
完整串行 host CTest `53/53` 和独立 QEMU smoke suite `84/84` 均通过。该修复只涉及
smoke fixture 和记录文件，未修改 `trobot/`。

# 2026-09-01 H723 D1 HPRE/HCLK -> ADC synchronous clock review

本轮将 H723 D1 时钟树中的 CPU 分频与 HCLK 分频提取为板卡无关的
`dm_stm32h7_clock_tree` helper。producer 是 RCC 的有效 SYSCLK 和 `D1CFGR`；boundary
是 CPU/HCLK 派生接口以及 RCC 暴露的 HCLK callback；consumer 是 ADC12 common
`CCR.CKMODE`。这样 ADC 不再通过 CPU 频率反推 HCLK，board profile 也不承载时钟树规则。

`D1CPRE` 只作用于 CPU，`HPRE` 按 H723 有效编码产生 `/1`、`/2`、`/4`、`/8`、`/16`、
`/64`、`/128`、`/256`、`/512` 的 HCLK。ADC synchronous `CKMODE=01/10/11` 分别使用
`HCLK/1`、`HCLK/2`、`HCLK/4`。寄存器配置仍在冷路径解析，转换热路径只读取已计算的
有效时钟和当前 rank deadline。

隔离验证 `dm_stm32h7_clock_tree_smoke` 覆盖全部 HPRE 编码，并断言 D1CPRE/HPRE
独立；ADC qtest 覆盖 9 个 HPRE 编码、3 个 CKMODE 以及运行中改写 HPRE 时当前 rank
剩余时间的重排。纯 helper、QEMU 重链和完整 ADC qtest 均通过，ADC qtest 为 `40/40`。

收尾重建时发现三个 Flash smoke target 在 Release 的 `NDEBUG` 下会因 `assert` 被优化掉
而触发 `-Werror` 未使用变量错误。已在 CMake 中为这三个测试 target 显式加入 `-UNDEBUG`；
修复后的完整 host 构建和 CTest `53/53` 通过。这是测试构建契约修复，不改变 QEMU 或
Flash 运行时语义。

残余风险：当前没有完整 APB/D2/D3 分频、ADC kernel source 全矩阵、clock security、CSS、
PLL/oscillator settling 或低功耗域语义。该切片只证明 HCLK 直接消费者边界，不能宣称
完整 H723 clock-tree 兼容性；下一步应继续选择单个直接 kernel-clock consumer，并为其
建立独立边界测试。

# 2026-09-01 H723 RCC source readiness review

此前 RCC 的首个错误状态是：guest 把 `RCC_CFGR.SW` 写成尚未打开的 HSE 或尚未有效的
PLL1 后，`dm_mc02_pwr_rcc_cpu_clock_hz()` 直接按 `SW` 计算频率，`SWS` 也伪装成请求值；
这会把错误的时钟传播到 ARMv7-M、TIM 和 ADC。producer 是 RCC enable/config 状态，边界
是 `SW/SWS`，consumer 是 clock callback；问题不在 board profile 或 timer 调度。

修复后 effective system source 独立存储。RCC 只在请求源 ready 时更新它，并在 CR、PLL
配置或 CFGR 写入后重试保存的请求；PLL ready/output 同时检查输入源和分频有效性。因而未
就绪请求保持原 effective source，源就绪后再传播新频率，且不会向热路径增加配置解析。

验证：`run-pwr-rcc-smoke.sh` 通过未就绪 HSE、HSE 自动切换、无效 PLL 和有效 PLL 延迟
切换断言；`run-tim2-clock-smoke.sh`、ADC trigger smoke、`run-mc02-smoke.sh` 通过；
QEMU `build/qemu/qemu-system-arm` 已重链。该切片未修改 `trobot/`。

已知限制：就绪采用同步模型，未实现真实 oscillator/PLL settle/lock 时间、CSS、低功耗
时钟切换、备份域和完整 H723 kernel clock matrix。当前验证证明的是 modeled register and
consumer boundary，不代表完整 H723 RCC 兼容性。

# 2026-09-01 H723 USB host-channel multi-packet PIO/DMA review

本轮完成 H723 host-channel 的多包 PIO/DMA 芯片层切片。producer 是 guest 对
`HCCHAR/HCTSIZ/HCDMA/HCFIFO` 的编程以及每个 accepted packet 的结果；boundary 是
`PKTCNT/XFERSIZE`、私有 DATA toggle、PIO FIFO/DMA data-path 和 channel interrupt state；
consumer 是板卡无关的 host-channel transport。该边界没有把 QEMU USB bus、DM-MC02 pin map
或外部设备策略倒灌到 H723 模型。

实现行为是：`HCCHAR.CHENA` 上升沿从 `HCTSIZ.DPID` 初始化私有 `next_pid`；每个 accepted
packet 后只在私有状态中切换 `DATA0/DATA1`，`HCTSIZ.DPID` 保持初始编程值。PIO 和 DMA
都按当前 packet 的 accepted length 推进 `HCTSIZ`；DMA 同时推进当前 `HCDMA`，PIO 则消费
或填充对应 channel FIFO。accepted short packet 立即停止，NAK/错误不推进成功路径的
toggle。测试 fixture 的 submit callback 与 PIO callbacks 原先错误地共享 opaque owner，
已分别改为 `TransportFixture` 和 host，避免把 callback 所属对象错误传播到 data path。

定向验证记录为 host-controller unit `16/16`、host transport unit `14/14`、data-path
`3/3`、channel-control `4/4`、completion scheduler `6/6`、QEMU adapter `13/13` 和 H723
qtest `4/4`；多包用例覆盖 PIO/DMA `64+64+2` OUT、short packet stop、PID、`HCTSIZ`、
`HCDMA`、FIFO 及最终 `HCINT`。这些结果只证明上述测试覆盖的契约，不等于完整 USB Host
支持。

残余风险：源码仍没有完整 USB bus/PHY、电气时序、SOF 驱动 retry/timeout、全局 FIFO
arbitration、descriptor DMA、ISO/split、hub/topology、宿主机 passthrough 或 DM-MC02
Host board wiring。下一道门应留在 H723 芯片层，先为 retry/SOF 以及 DMA/FIFO 边界建立
独立测试，再进入 host-role board 组合。

# 2026-09-01 构建、回归与实时性复核

当前 host-controller 修改已重建到 `build/qemu/qemu-system-arm`。定向 unit 结果为
host controller `16/16`、host transport `14/14`、data-path `3/3`、channel-control
`4/4`、completion scheduler `6/6`、QEMU adapter `13/13`；项目 CTest 串行 `44/44`
（含 QEMU smoke suite）通过。

真实 Release 固件从复位运行的采样得到 `1.000165x` RTF，QEMU CPU 约 `101.8%`、RSS
约 `50 MiB`，IWDG 无超时；NullEngine 为约 `199884 IMU frames/s`，DM-MC02 启动 smoke
约 `50.021 ms`。TCG `multi`/`single` 分别为 `0.999815x`/`0.998829x`，说明当前单固件
路径的主要边界是单 vCPU/event loop，开启 TCG 多线程没有带来实测收益。

`perf stat` 因主机 `perf_event_paranoid=4` 被拒绝，尚无硬件计数器级热点证据；上述结论
仅基于 wall-clock、QEMU 自报 CPU/RSS 和 guest tick，不能替代硬件 profiler。QEMU 上游
已有的 `vhost-shadow-virtqueue.c` 未初始化警告仍存在，但不在本轮修改路径。

# 2026-09-01 H723 host SOF periodic scheduling review

本轮继续留在可复用 H723/DWC2 host-controller 芯片层。SOF 路径现在只对 ISO/interrupt
channel 按 `HFNUM` 奇偶匹配 `HCCHAR.ODDFRM` 后提交；control/bulk 不受该门控，仍按每个
SOF 对已 NAK 的 channel 自动重试。CHENA 首包和显式 `service_channel()` 没有被改成隐式
的周期调度接口。

对照 QEMU 原生 `hcd-dwc2.c` 后修正了一个代理实现错误：periodic NAK 不是自动 SOF 重试，
而是置 `NAK|CHHLTD`，由软件重新使能 channel；修正后 NAK 不改变 PID、`HCTSIZ` 或 DMA
地址。测试新增 odd/even 多包 SOF 调度与 periodic NAK halt，H723 host-controller
`18/18` 通过。

残余风险：没有 per-endpoint interval、完整 high-speed microframe bandwidth/global FIFO
arbitration、descriptor DMA、split/ISO payload、PHY 或电气时序。这是 host-controller
边界收敛，不构成 DM-MC02 Host board 或完整 USB bus 支持声明。

# 2026-08-31 USB host-port lifecycle boundary review

## 2026-08-31 STM32H7 OTG host-port lifecycle review

本轮没有将 host 逻辑塞入 DM-MC02 的现有 device-mode adapter。只读核对 `trobot` 后确认
实际固件在 `USB1_OTG_HS @ 0x40040000` 使用 CherryUSB device mode、内置 FS PHY 和
PA11/PA12；因此把 host MMIO 接进同一 board window 会制造不存在的双 role 硬件状态。

新增的 `DmStm32H7OtgHost` 只归属 STM32H7 芯片层：它以 DWC2 的真实 `HPRT0=0x440`、
`GINTSTS.PRTINT=bit24` 和 change-bit W1C 语义建模单 host port。其 `RST` 释放在 connected
port 上才通过 `DmUsbHostPort` 传递 caller 的 virtual timestamp；下游测试把 callback 连接
到 `dm_usb_dwc2_bus_reset()`，验证清理的是 USB bus runtime state，而非错误地调用 core
soft reset。这样 STM32H7 host 寄存器、通用 port lifecycle 和 device consumer 保持三个
独立可复用层。

验证：芯片/边界 unit `5/5`，相关 host-port `2/2`、DWC2 `6/6`，DM-MC02 USB qtest
`9/9`、两项 USB bare-metal smoke 与整机 QEMU suite `79/79` 全部通过；`qemu-system-arm`
重链通过。

残余风险必须保持可见：没有 `GUSBCFG` role 转换、host channels、SOF、DMA、FIFO/PHY/VBUS、
真实 USB enumeration、QEMU host bus composition 或 passthrough。当前 model 是未来
host-role board profile 的底层 producer，不改变 DM-MC02 当前 USB Device 功能，也不构成
host 支持声明。

复核此前把可选 reset callback 加到 `DmUsbHost` 的方案后，发现这会把无状态的 packet
producer 与 port lifecycle 混在同一个抽象中。QEMU `usb_port_reset()` 是 host-port 事件，
不是 SETUP/IN/OUT transaction；因此改为独立、最小的 `DmUsbHostPort`，仅传递 callback、
opaque 和调用者提供的虚拟时间戳。

该层不暴露 `USBPort`、QEMU 时钟、HPRT0 或 DWC2 寄存器。callback 缺失时同步 no-op，既不
伪造 transaction，也不推断下游状态。隔离测试 `2/2` 检查精确时间/opaque 与 no-op；host
`4/4`、DWC2 `5/5`、QEMU adapter `7/7` 回归以及 `qemu-system-arm` 重建均通过。

复核还确认已有 QEMU adapter fixture 曾把 port reset callback 直接接到
`dm_usb_dwc2_reset()`；那只能覆盖 fixture 的 core-reset 清理，不能作为 USB bus-reset
语义。下一切片必须先在可复用 DWC2 device 层单独定义 address/configuration、control phase、
PID/halt 与 pending endpoint data 的 bus-reset 状态，并在之后才由 QEMU adapter 接入。

## 2026-08-31 DWC2 bus-reset boundary and DAD review

在实现 consumer 前先检查 DWC2 register contract，发现 `DCFG.DAD` 曾错误使用 bit `0..6`。
实际 DM-MC02 `trobot` DWC2 driver 按 `addr << 4` 写入 bit `4..10`；模型与 board qtest 已
改为相同字段，qtest 从真实 SET_ADDRESS control transfer 断言该寄存器边界。未修改
`trobot/`。

随后将 USB bus reset 与 `GRSTCTL.CSFTRST` 分离。`dm_usb_control_bus_reset()` 清空 control
state 并对 consumer 发出 address/configuration=0；`dm_usb_dwc2_bus_reset()` 清空 DAD、PID/
halt、endpoint FIFO/transfer/interrupt 及 `EPENA/STALL`，但保留 core programming state，
包括 DCFG 非 DAD 位、global masks、FIFO sizing、DMA 地址和 endpoint 描述字段。该差异由
control `6/6` 和 DWC2 `6/6` 的状态断言锁定。

QEMU adapter 现在将 `usb_port_reset()` 通过 `DmUsbHostPort` 以 QEMU virtual timestamp
下发到 DWC2 bus-reset consumer；real-`USBBus` adapter test `7/7` 验证时间透传和 endpoint
runtime reset，不再把 port reset 误作全 DWC2 core reset。系统重链、DM-MC02 USB qtest
`9/9`、controller-ready/CDC pipe smoke、host CTest `22/22` 与整机 QEMU smoke `79/79` 都
通过。

残余边界明确：DM-MC02 machine 没有 USB host controller，当前 QEMU adapter 仍是 test-only
transport fixture；没有 H723 host port state/SOF、枚举、async completion/cancel、DMA、PHY、
OTG 或宿主机 USB passthrough。后续应从 H723 host-controller 层开始，不得用此 adapter
代替板级 USB host。

# 2026-08-31 DWC2 board IRQ boundary review

本轮复核了可复用 DWC2 device-mode core 到 DM-MC02 板级/NVIC 的直接边界。DWC2 core
只产生 board-independent 的 level-sensitive IRQ callback；DM-MC02 adapter 通过
`dm_mc02_usb_set_irq()` 保存 QEMU IRQ，并由 `dm_mc02_board_connect_irqs()` 按 profile
的 `irqs.usb=77` 连接到 ARMv7M external IRQ input。没有把 NVIC 逻辑或板级 IRQ 常量
写回通用 USB core。

失败定位首先发现测试读取了 `0xE000E108`，该地址是 NVIC `ISER2`，而不是 `ISPR2`。
修正为 `0xE000E208` 后，`GINTSTS.OEPINT`、NVIC pending bit、endpoint W1C 和
`ICPR2` 清除均符合预期。

首轮全量 smoke 还暴露了两个接入回归：controller-ready smoke 读不到
`CSRSTDONE`，FIFO0 CDC smoke 看不到 `RXFLVL`。前者由通用 core 的同步 reset-complete
状态补齐，后者由 adapter 在 `GINTSTS` 读取时合并 legacy FIFO 状态；没有把 raw pipe
状态写入通用 endpoint core。

验证：USB qtest `9/9`，DWC2/control/transaction/host unit 分别 `5/5`、`5/5`、
`5/5`、`4/4`；controller-ready 和 FIFO0 CDC legacy smoke 通过，host CTest `22/22`；
QEMU system build 通过。未修改 `trobot/`。

残余风险：这只证明板级 MMIO/IRQ 组合，不代表真实 USB bus、DMA、SOF、PHY、宿主机
枚举或完整 H723 device-mode 语义已经实现；下一步应先接明确 transport adapter，再
进入 QEMU USB bus 集成。

# 2026-08-31 shared v2 fixed payload codec review

本轮把 QEMU link 中五类固定 v2 payload 的字段布局抽到
`cosim/dm_mc02_v2_payload.[ch]`。共享层只依赖标准 C 和公共 little-endian wire helper，
不拥有 chardev、QEMU clock、队列、session 或 board pin 状态；QEMU link 现在只负责生成
状态快照并提交给 codec，再把完整 payload 交给既有 transport。

codec 对 RESET_ACK、STEP_ACK、DIAGNOSTICS、STEP_DONE 和 board TELEMETRY 提供固定长度
编码、解码和验证。status、capability bits、reserved fields 以及 STEP_DONE 的
consumed/missing mask 约束集中在同一边界，避免各个发送路径继续维护不同的字节偏移。
IMU、ADC、MotorCommand/MotorState section 的可变 schema 和 session/step 状态仍留在
消费者层，保持协议 framing 与板级运行状态解耦。

验证：`dm_mc02_v2_payload_smoke` 通过；QEMU 重链通过；v2 STEP、RESET、ADC-only、
motor callback、STEP_DONE、控制/遥测背压 smoke 全部通过；host CTest `20/20`、Python
`248 passed`。测试中的结构比较改为逐字段断言，避免 padding 字节影响结果。

残余风险：IMU 与 MotorCommand/MotorState 变长 section payload 仍由消费者分别校验，
完整跨进程可靠锁步不属于本切片；固定 response 的 C/Python 向量门已在后续切片补齐。

# 2026-08-31 shared v2 ADC section payload review

本轮把 ADC_INPUT 和 ADC_PIN_VOLTAGE 的固定 section 布局收敛到
`cosim/dm_mc02_v2_payload.[ch]`。共享 codec 只负责协议字段、边界和 little-endian
序列化，不依赖 QEMU ADC 寄存器、board profile、时间队列或 plant；QEMU link 通过
decode 结果生成既有 `DmMc02CosimAdcSample`，因此 ADC 模型仍是独立 consumer。

验证覆盖 channel 0/31、raw 最大值、3.3 V 上限、PIN_OVERRIDE、reserved/flags/电压
越界拒绝。ADC section smoke、QEMU 重链、ADC-only/混合 STEP/reset/motor v2 smoke、
host CTest `22/22` 和 Python `255 passed` 均通过。没有改变 v1 payload 或 ADC 芯片层
的触发/采样时序。

残余风险：IMU 与 MotorCommand/MotorState 仍是变长 schema，ADC 其它外部触发源和严格
跨进程锁步仍需按芯片层/协议层边界分别实现，不能由本 codec 的 fixed-section 验证替代。

# 2026-08-31 shared v2 wire codec/section parity review

本轮复核了 v2 framing 从 QEMU link/Python 两份实现抽出的协议层边界。producer 是完整
v2 frame body，`cosim/dm_mc02_v2_wire.[ch]` 负责 magic、36-byte header、little-endian
字段、payload 上限和 section 的结构遍历；outer `u32 body_len` 仍由 transport 负责。
该 codec 不依赖 QEMU、DM-MC02 pin map、外设模型或 plant，因此可以被其它 board profile
和外部工具复用。

section iterator 只验证 type/flags/length 的结构约束，不把 RESET/STEP ACK、session
状态或 typed payload 语义塞进底层 codec。60-byte compact IMU 与 section payload 的
歧义通过显式 section marker 处理；当 compact payload 的判别字段与 section marker
冲突时，底层拒绝该模糊输入，不会静默把它当成 section header。

验证包括 C `v2_wire_smoke`、C/Python 完整 golden-vector parity、QEMU v2 STEP/RESET/
motor callback/STEP_DONE smoke 和 full QEMU suite；host CTest `19/19`、Python
`248 passed`、QEMU suite `79/79` 均通过。该切片没有改变 v1 wire 或 typed consumer 的
session/重试语义。

残余风险：typed payload 编解码/校验、跨进程可靠传输、多步窗口和严格外部锁步仍由
后续层负责；当前 parity fixture 覆盖 framing/section 代表性向量，不等于完整消息种类
的 schema coverage。

# 2026-08-31 shared v1 wire codec integration review

本轮回归确认共享 v1 codec 没有引入功能或实时性回退：native link、timing、worker
smoke、独立 `dm_mc02_wire_smoke`、host CTest `17/17` 和 Python `243 passed` 均通过。
当前实测 NullEngine
`195701 frame/s`，真实 Release 固件 RTF `0.999700x`，CPU `113.7%`，RSS
`50304 KiB`，IWDG timeout `0`；性能数字受宿主机瞬时负载影响，不能替代长期基线。

本轮将 v1 co-sim frame 的公共边界抽到 `cosim/dm_mc02_wire.[ch]`。host
`dm_mc02_protocol` 和 QEMU chardev link 现在共用 header/body 的 little-endian
编解码、精确 payload 长度和 typed payload 合法性检查；transport、QEMU 队列、
虚拟时间映射及回调分发仍保持在各自 consumer 所属层。codec 不依赖 QEMU 内部头文件，
QEMU 只通过项目源码树编译同一份纯 C source，因此该边界可被其它 board profile 或
外部工具复用。

迁移中曾把 payload 语义检查放进 encode/decode，导致既有 host 契约中“可编码非法
payload、再由 validate_frame 拒绝”的 NaN 回归；已改为结构 codec 与显式 payload
validator 分离，并由 host/QEMU 各自在提交或 dispatch 前调用 validator。该行为已由
host protocol/transport smoke 和 QEMU-native co-sim、timing、worker smoke 复核。

残余风险：typed v2 payload 仍由消费者分别校验，跨进程可靠传输、多步发送窗口和完整
消息 schema coverage 仍未完成；不能把 framing parity 误认为完整 v2 协议解耦。

# 2026-08-31 USB endpoint packet queue integration review

本轮复核了可复用 USB packet queue 到 DM-MC02 内部 endpoint harness 的直接边界。
queue 本身只负责固定容量、packet 边界、方向/类型元数据和 virtual timestamp；它不依赖
DM-MC02，也不把 USB token、PID、toggle 或 PHY 状态塞进通用接口。QEMU build、queue
unit test `5/5`、USB qtest `5/5`、legacy controller-ready/CDC pipe smoke 和 host CTest
`16/16` 均通过。

EP1..EP5 的 adapter 按 64-byte MPS 进行 RX 分包；TX 先进入 endpoint-specific pending
buffer，在 IN request 时转为 queue packet。qtest 精确验证了 130-byte TX 按 `64/64/2`
输出，以及此前的 EP1 RX 4 KiB FIFO 边界和 EP2..EP5 endpoint 完成状态。queue 满或
pending buffer 截断现在累计内部丢弃字节，chardev 初始化失败释放已分配 queue/timer。

残余风险：当前 endpoint 仍只存在于私有 qtest packet harness，serial slot 10 仍是
FIFO0 原始 byte pipe；没有真实 USB bus attachment、宿主机枚举、token/data toggle、
NAK/STALL、DMA descriptor、PHY、电气时序或完整 controller state machine。RX queue 满
时保留前缀并丢弃后续输入是显式兼容策略，但不是总线级 backpressure，后续应在 USB
transaction 层定义并测试该行为后再向宿主机 USB 集成。

# 2026-08-31 DMA FIFO + DBM + endpoint 组合复核

本轮完成 STM32H723 DMA 芯片层的组合边界。producer 是 DMAMUX 选中的 peripheral
request 和 DMA FIFO/DBM 状态机，boundary 是同步的 `DmMc02DmaEndpoint` 单 beat
callback，consumer 是 endpoint 字节序列以及 DMA 的 `NDTR/CT/M0AR/M1AR/HT/TC` 状态。
host fixture 覆盖 M2P 与 P2M：M2P 验证 16-bit memory beat 经 FIFO 按 8-bit peripheral
beat 有序交付，P2M 验证 8-bit endpoint beat 按 threshold 组装为 16-bit memory beat；
两条路径均验证 M0/M1 切换、阈值对应的 FS 状态、FIFO 清空和 timestamp 保持。

验证：`cmake --build build/host -j2`、ASan/UBSan 组合测试、host CTest `16/16`、
`run-dma-fcr-smoke.sh`、`run-dma-dbm-reconfigure-smoke.sh`、`run-tim8-dbm-smoke.sh`
和 `tools/build-qemu.sh` 均通过。过程中曾发现测试把 FIFO 7/16 占用错误期待为 FS
3/4；实际 7 字节属于 1/2 档，已修正测试 oracle，没有修改 DMA 实现来迁就错误预期。

残余风险：这是同步、确定性的 FIFO data path，不是完整 DMA 总线模型；真实 FEIF 触发
条件、`MBURST/PBURST`、时钟级仲裁、异步 endpoint backpressure、per-beat rollback、
M2M DBM 和完整错误恢复仍未实现。已有 UART/ADC/SPI endpoint 集成不自动获得这些组合
语义，必须继续通过独立芯片层测试和直接消费者边界测试逐级接入。

# 2026-08-31 DMA DBM inactive-buffer reconfiguration review

本轮修复了 STM32H723 DMA 双缓冲运行时改址的芯片层边界。DBM/EN 运行期间写入
`M0AR/M1AR` 时，寄存器继续表示配置基址，当前活动目标的私有 cursor 不被改写；
非活动目标的 reload base 和 cursor 更新为新地址，下一次 `CT` 切换从新 buffer 开始。
DMA 仍独立拥有 `NDTR`、CT、HT/TC、DMAMUX 和 endpoint 交付状态，consumer 不访问
DMA 私有 cursor。

验证：新增 `run-dma-dbm-reconfigure-smoke.sh`，覆盖 M0 -> M1 -> 运行中重写 M0
-> M0 的直接 peripheral-request 边界，断言 DBM/CT、NDTR、配置基址、HT/TC 和
替换 buffer 的首个 beat；原有 `run-tim8-dbm-smoke.sh`、DMA FIFO、UART、SPI、ADC
相关 smoke 均通过，QEMU 已重链。

残余风险：本轮没有实现 M2M DBM、`MBURST/PBURST`、真实总线仲裁；异步 endpoint
backpressure 和 per-beat rollback 仍未实现。

# 2026-08-31 ADC JAUTO automatic conversion review

本轮在 STM32H723 ADC 芯片层实现 `CFGR.JAUTO` 的最小可复用语义。regular sequence
的 EOS 是 producer，ADC 内部以已提交的 injected `JSQR` context 作为边界，injected
结果和状态寄存器是 consumer；DM-MC02 board route、timer callback 和 worker 均未参与
该状态转换。

JAUTO 仅接受 `JEXTEN=0` 的软件 injected context，并拒绝 `DISCEN/JDISCEN` 组合。非连续
regular start 只自动执行一组 injected sequence；连续 regular 在自动 injected 完成前
暂停，随后从下一 regular sequence 恢复。`JADSTP` 中止自动 injected 时会清除等待状态，
恢复连续 regular；`AUTDLY` 仍要求 guest 先读取 `ADC_DR`，因此不会越过数据消费边界。

验证：重编译 `qemu-system-arm` 后，ADC qtest 全量 `36/36` 通过；新增用例覆盖
`jauto-regular-to-injected`、JAUTO 关闭、连续模式 `JADSTP` 恢复以及
`JAUTO+AUTDLY` 的 DR 消费边界。此前一次
误判来自只重编译 qtest、未重链 QEMU 主体，已定位并修正验证流程，不是设备模型行为。

残余风险：当前尚未以真实 guest/DMA fixture 覆盖 `JAUTO+AUTDLY` 和 `JAUTO+JQM` 的组合，
也未实现 regular low-power auto-power-off、完整 injected 外部 source matrix。不能据此
宣称 H723 ADC 的完整 JAUTO 或低功耗一致性。

# 2026-08-31 UART DMA endpoint integration review

UART1 and USART2 now use the reusable DMA endpoint boundary by default. The
UART producer/consumer owns only the `RDR` FIFO-consumption and `TDR` write
side effects; the DMA chip model remains responsible for DMAMUX selection,
stream arbitration, live address movement, NDTR, HT/TC, TE, and IRQ state.
The explicit `uart-dma-endpoint=off` machine property keeps the old MMIO path
available for compatibility comparison.

The first implementation also exposed a build-boundary problem: a host test
that constructed `DmMc02Dma` directly attempted to link several QEMU private
static archives. Those archives are internal composition artifacts rather than
a supported standalone test interface, so the target produced multiple and
undefined symbol failures. The target and its CTest registration were removed;
the board-independent `dm_mc02_dma_endpoint_smoke` remains the isolated
callback contract test, while UART controller behavior is verified by the real
guest fixtures `run-uart-dma-smoke.sh` and `run-uart2-dma-smoke.sh`, each in
both endpoint and legacy modes.

Residual risk: endpoint transfer is still synchronous. DMA FIFO
packing/unpacking and threshold handling now invoke the endpoint for each
peripheral-width beat, while DMA retains FIFO and transfer status ownership.
Basic DBM inactive-buffer reconfiguration is covered separately; FIFO+DBM
endpoint staging, asynchronous backpressure, per-beat rollback, and complete
`ReceiveToIdle_DMA` event-size behavior remain unsupported or unverified.

# 2026-08-31 ADC1 DMA endpoint integration review

ADC1 is now the first real consumer of the reusable DMA endpoint contract. The
ADC producer submits a P2M request with the current virtual timestamp; the
endpoint callback reads `ADC_DR`, clears EOC, resumes an AUTDLY-held conversion,
and lets the common DMA state machine commit the memory beat. This keeps ADC
data-consumption semantics in the reusable ADC model while leaving DMAMUX,
stream arbitration, NDTR/address progression, circular reload, HT/TC, and IRQ
state in DMA.

The machine property `adc-dma-endpoint` defaults to `true`. Setting it to `off`
selects the old MMIO `ADC_DR` path, so compatibility is explicit and testable.
The property is applied again after peripheral initialization because QOM
property setters may run before the ADC object has been initialized.

Validation: ADC qtest `36/36`, ADC DMA smoke with `on`, ADC DMA smoke with `off`,
and host CTest `15/15` passed. The smoke checks identical 16-bit circular
samples, NDTR/address wrap, and HT/TC flags in both paths.

Residual risk: this ADC integration only validates synchronous direct-mode
equal-width transfers. The reusable DMA path now also contains FIFO width
conversion and basic DBM reload/reconfiguration, but ADC does not exercise
those combinations; asynchronous backpressure, rollback, and the ADC M2P
endpoint case remain unverified. The endpoint smoke remains required because
ADC does not cover M2P or callback failure semantics.

# 2026-08-31 SPI2 DMA endpoint integration review

The first SPI2 endpoint run exposed a boundary bug in the reusable DMA layer,
not in BMI088 framing. With `PINC` enabled, DMA request routing correctly used
the captured `PAR` base, but the endpoint callback was still invoked after the
live `SxPAR` had advanced to another address. The second test byte was therefore
mistakenly delivered to BMI088 and changed a register from `0xA5` to `0x55`.

The fix is in `dm_mc02_dma_request_selected()`: a fixed endpoint callback is
used only when the live peripheral address equals the configured endpoint
identity. Other `PINC` beats retain normal MMIO access, address progression,
NDTR and completion semantics. This keeps the producer/boundary/consumer
contract in the DMA layer and avoids a SPI-specific `PINC` workaround.

The SPI implementation was also extracted from `dm_mc02.c` into reusable
`dm_mc02_spi.[ch]`; the board composition root now supplies DMA channels,
request IDs, peripheral addresses and GPIO selection. `spi-dma-endpoint=on`
is the default, while `off` preserves the old MMIO compatibility path.

Validation: QEMU incremental build, ADC qtest `36/36`, TIM qtest `19/19`,
host CTest `15/15` (including the auto-discovered endpoint and legacy smoke),
Python `243 passed`, shell syntax checks, `run-spi2-dma-smoke.sh on`,
`run-spi2-dma-smoke.sh off`, and the legacy wrapper all pass. The fixture checks
BMI088 RX bytes, DMA NDTR/flags, memory address bases, the `PINC` register
guard, and end-to-end completion.

Residual risk: the SPI endpoint integration remains synchronous and direct-mode
equal-width. The reusable DMA layer has basic FIFO/DBM support, but SPI does not
exercise FIFO+DBM endpoint staging; asynchronous backpressure, rollback, and
complete SPI transfer state/timing are not covered. The existing MMIO
compatibility path remains useful until those contracts have independent
lower-layer tests.

# 2026-08-31 DMA endpoint contract review

本轮完成 DMA 芯片层的第一道板卡无关 endpoint 边界。`DmMc02DmaEndpoint` 将外设
数据 producer/consumer 从 `address_space_memory` 解耦：P2M 调用 `read`，M2P 调用
`write`，并把 beat 大小与 request 的 virtual timestamp 一并传递。DMA 仍负责
DMAMUX 路由、stream 仲裁、NDTR/地址推进和错误状态；endpoint 不可见 DMA 私有寄存器
或 DM-MC02 pin policy。

首阶段明确限制为同步 direct mode、等宽 beat；FIFO、DBM、宽度转换和 rollback 尚未
通过该回调路径实现，旧 MMIO 路径没有改变。缺失 callback、零长度/空 buffer 或 callback
失败均返回 false，不伪造已提交传输。host smoke 覆盖 P2M/M2P 的 1/2/4-byte、时间戳、
缺失 callback 和失败传播；QEMU 主体已成功重链。

下一道门是迁移另一个真实芯片消费者，优先 SPI2，并补充 DMA controller + callback
端到端测试。当前不能把 endpoint smoke 或 ADC1 集成解释为完整 DMA 外设、FIFO 或
总线时序验证。

# 2026-08-31 ADC injected context queue review

本轮完成 STM32H723 ADC 芯片层的 injected context queue 切片。完整 `JSQR` 写入现在
是 context admission 边界，queue-enabled 模式固定保存一个 active 和一个 pending
快照；后续 guest 写入不会改变活动转换。第三个 context 不覆盖已有内容，而是锁存
`ISR.JQOVF`，`IER.JQOVFIE` 只门控共享 ADC IRQ，`ISR` W1C 负责清除原因。

`JQM=0` 完成后保留最后 context，`JQM=1` 在无 pending context 时清空 `JSQR` 并保留
外部 arm，完整新 `JSQR` 写入后可恢复。stateful OCREF qtest 原先在同一进程内重复
写入 JSQR，新增队列后首个错误是第二次启动仍消费旧 active context；测试辅助函数现
显式选择 `JQDIS`，恢复其单 context 意图，没有修改 ADC 队列实现来迁就旧测试。

验证：QEMU ADC qtest `33/33`，包含 FIFO 顺序、溢出、IRQ/W1C、JQM end-empty 和恢复；
完整 CTest `14/14` 也通过，其中整机 smoke `75/75`。回归中发现校准 guest fixture
先写 `JSQR`、后写 `JQDIS` 会触发空闲模式切换的 queue flush，导致注入值回退；已调整
fixture 为先配置 `JQDIS` 再提交 `JSQR`，没有改变芯片层队列规则。本切片未接入 board
route 或上层 worker。残余风险是队列固定为两级近似，`JAUTO`、低功耗自动断电和其它
H723 injected trigger source 仍未实现。

# 2026-08-31 ADC JDISCEN software-start fix

本轮修复了一个芯片层状态错误：`adc_injected_discontinuous()` 原先只检查
`JDISCEN` 和序列长度，导致合法的 `JQDIS=1 + JDISCEN=1 + JEXTEN=0` 软件
`JADSTART` 在首个 rank 后错误进入等待外部边沿的暂停状态。现在只有选择了外部
`JEXTEN` 且序列超过一个 rank 时才启用 injected discontinuous 分组；软件启动
继续一次完成整个 sequence。该判定仍位于可复用 ADC 芯片模型，board route 不参与
拼接或恢复 rank。

新增 qtest 覆盖三-rank 软件启动的 `JEOC`/`JEOS`/`JADSTART` 边界；无配置的第三
rank 使用既有确定性零回退值。ADC qtest `32/32` 通过。当前仍未实现 `JQM`、
`JQOVF`、`JAUTO` 和其它 H723 ADC trigger source。

# 2026-08-31 ADC injected discontinuous review

依据本地 STM32H723 CMSIS/HAL 定义，`CFGR.JDISCEN` 位于 bit 20，injected
discontinuous 的粒度固定为一个 rank；`DISCNUM` 只属于 regular group。QEMU ADC 芯片层
现在在外部 injected trigger 下保存 `current_injected_rank`，每个匹配边沿只调度一个
rank，subgroup 间停掉 virtual timer 并保持 `JADSTART`，最后一个 rank 才置 `JEOS` 并
清除 `JADSTART`。单 rank 序列不受 `JDISCEN` 影响，忙时触发不排队。

隔离验证新增 qtest：三 rank JSQR 由三次 TIM8 TRGO 边沿按 rank 1/2/3 输出，前两次
不置 `JEOS`，第三次同时置 `JEOC/JEOS` 并清除 `JADSTART`。本切片没有接入 `JQM`、
`JAUTO`、`JQOVF` 或新的 board route；这些仍需独立芯片层切片。

# 2026-08-31 ADC AUTDLY and trigger-batch review

# 2026-08-31 ADC regular discontinuous review

本轮把 `CFGR.DISCEN/DISCNUM` 下沉到可复用 ADC 芯片层。外部触发模式下，每个匹配边沿
只启动一个有界 subgroup，`DISCNUM=0..7` 对应 `1..8` 个 rank；subgroup 尚未到序列尾部
时停掉 virtual timer，保留 `current_rank` 和 `ADSTART`，下一匹配边沿继续。完整序列的
最后一项才置 `EOS` 并清除 `ADSTART`。软件启动路径保持一次完整 sequence，避免把
`DISCNUM` 错误扩展成软件触发队列。

`AUTDLY` 与 discontinuous 的交界也在芯片层处理：每个 `EOC` 仍等待 `ADC_DR` 读取，
读取最后一个 subgroup 结果后才进入组间暂停；忙时和压缩 trigger batch 不排队。非法的
`CONT+DISCEN` 组合沿连续路径处理，与 HAL 的“二者不可同时配置”约束一致。

验证：ADC qtest `30/30` 通过，新增外部 subgroup、软件完整 sequence 和
`AUTDLY` subgroup boundary 用例；未修改 board route 或上层 worker。当前仍未实现
injected `JQM/JQOVF`、`JDISCEN`、低功耗自动断电和其它 H723 trigger source。

本轮把 ADC 忙时外部触发的边界固定在芯片层：trigger bus 的 `event_count` 是同步接口
上的压缩数量，ADC 不把它展开成不存在的硬件 FIFO；空闲时最多接受一次，转换进行中则
忽略整批。这样 timer 的吞吐优化不会改变 ADC 的寄存器可见结果，也不会把 board 层耦合
到 ADC 内部状态。

同时实现 H723 `CFGR.AUTDLY` 的最小真实语义。regular 转换在 `EOC` 仍置位时停在当前
数据消费边界，`ADC_DR` 读取清除 `EOC` 后才恢复下一 rank 或连续 sequence；DMA 的同步
DR 读取自然释放该等待。默认 `AUTDLY=0` 路径没有额外等待或 host timer。

验证：ADC qtest 新增 `AUTDLY` 保持/DR 释放/停止用例；后续应补真实 firmware polling
与 DMA 组合路径，以及 injected context queue 的独立芯片层测试。

# 2026-08-31 center-aligned timer review

本轮在通用 timer 芯片层补齐了中心对齐的三角 counter phase。`CNT` 读回、update
周期、compare deadline、PWM 观察和 clock/register re-anchor 共享 phase 计算，因此
DM-MC02 board route 不需要知道计数方向，也没有引入逐边沿 host timer。`CMS=01/10/11`
对 compare flag/DMA match 分别选择下数、上数或双向；master OCREF route 继续保留内部
参考波形的双向边沿。

验证：TIM2 qtest `13/13` 通过，原有 edge-aligned compare/active-shadow/stateful
OCREF 用例均回归通过，QEMU 增量构建成功。当前仍未覆盖 RCR、组合/边沿模式、Break/
dead-time 和高级定时器输出级；这不是完整 H723 timer 语义的声明。

# 2026-08-31 stateful OCREF backlog review

本轮修复了通用 timer 的一个边界错误：宿主回调晚于 compare deadline 执行时，旧路径
会把多个 stateful OCREF 匹配合并到一个事件，并只执行一次 toggle；下游因此可能收到
与实际 OCREF 不一致的 `event_count` 和 `rising`。现在 active-on-match、
inactive-on-match、toggle-on-match 采用原始 virtual deadline 逐事件恢复，OCREF 状态
和每个 master/trigger 事件保持一致；frozen 模式不进入该恢复路径，避免普通 compare
受到额外开销。

验证：QEMU 增量构建通过，TIM2 芯片 qtest `10/10`，ADC 芯片/板级边界 qtest `25/25`，
host CTest `14/14`（含整机 QEMU smoke suite），Python `243 passed`，shell 语法和
`uv lock --check` 均通过。本轮尚未加入可控宿主停顿注入，因此上述延迟恢复由代码审阅
和相位算法覆盖；真实长时间高频积压的恢复成本仍是线性，不能把该路径宣传为无限吞吐。
若后续需要降低这项成本，应先版本化事件 ABI 表达 alternating edge batch，并同步定义
ADC/其它 sink 的排队或丢弃语义。

## 2026-08-30 H723 ADC trigger bus review

此前 DM-MC02 的 TIM8 update callback 直接调用 ADC，并把 `EXTSEL=0` 定义为
“TIM8 source”。H723 ADC1/ADC2 的 `EXTSEL=7` 才是 `TIM8_TRGO`；旧编码会使
真实 firmware 配置 `EXTSEL=7` 时静默不触发，属于芯片层 source-map 缺陷，不能
在板级继续保留常量 workaround。

现已新增与 ADC/timer 类型无关的 `DmMc02TriggerBus`。machine 在初始化阶段固定
注册 ADC1/ADC2 sink，TIM8 只发布 source 7、上升沿和当前 virtual timestamp；ADC
在自己的寄存器语义中完成 source/edge/ADEN/ADSTART 过滤。bus 为固定 8 个 sink、
同步调用、无队列和无动态分配，适合当前单线程 QEMU event-loop，也保持 host
单测可直接复用。

验证：trigger host smoke、ADC 软件/连续/TIM8 上升沿/ADCAL 以及错误 source、下降沿
拒绝 smoke 均通过；host CTest `13/13`、QEMU smoke `69/69` 和 QEMU 构建通过。真实
Release `trobot.elf` RTF 为 `0.999914x`，CPU `102.9%`，IWDG timeouts `0`，未观察到
该抽象带来的实时性回退。当前没有声称完整 H723 trigger matrix：其它 EXTSEL source、
timer `MMS/MMS2`、TRGO2/CC 和精确总线边沿时序仍是后续芯片层工作。

## 2026-08-30 DMA peripheral-request batch 复核

本轮将批量调度边界下沉到 DMA 芯片层。`dm_mc02_dma_request_batch()` 只合并
DMAMUX stream 查找和 level-sensitive IRQ fan-out；每个 item 仍通过
`address_space_memory` 完成读写，因此 UART TDR 等 MMIO 外设副作用不会被
`memcpy` 或板级旁路替代。原 `dm_mc02_dma_request()` 保持单事件入口，用于
需要每个事件都可见的时序/中断测试。

UART TX timer 已使用该批量入口，新增 batch smoke 验证 `BATC` 字节顺序、循环
`NDTR/M0AR/PAR` 重装以及 HT/TC 状态。当前改进仍不是完整 DMA 引擎：FIFO/FE/DME、
仲裁、可注入 endpoint 和每 item 中断边界仍未实现。批量入口也只应由调用方传入
有界 item 数；准确时序调用方应继续使用单 item 路径。

## 2026-08-30 SoC CPU contract 与 board profile 复核

本轮新增 `STM32H723-EVAL` profile，验证了同一 `STM32H723` SoC 模型可以被第二个
board composition 复用。profile 只提供静态 wiring 和资源数量；GPIO/AF 解码、IRQ
route、DMA route 的 host smoke 先独立验证，再通过 `run-board-profile-smoke.sh` 进入
真实 QEMU 初始化。

回归首先发现一个由 profile 抽取引入的启动缺陷：`ARMv7M` 的 `cpu-type` 属性要求
完整 QOM 类型名，SoC profile 却保存了命令行风格的 `cortex-m7` 简写。QEMU 因此在
realize 阶段报告 `invalid object type: cortex-m7`，绝大多数 QMP smoke 的表面错误才
表现为 connection refused。修复为 `cortex-m7-arm-cpu` 后保持 SoC 文件不依赖
`target/arm` 私有宏，并恢复启动链。

验证结果：QEMU build、host CTest `10/10`、QEMU smoke `65/65` 和备用 profile QMP
smoke 均通过。host smoke 中 QEMU 内部头文件仍会产生既有 GNU extension/pedantic
告警；正式 QEMU build 未因此放宽诊断。当前 profile 仍是虚拟评估板描述，不代表真实
商业开发板的完整电气或外设行为。

## 2026-08-30 BMI088 电源状态修复复核

此前 BMI088 芯片对象虽然已经拥有寄存器和 FIFO 状态，但 `sample()` 没有检查
`ACC_PWR_CONF/ACC_PWR_CTRL/GYRO_LPM1`，断电或 suspend 状态仍可能产生 raw/FIFO
数据。这是芯片层的功能缺陷，不应由 DM-MC02 machine 或 host worker 绕过修补。

现已在 [`dm_mc02_bmi088.c`](qemu/upstream/hw/arm/dm_mc02_bmi088.c:92) 集中判断
die 电源状态，新增 `dm_mc02_bmi088_is_powered()`，断电时拒绝新样本并清除 DRDY；
判断逻辑仍独立于板级 pin、电源 policy 和 co-sim。FIFO smoke 先验证断电注入不会
产生 data frame，再执行真实上电寄存器序列并复用原有 FIFO/sensor-time 断言。

当前仍未模拟上电稳定延迟、完整 power-domain 电气行为或 BMI088 外部中断脚；本修复
只收敛寄存器级采样门控，不提升模型的整体 Timed/Electrical 等级。

## 2026-08-30 worker Python/uv 入口复核

本轮收敛 worker 的启动边界。`tools/run-worker.sh` 现在是 Null、MuJoCo 和 ROS2
worker 的共同入口：前两者通过项目 uv 环境运行，MuJoCo 自动添加 `mujoco` extra；
ROS2 仍使用调用者已 source 的系统 `python3`，避免把 `rclpy` 错误隔离到项目环境。
五个 worker 相关 smoke（Null、MuJoCo、ROS2、QEMU-native、DM-MIT）已改为调用该入口，
harness 与 worker 的解释器不再隐式绑定。

本轮新增 `BackendRegistry`。内建 backend 通过每次创建的隔离 factory 表构造；用户可
用 `--backend MODULE:FACTORY` 直接加载 factory，或用
`--backend-registry MODULE:INITIALIZER --engine NAME` 注册后选择自定义 plant。factory
只接收 worker 配置并返回最小 backend 对象，QEMU wire、CAN adapter 和时钟 owner 不被
自定义 backend 依赖。registry 单测与 worker 选择契约共 `16 passed`。

Null、MuJoCo、ROS2、QEMU-native、DM-MIT 以及自定义 backend plugin smoke 均通过；本轮没有修改 QEMU
协议、芯片模型或 `trobot/`。
这项收敛提供了进程内第三方 backend 加载接口，但不提供 Gazebo 非 ROS 原生 adapter
或跨进程严格锁步；外部 backend 仍需自行实现真实机构和传感器语义。

## 2026-08-30 v2 motor endpoint 一致性修复

本轮修复了 v2 电机扩展的三处真实问题：

- Python codec 已接受 `MOTOR_STATE`/`CAP_MOTOR`，并补齐 qemu-to-host 的 session、step
  时间和 payload 校验测试；全量 Python 测试为 `204 passed`。
- QEMU 不再无依据地拒绝非零 `MotorCommand.flags`。该字段现在作为 endpoint-defined
  metadata 透传，和 Python codec 的现有 `flags=9` 用法兼容。
- 最近一次成功 STEP 的 `MotorState` 会进入幂等缓存，重复 STEP 会重发 ACK 和状态。若
  混合 STEP 的后续 IMU/ADC 提交失败，motor callback 的成功结果在 retry 中复用，不会因
  队列背压重复推进外部 plant。

当前边界：默认 `dm-mc02` machine 不注册具体 motor callback，worker v2 也未发送/消费
该 endpoint；默认电机仍通过 FDCAN 与 Null/MuJoCo/ROS2 engine 联调。混合 STEP 的全部
board side effect 还没有通用回滚事务，callback 返回失败时仍要求实现方保证“不产生已提交
副作用”。断线后的无损恢复和跨进程状态持久化仍不支持。

## 2026-08-30 v2 session 与 ADC-only STEP 复核

本轮确认并修复两个问题：

- v2 session ID 已在 Python、worker 和 QEMU endpoint 间闭环；运行中 QMP reset 会产生新
  session，旧 session 的 STEP 被拒绝，正确 session 可以继续运行。
- `v2_step_payload_valid()` 此前错误地要求每个 section STEP 必须包含 IMU，导致合法的
  ADC-only STEP 被拒绝。现在 ADC_INPUT/ADC_VOLTAGE 可独立组成 STEP，并由 QEMU smoke 验证。

此前 reset smoke 的失败不是 QEMU reset 回调丢失，而是测试最后的兼容性检查在 v1 RESET 后
发送了 `flags=0` 的 v2 STEP；该帧按当前 session 规则应被拒绝。测试已改为发送当前 session
ID，并覆盖“v1 RESET 不降级 v2、随后 v2 STEP 仍成功”。

定向验证：runtime RESET、ADC-only STEP、普通 STEP、STEP_DONE、控制队列背压和 telemetry
重试全部通过。当前仍未实现 ACK 超时/重传、v2 电机 section endpoint、完整 H723/USB/FIFO
语义和跨进程严格锁步。

完整回归：QEMU 构建、host CTest `7/7`（含全量 QEMU smoke）、uv pytest `186 passed`、
`bash -n tools/*.sh`、`uv lock --check` 和 host CMake 构建均通过。

# 2026-08-30 当前功能缺陷复核与修复

本轮确认并修复一个真实的功能缺陷：BMI088 gyro FIFO 在 stop-at-full 已积累 100 帧、
随后切换为 stream 模式时，旧实现以 `uint16_t` 计算 `limit - fifo_length`，发生下溢，
可能把 FIFO 长度扩大到有效容量之外并污染状态帧数。现在容量判断先处理
`fifo_length > limit`，再按完整 gyro 帧淘汰；回归覆盖了 800-byte 到 792-byte 的模式切换。

同时完成板级 GPIO 输出解码收敛。电源 ODR、BMI088 CS、WS2812 dirty、蜂鸣器 GPIO/AF
和 UART2/UART3 RS485 DE 均由纯数据 API 从 profile 与 GPIO snapshot 解码，machine 层只
执行副作用；蜂鸣器 AF2 不再硬编码。异常 profile 的 bank、UART 数量和 RS485 pin 也会
在 decoder 入口拒绝，profile 初始化校验继续在热路径外执行。

验证结果：

- QEMU 构建通过；上游 `vhost-shadow-virtqueue.c` 仍有既有 `-Wmaybe-uninitialized` 警告，
  本轮 DM-MC02 文件无新增编译警告。
- 本轮定向 v2 smoke 通过；完整 CTest/全量 QEMU smoke 结果以本节后续回归记录为准。
- BMI088 FIFO、RS485、PWM buzzer、GPIO 定向 smoke 全部通过；shell 语法和 `uv lock`
  校验通过；本轮未修改 `trobot/`。

仍存在明确的能力边界，不应当当作已实现功能：

- QEMU 是 H723 的功能子集，不是完整芯片模型；USB 目前仍不能真实枚举并挂载宿主机设备。
- co-sim v2 已有 session ID，但尚无可靠 ACK/重传和无损恢复；`STEP_ACK` 只代表接收/排队，
  严格 guest 消费需使用 `STEP_DONE`。
- v2 `MotorCommand/MotorState` section 尚未由 QEMU endpoint 消费，电机闭环仍走 guest
  FDCAN 到 host adapter。
- BMI088 FIFO 尚未覆盖完整 INT、水位、tag、丢帧和精确中断时序；UART/CAN 也不是位级
  信号完整性模型。

# 2026-08-30 GPIO 回调解耦复核

本轮完成一个低风险的公共 GPIO API 收敛：`DmMc02GpioOdrChanged` 增加 `bank_index`，
由 `DmMc02GpioBank` 保存并传递；machine 层改为所有 bank 共用同一回调，删除 A/B/C/D/E/F/G/H
八个专用包装函数和对应的初始化 `switch`。板级行为仍由 machine 根据 profile 处理，
没有把 DM-MC02 pin 语义塞入 GPIO 模型。

定向验证通过：GPIO、RS485、VIN=0/恢复、电源复位、BMI088 直读/FIFO、v2 runtime reset。
完整回归通过：QEMU 构建、host CTest `6/6`（其中 QEMU smoke `61/61`）、uv pytest
`180 passed`。本次改动不涉及 `trobot/`。

该改动之后已补齐纯 board signal decoder；session 可靠传输、v2 电机 section、完整
H723/USB/FIFO 语义仍是既有功能边界。

# 2026-08-30 board profile 校验复核

`dm_mc02_board_validate()` 现在在 machine 初始化前执行，集中检查固定数组容量、GPIO
和 IRQ 范围、serial slot 唯一性、器件 pin、BMI088 SPI/DMA、RS485 DE 以及 DMA UART route。
这解决了新增 profile 可能越界或静默不生效的初始化风险；校验不位于仿真热路径。

当前校验仍以现有 `dm_mc02.c` composition root 的容量和 SPI2/serial slot 约束为准；board
signal 解码已抽成纯数据 ops。CPU reset/ref 时钟的重复字段已移除，其它重复地址字段仍
待后续 profile 扩展时整理。

# 2026-08-30 异步 reset 与 telemetry 背压复核

本轮修复了一个会直接中断外部仿真运行的跨组件缺陷：QEMU 运行中执行
`system_reset` 后会发送 v2 `RESET/RESET_ACK`，worker 之前只跳过 `RESET`，收到
`RESET_ACK` 时会抛出 `unexpected v2 response kind 5`。现在 worker 将其作为新的
session epoch 处理，清空待执行的 plant/CAN 命令、重置 backend 状态和时间映射，再从
第一个 STEP 继续。

同时修复 QEMU telemetry 的边界语义：发送队列在控制帧完全占满时，telemetry 可能无法
入队；旧代码仍更新 `last_telemetry`，导致板卡状态不再变化时永久缺少重试。现在只有
成功入队才更新去重基线，并保留 pending 状态；控制队列排空后由 TX timer 重试最新快照。
控制帧仍保持优先级，队列满载时不阻塞 QEMU。

新增 `run-qemu-v2-telemetry-backpressure-smoke.sh`，通过 QMP 确认控制队列满载，再用
qtest 改变 PC13，验证控制响应完整保序且最终带有 `board_flags` 的 telemetry 到达。

验证结果：QEMU Release 构建通过，v2 runtime reset smoke、v2 STEP_DONE guest
consumption smoke、BMI088 FIFO smoke 和 telemetry 背压 smoke 通过；host CTest `6/6`，
内部 QEMU smoke `61/61`，uv pytest `148 passed`，
所有 shell 脚本语法和 `uv lock --check` 通过。当前未修改 `trobot/`。

剩余功能边界仍包括 session ID/ACK 重传、v2 MotorCommand/MotorState 由 QEMU endpoint
直接消费、完整 STM32H723 外设、完整 USB 总线和 BMI088 FIFO 中断语义；这些不能由本轮
回归结果替代。

## 2026-08-30 v2 typed STEP 契约复核

本轮发现并修复了 Python/QEMU typed STEP 校验的一个不一致：Python 已拒绝同一
`ADC_INPUT` 或 `ADC_VOLTAGE` section 中重复 channel，QEMU C 端此前只检查 channel 范围。
现在 C 端分别使用 32 位 channel 位图完成同类型去重；`ADC_INPUT` 与 `ADC_VOLTAGE`
属于不同输入语义，同通道并存仍被保留。该检查位于 dispatch 之前，因此非法 payload
不会提交 ADC、副作用或推进 STEP session。

同时修正 motor 回归脚本中的两个测试缺陷：非法 section flags 检查留下的 50 ms
socket timeout 会影响后续合法 60-byte STEP；非法 STEP 被丢弃后，后续 STEP 的
`dt_ns` 必须匹配上一条已提交 STEP，而不能把被拒绝的时间点算进去。新增/修正的
ADC 与 motor smoke 均已通过。

验证结果：QEMU 增量构建、host CMake build、CTest `7/7`（内部 QEMU smoke `63/63`）、
Python `227 passed`、`bash -n tools/*.sh` 和 `uv lock --check` 全部通过。

剩余风险没有被本轮测试消除：v2 仍不是跨断线可靠传输；混合 STEP 的所有 board side
effect 尚未成为可回滚事务；默认 machine 仍不绑定真实外部 motor plant；QEMU 仍是
H723/USB/FIFO 的功能子集。上述限制继续作为接口和测试边界保留。

# DM-MC02 QEMU 全量 Review

## 2026-08-30 STEP_DONE 修复结果

本轮完成 v2 guest-consumption `STEP_DONE` 全链路。QEMU 会为 v2 IMU 输入建立消费 token，
在 guest 通过 SPI 读取完整 accel/gyro direct raw burst 或 FIFO data frame 后发送与
`STEP_ACK` 相同 step 的二阶段确认；运行中 reset/reconnect 会清理旧 token。Python worker
增加方向化 session validator 和 `--wait-step-done`，默认模式仍兼容不发送 DONE 的旧
endpoint。真实 guest BMI088 读取 smoke 和严格 worker smoke 均已通过。

同时修复 telemetry 队列淘汰控制响应的问题：队列压力下优先淘汰未发送 telemetry，保留
RESET/ACK/STEP_DONE；并修复 BMI088 sensor-time 纳秒转 tick 的长时间乘法溢出。

本轮验证：QEMU smoke `59/59`、CTest `6/6`、uv pytest `147 passed`、QEMU build 和
`uv lock --check` 通过。session ID、ACK/重传、完整 FIFO 中断和 error recovery 仍未实现。

## 2026-08-30 当前功能缺陷复核

本轮发现一个会阻断运行中锁步的 v2 复位缺陷：QEMU 在运行中执行
`system_reset` 时已经向 host 发送 `RESET/RESET_ACK`，但内部 v2 validator 仍保持
未初始化，导致 host 按收到的复位事件直接发送的第一条 `STEP` 被拒绝。现在复位路径
会将 QEMU 内部 session 同步到发出的 reset 时间，并新增 reset 后首个 STEP 的回归断言。

同时修复两个诊断/能力契约问题：`RESET_ACK` 现在声明已实现的 ADC capability；ADC
队列溢出独立计入 `adc_dropped`，不再污染 `imu_dropped`，并通过 QMP diagnostics 暴露
独立队列深度和计数。QEMU 构建、v2 STEP smoke、运行中 RESET smoke、CTest `6/6` 和
uv pytest `128 passed` 均通过。

当前仍有明确功能边界，但不属于隐藏故障：v2 `STEP_ACK` 只表示输入已接收/排队，
需要消费栅栏时应等待 `STEP_DONE`；`MotorCommand/MotorState` section 仍不被 QEMU endpoint
消费，真实电机路径必须继续使用 guest FDCAN 到 host adapter；v1 没有 ACK/重传，且
QEMU 仍是 H723 功能子集，不应当作完整芯片或完整 USB/传感器器件模型。

随后已将不支持的 Motor section 改为显式 `STEP_ACK(UNSUPPORTED)`，将当前非零
header flags 改为显式 `STEP_ACK(PROTOCOL)`，并把 worker 的 ACK payload 校验改为
固定长度和保留字段校验；新增可复用 `DmMotorBusAdapter` 接入现有 FDCAN 电机闭环。
随后新增 `StepCoordinator`，将 CAN timestamp 排序和 backend step 边界从 worker
主循环抽出；v2、worker、CTest 和全量外设 smoke 均保持通过。

## 2026-08-30 v2 step section 输入扩展

本轮将 v2 从独立 Python codec 接入 QEMU native chardev 和 worker：同一连接按版本字段
分流，v1 保持默认；v2 已验证 `RESET -> RESET_ACK -> STEP -> STEP_ACK`，STEP 使用
60-byte `ImuSampleV2` 或 section payload，并复用已有 IMU/ADC virtual-time 路径。
QEMU/worker smoke 通过。

当前仍有三个明确边界：STEP_ACK 表示 QEMU 接收/排队，不代表 guest 已消费；v2 的
MotorCommand/MotorState 还没有由 QEMU step endpoint 消费；没有 ACK/重传、session ID
和无损诊断。生产使用仍应根据
`cosim-diagnostics` 和 ACK 状态判断是否丢样本。

本轮局部验证：QEMU v2 smoke、section payload `25 passed`；最终全量回归待本轮完成。

## 2026-08-30 架构复用与实时路径收敛

本轮修复并收敛了几处会影响复用或性能的实现细节：

- TIM2 与 TIM3 不再共享 `timer[0]` IRQ 索引；TIM2 使用独立 `irqs.tim2`，TIM3
  使用 IRQ29，并新增 TIM3 IRQ/UIF/W0C 裸机 smoke。
- timer/UART/FDCAN route 将各自 IRQ 与地址、通道元数据放在同一条 profile 数据中，
  重排 route 不会静默错接 NVIC；BMI088 CS 的启动/reset 也统一按 profile bank/pin
  合并 ODR 操作。
- machine 新增初始化前可选的 `board-profile` 属性，初始化后锁定；当前注册 profile
  为 `DM-MC02`，后续同 SoC 开发板可复用芯片模型和 composition root。
- co-sim RX 缓冲增加消费游标，正常逐帧解析不再执行剩余数据搬移，只在尾部空间不足
  时压缩，保持 v1 framing、顺序和有界队列语义不变。

本轮验证：QEMU smoke `56/56`，host CTest `6/6`，uv pytest `25 passed`；真实 Release
固件 RTF `1.000080x`，有效 `1000.080 tick/s`，QEMU CPU `101.8%`，IWDG timeout `0`。
NullEngine 吞吐 `207444 frame/s`，启动 smoke `51.891 ms`。

## 2026-08-30 GPIO 供电传播回归

发现并修复一个真实的板级功能缺陷：GPIOC ODR 回调此前只更新电源模型和 telemetry，
没有调用板级供电传播逻辑，因此 `electrical-power=true` 时运行中切换 PC15 并不会真正
切断或恢复 FDCAN/RS485 收发器。现在 PC15 的每次 ODR 变化都会立即同步到 FDCAN 和
USART2/USART3 的 transceiver 状态；VIN 保持有效时 MCU 不复位，收发器掉电期间的帧会
丢弃，恢复后可继续接收。FDCAN smoke 新增了启动计数和 off/on 阶段断言。

本轮验证：QEMU smoke `55/55`，host CTest `6/6`，uv pytest `24 passed`。该修复不改变
VIN=0 的 MCU brownout/reset 语义。

## 2026-08-30 架构与时间边界收敛

### VIN=0 MCU 电源边界

此前电源模型虽然将 VIN=0 标记为 `POWER_OFF`，但 machine 只把结果传给 FDCAN 和
RS485 收发器，Cortex-M7 仍会继续执行。现已修复：VIN=0 会复位 CPU 并保持 halted，
恢复有效 VIN 后 CPU 从复位入口重新启动；该行为与 `electrical-power` 无关，避免
`ideal_power` 兼容模式产生物理上不可能的“0V MCU 运行”。QMP 只读属性
`/machine mcu-power-good` 可供工具确认状态。新增测试覆盖启动时掉电、运行中掉电和恢复。

本轮新增电源运行时 smoke 后，QEMU 构建、全量 QEMU smoke、host CTest 和隔离 Python
测试均需重新串行回归确认。

本轮将 machine 中硬编码的 EXTI、ADC、Timer、FDCAN、UART 和 DMA IRQ wiring 移入
`DmMc02BoardProfile.irqs`。芯片模型仍只暴露通用 IRQ 端口，machine 负责连接 profile
选择的 vector；新增板卡不需要复制外设模型或 machine 组装逻辑。

ROS2 worker 现在读取 `sensor_msgs/Imu.header.stamp`，以首个有效 stamp 建立时间 origin，
并将后续前进的外部时间映射到 virtual time；重复、回退和缺失 stamp 会沿用固定步长
并保持严格单调。QEMU 构建、host CTest `5/5`、Python `23 passed` 和 Release 固件
RTF `0.999827x`（IWDG timeouts=0）均已验证。

这些改动没有改变当前已知的能力边界：co-sim v1 仍无 ACK/重传，SocketCAN ACK 仍是
host backend 代理，USB 仍不是完整可挂载设备，外设模型仍是 H723 功能子集。

## 2026-08-30 功能缺陷审计

本轮确认并修复了三类会污染功能结论的问题：

- QEMU FDCAN host ingress 现在拒绝 DLC 高四位非零的 wire frame；此前例如
  `DLC=0x10` 会被掩码成零长度帧。
- SocketCAN native CAN-FD ingress 现在拒绝 ESI 和未知 flag。v1 wire 没有 ESI
  表达位，继续转发会静默改变帧状态，因此选择显式失败。
- QEMU worker smoke 不再只检查 worker 退出和 QEMU 仍运行；它等待 guest 配置
  BMI088，使用 `--realtime`，再通过 guest RAM 中的 sensor-time 验证 IMU 已经进入
  guest 可见的 BMI088 数据路径。ROS2 smoke 也增加了 `rclpy` 预检和连接超时。

本轮验证结果：QEMU smoke `55/55`，串行 host CTest `6/6`，uv pytest `24 passed`。
一次与 CTest 并行启动独立 QEMU suite 导致的 `run-cosim-link-smoke.sh` 瞬态失败，
在串行 CTest 和单独 smoke 中均复现为通过，不属于产品回归。

仍存在以下功能限制，属于当前协议/模型边界而非已修复问题：

- co-sim v1 没有 ACK/重传和丢帧补偿。已接收的 future-timestamp burst 在 peer 断开后
  仍会由 QEMU 按虚拟时间交付，但如果发送方在写入过程中遇到背压/队列溢出，仍不能
  宣称 v1 具备可靠无损传输。
- SocketCAN bridge 只做帧转发，不能取得外部 CAN 节点的物理 ACK；现已增加显式
  `fdcan-host-ack=on` policy 作为外部 bridge 的 ACK 代理，但它表示 host backend
  已接受帧，不等同于真实物理 ACK。默认关闭时外部总线仍可能按 no-ACK 累计 TEC。
- ROS2 engine 当前读取 `sensor_msgs/Imu.header.stamp` 建立 external-time origin；
  时间回退（例如 Gazebo reset）会建立新的映射 epoch，但仍不提供跨进程的严格锁步。

## 2026-08-30 USB endpoint queue 收敛

修复描述符与内部 packet harness 的端点覆盖不一致：描述符声明的 EP1..EP5 现在都有
对应的内部 IN/OUT 队列，提交/完成路径使用实际 endpoint 编号更新 `DAINT`、
`DIEPINTn`/`DOEPINTn`，复位 `DAINTMSK` 覆盖 EP0..EP5。新增 qtest 覆盖 EP2/EP3/EP4/EP5，
USB qtest 4/4 通过。该改动只修复测试/兼容子集的数据结构和状态，不宣称完整 USB
枚举、DWC2 事务状态机、PHY 或宿主机挂载。

## 2026-08-29 BMI088 FIFO 基础数据面

BMI088 原先只有 data-register/DRDY 路径，高频固件若启用 FIFO 会读到普通寄存器零值，
不具备可用性。现在 accel 的 `FIFO_LENGTH(0x24..0x25)`/`FIFO_DATA(0x26)` 与 gyro 的
`FIFO_STATUS(0x0e)`/`FIFO_DATA(0x3f)` 均接入真实 SPI burst 语义（gyro frame 包含 6-byte
rate 和 2-byte sampled interrupt field）；FIFO 入队由现有
co-sim 已接受样本驱动，因而不会创建 host 高频定时器或降低默认实时性。专项 smoke 覆盖
FIFO fill、数据顺序、accel partial-frame repeat、drain 和 overread；全量 smoke 通过。

该实现仍是高价值基础切片，不应宣称完整 FIFO：缺 accel INT tag/sample-drop frame、
FIFO interrupt pin、gyro external tag 与精确 watermark/full timing。

## 2026-08-29 FDCAN BUS-OFF 粗粒度语义

修复了一个会影响真实故障测试的功能缺陷：无 ACK 的发送错误此前只将 TEC 增加 1，
永远不会进入 BUS-OFF。现在按帧级近似将 TEC 增加 8；达到阈值后设置 `ECR.TEC=255`、
`PSR.BO`、`IR.BO`，置 `CCCR.INIT`，停止该节点参与 virtual medium，并完成尚未完成的
TX 请求，避免固件永久等待。显式执行 `CCCR.INIT -> normal` 可清除 BO 并将 TEC 清零；
完整 error frame、位级重试和 129×11 recessive-bit recovery 仍未建模。

新增 `run-fdcan-busoff-smoke.sh`，覆盖无接收节点累计 ACK error、BUS-OFF 状态和显式
恢复后的重新收发。该能力是 Functional/故障注入切片，不代表完整 CAN 物理层。

## 2026-08-29 RTF 采集工具路径修正

RTF 采集脚本不再自动回退到历史 `build/qemu-release/qemu-system-arm`；未显式设置
`QEMU_SYSTEM_ARM` 时只使用当前 `build/qemu/qemu-system-arm`，避免旧 machine binary
污染性能对比。当前 CPU 余量仍约为单个 TCG vCPU 的极限，`thread=multi` 与 TB cache
调整尚未显示稳定收益，因此没有把它们写入默认启动参数。

## 2026-08-29 FDCAN 可选帧级 timing 收敛

此前加入的 FDCAN timing 草稿存在前置声明错误，导致 QEMU 无法编译；同时
`busy_until_ns` 尚未参与 medium 调度。现已修复 NBTP 位域解析，补齐 frame duration
内部元数据、120 MHz FDCAN 时钟、`fdcan-accurate-timing` machine 属性和总线忙闲调度。
默认关闭，因此普通仿真仍走快速路径；开启后同一总线空闲时按 CAN ID 仲裁，并在总线忙时
延后后续帧。新增/更新 medium smoke 覆盖 1 Mbit/s 配置下的序列化和最终 TXBRP 清零。
该实现是帧级占用估算，不代表完整 bit stuffing、error frame 或物理层 BUS-OFF recovery。

本轮验证：QEMU 全量 smoke `53/53`、host CTest `4/4`、uv pytest `16 passed`。

## 2026-08-29 FDCAN 双中断线修复

修复了 FDCAN 只连接一条 NVIC 中断线且忽略 `ILS` 的功能缺陷。现在 FDCAN1/2/3
均接入 H723 的 IT0/IT1，`ILS` 按事件位选择目标线，`ILE` 分别控制两条输出，
并保留未配置 `ILS` 时的默认 IT0 快路径。现有 FDCAN smoke 已将 HPM 事件路由到
IT1 并验证 CPU handler 被调用；FDCAN calibration interrupt 和完整 M_CAN 错误
语义仍未实现。

## 2026-08-29 SYSCFG/EXTI 动态复用修复

修复了一个可复现的重入时序缺陷：EXTI 外部输入变化在断言 IRQ 前没有先更新内部
`line_level`，IRQ handler 若立即改写 SYSCFG `EXTICR`，端口复用回调会读取旧电平，
从而丢失新端口已有高/低电平对应的边沿。现在先锁存输入电平，再更新 pending/IRQ；
SYSCFG 的 `EXTICR1..4` 改写也会立即重新应用当前 GPIO 输入，并新增 GPIOA→GPIOB
运行时复用 smoke。该路径已通过专项测试；完整 EXTI 线、电气输入和其它 H723 外设语义
仍不在模型范围内。

## 2026-08-29 EXTI 功能补齐

此前 EXTI 窗口只有寄存器存储，软件触发和 pending 清除不会到达 NVIC。现在已实现
EXTI0--15 常用路径：上升/下降沿选择、SWIER1、PR1 W1C、C1IMR1 屏蔽，以及
EXTI0--4、EXTI9_5、EXTI15_10 的中断输出；新增 `run-exti-smoke.sh` 验证 IRQ40。
DM-MC02 的 PA15 active-low 用户键已通过 `user-key` machine/QMP 属性接入 GPIOA IDR，
按下/释放会驱动 EXTI15_10；同时已支持 `gpio-input=PORTPIN=0|1`，输入值经 GPIO IDR，
并根据当前 SYSCFG EXTICR 选择驱动 EXTI0--15。运行时 EXTICR 复用传播和 IRQ 重入
时序问题已在后续修复记录中补齐。

本次回归共 52/52 QEMU smoke、CTest 4/4、pytest 16/16；真实 Release 固件 1 s 样本
RTF `0.999982x`、CPU `102.9%`、IWDG timeout `0`。新增 PA15 用户键输入后，GPIO/EXTI
定向测试和全量回归仍通过。

## 2026-08-29 当前功能复核

修复了 `run-fdcan-smoke.sh` 中 priority-only 过滤器与电源恢复步骤复用 `0x321` 导致的
测试误报；恢复步骤现使用 FIFO filter 的 `0x456`。修复后构建通过，51/51 QEMU smoke、
CTest 4/4、pytest 16/16 通过。真实 Release 固件 1 s 样本为 RTF `0.999960x`、CPU
`102.9%`、IWDG timeout `0`。

当前未发现已覆盖路径中的可复现功能故障；下文列出的项目属于模型能力边界或测试覆盖缺口，
不能按完整 STM32H723/真实电气系统语义使用。

## 2026-08-29 性能复核更新

对真实 Release `trobot.elf` 做了约 80--140 ms 的 QEMU execution trace，并结合
3 s RTF 样本核对热点。稳定运行阶段的高频 guest 区域主要是 FreeRTOS
`prvIdleTask` 的 busy idle loop、SysTick/TIM2 周期中断和 SPI 轮询；QEMU 进程
长期约占用一个 host CPU。单 vCPU 的 Cortex-M7 TCG 执行是主瓶颈，增加 host 多核
不能把这段 guest 指令自动并行化。

本轮对 DMA、ADC、UART、FDCAN、TIM 的 level-sensitive IRQ 输出加入电平缓存：
仅在输出电平发生变化或 IRQ sink 首次连接时调用 `qemu_set_irq()`，不合并请求、
不减少 guest 可见状态更新，也不改变中断边界。全量 QEMU smoke `51/51`、CTest
`4/4`、pytest `16 passed` 通过。3 s Release 样本为 RTF `1.000004x`、CPU
`101.6%`、IWDG timeout `0`；与短时基线相比收益仍在调度噪声范围内，不能宣称
已有稳定余量。

QEMU 编译已是 `-O3`；额外的本机指令集/LTO 构建在本机短测中未显示稳定优势，
因此暂不替换默认的可移植 Release 构建。后续性能工作应优先针对 TCG/guest
执行路径和不改变真实时序的事件调度优化。

## 2026-08-29 复核更新

随后发现并修正 FDCAN 中断位定义与测试常量同时错误的问题：H723 的 RF0F、RF1N、RF1F
实际分别位于 bit2、bit4、bit6，旧模型使用了 bit3、bit1、bit4；当前已按 CMSIS 头文件
纠正。并补齐标准/扩展 FIFO 高优先级过滤器的 `HPMS` 与 `IR.HPM`，`HPMS.MSI` 使用
真实的 FIFO0/FIFO1 编码；扩展 FIFO1 smoke 已验证 `IR=RF1N|HPM` 和 `HPMS`。

本轮修复一个 FDCAN dedicated Rx Buffer 路由缺陷：当标准或扩展过滤器已经匹配到一个
仍置位 `NDAT` 的专用 Rx Buffer 时，旧实现会继续执行全局非匹配策略，导致重复帧可能
错误进入 FIFO0/FIFO1。现在该过滤器命中即终止接收并丢弃忙 buffer 的帧；清除 `NDAT`
后同一 buffer 可再次接收。专项 smoke 已覆盖重复帧不污染 FIFO、释放后重收以及标准/扩展
ID 路径，之后全量 51/51 smoke 回归通过。

已修复一个会污染验证结论的工具缺陷：两个 smoke 入口默认选择了旧的
`build/qemu-release/qemu-system-arm`，导致当前 FDCAN 代码下的电机测试出现假失败。
现在默认统一使用 `build/qemu/qemu-system-arm`，并保留 `QEMU_SYSTEM_ARM` 显式覆盖。
最新构建和全量回归已确认通过：QEMU smoke `51/51`、host CTest `4/4`、Python
`16 passed`。

日期：2026-08-29  
范围：`dm-mc02-qemu/` 项目代码、QEMU 8.2.2 本地 machine patch、host codec/transport、Python worker、构建脚本和 smoke 测试。只读检查了 `trobot/` 的 linker、`.ioc` 和时钟初始化，用于核对板级契约；没有修改 `trobot/`。

本轮补齐了 FDCAN 扩展 ID 过滤器和 FIFO1：支持扩展 range/dual/mask、EFEC 路由、全局
非匹配策略、FIFO1 状态/释放和 RF1N/RF1F；新增 `run-fdcan-ext-smoke.sh`，并回归标准
FIFO0、CAN-FD 和进程内 medium。另将 pytest 加入 uv dev 锁定环境；使用隔离环境执行
`env -u PYTHONPATH uv run --group dev python -m pytest -q` 得到 16 passed。

本轮专项回归结果：USB qtest 3/3、host CTest 4/4、FDCAN 标准/扩展/medium smoke 全部
通过；Release 固件 1 s RTF `0.999958x`、约 999.958 tick/s、IWDG timeouts=0。当前仍
不模拟 FDCAN error frame 和完整 bus-off recovery；粗粒度 BUS-OFF 已由独立的
`fdcan-accurate-timing` 路径覆盖。

本轮补齐 FDCAN dedicated Rx Buffer：支持标准/扩展 `SFEC/EFEC=7` 的精确 ID 匹配、
`ID2[5:0]` buffer index、`RXBC/RXESC` 元素地址、`NDAT1/2` W1C 和 `IR.DRX`，并修正
`SIDFC/XIDFC.FLSSA` 的 32-bit word-address 语义。专用 smoke 和全量 QEMU `51/51`
通过；calibration/debug message destination 和 FDCAN calibration interrupt 仍未实现。

随后又修正 `TXBC/RXF0C/RXF1C/RXBC/SIDFC/XIDFC` 地址字段的保留位处理，并让专用
smoke 同时验证标准 ID 到 `NDAT1` 和扩展 ID 到 `NDAT2`。最新 Release 构建实时系数
为 `0.999735x`、CPU `100.9%`，未观察到性能回退。

本轮新增 DMA 双缓冲功能并完成真机地址语义收敛：基础 peripheral-request 路径支持
`M1AR`、`DBM`、`CT`，每个 buffer 独立重装 `NDTR`，TC 后交替 M0/M1；DMA 内部维护
current cursor，`M0AR/M1AR` 保持配置基地址。新增 TIM8→DMA2 Stream6 DBM smoke，并
更新既有 DMA/UART/SPI smoke 以验证基地址语义。49/49 QEMU smoke、CTest 4/4、Python
15 passed/1 skipped 通过；Release 固件 RTF `0.999912x`，IWDG timeouts=0。

本轮又修复了 FDCAN 接收过滤缺陷：模型现在读取 H723 `SIDFC`/`XIDFC` 指向的标准和扩展
过滤器，支持 range/dual/mask 匹配、FIFO0/FIFO1 路由、全局非匹配策略和标准/扩展远程帧拒绝，
并将匹配索引及 `ANMF` 写入 Rx FIFO element。新增扩展 ID + FIFO1 端到端验收；49/49 QEMU
smoke、CTest 4/4、Python 16 passed 通过，Release 固件 RTF 约 `0.9998x`。

## 2026-08-29 review closure

本轮补充了动态 RCC→TIM2 首周期专项验收，并修复了一个此前缺少独立覆盖的时序缺陷：
改频时虽然 CNT 已经锁存，但后续 deadline 会被重排成完整的新周期，导致首个 update
偏晚。现在按改频瞬间剩余 CNT、PSC/ARR 和新 TIMERCLK 计算首个 deadline；专项 smoke
验证了半周期计数从约 533 ms 的完整新周期缩短为约 267 ms 的剩余周期。

本轮又修复并优化了四处当前实现问题：RCC 动态改频时先保存旧时钟下的 timer CNT、更新
TIMERCLK 后再按新频率重排 deadline，避免改频后的首个周期错误；TIM update interval
增加失效式缓存；CAN medium 使用固定容量最小堆替代每帧扫描 128 个槽位，并缓存发送者
注册顺序；Python worker 的项目 pytest 入口、发送队列重复 flush 和整数频率时间戳路径得到
整理。另修正 PLL1M1 的真实六位 divider 掩码。以上不改变已约定的虚拟时间、CAN 仲裁、
DMA/IRQ 或 worker 帧顺序语义。

本轮针对可观察的已实现接口缺陷完成修复：

- FDCAN host ingress 现在保留 wire 中的 `virtual_time_ns`，不会被当前 QEMU 时间覆盖。
- FDCAN TX 根据 `TXESC` 限制数据读取范围；DLC 大于元素容量时剩余 host payload 清零，避免越界读取相邻元素。
- FDCAN virtual medium 拒绝满队列时，TX slot 也会完成并报告粗粒度 no-ACK，而不是永久等待。
- UART `TC` 仅在 host-facing TX 队列为空时置位；TDR 接受并不等价于发送完成。
- Null/MuJoCo/ROS2 的 legacy float 电机命令统一遵守 enable gate 与 torque limit，并清除旧 DM 控制状态。
- C transport 在 pending non-blocking send 中传入不同 frame 时返回 `INVALID`，不再静默忽略新 frame。
- `run-peripheral-reset-smoke.sh` 已恢复可执行权限。
- FDCAN `TXFQS` 现在按 virtual-time medium 中未完成的 TX 请求报告空闲槽位，
  `TXBRP` 暴露 pending 位；发送完成或收发器断电后状态均可收敛，避免 guest 依据
  错误的空闲数重复占用发送槽位或永久等待。

新增覆盖包括 FDCAN TX element 边界、C transport pending-send API 和三种 worker engine 的 legacy float 行为；相关 CTest、Python、UART/FDCAN/RS485/电机 smoke 均已回归通过。

新增的 FDCAN medium smoke 还验证了 TX 请求提交前、virtual-time 完成后的
`TXFQS`/`TXBRP` 一致性。

## 结论

当前版本适合作为“固件启动兼容性 + 外设最小功能切片 + 外部仿真桥接”的实验后端；DM-MIT 的 Null/MuJoCo 控制反馈链路已可用，但仍不适合作为时序精确的 H723 仿真器或带可靠传输保证的长期测试平台。

本轮又修复了两个已确认的功能问题：FDCAN host TX 在 worker 晚连接时会先进入 QEMU 的有界队列，避免启动阶段丢失 DM reset/enable/control 帧；worker 的 DM-MIT engine 现在统一遵守 enable gate，Null/MuJoCo 使用配置扭矩上限，MuJoCo 执行位置/速度/Kp/Kd 控制项。DM-MIT QEMU 联合 smoke 和全量现有 smoke 均已通过。

2026-08-29 后续真实固件回归发现并修复了一个此前测试未覆盖的 P1 缺陷：SPI2 模型没有实现 H723 `SR.EOT`，导致 `HAL_SPI_Transmit()` 在 BMI088 初始化期间永久等待，随后 IWDG 复位。现已补齐 `CR2.TSIZE` 基础计数、`SR.EOT`、`IFCR.EOTC` 以及 TXDR 的 8/16/32-bit 打包写；修改后的真实 Release `trobot.elf` 已完成 BMI088 初始化，并在 10 秒测试中保持 1 kHz tick、无 IWDG reset。此前的低 RTF 数据来自初始化卡死，不能作为性能结论。

本轮修复了 MuJoCo smoke 的工具入口缺陷：此前脚本从项目目录外启动时，`uv --extra mujoco` 不会使用本项目环境，worker 因缺少 MuJoCo 而提前断开；现在通过 `uv run --project` 显式绑定项目目录。该场景已从仓库外目录回归通过。

当前最重要的风险不是 QEMU TCG 本身，而是模型边界和复位/时钟契约没有闭合：

1. CPU/SysTick 已经动态跟随 RCC，真实 Release 固件约 1.0× RTF；当前板级 timer 已改为共享动态 `TIMERCLK`，ADC 有效转换时钟也已按当前板级独立 PLL2P 与 ADC common `CCR.CKMODE/PRESC` 更新，但 ADC kernel source/APB 全时钟树仍未完成。
2. QMP `system_reset` 已接入 machine 统一 reset，关键寄存器/FIFO/传感器/CAN medium/timer/co-sim 状态可恢复；完整芯片 reset 语义仍不在范围内。
3. VIN、5 V 和收发器新增可选 electrical policy；默认仍是兼容性的 ideal policy，SPI 等其它外设尚未全部做 rail 门控。
4. co-sim telemetry、FDCAN 和 UART 均有有界非阻塞发送队列；UART 短写可在虚拟时间重试，但协议层尚无 ACK、重传、序号缺口报告。

因此，当前版本可评为：启动与基础数据面“可用”，时序/电源/复位/可靠性“需补齐”。

本次后续修复：Python worker 的 co-sim、FDCAN 和 SocketCAN 发送路径已改为有界非阻塞队列，避免 host 背压在仿真循环内等待；队列上限和有限运行结束排空已有 host 单元测试覆盖。协议层 ACK/重传和 session 身份仍未实现。

本轮又修复了两个可复现的工具缺陷：QEMU FDCAN host wire 接收端现在严格校验 reserved
字段、flags、CAN ID、DLC 及非法 CAN-FD 组合；worker `--realtime` 现在以收到的初始
virtual-time 作为相对原点，避免 QEMU 已运行一段时间时产生额外启动等待。两者均有专项
回归覆盖。

本轮还修复了一个实际使用缺陷：此前 VIN 只有内部 `dm_mc02_power_set_vin_mv()` helper，
QEMU 用户无法通过启动参数或 QMP 改变输入电压。现在提供 `vin-mv` machine/QMP 属性；
运行时修改会同步电源策略，reset 保留外部 VIN，新增配置 smoke 已覆盖。

本轮继续修复了四个可复现问题：warm reset 现在清理已建模的 USB/CORDIC/EXTI/SYSCFG/
DBGMCU/FMC 状态；co-sim 的 `rx_dropped_bytes` 不再把正常消费的帧计为丢弃；DM 多电机
参数拒绝控制/反馈 ID 冲突；ROS2 `JointState` 缺失字段不再沿用旧反馈。新增跨复位
CORDIC/USB smoke，相关 QEMU 回归已通过。

同时补齐了通用定时器 update 的 UIF/UIE/W0C 和 level-sensitive IRQ，并把 TIM2、TIM8 update、TIM12、TIM24 接入 H723 对应 NVIC 外部 IRQ；TIM2 HAL timebase 有独立裸机验收。TIM12/TIM3/TIM8 的 PWM 输出目前提供虚拟时间惰性观察，但仍不是逐边沿 compare/PWM 波形模型。

## 验证证据

| 项目 | 结果 |
|---|---|
| QEMU | 8.2.2，upstream commit `11aa0b1ff115...`，`dm-mc02` machine 已注册 |
| QEMU/host 集成 smoke | 49/49 通过（含 DM-MIT worker 闭环、SPI EOT、BMI088 ODR/DRDY/sensor-time、IWDG 超时复位、CRC 向量、RESET 相对 IMU 时间调度、外设 warm-reset、TIM2 动态改频首周期和 TIM8 DMA 双缓冲） |
| host CTest | 4/4 通过 |
| Shell/Python/uv 检查 | `bash -n`、`compileall`、`uv lock --check` 通过 |
| NullEngine | 本次约 205,625 IMU frame/s；10,000 帧，worker 启动到 RESET 39.181 ms |
| Release 固件 | 1,001 tick / 1.001000 s，RTF `0.999606×`，约 999.606 tick/s，IWDG timeout `0` |
| QEMU 资源 | 采样时 RSS 约 49.7 MiB，进程 CPU 约 102.0%（含辅助线程） |
| trobot 时钟依据 | HSE 24 MHz，PLL M=2/N=40/P=1，CPU 480 MHz；TIM2 output 240 MHz，ADC kernel 96 MHz |

所有 smoke 通过只说明已覆盖的最小路径成立，不代表未实现寄存器具有真实 STM32 语义。

本轮实测 Release 固件仍为 \`0.9997x\` 左右 RTF、约 \`101.9%\` CPU；由于该基线主要由单个
ARMv7-M TCG vCPU 执行固件占用，定时器/CAN/worker 优化不会把它线性提升为多核性能。CAN
堆和 worker 优化主要在高并发总线或 host bridge 场景生效，仍需以相同负载的专门压力测试
量化收益。

## Findings

### P1 — USB EP1 OUT 默认全局中断摘要缺失（已修复）

EP1 OUT 收包会置位 `DAINT` bit17 和 `DOEPINT1.XFRC`，但复位默认
`DAINTMSK=0x00010003` 没有打开 bit17，导致 `GINTSTS.OEPINT` 不会向依赖全局摘要的
guest USB 驱动反映 EP1 OUT 完成。现已改为 `0x00030003`，并在 qtest 中增加全局
`OEPINT` 断言；USB qtest 3/3 通过。

该修复只闭合了已实现 EP0/EP1 测试 harness 的中断摘要。USB 仍不是完整 DWC2
guest/device 状态机，不能据此宣称真实 USB 枚举、PHY 或宿主机 USB 挂载已支持。

### P1 — QMP/system reset 不会复位自定义 machine 状态（已修复）

位置：[`qemu/upstream/hw/arm/dm_mc02.c`](qemu/upstream/hw/arm/dm_mc02.c:452-500)、[`qemu/upstream/hw/arm/dm_mc02.c`](qemu/upstream/hw/arm/dm_mc02.c:1252-1272)

此前 machine 只定义了 `instance_finalize`，没有统一 reset callback。现已增加统一 handler，并为关键普通 C 结构、byte array、FIFO、Flash 控制器和 `QEMUTimer` 增加 reset 路径；未覆盖的完整芯片寄存器语义仍不在范围内。另提供 `cold-reset=on` 清空片上 SRAM。

影响（修复前）：重复测试、失败后重试、仿真“从头开始”都可能继承上一轮状态。  
已修复：machine 注册统一 reset handler；关键外设、FIFO、CAN medium、DMA、BMI088、timer、power 和 co-sim parser 均清理，QMP smoke 已覆盖片选恢复。

本轮又补齐了此前遗漏的已建模状态：CORDIC 结果/参数、USB 控制寄存器、EXTI、SYSCFG、
DBGMCU 和 FMC 现在在 warm reset 后恢复复位值；跨复位 smoke 已验证 CORDIC 未消费结果
和被修改的 USB `GRXFSIZ` 均不会泄漏到下一轮。完整 H723 未建模寄存器当然仍不具备真实
复位语义。

### P2 — co-sim 接收丢字节计数曾误计正常帧（已修复）

位置：[`qemu/upstream/hw/arm/dm_mc02_cosim_link.c`](qemu/upstream/hw/arm/dm_mc02_cosim_link.c:125)

帧解析成功后会消费接收缓冲区，但该消费不应计入 `rx_dropped_bytes`。旧实现把所有已消费
字节都累计为丢弃字节，导致后续诊断不可信；现在只有坏帧前缀、坏帧和缓冲溢出才增加该计数。

### P2 — DM 多电机控制/反馈 ID 冲突（已修复）

位置：[`tools/dm_mc02_sim_worker.py`](tools/dm_mc02_sim_worker.py:768)

参数解析现在检查显式映射与未显式映射的连续默认 ID，并拒绝控制 ID、反馈 ID 之间的任意
冲突，避免反馈帧被当作另一个电机的控制命令或导致电机不可达。

### P2 — ROS2 JointState 缺失字段沿用旧值（已修复）

位置：[`tools/dm_mc02_sim_worker.py`](tools/dm_mc02_sim_worker.py:641)

`sensor_msgs/JointState` 允许 position、velocity、effort 数组长度不同。worker 现在每条
消息先清除上一条状态，再按索引填充当前消息提供的字段；未提供的字段为零，effort validity
也同步清除。

### P1 — 外设 timer 时钟与动态 CPU/RCC 时钟脱节（已修复当前配置）

位置：[`qemu/upstream/hw/arm/dm_mc02_tim2.c`](qemu/upstream/hw/arm/dm_mc02_tim2.c:25-85)

旧实现将 `TIM2_CLOCK_HZ` 固定为 24 MHz。当前实现由共享 `TIMERCLK` 驱动这些 timer，并在 RCC 切换时保持 CNT 连续；ADC kernel clock 和完整 APB 分频树仍未覆盖。

影响（修复前）：PWM、WS2812 DMA、ADC 外部触发、蜂鸣器频率和任何依赖 timer 的控制测试时序错误。

已修复当前板级配置：新增共享 `TIMERCLK`，按 SYSCLK/2 驱动 TIM2/TIM3/TIM8/TIM12/TIM24，并在切换时保持 CNT 连续。ADC kernel clock 和完整 APB 分频树仍是后续工作。

### P1 — PLL 时钟树只覆盖当前配置，通用 RCC 语义不正确（已修复）

位置：[`qemu/upstream/hw/arm/dm_mc02_pwr_rcc.c`](qemu/upstream/hw/arm/dm_mc02_pwr_rcc.c:139-214)

当前模型对当前 HSE/M=2/N=40/P=1、FRACN=0 配置能得到 480 MHz，但存在三个边界：

- 旧实现错误解释了 `PLLCKSELR.PLLSRC`，忽略 `RCC_PLL1FRACR`，并过早执行整数除法。

已修复：修正 source 编码，加入 FRACN 和 fractional enable，并使用整数分子保留精度；无源或非法 divider 返回未时钟状态。完整 source/fractional bare-metal 覆盖仍待补充。

### P1 — 电源状态没有成为运行时供电边界（已修复为可选策略）

位置：[`qemu/upstream/hw/arm/dm_mc02_power.c`](qemu/upstream/hw/arm/dm_mc02_power.c:37-62)、[`qemu/upstream/hw/arm/dm_mc02_power.c`](qemu/upstream/hw/arm/dm_mc02_power.c:64-84)、[`qemu/upstream/hw/arm/dm_mc02_fdcan.c`](qemu/upstream/hw/arm/dm_mc02_fdcan.c:344-352)

`dm_mc02_power_update()` 计算了 `system_5v_good`/`system_3v3_good`，但 FDCAN medium 的 enabled 条件只是 RX FIFO 已配置；FDCAN、UART、SPI 等路径没有检查 rail。`system_5v_good` 也只依赖 VIN 正常，不依赖 PC15 的 switched-5V enable。`switched_5v_enabled` 目前是记录值。

影响：VIN=0 或 switched 5V 关闭时，外设仍可工作；telemetry 与固件可观察状态不一致。

已修复：新增 `-machine dm-mc02,electrical-power=true`；默认兼容模式保持旧行为，真实策略下 PC15/VIN 会门控 FDCAN 和 USART2/3 RS485，power smoke 已覆盖开关。

### P1 — chardev TX 丢帧且没有可恢复的可靠性契约（co-sim telemetry 重试已修复）

位置：[`qemu/upstream/hw/arm/dm_mc02_cosim_link.c`](qemu/upstream/hw/arm/dm_mc02_cosim_link.c:307-353)、[`qemu/upstream/hw/arm/dm_mc02_fdcan.c`](qemu/upstream/hw/arm/dm_mc02_fdcan.c:209-225)、[`qemu/upstream/hw/arm/dm_mc02_uart.c`](qemu/upstream/hw/arm/dm_mc02_uart.c:326-336)

co-sim telemetry、FDCAN 和 UART TX 已改用有界队列及 partial-write 续传；UART 队列满时按 drop-newest。co-sim telemetry 在控制帧占满队列时会延迟重试最新状态。协议虽然有 sequence，但没有 ACK、重传、丢失区间或 host 查询诊断的接口。

影响：高频 telemetry/CAN、慢速 host、断线重连时数据会静默缺失；不能把收到的日志当作完整 trace。

已修复 co-sim telemetry/FDCAN/UART：分别使用 8/16 项帧队列和 4096-byte UART TX ring，明确溢出策略和 partial-write 续传，绝不调用阻塞写接口；`tx_dropped`/`tx_short_writes` 已在模型状态中保留。FDCAN host wire 格式校验也已补齐；但 ACK、重传、丢失区间和 session 身份仍未实现，UART v1 原始通道仍需后续补齐。

FDCAN host TX 在 backend 尚未打开时现在会先进入 QEMU 有界队列，连接建立后续传，已修复 worker 启动竞态造成的 DM 首帧丢失。

### P1 — 外部电机 engine 语义曾不一致（Null/MuJoCo/ROS2 基础链路已修复）

位置：[`tools/dm_mc02_sim_worker.py`](tools/dm_mc02_sim_worker.py:410-630)

DM-MIT 的 QEMU/worker 闭环现在覆盖 reset、enable、位置/速度/Kp/Kd/前馈力矩和反馈编码；NullEngine、MuJoCo 与 ROS2 均遵守 enable gate 和配置扭矩上限。ROS2 通过可配置的 `sensor_msgs/JointState` 按索引接收每个电机的 position/velocity/effort；没有状态消息时退化为零状态，因此具体 Gazebo 模型仍需提供该话题才能完成有效闭环。

当前仍需补充具体 Gazebo 机构模型、时间戳对齐和多电机 ID 映射验收。

### P1 — FDCAN 接收过滤器曾被忽略（已修复当前板级路径）

位置：[`qemu/upstream/hw/arm/dm_mc02_fdcan.c`](qemu/upstream/hw/arm/dm_mc02_fdcan.c:250-350)

旧模型把收到的帧直接写入 Rx FIFO，未读取 `GFC/SIDFC` 和标准过滤器 message-RAM 元素。
现在支持标准/扩展 range、dual-ID、mask 过滤，匹配 FIFO0/FIFO1/拒绝路由、全局非匹配策略、
远程帧拒绝，并写入 HAL 使用的 `FilterIndex/ANMF` 字段。`run-fdcan-smoke.sh` 和
`run-fdcan-ext-smoke.sh` 已增加有效非匹配与 FIFO1 断言。

当前仍未覆盖高优先级消息的完整 M_CAN 语义；帧级 bit timing 已覆盖，Rx Buffer 已覆盖正常 dedicated-buffer 路径，
但 calibration/debug destination、Message RAM access failure 和完整 M_CAN 语义仍未实现。

### P2 — BMI088 仍是简化传感器模型

位置：[`qemu/upstream/hw/arm/dm_mc02_bmi088.h`](qemu/upstream/hw/arm/dm_mc02_bmi088.h:1)、[`qemu/upstream/hw/arm/dm_mc02_bmi088.c`](qemu/upstream/hw/arm/dm_mc02_bmi088.c:1)、[`qemu/upstream/hw/arm/dm_mc02_bmi088_signal.h`](qemu/upstream/hw/arm/dm_mc02_bmi088_signal.h:1)、[`qemu/upstream/hw/arm/dm_mc02.c`](qemu/upstream/hw/arm/dm_mc02.c:180)

现已支持量程/灵敏度寄存器联动、由 accel `ACC_CONF` 与 gyro `BANDWIDTH` 配置的 ODR 节流、数据手册带宽档位对应的虚拟时间一阶低通、DRDY 状态和 accel 25.6 kHz sensor-time；RESET 把 host time 映射到 QEMU virtual time，未来样本在对应虚拟时刻进入 BMI088。首个滤波样本直接初始化，零噪声时默认数据路径仍保持确定性。host 的 dps/g 输入仍不是一个完整的连续传感器模拟：模型不回放中间遗漏帧，且当前噪声仍是可配置的独立采样。

陀螺仪 soft-reset 已按真实 `0x14=0xB6` 修正，噪声近似也已做方差归一化；温度寄存器已按可配置、默认 25°C 的 11-bit BMI088 编码实现。BMI088 寄存器、采样状态和基础 accel/gyro FIFO 数据面现由可复用的 `DmMc02Bmi088` 芯片对象负责，信号处理由不依赖 QEMU 的 `DmMc02Bmi088Signal` 负责；`dm_mc02.c` 只负责板级 SPI glue。芯片对象通过 `DmMc02Bmi088ReadEvent` 报告完整 FIFO 数据帧完成及其样本序号，SPI glue 再将该事件与当前外层 `consume_step_id` 结合，调用 co-sim 的 `STEP_DONE` 消费通知。这里的样本序号不是 `step_id`，且芯片层不持有 co-sim 协议状态。

仍缺：真实滤波器离散响应/群延迟的统计校准、动态温漂、bias random walk、accel FIFO INT tag、sample-drop frame、FIFO interrupt/真实 DRDY pin 映射、gyro 外部 tag 和精确 watermark/full interrupt 时序，以及基于真实 BMI088 记录的统计校准。当前 FIFO 的 config/skip/sensortime 行为是已实现的基础数据面，但不能据此宣称完整器件级 FIFO 兼容。高频噪声/零漂姿态解算测试不能仅凭当前 QEMU 后端作为最终结论。

### P1 — IWDG 超时复位（已修复最小语义）

位置：[`qemu/upstream/hw/arm/dm_mc02_iwdg.c`](qemu/upstream/hw/arm/dm_mc02_iwdg.c:1)

此前 IWDG 只是安全寄存器窗口，固件不刷新时不会复位。当前已支持 `KR` 解锁/reload/start、`PR` `/4..../256`、12-bit `RLR`、默认 32 kHz LSI 和 QEMU virtual timer；超时请求 `SHUTDOWN_CAUSE_GUEST_RESET`，并注册 reset 清理内部状态。`run-iwdg-smoke.sh` 已验证锁定配置、启动、超时和第二次 Reset_Handler 执行。

仍未覆盖 window mode、LSI 动态误差和完整 IWDG 独立电源域；这些不影响当前 `trobot` 的普通初始化/刷新路径。

### P2 — TIM update deadline 漂移（已修复当前抽象模型）

位置：[`qemu/upstream/hw/arm/dm_mc02_tim2.c`](qemu/upstream/hw/arm/dm_mc02_tim2.c:80)

更新事件现在保存绝对 virtual-time deadline；回调延迟时按周期推进 deadline，并跳过已经错过的抽象事件，避免 callback 延迟改变长期频率或形成追赶风暴。该策略仍是当前 TIM update/DMA 抽象的 pacing policy，不等同于逐个 compare-match 波形。

### P2 — ADC、DMA 和定时器输出仍是窄切片，不能代表真实吞吐/波形

位置：[`qemu/upstream/hw/arm/dm_mc02_adc.c`](qemu/upstream/hw/arm/dm_mc02_adc.c:57-63)、[`qemu/upstream/hw/arm/dm_mc02_adc.c`](qemu/upstream/hw/arm/dm_mc02_adc.c:302-315)、[`qemu/upstream/hw/arm/dm_mc02_dma.c`](qemu/upstream/hw/arm/dm_mc02_dma.c:1-13)、[`qemu/upstream/hw/arm/dm_mc02_tim2.c`](qemu/upstream/hw/arm/dm_mc02_tim2.c:136-204)

ADC kernel source/APB 分频仍未完整建模；当前板级的 PLL2P 与 ADC common `CCR.CKMODE/PRESC` 已参与有效 ADC 时钟计算，默认连续序列保留 1 ms 最小间隔以限制 QEMU 事件量，`accurate-timing=on` 才按转换周期运行。DMA 已支持基础 peripheral-request DBM/M1AR/CT 交替，但没有 FIFO、FE/DME 和完整错误/仲裁语义。TIM8 当前是 update event 近似，不是 compare/PWM 波形。

影响：1 kHz 固件控制回路可联调，TIM12 的频率/占空比可被工具观察，但高频采样、过采样、DMA 竞态和真实 PWM 边沿/捕获测试不应据此下结论。

### P1 — DMAMUX duplicate request 可能漏服务（已修复）

位置：[`qemu/upstream/hw/arm/dm_mc02_dma.c`](qemu/upstream/hw/arm/dm_mc02_dma.c:359)

旧缓存只记录一个 `request_id -> stream`，当多个 DMAMUX channel 同时选择同一 request 时，后续 stream 不会收到该 peripheral event。当前缓存改为 8-bit stream mask：常见单 stream 仍使用快速路径，共享 request 时服务全部匹配 stream；DMA stream 的 CR/PAR 改写会使缓存失效，避免 endpoint/enable 状态陈旧。

现有 DMA、ADC、UART、SPI 和 TIM8 smoke 已回归；仍没有专门的双 stream 同 request bare-metal 验收，后续应补充该覆盖。

### P2 — Python 工具没有统一通过 uv 入口运行（已修复）

位置：[`tools/run-worker.sh`](tools/run-worker.sh:1-29)、三个 worker smoke。

`run-worker.sh` 已按 engine 统一选择 uv 或 ROS2 system Python，三个 worker smoke 和
性能基线使用项目环境；MuJoCo extra 的依赖由入口自动声明。ROS2 仍要求调用者先 source
对应发行版，这是外部环境前提而非 uv 项目依赖。

worker realtime 的初始虚拟时间偏移已修复，并由
`test_realtime_origin_is_relative_to_initial_virtual_time` 覆盖。

### P2 — register model 的 sub-word 访问语义不一致

位置示例：[`qemu/upstream/hw/arm/dm_mc02_gpio.c`](qemu/upstream/hw/arm/dm_mc02_gpio.c:32-70)、[`qemu/upstream/hw/arm/dm_mc02_gpio.c`](qemu/upstream/hw/arm/dm_mc02_gpio.c:80-126)

GPIO 已实现 8/16/32-bit lane merge 并有 smoke 覆盖；其它仍允许子字节访问的外设（例如部分 SPI/UART/timer 窗口）仍可能存在寄存器中间字节语义不统一。

建议：公共实现 `reg_read_le/reg_write_le` 和按寄存器 mask 的 lane merge；若某寄存器只允许 32-bit，应把 `valid.min_access_size` 收紧并增加 guest error，而不是宣称支持 sub-word。

### P3 — 维护入口有一个容易误导的历史 machine skeleton

位置：[`src/qemu/dm_mc02_machine.c`](src/qemu/dm_mc02_machine.c:1-35)、[`CMakeLists.txt`](CMakeLists.txt:14-19)

真正实现位于 `qemu/upstream/hw/arm/dm_mc02.c`，`src/qemu/dm_mc02_machine.c` 只是不会被编译的历史 marker，却包含同名 machine skeleton。新开发者可能误改或误测它。

建议：改名为 `integration_marker.c`、移到 `docs/`，或者在 CMake 中仅作为明确的 generated/source reference 展示，不保留同名 machine 定义。

## 测试覆盖缺口

- QMP `system_reset` 已有 GPIO 片选恢复回归；更完整的每个外设寄存器/FIFO 对比仍待补充。
- 没有 bare-metal test 证明 PLL1/PLL2 和 ADC common prescaler 的配置在转换期间联合变化；当前计算路径已实现，但该联合验收仍缺失。
- FDCAN 标准/扩展过滤器、FIFO0/FIFO1、dedicated Rx Buffer、全局非匹配策略、可选帧级
  timing 和粗粒度 BUS-OFF 已有专项覆盖；error frame、物理层 bus-off recovery 仍未覆盖。
- 没有 PLL source/FRACN/非法状态测试。
- 已覆盖 v2 控制队列和 telemetry 背压；仍没有完整的慢 chardev partial TX、断连/重连、UART/FDCAN TX queue overflow 测试。
- 没有 power rail 关闭后 CAN/485/UART/SPI 的行为测试。
- BMI088 已有 ODR、带宽阶跃、DRDY、sensor-time 和量程 smoke；仍没有基于 BMI088 实测噪声谱、零漂与姿态解算的端到端验收。
- transport 没有覆盖 non-blocking connect 完成、partial send 后错误调用新 frame、IPv6 和 listener path 并发竞争。
- MuJoCo/ROS2 已有 DM-MIT 控制项、enable gate 和 JointState 反馈接口的基础验收，但没有具体 DM-MC02 机构模型、时间戳对齐和闭环控制器验收。
- 没有 IWDG window mode、LSI 动态误差和完整独立电源域验收。

## 推荐修复顺序

1. USB 事务级 device/endpoint/枚举模型。
2. ADC 精确 cadence 与完整触发矩阵。
3. TIM 逐边沿 PWM、输入捕获和 Break/dead-time。
4. DMA FIFO/仲裁/cache 一致性及 FDCAN error recovery。
5. BMI088 噪声零漂、Gazebo/MuJoCo plant adapter 和 co-sim 可靠性控制面。
## 2026-08-30 协议版本固定复核

发现并修复一个会破坏长期连接的 P1 缺陷：QEMU 已进入 v2 session 后仍接受 v1
`RESET`，内部接收版本会降回 v1，导致后续 v2 `STEP` 被静默拒绝。现在 v1 `RESET`
在活动 v2 session 中直接被拒绝；同一连接的协议版本保持固定。`run-qemu-v2-reset-smoke.sh`
在运行中 v2 reset、v2 telemetry 和非法 v1 reset 后继续 v2 step 的路径上增加了端到端断言。

最终验证已完成：Python pytest `180 passed`、CTest `6/6`、QEMU smoke `61/61`；
session ID、ACK/重传、完整 v2 电机 section、完整 H723/USB/FIFO 语义仍是明确的功能边界。
## 2026-08-30 v2 重连与时间基准修复

本轮修复三个已确认的联调问题：

- QEMU chardev 断开后会清空当前 RX validator，但现在保留最近一次协商的协议版本；
  v2 客户端重连时先收到 v2 RESET/RESET_ACK，不会误收到 v1 28-byte RESET。
- v2 worker 使用 RESET_ACK 中的 QEMU virtual clock 建立 CAN timestamp epoch 映射，
  不再把 QEMU 绝对时间直接交给 worker 的相对 step coordinator。
- ROS2 `--ros-wait-imu` 在启动时没有首帧时也会在有界时间内等待第一条有效 IMU。

新增 v2 重连 smoke 和 worker timestamp/ROS 单测。验证结果：QEMU build、v2
reconnect/reset smoke、host CTest `7/7`、uv pytest `193 passed`。本轮未修改 `trobot/`。

仍待实现：断线后的 session 恢复、v2 Motor section endpoint 消费、完整 H723/USB/FIFO
语义，以及跨 Gazebo/MuJoCo backend 的严格锁步。

## 2026-08-30 v2 ACK 重试与幂等确认

本轮增加轻量可靠性闭环：worker 在 STEP ACK 超时后最多重发 3 次；QEMU 为最近一个已
成功 STEP 保存 step/time/dt/payload 指纹和状态，收到完全相同的重复 STEP 时只返回缓存
语义的 ACK，不再次注入传感器数据。`QUEUE_FULL` 仍使用已有的精确 payload 重试路径，
严格 `STEP_DONE` 保持独立的 2 秒消费等待。

新增重复 STEP 断言并通过 QEMU worker、v2 reconnect smoke；Python `193 passed`，host
CTest `7/7`。该机制仍不覆盖断线后的无损恢复、多步窗口和跨进程持久化状态。
## 2026-08-30 v2 motor callback 缺陷修复

本轮修复了最近 review 发现的三类真实功能问题：

- `parse_rx()` 现在按一个 STEP 可能产生的响应数预留控制队列槽位。安装 motor
  callback 且返回状态时，会同时为 `STEP_ACK` 和 `MOTOR_STATE` 保留容量，不再出现
  ACK 为 `OK` 而 MotorState 入队失败的半成功结果。
- 无 motor handler 的 `UNSUPPORTED` 快速路径现在先执行完整 STEP section/payload
  校验，非法 MotorCommand、方向错误的 MotorState 和非零 section flags 不会推进
  session。
- callback 成功后返回非法 MotorState 会记录为 `PROTOCOL`，不会把已经提交的 plant
  操作重新当作 `QUEUE_FULL` 重试；正常的后续 IMU/ADC 背压仍复用已缓存的状态。

Python codec 与 C 端已统一 section flags 为保留值 0，MotorCommand payload 内的
`flags` 仍作为 endpoint-defined metadata 原样传递。新增默认关闭的
`cosim-motor-loopback` fixture 和 `run-qemu-v2-motor-smoke.sh`，覆盖合法反馈、重复
STEP 和非法输入不推进 session。

验证结果：QEMU incremental build、motor callback smoke、CTest `7/7`、Python `209 passed`。

当前仍没有真实 plant callback 的默认绑定；完整混合 STEP 事务回滚、断线无损恢复和
多步发送窗口仍是后续工作。
## 2026-08-30 BMI088 采样层复核

本轮将 BMI088 的输入采样算法从 `dm_mc02.c` 抽为独立的
`DmMc02Bmi088Signal` C 模块。模块不依赖 QEMU 对象，负责 bias/noise/RNG、ODR
采样窗口、带宽一阶滤波、量程和 raw 饱和；板级代码继续负责寄存器地址、SPI
命令/dummy byte、FIFO、DMA 和消费确认，因此没有改变现有线协议和 FIFO 字节格式。

审查中发现新 host smoke 使用 `assert`，Release 的 `NDEBUG` 会让断言和变量检查
失效；已改为始终生效的 `CHECK`，并重新通过 `-Wall -Wextra -Wpedantic -Werror`
及 ASan/UBSan 编译运行。

验证：QEMU build、BMI088 polled/filter/FIFO smoke、CTest `8/8`、Python `243 passed`。
本轮已完成 BMI088 芯片对象抽取；回归保持既有 transaction、dummy byte、DRDY 状态、
FIFO frame 和 `STEP_DONE` 时序。后续扩展应继续通过芯片对象的窄 API 完成，不能把
co-sim `step_id` 或板级 SPI 细节下沉到芯片层。

## 2026-08-30 BMI088 芯片对象边界 smoke 复核

新增 [`tests/bmi088_chip_smoke.c`](tests/bmi088_chip_smoke.c)，直接测试可复用的
`DmMc02Bmi088` 接口：两个 die 的电源状态、断电样本拒绝、温度编码边界、越界寄存器
访问、FIFO partial read，以及 accel config frame 不误报 data-frame completion。该测试
使用始终生效的 `CHECK`，避免 Release `NDEBUG` 使回归断言失效。

由于芯片对象实现复用了 QEMU 的 `osdep.h`/内部宏，host CMake 仅对该 smoke target
启用 GNU C 扩展并使用 `-UNDEBUG`；其它 host target 的严格 C11 设置不变。Release host
build、CTest `9/9`、QEMU smoke `64/64` 和 QEMU build 均通过。QEMU 内部 GNU 宏在该目标的
`-Wpedantic` 下仍会产生既有告警，但未发现 DM-MC02 实现新增的编译错误或运行时失败。
## 2026-08-30 DMA FCR/DME 最小语义复核

本轮在 DMA 芯片层补齐了可以独立验收的 FCR/DME 子集。`SxFCR` 只锁存
`FTH/DMDIS/FEIE`，`FS` 由模型合成且同步 request 当前始终为空；direct mode
的 peripheral/memory 宽度不匹配会锁存 `DMEIF` 并清除 `EN`，`DMEIE` 只负责
IRQ 输出门控，标志仍可轮询并通过 LIFCR/HIFCR W1C 清除。FIFO mode 复用了当前
逐 request 的低字节保留宽度转换，保证 UART TDR 的 MMIO side effect 仍真实发生。

新增 [`smoke/dm_mc02_dma_fcr_smoke.c`](smoke/dm_mc02_dma_fcr_smoke.c) 及
[`tools/run-dma-fcr-smoke.sh`](tools/run-dma-fcr-smoke.sh)，测试 FCR 保留位、
DMEIF/IRQ/W1C、FIFO 16-bit 到 8-bit 转换和 HTIF/TCIF。定向 smoke 和全量
QEMU smoke `67/67` 通过；原有 DMA、UART DMA、SPI2 DMA 和 TIM8 DBM smoke
保持通过。

审查结论：本切片没有宣称完整硬件 FIFO。真实 FIFO 队列、FS 水位、FEIF 产生
条件、threshold 触发、请求仲裁和完整错误恢复仍是明确缺口；后续必须在 DMA
芯片层完成并增加独立状态/时序测试后，才能由 UART/ADC/TIM 等上层使用。
## 2026-08-30 BMI088 dynamic signal drift 复核

本轮在可复用的 `DmMc02Bmi088Signal` 层实现每轴温度系数和 bias random walk。
温漂以 25°C 为参考，random walk 使用接受样本的 virtual timestamp 按
`standard_deviation * sqrt(dt)` 更新；默认强度为零，因此现有实时路径不增加
随机游走计算。BMI088 chip object 只转发参数，machine 层以四个三轴 QOM 字符串
属性提供配置；板级 `imu-temperature-c` 同步更新 accel/gyro 两个 die。

新增 [`tests/bmi088_drift_smoke.c`](tests/bmi088_drift_smoke.c) 和
[`tools/run-bmi088-drift-smoke.sh`](tools/run-bmi088-drift-smoke.sh)。host smoke
验证温漂轴向映射、seed 确定性、ODR 拒绝不推进漂移以及 reset 保留配置/清除动态
bias；QMP smoke 验证启动和运行时属性。drift smoke、QEMU build、CTest `11/11`
（含 QEMU smoke `68/68`）和 Python `243 passed` 均通过；Release 固件 RTF
`0.999561x`，IWDG timeouts `0`。

审查结论：这是参数化、可复现的信号模型，不等同于真实 BMI088 的统计标定。
真实噪声谱、温度系数、bias random walk 和动态温度源仍需实测数据后校准；默认
配置仍保持零漂移兼容。

## 2026-08-30 DMA FIFO data-path 复核

本轮继续在 DMA 芯片层完成 FIFO 数据面，而不是把宽度转换塞进 UART 或板级
代码。每个 stream 使用四个 32-bit word 的 byte FIFO；`FTH` 决定同步 request
达到何种占用后排空，`FS` 从实际占用合成。M2P 支持 memory beat 到 peripheral
beat 的顺序拆包，P2M 支持 peripheral beat 累积到 memory beat；`NDTR` 仍按
peripheral beat 递减，HT/TC、循环重载和 FIFO reset 边界由 DMA 层处理。

`run-dma-fcr-smoke.sh` 现在同时覆盖 direct-mode DMEIF、UART TDR 拆包、UART
RDR 拼包、quarter-full FS、HT/TC 和 circular reload。WS2812 的近似
`dm_mc02_dma_advance_stream()` 也不会在 FIFO 配置下静默绕过 FIFO；该无端点 API
只支持 M2P 状态推进，P2M 仍必须经过真实 peripheral request。

审查结论：这是可复用的同步 FIFO data-path，不是完整 DMA 总线模型。FEIF 的真实
触发条件、burst/threshold 的精确 bus timing、request arbitration、FIFO 与 DBM
交互的全部边界以及完整错误恢复仍需在 DMA 芯片层继续补齐。
## 2026-08-30 DMA stream request arbitration 复核

此前 DMA1/2 的同一 DMAMUX request 会把一个 peripheral event 推进到所有
匹配 stream，绕过 STM32H723 `SxCR.PL[17:16]` 仲裁，并在 batch 路径中复制
外设副作用。现已将选择逻辑下沉到 DMA 芯片层：每个 event 只选择一个启用且
仍有 `NDTR` 的 P2M/M2P stream，高 `PL` 优先，同优先级按低 stream 编号稳定
决胜；`dm_mc02_dma_request_batch()` 对每个 item 重新选择，完成 stream 后由
其它候选接管。单匹配 stream 仍保留缓存快速路径，避免正常 UART/SPI 请求的
性能回退。

新增 `run-dma-arbitration-smoke.sh`，通过同一 UART TDR 的 DMA TX batch 验证
高优先级 stream 先完成、低优先级 stream 随后接管，再通过 UART RX 的单事件
request 验证相同选择规则；旧广播实现会产生交错 TX 或重复 RX，当前结果为
`CDAB`、`rx0=yz`、`rx1=wx`。

本轮定向 QEMU build、DMA arbitration/FCR/batch smoke 和全量 QEMU smoke
`69/69` 通过。该切片只实现 stream-level request arbitration；FIFO FEIF
真实产生条件、MBURST/PBURST 总线占用和时钟级仲裁仍未实现。

## 2026-08-30 DMA PINC live endpoint 复核

审查仲裁修复时发现 `PINC` 的第二个地址一致性缺陷。原实现已经在完成每个
request 后递增并保存 `SxPAR`，但单 request 的 `dm_mc02_dma_request_selected()`
仍把调用者传入的固定 peripheral address 交给实际读写函数。因此 request
匹配看起来正常，实际 MMIO 访问却可能重复首地址；此前的普通 UART/SPI DMA
测试没有启用 `PINC`，所以不会暴露该问题。

现在 DMA 芯片层区分两个语义：DMAMUX/peripheral request 路由使用捕获的
`reload_par` 作为 `PINC` stream 的 endpoint base，实际传输使用 live `SxPAR`；
普通 stream 保持原有固定 endpoint。SPI2 DMA smoke 先把 BMI088 gyro 寄存器
`0x10` 设为 `0xA5`，再用 `PINC` 单 request 发送 `{0x10, 0x55}`；正确行为只
执行首个 `SPI2_TXDR` 写入，寄存器仍为 `0xA5`，并且 `PAR` 增加 2。该测试覆盖
首 request 的扫描路径和后续 request 的缓存快速路径。

验证结果：QEMU build、SPI2 PINC smoke、DMA arbitration/FCR/batch smoke、
CTest `11/11`、QEMU smoke `69/69`、Python `243 passed`、shell 语法、
compileall 和 `uv lock --check` 全部通过。该修复不提升模型的 DMA 总线时序
精度；真实 FIFO/FEIF、burst 占用、时钟级仲裁和完整错误恢复仍是明确边界。

## 2026-08-30 SoC calibration ROM 复核

SoC profile 将 UID 和 ADC factory calibration 描述为 immutable data，但原实现使用
普通 RAM region，guest 可以覆盖这些值。这是芯片层的所有权错误，不能由板级 profile
或工具层补救。现在 `dm_mc02_soc_memory_init()` 使用 QEMU ROM memory region：
初始化阶段仍通过 backing pointer 写入固定测试数据，guest 侧 16/32 位 store 被忽略。

新增 `dm_mc02_calibration_rom_smoke`，在 `0x1FF1E000` 读取 3 个 UID word 和 3 个
校准 halfword，执行两种宽度的写入，再次读取并精确比较；若任一写入生效，runner
报告具体 offset/old/new 并失败。该测试只依赖 SoC memory map，不读取 DM-MC02 pin
或外设私有状态，保持了 SoC 层可复用边界。

验证结果：SoC build、calibration ROM smoke、全量 QEMU smoke `70/70`、CTest
`11/11`、Python `243 passed`、shell/compileall/`uv lock --check` 通过。不同
H723 器件的真实 UID/calibration 内容、Flash option bytes 和 ECC 仍未模拟。
## 2026-08-30 RNG continuous output refill review

此前 RNG 模型只在 `RNGEN` 上升沿填充四个字，读取完后永远保持 `DRDY=0`。
这与本工程 H723 HAL 在 `HAL_RNG_GenerateRandomNumber()` 中反复等待下一次
`DRDY` 的调用契约不符，第二次连续生成会超时。

现已把补充逻辑保留在 RNG 芯片层：四字耗尽后先让一次 `SR` 读取观察到空状态，
下一次状态轮询再填充四字并更新 IRQ。该边界由 host smoke 和 bare-metal guest
smoke 覆盖；没有把补充逻辑复制到 board 或 HAL workaround。该切片仍是功能模型，
不宣称真实熵、健康测试、时钟分频或生成延迟。

## 2026-08-30 RNG conditioning/configuration contract review

本轮 review 发现两个底层问题并在 RNG 芯片层修复。第一，错误状态只在空 FIFO
轮询路径阻止补充，`RNGEN` 上升沿和填充函数仍可能绕过错误状态；现在统一由
`dm_mc02_rng_can_generate()` 门控，保留 clock error 前已有 FIFO 数据，但在当前
`SECS` 置位时屏蔽输出、仅保留 `SEIS` 中断锁存，符合 H723 HAL 对两类错误的区别。第二，`CONDRST` 原先
只是常量，没有进入可读 CR，HAL 的置位/清零恢复序列无法观察；现在置位会保留
CR 位、清空 FIFO/错误并隐藏 `DRDY`，清零后按 `RNGEN` 重新填充。

同时补齐 H723 v3.2 `RNG_CONFIG1/2/3/NISTC/CLKDIV` 的 CR 存储和 `CONFIGLOCK`
锁后写保护，`HTCR` 也遵守同一锁。测试先验证 host 芯片状态和 IRQ 电平，再用
bare-metal guest 验证真实 NVIC 第二次 IRQ，避免只凭内部字段通过。

验证结果：QEMU 增量构建通过；host CTest `12/12`（含全量 QEMU smoke suite）、
QEMU RNG smoke 通过；Python `243 passed`，shell 语法、compileall 和 `uv lock
--check` 通过。当前仍未模拟真实熵、健康测试、错误注入或时钟分频引起的生成
延迟；配置字段的支持限于固件可见寄存器契约。

## 2026-08-30 RNG follow-up review

Terra review 发现 runner 只等待早期启动 marker，存在读取未完成结果块的时序风险；
已改为等待 guest 最后写入的 `DONE` marker。原 runner 还把 refill 后的第六字当成
第五字，现已按真实读序校验第五字和第六字的固定 xorshift32 值。

对照 H723 HAL 的 `RNG_RecoverSeedError()` 后，确认 `SEIS` 是中断锁存而 `SECS`
才是当前 seed 错误。模型已改为仅以 `CECS/SECS` 阻止生成，并允许
`SECS=0, SEIS=1` 时保留/读取 FIFO；host smoke 新增该自动恢复边界。配置字段、
`CONFIGLOCK`、`CONDRST` 和错误注入尚未全部通过真实 guest MMIO 覆盖，仍保持为
明确的测试缺口，而不是宣称已完成的硬件语义。

验证结果：`build/host/dm_mc02_rng_smoke`、`tools/run-rng-smoke.sh` 和 QEMU
增量构建通过。芯片层编译仍会显示 QEMU 内部头文件已有的 GNU 扩展/未使用参数
warning，本轮未引入新的编译错误。

## 2026-08-31 advanced timer output-gate review

TIM1/TIM8 之前复用了通用 timer 的 PWM 观察逻辑，却没有考虑高级定时器的
`BDTR.MOE` 和 Break 输出级。这样会让外部观察者在硬件实际关闭输出时误判 PWM
仍然有效。修复保留在 timer 芯片层：仅由 board profile capability 打开的实例
解释 `BKE/BKP/AOE/MOE`，普通 timer 不受影响；Break 只清除外部主输出的 MOE，
不会阻断 OCREF、compare、DMA 或 TRGO/TRGO2 内部路径。

公共 `dm_mc02_tim2_set_break_input()` 接口接收物理 BKIN 电平，释放后的自动恢复
只发生在下一个 qualified update 且 `AOE=1` 时。输出观察增加 CH1..CH3 互补门控、
`DTG` 分段解码和 CKD 换算，并正确解码 H723 高级 timer 的 `OCxM[3]` 分裂位；
未实现扩展模式不会再别名到低三位。TIM2/TIM8/TIM1 定向 qtest `21/21` 通过，
QEMU 主体重新链接通过。

剩余风险：当前死区是基于虚拟相位的点采样门控，不提供逐边沿事件；BKIN2、LOCK
写保护、完整 OSSI/OSSR/OIS off-state、COM/UEV 细节和组合/边沿 OCxM 仍未实现。
因此该切片仍不能用于验证完整半桥死区或电机安全时序。

## 2026-08-31 USB control-transfer core integration review

本轮把 EP0 的 control-transfer 状态从 `dm_mc02_usb.c` 下沉到板卡无关的
`DmUsbControlDevice`。producer 是 setup/IN/OUT packet，boundary 是 core 的阶段、
分包、缓存和 status 提交语义，consumer 是 DM-MC02 descriptor/CDC callback 与 DCFG
状态。DM-MC02 不再重复解析标准请求；`SET_ADDRESS` 和 `SET_CONFIGURATION` 只在
status-IN 消费时通过 callback 提交，避免 setup 阶段提前改变 device state。

构建初次暴露的第一个错误是 core 独立 unit target 使用了未定义的 QEMU `MIN` 宏，
并在 descriptor 已位于内部缓存时产生自拷贝。修复位于公共 core：使用本地 `min_size()`
并跳过同一地址的 memcpy；没有把问题转移到 board 或测试 fixture。随后
`test-dm-usb-control` 为 `5/5`，DM-MC02 USB qtest 为 `7/7`，新增覆盖 CDC
`SET_LINE_CODING` 的 setup/data/status 三阶段、3+4-byte OUT 分包和 board-visible
EP0 STALL。

残余风险：这是 control-transfer core 和私有 packet harness，不是完整 USB。token/PID/
data toggle、NAK/STALL 的总线传输语义、endpoint 状态机、DMA、PHY、电气时序、真实
QEMU USB bus attachment 和宿主机枚举仍未实现。当前 serial slot 10 的 FIFO0 原始
byte pipe 也未被改变，不能把 CDC callback qtest 当成宿主机可用 USB 设备。

全量 host CTest 首次运行还暴露了既有 `run-fdcan-medium-smoke.sh` 的一个测试 oracle
问题：guest 将 `TXFQS` 与 `TXBRP` 分成两次 MMIO 读取，在高负载下第一帧可在两次读取
之间完成，原断言错误地要求两个值构成原子快照。单独重跑显示 FDCAN 模型状态正确；
现已只拒绝违反时间方向的组合，并保留准确时序的非空 pending 与最终 `TXBRP=0` 检查。
该修复属于 smoke 验证层，不改变 FDCAN 模型。

## 2026-08-31 USB transaction core integration review

本轮继续沿 USB 可复用层向上切片，新增 `DmUsbTransactionDevice`。它只拥有 token
路由、端点配置、每方向 DATA PID toggle、NAK/STALL/INVALID 映射以及 halt 恢复；EP0
阶段仍由 `DmUsbControlDevice` 拥有，EP1..EP15 通过带 `timestamp_ns` 的 IN/OUT
callback 提供数据。DM-MC02 仅实现 callback 和 qtest adapter，不把 DWC2 寄存器或
板级队列策略下沉到通用层。

验证结果：QEMU 增量构建通过；`test-dm-usb-transaction` `5/5`；DM-MC02 USB
qtest `8/8`；DWC2 controller-ready bare-metal smoke 和虚拟 USB CDC pipe smoke
均通过。第一次 qtest 运行缺失 `QTEST_QEMU_BINARY`，补充当前构建路径后通过；该
问题属于运行入口环境，不是模型错误。

复核时修正了两个边界：callback 现在接收 producer 的虚拟时间戳，避免板级 adapter
重新读取 wall/当前时间；control STALL 会锁存 EP0 halt，新 setup 清除它。空 IN
只返回 NAK，不置 `DAINT/DIEPINT` 完成状态。

残余风险：当前 transaction 层是同步、内进程的设备侧 dispatcher，没有 async
completion/cancel、USB bus upstream host 拓扑、真实 DWC2 device-mode FIFO/DMA、
SOF、PHY、电气时序或宿主机枚举。PID mismatch 目前是调用者契约错误并返回
`INVALID`，尚未模拟真实 USB duplicate-data ACK/retry；这些行为应由 synthetic host
和 transport 层在下一道隔离测试中定义，不能由当前私有 qtest harness 通过替代。

## 2026-08-31 USB synthetic upstream host review

本轮在 transaction dispatcher 之上增加板卡无关的 `DmUsbHost`。它通过单一
`DmUsbHostSubmitTransaction` 回调生成 SETUP/IN/OUT token，按 EP0 或调用者提供的 MPS
完成控制传输和 bulk 分包，并返回精确的 data 长度、token 数和 NAK/STALL/INVALID
状态。host 不读取或修改 device 的 PID、halt、端点队列或 DM-MC02 状态，因此可复用于
未来的 QEMU transport、其它 board profile 和独立 fixture。

验证结果：QEMU 增量构建通过；`test-dm-usb-host` `4/4`、既有
`test-dm-usb-transaction` `5/5`、DM-MC02 USB qtest `8/8` 通过。首个失败曾暴露
host control OUT 结果没有累计 data-stage 长度，修复在 host 的结果聚合处并由 control
OUT 回归锁定；随后又补充无 data-stage `SET_ADDRESS`，确认不会伪造额外 OUT token。

残余风险：该 host 是同步、进程内的 token producer，不是 USB host controller；没有
SOF/帧调度、设备枚举拓扑、异步完成/取消、总线仲裁、真实 duplicate DATA ACK/retry、
DWC2 device-mode FIFO/DMA/IRQ、PHY 或宿主机 USB 设备挂载。下一切片应连接明确的
transport adapter，再独立实现 DWC2 device-mode 状态机。

## 2026-08-31 USB DWC2 device-mode core review

本轮继续在可复用 USB 器件层实现 `DmUsbDwc2Device`，没有修改 `hcd-dwc2.c`、
DM-MC02 board wiring 或 `trobot/`。producer 是 `DmUsbHost`/未来 transport 的
transaction，boundary 是 DWC2 endpoint register/FIFO/IRQ API，consumer 仍待由
DM-MC02 MMIO/NVIC adapter 接入。EP0 复用已有 control core，EP1..EP15 共享
transaction dispatcher 的方向、MPS、PID 和 halt 契约。

首个构建错误是新核心实现和头文件之间的宏前缀不一致，随后暴露了 unit target 漏掉
`dm_usb_host.c` 的链接依赖；两处分别在 DWC2 实现和测试构建边界修复。逐行检查还
发现 endpoint register 的跨界/3-byte 访问会进入未定义移位路径，已统一拒绝；复位
时如果 IRQ 已经拉高，现会显式回调 low；FIFO-consuming read 的 `const` 误导也已
清除。

验证结果：DWC2 unit `5/5`；USB control/transaction/host units `5/5`、`5/5`、
`4/4`；DM-MC02 USB qtest `8/8`；host CTest 串行 `22/22`。新核心仍未进入 DM-MC02
地址空间，因此这些结果只证明板卡无关的寄存器/FIFO/transaction/IRQ 契约，不证明
真实 H723 USB 枚举或宿主机可挂载设备。

残余风险：当前 FIFO 是每 endpoint 固定 4096-byte 软件环，`GRXFSIZ` 只作为 OUT
容量上限；没有 DMA、RX status queue、SOF、PHY、电气时序、USB bus attachment、
async completion/cancel、真实 duplicate-data retry 或完整 DWC2 register quirks。
下一道集成门应是 DM-MC02 地址映射和 NVIC IRQ 接线，且需先补板级 guest smoke。

## 2026-08-31 QEMU USB device adapter review

本轮把已经验证的 board-independent transaction contract 接到 QEMU 8.2.2 的
`USBDeviceClass` 回调，新增 `DmUsbQemuAdapter`。producer 是 QEMU generic USB
`USBPacket`，boundary 是 adapter 的同步控制/数据转换，consumer 是
`DmUsbTransaction` submit/reset callback；DM-MC02 和 DWC2 寄存器没有下沉到该层。

首个真实失败是 `dm_usb_qemu_adapter_submit_setup()` 对数组参数使用
`sizeof(*setup)`，导致 setup transaction 长度为 1 而非 USB 要求的 8；已在 adapter
层固定 SETUP 长度并由测试锁定。另一个测试边界问题是 unit main 没有调用 QOM 类型
注册，现已显式初始化 `MODULE_INIT_QOM`。裸链接 `core.c/bus.c` 需要的 monitor、pcap
和 migration 元数据入口只在 unit harness 中 stub，不影响 system build。

逐行 review 后将能力收窄为 Full-Speed/64-byte packet，避免原先 Full/High-Speed
声明与 64-byte bulk contract 不一致。adapter 对同步 submit 回调增加轻量重入门禁，
嵌套 handler 不会在外层 transaction 期间写入共用 `USBDevice.data_buf`。

验证结果：`ninja -C build/qemu tests/unit/test-dm-usb-qemu-adapter -j2` 成功；
`build/qemu/tests/unit/test-dm-usb-qemu-adapter --tap` 为 `5/5`，control 测试走
真实 QEMU `usb_handle_packet()` 的 SETUP/DATA/STATUS 路径。当前仍不能宣称 DM-MC02
宿主机 USB 挂载：adapter 尚未接入 machine 的 QEMU `USBBus`，也没有 host controller、
枚举、SOF、async completion/cancel、PHY 或外部宿主机设备联调。下一步是独立的
real `USBBus` attachment fixture/transport，不应默认给 DM-MC02 添加虚假 host controller。

## 2026-08-31 QEMU USB adapter real-bus fixture review

本轮只推进可复用 USB transport adapter 到 generic QEMU bus 的边界，不改 DM-MC02 机器
组合。unit fixture 以具体 QOM `DeviceState` 持有真实 `USBBus`，注册一个 Full-Speed root
port，并在安装 submit/reset callback 后通过 `usb_realize_and_unref()` 挂接
`DmUsbQemuAdapter`。port reset 的 detach/attach/reset callback、Full-Speed 匹配、地址 0 的
`usb_find_device()` 路由，以及 device unparent -> port unregister -> bus release -> bus
unparent 的销毁顺序均由断言覆盖。

首个 fixture 错误是把 QEMU endpoint array 的索引 1 当成 endpoint 1；该数组的索引 0 才是
endpoint number 1，因此 transaction 层正确拒绝了未配置的 endpoint 2。修正测试索引后，
adapter unit `6/6` 通过。随后 `qemu-system-arm` 重链、DWC2 controller-ready/legacy CDC
pipe smoke、host CTest `22/22` 和整机 QEMU smoke `79/79` 均通过。

残余风险没有变化：fixture 只验证 QEMU generic USB 对象、port 与 packet 生命周期，不提供
DM-MC02 的 USB host controller、真实 OTG mode、枚举/SOF、异步 completion/cancel、DMA、PHY、
USB 时序或宿主机设备 passthrough。后续必须先定义 board-independent host-controller transport
接口和其隔离测试，不能把 test fixture 接线误作板级硬件能力。

## 2026-08-31 QEMU adapter to DWC2 transaction boundary review

本轮仅连接两个已存在的可复用层：QEMU `USBPacket` 通过 `DmUsbQemuAdapter` 转为带虚拟
时间戳的 `DmUsbTransaction`，fixture 的 callback 将它交给 `DmUsbDwc2Device`。没有把
`USBPacket`、QOM、`USBBus`、DM-MC02 MMIO、pin map 或 DWC2 endpoint 私有字段引入
transaction/DWC2 core；DWC2 仍独占 endpoint arming、FIFO、`DIEPTSIZ`、`DIEPINT` 和 reset
状态。

边界测试在真实 test-only `USBBus` 上配置 DWC2 EP1 IN FIFO，按地址 0 路由 QEMU packet，
验证 payload、`DIEPTSIZ=0` 和 `DIEPINT.XFRC`。随后 port reset 经 adapter callback 调用
`dm_usb_dwc2_reset()`，EP1 control 状态清零且设备重新以 default address 可寻址。adapter
unit `7/7`、DWC2 unit `5/5` 通过；只更新 unit target 的 source dependency，未改 system
source、DM-MC02 machine 或 `trobot/`。

这仍不能证明 QEMU HCD 帧调度、USB 枚举、SOF、异步 complete/cancel、DMA、PHY、OTG role
切换或板级 external USB transport。下一步若要接入板卡，必须先以板卡无关的
host-controller transport 契约描述这些事件，再从 H723/DM-MC02 所属层实现，不能把当前
同步 callback 直接作为真实 host controller。

## 2026-08-31 STM32H7 host-channel/transaction boundary review

本轮先检查 upstream DWC2 host 的 channel interrupt、`HCTSIZ` 递减和 NAK 行为，再把
最小、可测的子集留在 STM32H7 芯片层。`DmStm32H7OtgHost` 使用真实的 host channel 和
summary-register 偏移；`HAINT` 只反映 `HCINT & HCINTMSK`，再由 `HAINTMSK` 派生
`GINTSTS.HCINT`。因此 guest 必须 W1C channel 原因，不能错误地通过清 global bit 遗失
底层中断状态。

首个设计风险是为 12 个同步 channel 预留 12 份 2047-byte transport scratch buffer。该
adapter 在 start callback 内同步提交并完成 transaction，不存在 buffer 的跨 channel 存活期；
已收敛成单一 2047-byte buffer，减少约 22 KiB 实例常驻内存，同时把“同步、不可重入”写入
接口。这样没有为尚未存在的 async path 引入锁、队列或额外错误分支。

transport adapter 是直接的、但仍板卡无关的 transaction consumer：OUT 由 `read_out`
callback 供给，IN 经 `write_in` callback 回送，SETUP/IN/OUT 和
ACCEPTED/NAK/STALL/INVALID 都映射到公开的 channel completion。它不访问 controller
私有状态、不解析 QEMU USBPacket，也不让 device model 知道 HCCHAR。边界 tests 分别覆盖
controller `9/9`（含 multi-packet、NAK、W1C/IRQ）和 transport `3/3`（精确 OUT、短 IN、
NAK-to-STALL）。

系统验证重建了 Release ARM QEMU，DM-MC02 USB qtest `9/9`、两项既有 USB bare-metal
smoke 和全量串行 QEMU smoke `79/79` 均通过。`trobot/` 没有改动。

残余风险保持显式：没有 host FIFO/PIO window、HCDMA/DMA 地址搬运、SOF/HFNUM 调度、
packet retry 定时、NYET/ACK/babble/overcurrent、split transaction、isochronous DATA2/MDATA、
PHY/VBUS/SE0、电气时序、async completion/cancel、host enumeration、QEMU USB bus 或
passthrough。当前 DM-MC02 profile 仍是实际固件使用的 USB Device mode；不得把该芯片层
接口当作板级 USB host 支持。下一切片应只推进虚拟时间 SOF/frame 调度，并继续保持
host-role machine 未组合。

## 2026-08-31 STM32H7 host SOF/frame timing review

SOF 没有被放进 QEMU wall-clock timer 或 DM-MC02 board。芯片层只接收调用者给出的单调
virtual timestamp，因而可由未来 board adapter、replay fixture 或外部锁步驱动复用。port
reset release 建立 frame epoch；Full/Low-Speed 采用 1 ms，High-Speed 采用 125 us。每个
frame 同时更新 `HFNUM.FRNUM`、W1C `GINTSTS.SOF` 和现有 channel service 边界，避免另建
一条与 transaction 不一致的调度路径。

复核发现 PWR-off 若只清 port bit 而不停止 frame clock，会在无供电端口上继续发 SOF；已在
`HPRT0` 状态转换所属层修复，并新增 reset-release 才可重新启动的回归。host controller
unit 目前 `12/12`，SOF 行为仍未接入 current device-role board。剩余限制更新为：没有
host PIO FIFO、HCDMA/DMA、frame-time retry/timeout、NYET/ACK/babble、split/isochronous、
PHY/VBUS、电气时序、async completion、枚举或 QEMU host bus。下一切片应仅实现 PIO FIFO
packet boundary，不能借 transport scratch buffer 假装 guest 可见 FIFO。

## 2026-08-31 STM32H7 host PIO FIFO boundary review

本轮只推进 host-controller 数据面。可复用 `DmUsbHostPioFifo` 是固定 2048-byte 环形
字节队列，不包含 QEMU/QOM、DM-MC02 地址、DMA、USBPort 或外部设备策略。`DmStm32H7OtgHost`
为十二个 channel 各组合一个 OUT 和一个 IN queue，通过 `HCFIFO(n)` 的 little-endian
32-bit PIO 访问暴露给 guest；合计固定常驻 48 KiB，避免每 packet 分配。

OUT reader 使用 exact-read，只有完整的当前 packet 才消耗 FIFO，缺少数据会终止 channel
而不会让半包进入 transaction consumer。IN writer 只有整包完整放入 FIFO 才成功，容量耗尽
映射为 `HCINT.XACTERR|CHHLTD`。controller reset 清空所有 host PIO stream。该路径保持
同步、无锁、无线程和单个 2047-byte transport scratch buffer，未提前引入 async 队列。

复核发现 transport 在 consumer 报告 accepted 后先前未确认 `actual_length` 是否不超过本次
issued packet；异常 consumer 可能令 IN writer 读取 scratch buffer 的未定义范围，然后才由
channel 层报错。现已在 transport boundary 先拒绝 oversized accepted result，直接完成为
`XACTERR|CHHLTD`，并由独立回归锁定。此检查属于外部 callback 的实际边界，不是对内部
不可能状态的冗余防御。

定向验证：FIFO unit `3/3`、host-controller unit `13/13`、channel transport unit `6/6`。
覆盖环绕/容量、exact-read 不消费、PIO word 字节序与 reset、精确 OUT/短 IN、IN FIFO 满和
oversized IN result。Release `qemu-system-arm` 重链、host-port `2/2`、DWC2 `6/6`、
transaction `5/5`、DM-MC02 USB qtest `9/9` 和完整串行 QEMU smoke `79/79` 也全部通过；
`trobot/` 未修改。

残余边界必须保持明确：目前每 channel 的 PIO stream 只是同步 packet transport，未实现
真实 DWC2 的 RX status/FIFO arbitration、可编程全局 TX/RX FIFO、FIFO flush、HCDMA、
cache coherency、async completion/cancel、device enumeration、PHY/VBUS、电气时序、QEMU
USB host bus 或 host-role DM-MC02 machine。现有 DM-MC02 仍维持真实固件使用的 USB Device
mode，不能因为该芯片层模型而宣称 board host USB 支持。

## 2026-08-31 STM32H7 HCDMA and data-path boundary review

上游 DWC2 的实际 channel register layout 表明 `HCDMA(n)` 位于 `0x514+0x20*n`，并且
success 后按 `actual_length` 推进，NAK/STALL/error 不推进。本轮将该部分先留在
`DmStm32H7OtgHost` 芯片层：`GAHBCFG.DMAEN` 是唯一 gate，`HCDMA` 仍只是 byte-address
寄存器，不让 controller 知道 QEMU memory map 或 board policy。

测试此语义时暴露了首个相关错误：controller reset 之前只清 FIFO，遗漏清除 `channel[]`
的 registers、active/waiting state。该问题会让 reset 后的 host 从旧 `HCCHAR/HCTSIZ` 或
DMA address 继续。修复把 channel state reset 放回 host-controller 层，并以 HCDMA case
同时断言 DMA successful progress、PIO/NAK 不推进和 reset 清零。

随后新增 `DmUsbHostChannelDataPath`。它只根据 `DMAEN` 在已存在的 PIO FIFO 和注入的
physical-address memory callback 之间选择：OUT 在 current `HCDMA` 读、IN 在 current
`HCDMA` 写；completion 仍由 controller 单点推进地址。直接 transport fixture 验证 DMA
OUT/IN 都绕过 PIO stream、传递精确字节且 HCDMA 进位正确。callback 缺失或拒绝返回 false，
沿既有公开边界形成 `XACTERR|CHHLTD`，没有把 QEMU `AddressSpace` 或额外异步队列写入
可复用层。

当前验证：host controller `14/14`、data-path `3/3`、channel transport `7/7`。完整系统
回归已完成：Release `qemu-system-arm` 重链、host PIO `3/3`、host-port `2/2`、DWC2
`6/6`、transaction `5/5`、DM-MC02 USB qtest `9/9` 和完整 QEMU smoke `79/79` 均通过。

DMA transport 的首轮直接测试曾触发段错误。定位后确认这不是 memory callback 或 HCDMA
问题，而是原 transport 只有一个 `opaque`，submit 和 data-path selector 需要不同 owner
却被强行共用。现在新增三个独立 callback context 的 initializer，并保留单 context API
作为兼容包装；针对 DMA selector 的 integration test 与七项 transport 回归都通过。

残余边界：尚未绑定 QEMU system memory 或任何 board，因而没有 cache coherency、AHB
burst/error、descriptor DMA/`HCDMAB`、异步 completion、全局 FIFO arbitration、QEMU USB
bus、枚举、PHY 或 host-role DM-MC02。该代码是未来 host profile 的底层 producer，绝不改变
真实 `trobot` 使用的 DM-MC02 device-mode USB 接线。

## 2026-08-31 QEMU AddressSpace host-DMA adapter review

`DmUsbHostQemuMemory` 将已经完成的通用 HCDMA data-path callback 绑定到调用方提供的
QEMU `AddressSpace`。这是刻意放在 QEMU adapter 层的很薄一层：芯片模型继续只看到
同步 physical-address read/write contract，DM-MC02 继续没有 host-role wiring，adapter
只调用 `dma_memory_read()`/`dma_memory_write()` 并把非 `MEMTX_OK` 归一为 false。这样地址
推进、transaction result 和 `HCINT.XACTERR|CHHLTD` 仍分别由既有 controller/transport
owner 决定，不会把 QEMU memory-map 或 board policy 倒灌到可复用层。

隔离测试用受控 `address_space_rw()` stub 断言 transaction 方向、地址、长度、字节和错误
映射；同一份 adapter 也由 transport integration case 经 `data path` 调用，避免只测试一个
未接入的 wrapper。该测试策略刻意没有将 `system/memory.c`/`system/physmem.c` 链进轻量 unit
target，因为那会把完整系统静态依赖带入本应只测 callback contract 的测试。

真实系统链接已由重链 Release `qemu-system-arm` 验证；host PIO `3/3`、host controller
`14/14`、data path `3/3`、QEMU-memory adapter `1/1`、channel transport `8/8`、host-port
`2/2`、DWC2 `6/6`、transaction `5/5`、DM-MC02 USB qtest `9/9` 和完整 QEMU smoke
`79/79` 均通过。即使如此，它仍不表示实现 cache coherency、IOMMU、AHB burst/fault IRQ、
descriptor DMA、async completion、host USB bus、enumeration、PHY 或 DM-MC02 USB host
支持。

## 2026-08-31 QEMU host-device transport composition review

本轮新增的 `DmUsbHostQemuTransport` 只位于 QEMU adapter 层。它持有调用方选择并保持
生命周期的一个 `USBDevice`，把已有 `DmUsbTransaction` 转成栈上的 `USBPacket` 后同步调用
QEMU USB core；H723 controller 仍独立拥有 FIFO/HCDMA/HCINT，DM-MC02 board 没有新增 host
mapping。最小直接链路已验证为 `HCFIFO -> host channel -> QEMU USB device -> generic
transaction device`，因此这不是绕过芯片层的 packet harness。

复核中首先发现 async fixture 的 `USBPortOps.attach/detach` 为空，而 QEMU 在 device realize
时无条件调用两者，导致测试在提交 packet 前空指针崩溃。修复是 test fixture 层补充明确 no-op
callbacks，adapter 的 async 语义保持不变。随后既有 reset test 把 reset 前单独采样的 virtual
clock 与 callback 内部采样要求相等；运行中时钟前进 1 us 证明该 oracle 不成立，已改为要求
callback timestamp 位于调用前后两个单调采样之间。

adapter 在目标未 reset/default state 时返回 `INVALID`，避免 QEMU `usb_handle_packet()` 的
state assertion；对真实 async/queued packet 立即调用 `usb_cancel_packet()`，使 stack packet
不留在 endpoint queue。新 adapter test `10/10` 覆盖 SETUP、OUT、短 IN、NAK、STALL、未就绪
port、async cancel 和完整 PIO channel composition。Release `qemu-system-arm` 重链、相邻 USB
unit、DM-MC02 USB qtest `9/9` 与完整 smoke `79/79` 均通过。

残余边界明确：当前只针对一个调用方选定的 QEMU device，未使用 host channel request 的
device address；没有 hub/topology/routing、多设备枚举、PID/toggle、async completion、DMA
composition、isochronous、PHY、passthrough 或 board host-role mapping。任何多 device 扩展都
必须先在可复用 transport 层定义地址路由契约，不能将这个 test fixture 接入当前 USB Device
mode 的 DM-MC02 machine。

## 2026-08-31 USB host address-routing review

本轮先追踪 `HCCHAR.DEVADDR` 的完整数据流。芯片层已在
`DmStm32H7OtgHostChannelRequest` 中保存 address，但旧
`DmUsbHostChannelTransportSubmit` 只接收 address-free transaction，导致该字段在 transport
边界被丢弃。修复没有把 QEMU 或 board 策略倒灌到 `DmUsbTransaction`：新增可选
`DmUsbHostChannelTransportRoute(opaque, device_address, transaction)`，未安装时仍精确保留旧
single-target submit 行为。两项 transport 回归分别锁定 nonzero address 的兼容提交与
`DEVADDR=37` 的精确传播。

QEMU 适配层的首个实际失败出现在 `SET_ADDRESS` 的 status-IN。之前 adapter 对所有
host-to-device control transfer 都先提交一个 OUT data packet；无数据 `SET_ADDRESS` 的下层
control state 已直接位于 `STATUS_IN`，因此额外 OUT 返回 `INVALID`。修复使 data OUT 只在
`wLength > 0` 时发生。status-IN 成功后，adapter 才对严格的标准、device-recipient
`SET_ADDRESS` 更新 QEMU `USBDevice.addr`；port router 只调用 `usb_find_device()`，没有伪造或
猜测地址。新 QOM fixture 验证 setup/status 前仍只能按 address 0 路由，status 后 address 0
不可达而 address 13 可达，并能继续完成 GET_STATUS。

Release `qemu-system-arm` 重链、所有受影响 USB unit、DM-MC02 USB qtest `9/9` 与完整
QEMU smoke `79/79` 均通过。power-boundary smoke 仍输出既有 QEMU header 的 `-Wpedantic`
warning，非本切片引入。残余风险不变：当前只验证 QEMU tree 中已连接设备的地址查找，不证明
hub 枚举、多个真实设备并发、PID/toggle、async completion delivery、isochronous、PHY/VBUS、
passthrough 或 DM-MC02 host board profile。

## 2026-08-31 QEMU USBPort lifecycle review

地址路由之后，QEMU `USBPort` 仍只由 fixture 手工维护，因此无法验证真实 attach/detach 到
H723 `HPRT0` 的边界。本轮新增 `DmUsbHostQemuPort`，它位于 QEMU adapter 层并注册一个 root
port；attach 按已协商 QEMU speed 连接 H723 host，detach 走 host 已有的 disconnect state
transition。芯片层因此不依赖 QOM、USBBus、USBPort 或 QEMU device lifetime。

这里的关键风险是 QEMU `usb_port_reset()` 以 detach/attach 实现 bus reset。若 adapter 直接把
这两次回调传入 `set_port_connected()`，host 会看见一次虚假的物理断连，清除 `ENA` 并重新置位
`CONNDET`。adapter 的 reset callback 只在调用 `usb_port_reset()` 的同步窗口抑制 lifecycle
回调；测试先 W1C 清除 initial `CONNDET`，再验证 HPRT reset release 后 connection/enable
保持、`CONNDET` 不会重现、QEMU address 归零，最后验证真实 detach/re-attach 仍产生变化位。

Release `qemu-system-arm` 已重链；QEMU adapter `12/12`、host controller `14/14`、channel
transport `10/10` 和 DM-MC02 USB qtest `9/9` 通过。该 adapter 仍不是 host board、hub
enumeration、topology policy、PID/toggle、async completion、PHY/VBUS、passthrough 或当前
DM-MC02 USB host 支持；这些限制将保留到独立 host-role board profile 的下层契约均完成之后。

## 2026-08-31 STM32H7 host control scheduler review

本轮只在 H723 host-controller fixture/client 层新增
`DmUsbHostChannelControl`。它不拥有 QEMU `USBDevice`、`USBPort`、DM-MC02 地址映射或
板级策略；唯一职责是以一个已选 host channel 依次编排 SETUP、MPS 分包 data 和反向
zero-length status，并从既有 `HCINT/HCTSIZ/HCFIFO` 的结果观察 completion。这样 QEMU
router、port lifecycle 和 device model 继续是独立的 consumer，而不是将枚举逻辑写进
transport 或 board。

首次接入时，QEMU adapter unit 的链接失败准确暴露出 `tests/unit/meson.build` 漏掉了新 helper
source，而不是控制器行为错误；已只将 source 加入该 target。随后 lifecycle integration
fixture 误把真实 attach 已锁存的 `HPRT0.CONNDET` 当成 reset 产生的变化位。按 HPRT0 W1C
语义在 reset 前清除后，测试能够验证 host reset 不会伪造物理拔插，且实际 port lifecycle
实现无需变更。

代码复核还发现两个应该在 controller boundary 拦截的问题：8-bit `device_address` 若大于
127，左移写入 `HCCHAR` 会触及 `DEVADDR` 以外的 bit；异常 transport 若对 control OUT
报告短的 accepted completion，helper 不能把它返回为成功。现在前者在任一寄存器写入前拒绝，
后者映射为 `INVALID`；4 项隔离测试分别覆盖分包 IN、OUT/status、status 后地址转换和越界地址。
QEMU integration 使用真实 `DmUsbHostQemuPort` 和 port router 完成
`GET_DESCRIPTOR -> SET_ADDRESS -> GET_STATUS`，证明地址 0 在 status-IN 前有效、之后只按
地址 13 路由。

验证：Release `qemu-system-arm` 已重链；host controller `14/14`、control scheduler `4/4`、
data path `3/3`、channel transport `10/10`、QEMU-memory `1/1`、PIO `3/3`、port `2/2`、
QEMU adapter `13/13`、control `6/6`、transaction `5/5`、DWC2 `6/6` 与 DM-MC02 USB qtest
`9/9` 全部通过。完整串行 QEMU smoke suite 为 `79/79`。power-boundary smoke 仍有既有
QEMU 头文件的 `-Wpedantic` warning，未由本切片引入。`trobot/` 未修改。

残余风险保持明确：该 helper 是同步、单 channel 的 fixture/integration client，并非 guest
firmware host driver。没有 descriptor parser、endpoint configuration、address allocation、hub/
topology、多 device 并发、NAK retry/timeout、异步 completion/cancel、isochronous、严格 PID/
toggle、全局 FIFO/HCDMA descriptor、PHY/VBUS/passthrough 或 host-role DM-MC02 board。下一步
必须先为独立 host-role board profile 定义最小 machine composition contract；当前 DM-MC02
`USB1_OTG_HS` 继续只表示 USB Device mode。

## 2026-08-31 STM32H723 host-role reference profile review

本轮将已验证的通用 host/controller、QEMU transport、port lifecycle 和 QEMU memory adapter
放入一个新的 `DmStm32H7OtgHostQemu` SysBus wrapper。wrapper 是 QEMU ownership boundary：
它只拥有 MMIO、IRQ output、virtual SOF timer、USBBus/root port 和 QEMU `AddressSpace` binding；
通用 controller 不依赖 QOM，board profile 也只选择地址与 IRQ。这样 host-mode 组合不会反向
污染 DM-MC02 Device-mode USB adapter。

首次实现复核发现 FIFO 的 generic controller API 接收明确的访问宽度，而普通 MMIO read path
没有 width parameter。若 wrapper 对 FIFO byte-read 固定调用 32-bit read，会每次多消费三个
字节，导致 guest descriptor payload 发生静默错位。修正后的 wrapper 在 FIFO window 直接调用
width-preserving FIFO API，寄存器访问仍经通用 controller；新 qtest 以逐字节读取的 descriptor
验证了这个边界。该问题在 profile integration 前被局部化修复，没有传到 transport 或 board。

`stm32h723-usb-host` 只提供 H723 host-role reference composition，测试通过真实 QEMU
`usb-kbd` 验证 attach、HPRT0 reset、IRQ 77、virtual SOF 和 EP0 `GET_DESCRIPTOR`。这证明
machine/MMIO/IRQ/port/router path，不证明 hub enumeration、多 endpoint configuration、async
completion、PID/toggle、global FIFO/HCDMA descriptor、PHY/VBUS/electrical timing 或 passthrough。
尤其 `dm-mc02` 继续在同一 `0x40040000` window 使用 USB Device mode，不能因 reference profile
存在而宣称 DM-MC02 host USB support。

验证：Release `qemu-system-arm` 重链、新 profile qtest `2/2`、host controller `14/14`、
control scheduler `4/4`、channel transport `10/10`、QEMU adapter `13/13`、DM-MC02 USB qtest
`9/9` 和完整串行 QEMU smoke `79/79` 均通过。power-boundary smoke 的 `-Wpedantic` 输出来自
既有 QEMU headers；本切片没有引入新的编译 warning。

## 2026-08-31 STM32H723 guest EP0 control smoke review

新增的 Cortex-M7 guest smoke 首次报告 `SET_ADDRESS/GET_STATUS` 失败。寄存器级排查先确认
descriptor、address routing 和每个 control stage 均成功：最后一 stage 的
`HCINT=XFRC|CHHLTD`，caller 的 `r0` 和 `RESULT[5]` 均为 `OK`。首个错误状态实际位于
smoke 的结果解释：`RESULT[6]` 是 `GET_STATUS` 的两个 payload byte，不是 transfer result。
QEMU `usb-kbd` 将 self-powered bit 报为 `0x0001`，原断言错误要求 `0x0000`。

修复只调整 smoke 的期望值为 `0x0001`，并移除了定位期间的临时 driver diagnostics；host
controller、QEMU transport、route 和 guest polling driver 的行为均未被 workaround 改写。
这也使测试明确区分 control result 与 control data，避免未来 QEMU USB device 的有效状态字节被
误报为传输失败。

验证：guest EP0 smoke、host profile qtest `2/2`、host controller `14/14`、control scheduler
`4/4`、channel transport `10/10`、QEMU adapter `13/13`、DM-MC02 USB qtest `9/9` 与完整串行
QEMU smoke `80/80` 均通过。该结果仍不证明 descriptor parsing、endpoint setup、hub、多设备、
NAK retry/timeout、async completion、PID/toggle、DMA FIFO 调度、PHY/VBUS、passthrough 或
DM-MC02 USB Host 支持。

## 2026-08-31 USB configuration descriptor and root-port review

扩展 guest smoke 前，先检查了其连接的第一个 QEMU USB device。旧命令写为 bare
`-device usb-kbd`；QEMU 的 `usb_claim_port()` 会在唯一空 root port 前自动插入 hub。旧 smoke
只断言 device descriptor 的 bLength/type，因此 hub 的 `18/1` 也会通过，后续把 hub 的
25-byte configuration、2-byte interrupt endpoint 和 self-powered status 错当成 keyboard 行为。
根因位于 QEMU composition，而非 H723 host controller、channel transport 或 polling client。

修复将每一处项目调用改为 `usb-kbd,bus=usb-bus.0,port=1`，并将 configuration descriptor
解析保留在独立 freestanding parser 中。真实直接 keyboard 的 values 为 total length 34、
configuration 1、HID interface 0/0、endpoint `0x81`/interrupt/MPS 8。parser 对完整
`wTotalLength` 序列做结构验证，因而上层客体不会因截断或 zero-length descriptor 在扫描时越界。
它不承担 configuration policy 或 endpoint scheduler，避免把 board/client 策略反向写进通用
parser。

直接接入 keyboard 后，host profile qtest 的旧 SOF assertion 以 1 ms 只期待一个 frame，实际
`HPRT0.SPD` 为 High-Speed，故 `HFNUM` 正确增加八次。qtest 现在依据 port speed 选择 125 us
或 1 ms，精确检查一个 frame。这是测试预期错误，不是 controller 计时或性能回退。

验证结果：descriptor smoke、host-controller `14/14`、channel-control `4/4`、QEMU adapter
`13/13`、host qtest `2/2`、DM-MC02 Device-mode USB qtest `9/9`、Cortex-M7 guest smoke、完整
CTest `23/23` 和串行 QEMU smoke `80/80` 全部通过。一次完整 CTest 曾在无关 DMA DBM
reconfigure smoke 中采样到 replacement buffer 的第二个 beat (`NDTR=2`/`CCR1=302`)；该 smoke
连续独立运行 20 次与最终 CTest 均通过。该采样点不是原子 event boundary，后续 DMA 专项切片
应改为 event/IRQ 同步观察，不能把偶发轮询窗口归因到 USB 或用重试隐藏。

残余范围明确：reference profile 仍是单 root-port、同步 EP0 fixture，不是通用 USB host
firmware。尚无 generic endpoint state/scheduling、hub、多设备 enumeration、interrupt/bulk
transfer scheduling、NAK retry/timeout、async completion、PID/toggle、descriptor DMA、PHY/VBUS
或 passthrough；DM-MC02 的 USB1 OTG HS 仍只保持 USB Device mode。

## 2026-08-31 USB endpoint pipe configuration review

descriptor parser 输出的 `bEndpointAddress` 与 `wMaxPacketSize` 不能直接散落到每个 future
controller client，否则 endpoint number/direction、11-bit effective MPS 和 High-Speed
transactions-per-microframe 会被各层重复解释。本轮将这一步限制在 freestanding
`DmUsbHostPipe`：它只接收已解析的 endpoint metadata，不读取 descriptor bytes、MMIO、QEMU state
或 board policy。

检查重点是 raw `wMaxPacketSize` 的位语义。bits 10:0 是 effective MPS，bits 12:11 代表每
microframe transaction count，bits 15:13 保留。pipe 因而保留 ISO/interrupt 的 count，但不因自身
没有 port-speed 输入而伪造 speed-dependent validity；control/bulk 中该 encoding 不是有效配置，
在公共边界拒绝。所有失败在赋值前返回，避免调用者继续使用半更新的 pipe。

隔离 smoke 覆盖 keyboard interrupt pipe、ISO multi-transaction、address/endpoint/MPS 失败和
no-mutation contract；Cortex-M7 smoke 用真实 keyboard descriptor 构造 address 5/endpoint 1/IN/
interrupt/MPS 8 pipe。完整 CTest `24/24`、host qtest `2/2`、host-control `4/4` 与 QEMU smoke
`80/80` 通过。

该对象不是 transmission engine。它不 program H723 host channel、选择 channel、维护 DATA PID、
读取 FIFO、处理 NAK、定义 interrupt period 或提交 QEMU packet。下一层必须先实现一个 pipe 到
single-packet HCCHAR/HCTSIZ/HCFIFO/HCINT boundary；周期调度、重试和异步完成不能在这一对象中
绕过下层直接加入。

## 2026-08-31 H723 single-packet pipe client review

pipe 被接入 H723 client 前，先把 register encoding 保持为无 MMIO 的 pure function。这样
`HCCHAR.DEVADDR/EPTYPE/EPDIR/EPNUM/MPS` 和 `HCTSIZ.PID/PKTCNT/XFERSIZE` 的 bit-level contract
可由宿主 C test 精确覆盖，避免用 guest hang 或 QEMU device result 间接猜测错误来源。encoder 只
允许 DATA0/DATA1、one packet 和 one transaction/microframe；后两项是这一 small slice 的能力
边界，不是把 USB endpoint descriptor 的全部语义静默丢弃。

MMIO transfer 仍只有同步 PIO：OUT 在 channel enable 前写 FIFO，IN 只在 `XFRC` 后按 HCTSIZ
remaining count 读 FIFO；`NAK`、`STALL` 和 `XACTERR` 不被重试或改写为成功。客体 direct test 在
QEMU keyboard configured 后发送 endpoint 1 DATA0 IN。没有输入的正确状态是 `NAK`，该结果证明
endpoint route/configuration 已生效，同时防止测试把 idle condition 当作 zero-length HID report。

验证覆盖 host encoding smoke、Cortex-M7 direct consumer、完整 CTest `25/25`、host qtest `2/2`、
controller `14/14`、host control `4/4`、DM-MC02 USB qtest `9/9` 与 QEMU smoke `80/80`。本轮未
修改 QEMU controller、QEMU transport 或 DM-MC02 board composition，因此没有扩大当前板的
USB Device-mode role。

残余风险是 endpoint state 不存在：调用者暂时必须显式选择 channel 与 PID，无法跨 packet 保存
toggle、halt/reset 或 short-packet policy；也没有 SOF periodic dispatch、NAK retry、multi-packet/
ISO transaction、DMA、async completion、hub/PHY/VBUS/passthrough。下一层需要先从独立、
board-independent endpoint state object 开始，而不是将这些策略写回 pipe 或 reference board。

## 2026-08-31 USB endpoint state review

`DmUsbHostEndpointState` 保持最小：它复制已验证 pipe，只管理 `next_pid` 和 halted bit。成功
Bulk/Interrupt completion 才翻转 DATA0/DATA1；NAK/error 不变，STALL 进入 halt，clear-halt/reset
回 DATA0。short success 同样翻转，但是否结束请求仍不由该对象决定。ISO packet 没有 DATA toggle，
也不因 accepted completion 改变状态。这将 USB protocol state 和 board/controller 的 MMIO、timer
或 scheduler policy 分开。

H723 adapter 不复制 PIO path，只按 `prepare -> existing pipe transfer -> complete` 串联。它将 halted
state 映射为 STALL，对 client 的 NAK/STALL/XACTERR 分别回写 generic completion。direct guest
在 configured QEMU keyboard 上观测到 endpoint 1 NAK 后仍为 DATA0；这证明 idle backpressure
没有被错误视为 endpoint progress。

验证：endpoint-state unit、H723 pipe encoder、Cortex-M7 guest、完整 CTest `26/26`、host qtest
`2/2`、controller `14/14`、host control `4/4`、DM-MC02 USB qtest `9/9`、QEMU smoke `80/80`。
未修改 QEMU controller/adapter 或 DM-MC02 board。残余风险是无 channel allocation、periodic SOF
eligibility、retry budget、multi-packet/short-request policy、ISO multi-transaction、DMA、async
completion 和 hub/PHY/VBUS；这些必须继续由独立 reusable scheduling boundary 逐层接入。

## 2026-08-31 USB periodic endpoint schedule review

本轮没有直接把 keyboard polling 塞入 H723 的 SOF callback。周期值既依赖 descriptor 的
`bInterval`，也依赖连接速度；若将它写入 board wrapper，未来其它 controller 或 board profile
会重复解释 High-Speed microframe 和 Full/Low-Speed frame 规则。新增的 freestanding
`DmUsbHostPeriodicSchedule` 只持有 copied pipe、speed、interval 与 next-slot virtual timestamp，
将这个契约留在可复用 driver 层。

关键时间语义是：origin 本身可 poll，`eligible()` 不改变状态，真正提交一次 poll 后才
`advance()`；若 virtual time 已跳过多个时隙，next slot 直接计算为当前 timestamp 之后的第一个
时隙。这样上层 timer 无需循环补发过期 poll，避免调度积压转化为启动或实时性能问题。该对象不
把 NAK 看作完成，也不拥有 PID、channel 或 retry；这些仍是后续层的职责。

smoke 覆盖 HS `125 us * 2^(bInterval-1)`、FS/LS frame intervals、FS ISO one-frame 限制、
speed-specific multi-transaction rejection，以及 early/late timestamp 行为；定向相邻 CTest
`5/5` 通过。尚未在 H723 SOF 直接 consumer 上验证，因此不能宣称当前 guest 会按 endpoint
period 自动 polling；下一切片必须只接这个边界并验证单 channel，不得改动 DM-MC02 的 USB
Device-mode mapping。

## 2026-08-31 USB periodic poller and H723 adapter review

复核确认，`DmStm32H7OtgHost` 的 channel register 不包含 descriptor `bInterval`。把 periodic
schedule 直接放进 controller 会迫使芯片层解析 driver metadata，破坏可复用边界。因此本轮在
controller 之上增加 controller-neutral `DmUsbHostPeriodicPoller`，其 callback 只收到已验证 pipe、
current PID 和 packet bytes；H723 adapter 仅将其映射到现有 PIO client。没有修改 controller 的
SOF loop、QEMU wrapper 或 DM-MC02 Device-mode mapping。

poller 的关键顺序已经由 isolated smoke 固定：not-due 不触碰 submitter；一次 NAK 仍是一次真实
poll，因而推进到下一个 slot 但保持 DATA0；accepted 用已有 endpoint state 切换 PID；STALL 后不
再提交。late timestamp 只会调用一次 submitter，再由 schedule 跳到未来 slot，避免因仿真时间
跳跃出现无界补发。

直接 Cortex-M7/QEMU test 使用真实 `usb-kbd` descriptor 的 interrupt-IN pipe，首次 periodic PIO
poll 返回 NAK，第二次在同一 timestamp 为 `NOT_DUE`。发现的首错是 freestanding link 未包含
64-bit modulo 所需的 `__aeabi_uldivmod`；这不是模型或 controller 错误，已由 smoke 的显式
`-lgcc` 修复。此模块的正常运行时路径没有新增动态分配、锁或 wall-clock sleep。

残余限制仍很明确：时间戳当前由 caller 提供，尚未从 H723 `HFNUM`/SOF 导出，因而这不是自动
periodic polling，也没有 retry/timeout/channel allocation、多包/ISO multi-transaction、DMA/FIFO
arbitration、async completion 或物理层/拓扑支持。下一切片应只建立 driver-owned SOF timestamp
adapter，再验证 virtual SOF 触发的 poll；不能通过向 controller 添加 descriptor policy 绕过该层。

## 2026-08-31 USB SOF timestamp source review

`HFNUM.FRNUM` 是 H723 controller 暴露给 driver 的 frame/microframe counter，不能让每个 endpoint
重新自行计算速度或处理 wrap。本轮将 conversion 放进 protocol-neutral `DmUsbHostSofClock`：High-
Speed 使用 125 us，Full/Low-Speed 使用 1 ms，unsigned 16-bit delta 覆盖正常计数器回绕。port reset
不是 counter value 的可辨识事件，要求 caller reinitialize 也避免了在时钟 helper 中猜测板级 lifecycle。

H723 adapter 只在 init 时读取 `HPRT0.SPD` 和 `HFNUM`，后续 `timestamp()` 仅读取 `HFNUM`；没有
SOF IRQ、QEMU timer、device descriptor、channel allocation 或 board profile 依赖。Cortex-M7/QEMU
keyboard smoke 的实际链路是 `HFNUM -> generic clock -> periodic schedule -> generic poller -> H723
PIO`：首次 NAK、同 timestamp defer、到下一个 `next_slot_ns` 后第二次 NAK 均通过。这是 driver
消费 virtual SOF 的证据，但不等同于 event-driven 自动 host scheduler。

clock smoke 覆盖 exact HS/FS/LS period、wrap 与 invalid speed，normal runtime path 为常数次整数
运算和一次 volatile `HFNUM` read，不引入分配、锁或 wall-clock sleep。仍缺一个以 SOF IRQ/event
唤醒、遍历已注册 periodic endpoint 的单-channel scheduler；下一切片应先定义这个上层 event
adapter，不能改变 controller 的 descriptor-free register contract。

## 2026-08-31 H723 periodic SOF-event adapter review

此前已能从 `HFNUM` 重建 timestamp，但 caller 仍需自行在每个 SOF 执行 poller。新增的
`DmStm32H7UsbHostPeriodicSof` 将这两个操作收敛为一个 H723 driver adapter：它只借用 SOF
source 和 generic poller，在每个观测到的 SOF 读取 timestamp 并最多提交一个 due packet。endpoint
state、PID、`bInterval` 和 PIO 仍分别属于下层通用对象或现有 H723 PIO adapter，未泄漏到
controller/board。

adapter init 拒绝 source/poller speed 不一致，避免 125-us High-Speed tick 在 Full/Low-Speed
schedule 上静默运行。isolated smoke 使用伪 `HPRT0/HFNUM` 和 fake submit，固定 first due submit、
middle SOF no-submit、later due submit 与 mismatch 行为。实际 Cortex-M7/QEMU smoke 轮询并 W1C
`GINTSTS.SOF`，确认 keyboard endpoint 的第二次 NAK 只在到期 SOF 后出现。这验证了 event-loop
组合，不是 QEMU controller 自动调度的声明。

运行路径每个 SOF 是一次 `HFNUM` MMIO read 和一次 due comparison，未到期不进入 PIO/transport；
不增加分配、锁、wall-clock sleep 或 controller-side descriptor state。残余限制是单 endpoint/
polling loop，没有 IRQ wakeup、registry、channel ownership、NAK retry/timeout、multi-packet/ISO、
DMA/FIFO arbitration、async completion 或总线物理/拓扑支持。下一切片应先建立独立 registry 的
数据契约，再讨论将 SOF 通过 IRQ 交给多个 endpoint。

## 2026-08-31 Periodic endpoint registry review

单 endpoint SOF adapter 已验证事件时序，但不能让多个 endpoint 分别扫描同一个 `HFNUM` 或在
board/controller 保存 `bInterval`。本轮将 collection 放在 controller-independent
`DmUsbHostPeriodicRegistry`：entry 只保存通用 poller、caller-owned packet buffer 和 optional callback；
H723 adapter 只从既有 source 取得一个 shared timestamp 后调用 dispatch。不同 controller 或 board
profile 可复用 registry，且不会依赖 H723 register layout。

每 SOF 的 work 是对 active compact entries 的一次线性、无分配扫描。not-due 不触发 PIO；STALL、
halt 和 invalid submit 都从 active set 移除，避免终态 endpoint 继续消耗 SOF 热路径。registry init
原先的 aggregate assignment 会在 `-nostdlib` ARM build 产生 `memset` unresolved symbol；确认 unused
slot 从不在 `count` 外访问后改为只初始化两个活跃字段，保留正确性且去掉 libc 依赖。

generic smoke 覆盖多 endpoint cadence、duplicate/speed rejection、STALL auto-remove 和 explicit
remove；H723 fake-MMIO smoke 覆盖 source-to-registry SOF composition。Cortex-M7/QEMU keyboard smoke
把实际 endpoint 注册到 registry，并从 W1C `GINTSTS.SOF` 事件循环取得下一到期 NAK。它证明的是
driver-owned registry event loop，不意味着 QEMU controller 已拥有多 endpoint descriptor schedule。

剩余边界保持明确：当前没有 NVIC SOF IRQ、channel allocator、retry/timeout、multi-packet/ISO、DMA/
FIFO arbitration、async completion 或 USB topology/physical model。下一层应只定义 H723 IRQ handler
边界并让它调用既有 registry，不能将 polling loop 的策略反向写入 controller 或 board。

## 2026-08-31 H723 SOF IRQ boundary review

polling `GINTSTS.SOF` 已能证明 registry cadence，但真实 firmware 需要从 NVIC 唤醒。新增的
`DmStm32H7UsbHostSofIrq` 只保留必要 H723 register behavior：无 SOF 状态立即返回；有 SOF 时
W1C 后调用现有 registry SOF adapter。它不吸收 `GINTMSK/GAHBCFG/NVIC` 初始化，因为这些是
firmware/board 的 IRQ policy，不能混入可复用 driver 和 registry。

isolated fake-MMIO test 覆盖 status gate 与 due/no-due/due schedule composition。Cortex-M7 reference
guest 将 IRQ 77 vector 指向 adapter caller，清 pending、打开 controller global/SOF mask 与
`NVIC_ISER2` bit 13 后在 `wfi` 等待。真实 QEMU IRQ 唤醒 handler，并且只有到期 SOF 发起第二次
keyboard NAK。这既复核了 existing board profile 的 IRQ 77 route，也没有改动 DM-MC02 USB Device
mapping。

热路径是一个 status read、一次 W1C、一次 shared timestamp 和 active registry scan；未到期 endpoint
不进入 PIO。没有增加 timer thread、dynamic allocation、lock 或 wall-clock work。边界仍是单
controller/registry consumer；channel ownership、多 endpoint actual PIO channel allocation、retry/
timeout、multi-packet/ISO、DMA/FIFO arbitration、async completion 与 USB topology/physical layer
仍需先分别建模，不能借由 IRQ handler 直接堆叠。

## 2026-08-31 USB host channel lease allocator review

在让 H723 PIO client 消费多个 channel 之前，先将有限 channel 的所有权从 register adapter 中
分离。`DmUsbHostChannelAllocator` 只接收 caller-provided ID set 和 owner identity；它不假定 0..11
或任何 DWC2 register，因此未来不同 controller/profile 可以重用，而 H723 range validation 保持在
H723 client 层。固定 16-entry array、初始化顺序的线性分配和无分配/无锁实现使热路径有明确上界。

首次定向构建暴露的首错是 C source 使用 `NULL` 却未包含 `<stddef.h>`，而非 allocator state
逻辑。补齐标准头后，isolated CTest 通过。smoke 锁定 init failure 不改变 active state、full 不改
输出 lease、owner mismatch 不释放，以及 release/reacquire 后 generation 使 copied stale lease 失效。

仍未将 lease 接到 H723 client，因而本轮不能宣称实际 `HCCHAR` channel 已由 allocator 驱动；也不
提供 ISR 并发保护、channel cancel/recovery、retry、DMA/FIFO、multi-packet 或 host board wiring。
下一切片必须保持 allocator 通用，将 H723 raw-channel field 改为只消费 active lease，并先验证精确
MMIO word 后再运行 Cortex-M7/QEMU integration smoke。

## 2026-08-31 H723 PIO channel-lease consumer review

H723 PIO client 原先存储裸 channel number，虽然上层已经有通用 allocator，但缺少两层之间的实际
ownership contract。client 现借用 allocator 与 mutable lease 的存储；init 验证 lease active 且只在
H723 层接受 `0..11`，transfer 再次验证后才生成 MMIO offset。这样通用 allocator 仍不需要知道
DWC2 channel count，H723 adapter 也不再复制 allocation policy。

isolated smoke 先绑定 range-invalid 的通用 channel 12 并确认 H723 client 拒绝，再以 channel 2
建立 client、release lease、在 base 0 下调用 transfer。它返回 `INVALID` 并保持 caller length，证明
lease guard 在任何 volatile PIO access 之前执行。Cortex-M7 reference guest 则分配 channel 1 给
keyboard endpoint，结果断言记录的 channel-1 `HCCHAR` address/type/direction/endpoint/MPS fields，
同时保持 EP0 discovery、NAK cadence 与 SOF IRQ registry 路径。

本轮仍不是 concurrent host scheduler：EP0 control fixture 继续用 raw channel 0，且其 transfer 已在
periodic channel 1 被分配前结束。没有完成回调自动 release、cancel、locking、retry/timeout、多包、
DMA/FIFO 或任何 DM-MC02 Host board wiring。allocator reinit 要求 caller 丢弃旧 lease 并重建 client；
它不是可在 active client 上透明替换 resource set 的 API。

## 2026-08-31 H723 periodic dynamic lease/release review

现有 generic poller 的 submit 是同步 callback，因此让 poller 或 registry 直接管理 channel allocator
会破坏层次：它们并不知道 controller resource，也会把暂时没有 channel 错误地变成 endpoint 终态。
本轮增加 `DEFERRED` 结果；它不提交 packet、不改变 PID/时隙，registry 保留 entry。H723 periodic
adapter 在 due callback 内 acquire lease、调用既有 PIO client、映射 completion 后立即 release，资源
生命周期与真实同步 transfer 对齐。

测试首先因新增枚举值造成旧 guest oracle 把 `POLL_SUBMITTED=2` 误判为失败；修正 oracle 后 guest
通过，并新增结果区观测 `HCCHAR(channel=1)` 与 allocator owner 已清零。所有 CTest `35/35` 通过。
仍需明确保留：异步 channel 正在使用时不能复用这套同步 release 语义；cancel、IRQ locking、DMA/
FIFO、multi-packet 和多端点公平性应在相应层单独定义。

## 2026-09-01 H723 periodic shared allocator multi-adapter review

同步 PIO transfer 返回时已经拥有完整 completion，因此本轮把 lease 生命周期留在 H723 periodic
adapter，并通过显式 transfer callback 建立可测试的替换边界。generic poller 只处理
`DEFERRED`/submitted 两类调度结果；它不知道 channel 数量和 allocator。嵌套 fixture 证明外层 adapter
持有 channel 2 时，另一个独立 adapter 可获得 channel 3，两个 callback 返回后资源均为空。

首错是新增 `DEFERRED` enum 改变了旧 guest smoke 中 `POLL_SUBMITTED` 的数值，导致脚本误报；更新
oracle 后，poller、registry、Cortex-M7/QEMU 和全量 CTest `36/36` 均通过。资源耗尽保持 due slot
等待，而不是终止 endpoint。未实现的异步 completion/cancel、并发锁、DMA/FIFO、multi-packet、
公平调度与真实多设备 topology 仍需后续独立边界。

## 2026-09-01 USB bulk composition review

本轮先在 H723/QEMU 之外定义 bulk 多包组合边界，避免把 controller 的 packet 生命周期和
channel ownership 偷塞进通用对象。新增的 `DmUsbHostBulkTransfer` 只持有一个已经配置的
`DmUsbHostEndpointState` 和 single-packet submit callback；它可以被 H723 PIO、其他 host
controller 或纯测试 fixture 消费，板卡和 QEMU 状态不会向下泄漏。

OUT 按 endpoint MPS 生成连续 packet，IN 每次最多请求一个 MPS，并在 accepted short packet
或调用者 buffer 耗尽时返回。精确 MPS 的 OUT 可由显式 `append_zero_packet` 追加 ZLP。accepted
packet 才交给 endpoint state 翻转 DATA PID；NAK 返回本次已经成功的累计字节并保持待重试 PID，
DEFERRED 在提交前返回且不改变 PID 或进度，STALL 锁定 endpoint。该函数是同步组合调用：若中途
遇到 NAK/DEFERRED，caller 必须依据 `actual_length` 传入剩余 span 重试，函数不会保留隐藏的
异步事务，也不会重复提交已经成功的 packet。

隔离 smoke 覆盖 `64/64/2` OUT、IN short、精确 MPS 的 ZLP、NAK 累计长度、DEFERRED 状态不变、
STALL halt 和非 bulk reject。定向目标已构建并通过。当前没有把它误报为 H723 bulk 支持：仍缺
逐 packet H723 PIO 接入和 channel lease 生命周期、retry budget/timeout、async completion/cancel、
DMA/FIFO arbitration、ISO multi-transaction、hub/拓扑、PHY/VBUS、宿主机 passthrough 和
DM-MC02 Host board mapping。下一道门是以既有 single-packet PIO callback 接入一个 bulk packet，
再用直接 guest/transport 测试锁定真实寄存器、剩余长度和资源释放。

## 2026-09-01 STM32H723 bulk packet adapter review

本轮将 generic bulk composer 接到 H723 client，但保持接入范围为“一个同步 packet 的
controller adapter”。新增 `DmStm32H7UsbHostBulk` 不复制 PIO register 编码：它为每个 packet
向已有 allocator 申请临时 lease，复用 `DmStm32H7UsbHostPipeClient`，调用真实或注入的
`DmStm32H7UsbHostPipeTransfer`，再在 completion 返回后释放 lease。这样 channel ownership
属于 H723 adapter，bulk packetization、PID 和 short/NAK policy 仍属于下层通用对象。

隔离的 H723 smoke 证明每个 packet 观察到 active lease，`64/64/2` OUT 和 IN packet 均按预期
映射，部分 NAK 返回累计 64 字节且 caller 传入剩余 span 后从保留的 DATA1 继续，资源耗尽返回
DEFERRED 且 endpoint 不变，STALL 后停止后续提交并释放资源。第一次窄测试运行前发现构建目录
没有重新生成新 target；重新执行 `cmake -S . -B build/cmake` 后编译无 warning 并通过。这是
构建入口问题，不是 USB 状态错误。

验证结果：`dm_stm32h7_usb_host_bulk_smoke` 通过，完整 host CTest `37/37` 通过，QEMU smoke
suite 通过。当前仍没有 reference guest 的真实 bulk endpoint configuration/packet path，也
没有 async completion/cancel、IRQ 并发保护、retry/timeout、DMA/FIFO arbitration、ISO multi-
transaction、hub/拓扑、PHY/VBUS、passthrough 或 DM-MC02 Host board wiring。下一道门必须在
host profile 中显式选定一个 bulk device/endpoint，先用寄存器和 QEMU packet 长度断言锁定单包
路径，再扩大到 guest 多包测试；不能用 callback fixture 替代该证据。

## 2026-09-01 STM32H723/QEMU bulk direct consumer review

本轮把 bulk adapter 接到独立 reference host profile 的真实 QEMU USB bus。guest 使用
`usb-serial` 的 configuration descriptor，选择 interface 0 / endpoint 2 的 64-byte bulk OUT，
再通过默认 H723 PIO adapter 发送 130 bytes。QEMU file chardev 收到精确的 `0..129`，guest
同时检查最终 channel-1 `HCCHAR/HCTSIZ`、三包后的 DATA1 状态和 allocator lease 已释放。

验证过程中先发现两个测试边界问题：guest 使用了 descriptor API 实际不存在的
`interface_number` 字段，随后 oracle 又把最后一个 bulk DATA0 PID 错写成 control SETUP PID；
分别依据公共结构定义和真实 DATA0/DATA1/DATA0 序列修正后，独立脚本通过，自动 QEMU smoke
suite 为 `81/81`，host CTest 保持 `37/37`。

这条路径证明的是同步 `descriptor -> H723 PIO -> QEMU USB device -> chardev`，不证明每个
packet 的独立 trace、bulk IN 注入、NAK retry budget/timeout、async completion/cancel、IRQ
并发保护、DMA/FIFO arbitration、hub/拓扑、PHY/VBUS、passthrough 或 DM-MC02 Host board
mapping。下一道门应在 H723 channel completion/NAK retry 边界建立测试，再扩展真实 IN/OUT 行为；
不能用当前 chardev fixture 代替完整 USB Host 实现。

## 2026-09-01 STM32H723/QEMU bulk direct consumer 修正 review

本轮复核发现的首个功能错误在通用层，而不是 QEMU 设备：`dm_usb_host_bulk_transfer_run()` 为
IN packet 调用 `DmUsbHostBulkSubmit` 时把 packet 长度放进了 `out_length`，因此真实 H723
pipe 依据方向互斥契约返回 `INVALID`。修复为 IN 只传 `in_capacity`、OUT 只传 `out_length`，并在
隔离 smoke 中分别断言两个参数集合。

随后发现 guest smoke 的静态 channel allocator 没有可写存储：链接脚本缺少 `.bss` 输出段，GNU
ld 将它放到 Flash orphan section。该问题会让 descriptor 阶段看似成功、bulk 阶段无法获得
channel；链接脚本现将结果区与 DTCM runtime 区分开，allocator 位于可写 DTCM。

真实 QEMU 双向 smoke 还暴露了两个测试 oracle/时序问题：IN endpoint descriptor 地址必须包含
方向位 `0x81`，FTDI `usb-serial` IN 包包含默认 modem 状态头 `b1 00`；此外，guest 在连续同步
执行时会先于 QEMU chardev 主循环消费 socket 输入。guest 以 SysTick/WFI 发布 READY，host 在
该边界注入 10 bytes，随后真实 USB IN 返回 12 bytes。上述处理均保留了设备、QEMU transport、
H723 PIO 和 endpoint state 的实际路径，没有用固定返回值绕过错误。

验证：独立双向 guest smoke 通过，`dm_usb_host_bulk_smoke`、`dm_stm32h7_usb_host_bulk_smoke`
通过，完整 host CTest `37/37` 通过。残余风险仍为同步单 root-port fixture；NAK 重试预算、异步
completion/cancel、DMA/FIFO、hub/topology、PHY/VBUS、passthrough 和 DM-MC02 Host board
mapping 未实现。

## 2026-09-01 H723 bulk NAK completion/channel lease review

本轮继续沿 `QEMU usb-serial producer -> H723 HCINT/HCCHAR boundary -> synchronous
PIO consumer` 定位。首个运行时缺陷是 bulk NAK 只设置 `HCINT.NAK` 而保持 `HCCHAR.CHENA`；
同步 PIO consumer 原先只等待 `CHHLTD`，并在返回时释放 lease，造成 guest 无限等待，且下次
复用同一 channel 时没有新的 `CHENA` 上升沿。修复在 H723 PIO 层完成：把 NAK 纳入 completion
条件，并在释放 lease 前写 `HCCHAR.CHDIS`；NAK 结果仍保持 endpoint PID、剩余长度和实际字节
数不变。

验证过程中首个测试夹具错误是 `in_state` 未初始化就重新绑定给 IN bulk adapter，导致执行在
最终 idle NAK 前退出。修正后又定位到 host oracle 没有观察到 READY：guest 的 1 ms SysTick
窗口在 host 首次查询前已被覆盖，Python 实际没有执行输入注入。READY 改为有限 100 ms 虚拟
窗口后，chardev 输入经真实 QEMU callback 入队，IN 返回 FTDI 状态头加 10 bytes，随后空 IN
稳定返回 NAK 且 `CHENA=0`。这两个修复均属于测试边界/夹具，不是用上层 fallback 掩盖模型缺陷。

验证结果：独立 H723/QEMU bulk smoke 通过；host build 通过；CTest `38/38` 通过。当前仍不
提供 retry budget/timeout、SOF retry scheduler、async completion/cancel、IRQ locking、
DMA/FIFO arbitration、hub/topology、PHY/VBUS、passthrough 或 DM-MC02 Host board wiring；
同步 PIO adapter 的 stop-before-release 规则不能被异步 adapter 直接复用。

## 2026-09-01 USB host retry boundary review

本轮把 retry 放在板卡无关的单包/组合层，避免让 H723 PIO 通过虚拟时间循环掩盖
controller completion。`DmUsbHostRetryPolicy` 的 producer 是单包 completion，boundary
是显式的 `begin/check/on_nak/complete/finish/reset` 生命周期，consumer 是 bulk/periodic
caller。初始 packet 不消耗 retry budget；只有实际返回 NAK 时才累计 retry，`DEFERRED`
不消耗预算。timeout 使用虚拟时间并检查 modulo `uint64_t` 的单调差值，模糊的大回退直接
返回 `INVALID`。

bulk retry 保持无状态重放边界：函数返回中断请求的 `actual_length`，caller 必须传入剩余
buffer span，才能保证已经 accepted 的 packet 不会被重放。periodic retry 则显式保存 pending
packet 的 buffer 指针；调用者在 pending 期间承担 buffer 生命周期。NAK 不推进 endpoint
DATA PID 和 periodic schedule，预算耗尽/超时才结束 due slot；H723 adapter 只负责真实
PIO completion 和临时 channel lease。

验证过程中修复了一个 freestanding guest 链接问题：retry 初始化最初使用整体结构赋值，
在 `-nostdlib` smoke 中引入了隐式 `memset` 依赖；已改为显式字段初始化，并将 retry 源文件
加入 guest 链接清单。之后 retry、endpoint state、bulk、periodic、SOF、H723 adapter 隔离
测试，两个 H723/QEMU guest smoke，host CTest `38/38` 和 QEMU suite `81/81` 均通过；
Python `255 passed`，shell/compileall/`uv lock --check` 通过。

残余风险是明确的：当前接口仍是 caller-driven、同步 PIO、单 root-port 组合；没有
completion-driven channel ownership、异步 cancel、IRQ locking、DMA/FIFO arbitration、
SOF retry scheduler、ISO multi-transaction、hub/topology、PHY/VBUS、passthrough 或
DM-MC02 Host board wiring。通过这些 smoke 只能证明本轮状态和边界契约，不能宣称完整 USB
Host 支持。`trobot/` 未修改。

## 2026-09-01 Completion-driven channel operation review

本轮在已有 generation-protected channel allocator 之上增加一个很小的公共生命周期层，
没有把异步语义塞入 allocator，也没有让 H723 adapter 自己复制 pending/complete/cancel
状态。`DmUsbHostChannelOperation` 以自身地址作为 owner，`begin()` 获取并保持 lease，
controller 完成时调用 `complete()`，取消时调用 `cancel()`；两者都先释放 allocator entry
再进入终态。operation 只处理 ownership，不解释 USB completion code，不创建线程、锁、等待或
callback dispatch。

隔离测试覆盖双 operation 并行占用、资源耗尽不改变 idle operation、callback 期间 lease active、
完成/取消后的 lease 清理、重复终结拒绝和终态复用。H723 bulk/periodic adapter 已改用该公共
生命周期，现有 synchronous PIO 行为不变；两条 freestanding H723/QEMU guest smoke 用新增源
文件重新链接并通过。

首个失败是 operation 源文件使用 `NULL` 却未包含 `<stddef.h>`，编译器在 `-Werror` 下直接停止。
按 producer（operation 实现）边界修复 include 后，operation、adapter 和全套回归均通过。验证：
host CTest `39/39`、QEMU smoke suite `81/81`、Python `255 passed`、shell/compileall/`uv lock
--check` 通过。剩余边界仍是异步 controller 实现、completion/cancel event dispatch、IRQ
并发保护、DMA/FIFO、SOF scheduler、hub/topology、PHY/VBUS 和 DM-MC02 Host wiring。

## 2026-09-01 Controller-independent async completion/cancel record review

本轮在既有 `DmUsbHostChannelOperation` ownership 边界之上增加
`DmUsbHostChannelCompletion`。producer 是任意 host controller 的 packet start，boundary
是固定容量的 pending/complete/cancel record，consumer 是未来的 IRQ 或 event-loop adapter。
它复用 generation-protected lease，不读取 H723/QEMU 寄存器，不创建线程、锁、等待或回调
分发；packet buffer 仍由调用者持有，记录对象在 terminal event 前必须保持稳定地址。

`begin()` 同时生成一个可复制的 generation token。`complete()`/`cancel()` 必须带回匹配的
token，旧 operation 的迟到事件不能完成被复用的 record；随后 `complete()` 再按公共 endpoint
completion contract 校验 actual length 和非 accepted 的零长度要求，成功后才释放 lease 并保存
completion、actual length 与调用者提供的 virtual timestamp。非法 completion 保持 pending，
`cancel()` 只记录取消时间并释放 lease，不伪造 USB completion code。lease 访问为只读，终态
对象可以重新 `begin()`。

隔离测试 `dm_usb_host_channel_completion_smoke` 覆盖双 channel 占用、资源耗尽、非法长度保留
pending、accepted/NAK、cancel、时间戳、重复终结、旧 token 拒绝和复用；窄编译与 CTest 定向
回归通过。Luna 的只读审查发现旧 token 风险和可写 lease 风险，均在继续集成前修复并关闭其
会话。时间戳在这一层明确为调用者提供的 virtual event timestamp，不在此层强制全局单调性。

该切片仍不声称异步 H723 register adapter、IRQ locking、DMA/FIFO、SOF、QEMU topology 或
DM-MC02 USB Host 支持；下一步应在 H723 层实现 start/IRQ completion/cancel 消费，并补直接
guest 边界测试。

## 2026-09-01 H723 async PIO completion/cancel adapter review

本轮完成了上一道 completion record 与 H723 单包 PIO 之间的最小消费边界。producer 是
`dm_stm32h7_usb_host_pipe_async_start()` 对 `HCCHAR/HCTSIZ/HCFIFO` 的 packet programming，
boundary 是固定容量 completion record 和 copy-only channel lease，consumer 是 caller 驱动的
`poll()`/`cancel()` event boundary。adapter 不把 operation 私有字段暴露给消费者，也不把
DM-MC02 board wiring 或 QEMU event loop 引入通用 H723 层。

复核时修复了两个实际缺陷。第一，completion token 最初复用了 allocator 的 per-channel
generation；当旧 operation 在 channel 2 结束、channel 2 被其它 operation 占用、新 operation
迁移到 channel 5 时，旧 token 可能与新 lease generation 重合。现在 token 使用 completion
对象自己的非零 generation，并增加跨 channel migration 回归。第二，async adapter 接入后两个
freestanding guest smoke 漏链接 completion implementation；补齐依赖后又发现大结构体清零在
`-nostdlib` 下隐式调用 `memset`，已改为字段级初始化。完整 host 重编译又发现
pipe、bulk、periodic 三个 CMake smoke target 漏列 async completion 依赖，已补齐后从零链接验证。

验证：completion 与 H723 async smoke 均通过；host CTest `41/41`；两个 ARM/QEMU guest
USB host smoke 通过；整机 QEMU smoke suite `81/81`。文档同步了 opaque storage、token
active、copy-only lease、result/timestamp 读取和 pending reinit 契约；未修改 `trobot/`。

当前仍不能宣称完整异步 USB Host：`poll()` 是 caller-driven 的寄存器观察，不是带并发保护的
IRQ handler；没有 IRQ locking/callback dispatch、DMA/FIFO arbitration、SOF retry scheduler、
ISO、多设备 topology、PHY/VBUS、passthrough 或 DM-MC02 Host board wiring。下一道门应先定义
H723 IRQ 事件分发与锁边界，再扩展 DMA/FIFO 或 host-role board 组合。

## 2026-09-01 H723 async channel IRQ dispatcher review

本轮实现了 H723 层的最小 channel IRQ 消费边界。producer 是 H723 global/channel interrupt
summary，boundary 是 `DmStm32H7UsbHostPipeAsyncDispatch` 的固定 slot 表，consumer 是已有
async PIO completion record。dispatcher 在可选的外部锁内读取
`GAHBCFG -> GINTSTS/GINTMSK -> HAINT/HAINTMSK`，按 channel 调用既有 `poll()`，不复制
completion、HCINT 清除或 allocator release 逻辑。

设计上 `dispatch_start()` 将 PIO start 与 owner registration 放在同一锁范围内，避免普通
任务上下文在 start 返回到建立映射之间被 IRQ 插入。没有提供锁只适用于已串行化的 fixture；
真实 NVIC handler 必须使用同一 `enter/leave` 实现。`dispatch_handle()` 对外部完成/取消的
record 只清理 stale slot，不会使用失效 token 操作新的 generation。

验证期间的首个失败是 build-tree 路径选择错误，随后新 CMake target 首次链接又缺少公共
`dm_usb_host_endpoint_state.c`；分别确认多个并行 build tree 后修正 target 直接依赖。另一个
测试失败来自 allocator 按初始化顺序复用最先空闲 channel，测试原先错误地假设第三个 operation
必然得到 channel 7；改为先占用 channel 1/3，再让第三个 operation 确实落到 channel 7，随后
在同一 dispatch 中验证两个 accepted 与一个 NAK。

进一步复核发现 `dispatch_cancel()` 原先在进入锁前读取 lease，存在与 IRQ 完成竞争的窗口；现
已将 lease lookup 移入锁内。`dispatch_init()` 现在拒绝仍有注册 slot 的重初始化，防止无声丢失
异步 owner 映射；首次初始化要求调用方先清零对象。

`dm_stm32h7_usb_host_pipe_async_dispatch_smoke` 当前通过。它证明 H723 reusable adapter 的
寄存器门控、路由和生命周期边界，不证明真实 NVIC/QEMU deferred completion、W1C IRQ line
行为、DMA/FIFO、SOF retry、ISO、hub/topology、PHY/VBUS、passthrough 或 DM-MC02 Host
board wiring。Terra 的只读架构复核已完成并关闭；其建议的通用 visitor 可作为后续抽象，
本轮保留直接 async/token dispatcher 以避免跨层提前扩张。

## 2026-09-01 QEMU host-channel deferred completion boundary review

本轮在已验证的 `DmUsbHostChannelTransport` 同步桥上增加可选的
`DmUsbHostChannelTransportScheduleCompletion`。首个完成状态仍由 transaction consumer
同步计算；transport 在 `write_in` 成功后只把稳定的 host/channel/token/completion/actual length
值交给 scheduler。这样延迟路径不会引用 `dm_stm32h7_otg_host_service_channel()` 的栈 request、
transport 的单个 packet buffer 或 transaction 内部指针。未安装 scheduler 时继续直接调用
`dm_stm32h7_otg_host_complete_channel()`，保持现有 QEMU/native 和 fixture 行为；延迟路径必须
使用 token-checked `dm_stm32h7_otg_host_complete_channel_with_token()`。

新边界测试先验证 accepted IN 数据已经到达 host 侧、channel 仍为
`waiting_completion`，且 `HCINT`、`HAINT`、`GINTSTS.HCINT` 和 IRQ line 都未置位；随后通过
公共 token-checked `complete_channel()` 投递捕获的终态，先验证旧 token 在 channel reuse 后被
拒绝，再验证真实的 `XFRC|CHHLTD`、channel summary、global summary 和 IRQ line。测试为
`test-dm-usb-host-channel-transport` `12/12`，窄目标无编译警告。

该 hook 只是完成调度契约，不提供 timer、queue、locking、cancel 或 timestamp progression，
因此当前仍不能宣称 QEMU asynchronous host support。下一步应在 transport composition 层用
QEMU virtual timer 做一个可取消的多 channel fixture，并先锁定 reset/teardown 语义；不要把它
接入 DM-MC02 当前 USB Device profile 或借此绕过 H723 IRQ/ownership 边界。

## 2026-09-01 QEMU virtual-clock completion scheduler fixture review

本轮把上一道 transport completion hook 接到一个独立的 QEMU virtual-clock fixture。producer
仍是 transport 已经完成的 transaction 结果，boundary 是固定 12-entry timer 表，consumer
是 token-checked 的 H723 channel completion。scheduler 只保存稳定的 host/channel/token 和
completion 值，不保存栈 request、transaction 或 packet buffer，也不引入线程、锁或真实睡眠。

首个失败发生在测试 target 链接阶段：fixture 误调用 `cpu_timers_init()` 和
`cpu_enable_ticks()`，但该 unit target 没有 CPU timer 实现。移除整机初始化、在测试中提供受控
`cpu_get_clock()` stub 后，第二个失败暴露出 `qemu_clock_deadline_ns_all()` 返回的是相对当前
时间的 delta，测试错误地把它当成绝对 deadline。改为按 delta 推进受控 virtual clock 后，
零延迟、正延迟 reset、多个 channel 顺序、同 channel deadline 替换和 destroy teardown
均通过，窄测试为 `5/5`。

该 fixture 的容量和一 host/channel 一个 pending entry 是明确的 test-only 契约；它不代表
通用多设备 topology，也不负责 cancellation notification、IRQ locking、SOF retry、DMA/FIFO、
PHY/VBUS、passthrough 或 DM-MC02 USB Host wiring。下一道门仍应是独立 reference host
composition 的 deferred guest 边界，且必须先验证 reset/teardown，再向上层集成。

全量 QEMU Meson 回归实际结果为 `332` 通过、`9` 跳过、`1` 失败；失败是已有的
`qtest-arm/test-hmp`，在 `stm32h723-usb-host` 的通用 HMP 序列执行
`mouse_button 0` 时 QEMU 子进程 SIGSEGV，独立复现同样稳定。scheduler 相关 unit、USB host
transport 回归和 `stm32h723-usb-host-test` 均通过；该 HMP 缺陷未在本轮修复，不能将全量
回归宣称为全通过，后续应在 HMP/host-profile 边界单独定位其输入设备生命周期问题。

## 2026-09-01 QEMU reference host deferred-completion composition review

本轮把 virtual-clock scheduler 接入独立 `stm32h723-usb-host` reference profile，保持
`dm-mc02` 的 USB Device-mode 组合不变。QEMU host-controller device 通过两个初始化属性
表达组合策略：`completion-scheduler=false` 保持通用同步兼容路径，reference profile
显式开启；`completion-delay-ns=0` 只表示默认 virtual timer delay，不等于同步调用。
因此 scheduler 仍属于 QEMU composition 层，H723 core、通用 transport 和 board profile
可以在没有 QEMU timer 的环境中复用。

首个编译错误是 composition 文件漏包含 `hw/qdev-properties.h`，导致 `Property` 和
`DEFINE_PROP_*` 在该层不完整；补 include 后 QEMU 重链通过。随后发现属性表声明为
`const` 与 QEMU `device_class_set_props()` 的非 const 接口产生警告，按现有 QEMU
设备模式修正为静态 `Property` 表，窄构建无该警告。

生命周期复核确认 scheduler entry 保存裸的 H723 host 指针，因此 reset 必须先取消 timer，
unrealize 必须先清除 transport 的 callback 再销毁 scheduler。两条顺序已实现；新增
reference qtest 在 `1000 ns` 延迟下验证 deadline 前无 `HCINT`、deadline 后产生
`XFRC|CHHLTD -> HAINT -> GINTSTS.HCINT -> NVIC`，并验证 pending completion 遇到
`system_reset` 后不产生旧 completion。

验证：QEMU 主体重链、`stm32h723-usb-host-test` `3/3`、scheduler unit `5/5`、transport
unit `12/12`、`run-stm32h723-usb-host-smoke.sh` 和
`run-stm32h723-usb-host-bulk-smoke.sh` 均通过；未修改 `trobot/`。完整 QEMU 回归中既有
`qtest-arm/test-hmp` 的 `mouse_button 0` SIGSEGV 仍是独立已知缺陷，不能宣称全量回归全绿。

## 2026-09-01 QEMU multi-channel deferred completion/cancel review

本轮继续沿 QEMU composition 边界推进。producer 是同一个 reference
`stm32h723-usb-host` 中两个真实 USB control channel，boundary 是 H723 channel
cancel callback 与固定容量 virtual-clock scheduler，consumer 是 H723 的
`HCINT -> HAINT -> GINTSTS.HCINT -> NVIC` 汇总。没有把 QEMU scheduler 接到
DM-MC02 的 USB Device profile，也没有修改 `trobot/`。

此前的缺口是 guest 写 `HCCHAR.CHDIS` 时只停用 H723 channel，scheduler 没有收到取消通知，
因此 timer 会一直保留到 deadline。修复在 H723 host 层新增 `host + channel + token` cancel
契约；QEMU scheduler 只删除精确匹配的 timer。reset 仍由 composition 先清 scheduler、再
reset host；unrealize 先解除 callback、再销毁 scheduler。由于回调不持有 packet 或 request，
也不引入锁、线程、队列或额外数据复制。

实现复核特别检查了多 host 情况：取消匹配同时比较 host、channel 和 token，错误 token 不会
取消其它 pending entry。H723 host reset 会通知仍 waiting 的 channel；在 reference composition
中 scheduler 已先 reset，所以这些通知是无效匹配，不会重复减少 pending。`CHDIS` 路径只在
`CHENA && waiting_completion` 时通知 scheduler，已完成 channel 不会误触发取消。

最小验证通过：scheduler unit `6/6`、reference qtest `4/4`、host transport unit `12/12`、
H723 control/bulk guest smokes，以及 host CTest `42/42`。reference qtest 明确验证两个
channel 同时 pending 和共同 IRQ 汇总，并验证 channel 0 被取消后 channel 1 仍在 deadline
完成。仍未验证 firmware async dispatcher 直接运行在 guest IRQ handler 中；下一道门应保留
为一个最小真实 guest async IRQ smoke，不要从此处扩展到 DMA/FIFO 或 board host wiring。

## 2026-09-01 H723 async completion ownership correction review

本轮复核发现异步 firmware 层的事件所有权存在两个实际风险：bulk `poll()` 调用全局
dispatcher 可能消费同一 dispatcher 上其它 channel 的事件；若 IRQ handler 已先完成当前
channel，bulk 再以 `token_active()` 判断会把合法 terminal record 报成 invalid。另一个资源
风险是 dispatcher 忽略 `POLL_INVALID`，会留下 pending operation、slot 和 allocator lease。

修复将事件消费边界收敛为 `dispatch_handle_channel(dispatch, channel, token, timestamp)`，
通过 slot 中的 async 指针和 completion token 双重匹配，只处理调用者指定的 channel。bulk
`poll()` 仅在自身 completion 仍 pending 时调用该接口；IRQ 先消费时直接读取已记录的
completion result。malformed terminal register state 触发 async cancel，并清理 dispatcher
slot；如果 bulk 发现 dispatcher 异常但自身仍 pending，也执行一次 abort。`dispatch_start()`
在 async PIO 写寄存器之前验证 dispatcher 与调用者的 base 一致，避免错误 MMIO 写入。

首个回归失败来自既有 stale-slot oracle：stale 清理并非正常 completion，不应增加 global
dispatch 的返回计数；保留旧的 count 契约后窄测恢复。另一次验证命令使用了不存在的 bulk
脚本名称，已按实际 `run-stm32h723-usb-host-bulk-smoke.sh` 重跑通过。

验证：dispatcher/bulk 相关 CTest `3/3`，新增 contract smoke 独立 `-Werror` 编译运行，
ASan/UBSan 窄测 `3/3`，完整 host CTest `44/44`，严格 ARM 对象编译 `2/2`，真实 H723
async IRQ guest smoke 和 bulk guest smoke 均通过。残余风险：该层仍是固定 12 channel、单核
caller-provided lock 和单包 PIO；没有 controller 内部多包 continuation、DMA/FIFO arbitration、
SOF retry、ISO、hub/topology、PHY/VBUS、passthrough 或 DM-MC02 Host-role wiring。

## 2026-09-01 H723 DWC2 host lifecycle and packet accounting review

本轮审查的首个边界错误是 host channel 的异步 owner 生命周期：disconnect、`HPRT0.PWR`
断电和 controller reset 原先只改变 host 内部状态，外部 scheduler 仍可能保留旧 timer。
现在这些路径与显式 `HCCHAR.CHDIS` 共用取消逻辑；取消回调收到精确的 host、channel 和
completion token，随后 channel 清除 active/waiting、`CHENA` 和 token，不生成伪造的
`CHHLTD` 或 completion IRQ。scheduler 必须按该三元组移除 pending work，reset/teardown
顺序仍由 composition owner 保证。

第二个错误是 accepted short/zero-length packet 的寄存器 accounting。修复后
`PKTCNT` 按 `ceil(actual_length / MPS)` 递减，零长度不消费 packet、不切换 DATA PID；
DMA 开启时 `HCDMA` 只按实际 accepted 字节前进。token 不匹配的迟到 completion 在检查
active/waiting 后直接丢弃，不修改 `HCTSIZ`、`HCDMA`、PID 或 `HCINT`。

transport 边界也修正为显式检查 control endpoint 的 SETUP 条件。非 control endpoint 即使
携带 SETUP PID，仍依据 `HCCHAR.EPDIR` 形成 IN/OUT transaction，避免 PID 字段污染方向。
transport 的 deferred hook 只传稳定标量；IN 数据在 hook 前已复制，scheduler 不得保存
request、transaction 或 packet 指针。

验证：`test-dm-stm32h7-otg-host` `22/22`、`test-dm-usb-host-channel-transport` `15/15`，
完整 host CTest `44/44`，包括 QEMU smoke suite，均通过。未修改 `trobot/`。残余风险仍为
完整 USB bus/PHY、DMA/FIFO arbitration、SOF retry budget、ISO/split、hub/topology、
passthrough 和 DM-MC02 Host-role wiring；下一道 review gate 应先明确 NAK retry 的 caller
与 virtual-time controller 责任。

## 2026-09-01 H723 USB host control NAK slice review

本轮审查的首个缺陷位于同步 control helper 的完成条件：
`firmware/dm_stm32h7_usb_host_control.c` 原先只等待 `HCINT.CHHLTD`，而 H723/QEMU 芯片层
对 control/bulk NAK 可以只置 `HCINT.NAK`。因此 control transfer 遇到 NAK 时可能永久等待。
修复后的边界是等待 `CHHLTD | NAK`，观察 NAK 后写 `HCCHAR.CHDIS` 并等待后续
`CHHLTD`，再返回 `DM_STM32H7_USB_HOST_CONTROL_NAK`；NAK 只结束当前 caller-owned synchronous attempt，
不由 control driver 隐式重试，后续整笔 transfer 的重试由 caller 负责。

验证时首先暴露的是测试夹具而非生产代码：`dm_stm32h7_usb_host_control_nak_smoke` 的
共享 MMIO 没有实现 `HCINT` 的 W1C 语义。driver 清除 `HCINT_ALL` 后，普通共享内存仍保留
全掩码，造成伪造的 NAK/CHHLTD 状态，worker 最终报告
`NAK worker did not observe channel disable`。修正收敛在确定性 fixture：用数组 MMIO
和一次性的 poll 在驱动清 pending 后注入 NAK，验证阶段计数、`HCCHAR.CHDIS` 和
SETUP PID；该 fixture 不冒充完整 H723 W1C 模型。

另一个首个错误位于 control helper 的输出契约：非成功 stage 会提前返回，但原实现没有
初始化 caller 提供的 `actual_length`。现在在参数指针确认后先归零，避免 NAK/STALL/传输
错误泄露旧值；该修复属于同步 control client 层，不由上层 fallback 处理。

进一步复核发现 control client 的公共输入边界还缺少三类校验：非零数据长度可能携带空
指针，非法 channel/address/MPS 会形成错误 MMIO 或污染 `HCCHAR`，以及 accepted completion
的 `HCTSIZ.XFERSIZE` 大于本阶段长度时会发生无符号下溢。现在这些条件在 MMIO 前返回
`INVALID`/传输错误；回归同时锁定了输出长度、无数据阶段和端点字段范围。

当前状态：control NAK smoke、受影响 H723 host CTest `16/16`、三条真实 Cortex-M7/QEMU
guest host smoke 以及完整 CTest `45/45` 均通过。该切片仍不覆盖 retry budget/timeout、
异步 completion/cancel、DMA/FIFO、SOF、PHY/VBUS、拓扑或 DM-MC02 Host-role wiring。

## 2026-09-01 H723 DMA M2P FIFO burst review

本轮在 STM32H723 DMA 芯片层补齐了 `SxCR.PBURST[22:21]` 和 `MBURST[24:23]` 的四种
有效编码。producer 是 DMAMUX 选中的 M2P request 和 burst 配置，boundary 是 FIFO
内存预取与外设提交，consumer 是单 beat DMA endpoint。`MBURST` 现在限制一次 FIFO
fill 中每个连续 memory burst 的 beat 数；FIFO 满时允许在边界截断更大的 INCR8/INCR16，
下次 fill 从新的 burst 开始。`PBURST` 保持逻辑 request 分组信息，但不把多个外设
副作用合并，因此 UART endpoint 仍每次只收到一个 `PSIZE` beat。

隔离 fixture 新增 16 种 `MBURST/PBURST` 组合，验证 16-bit memory 到 8-bit endpoint
的连续字节顺序、callback size/call count、FIFO 清空、`NDTR` 和 `TCIF`；ASan/普通
host 版本均通过。UART1/USART2 真实 Cortex-M7 guest smoke 均设置 `MBURST=INCR4`、
`PBURST=SINGLE`，endpoint on/off 四条路径均通过；DMA FIFO、batch、仲裁、DBM、TIM8
和 SPI2 回归也通过，QEMU 主体已重链。

最终 host 构建和完整 CTest `45/45` 通过（含 QEMU smoke suite，耗时约 14.6 s）。

这是无总线时钟的确定性分组模型，不表示真实 burst 延迟、总线仲裁、FIFO FEIF 产生条件、
异步 endpoint backpressure、per-beat rollback、M2M DBM 或完整错误恢复。后续若需要
时序/吞吐精度，应先在 DMA 芯片层定义独立的 virtual bus arbitration 边界，再接上层
外设；不能把当前软件 batch 结果当成硬件 burst 证据。
## 2026-09-01 H723 ADC/TIM3 OC4REF trigger review

本轮继续停留在 STM32H723 ADC 芯片层及其与 DM-MC02 board profile 的直接边界。producer
是 TIM3 在 `MMS=7` 下产生的 `OC4REF` rising event；boundary 是 board timer route
根据 `DmMc02TimMasterEventKind` 选择 event-specific source；consumer 是 ADC regular
`EXTSEL=15` 或 injected `JEXTSEL=4`。ADC 芯片层仍将 regular source ID 与 injected
选择值分开解码，避免把两个 mux 数字混用。

实现采用 `master_event_source_id[2][8]` 与 `master_event_source_valid[2]`。只有
valid 位设置的事件覆盖旧的 `trgo_source_id`/`trgo2_source_id`；未设置时继续走旧
update route，因此已有 profile 保持兼容。board profile 初始化还拒绝有效 event-specific
source 大于 `0x1f`，但没有把其它保留编码宣称为已连接物理 source。

首个新增验证问题是 validation 错误信息使用 `%zu`，而该 QEMU 构建路径的格式检查将
其视为 warning；已在 board validation 中修为 `%u`。这属于诊断输出层问题，不是 ADC
触发状态错误。修正后窄构建和测试均通过。

隔离/边界验证：ADC qtest `38/38` 覆盖 regular 与 injected source matching 和错误
source rejection；TIM qtest `21/21` 覆盖 `OC4REF` producer；ADC trigger ARM smoke
`11/11` 覆盖真实 TIM3 `MMS=7`、DMA 结果和 `ADSTART` 清除。当前限制是完整 H723
`EXTSEL/JEXTSEL` source matrix、EXTI/LPTIM/HRTIM producer、组合 timer mode 和硬件级
跨总线 edge latency 仍未实现；下一步应继续逐个增加底层 producer 与对应边界测试，不能
用 `TIM3_CH4` 的映射替代完整矩阵。

## 2026-09-01 SPI target boundary 与 BMI088 adapter review

本轮复核了 SPI 解耦切片的 producer/boundary/consumer 责任。producer 是 H723 SPI2 的
MMIO/DMA byte event 和 DM-MC02 GPIO CS；boundary 是板卡无关的 `DmMc02SpiTarget`
transfer/select callback；consumer 是 `DmMc02Bmi088Spi`。此前 SPI core 同时持有 BMI088
framing、芯片状态和 co-sim 消费逻辑，导致未来替换 Flash 或复用 SPI 时必须携带 DM-MC02
私有状态。现在 core 只管理寄存器、DMA、RX/TX 状态和最多 4 个 target，BMI088 adapter
单独处理 command/read/write、accel dummy byte、FIFO streaming 以及 frame-consumed
事件，board composition 负责 DMA tuple、GPIO mask 和 outer step token。

边界审计确认：CS 从一个 target 切到另一个 target 时，所有 target 都收到完整 selected
mask，当前 adapter 会结束未完成 FIFO read 并清除 framing；多选 mask 不向任何 target
发送 transfer byte，保留为可观察的无电气争用 functional 行为。SPI core reset 会取消
TX timer 并清理自身状态，machine reset 随后显式 reset 两个 adapter 和 BMI088 chip；
因此 target-specific framing 不依赖 core 的 selected-mask 初值。SPI2 的 DMA timer 已在
board init 启用，SPI1 未配置 DMA channel，故本路径不会把 deferred TX 调度到空 timer。

首个验证路径是错误的测试范围而非生产代码：新增文件名为 `bmi088_spi_smoke` 的测试
最初只验证 adapter，并未覆盖 SPI core 的 target routing；审计后没有在 fixture 中复制
SPI core，而是把它明确记录为 adapter isolation gate，并继续使用已有 SPI2 polling/DMA
guest smoke 验证真实 core-to-board boundary。没有发现重复 `STEP_DONE`、跨 CS 半帧或
reset 后旧 framing 传播的问题。

验证：`ctest --test-dir build/host --output-on-failure` 为 `46/46`；其中 BMI088 chip
与 adapter CTest 均通过，QEMU smoke suite 为 `82/82`。QEMU 增量构建已通过；构建日志仍
只有 QEMU 上游 `vhost_svq_poll()` 的既有 `r may be used uninitialized` 警告，不在本切片
修改范围。`trobot/` 无新增修改，未保留活跃子代理。

残余风险：当前仍是 functional SPI byte model，不覆盖所有 SPI mode、bit-level timing、
真实 DMA FIFO/总线仲裁、电气 CS contention、target hot-plug 或 Flash adapter。新增 target
必须先通过自己的 adapter isolation test，再由 board composition 增加 direct boundary
smoke；不得把这些行为作为 SPI core 的隐式 fallback。

## 2026-09-01 SPI dispatch 与 OSPI page-program review

本轮首先验证了上次解耦后真正未覆盖的底层边界：既有 BMI088 adapter smoke 没有经过
`dm_mc02_spi.c` 的 MMIO target dispatch。新增的 `dm_mc02_spi_target_smoke` 使用两个
独立 fake target 和一个真实 BMI088 adapter，确认单选目标才收到字节、无选和多选不产生
功能响应、所有 target 收到完整 CS mask、时间戳透传，并在 CS 切换时丢弃半条命令。首次
失败来自测试夹具预期把多选 target 标成未选；生产实现按每个 mask bit 报告 selected，修正
oracle 后隔离测试通过。另一个夹具问题是 QEMU timer inline API 的重复桩定义，已改为只保留
必要的 `timer_init_full`、`timer_mod`、`timer_del` 和 address-space 链接桩。

OSPI 复核发现一个实际功能缺陷：page-program 长度超过 256 字节时，`execute()` 虽设置
`TEF`，却把 `tx_expected` 截断为 256；此前 `commit_program()` 未重新检查 DLR，因而在
第 256 字节到达时可能部分改写 Flash。修复在 OSPI 芯片层 commit boundary 重新验证
`DLR + 1 <= 256`，并在加一前拒绝 `UINT32_MAX`，拒绝交易而不改变 backing array；WEL
只有合法 program/erase 完成后才消费。该位置是根因修复，不是 board 或 firmware workaround。

验证：`dm_mc02_spi_target_smoke` 通过，`bash tools/run-ospi-smoke.sh` 通过，QEMU
`qemu-system-arm` 增量构建通过，完整 host CTest `47/47` 通过，其中 QEMU smoke suite
为 `82/82`。当前仍未覆盖完整 SPI line mode/bit timing、OSPI DTR/DMA/bus delay、真实
Flash persistence 或电气 CS contention；这些仍是后续层的明确限制。未修改 `trobot/`，
没有活跃子代理。

## 2026-09-01 NOR Flash storage 解耦 review

本轮沿 `STM32H723 -> DM-MC02 -> reusable device/driver` 继续向下闭合。原先
`dm_mc02_ospi.c` 同时拥有 8 MiB backing array、WEL/WIP、页编程和 sector erase 规则，
导致 OSPI 寄存器适配器与 W25Q64 存储语义不可复用。新增的 `DmNorFlash` 只绑定调用者的
存储和几何参数，返回明确的 `OK/BUSY/WRITE_PROTECTED/OUT_OF_RANGE/LENGTH_INVALID`
结果；OSPI 负责把命令和 DLR 事务转换为该接口。

复核期间发现一个解耦残留：OSPI 头文件还保留旧的 WEL/WIP 字段，`SR.BUSY` 仍读取重复
状态源。该问题不会在同步成功路径中显现，但会在未来引入异步 Flash 时产生状态分叉。
已删除重复字段，`SR.BUSY` 和 status-1 现在都来自 `dm_nor_flash_status()`。
同时，OSPI DR staging 只接收声明的 DLR 长度，避免一次较宽的 guest 写入把未声明字节
提交到 Flash。

验证：`dm_nor_flash_smoke` 通过，覆盖 WEL/WIP busy、无 WREN、NOR 1->0、页回绕、sector
erase、非法长度和越界原子性；`bash tools/run-ospi-smoke.sh` 通过；QEMU 完整增量构建
通过；host CTest `48/48` 通过，其中 QEMU smoke suite `82/82`。构建日志仍只有 QEMU
上游 `vhost_svq_poll()` 的既有 `r may be used uninitialized` 警告，不属于本切片。当前
核心没有真实擦写延迟或异步 WIP 窗口，Flash command framing、line mode、DTR、DMA、
persistence 和电气时序仍未覆盖。未修改 `trobot/`，没有活跃子代理。

## 2026-09-01 SPI NOR framing adapter review

本轮复核新增的 `dm_mc02_spi_nor` 是否遵守逐层边界。producer 是通用 SPI target
callback，adapter 只负责命令字节、三字节地址、CS 事务和结果提交；NOR geometry、WEL、
WIP、1-to-0 编程、页回绕和 sector erase 仍由 `DmNorFlash` 负责。adapter 不拥有存储，
因此可以被其它 SPI core 或测试夹具复用。

发现并修复了两个错误。第一个是测试 oracle 把普通 READ 错当成 page-wrap：页回绕只适用
PAGE PROGRAM，普通读取应在线性地址空间继续，修正后断言下一地址的擦除值。第二个是非法
CS mask 的 phase 虽已设为 ignore，但首字节仍先进入命令分派，非法 WREN 会错误设置 WEL。
现在 transfer 在命令解码前短路 ignore 态，测试覆盖多选 mask 不改变状态。

重点边界审计结果：CS release 时 program/erase 才提交；257-byte staging buffer 保证
超过最大页长度交给 NOR core 做原子拒绝；失败不会清除 WEL 或改变 storage；CS 中断和
reset 会丢弃 framing state；`last_result` 初始化为 `DM_NOR_FLASH_INVALID`。timestamp
目前只透传到接口，不声称真实 Flash latency。QEMU target table 尚未直接承载该 adapter，
所以本轮没有宣称 board-level SPI Flash 已接入。

验证：`dm_spi_nor_flash_smoke` 通过；`bash tools/run-ospi-smoke.sh` 通过；
`./.venv/bin/meson setup --reconfigure build/qemu qemu/upstream` 与
`ninja -C build/qemu qemu-system-arm` 通过；完整 host CTest 为 `49/49`，其中
`dm_mc02_qemu_smoke_suite` 为通过、耗时约 `66.69s`。构建仍有既有 QEMU 上游
`vhost_svq_poll(): 'r' may be used uninitialized` 警告，不在本切片范围。未修改
`trobot/`，没有活跃子代理。

残余风险：当前 adapter 仍不覆盖 quad/DTR/bit-level SPI timing、完整 W25Q status/config
寄存器、异步 WIP、persistence、DMA/FIFO arbitration、电气争用或 board pin mapping。
下一道门必须先增加 `dm_mc02_spi` MMIO/DMA 到该 adapter 的直接组合测试，再接入板级
profile，不能用 board workaround 代替该边界验证。

## 2026-09-01 SPI core 到 NOR adapter 直接组合 review

本轮沿 `STM32H723 SPI data path -> DmMc02SpiTarget -> SPI NOR framing -> DmNorFlash`
完成直接消费者验证。复用既有 `dm_mc02_spi_target_smoke` 的 QEMU MMIO fixture，新增
真实 NOR adapter 注册和 `TXDR/RXDR` 驱动，没有复制 SPI core，也没有把板级 pin map
写进通用器件层。

组合测试确认 JEDEC/status/WEL、page program、普通线性 readback 和 CS 中断均能跨过
target table；非法多选 mask 在 adapter 命令解码前被忽略，不能执行 WREN。测试使用
`PAGE PROGRAM` 的页边界回绕写入，同时验证 `READ` 在下一线性地址返回擦除值，固定了
协议层和测试 oracle 的边界。

验证：`dm_mc02_spi_target_smoke` 通过；完整 host CTest `49/49` 通过；
`bash tools/run-ospi-smoke.sh` 通过；`ninja -C build/qemu qemu-system-arm` 通过。
QEMU 构建仍有既有 `vhost_svq_poll(): 'r' may be used uninitialized` warning。
未修改 `trobot/`，没有活跃子代理。

残余风险：当前只证明 SPI core 的 MMIO/target-table 组合，不证明 DM-MC02 GPIO
active-low CS、alternate-function 解码、板级器件 profile、quad/DTR、真实 Flash
latency/persistence 或电气总线争用。下一道门应在 DM-MC02 board 层定义并测试 GPIO/AF
到 CS mask 的 route，再决定是否将 Flash 设备组合进 machine。

## 2026-09-01 GPIO SPI CS route review

本轮把 GPIO active-low CS 从 machine 的 BMI088 专用硬编码移动到
`DmMc02BoardProfile.spi_cs_routes`。板级 route 负责 controller、target bit、GPIO
pin 和 polarity；`dm_mc02_board_decode_spi_selected_mask()` 只读取 data-only 的
`MODER/ODR` snapshot，并把结果交给 SPI core。未处于 GPIO output mode 的 CS 不被视为
有效驱动，target capacity 由调用者传入，避免 board 层依赖 SPI core 的固定容量。

审计中首先发现 active-low 判断写反：高电平被误认为 selected，导致隔离测试在
`selected_mask == 0` 处失败。修复为 `active_low ? !high : high` 后，窄 route smoke
和真实 BMI088 polling smoke 均通过。随后发现 smoke guest 只写 BSRR、没有配置 PC0/PC3
为 GPIO output；这是 fixture 初始化缺失而非生产模型错误，已补齐相关 SPI/BMI088
fixtures 的 `MODER` 设置。

验证：`dm_mc02_board_profile_smoke` 通过；BMI088 polling、SPI2 DMA endpoint
`on/off`、FIFO、filter、cosim readback/timing smoke 通过；QEMU 增量构建通过；完整
host CTest `49/49` 通过，QEMU smoke suite `82/82` 通过。QEMU 上游仍有既有
`vhost_svq_poll(): 'r' may be used uninitialized` warning，不属于本切片。

残余风险：当前只覆盖软件 GPIO CS，不覆盖硬件 NSS/AF CS、真实电气 contention、SPI
bit timing、Flash device profile 或将 NOR adapter 接入 machine。下一切片应保持此 route
契约，添加独立的 board Flash target mapping 与 direct read/program/erase smoke。

## 2026-09-01 OCTOSPI Flash profile composition review

本轮复核 `DmMc02BoardFlashProfile -> DmMc02OspiFlashConfig -> DmMc02Ospi` 的
producer、边界和 consumer。DM-MC02 的真实外部 Flash 是 `OCTOSPI2/W25Q64JV`，
几何为 8 MiB、256-byte page、4 KiB sector、JEDEC `EF 40 17`；EVAL profile
显式无外部 Flash。board 层只描述 profile，OSPI 实例拥有 backing storage 和 NOR
语义，memory-mapped region 只在 profile capability 存在时组合，因此没有把未初始化
的 Flash region 接到无 Flash profile。

审计发现并修复一个芯片层错误：读事务在 `DLR + 1` 时可能因
`DLR=UINT32_MAX` 回绕为零，producer 的非法长度因此越过 boundary 被 consumer 当成
成功的空事务。现在统一的长度解码在加法前拒绝该值，读和 page program 均报告
`SR.TEF`；WEL 和 backing storage 保持不变。验证过程中还发现 smoke consumer 仍读取
`xp /22wx`，无法观察新增的两个结果字；修正为 `/24wx` 后再验证，避免把测试观测缺口
误判为模型缺陷。

验证结果：`dm_mc02_board_profile_smoke` 通过；`bash tools/run-ospi-smoke.sh` 和
`bash tools/run-board-profile-smoke.sh` 通过；`ninja -C build/qemu qemu-system-arm`
通过；host CTest `49/49` 通过。构建仍可能出现 QEMU 上游
`vhost_svq_poll(): 'r' may be used uninitialized` warning，不属于本轮路径。当前
仍不覆盖 OCTOSPI DMA、真实 line mode/DTR、Flash 延迟/异步 WIP、ECC/option bytes、
persistence、总线电气时序或其它 OSPI 设备；这些必须在相应底层边界单独实现和验证。
本轮未修改 `trobot/`，Luna 审计任务完成后立即关闭。

## 2026-09-01 W25Q64 erase granularity review

本轮沿 `DmNorFlash -> DmMc02Ospi -> DM-MC02 W25Q64 profile` 检查擦除命令的分层。
原核心只有 sector erase，而固件侧 W25Q64 driver 还公开使用 32 KiB、64 KiB 和整片
擦除。新增的 `dm_nor_flash_erase()` 只处理可复用的几何、地址对齐、WEL/WIP 和存储
修改；OSPI 仅把 `0x20/0x52/0xd8/0xc7` 转换到该核心，board profile 没有新增设备
特定逻辑。

测试先在 128 KiB host fixture 中验证非对齐 block address、非法粒度的原子拒绝、WEL
保持和 chip erase，再在真实 ARM guest 的 OSPI register path 中验证三种新命令。测试期望
使用对齐地址编程、非对齐地址擦除，避免把 32-bit MMIO little-endian 表示误判成 Flash
地址错误。chip erase 运行后通过 memory-mapped window 检查已编程位置回到 `0xffffffff`
并确认 status 清零。

验证：`dm_nor_flash_smoke`、`dm_spi_nor_flash_smoke`、`bash tools/run-ospi-smoke.sh`、
`ninja -C build/qemu qemu-system-arm` 和 host CTest `49/49` 全部通过。没有发现本轮
新增的 producer/boundary/consumer 不一致；QEMU 上游既有
`vhost_svq_poll(): 'r' may be used uninitialized` warning 仍不属于本路径。剩余风险是
真实擦除时延/异步 WIP、掉电、保护位、ECC、persistence 和其它 Flash 型号，不能由当前
同步 functional model 的通过结果推断已支持。

# 2026-09-01 OCTOSPI DMA endpoint review

The first failure in this slice was in the isolation fixture, not the OSPI
endpoint: `flash_window` intentionally returns `0xff` unless OSPI is in
memory-mapped mode, while the test read it immediately after programming.
The fixture now writes `OCTOSPI_CR.FMODE=memory-mapped` before inspecting the
window. The production endpoint was not changed to accommodate the test.

The endpoint review confirms that P2M rejects an empty or exhausted RX queue,
and M2P rejects inactive commands, invalid directions, zero-length beats, and
beats beyond the declared page-program length. OSPI reset clears command,
queue, staging, and memory-mapped runtime state while preserving the backing
Flash bytes through the NOR core.

The direct consumer smoke then exercises the real DMA/DMAMUX state machine with
synthetic routing identifiers: M2P programs four bytes, P2M reads them back
into guest RAM, and an endpoint rejection latches TEIF while leaving the P2M
destination and NDTR unadvanced. `dm_mc02_ospi_dma_endpoint_smoke` and
`dm_mc02_ospi_dma_integration_smoke` pass, as do full host CTest `51/51`, the
`82/82` QEMU smoke suite, both OSPI/profile scripts, and the ARM system build.

The route is deliberately not connected to the DM-MC02 profile yet. The
current firmware configuration does not provide evidence of an enabled OSPI
DMA request, so this review does not claim a real DMAMUX ID, OCTOSPIM route,
or firmware-level OSPI DMA support. Existing QEMU header diagnostics and the
uninitialized-variable warning in upstream `vhost-shadow-virtqueue.c` remain
outside this slice.

## 2026-09-01 NOR Flash raw-image persistence adapter review

本轮复核 `dm_nor_flash_persistence.[ch] -> DmMc02Ospi -> dm-mc02 machine`
的 producer、boundary 和 consumer。可复用 adapter 只接收 caller-owned byte
storage、storage size 和 path，读写完整 raw image；文件必须是精确设备几何的原始镜像，
没有 header 或格式转换。`DmMc02Ospi` 的 load/save 是薄封装，只传递 OSPI backing
storage 与 `flash_size`，没有复制 persistence 逻辑。

结果枚举明确区分 `OK`、`DISABLED`、`NOT_FOUND`、`IO_ERROR`、`SIZE_MISMATCH` 和
`INVALID`。空路径返回 `DISABLED` 且不做磁盘 I/O；缺失文件返回 `NOT_FOUND` 并保持
调用者预置的擦除态；尺寸不匹配在读入前拒绝且不改变 storage；其它文件操作错误返回
`IO_ERROR`。machine 的 `ospi2-flash-file` 只允许在初始化锁定前设置，OSPI2 创建后执行
启动加载，缺失文件可接受，其它加载错误告警并保留擦除镜像；正常退出 notifier 才执行
保存，保存错误告警。

验证：`dm_nor_flash_persistence_smoke` 通过，覆盖空路径/缺失文件、完整 raw bytes
往返、尺寸不匹配的无修改保证和 I/O error；OSPI 直接边界及
`bash tools/run-ospi-smoke.sh` 通过，覆盖 8 MiB W25Q64 启动加载、guest 读回、正常
`quit` 后保存及输出尺寸检查；host CTest 为 `52/52`。本轮没有修改 `trobot/`。

残余风险：这是正常生命周期的 functional persistence，不是掉电模型。当前不提供真实
Flash latency、ECC、异常终止/崩溃保存、掉电原子性或恢复协议；保存只发生在正常退出，
不能据此宣称真实 Flash durability。

## 2026-09-01 ADC JAUTO/JQM regular DMA review

本轮沿 `ADC1 regular EOS -> JAUTO injected context -> regular DMA` 检查
producer、boundary 和 consumer。ADC 芯片层负责 regular EOS 时自动消费有效的
injected `JSQR` context，并在 `JQM=1` 且没有 pending context 时清空活动 context
和 CPU-visible `JSQR`；DMA 仍只负责 request、地址、NDTR、HT/TC 和 circular
状态，guest smoke 只观察公开寄存器和 RAM buffer，没有访问 ADC/DMA 私有状态。

新增的 ARM guest smoke 首次运行暴露的是测试 consumer 缺陷：QMP `xp` 返回的单元素
列表被脚本直接用于位运算，导致观测逻辑异常。脚本已先确认返回长度再读取值，并重新
通过；没有为测试修改 ADC 或 DMA 模型。最终断言 regular DMA 在自动 injected sequence
完成后继续运行，`JEOC|JEOS` 置位、`JDR1=0x0100`、`JSQR=0`、DMA HT/TC 置位且
两个 32-bit DMA words 均编码为按 rank 顺序排列的
`0x0100, 0x0200, 0x0100, 0x0200` 四个 half-word sample。

验证边界是明确的：该结果证明当前成功路径上的 `JAUTO+JQM+regular circular DMA`
组合（regular/injected 各两 rank），不证明完整 H723 injected queue、外部 trigger source matrix、discontinuous
组合、低功耗语义或真实 ADC/DMA 时序。下一道门应在 ADC 芯片层增加多 rank、JADSTP
中止、缺失 context 和 queue overflow 的回归，再接入更复杂板级采样配置。

## 2026-09-01 VIN smoke cleanup review

审计 `run-vin-config-smoke.sh` 的 producer/boundary/consumer 后确认，成功路径的
QEMU 进程会被终止，但 cleanup 只执行 `rmdir`，而目录内仍保留 `qemu.stderr`，导致
每次运行泄漏一个临时目录。修复只删除本次 runner 创建的 stderr 和 QMP socket，随后
移除该精确目录；没有使用上层测试结果或 QEMU 模型逻辑掩盖问题。

验证：`bash -n tools/run-vin-config-smoke.sh`、VIN smoke 通过，精确目录计数保持
`407 -> 407`。此前已存在的 407 个目录未删除，避免把历史生成物清理混入本切片。
该修复只覆盖该 runner；其它 smoke 的 cleanup 策略仍需单独审计，不能据此宣称所有
工具都具备无泄漏退出保证。

## 2026-09-01 H723 D2 APB/timer clock review

本轮首先确认生产代码的错误来源是 DM-MC02 machine 中所有 timer 共享
`CPU/2`，而不是 timer lazy phase 算法。真实 H723 配置的 producer 链为
`SYSCLK=480 MHz -> HCLK=240 MHz -> APB1/APB2=120 MHz -> timer=240 MHz`；
因此将分频规则提取到板卡无关 `dm_stm32h7_clock_tree`，RCC 负责有效时钟查询，
board profile 只声明 timer 所属 APB domain。

实现覆盖 `D2PPRE1/2`、`TIMPRE`、APB1/APB2 PCLK 和 timer kernel clock，并在
`D2CFGR` 或 `CFGR` 改写时调用既有通知边界。DM-MC02 用独立的 APB1/APB2 QEMU
`Clock`，运行中改 APB1 时先 snapshot timer phase，再只更新对应 domain；未就绪
时钟仍不把零频率传播到 timer scheduler。profile validation 拒绝未知 clock domain。

验证先在纯 helper 运行 APB 全部有效编码和 TIMPRE 组合，再由
`/dm-mc02/tim2/apb-timer-domains-independent` 观察真实 QEMU MMIO 改频：TIM2
速率改变，TIM8 不变，二者相位连续。最终 helper、QEMU build、定时器 qtest `22/22`、
TIM2/PWM smoke 和 CTest `53/53` 均通过。PWM smoke 首轮失败是旧 fixture 假设复位
timer 为 32 MHz，已将其改为真实 64 MHz 的等价 1 kHz 配置；没有用 machine 常量
掩盖时钟错误。未修改 `trobot/`，没有活跃子代理。

残余风险：当前仅把 timer consumer 接到 APB1/APB2，FDCAN 仍使用 board profile 的
固定 120 MHz，UART/SPI/其它 kernel mux、APB3/APB4/D3、clock settling、CSS 和低功耗
时钟语义尚未建模。下一步应在相应芯片层为一个直接 kernel-clock consumer 建立隔离门，
不能由 board 层继续添加固定频率 workaround。
# 2026-09-01 FDCAN kernel-clock review

- Resolved: DM-MC02 no longer injects a board-level fixed `120 MHz` FDCAN
  clock. H723 RCC now calculates HSE/PLL1Q/PLL2Q from `D2CCIP1R`, PLL source,
  M/N/Q, fractional state, and Q output enable, then propagates changes to all
  FDCAN consumers.
- Resolved: the prior test fixture assumed PLL1 was usable after only setting
  `PLL1ON`; it now enables the required PLL1P output for the system-clock
  readiness test. A transient test race was removed by using separate guest
  launches and a completion marker rather than relying on a wall-clock window.
- Verification: QEMU build, FDCAN source smoke `4/4`, FDCAN medium smoke,
  PWR/RCC smoke, machine smoke, and the real Release firmware RTF probe passed;
  `trobot/` was not modified.

Residual risks: source readiness is immediate, FDCAN operation is not yet
blocked by a zero kernel clock outside accurate-timing scheduling, and the
complete H723 kernel-clock matrix/low-power/CSS/settling behavior remains
unimplemented. These are chip-layer follow-ups, not board-profile constants.

# 2026-09-01 USART kernel-clock review

The first incorrect boundary in the previous UART model was the implicit
board-level clock assumption: routed UARTs had no H723 `D2CCIP2R` source
selection and could not observe APB/PLL/HSI changes. The producer is now RCC's
effective source/readiness state, the boundary is the two USART mux-group
queries plus QEMU `Clock` propagation, and the consumer is the reusable UART
model's cached kernel rate. No UART code reads RCC registers or profile data.

The source matrix is explicit: USART16 uses mux bits `5:3` and APB2;
USART234578 uses bits `2:0` and APB1. APB, PLL2Q, PLL3Q, HSI/HSIDIV, CSI,
LSE, and reserved/disabled results are covered. PLL Q outputs require the
selected PLL input, valid divisors, and `DIVQEN`; unavailable sources return
`0 Hz` instead of retaining a stale rate. UART clock callbacks are cleared
before UART teardown, so the machine's clock objects cannot call freed UART
state during destruction.

Verification: `ninja -C build/qemu qemu-system-arm`, the 9-case UART source
matrix smoke, the complete QEMU smoke suite `86/86`, and serial host CTest
`53/53` passed. A power-boundary fixture still emits existing QEMU-header
`-Wpedantic`/unused-parameter warnings; it is outside this UART path.

Residual risks: the current HMP command set has no supported `mw`, so runtime
RCC rewrites are not covered by the UART smoke; each source case starts a fresh
guest. The model still has no BRR/PRESC baud timing, bit serialization,
oversampling, settling latency, CSS, clock gates, or full H723 kernel mux
matrix. The next gate belongs to the reusable UART layer: define and isolate
BRR/PRESC baud timing before adding more board integration.

# 2026-09-01 H723 USART BRR/PRESC virtual-time TX review

本轮将 BRR/PRESC 时序留在可复用 UART 器件边界。`dm_uart_timing_calculate()` 只接受
kernel clock、BRR、PRESC 和 OVER8，按 STM32H7 的 PRESC 表与 16x/8x BRR 规则返回精确
整数 baud ratio 以及默认 8N1 的虚拟 frame duration；没有把 RCC、DM-MC02 profile 或
chardev 状态倒灌到 helper。非法 source/分频/BRR 会返回无效结果，helper 采用
`__uint128_t`，不引入浮点或 wall-clock 时序。

DM-MC02 UART 在 RCC clock callback、BRR/PRESC/OVER8 写入后重新计算该结果。有效配置下
TX queue 由 QEMU virtual timer 每帧发出一个字节，短写仅安排非阻塞重试；最后一个字节
实际写入 host backend 后才置 `TC`。复位和未配置 BRR 时仍使用旧的立即兼容路径，因此
已有 polling smoke 不会被新时序要求卡住。RX 仍是 chardev 字节流，尚未宣称位级采样。

验证：`dm_uart_timing_smoke` host CTest 通过；`tools/run-uart-timing-smoke.sh` 用
qtest 精确推进 `999999 ns + 1 ns` 边界，验证两个 1 ms 8N1 帧、字节顺序及 `TC/TXE`；
`run-uart-smoke.sh`、`run-uart-idle-smoke.sh`、`run-uart2-dma-smoke.sh on/off` 通过；
QEMU `qemu-system-arm` 重链通过。真实 Release 固件采样为 `1001` tick /
`1.001201 s`、RTF `0.999800x`、CPU `102.9%`、RSS `50296 KiB`、IWDG
`timeouts=0`；NullEngine `184376 IMU frames/s`、worker startup `64.953 ms`、
DM-MC02 startup `49.847 ms`。未修改 `trobot/`，Luna 已复核并关闭。

残余风险：当前 helper 只提供默认 10-bit 8N1 frame，未消费 UART parity/stop-bit 配置；
RX/IDLE 没有 baud-rate sampling，动态改钟只重排尚未发送的 TX queue 而不保留真实在途
帧的位相，LIN/Smartcard/IrDA 和完整 H723 USART mode matrix 仍未实现。下一道门应在
可复用 UART 层单独定义 RX/IDLE virtual-time 契约，再由 DM-MC02 组合，不应通过外部工具
或 UI 伪造时序。

# 2026-09-01 FDCAN -> QEMU standard CAN bus migration review

本轮的首个错误边界是 transport ownership，而不是 FDCAN message-RAM 解析：旧生产路径把
板级 virtual-time medium 同时承担了 CAN-ID 仲裁、帧持续时间和 ACK 判定；这些能力并不由
QEMU standard `CanBusState` 提供。现在 producer 是 FDCAN TX message-RAM path，boundary
是板卡无关的 `DmCanBusAdapter`，consumer 是 QEMU CAN clients 与 FDCAN RX filter/FIFO/
dedicated-buffer path。FDCAN 继续拥有寄存器、message RAM、过滤器、FIFO、IRQ 和粗粒度
controller error state，adapter 不访问这些私有状态。

生产路径现在统一为 QEMU standard CAN bus：FDCAN1/2/3 默认连接 machine 创建的内部
`CanBusState`，也支持显式 `-object can-bus,id=canbus -machine dm-mc02,canbus=canbus`。
standard bus 只提供即时 peer 广播、发送者不回环和 peer-acceptance 返回值；没有 CAN-ID
仲裁、帧 duration、bit stuffing、物理 ACK、error frame 或 frame timestamp。standard-bus
ingress 由 FDCAN 使用当前 `QEMU_CLOCK_VIRTUAL` 赋时；固定 84-byte chardev 仍是独立的
legacy host wire，保留外部 timestamp，并不经过 `CanBusState`。`fdcan-host-ack=on` 仍是
明确的 host-wire policy，不代表物理 ACK。

为避免 adapter 首次连接把未初始化结构误当作已连接 client，生命周期已收敛为显式
`init -> connect -> disconnect`；重复 connect 被拒绝，重新连接必须先 disconnect。旧
`dm_mc02_can_medium.[ch]` 已从 ARM Meson production target 移除，并明确标记为未来
virtual-time scheduler 的 test-only fixture；没有保留等价 production runtime 双轨。

验证：`ninja -C build/qemu qemu-system-arm`、`test-dm-can-bus-adapter`（1/1）、FDCAN
standard bus/classic-FD/filter/Rx Buffer/BUS-OFF/RCC/machine 定向 smoke、host CTest
`54/54` 和 QEMU smoke suite `88/88` 均通过。未修改 `trobot/`，没有活跃子代理。

残余风险：QEMU standard bus 的 ACK 只是 peer callback 的 functional 结果；没有真实 CAN
物理层、仲裁、位级时序、错误帧或 timestamp transport。若未来需要准确虚拟时序，必须在
standard bus 上增加独立、通用的 scheduler adapter，并先通过隔离和直接边界测试，不能把
`fdcan-accurate-timing` 或 board-level timing workaround 加回 FDCAN production path。

# 2026-09-01 QEMU build isolation and ADC fixture review

本轮先单独重现 ADC 全量测试的首个失败：`/dm-mc02/adc/tim2-compare-pulse-only-cc1`
在 `dm-mc02-adc-test.c:1089` 观察到 `ADC1_ISR.EOC=1`。producer 是 qtest guest；它在
`system_reset` 后没有恢复此前为测试建立的 `RCC_D1CFGR=0x8` 和 `RCC_D2CFGR=0x440`，
因此 TIM2 使用复位时钟，compare pulse 早于预期边界产生。补回同一 fixture 后，独立
用例和 ADC 全量 `40/40` 通过；ADC/TIM 生产模型未被放宽。

随后按构建边界核对两个 profile。`tools/build-qemu.sh` 重新完成 Release
`arm-softmmu='dm-mc02'` 构建，生成配置含 `CONFIG_DM_MC02=y`、`CONFIG_STM32H723=y`
和 `CONFIG_STM32H723_USB_HOST=y`。新增 `tools/build-qemu-generic.sh` 固定使用独立
`build/qemu-generic` 和 `--without-default-devices`；generic 的
`arm-softmmu-config-devices.{mak,h}` 不含三个项目 feature symbol，构建目录没有对应
DM-MC02/H723/co-sim 对象，`nm` 也没有匹配项目专用符号。generic `-machine help` 只提供
QEMU 通用 ARM 机器，未把它误报为 DM-MC02 支持。

验证结果：Release ARM QEMU 构建通过；generic ARM QEMU 构建通过；串行 host CTest
`54/54` 通过，其中 QEMU smoke suite `86/86`。本轮没有修改 `trobot/`。

残余风险：generic profile 是构建隔离证据，不是通用 QEMU 性能或 DM-MC02 行为证据；
QEMU 上游 `vhost-shadow-virtqueue.c` 的既有 `r may be used uninitialized` warning 仍在，
不属于本轮修改。下一道迁移门仍是 W25Q64 die 接入 QEMU `m25p80`，必须先完成独立
adapter/differential gate 再接 board profile。
 
# 2026-09-01 STM32H723 timer VMState review

本轮组件级审查发现两个可在当前边界闭合的问题：序列化 scheduler 曾接受早于 phase
anchor 的 deadline，或接受与恢复后的活动输出通道不一致的 compare mask；成功回载也
没有通知负责重新计算 PWM/trigger 投影的板级 consumer。producer 是 timer 的寄存器和
活动调度状态，boundary 是 `dm_mc02_tim2_post_load()`，consumer 是
`dm_mc02_tim2_sync_runtime()` 与运行时 `changed` callback。

现在 post-load 在任何 timer/IRQ 副作用前调用 `dm_mc02_tim2_validate_vmstate()`，拒绝
deadline/anchor 倒序，并在 compare deadline armed 时校验每个 mask bit 对应活动输出
compare 且 CCR/ARR 可匹配。成功重建 runtime 后只调用一次 `changed` callback。新增隔离
回归覆盖这两个拒绝路径和通知，`test-dm-tim2-vmstate` 为 `5/5`；生产 QEMU 重链和
直接 `dm-mc02-tim2-test` 为 `22/22`。

审查指出的 Clock 风险仍有意保留为上层迁移门：absolute virtual nanosecond 字段仍要求
未来 machine-level 组合提供兼容的 Clock 和 virtual-time epoch。本组件不序列化 Clock、
QEMUTimer、callback 或 IRQ handle，当前不能宣称跨 epoch 或整机迁移。

# 2026-09-01 STM32H723 ADC dynamic VMState review

本轮首先把 ADC 的状态边界与运行时 wiring 分开。producer 是 CPU-visible ADC register
mirror、regular/injected rank 进度、校准/稳压器剩余状态、JAUTO/JQM 的 active/pending
JSQR context 以及 board/external channel source；boundary 是
`dm_mc02_adc_post_load()`；consumer 是 ADC 自有 virtual timers、level-sensitive IRQ 和
未来的 DMA/trigger/common-clock machine composition。

VMState 只保存会影响下一次 guest-visible conversion 的 producer state：regular 与
injected 的 rank cursor、剩余 half-cycle、phase timestamp、deadline、calibration
window/remaining cycles、regulator state、context snapshots、寄存器和 input override
maps。`MemoryRegion`、QEMUTimer、DMA/DMAMUX 指针、endpoint callback、IRQ/common-clock
callback、当前 `clock_hz` 以及 `power_model`/`accurate_timing` 是运行时 wiring 或策略，
不进入状态流。regular/calibration 共用 sample timer，injected 使用独立 timer；停止
kernel clock 时清除 deadline 但保留剩余进度，后续时钟恢复再继续。

首个错误边界在单测编译阶段，而非 ADC producer：测试缺少 `migration/savevm.h` 导致
`QEMU_VM_EOF` 未定义，并把 32-bit `CFGR.CONT` 写入单个 byte，造成 fixture 状态被截断。
已在测试层补齐头文件并按 little-endian 写入四个 byte；没有用 production fallback
掩盖该问题。

post-load 在任何 timer/IRQ 副作用前拒绝版本错误、截断/不完整动态状态、rank 越界、
寄存器 command 与 active state 不一致、非法 AUTDLY/DISCEN/JDISCEN/JAUTO 等待态、
active/pending JSQR 不一致、校准窗口/时间边界错误和 external override 位图不一致。
成功加载只调用 `dm_mc02_adc_sync_runtime()` 重建 ADC 自有 timer/IRQ，不调用 common-clock
callback，也不假定 DMA/trigger 连接已可由组件恢复。

验证证据：`test-dm-adc-vmstate` `3/3`；`dm-mc02-adc-test` `40/40`；ADC analog/input/
IRQ/DMA/power/trigger/JAUTO-DMA bare-metal smokes；`ninja -C build/qemu qemu-system-arm`；
QEMU smoke suite `89/89`；串行 Host CTest `54/54`。QEMU 重链仍报告既有上游
`vhost_svq_poll()` 的 `-Wmaybe-uninitialized` warning，与 ADC 改动无关。

残余风险：该描述仍未注册到 DM-MC02 machine，不能宣称整机 snapshot/migration；ADC2/
common shared state、模拟电气效应、深度 injected FIFO 和
timer/ADC/DMA 联合回载排序尚未覆盖。绝对 virtual timestamp 仍要求未来 machine-level
组合提供一致 Clock/epoch。`trobot/` 本轮未修改，未留下活跃子代理或测试进程。

# 2026-09-01 STM32H723 IWDG dynamic VMState review

本轮继续留在 STM32H723 芯片层，选择已有直接 guest consumer 的 IWDG 作为动态状态
producer。下一次寄存器/复位行为依赖寄存器镜像、写保护与启动状态、boot-grace 状态和
当前 watchdog 窗口的绝对虚拟 deadline；`QEMUTimer`、MemoryRegion、LSI 频率、宽限配置
和诊断计数属于运行时 wiring、配置或非行为性统计，未进入迁移流。

首个窄边界错误是 VMState 校验按字节数组读取 `uint32_t regs[]`，使合法 PR/RLR 状态被
误拒绝；已改为按寄存器槽读取，并由 `test-dm-iwdg-vmstate` 回归锁定。成功回载只在
所有字段通过校验后调用 `dm_mc02_iwdg_sync_runtime()`；started/deadline 不一致、PR/RLR
保留位、超出有符号 QEMUTimer 范围和截断流均不会产生 timer 副作用。

验证：`test-dm-iwdg-vmstate` TAP `5/5`、`tools/run-iwdg-smoke.sh`、
`ninja -C build/qemu qemu-system-arm` 均通过；QEMU 重链保留已有上游
`vhost_svq_poll()` `-Wmaybe-uninitialized` warning，与本切片无关。`trobot/` 未修改。

残余风险：IWDG 组件描述仍未注册到 DM-MC02 machine，不能宣称整机 snapshot/migration；
当前模型仍不表示 LSI 就绪/频率变化、SR 窗口更新时序或 watchdog reset reason。下一道
迁移门继续审计其它 H723 动态外设及联合 timer/clock/reset 顺序。

# 2026-09-01 ADC paused-progress VMState restore review

The first incorrect transition was in the ADC post-load consumer, not in the
serialized producer fields. When the ADC kernel clock stops, the model keeps
the current rank/calibration progress and clears its virtual deadline because
no QEMU timer may represent progress at 0 Hz. The existing restore path only
re-armed a non-zero deadline. Restoring that state into a composition whose
ADC clock was already valid therefore left `conversion_active`, injected
conversion, or `calibration_active` set with no timer: the guest-visible
operation silently stalled.

The fix keeps the boundary explicit. `dm_mc02_adc_sync_runtime()` now uses the
remaining half-cycles/cycles and the saved phase anchor to construct a new
deadline when the restored deadline is zero and the destination clock is
valid. It updates the active timing-clock marker at the same boundary. If the
destination clock is still stopped, it clears the timer and anchors the
paused operation at the current virtual time so a later clock callback does
not charge time spent while the clock was unavailable. Existing non-zero
deadlines still follow the absolute virtual-time contract.

The regression test `test-dm-adc-runtime-sync` performs an actual VMState
round-trip and advances the controlled virtual clock. It covers regular rank
EOC at 1667 ns, injected rank JEOC at 1667 ns, and calibration completion at
3334 ns after restoring a 1.5 MHz clock. The original `test-dm-adc-vmstate`
remains the malformed-state/post-load-side-effect gate.

The change is confined to the STM32H723 ADC component and its unit-test
fixture. It does not register machine-level migration, reconstruct DMA,
trigger or common-clock wiring, or claim cross-clock-epoch migration.

# 2026-09-01 STM32H723 ADC SQR2/SQR3/SQR4 review

The first incorrect boundary in the previous ADC implementation was the
regular sequence decoder: it read only SQR1 and silently collapsed any
configured sequence longer than four ranks into the legacy sample fixture.
The producer is the guest's SQR1/SQR2/SQR3/SQR4 MMIO programming; the boundary is
the rank-to-channel decoder; the consumer is the ADC conversion scheduler and
its DR/EOC/EOS data path. The fix stays in the ADC chip layer and does not add
board-specific channel rules.

The decoder now covers all H723 regular ranks 1–16 with the documented six-bit
field stride: SQR1 ranks 1–4, SQR2 ranks 5–9, SQR3 ranks 10–14 and SQR4 ranks
15–16. Overlapping full, half-word and byte writes select the register-backed
sequence, preserving the existing MMIO lane behavior.

The focused qtests use 14 and 16 configured ranks, exercise SQR2/SQR3/SQR4
lane writes, check every conversion result in order, and check the final
EOS/ADSTART transition. They pass as `/dm-mc02/adc/regular-sequence-sqr2-sqr3`
and `/dm-mc02/adc/regular-sequence-sqr4`; the full ADC qtest is `42/42`. ADC VMState and runtime-sync are `3/3` each, the QEMU
smoke suite is `89/89`, and serial host CTest is `54/54`. The initial manual
qtest invocation lacked `QTEST_QEMU_BINARY`; rerunning with the rebuilt
`build/qemu/qemu-system-arm` isolated that environment error from model
behavior.

This review does not widen the support claim to ADC2/common shared state,
complete analog/electrical behavior, or machine-level migration. The next gate
remains an isolated ADC chip-layer slice for ADC2/common shared state.
# 2026-09-02 STM32H723 ADC12 common register review

The first incorrect boundary was the old common-register representation: two
ADC instances existed alongside a third ADC-shaped common mirror, while the
machine also kept a duplicate CCR/configured state. The common producer was
therefore not a single owner, and the ADCx.CR offset collision left an obsolete
compatibility path in the ADC device. The correction keeps the common register
block as its own reusable component and removes that stale ADC-side path.

`DmMc02AdcCommon` owns the CPU-visible common registers and no board policy.
ADC1/ADC2 are status producers through callbacks; the board clock tree is a
consumer through the CCR callback. CSR is derived at read time, so status is
not copied into a second cache. CCR writes are lane-aware and mask reserved
bits before notifying the clock consumer. CDR/CDR2 are intentionally only
read-only staging values in this slice.

The isolated boundary test is `test-dm-adc-common` `4/4`: CCR mask and
full/half/byte accesses, callback notification, status projection, read-only
CDR/CDR2, reset wiring retention, and VMState round-trip/invalid/truncated
streams. The direct consumer `dm-mc02-adc-test` is `45/45`; the new cases cover
ADC2 independent conversion, high-byte/half-word CCR clock changes, CSR
projection for both ADCs and JQOVF. The QEMU system target was rebuilt after
removing the stale callback field.

Residual risk is deliberately local: CDR/CDR2 do not yet receive packed
dual-mode results; there is no synchronized dual ADC start, multimode DMA or
machine-level common/ADC migration composition. The component VMState excludes
callbacks, owner and `MemoryRegion`, and therefore cannot be used as a claim of
whole-machine snapshot support. No `trobot/` files were modified.

# 2026-09-02 ADC12 regular-simultaneous CDR boundary review

The first incorrect boundary in this slice was the common data consumer: ADC1
and ADC2 produced independent regular results, but the common register block
had no explicit pairing contract, so CDR could not represent a dual result.
The producer is the completed ADC regular rank; the boundary is the reusable
sample event and bounded matcher; the consumer is `ADC12_COMMON.CDR`.

The matcher now accepts only H723 regular-simultaneous `DUAL=0x6` and the two
implemented transfer formats. It compares sequence, zero-based rank and
virtual timestamp, is independent of source arrival order, and stores only one
pending event per ADC. Duplicate, mismatch and unsupported-mode outcomes are
explicit. DUAL/DAMDF changes clear stale pending state. DAMDF=2 packs the
master into CDR low half-word and slave into high half-word; DAMDF=3 packs the
low bytes. CDR2 is intentionally not consumed.

The common VMState now includes both pending slots because an unmatched event
changes the next observable CDR result. Post-load validation rejects invalid
sequence/rank/timestamp and unsupported pending modes, while callbacks and
MemoryRegion remain runtime wiring. This is a component contract only; it does
not register machine migration or imply external-trigger synchronized startup,
alternate CDR2
data, DMA, or complete multimode support.

Validation: `test-dm-adc-common` `7/7`, ADC VMState `3/3`, runtime-sync `3/3`,
and the serial full ADC qtest `47/47`. The direct CDR boundary is
`/dm-mc02/adc/common-cdr-regular-simultaneous`. During verification, the
activity clock-change test exposed a stale fixture oracle (1 MHz assumed versus
the model's 1.5 MHz reset-compatible clock); the test was corrected to 11
remaining half-cycles/1375 ns. No production timing workaround was added and
no `trobot/` file was modified.

# 2026-09-02 ADC12 software master/slave start review

The first incorrect transition in this slice was the start ownership boundary:
the direct CDR qtest still issued ADC1 and ADC2 software `ADSTART` independently
under `DUAL=0x6`. That was a stale consumer expectation, not a missing ADC
conversion result. RM0468 and the H723 HAL make ADC1 the regular-simultaneous
master; the normal sequence is to enable ADC2 and start ADC1 once.

The reusable common component now owns only the admission rule. Its master
request invokes a runtime peer callback supplied by the DM-MC02 composition;
the callback checks the public ADC2 enabled state and starts the peer through
the public start API. An ADC2-owned request is rejected and its `ADSTART` bit
is cleared. The callback is runtime wiring and remains connected across ADC
common reset. Independent DUAL modes keep their previous independent-start
behavior.

The isolated common test is `7/7`. The direct qtest
`/dm-mc02/adc/common-master-start-drives-slave` verifies HAL ordering, rejects
slave-only startup, and verifies the same master path after reset. The full ADC
qtest is `47/47`; the narrow QEMU target rebuild passed. The expected malformed
VMState negative case logs `Failed to load dm-mc02-adc-common:ccr` but returns
success after asserting rejection, so it is not a test failure.

The boundary deliberately covers software starts only. External trigger edges
still need a common master-only admission path, and exact timestamp matching
in the CDR matcher should later be replaced by a common-generated conversion
ID before multimode DMA is considered. No `trobot/` file was modified.

# 2026-09-02 ADC12 shared conversion ID pairing review

The first incorrect boundary was the CDR matcher key. The two ADC kernels are
independent virtual-time producers, so requiring equal timestamps made a
valid common conversion depend on scheduler ordering. The producer is the
common software-start admission; the reusable boundary is a non-zero
`conversion_id` carried by each regular sample; the consumer is the CDR pair
matcher. Timestamp remains diagnostic state, but is no longer an identity
field.

The common block allocates a non-zero monotonic ID for every accepted master
software start and passes it through `DmMc02AdcCommonRegularStartPeer`. The
DM-MC02 composition seeds both ADC kernels through the public setter before
starting ADC2. The kernels therefore publish the same ID for the first rank,
and continuous sequences advance from the same seed. Independent ADC and
external-trigger paths publish ID zero and are rejected by the common matcher.
The matcher keeps one pending slot per source and still compares ID plus
zero-based rank, retaining the bounded mismatch/duplicate policy.

Common VMState is now version 2 and saves `next_conversion_id`; ADC VMState is
version 2 and saves `regular_shared_conversion`. Version-1 streams remain
loadable without the new generator field; common post-load reconstructs its
frontier from legacy pending IDs so the next master start cannot reuse one.
Common reset clears the ID generator and pending pair while leaving runtime
callbacks connected.

Validation: `test-dm-adc-common` `8/8`, including explicit v1-stream loading;
`test-dm-adc-vmstate` `3/3`; direct ADC qtest `47/47`; and the narrow QEMU
rebuild including `qemu-system-arm`. The malformed-stream messages emitted by
the negative unit cases are expected and their processes exit successfully.

This remains a software-start/data-pairing slice. External-trigger ownership,
CDR2, multimode DMA, CDR read side effects and machine-level migration remain
unsupported. No `trobot/` file was modified.

# 2026-09-02 ADC12 regular-simultaneous external-trigger review

The first incorrect transition was trigger ownership: both ADC instances were
connected to the shared trigger bus and could independently consume one timer
edge. In regular-simultaneous mode that can produce two unrelated sequences,
or allow ADC2 to allocate a local identity that cannot be paired in CDR. The
producer is the trigger-bus event, the reusable boundary is common admission
plus the shared conversion ID, and the consumers are the ADC1/ADC2 regular
kernels and the CDR matcher.

The common component now admits only ADC1/master for `DUAL=0x6`, forwards the
source/edge/count/virtual timestamp to the board composition, and requires the
composition to start an enabled ADC2/slave through the explicit ID-bearing
peer API. ADC2's second bus delivery is rejected at its own common admission
call. Failed peer admission returns no usable ID to the producer; monotonic
generator advancement is intentional and bounded by integer wrap handling.
Non-simultaneous modes and injected triggers retain their local paths.

The local ADC consumer still owns register-side validation (`EXTSEL`, edge
selection, ADEN/ADSTART, calibration and busy state) and virtual scheduling.
The current event-count policy deliberately folds a coalesced batch into one
sequence; it is not an exact edge queue. This is recorded as an unsupported
precision boundary rather than hidden in the common matcher.

Validation: `test-dm-adc-common` `9/9`, ADC qtest `48/48`, QEMU smoke suite
`89/89`, and serial Host CTest `54/54`. The direct qtest
`/dm-mc02/adc/common-external-trigger-drives-slave` covers TIM8 trigger,
master/slave EOC and CDR, a master-only negative case, and reset. No
`trobot/` file was modified.

Residual risks remain local: CDR2, other dual modes, multimode DMA, CDR read
side effects, exact per-edge batching, and machine-level common/ADC/timer/DMA
migration are not implemented. The external peer callback is runtime wiring
and is intentionally excluded from component VMState.

# 2026-09-02 BMI088 SPI framing adapter VMState review

This slice remains at the reusable device/adapter layer. The producer is one
BMI088 SPI framer's in-flight command, direction, dummy phase and register
cursor; the boundary is `dm_mc02_bmi088_spi_vmstate()`; the consumers are the
SPI target callback and the BMI088 register/FIFO data path.

The adapter saves `command_seen`, `read_transfer`, `dummy_pending`, `reg` and
`read_start_reg` in VMState version 1. The BMI088 pointer, completion callback
and opaque are destination-owned runtime wiring. The sensor register image,
FIFO contents and cursor, sample sequence, signal noise/bias/random-walk/
temperature state are not copied into this adapter description. Keeping those
states separate prevents a component test from being mistaken for a complete
BMI088 migration contract.

The first invalid-state boundaries are local to the framer: a version other
than 1, cursor or mode bits without a command, and a dummy phase on a write
transaction are rejected before runtime consumers can run. The isolated
`test-dm-bmi088-spi-vmstate` passes `5/5`, including legal read/write
round-trips, runtime sentinel preservation and truncated-stream rejection.
The direct `run-bmi088-smoke.sh` and `run-cosim-link-smoke.sh` gates pass, and
the `qemu-system-arm` target was rebuilt successfully. Negative cases emit
expected QEMU malformed-stream diagnostics; the tests still exit successfully.

No `trobot/` file was modified, and the completed test subagent was reviewed
and closed. This is not machine-level snapshot/save-load or migration support.
The next lower-layer gate is a separate BMI088 sensor-state VMState contract
covering register/FIFO and signal-generation state, followed by explicit
SPI/GPIO/DMAMUX restore ordering.

# 2026-09-02 ADC12 regular-interleaved CDR2 review

This slice stayed at the reusable STM32H723 ADC/common layer. The first
evidence check used the local H723 CMSIS/HAL sources: `CDR2.RDATA_ALT` is the
regular data register for alternated master/slave ADCs, and the LL mode values
are `DUAL=0x7` for regular interleaved and `DUAL=0x3` for regular interleaved
plus injected simultaneous. No external source was available in the current
environment, so no broader timing claim was inferred.

The producer is the existing `DmMc02AdcRegularSample` callback and the
consumer is the common block's read-only `cdr2` mirror. For these two modes,
with `DAMDF=0`, each accepted rank writes a zero-extended 16-bit value and the
latest event wins. It bypasses the CDR pair matcher and does not call the CDR
DMA callback. Non-zero DAMDF is rejected before the value can overwrite CDR2,
avoiding a silent alias between CDR2 data and CDR DMA formatting.

The isolated gate `test-dm-adc-common` is `11/11`. The direct gate
`/dm-mc02/adc/common-cdr2-regular-interleaved` passes for both dual values,
successive rank overwrites, unchanged CDR and reset clearing. The qtest
initially assumed ADC2's first default sample differed from ADC1's; the first
failure showed both compact producers use the same default first rank. The
oracle was corrected to start ADC2 separately with a two-rank sequence and
assert the deterministic `0x0100 -> 0x0200` CDR2 transitions. No production
workaround was added.

Known limitations remain explicit: the compact model does not schedule the
exact hardware master/slave interleaved cadence or `CCR.DELAY`; CDR2 DMA,
other dual/DAMDF modes, CDR read side effects and machine-level migration are
not supported. `trobot/` was not modified.

# 2026-09-02 ADC12 regular-simultaneous multimode DMA review

本切片继续停留在 STM32H723 ADC/common/DMA 芯片层及其 DM-MC02 直接组合边界。首个失败
状态不是 DMA 搬运，而是新增 qtest 的 producer fixture：它没有写入
`ADC12_COMMON.CCR` 的 `DUAL=0x6/DAMDF=0x2`，所以 ADC1/ADC2 按独立模式运行，ADC2
没有提交可配对的 common sample，最终表现为 `CDR=0`、DMA `NDTR` 不变。补上 HAL 等价
的 common 配置后，第二个测试预期错误才暴露：两个 legacy regular rank 应产生连续的
`0x01000100` 和 `0x02000200`，并非重复的第一 rank；该错误同样属于 consumer oracle，
没有修改生产时序来迎合测试。

生产边界已收敛为：ADC common 的 producer 回调只在完整 shared-ID/rank pair 形成后给出
packed CDR word 和虚拟时间戳；DM-MC02 consumer 检查 ADC1 regular DMA 是否启用，然后通过
DMA1 request 9 走 endpoint 或 CDR MMIO 路径。DMA request cache 的另一个真实根因是
request ID 不是唯一 peripheral identity：ADC1 `DR` 与 common `CDR` 可共享 request 9。
缓存现按 `(request_id, peripheral_addr)` 建立，避免一个 endpoint 的负查找结果抑制另一
个 endpoint。

当前准确支持 `DUAL=0x6`、`DAMDF=0x2/0x3` 的 regular CDR DMA，每个 pair 一个 DMA beat；
endpoint/MMIO 两条路径共用 DMA 的 NDTR、stream arbitration 和 IRQ 语义。隔离
`test-dm-adc-common` `10/10`、ADC qtest `51/51`、QEMU smoke `89/89`、Host CTest
`54/54` 和 `qemu-system-arm` 重链均通过。未修改 `trobot/`。

残余风险仍明确限定为 CDR2、其它 dual/DAMDF 模式、CDR 读取确认副作用、精确物理 DMA
仲裁/传输时序，以及 common/ADC/DMA machine-level migration；这些不能由本切片的 qtest
通过推出支持结论。

# 2026-09-02 ADC12 CDR read acknowledgement review

本轮的首个错误状态位于 endpoint DMA 到 common CDR 的边界：MMIO DMA 会经过
`DmMc02AdcCommon` 的 `MemoryRegion` read callback，自然触发 CDR 读取确认；endpoint DMA
直接从已打包的 producer value 取数，绕过该 callback，因而成功搬运后 ADC1/ADC2 的 EOC
仍保持置位，`AUTDLY` 也不会继续。producer 是 ADC12 common 的 CDR packed-result，
boundary 是 CDR read notification，consumer 是 ADC1/ADC2 regular data-consumption path。

修复将确认语义收敛为一个可复用的 runtime wiring 接口：common 新增
`DmMc02AdcCommonCdrRead`、`dm_mc02_adc_common_set_cdr_read_callback()` 和
`dm_mc02_adc_common_notify_cdr_read()`。支持的 `DUAL=0x6` 且 `DAMDF=0x2/0x3` 下，
任何合法 CDR 重叠 MMIO 读在组装返回值后发出通知；DM-MC02 endpoint consumer 仅在 DMA
成功后显式发出同一通知。board callback 通过公开的
`dm_mc02_adc_acknowledge_regular_data()` 同时清除两个 EOC，并复用既有 ADC data
consumption continuation，因此 `AUTDLY` 的释放点与真实 CDR 消费一致。CDR2、未支持的
CDR 格式和其它 dual/DAMDF 模式不会误触发该副作用；callback 及其 opaque 是运行时 wiring，
reset 保留且不进入 common VMState。

依据是已保存的 ST RM0468 Rev.3（`/tmp/RM0468.pdf`，ADC dual-mode 章节 28.4.32）：
regular simultaneous 的 CDR 读取/DMA 消费同时确认 master/slave EOC，并在 AUTDLY 下
允许后续转换。该资料依据只支持上述已建模格式，不外推其它 H723 dual mode 或物理 DMA
时序。

变更文件为 `qemu/upstream/hw/arm/dm_mc02_adc_common.[ch]`、
`qemu/upstream/hw/arm/dm_mc02_adc.[ch]`、`qemu/upstream/hw/arm/dm_mc02.c` 和对应
ADC common unit/qtest。隔离 `test-dm-adc-common` 为 `12/12`；直接
`/dm-mc02/adc/common-cdr-read-acknowledges-both-eoc`、endpoint DMA、MMIO DMA 均通过；
完整 `dm-mc02-adc-test` 为 `53/53`；串行 Host CTest 为 `54/54`（含 QEMU smoke
suite）；`ninja -C build/qemu
tests/unit/test-dm-adc-common tests/qtest/dm-mc02-adc-test qemu-system-arm` 通过，
`qemu-system-arm --version` 为 8.2.2。单测输出的 `Failed to load dm-mc02-adc-common:ccr`
来自预期的截断/损坏流负例，测试仍以成功状态退出。

本切片不注册 machine-level VMState，不能宣称整机 snapshot/migration；不支持 CDR2 DMA、
其它 dual/DAMDF 的读取确认、精确物理 DMA 仲裁/传输时序。下一道门继续停留在 ADC 芯片
层，先建立 interleaved master/slave cadence 与 `CCR.DELAY` 的精确边界测试。`trobot/`
未修改，当前无活跃子代理或残留 QEMU/构建进程。

# 2026-09-02 ADC12 regular-interleaved cadence review

本轮完成了上一轮留下的 ADC 芯片层下一道门：interleaved master/slave cadence 与
`CCR.DELAY`。首个边界仍由 common admission 负责，而不是由板级代码直接操作 ADC
私有状态。对于 `DUAL=0x7` 和 `DUAL=0x3`，ADC1 是唯一 regular master；ADC2 必须已
使能，并由 DM-MC02 composition 通过公开的绝对虚拟时间 API 启动。ADC2 自己写
`ADSTART` 或再次消费同一 regular trigger 不会形成第二个独立 common sequence。

时间语义收敛为：slave sampling start = master start + master sampling phase +
`CCR.DELAY` phase。`CCR.DELAY` 使用 RM0468 Table 236 的 16/14/12/10-bit 表，按有效
ADC kernel clock 转换为整数虚拟纳秒并向上取整；rank 完成仍由 ADC kernel 按自身采样时间、
17 个 conversion half-cycles 和当前时钟调度。新增的
`dm_mc02_adc_start_regular_at()`、`dm_mc02_adc_external_trigger_with_id_at()` 和
`dm_mc02_adc_interleaved_slave_start_ns()` 是板卡无关的组合接口；common 只暴露
`get_dual()`、`get_delay_code()` 和 interleaved 判定，不复制时钟/采样计算。

`DUAL=0x3` 的 injected simultaneous 仍走共享 trigger bus，并验证两个 ADC 在同一虚拟
触发下同时完成；本轮没有把 injected result packing 静默扩展到 common CDR。停钟时以
`UINT64_MAX` 表示无法解析 phase，composition 将 peer 锚定在当前 epoch，ADC 自己的
clock callback 负责保持暂停，避免 wall-clock 或 board fallback 改变 guest 时序。

验证：隔离 `test-dm-adc-common` `12/12`；直接 `dm-mc02-adc-test` `55/55`，覆盖软件
master-only 启动、TIM8 regular 外部触发、`DELAY=0/2`、默认分辨率及 `DUAL=0x3`
injected simultaneous；QEMU 增量 system target 构建通过。`trobot/` 未修改，也没有
活跃子代理或残留构建进程。

残余风险：精确 event-count 队列、CDR2 DMA、其它 dual/DAMDF 模式、injected common data
packing、物理 DMA 仲裁/传输时序和 common/ADC/timer/DMA machine-level migration 仍未
实现。当前实现验证的是可复现的虚拟时序契约，不代表完整 H723 模拟或真实 DMA 电气行为。

# 2026-09-02 ADC12 regular-interleaved DAMDF=3 8-bit packing review

本轮仍属于 STM32H723 ADC12 common 芯片层到 DM-MC02 DMA 的直接边界，没有向板级
外设、外部仿真器、Web/UI 或 `trobot/` 扩展。producer 是 ADC1/ADC2 已完成的
regular-rank 事件；boundary 是 common 的 DAMDF=3 固定容量累积器；consumer 是
ADC1 request 9 的 DMA endpoint 或 CDR `MemoryRegion` 读取路径。

首个错误状态发生在 interleaved DAMDF=3 的 producer/boundary 交界：原有 CDR2
路径只覆盖 DAMDF=0/2，而 CDR 的 DAMDF=3 路径没有表达四个交替来源组成一个 beat
的契约，因而无法同时保证 partial word 不产生 request、来源顺序和最终 CDR 内容。
修复后 common 只接受 `master, slave, master, slave`，分别写入 CDR 的四个 byte
lane；来源次序错误会清空 partial state 并返回 `MISMATCH`，不合成数据。

第二个错误是时间状态的 producer 归属：pair matcher 在对端先到时曾使用当前回调的
timestamp，而不是保存于 pending slot 的 peer timestamp；DAMDF=3 的 partial
accumulator 还需要跨 pair 保留已见事件的时间。现在每个 pair 取两路 timestamp
最大值，四值 word 再取所有 pair 最大值，并把该 aggregate timestamp 纳入 common
VMState v5。v1–v4 回载会显式清理不存在的新字段，避免复用 destination state。

验证时还修正了 consumer fixture 的 oracle，而不是改生产模型：24 V VIN 经 11:1
分压后 ADC1 低字节为 `0x41`，ADC2 fallback 为 `0x00`，所以完整 CDR 是
`0x00410041`。测试覆盖了非递增 producer timestamp，证明 data-ready 时间不会因
回调顺序回退。

验证结果：隔离 `test-dm-adc-common` `14/14`；直接 ADC qtest `59/59`，包含
endpoint/MMIO DAMDF=3 路径；QEMU smoke suite `89/89`；Host CTest `54/54`；
`qemu-system-arm` 增量重链通过。`git diff --check` 通过，`trobot/` 无修改，且
没有残留 QEMU、ninja 或 ctest 进程。

残余风险保持明确：CDR2 在 DAMDF=3 下不启用；其它 dual/DAMDF、精确物理 DMA
仲裁/传输时序、overrun 停止请求、injected common-data packing，以及
common/ADC/DMA 联合 machine-level migration 仍未支持。这一切片只证明固定
interleaved CDR packing 与其 DMA 边界，不代表完整 H723 multimode 行为。

# 2026-09-02 ADC regular DMA overrun request gate review

本轮完成 STM32H723 ADC regular data/DMA 边界，未向 DM-MC02 板级、外部仿真器、Web/UI
或 `trobot/` 扩展。producer 是 `adc_emit_sample()` 的 regular rank completion；boundary
是 ADC-local DMA request；consumer 是现有 ADC1/ADC2 DMA request 的 endpoint 或 MMIO
路径。

首个错误状态是生产代码在检测到旧 `EOC` 并置位 `OVR` 后仍继续发出 ADC-local DMA
request，违反 RM0468 的“OVR 清除前停止 DMA request”语义。修复只保存进入函数时的
`OVR` 布尔值，并将其与当前 `EOC` overrun 一起作为 request gate；没有新增 DMA 私有
状态，也没有把这条 gate 传播到 common CDR/CDR2 callback。后者是必要边界：DAMDF=3
regular-interleaved 可能在四值累积期间保持 ADC `EOC`，common CDR 仍须完成自己的
data-ready 契约。

`CFGR.OVRMOD` 仍只控制 `ADC_DR` 的保留/覆盖，`ADC_DR` 读取只清 `EOC`，`OVR` 仍由
ISR W1C 清除。新增直接 qtest
`/dm-mc02/adc/regular-dma-overrun-request-gate` 使用四个 fresh machine case，交叉
覆盖 endpoint/MMIO 和 `OVRMOD=0/1`：DMA stream 先保持 disabled 以产生首个 `EOC` 和
第二个 rank 的 `OVR`，检查数据寄存器策略及 `NDTR` 不变；随后在 OVR 仍置位时启用
stream，确认 request 继续被抑制；最后 W1C 清 OVR 并通过正常 DR consumer 清 EOC，
确认下一 rank 搬运成功、`NDTR=0` 和 TCIF 置位。

验证结果：`ninja -C build/qemu qemu-system-arm tests/qtest/dm-mc02-adc-test` 通过；
新增窄测通过；完整 `dm-mc02-adc-test` 为 `60/60`。本轮暂未宣称 common CDR/CDR2
与 ADC OVR 联动、其它 dual/DAMDF、精确物理 DMA 仲裁/传输时序或 machine-level
migration。`trobot/` 未修改，当前没有活跃子代理或残留构建进程。
# 2026-09-02 STM32H723 SPI component VMState review

本轮继续留在 STM32H723 芯片/可复用 SPI 数据层。producer 是 SPI CPU-visible
配置寄存器、当前传输进度和 RX 单槽；boundary 是
`dm_mc02_spi_vmstate()`；consumer 是未来的 machine-level migration 组合以及
SPI 自身可选的 TX-DMA virtual timer。

审计确认 SPI 的异步热路径只有一个需要保留的绝对虚拟 deadline：DMA TX stream
仍有 NDTR 时，SPI 用 `dma_tx_next_ns` 表示下一次有界续传。原实现只把 deadline
藏在 `QEMUTimer` 中，组件恢复会丢掉尚未执行的 TX continuation；修复在 SPI
producer/boundary，增加显式 deadline，并将所有延期调度收敛到同一 helper。恢复时
先校验 transfer count 和 deadline，再由 `dm_mc02_spi_sync_runtime()` 清理递归
request guard、取消目标 timer 并按目标 `QEMU_CLOCK_VIRTUAL` 重挂 deadline。

target callback、DMA/DMAMUX 指针、endpoint callback、QEMUTimer、GPIO-derived
selected mask、endpoint/batch 配置均保持 runtime wiring/config，不被错误写入组件
状态。片选由未来 machine 组合在 GPIO 回载后重新投影；本切片未提前引入 machine
级迁移或板级 workaround。

验证：隔离 `test-dm-spi-vmstate` `4/4`；直接 `run-bmi088-smoke.sh`、
`run-spi2-dma-smoke.sh on` 和 `off` 均通过；`qemu-system-arm` 重链通过。本轮
未修改 `trobot/`，没有启用子代理。

残余风险：SPI target（BMI088）内部状态、DMA/DMAMUX 联合回载顺序、GPIO 片选回载
顺序、SPI 真实 bit-level/electrical timing 和 machine-level VMState 仍未覆盖。
下一步继续审计另一个明确的 H723 动态外设边界，先做组件隔离和直接消费者门。

# 2026-09-02 STM32H723 USART component VMState review

本轮继续留在 STM32H723 可复用 USART 数据层。producer 是寄存器镜像、RX wire/CPU
FIFO、TX FIFO 和计数器；boundary 是 `dm_mc02_uart_vmstate()`；consumer 是 UART 自己的
虚拟时间调度器和未来 machine-level migration 组合。子代理只生成了隔离测试文件，主代理
复核后关闭其会话，未让子代理长期占用资源。

审计确认 USART 的异步状态不能只保存寄存器：未完成的 RX wire byte、paced host TX、
IDLE indication 和 DMA-TX bounded retry 都由 QEMUTimer 驱动。首个风险边界是 timer
对象本身属于 destination runtime wiring；若只恢复 FIFO 而丢失 timer deadline，恢复后
会丢字节或改变 IDLE/TX 顺序。修复将四个绝对 `QEMU_CLOCK_VIRTUAL` deadline 纳入组件
状态，并在 `dm_mc02_uart_sync_runtime()` 中先取消目标 timer、重算 baud timing，再按
保存 deadline 重挂；已经过期的 deadline 在当前虚拟时刻继续执行。

状态边界同时排除了 chardev、Clock、DMA/DMAMUX、IRQ、QEMUTimer、endpoint callback、
RS485/DE/供电、endpoint mode、DMA started marker 和派生 timing。post-load 先检查 FIFO
head/length、deadline 的 signed-clock 范围，以及 RX delivery/IDLE/TX deadline 与队列状态
的组合；非法状态在 runtime sync 前拒绝，避免把坏流传播到 wiring 或 timer。

验证：`test-dm-uart-vmstate` `5/5`；UART polling、IDLE、TX/RX virtual-time、USART1/2
DMA endpoint/MMIO on/off 直接门共 `8/8`；`qemu-system-arm` 增量重链通过；`git diff --check`
通过。本轮只修改 UART/QEMU 构建登记、隔离测试和项目记录，未修改 `trobot/`，且已关闭
完成的子代理。

残余风险：这是 component contract，不是整机 snapshot/migration；chardev reconnect、
慢后端 partial write、UART/DMA/DMAMUX 联合回载顺序、真实串行位级/电气时序和完整 H723
USART mode matrix 仍未覆盖。下一道门继续留在芯片层，先选择一个独立动态外设状态边界。

# 2026-09-02 BMI088 sensor component VMState review

本轮继续留在可复用器件层，producer 是 BMI088 动态寄存器/采样状态，boundary 是
`dm_mc02_bmi088_vmstate()`，consumer 是 BMI088 SPI target 数据面和未来的 machine-level
迁移组合。首个测试错误发生在测试 consumer：目的端把被排除的 `accel`/`signal.kind`
清零，post-load 正确拒绝了身份不匹配；修复测试初始化后，未改动生产行为。

VMState v1 覆盖 256 字节寄存器、信号参数与历史、RNG/ODR 时间、完整 FIFO 和 sequence
数组、FIFO 读帧游标、sensor-time/overrun、sample sequence 及陀螺 DRDY deadline。
`double` 使用固定大端 IEEE-754 bit pattern，避免主机 ABI 表示差异。`accel` die identity
和 `signal.kind` 是目的端静态配置，明确不进入状态流；post-load 在 runtime wiring 前
检查 identity、数值有限性以及 FIFO/frame 不变量。

验证：`test-dm-bmi088-vmstate` `5/5`；`run-bmi088-smoke.sh`、
`run-bmi088-fifo-smoke.sh`、`run-bmi088-drift-smoke.sh`、`run-bmi088-filter-smoke.sh`
均通过；`ninja -C build/qemu qemu-system-arm` 通过。本轮未修改 `trobot/`，已复核并关闭
Maxwell 子代理。

残余风险：该 VMState 未注册到 DM-MC02 machine，不能宣称整机 snapshot/migration；SPI
framer、GPIO 片选、DMA/DMAMUX 与 sensor 的联合回载顺序，以及真实 SPI 位级/电气时序，
仍未验证。下一道门是 joint SPI target/传感器状态边界，仍需先做隔离和直接消费者测试。

# 2026-09-02 BMI088 SPI link composite review

本轮进入可复用 SPI/BMI088 组合边界。首个错误状态是恢复顺序而不是传感器数据：SPI
普通片选投影会调用 BMI088 target 的 `select` callback，而 callback 会结束当前读事务并
清零 framer。如果在传感器/framer 状态加载前或加载期间投影 `selected_mask`，保存的半帧
会被静默破坏。

修复位于 SPI/组合层：`dm_mc02_spi_vmstate_raw()` 保留原始字段但不执行 scheduler
`post_load`；`DmMc02Bmi088SpiLink` 的 `pre_save` 保存 GPIO 派生片选快照，child 状态
验证成功后使用无回调路径，最后才调用 `dm_mc02_spi_sync_runtime()` 激活 DMA
continuation。普通运行时 GPIO 转换仍使用正常 callback，因此不会改变 live transaction
的结束语义。

第二个边界是静态身份：单颗 BMI088 的 VMState 不保存 `accel` 和 `signal.kind`，单颗
validator 只能判断目的端配置自洽，不能知道它是否被放入错误的组合槽位。组合 post-load
现在额外检查固定 accel/gyro slot、signal kind 以及两个 framer 指向组合内对应 die；不
通过时不恢复片选、不同步 timer，也不调用 target callback。

验证：`test-dm-bmi088-spi-link-vmstate` `4/4`；SPI/BMI088 既有 VMState `4/4`、`5/5`、
`5/5`；BMI088 polled/FIFO/drift/filter smoke、SPI2 DMA endpoint on/off、
`qemu-system-arm` 重链均通过。machine runtime 已改用 link 对象并保留既有直接门行为。

残余风险：组合 VMState 尚未注册到 DM-MC02 machine，不能宣称整机迁移；GPIO 输入、
DMA/DMAMUX、CPU/IRQ、片上 RAM、co-sim 队列的联合恢复仍未覆盖，真实 SPI 位级/电气
时序亦未建模。新增 link core 的状态初始化/正常片选已由 machine 直接门覆盖，但无独立
link-core unit；下一道门应先补齐相邻状态边界再考虑 machine-level 注册。

# 2026-09-02 STM32H723 DMA/DMAMUX subsystem state review

本轮继续留在 STM32H723 芯片层。首个失败状态发生在测试 target 链接：组合测试 target
误把既有 `test-dm-dma-vmstate.c` 一并编译，导致两个 `main` 重定义；移除重复测试源后，
生产 DMA/VMState 代码无需为此修改。

审计确认 DMA 与 DMAMUX 的单组件 post-load 不能证明联合恢复顺序：DMA 的 request cache
依赖 DMAMUX `generation` 和寄存器，且 cache、IRQ handles、channel offset 与 endpoint
均是目的端 runtime wiring。新增 `DmMc02DmaSubsystem` 使用 DMAMUX-first 的组合布局，
raw DMA child 不执行独立 sync；父级在全部字段装载后复用同一 FIFO/count 校验，再重建两路
cache 和 level-sensitive IRQ projection，避免部分状态污染运行时连接。

变更文件：`hw/arm/dm_mc02_dma.h`、`dm_mc02_dma.c`、`dm_mc02_dma_vmstate.c`、
`dm_mc02_dmamux_vmstate.c`、新增 `dm_mc02_dma_subsystem_vmstate.c`、构建清单及
`test-dm-dma-subsystem-vmstate.c`。验证：组合单测 `4/4`，既有 DMA VMState `3/3`，
DMA memory/arbitration/batch 三个直接 smoke 和 `qemu-system-arm` 重链通过，
`git diff --check` 通过；本轮未修改 `trobot/`，代理已复核并关闭。

残余风险：组合仍未注册到 DM-MC02 machine，不能宣称整机 snapshot/migration；endpoint
外部状态、DMA 与 SPI/UART/ADC scheduler、CPU/IRQ、RAM、co-sim 队列和真实 DMA 总线
仲裁/传输时序仍需后续独立边界。下一道门是继续建立这些下层组合契约，不用 machine/UI
workaround 掩盖未完成的恢复顺序。

# 2026-09-02 DMA endpoint non-fatal backpressure review

本轮所属层为可复用 STM32H723 DMA 芯片层到板卡无关 endpoint 边界。首个错误状态是
endpoint callback 的 `false` 没有区分“暂时背压”和“永久错误”：DMA direct/FIFO
路径会立即置 TEIF、关闭 stream，batch 路径还可能继续尝试后续 item。这会把合法的
外部设备容量不足传播成 DMA 配置故障。

修复集中在 endpoint boundary，而不是 ADC/SPI/UART 等 consumer：新增可选
`read_ex`/`write_ex` 结果回调和 `DmMc02DmaEndpointResult`。`RETRY` 在四条 DMA
endpoint transfer path（direct/FIFO、P2M/M2P）都在任何 live state commit 前返回；
FIFO 已预取字节也保留。`ERROR` 继续使用原 TEIF/停 stream 语义，legacy bool callback
的 `false` 映射为 `ERROR`。batch 在第一个无进展结果处停止，避免对背压 endpoint
进行虚拟时间忙循环。

隔离与直接芯片层证据：`dm_mc02_dma_endpoint_smoke` 验证旧 callback 兼容和
`RETRY -> ACCEPTED` 结果序列；`dm_mc02_dma_fifo_dbm_endpoint_smoke` 验证 direct/FIFO
的 M2P 与 P2M retry、已有 FIFO partial bytes、guest memory 不变性、bounded batch 的
`NDTR`、配置寄存器/live cursor、FIFO、EN/TC/TEIF 状态。DMA/DMAMUX VMState 单测、
`qemu-system-arm` 重链、QEMU smoke suite `89/89` 和 Host CTest `54/54` 均通过。

已知限制：`RETRY` 仍是调用方驱动的同步结果，不拥有 timer、worker 或持久化队列；在这次
历史背压审查时，ADC/SPI/OCTOSPI 以及 UART RX 生产 endpoint 仍使用同步 legacy callback
（历史状态）；后续 OCTOSPI、UART RX、SPI RX 和 ADC1 已各自获得 direct P2M reservation，UART TX 已建立独立的
virtual-time retry owner。若未来加入
真正异步 endpoint，必须在更高层定义 virtual-time retry owner、取消/生命周期和
consumer transaction rollback；旧 P2M callback 已消费数据后发生内存写失败时，当前
边界不能回滚该外部副作用。该切片没有注册 machine-level migration，也未修改
`trobot/`。

# 2026-09-02 DWC2 power-on TX FIFO and Release RTF gate review

本轮的首个错误状态位于 USB 芯片层到 guest 的 MMIO 边界：DM-MC02 复位时只给旧的
compatibility register array 写入 `DIEPTXF[0..14]`，而 guest 通过 DWC2 MMIO 读取这些
地址时得到 0。CherryUSB 的 `usb_dc_init -> dwc2_set_txfifo()` 因上电 FIFO depth 为 0
进入断言死循环；约 2.048 s 后 IWDG 复位。这不是性能采集器误判，也不是应该通过
watchdog grace 掩盖的固件问题。

修复收敛在可复用 `DmUsbDwc2Device`：增加 `dieptxf[15]` 状态，复位时提供非零上电
FIFO depth，并在 `0x100..0x13c` 实现寄存器读写；guest 后续动态配置可读回。新增的
`/dm-mc02/usb/power-on-tx-fifo-registers` 与既有 USB 数据面共同通过 `10/10`。构建时
暴露的 `ARRAY_SIZE` 隐式声明/链接错误也已改为显式 `DM_USB_DWC2_DIEPTXF_COUNT`，避免
依赖未包含的宏。

Release 性能 gate 采用 `trobot/build/Release-current/trobot.elf`，从 reset 开始，等待
`xTickCount >= 250` 的 startup-ready 点，以 60 个虚拟秒的 FreeRTOS tick 结束采样，
重复三次并读取 IWDG QOM diagnostics。三次结果：startup latency
`0.523267..0.523908 s`，RTF `0.999999..1.000012x`，CPU `115.65..115.69%`，RSS
`50384..50632 KiB`，IWDG `starts=1,reloads≈12061,timeouts=0`。由于 QMP/wall-clock
采样存在微小抖动，标准脚本以 `0.999x` 判定容差保留精确 RTF 输出；这不代表把明显低于
1x 的运行视为通过。

残余风险：DWC2 仍未实现真实 USB bus attachment、SOF/PHY/USB DMA 和宿主机枚举；RTF
指标是 Release 固件、默认 DM-MC02 profile 的单机基线，不能外推到 Gazebo/MuJoCo、调试
或非默认固件配置。`trobot/` 本轮无改动。
# 2026-09-02 DWC2 FIFO query boundary and atomic invalid MMIO review

本轮按“producer -> boundary -> consumer”定位了一个真正的底层边界错误。producer 是
`DmUsbDwc2Device` 端点 FIFO，consumer 是 DM-MC02 USB adapter 的 test/status/packet
路径。原 adapter 直接读取 `s->dwc2.endpoint[ep].in_fifo_count` 和
`out_fifo_count`，把可复用 core 的私有布局泄漏到了板级层。

修复在 DWC2 core 增加公开的
`dm_usb_dwc2_endpoint_fifo_count(const DmUsbDwc2Device *, unsigned, bool, size_t *)`。
它只读、无破坏性，拒绝空设备、越界端点和空输出指针；adapter 的三个读取点已全部改用
该接口。隔离单测新增有效 IN/OUT 查询、重复查询无副作用和非法参数断言。

测试增强同时暴露第二个首个错误：qtest 对 `DIEPTXF0 + 2` 的 32-bit 访问在
`MemoryRegionOps.impl.unaligned=false` 下被 AddressSpace 拆成两个 16-bit 操作，第二个
操作落入 `DIEPTXF1`，导致跨寄存器写入产生 `0x5566ffff`。这不是 core 的
`dieptxf_access_valid()` 失效，而是 board MMIO transport 在调用 core 前改变了事务边界。
修复把该 region 的 `.impl.unaligned` 设为 `true`，保留 `.valid.unaligned=false`，于是整笔
非对齐事务由 valid 层拒绝，不会有部分副作用。这个规则只作用于 DM-MC02 MMIO，不改变
core 直接 API 的寄存器语义。

验证结果：DWC2 unit `7/7`，USB qtest `10/10`，`ninja -C build/qemu ... qemu-system-arm`
重链通过。测试覆盖的是公共查询和 DM-MC02 这组 MMIO 边界；仍不能据此宣称真实 USB
FIFO 仲裁、DMA、PHY、SOF、宿主机枚举或 machine-level migration 已支持。

残余风险：DWC2 的 FIFO 是有界软件队列，未建模硬件 FIFO 分区/仲裁；DM-MC02 的 legacy
FIFO0 仍由 adapter 自己拥有。下一道门应继续在 transport adapter 到 QEMU `USBBus` 的
边界定义接口和独立测试，不要用当前 test harness 代替宿主机 USB 总线。

# 2026-09-02 DWC2 device-mode component VMState review

本轮继续留在可复用 USB 芯片/器件层，未向 DM-MC02 machine 注册整机迁移。producer 是
`DmUsbDwc2Device` 的 guest-visible DWC2 寄存器、`DIEPTXF`、端点 FIFO/游标、transfer
counter、DMA 地址、端点 transaction 配置/PID/halt 和 overflow 计数；boundary 是
`dm_usb_dwc2_vmstate()`；consumer 是未来的组合快照恢复流程。

审查重点发现两个格式风险并在 producer/boundary 修复：自定义 `size_t` 编解码器原先用
本机 `sizeof(size_t)` 拒绝字段，和固定 64-bit wire 设计矛盾；枚举解码原先按 4 字节
`memcpy` 写入目的对象，依赖枚举底层宽度。现在 callback 完整拥有格式，游标固定读写
64-bit big-endian，PID/type 固定读写 32-bit big-endian，并通过类型赋值解码；非法 PID
仍在 boundary 被拒绝。

恢复顺序为：普通字段装载 -> EP0/FIFO/端点 type/PID 校验 -> 重绑 transaction callback
和 opaque -> 根据 masks/status/endpoint interrupt 重投影 IRQ。control device、IRQ
callback/opaque/level、QOM ownership 和 `MemoryRegion` 是目的端 wiring，不进入状态流。
`dm_usb_dwc2_sync_runtime()` 会先把目的端 asserted IRQ 撤销，再投影恢复后的 level；失败
或不支持版本在校验完成前返回，不调用 sync。这里的“失败不变”仅针对 runtime wiring，
VMState 仍可能在错误返回前写入普通 state 字段，不提供事务级 rollback。

变更文件：`hw/usb/dm_usb_dwc2_device.h`、`dm_usb_dwc2_device.c`、
`hw/usb/dm_usb_dwc2_device_vmstate.c`、`tests/unit/test-dm-usb-dwc2-vmstate.c` 及
USB/unit 构建清单；未修改 `trobot/`。

验证：`test-dm-usb-dwc2-vmstate` `4/4`；DWC2 core `7/7`；QEMU USB adapter `13/13`；
DM-MC02 USB qtest `10/10`；`qemu-system-arm` 重链通过。qtest 使用
`QTEST_QEMU_BINARY=build/qemu/qemu-system-arm` 启动真实 DM-MC02 QEMU。非法 PID/版本拒绝
会输出预期 VMState 错误日志，但测试成功。

残余风险：该描述未保存 `DmUsbControlDevice`、CPU/IRQ/NVIC、RAM、DMA/DMAMUX、USB PHY/
SOF、QEMU bus topology、异步 transaction 或 co-sim 队列，也未验证跨这些组件的恢复顺序；
因此不能宣称 DM-MC02 machine-level snapshot/migration。下一道门仍是先定义 USB transport
adapter 与 QEMU `USBBus`/控制器的联合边界，再决定是否具备进入 machine composition 的条件。

# 2026-09-02 QEMU USB transport binding review

本轮所属层是可复用 USB host transport 到 QEMU 标准 USB bus 的边界。首个确定问题是
transport 仅保存裸 `USBDevice *`/`USBPort *`，没有明确所有权和 teardown 顺序；空
transaction 还会在 transport 内部解引用，异步/排队结果也被统一折叠为 `INVALID`。
这会把 bus/port 释放后的回调风险和同步栈 packet 的 buffer 生命周期隐藏起来。

修复集中在 transport/port boundary：direct device 与 routed port 现在互斥，null、错误
binding kind 和 cleared binding 被拒绝；新增 `clear()`，host-channel QEMU composition
在释放 port/bus 前先断开 route callback 并清除借用指针；port cleanup 后清空 host/bus
指针，reset 对未注册/无设备状态安全返回。QEMU `NODEV`、`BABBLE`、`IOERROR` 和
`ASYNC/ADD_TO_QUEUE` 分别映射到 `NO_DEVICE`、`BABBLE`、`IO_ERROR`、`DEFERRED`，其中
deferred packet 在返回前取消，确保 caller-owned buffer 不会逃逸。

变更文件：`hw/usb/dm_usb_transaction.h`、`dm_usb_host_qemu_transport.[ch]`、
`dm_usb_host_qemu_port.c`、`dm_stm32h7_otg_host_qemu.c`、
`tests/unit/test-dm-usb-qemu-adapter.c` 及架构/接口/计划文档；未修改 `trobot/`。
验证：`test-dm-usb-qemu-adapter` `15/15`，`ninja -C build/qemu qemu-system-arm` 通过。

残余风险：QEMU `USBDeviceClass.handle_control` 是 request-level callback，不提供每个
status packet 的回调；因此本轮没有把 device-side `dm_usb_qemu_adapter` 伪装成逐 packet
映射。它仍保留 control phase 双状态机限制，需要单独设计新的 control request boundary。
新 transport status 在较窄 H723 controller consumer 中仍会折叠为 generic transaction error；
完整错误传播和 USB PHY/VBUS、拓扑、isochronous/streams、DM-MC02 host wiring 仍未支持。

# 2026-09-02 USB control-core VMState invariant review

本轮仍留在可复用 USB control-transfer core 层。producer 是
`DmUsbControlDevice` 的控制传输状态，boundary 是 `dm_usb_control_vmstate()`，
consumer 是未来 DWC2/device composite 的恢复流程。

审查发现的首个错误不是 VMState 编码，而是恢复后可继续执行的状态集合过宽：原校验
允许 `zero_length_packet=true` 出现在 `STATUS_OUT`，也允许 `DATA_IN` payload 超出
setup 的 `wLength`，以及把 pending address/configuration 与无关 request 组合。这样的
状态会在下一次 token 处理时跳过/发送错误数据，或触发与当前 setup 无关的板级 callback。

修复集中在组件边界：

- `DATA_IN` 现在要求标准/class IN request、payload 非空或带 ZLP，且不超过
  `request.length`；ZLP 只允许在 DATA-IN、实际 payload 小于 wLength 且按 MPS 整包结束。
- `DATA_OUT`、`STATUS_OUT` 和 `STATUS_IN` 分别校验方向、游标和已提交 payload 的
  关系；class OUT 的 STATUS_IN 必须已完整接收。
- pending address/configuration 必须匹配对应 standard request、`wValue` 和空 status
  payload；STALLED 保留当前实现允许的部分 OUT cursor，但清除/拒绝活动 pending/ZLP。

代码变更文件仅为 `hw/usb/dm_usb_control_vmstate.c` 和
`tests/unit/test-dm-usb-control-vmstate.c`；同步更新了项目长期文档。新增回归覆盖 ZLP 错 phase、payload 超界、
空 DATA-IN 和无关 pending 状态。隔离测试 `test-dm-usb-control-vmstate` 为 `6/6`；
control core、DWC2 core/VMState、QEMU adapter、DM-MC02 USB qtest 均通过，目标重链通过。

残余风险：VMState 仍可能在 post-load 返回错误前写入普通字段，不提供事务级 rollback；
该描述尚未注册到 DM-MC02 machine，也未覆盖 DWC2 control 联合恢复、CPU/IRQ/NVIC、RAM、
USB bus/PHY/SOF/DMA、宿主机枚举或整机 snapshot/migration。下一道门应先定义并测试
control core 与 DWC2 composite 的加载顺序，不应由板级或 UI workaround 掩盖缺失状态。

# 2026-09-02 QEMU request-level control bridge review

本轮所属层是可复用 USB control core 到 QEMU device callback 的边界。首个错误确认在
先前 adapter 的 control callback：QEMU `core.c` 已经拥有 SETUP、DATA 和 status 的
控制阶段，adapter 又对下层 `DmUsbTransaction` 重复提交整套 SETUP/DATA/STATUS。该做法
虽然同步测试可以通过，但会让短包、ZLP、status、地址提交和未来迁移状态由两套状态机
分别决定。

修复将 QEMU `USBDeviceClass.handle_control` 明确建模为 request-level producer。新增
`DmUsbQemuControlSubmit`，callback 一次接收解析后的 request 和完整 data stage；IN
返回聚合 payload，OUT 消费完整 payload，参数只在同步调用期间借用。adapter 不再从
control callback 调用 lower transaction submit，仍保留 bulk/data 的 transaction 边界，
并继续在成功的 `SET_ADDRESS` request 后更新 QEMU device address。缺失 callback、非法
字段、方向/长度违规和 callback 重入均在 adapter 边界拒绝。

底层 consumer 使用新增的 `dm_usb_control_execute_request()`。该 helper 只驱动现有
`DmUsbControlDevice` 的公开 SETUP/data/status API：IN 按 core 的最大 packet size
收集数据并完成 status OUT，OUT 在完整 data stage 后完成 status IN，成功回到 IDLE，
没有创建第二套协议状态机。验证中首次失败是 helper 对零长度 OUT 误调用 DATA_OUT，
而 `SET_ADDRESS` setup 后已处于 STATUS_IN；修复分支后，control core 隔离门为 `7/7`，
真实 QEMU bus adapter 为 `15/15`。

变更文件：`hw/usb/dm_usb_control.[ch]`、`hw/usb/dm_usb_qemu_adapter.[ch]`、两个
USB unit test，以及架构/接口/计划文档。`trobot/` 未修改。

该切片仍只证明同步 request-level callback 和既有 control core 的组合，不证明 QEMU
async control completion、逐 token lower transport、USB PHY/VBUS/SOF、完整 DWC2
control composite VMState 或 machine-level migration。下一道门应先设计 DWC2 control
状态与该 request boundary 的联合回载顺序，并为其增加隔离和直接 consumer 测试。

# 2026-09-02 DWC2/control composite VMState review

本轮所属层是可复用 USB control core 与 DWC2 device-mode 芯片层的联合边界。首个设计
风险是两个已有 component VMState 都带有独立 post-load：若直接嵌套，DWC2 可能在
control phase 尚未恢复时重建 transaction callback/IRQ projection。当前实现新增
`DmUsbDwc2ControlLink`，以 `control -> DWC2 raw` 的顺序加载，两个 child 均使用无副作用
raw 描述，parent 在完整字段恢复后统一验证并只同步一次 DWC2 runtime。

联合边界还检查 embedded control pointer、EP0 MPS 一致性和两个 distinct child marker。
因此交换 child、截断流、非法 control phase/FIFO 或不支持版本不会触发 callback/IRQ
同步；control callback、opaque、IRQ/QOM/MemoryRegion 仍由目的端拥有。VMState 普通字段
在错误返回前可能已写入，未引入事务级 rollback。

验证：联合隔离/直接消费者 `test-dm-usb-dwc2-control-link-vmstate` `7/7`，覆盖 DATA_IN
中途继续、SET_ADDRESS pending、同时恢复 endpoint FIFO、request-level consumer、非法
phase/FIFO、错误 child 顺序、截断和版本；既有 control VMState `6/6`、control core
`7/7`、DWC2 VMState `4/4`、DWC2 core `7/7` 均通过。本轮未修改 `trobot/`，未注册
DM-MC02 machine-level VMState。

收尾验证还包括全部 16 个 USB unit、DM-MC02 USB qtest `10/10`、H723 USB host qtest
`4/4`、Host CTest `54/54` 和 `qemu-system-arm` 重链；负向 VMState 用例的 marker、
版本和 FIFO 诊断均为预期拒绝路径。

残余风险：QEMU USB adapter 对象本身、USB bus/PHY/SOF/DMA、CPU/IRQ/NVIC、RAM、外部
co-sim 队列及其跨组件恢复顺序仍未覆盖；该 composite 不能推出整机 snapshot/migration
支持。下一道门仍应选择相邻底层状态边界，先完成其独立和直接 consumer 契约。

# 2026-09-02 DM-MC02 board reset composition review

本轮所属层是 DM-MC02 board composition，未修改 `trobot/`，也未注册整机
VMState。producer 是 GPIO/SYSCFG/EXTI/power 组件的复位状态和板级外部输入；boundary
是新增的 `dm_mc02_board_reset_gpio_power()`；consumer 是 machine reset 中的 BMI088
片选、外部 GPIO/EXTI 电平及 FDCAN/RS485/MCU 电源投影。

首个错误出现在隔离测试的 consumer 期望，而不是组件实现：stub 实际记录了
`GPIO0, GPIO1, reset_cs, SYSCFG, EXTI, apply_gpio_inputs, power_reset, apply_power`
八个事件，测试原先漏掉了 EXTI 和 power reset 两项。修正期望后，另修正 qtest 中
跨行宏的预处理续行；窄编译确认 producer、边界和 consumer 顺序一致。第一次构建
命令还使用了不存在的裸 qtest 目标名，随后改为
`tests/qtest/dm-mc02-reset-test`，这属于构建调用错误，不是源码失败。

实现将 board-level 顺序集中到 helper：GPIO reset 后才投影 inactive CS，随后清理
SYSCFG/EXTI 并重注入输入，再 reset power model，最后由 machine hook 更新下游
FDCAN/RS485/MCU 状态。hook 不调用第二次 power setter，因此 power model 的一次
`dm_mc02_power_reset()` update 是该阶段唯一 ADC source 更新。其它外设仍由
`dm_mc02_machine_reset()` 负责，未把板级策略下沉到通用组件。

验证：`test-dm-board-reset` `1/1`；`dm-mc02-reset-test` `2/2`；
`ninja ... qemu-system-arm` 窄构建通过；Host CTest `54/54` 通过。

残余风险：helper 只定义板级 GPIO/电源组合，不区分 warm/cold 的 volatile-memory
策略，也不保存任何 VMState；CPU/IRQ、RAM、DMA、总线、chardev、clock、co-sim 及
整机 snapshot/migration 的恢复顺序仍未闭合。因此本轮不能宣称整机迁移或所有外设
复位语义已经支持。

# 2026-09-02 STM32H723 ADC pair composite VMState review

本轮所属层是 STM32H723 ADC 芯片层，未注册 DM-MC02 machine-level migration，也未修改
`trobot/`。producer 是 ADC12_COMMON 的共享状态及 ADC1/ADC2 的可继续转换状态；boundary
是 `DmMc02AdcPair` 的 version-1 composite VMState；consumer 是未来 SoC/board restore
组合。该边界固定 `common raw -> ADC1 raw -> ADC2 raw` 的加载顺序。

首个错误状态是底层 ADC validator 的合法状态判定：表达式把非法条件以逻辑或返回，导致
所有约束都满足的合法 fixture 返回 false。修复 producer/validator 后，合法 ADC 单体和
pair round-trip 均恢复；既有非法 active-state 用例仍被拒绝。这个错误没有通过 pair-level
放宽校验来规避。

raw child 描述只执行字段验证，不调用 clock callback、timer、DMA 或 IRQ projection。
parent 在两个 ADC 和 common 字段都装载后，检查 common pending master/slave sample 的
conversion ID 与对应 ADC active regular sequence ID 一致，然后按 common clock projection、
ADC1 scheduler/IRQ、ADC2 scheduler/IRQ 的顺序各执行所需同步。回载失败不会执行 runtime
projection，但 VMState 普通字段在错误返回前可能已经写入，不提供事务级 rollback。

变更文件：`hw/arm/dm_mc02_adc_vmstate.c`、ADC/common 头与 VMState 文件、新增
`hw/arm/dm_mc02_adc_pair.[ch]` VMState、ARM/unit Meson 清单、pair VMState 单测和
test-only stubs。验证为 ADC VMState `3/3`、common `15/15`、pair `3/3`、直接 ADC qtest
`62/62`、ADC smoke 全部通过、QEMU smoke `89/89`、Host CTest `54/54` 和
`qemu-system-arm` 重链通过；负向截断/身份/非法状态日志均为预期拒绝路径。

残余风险：pair 尚未接入 machine-level VMState；DMA/DMAMUX、CPU/IRQ/NVIC、片上 RAM、
trigger bus、board power、外部 bus 和 co-sim queue 的联合恢复顺序仍未定义。该切片不
代表整机 snapshot/migration、精确 DMA 仲裁或完整 ADC multimode 支持。

# 2026-09-02 STM32H723 FDCAN/shared Message RAM composite review

本轮继续留在 STM32H723 芯片层，没有注册 DM-MC02 machine-level migration，也没有修改
`trobot/`。producer 是 QEMU `DmMessageRam` RAM owner 与一个 FDCAN 的寄存器/raw 状态；
boundary 是 `DmMc02FdcanMsgRamLink`；consumer 是未来的 SoC/board composite restore。
本轮只定义一个 FDCAN 链接，不把多个设备或整机迁移一次性接上来。

首个风险在原有 FDCAN 的 RAM 地址契约缺失：运行路径对每次元素访问有局部边界判断，
但 VMState validator 不知道 SIDFC/XIDFC、FIFO、Rx Buffer 和 Tx FIFO 的完整几何范围，
因此一个恢复后的非法寄存器组合可能要等到后续 CAN 流量才暴露。另一个底层错误是
`RXESC` 的 FIFO1 数据大小没有按 F1DS 字段移位，FIFO1 的元素步长可能被误读为 FIFO0
配置。

修复集中在 FDCAN producer/边界：新增
`dm_mc02_fdcan_message_ram_state_valid()`，统一用 checked 64-bit span 检查过滤器、
已配置 FIFO、64 个模型专用 Rx Buffer 和 Tx FIFO；link validator 进一步要求 marker、
`DmMessageRam` 指针/大小和 FDCAN runtime 指针完全一致。保存时 marker 从 RAM owner
几何生成；恢复时 marker 先于 raw FDCAN 状态加载，随后只在所有范围有效后调用一次
`dm_mc02_fdcan_sync_runtime()`。Message RAM 字节保持 QEMU RAM owner 的职责，不在 link
内重复序列化。raw child 不触发 timer、IRQ、CAN 或 chardev 投影。

验证为 `test-dm-fdcan-msg-ram-link` `4/4`、FDCAN VMState `5/5`、Message RAM owner
`2/2`、FDCAN standard/extended/Rx Buffer/FIFO1/bus-off/kernel-clock smoke，以及
`qemu-system-arm` 重链通过。link 单测同时确认目的端 RAM 字节不被覆盖、几何不匹配和
FIFO/Buffer 越界在 IRQ 投影前拒绝、两个 link 共享同一 RAM 字节。

残余风险：link 仍是 component contract，不代表 RAM block 与多个 FDCAN 的 machine-level
migration 顺序已经闭合；当前模型仍不提供 section overlap 诊断、精确 M_CAN arbitration/
physical CAN timing、CPU/IRQ/NVIC、DMA/DMAMUX、CAN queue 或整机 snapshot/migration。
现有 DM-MC02 machine 继续使用其直接 RAM 指针 wiring，待 RAM、FDCAN、IRQ、DMA 和 bus
所有者分别具备下层门后，再定义 machine-level composite。

# 2026-09-02 STM32H723 GPIO/SYSCFG/EXTI composite review

本轮所属层是 STM32H723 SoC 的 GPIO 到 NVIC 外部中断边界。producer 是 GPIO bank
寄存器/采样输入、SYSCFG EXTICR 路由和 EXTI pending/line 状态；boundary 是新增的
`DmMc02GpioExti` version-1 composite；consumer 是板级 GPIO 输出、外部输入注入和
NVIC 的 level-sensitive IRQ wiring。没有注册 DM-MC02 machine-level migration，也未
修改 `trobot/`。

首个边界风险是三个现有 standalone VMState 都带 post-load：GPIO 会立刻通知板级 ODR
consumer，SYSCFG 会立刻重注入输入，EXTI 会立刻驱动 IRQ。若直接嵌套，恢复顺序可能在
EXTICR 尚未恢复时产生错误的输入边沿或 IRQ。修复是在 producer 所属层增加三个 raw
child description，parent 固定按 `GPIO -> SYSCFG -> EXTI` 加载，并在字段全部有效后
统一执行 ODR projection、板级 input hook 和 EXTI IRQ projection。

组合对象只验证目的端 profile 的 bank count 和固定 bank identity；MemoryRegion、IRQ
handle、ODR callback、board external input source 和 GPIO 电气规则保持 runtime-owned。
input hook 是唯一允许板级电气输入重注入的边界，避免通用 SoC 层猜测 pull-up/AF 行为。
VMState 在错误返回前可能已经写入普通字段，但 invalid geometry/truncation 均不会调用
projection hook。

变更文件：`hw/arm/dm_mc02_gpio.[h]`/VMState、`dm_mc02_syscfg.[h]`/VMState、
`dm_mc02_exti.[h]`/VMState、新增 `dm_mc02_gpio_exti.h` 和其 VMState、
`dm_mc02.c`、ARM/unit Meson 清单、新增组合 focused test/stub，以及
`PLAN.md`/`INTERFACES.md`/`ARCHITECTURE.md`。验证为组合单测 `3/3`，GPIO/SYSCFG/EXTI
既有单测 `2/2 + 2/2 + 2/2`，`dm-mc02-reset-test` `2/2`，`dm-mc02-cpu-test` `2/2`，
`qemu-system-arm` 重链通过。

剩余风险：当前组合不注册整机 VMState，不保存 machine 的 `external_gpio`、CPU/NVIC、
RAM、DMA/DMAMUX、外设、总线或 co-sim 队列；也不宣称完整 H723 GPIO AF/电气语义、EXTI
安全域/event-only 行为或整机 snapshot/migration。下一步仍应先闭合另一个相邻 SoC
状态 owner，再考虑 machine composite。

# 2026-09-02 EXTI 到 native NVIC 接线边界 review

本轮仍属于 STM32H723 SoC 直接 IRQ 边界。producer 是 `DmMc02Exti` 的七组 level
输出，boundary 是 `dm_mc02_exti_connect_nvic()` 的 borrowed `qemu_irq` 路由表，
consumer 是 QEMU 原生 ARMv7-M/NVIC external input。未修改 `trobot/`，未新增本地
CPU/NVIC 状态镜像。

首个实现风险是把 controller input 当作七组 EXTI 的数组下标；DM-MC02 使用 NVIC
input 40，这会越界。实现改为 pairwise duplicate 检查并以调用者提供的
`controller_input_count` 做边界校验。完整路由表在所有校验通过后才绑定，因此后续坏项
不会留下部分接线。EXTI pending 和 NVIC pending 的独立性也被直接 qtest 明确验证。

变更文件：`hw/arm/dm_mc02_exti.[ch]`、`hw/arm/dm_mc02_board.c`、
`tests/unit/test-dm-gpio-exti-link-vmstate.c`、`tests/qtest/dm-mc02-cpu-test.c`，以及
`AGENTS.md`、`PLAN.md`、`INTERFACES.md`、`ARCHITECTURE.md`；未修改 `trobot/`。
隔离测试为 `test-dm-gpio-exti-link-vmstate` `4/4`，直接 CPU/NVIC qtest 为 `3/3`。

残余风险：该接口只闭合运行时接线和 direct consumer，不加载 QEMU native CPU/NVIC
VMState，也不定义 machine-level migration 的 child 顺序；EXTI event-only、安全域、
完整 GPIO 电气规则和其它外设 IRQ 仍未覆盖。当前 board profile 的七条 route 依赖
profile validator 已先验证 vector geometry。

# 2026-09-02 STM32H723 DMA/DMAMUX composite-to-machine wiring review

本轮继续停留在 STM32H723 芯片组合与 DM-MC02 machine wiring 边界。首个结构问题是
machine 同时持有 `dma1/dma2/dmamux1/dmamux2` 四个字段，而已存在的
`DmMc02DmaSubsystem` 又描述同一组 producer；这会让后续组合回载存在两个可能的 owner。
修复是让 machine 只嵌入该组合并从 `dma[0..1]`、`dmamux[0..1]` 借用所有指针，未改变
地址映射或 channel offset。

第二个边界风险是 DMA1/2 和 DMAMUX1/2 的 positional child 没有身份检查。当前组合流升为
version 2，在 raw child 前写入四个固定 marker，并在 pre/post-load 验证；marker、FIFO 或
截断错误都在 request-cache 清理和 IRQ projection 前返回。旧的 version-1 无 marker stream
由独立 fixture 验证仍可读取，目的端必须先安装静态 identity。

变更文件：`hw/arm/dm_mc02_dma.[ch]`、`dm_mc02_dma_subsystem_vmstate.c`、`dm_mc02.c`、
`tests/unit/test-dm-dma-subsystem-vmstate.c`，以及 root/QEMU 的约束、计划和契约文档。
未修改 `trobot/`。隔离测试 `test-dm-dma-subsystem-vmstate` 为 `6/6`；直接
`run-dma-smoke.sh`、`run-dma-arbitration-smoke.sh`、`run-dma-batch-smoke.sh`、
`run-dma-fcr-smoke.sh`、`run-dma-irq-smoke.sh`、`run-uart-dma-smoke.sh` 全部通过，
`qemu-system-arm` 窄重链通过。

残余风险：组合仍未注册 DM-MC02 machine-level VMState；DMA/RAM、CPU/NVIC、外设 timer/
endpoint scheduler、backpressure retry、跨外设恢复顺序和真实 DMA 总线仲裁/物理时序仍
未定义。当前结果只证明 owner/wiring 收敛和已有直接 consumer 不回归。

# 2026-09-02 UART TX DMA backpressure direct-boundary review

本轮首先复核 UART endpoint 接入，而不是把 FIFO 满误判为 DMA 配置错误。首个错误状态
是 host-facing TX FIFO 已满时 endpoint 只能返回旧的 bool 失败；DMA 会将合法的暂时容量
不足当作 TEIF，并可能丢失当前字节。修复后的 UART `write_ex` 在容量不足时返回
`DM_MC02_DMA_ENDPOINT_RETRY`，不写 TDR、不推进 NDTR/游标、不关闭 stream、不置 TEIF。

进一步的代码审计发现 FIFO M2P 还有一个更隐蔽的 producer/boundary 错配：endpoint 回调
之前，DMA 可能为形成当前外设 beat 预取一个或多个内存 word；如果随后返回 RETRY，只
保留 FIFO 字节而不保留对应内存游标，下一次请求会从旧地址再次预取，最终出现重复字节。
该错误属于 DMA FIFO transaction boundary，不应由 UART 或 UI workaround 掩盖。现在
`RETRY` 回滚本次请求新增的 FIFO head/length，保留此前已提交的 FIFO 内容；因此已提交
内存游标和 FIFO 成对不变，下一次 retry 可从同一地址重建相同 beat。

变更文件：`qemu/upstream/hw/arm/dm_mc02_uart.c`、
`qemu/upstream/hw/arm/dm_mc02_dma.c`、
`qemu/upstream/tests/qtest/dm-mc02-uart-test.c`、
`qemu/upstream/tests/qtest/meson.build`、
`tests/dma_fifo_dbm_endpoint_smoke.c`，以及对应契约/计划/约束文档；未修改
`trobot/`。

验证证据：`test-dm-uart-vmstate` `5/5`；直接 UART qtest
`/dm-mc02/uart/dma-tx-backpressure` `1/1`；
`run-uart-dma-smoke.sh on|off` 和 `run-uart2-dma-smoke.sh on|off` 均通过；
`dm_mc02_dma_fifo_dbm_endpoint_smoke` 普通构建及 ASan 构建均通过；
`qemu-system-arm` 增量重链通过。qtest 使用 QEMU `ringbuf`，首个 DM-MC02 UART serial
slot 显式配置为 `-serial none -serial chardev:uart1`，避免测试误连 slot 0。

残余风险：UART DMA retry 当前由 UART virtual timer 驱动，仍不等同于通用异步 transport
completion；chardev close/reopen 的直接 qtest、UART/DMA/DMAMUX 联合 VMState、真实串行
位级/电气时序和整机 migration 仍未覆盖。该切片也只为 UART TX endpoint 建立直接门；在
该历史切片完成时 ADC/SPI/OCTOSPI 的 legacy callback 限制仍存在，后续 ADC1、SPI RX
和 OCTOSPI consumer slice 已分别建立 direct P2M reservation。

# 2026-09-02 STM32H723 IWDG window-mode review

本轮继续停留在 STM32H723 芯片层，producer 是 IWDG 的寄存器镜像、reload/start
控制状态和绝对 virtual deadline，boundary 是 `KR=0xaaaa` reload 的窗口检查，consumer
是 guest reset 和 diagnostics。没有修改 `trobot/`，也没有注册 machine-level VMState。

首个错误状态出现在配置边界：`iwdg-boot-grace-ms=0` 仍保留
`boot_grace_pending=true`，若仅检查该布尔值会把严格硬件模式误当成宽限期，导致过早
reload 被接受。现在 reload 条件把零时长视为“无宽限”，并由 qtest 固定覆盖。

实现新增 12-bit `WINR` 寄存器和解锁写语义；`WINR >= RLR` 作为 reset/default 的窗口
关闭，`WINR < RLR` 时从当前绝对 deadline、LSI、prescaler 和 RLR 推导下降计数。
当计数大于 WINR，reload 不增加正常 reload 计数、不重新武装 timer，而是累计
`window-violations` 并请求 guest reset；窗口内 reload 才重装原有 timer。推导使用
`__uint128_t` 并在转换前饱和，避免极端 deadline 乘法溢出。修改 WINR 不重置当前倒计时。

验证：`test-dm-iwdg-vmstate` `6/6`、`dm-mc02-iwdg-test` `4/4`、既有
`tools/run-iwdg-smoke.sh` 和 `qemu-system-arm` 重链均通过。直接 qtest 覆盖默认窗口关闭、
锁定 WINR、第一 watchdog tick 的合法边界 reload、过早 reload reset 和 256 ms 超时。

残余风险：当前模型仍不提供每个 LSI tick 的可读倒计数、LSI 动态温漂、独立电源域或
machine-level snapshot/migration；`WINR`/RLR 的完整非法组合和精确 silicon 边界仍需
实机或参考手册证据进一步校准。

# 2026-09-05 STM32H723 IWDG deterministic LSI configuration review

本轮仍停留在 STM32H723 可复用芯片层。producer 是 IWDG 的名义 LSI 频率、固定 ppm
误差、PR/RLR 和绝对 virtual deadline；boundary 是无板卡依赖的整数时序换算；consumer
是 IWDG timer、窗口计数、guest reset 和 diagnostics。没有修改 `trobot/`，也没有把
该组件注册为 machine-level VMState。

首个错误状态出现在时序公式而非 QEMU 调度：重构 helper 后曾把已经包含 prescaler 的
`ticks` 又在分母中乘了一次 prescaler，导致超时短约 65536 倍。独立 timing 单测首先
复现并固定了 `+100000 ppm = 232727273 ns`、nominal `= 256000000 ns`、
`-100000 ppm = 284444445 ns`，随后直接 qtest 才验证机器边界。

修复将换算抽到 `dm_mc02_iwdg_timing.[ch]`。ppm 使用确定性的 `1,000,000` 有理数
比例，乘法使用 `__uint128_t`，超时和剩余计数采用向上取整，并保持一个 QEMU virtual
timer，而不是每个 LSI tick 创建事件。机器增加 `iwdg-lsi-hz` 与
`iwdg-lsi-error-ppm`，默认 `32000/0`，有效范围为非零频率和 `-999999..1000000` ppm；
无效配置在 QOM machine property 边界拒绝。诊断同时报告名义频率、固定误差和四舍五入
后的有效频率。

运行中的 LSI 配置被明确设为 startup-only。machine setter 和 component setter 都拒绝
started 状态的改变，且 setter 不调用 arm；因此不会用新频率重算旧绝对 deadline 或
延长当前 watchdog 窗口。配置只会在后续合法 start/reload 所建立的 deadline 中生效。

验证结果：`test-dm-iwdg-timing` `4/4`，`dm-mc02-iwdg-test` `5/5`，相关
`qemu-system-arm` 目标重链通过；既有 IWDG window、VMState 和前序回归需在本轮完整
套件中继续确认。已知限制仍包括 LSI 启动 settling、温度/随机漂移、独立电源域、每 tick
寄存器可见性和 machine-level migration；固定 ppm 仅用于可复现误差注入，不替代这些
物理行为。

# 2026-09-05 IWDG timing sentinel and configuration-boundary correction

本次复核仍停留在 STM32H723 IWDG 芯片层。发现的首个错误状态是 timing helper 在
`deadline_ns == 0 && now_ns == 0` 时返回计数器 0；0 实际是组件的未启动/无 deadline
哨兵，不能被解释为已经到期。修复后无效 deadline 始终返回完整 `reload`，只有非零
deadline 且 `now_ns >= deadline_ns` 才返回 0。

同一边界还发现组件 setter 对 `lsi_hz == 0` 的默认化行为与公共契约矛盾：machine
property 会拒绝零频率，但独立复用该组件的调用者会得到一个隐式 32000 Hz。setter
现在直接拒绝零值，保持 producer、边界和 consumer 的输入域一致；正常 reset 初始化
仍保留显式默认频率。

变更文件：`hw/arm/dm_mc02_iwdg_timing.c`、`hw/arm/dm_mc02_iwdg.c`、
`hw/arm/dm_mc02_iwdg.h`、`tests/unit/test-dm-iwdg-timing.c`，以及对应
`INTERFACES.md`/`ARCHITECTURE.md`。未修改 `trobot/`。

验证：`test-dm-iwdg-timing` `4/4`、`dm-mc02-iwdg-test` `5/5`、
`tools/run-iwdg-smoke.sh` 通过；`qemu-system-arm` 增量重链通过。残余限制仍为
LSI settling/漂移、独立电源域、每 tick 可见倒计时和 machine-level VMState。

# 2026-09-05 STM32H723 IWDG configuration-update status review

本轮仍是可复用 STM32H723 IWDG 芯片层，未改变 DM-MC02 板级策略、外部后端或
`trobot/`。本地 ST HAL (`Drivers/STM32H7xx_HAL_Driver/Src/stm32h7xx_hal_iwdg.c`)
明确在写入 `PR/RLR/WINR` 后等待 `SR.PVU/RVU/WVU` 清零，并说明 status update 最多需要
5 个 LSI period；原模型则在 MMIO 写入时立即改变寄存器并重置 deadline，遗漏了真实 HAL
consumer 可观察的第一个状态边界。

修复把已提交寄存器、各自 pending 值、SR flag 和 per-register absolute virtual deadline
分开。解锁后的首次 PR/RLR/WINR 写只设置相应 SR bit，同一 pending register 的后续写被
抑制；读取始终得到已提交值。新的 board-independent timing helper 以固定 ppm LSI 模型、
128-bit 算术和向上取整给出 5 个有效 LSI 周期的确定性上界。IWDG 仅使用一个
configuration-update QEMU timer 来调度最早 pending deadline，不新增每 LSI tick event。
PR/RLR commit 不重解释已运行 watchdog 的 deadline，WINR commit 则按 HAL 文档自动 reload。

状态是 component producer 的一部分，`dm_mc02_iwdg_vmstate()` 从 v1 升为 v2 并保存
pending value/deadline。v1 流在加载时明确归一化为无 pending 状态；SR/值/deadline 矛盾、
保留位和截断流均在 `dm_mc02_iwdg_sync_runtime()` 前拒绝。machine finalize 同时释放既有
watchdog timer 和新的 configuration-update timer，避免长期 runner 的 timer 泄漏。

验证：timing unit `5/5`，IWDG VMState `9/9`（含 v1 compatibility fixture），machine qtest
`6/6`，`tools/run-iwdg-smoke.sh` 和当前源码构建的真实 Release 固件 1 s 回归通过。真实
固件测得 startup `0.022570 s`、RTF `1.000097x`、CPU `113.9%`、RSS `50444 KiB`、IWDG
`starts=1,reloads=186,timeouts=0`。完整 Host CTest `54/54` 通过，其中包含 89 项 QEMU
smoke；`qemu-system-arm` 已重链。没有活动 QEMU/worker 进程。

残余风险：五周期是 HAL 公布的确定性上界而非 silicon synchronizer phase 的统计模型；LSI
startup、温度/随机漂移、IWDG 独立电源/复位域和 machine-level migration 仍未实现。该结果
只证明 IWDG configuration-update 与直接 HAL consumer 边界，不能外推为完整 H723 IWDG
物理行为。

# 2026-09-05 STM32H723 IWDG reset reason review

本轮新增的是 IWDG 到 PWR/RCC 的芯片层边界。首个错误状态是 reset request 只进入了 QEMU
全局 reset 队列，RCC `RCC_RSR` 仍只是普通镜像；随后 machine reset 清空镜像，导致下一次
guest 无法读取 watchdog 原因。另一个设计风险是让 IWDG 直接包含 RCC 私有状态，这会把可复用
芯片组件反向耦合到板级实现。

修复将 IWDG 的 producer 收敛为 `DmMc02IwdgResetRequested` callback。DM-MC02 在 PWR/RCC
初始化完成后绑定 callback；PWR/RCC 收到事件后设置 H723 实际 `RCC_RSR.IWDG1RSTF`
（offset `0xd0`, bit 26）。`RCC_RSR.RMVF`（bit 16）是 W1C，来源位只读。PWR/RCC reset
只保留当前已建模的 reset-source flags，因此 IWDG reset 后下一次 boot 能观察到 flag；写
RMVF 后清除。

变更文件：`dm_mc02_iwdg.[ch]`、`dm_mc02_pwr_rcc.[ch]`、`dm_mc02.c`、PWR/RCC unit 和
IWDG qtest 以及长期约束/契约文档；未修改 `trobot/`。验证为 PWR/RCC unit `5/5`、
IWDG qtest `7/7`、bare-metal smoke 的 `0x04000000 → 0x00000000` 读写链路和受影响
`qemu-system-arm` 重链通过。

残余风险：目前只支持 IWDG1 reset source；其它 H723 reset flags、cold power-domain cause、
完整 RCC 位语义、reset-cause migration 和真实 silicon reset-domain timing 仍未实现。普通
system reset 的 flag preservation 已覆盖，但不应外推为完整复位原因模型。

# 2026-09-05 STM32H723 SYSRESETREQ software reset reason review

本轮继续停留在 STM32H723 芯片层。首个边界风险是 guest 通过 AIRCR
`SYSRESETREQ` 发起的 reset 与 QEMU/host 的普通 reset request 在进入全局
reset 队列后看起来相同；若只在 machine reset hook 中猜测来源，会把所有
reset 错标为软件复位。QEMU 已提供原生 ARMv7-M/NVIC `SYSRESETREQ` named
output，因此 producer 复用该输出，DM-MC02 只在其 asserted edge 设置
`RCC_RSR.SFTRSTF`（bit 24），deasserted edge 不产生第二个事件。

变更文件：`hw/arm/dm_mc02_pwr_rcc.[ch]`、`hw/arm/dm_mc02.c`、PWR/RCC
unit 和 `tests/qtest/dm-mc02-cpu-test.c`，以及约束/计划/接口/架构文档；未
修改 `trobot/`。runtime `qemu_irq` wiring 不进入 machine VMState。

验证证据：PWR/RCC unit `6/6`；直接 `dm-mc02-cpu-test` `4/4`。qtest 通过
真实 guest-visible AIRCR 写入触发 reset，检查 reset 后 `SFTRSTF` 存在、后续
普通 `system_reset` 保留该位，最后用 `RMVF` 清除。`qemu-system-arm` 增量
重链通过。

残余风险：当前只覆盖 SYSRESETREQ→SFTRSTF，不涵盖 CPURSTF、D1/D2、BOR/POR、
pin、low-power、WWDG、完整 H723 reset-domain timing、reset-cause migration 或
machine-level migration；这些不能由本切片的映射推断。

# 2026-09-05 STM32H723 power-on reset reason review

首个边界风险是把 QEMU 的普通 reset 生命周期误当成 H723 的 power-on reset，
从而在每次 QMP/system reset 后重复制造 POR 原因。现有 machine 初始化是唯一能
明确标识初始上电的 producer；因此新增独立的
`dm_mc02_pwr_rcc_note_power_on_reset()` hook，只在 PWR/RCC 初始化后、guest
执行前调用一次，并将 `RCC_RSR.PORRSTF`（bit 23）写入来源 latch。普通 reset
继续保留来源，RMVF 清除；`cold-reset` 和任意 `cpu_reset()` 没有被提升为 POR。

变更文件：`hw/arm/dm_mc02_pwr_rcc.[ch]`、`hw/arm/dm_mc02.c`、PWR/RCC unit、
CPU qtest 以及工作区/项目约束和契约文档；未修改 `trobot/`。验证为 PWR/RCC
unit `7/7`、`dm-mc02-cpu-test` `5/5`、`qemu-system-arm` 增量重链通过。

证据边界：本地 Renode `STM32H7_RCC.cs` 用于交叉核对 H7 `PORRSTF` bit 23；它
不是 vendor reference 或 physical-board trace。BOR/PIN、CPU/domain、低功耗、
WWDG 和完整 reset-domain/migration 仍未验证，不能从该 hook 外推支持范围。

# 2026-09-05 STM32H723 discrete brownout reset reason review

本轮仍停留在 STM32H723/DM-MC02 电源复位边界。审计首先确认当前 `DmMc02Power` 只有
离散的 `OFF`、`UNDERVOLTAGE`、`NORMAL` 状态，不能把所有欠压采样或 `VIN=0` 直接猜成
H723 BOR。因而将 `NORMAL→UNDERVOLTAGE`（VIN 从 `>=12000 mV` 降到 `1..11999 mV`）
明确为唯一运行时 brownout producer；启动低 VIN 在 callback wiring 之前建立，OFF、保持
欠压和恢复也不重复发事件。

实现增加了电源组件的板卡无关 brownout callback，并在 reset 中保留 runtime wiring。DM-MC02
consumer 将它投影到 `RCC_RSR.BORRSTF` bit 21，再调用 QEMU 正常 reset request；PWR/RCC
继续只负责来源锁存和 `RMVF` 清除。这样没有把 PWR/RCC 类型反向塞进可复用 power producer，
也没有用 machine reset hook 猜测来源。

中途的首个测试错误是 fixture 顺序错误：回调已安装后写低 VIN，本身就是应触发的下降沿；
修正为先设置启动低 VIN、再安装 callback 后，power unit `5/5` 通过。PWR/RCC unit `8/8`
和真实 machine `dm-mc02-cpu-test` `6/6` 通过，后者覆盖启动低 VIN不触发、恢复后再次欠压
触发、普通 reset 保留 BOR 和 `RMVF` 清除。

残余风险：`12000 mV` 是显式离散 DM-MC02 假设，未由实机/官方电气阈值校准；没有模拟
斜率、滞回、converter transient、其它 H723 reset source/domain 或 machine migration。

# 2026-09-05 STM32H723 external NRST reset reason review

本轮所属层是 STM32H723/DM-MC02 的外部 reset-input 到 PWR/RCC 直接边界。首个设计
风险是把普通 QEMU reset request 猜成 PIN reset，或在 machine reset 时将外部 NRST
自动释放，从而让低电平保持重复触发。实现保留显式 producer：QOM 子对象
dm-mc02-reset-input 的命名 GPIO NRST；它只在 active-low 高到低边沿调用 callback。

producer、boundary、consumer 的责任分别是：reset-input 保存当前外部电平并抑制重复
low；DM-MC02 callback 先调用 dm_mc02_pwr_rcc_note_pin_reset() 再请求普通 QEMU reset；
PWR/RCC 锁存只读 RCC_RSR.PINRSTF（bit 22），由 RMVF 写 1 清除。普通 reset 只保留
已建模来源，不会从 reset API 反推 PIN 原因。machine reset 不改外部输入电平，因此
保持低电平不会重新调用 callback。

变更文件为 hw/arm/dm_mc02_reset_input.[ch]、dm_mc02_pwr_rcc.[ch]、dm_mc02.c、
ARM Meson 清单、reset-input unit、CPU qtest，以及工作区和 QEMU 项目约束/契约文档；
trobot/ 未修改。首个 qtest 失败表现为 reset 后 SYST_RVR 仍为 0，根因是 reset-input
reset hook 强制把 NRST 状态设为高，导致保持低电平被错误当作新下降沿；修复为保留
producer 当前电平后，问题消失。

验证：

- test-dm-reset-input：隔离 active-low edge/held-low contract，1/1。
- dm-mc02-cpu-test --tap：7/7，包含新增 external NRST pin-reset-reason。
- ninja -C dm-mc02-qemu/build/qemu tests/unit/test-dm-reset-input
  tests/unit/test-dm-pwr-rcc-vmstate tests/qtest/dm-mc02-cpu-test
  qemu-system-arm：目标重链通过。
- git diff --check：QEMU worktree 的 tracked diff 通过；新增文件另做空白扫描；
  工作区根目录不是 Git repository。

残余风险：/machine/reset-input、callback 和外部电平仍是 runtime wiring，尚未纳入
machine-level migration；未实现 CPURSTF、D1/D2/其它 reset domain、低功耗、WWDG、物理
NRST 去抖和完整 reset-domain 时序。当前 qtest 只证明该明确边界，不应外推为完整 H723
复位原因模型。

# 2026-09-05 STM32H723 WWDG1 window-watchdog review

本轮继续停留在 STM32H723 芯片层，并只向已有 DM-MC02 直接 guest consumer 接入一个
WWDG1 边界。producer 是 WWDG 的 `CR/CFR/SR`、可见 CNT 和虚拟 deadline；boundary 是
WWDG1 的 APB1 clock/IRQ wiring 与 reset callback；consumer 是 EWI/IRQ、guest reset 和
`RCC_RSR.WWDG1RSTF`。

行为参考采用本地 Renode `STM32H7_SystemWindowWatchdog`：CNT 到 `0x40` 进入 EWI 阶段，
下一 tick 到 `0x3f` 请求 reset；低于 `0x40` 的运行中 T 写入和窗口外 reload 直接失败。
QEMU 只调度下一可观察事件，不建立每 watchdog tick 的 host timer。tick 通过
`ceil(4096 * 2^WDGTB * 1e9 / clock_hz)` 计算，使用 128 位中间值。

审计发现并修复的首个实现风险是 CFR.WDGTB 写入顺序：原路径先覆盖 CFR，再使用新
divider 解释旧 `counter_start_ns`，导致运行中改频会改变已经消耗的 tick 数。现在先在旧
divider 下快照 CNT，再提交新 CFR 和新阶段起点。另一个语义修正是 WDGA：运行后 CR 写零
不能停表，T-only 写入仍是一次 reload；CR 读取保留动态 WDGA。MMIO 回调同时拒绝跨 32 位
寄存器边界的单次访问，避免错误的字节拼接。

变更文件包括 `hw/arm/dm_mc02_wwdg.[ch]`、timing helper、H723/DM-MC02 地址/clock/IRQ/
PWR-RCC wiring、Meson 清单和 `tests/qtest/dm-mc02-wwdg-test.c`；本轮未修改 `trobot/`。

验证：`test-dm-wwdg-timing` `3/3`、`dm-mc02-wwdg-test` `3/3`、受影响的 PWR/RCC、
IWDG、CPU/reset 回归、QEMU smoke suite `89/89`、Host CTest `54/54` 和目标
`qemu-system-arm` 重链均通过。qtest 精确覆盖默认寄存器、EWI/IRQ、EWIF 清除、WDGA
set-only、WDGTB 改频相位、窗口违规、超时以及 WWDG reset reason/RMVF。

残余风险：本轮未覆盖完整 H723 reset-domain/migration；官方手册细节未能在本轮通过网络
独立核验，Renode 仅是行为交叉参考。APB1 clock linkage、IRQ vector 0 和
`RCC_RSR.WWDG1RSTF` 是当前 DM-MC02 组合假设，不应外推为所有 H723 板卡或硅级电气
时序。

# 2026-09-05 STM32H723 WWDG1 component VMState review

本轮继续停留在 STM32H723 芯片层，补齐已实现 WWDG 的组件恢复边界。首个设计风险是把
QEMUTimer、IRQ、reset callback 或 APB1 clock 指针直接当作可迁移状态；这些对象属于目的
端 wiring/static configuration，序列化会把 QEMU 地址和板级所有权泄漏到流中。实现因此只
保存寄存器镜像、可见 CNT、计数相位、绝对虚拟 deadline、EWI/reset stage 和诊断计数。

normal 与 raw 描述共享同一字段和 producer 校验。normal 在所有字段通过保留位、计数、
deadline、阶段和目的端时钟检查后才调用 `dm_mc02_wwdg_sync_runtime()`；raw 只完成验证和
字段载入，不触发 timer/IRQ。这样未来 parent composite 可以明确安排
`WWDG raw -> destination wiring -> runtime projection`，也不会把 component gate 冒充整机
migration。

变更文件：`hw/arm/dm_mc02_wwdg.[ch]`、新增 `dm_mc02_wwdg_vmstate.c`、ARM/unit Meson
清单、`tests/unit/test-dm-wwdg-vmstate.c` 及其 runtime stub，并同步工作区/项目约束和契约
文档；未修改 `trobot/`。验证为隔离 VMState `8/8`、WWDG 直接 qtest `3/3` 和 QEMU
`qemu-system-arm` 增量重链。

残余风险：当前只提供 WWDG component VMState，不含 machine-level registration、RAM/CPU/
NVIC/PWR/RCC/DMA/总线联合恢复顺序；错误流的 qemu-file 诊断属于预期拒绝路径。下一步仍需
在相邻下层状态边界完成隔离门后，才能设计更高层 composite restore。

# 2026-09-05 STM32H723 DMA P2M FIFO overflow transaction review

本轮继续停留在 STM32H723 DMA 芯片层，producer 是 P2M peripheral request 与当前 FIFO
occupancy，boundary 是一个 `PSIZE` beat 的容量检查和提交顺序，consumer 是 peripheral
MMIO/`DmMc02DmaEndpoint`。首个错误状态位于 endpoint 读取之前：FIFO 已没有容纳完整 beat
的空间时，旧路径先消费 endpoint，再由 `fifo_push()` 报 FEIF；这会让外设数据已经产生
副作用，但 DMA 随后丢弃该 beat。

修复将容量预检查置于 endpoint/MMIO 读取之前。容量不足现在锁存 FEIF、清空 FIFO、清除
stream EN，且保持 NDTR、PAR、活动 memory cursor 与 guest memory 不变。FEIE 只影响 IRQ
投影，不影响 FEIF 锁存。这个顺序属于 DMA producer/boundary，未用 UART、板级或 UI 逻辑
绕过。

变更文件：`qemu/upstream/hw/arm/dm_mc02_dma.c`、
`tests/dma_fifo_dbm_endpoint_smoke.c`，以及 DMA/工作区约束和接口契约文档；未修改
`trobot/`。隔离 smoke 新增 FEIE 开/关两组已满 FIFO 场景，断言 endpoint 调用次数为零、
状态和内存不前进、FIFO 清空以及 FEIF/IRQ 分离。

验证：host DMA FIFO fixture 普通构建和 ASan 构建均通过，CTest 定向用例通过，
`tools/run-dma-fcr-smoke.sh` 通过，`qemu-system-arm` 增量重链通过。由于正常 guest 路径
在 FIFO threshold 到达时会排空 FIFO，ARM guest smoke 不注入 DMA 私有 FIFO，因此本轮不
声称完整硅级 FEIF 触发条件、DMA 总线仲裁、时钟级恢复或 machine-level migration。

# 2026-09-05 STM32H723 DMA direct P2M endpoint reservation review

本轮继续停留在 DMA 芯片层。审查定位到 direct P2M 的首个错误状态：旧路径先由
endpoint 消费 source，再执行目标内存写；目标写失败时 DMA 只保留 NDTR/cursor，却无法
恢复已经消费的外设字节。QEMU 没有可直接复用的“外设读取+目标写入”原子事务，
`dma_memory_read()`/`dma_memory_write()` 也是独立操作。

修复在可复用 endpoint 边界增加完整 `read_prepare/read_commit/read_abort` tuple。DMA
在 prepare 后暂存 source 数据，目标写成功才 commit，失败则 abort；partial tuple 在
边界被拒绝。旧 `read/read_ex` 保持明确的兼容语义，不能被误报为可回滚。OCTOSPI RX
cursor 现在只在 commit 时前进，legacy `read` wrapper 则显式执行 prepare+commit，以
维持直接 endpoint API 的既有行为。

变更文件：`qemu/upstream/hw/arm/dm_mc02_dma_endpoint.[ch]`、
`dm_mc02_dma.c`、`dm_mc02_ospi.[ch]`、`tests/dma_endpoint_smoke.c`、
`tests/dma_fifo_dbm_endpoint_smoke.c`、`tests/ospi_dma_integration_smoke.c`，以及
`AGENTS.md`、`PLAN.md`、`INTERFACES.md`、`ARCHITECTURE.md`；未修改 `trobot/`。

验证：endpoint contract、DMA FIFO/DBM、OCTOSPI endpoint、OCTOSPI-DMA integration
定向 CTest `4/4`；四个 smoke 可执行文件均通过；生产
`ninja -C build/qemu qemu-system-arm` 通过。

残余风险：该接口只保证同步单 beat 的 source consumption boundary；FIFO 多 beat
reservation 尚未设计。在该历史切片完成时，UART/SPI/ADC/ADC-common 仍使用立即消费
callback，未因此获得回滚能力；后续 UART RX 和 SPI RX consumer 切片已分别建立
reservation。QEMU 目标事务若在返回错误前产生部分副作用，DMA 不拥有其回滚权；这需要
独立的 atomic-memory boundary。该切片不宣称 machine-level migration、完整 DMA 总线
仲裁或所有硅级错误时序。

# 2026-09-05 UART RX direct P2M reservation review

本轮 UART RX 的首个错误状态与通用 DMA 审查相同：direct P2M 在目标内存事务完成前就
消费了 UART RX FIFO 队首。目标地址非法时，NDTR 和 DMA memory cursor 虽保持不变，
但输入字节已经丢失。

UART consumer 现在实现完整 reservation tuple：prepare 复制并记录队首身份，commit
只在目标写成功后消费该队首并更新 RDR/RXNE，abort 只释放 reservation。FIFO P2M 和
兼容模式仍保留立即消费 callback，因此其不可回滚语义是显式的。额外的同步重入保护
阻止 CPU RDR 读取在 reservation 期间偷走队首字节。

直接 guest gate 是 `tools/run-uart-rx-reservation-smoke.sh`：先在 DMAR 开启前将 host
字节排入 UART，再执行非法目标传输并检查 RXNE/NDTR/EN/TEIF 保持，最后将同一 stream
重新配置到有效 RAM，检查字节只提交一次。现有 QEMU UART focused test 继续覆盖 TX
backpressure；收尾的完整 QEMU smoke suite `90/90` 和 Host CTest `54/54` 均通过。本
consumer 不宣称 FIFO 多 beat reservation、目标端部分副作用回滚、UART/DMA 联合迁移或
物理串口时序。

# 2026-09-05 SPI RX direct P2M reservation review

本轮继续停留在 STM32H723 SPI 数据层到 DMA direct endpoint 的直接 consumer 边界。首个
错误状态是 SPI transfer 已将响应写入 RXDR 后，旧 DMA 路径在目标内存事务前立即消费该
单槽结果；非法目标因此会留下 NDTR/地址未推进，却丢失 RXDR 字节。

修复让 SPI RX endpoint 提供完整 reservation tuple：prepare 复制 RXDR 并记录保留值，
commit 只在 `dma_memory_write()` 返回 `MEMTX_OK` 后清除 `RXP`/RXDR，abort 只释放
reservation。同步 CPU RXDR 访问在 reservation 期间不会消费队首。DM-MC02 只在匹配的
SPI RX DMA controller/stream 重新 enable 时触发一次重试，DMA 的地址、仲裁和 TEIF/EN
状态仍由 DMA 层拥有。

变更文件：`qemu/upstream/hw/arm/dm_mc02_spi.[ch]`、`dm_mc02.c`、新增
`smoke/dm_mc02_spi_rx_reservation_smoke.c` 和 `tools/run-spi2-rx-reservation-smoke.sh`，
以及工作区/项目约束和契约文档；未修改 `trobot/`。

验证：SPI RX reservation guest smoke、SPI2 DMA on/off 回归和 `qemu-system-arm` 目标重链
均通过。本轮只覆盖同步单 beat/单 RXDR 槽；SPI RX FIFO、多 beat reservation、overrun
replacement、目标内存部分副作用回滚、SPI/DMA 联合迁移和物理 SPI 位级时序仍是后续边界。

# 2026-09-05 ADC1 `ADC_DR` direct P2M reservation review

本轮继续停留在 STM32H723 ADC 数据面到 DMA direct endpoint 的直接 consumer 边界。首个
错误状态是 ADC regular 转换已经置位 `ISR.EOC` 后，旧 DMA 路径在目标内存事务前立即
消费 `ADC_DR`；非法目标会令 DMA 置 `TEIF`/关闭 stream，却丢失仍应可重试的 ADC 结果。

修复集中在 ADC producer/endpoint boundary：`read_prepare` 只复制一个 `ADC_DR` beat
并建立同步 reservation，不清除 `EOC`；DMA 目标写成功后 `read_commit` 复用普通 ADC
数据读取路径清除 `EOC`，失败则 `read_abort` 只释放 reservation。DM-MC02 的 DMA1
stream-enable wiring 作为 level-like request 的重试入口，但 ADC 端先校验 DMA/DMAMUX
wiring、完整 reservation tuple、DMA 配置和 `EOC`，不生成第二次转换。DMA 仍拥有 stream
选择、地址、`NDTR`、`TEIF`/`TCIF` 与 IRQ 状态。

变更文件：`qemu/upstream/hw/arm/dm_mc02_adc.[ch]`、`qemu/upstream/hw/arm/dm_mc02.c`、
新增 `smoke/dm_mc02_adc_rx_reservation_smoke.c` 和
`tools/run-adc-rx-reservation-smoke.sh`，以及 ADC direct-consumer 约束/接口/架构文档；
未修改 `trobot/`。新 smoke 显式写入 `SQR1` 选择一个真实 rank，避免板级旧的双样本
compatibility fixture 让重试测试误判为产生了第二个样本。

验证：`tools/run-adc-rx-reservation-smoke.sh`、`run-adc-dma-smoke.sh on`、
`run-adc-dma-smoke.sh off` 均通过；`ninja -C build/qemu qemu-system-arm` 增量重链通过；
最终串行 QEMU smoke suite `92/92` 与 Host CTest `54/54` 均通过。
本切片只覆盖同步 direct P2M 的单一 ADC1 `ADC_DR` slot，不宣称 ADC FIFO 或 ADC12_COMMON
`CDR/CDR2` 多 beat reservation、目标内存部分副作用回滚、ADC/DMA 联合迁移或物理 ADC/DMA
时序。

# 2026-09-05 ADC12 common CDR direct P2M reservation review

本轮继续停留在 STM32H723 ADC common 数据面到 DMA direct endpoint 的单一边界。首个错误
状态不在 DMA 寄存器，而在 CDR 的 read acknowledgement：endpoint route 将一个临时复制的
CDR word 交给 DMA 后，只要 `dm_mc02_dma_request_endpoint()` 返回“request handled”便立即
调用 `notify_cdr_read()`。DMA 对非法 target 会置 TEIF 并停止 stream，同时仍返回已处理；结果
是 CDR producer 已被确认、两路 EOC 被清除而 NDTR/目标内存未推进。

修复在 ADC common producer 本身建立单槽 `cdr_valid` 和协议中立的
`prepare/commit/abort` API。prepare 只复制 word；commit 只由 direct DMA `MEMTX_OK` 调用，
并成为调用既有 CDR read acknowledgement 的唯一 endpoint 路径；abort 保留 word/EOC。CPU
CDR read 在同步 reservation 期间也不会偷走 producer。DM-MC02 仅在 direct P2M stream-enable
时对 pending CDR 重投 existing request-9，DMA 继续拥有 matcher、地址、NDTR、TEIF/TCIF 和
IRQ。没有以 board/firmware 特判掩盖 source-consumption 错误。

`cdr_valid` 进入 ADC-common component VMState v6；v1–v5 load 清空新 latch，避免把旧流的
retained register value 伪造成可重试 item。`cdr_read_reserved` 保持 transient，normal/raw
save 都拒绝 active reservation。该 component record 不构成 machine migration 支持。

验证：`test-dm-adc-common` `16/16` 覆盖 source prepare/abort/commit、CPU-read exclusion、
v6 producer state 和 normal/raw save rejection；`dm-mc02-adc-test` `63/63` 的新 direct qtest
对 invalid CDR target 断言 TEIF/EN/NDTR/EOC 保持，随后 valid re-enable 精确搬运
`0x01000100` 并只确认一次 EOC。ADC DMA on/off、ADC reservation smoke、ADC pair/ADC VMState
回归均通过；最终 `qemu-system-arm` 重链、串行 QEMU smoke suite `92/92` 与 Host CTest
`54/54` 均通过；`trobot/` 未修改。

残余风险明确保留：legacy CDR MMIO consumer 在 target transaction 前读取并确认；CDR2 仍是
legacy endpoint；FIFO、多 beat reservation、目标端 partial side effect rollback、ADC/common/
DMA composite migration 和物理 ADC/DMA 时序未实现。下一步必须为 CDR2 建立独立的 producer、
reservation、source-specific acknowledgement 和直接测试，不能把 CDR 对双 EOC 的规则外推。

# 2026-09-05 ADC12 common CDR2 direct P2M reservation review

本轮首个错误状态仍是 DMA endpoint 的“request handled”被误认为目标写成功，但 CDR2 的
consumer 具有 source-specific acknowledgement，不能调用 CDR 的双 EOC 路径。修复在
ADC-common producer 建立独立的单槽 reservation：prepare 复制 `RDATA_ALT` 和 source，
commit 仅在 `MEMTX_OK` 后 ack 对应 ADC，abort 保留结果与 EOC。DM-MC02 只在现有 DMA1
request-9 matcher 的 stream-enable 边界重试，DMA 继续持有地址、NDTR、TEIF/TCIF 和 IRQ。

`cdr2_read_reserved` 与 reserved source 不进入 VMState；normal/raw save 在同步 reservation
期间拒绝。验证为 `test-dm-adc-common` `17/17`、`dm-mc02-adc-test` `64/64`；最终
`qemu-system-arm` 重链、串行 QEMU smoke suite `92/92` 与 Host CTest `54/54` 均通过。

残余风险：legacy CDR2 MMIO 仍在目标 transaction 前读取并 ack；本切片不覆盖 CDR2 FIFO/
multi-beat、目标 partial-side-effect rollback、ADC/common/DMA composite migration 或物理
DMA timing。下一步应继续以独立 producer/source-specific ack contract 推进，不能将该结果
扩展为整机 migration 支持。
# 2026-09-10 reuse audit

Removed the obsolete duplicate machine skeleton and routed the native-source
target to pinned QEMU `hw/arm/dm_mc02.c`. Consolidated migrated smoke tooling
on `tools/dm_mc02_qmp.py`; framing and capability negotiation now come from
QEMU. At this checkpoint several special scripts still had raw QMP helpers;
the later consolidation entry records their completed conversion.
# 2026-09-10 full project review

The previous QMP migration left two stale helper calls in `run-dm-motor-smoke.sh`
and `run-rs485-smoke.sh`; both now use the canonical `QmpSession` negotiation
and pass their direct smoke tests. Full Host CTest is green: 54/54, including
the 92-case QEMU smoke suite. Python tests remain 32 passed, 1 skipped.

Current status: the duplicate machine skeleton is removed, QMP framing is
centralized for migrated tools, and no QEMU process or `trobot` change remains.
At this checkpoint special scripts still owned raw QMP framing. The later
consolidation entry records their completed conversion. This review did not
change production device semantics or claim machine-level migration support.
# 2026-09-10 model-switch re-review

Rechecked the workspace and project constraints, canonical QEMU source, tool
ownership, generated-artifact residue, and progress records. The retained
`src/dm_mc02_machine.c` is the standalone capability probe compiled by the
Host project; it is not a second QEMU machine implementation. The obsolete
QEMU-side duplicate remains removed, and the native-source target points at
`qemu/upstream/hw/arm/dm_mc02.c`.

Current verification is green: Host build, Python tests (32 passed, 1
skipped), shell syntax, Python compilation, and CTest 54/54 including the
92-case QEMU smoke suite. QEMU environment check confirms pinned upstream
v8.2.2, commit `11aa0b1ff1`, and the built `dm-mc02` machine. No active QEMU
process, temporary reject/original file, or `trobot` modification was found.

At this checkpoint the 3x60-second real-time gate and special-fixture QMP
conversion were still open. The following entry records both as completed.
No machine-level migration claim is made.
# 2026-09-10 QMP consolidation and current performance gate

Removed the remaining hand-written QMP greeting, JSON-line framing, response
matching and event skipping from every tool consumer, including the firmware
RTF collector. `tools/dm_mc02_qmp.py`, backed by pinned QEMU v8.2.2, is now the
single tooling boundary for QMP negotiation, framing, IDs, events and errors.
The individual scripts retain only their domain-specific commands and checks.

All eight formerly special smoke scripts pass their narrow regressions. The
collector's 1-virtual-second check passed at 1.000136x with zero watchdog
timeouts. The standard three-run 60-virtual-second Release gate then passed:
RTF 0.999984, 0.999995 and 1.000000x; startup latency 0.522994..0.523777 s;
CPU 115.70..115.73%; RSS 50264..50520 KiB; IWDG timeouts=0 throughout.
Final Python tests pass 32 with 1 skipped, Host builds cleanly, shell/Python
syntax checks pass, and CTest passes 54/54 including QEMU smoke 92/92.

This is a tooling/reuse slice. It does not alter device behavior, firmware,
component VMState formats or machine-level migration support.
