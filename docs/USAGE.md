# DM-MC02 QEMU 使用手册

本手册说明如何获取、构建、运行和验证项目，以及排查常见环境问题。命令均从仓库根目录执行；固件 ELF 由用户另行提供。

板级状态可通过原生 QMP 读取，无需检查固件内存：

```json
{"execute":"qom-get","arguments":{"path":"/machine","property":"board-state"}}
```

返回 schema 1 对象：`timestamp_ns` 是 QEMU 虚拟时间，`gpio` 包含 A–H 的
`moder`、`sampled`（与 IDR 相同）、`odr` 和 `output_mask`；`key` 给出 profile 引脚、
active-low 按下状态和采样值，`led` 给出引脚、RGB、亮度和来源，`vin_mv` 给出配置电压。
该属性只读，读取不会推进时间或消费外设状态。输入仍使用 `qom-set` 的 `user-key`
布尔值或 `gpio-input` 字符串，例如 `A15=0`。

LED 的 `ws2812-dma` 来源是既有 DMA 波形观察近似；`gpio-latch` 来源将 LED 引脚 ODR
映射成白/黑，适合初期调试。它不表示实际引脚电气驱动，应结合 GPIO mode 和 output mask
检查。AF/analog、电气波形和全局 CPU/plant 锁步仍未由这个接口验证。

Python 自动化脚本可复用 `sim.boards.QemuBoard(qmp)`，其中 `qmp` 是现有
`tools.dm_mc02_qmp.QmpSession`。`snapshot()` 校验原生对象；`digital("gpio.B0")`、
`digital("key")` 读取采样值，`digital("led")` 判断 RGB 是否非零。`set_gpio("B0", 1)`、
`set_key(True)`、`set_vin(24000)` 注入公开输入，`pause()`/`resume()`/`reset()` 使用
原生 QMP。调用方负责 QMP 的期限和关闭；这些 API 不判断应用是否已启动。

外部测试与工具共用 `tools.dm_mc02_test_harness.running_qemu()` 的进程、私有 `/tmp`
socket 和有界清理。socket 出现只表示 transport 可连接；固件 ready 必须另取实际应用事件。

`sim.boards.channels.LabChannels` 可借用既有 UART、CAN 和 co-sim 连接：UART 使用无帧
`StreamTransport`，CAN 使用固定 84 字节 `CanStream` 与 CAN Fabric，ADC1 使用 v1
`FramedSocket`。它不打开或关闭连接，也不推进 CPU 或 plant。`service(timestamp_ns)`
每个连接只读一次有界数据；CAN 观察保留原始 QEMU 时间，UART 时间只是调用者的轮询样本。
需要截止时间判断时，应在读取后再查询 QMP 虚拟时间作为观察上界。
已有独立 UART TX owner 时可调用 `service(timestamp_ns, flush_uart=False)`，
继续读取并报告 UART 字节，同时保留其发送队列和后缀；TX owner 负责发送与 flush。
该参数必须为布尔值，默认仍服务 UART 发送；CAN 和 ADC 服务保持各自原语义。

ADC1 支持 raw16 或 0–3.3 V；先读取连接开场帧，再显式 `reset(time)`，输入时间严格递增。
收到 QEMU RESET 后必须重新显式 reset。逻辑 reset 保留物理流尾部，不提供 CAN/v1 历史输入隔离。
可运行 `tools/run-lab-channels-smoke.sh` 验证真实固件 UART/CAN/ADC 与按键驱动 LED；
该裸机测试夹具是数字模型消费者，不代表实板校准或完整 HAL 初始化。

Lab 配置使用锁定的 PyYAML 6.0.3；运行 Python 工具前执行 `uv sync --frozen`。
`sim.lab.load_manifest(path)` 读取 schema 1 YAML，固件路径相对 manifest 目录；时间使用
整数 `ns`/`us`/`ms`/`s` 字符串。省略 `duration` 时取最大的 stimulus `at` 或 assertion
`before`。未知字段、重复 key、错误类型和不支持的端点直接拒绝；不支持 YAML anchor/alias。
配置文件最多 1 MiB，stimulus/assertions 各最多 1024 条，文本最多 4096 UTF-8 字节，
虚拟时间最多 3600 秒。`at=duration` 可做最终发送，响应没有额外等待窗口。

`LabExperiment` 只消费实际数字快照、UART 字节与 CAN 帧；`before` 为严格截止时间。
UART 判断用读取后 QMP 上界，CAN 用原始 wire 时间，数字用快照时间；观测较晚可能保守失败，
失败后不会被迟到数据改成通过。串口匹配只保留最小尾部，不缓存完整运行记录。

## Virtual Lab

先安装固定 Python 依赖，再运行实验：

```bash
uv sync --frozen
uv run tools/sim lab run path/to/lab.yaml --report build/lab-result.json
# 验证公共示例，自动编译专用裸机测试固件：
nix develop -c bash tools/run-lab-channels-smoke.sh
```

[board-io.yaml](../examples/labs/board-io.yaml) 展示 UART、按键/LED、GPIO、CAN 和 ADC1。
`firmware` 相对 YAML 目录，普通固件应填写自身实际输出的 `ready` 文本；省略 ready 时以
QEMU 首次恢复前的虚拟快照作为时间起点，不判断应用已就绪。显式 ready 的时间起点取读取
该串口批次后的 QMP 上界，可能晚于真正的 ready 字节。实验只连接配置引用的端点。

```yaml
schema: 1
name: uart-check
board: dm-mc02
firmware: build/student.elf
stimulus:
  - {at: 100ms, endpoint: uart1, bytes: hello}
assertions:
  - before: 200ms
    uart_contains: {endpoint: uart2, text: received 5 bytes}
  - before: 250ms
    digital_equals: {endpoint: led, value: 1}
```

UART 端点为 `uart1/2/3/5/7/10`，CAN 为 `can1/2/3`。输入还支持 `key`+`pressed`、
`gpio.A0`+`value`、`adc1.ch4`+`raw` 或 `voltage_uv`，CAN 使用 `frame` 的 `id/flags/dlc/data_hex`。
CAN 断言 `can_seen` 要求 `endpoint/id`，可再指定精确 flags、DLC 和完整 data_hex；输出 payload
较短时沿用既有 wire 零填充。UART 文本使用 UTF-8。ADC2 注入和自动 IMU 环境尚未支持。

`--qemu` 覆盖 QEMU 路径；`--timeout` 默认 30 秒，`--startup-timeout` 默认 10 秒，都从
启动前开始计入。QMP 连接和实际 ready 等待共用 startup 期限，不能以 socket 出现代替 ready。
输入在观测到 QEMU 时间到期后发送，存在轮询延迟；报告保留计划时间和发送时的观测样本。
串口显示 polling sample，CAN 显示原生 wire 时间。实验失败、进程退出、中断或清理失败都非零。
这些检查证明声明的数字边界，不建立 CPU/plant 全局锁步或物理真实性。

```bash
# Lab 内部 monitor 与用户 QMP 独立；GDB 使用 QEMU 原生 server：
uv run tools/sim lab run lab.yaml --qmp /tmp/student-qmp.sock --gdb-port 1234 --paused
# 在 startup 期限内从 GDB 恢复，或增大 --startup-timeout/--timeout：
arm-none-eabi-gdb build/student.elf
# (gdb) target remote 127.0.0.1:1234
# (gdb) continue
# 原生 UART terminal、monitor mux、GDB 和 QMP 仍可直接使用：
uv run tools/sim board run --elf build/student.elf --debug --qmp /tmp/student-qmp.sock
```

从仓库根目录可用 `uv run python -m sim`；`tools/sim` 也可从任意目录启动，或将 `tools`
加入 PATH 后直接使用 `sim`。`board run` 原样调用现有 launcher，不重建调试协议。

交互入口复用同一运行环境，默认连接 UART1，按需要增加 CAN 和 ADC：

```bash
uv run tools/sim lab interactive --elf build/student.elf --uart uart1 --can can1 --adc
```

连接提示只说明 transport 已连接。输入 `help` 查看命令；`state` 显示原生只读 JSON：

```text
key press
key release
gpio B0 1
vin 24000
uart uart1 "hello world"
can can1 0x321 0 8 4142434445464748
adc ch4 voltage_uv 1650000
adc ch4 raw 12345
pause
cont
quit
```

文本支持引号，按 UTF-8 原样发送，不展开 shell。只允许已配置的 UART/CAN/ADC 端点；
暂停时 ADC 输入明确拒绝，先 `cont` 再输入。命令错误会显示并继续，真实连接、运行或清理
错误返回非零。输入使用 pipe 或 TTY，行最多 4096 字节，超长行丢弃到换行；不支持重定向
普通文件作为 stdin。EOF 和 quit 会清理退出。默认总期限 300 秒，调试可用 `--gdb-port`、
`--paused` 和 `--qmp`，期限可显式调整。当前不提供交互 reset 或自动 IMU 环境。

Lab smoke 同时验证实际 GDB attach/detach 与第二 QMP monitor；需要 `arm-none-eabi-gdb`
或 `gdb-multiarch`，可用 `GDB_ARM` 覆盖。它验证数字固件消费者，不代表实板准确度。

## Robot manifest

`sim robot run` 将一块 DM-MC02、FDCAN1、一条 CAN 总线、四个 M3508 与既有 MuJoCo
四轮模型组合运行，可同时启动上位机程序。先在构建 QEMU 的环境中安装 MuJoCo extra：

```bash
uv sync --frozen --extra mujoco
uv run --extra mujoco tools/sim robot run robot.yaml --duration 5 --report build/robot-result.json
# 自动编译既有裸机测试固件，验证实际进程组合与失败清理：
nix develop -c uv run --frozen --extra mujoco bash tools/run-robot-smoke.sh
```

例如将下面的 `robot.yaml` 放在仓库根目录，将固件路径改成自己的 ELF：

```yaml
schema: 1
robot: {name: four-wheel}
boards:
  main: {type: qemu, board: dm-mc02, firmware: build/firmware.elf}
buses:
  drive: {type: can}
links:
  - {from: main.fdcan1, to: drive}
devices:
  fl: {type: dji-m3508, bus: drive, id: 1, joint: wheel_fl}
  fr: {type: dji-m3508, bus: drive, id: 2, joint: wheel_fr}
  rl: {type: dji-m3508, bus: drive, id: 3, joint: wheel_rl}
  rr: {type: dji-m3508, bus: drive, id: 4, joint: wheel_rr}
physics: {backend: mujoco, model: models/four_wheel.xml}
```

固件和模型路径相对 YAML 所在目录。schema 1 固定支持上述单板、单总线、单物理后端及
ID 1–4 的 joint 对应关系；标签可改，接线和身份必须一致。未知字段、重复 key、错误类型、
YAML anchor/alias 及资源超限都拒绝。模型的 joint/actuator/IMU binding 由 worker 在
连接 co-sim、发送 RESET 前校验。模型尺寸、质量、摩擦和电机参数是未标定的 toy profile。

固件若输出实际应用就绪标记，可增加以下字段：

```yaml
ready: {endpoint: main.uart1, text: "READY\n"}
upper_computers:
  observer: {command: [./observer, literal-argument]}
```

`ready` 支持 `uart1/2/3/5/7/10`，必须与实际固件相符；匹配后用该批字节读取后的 QMP
虚拟时间上界作为 duration 起点。省略时报告 `not_waited`，起点是恢复前的 QEMU 快照，
不验证应用 ready 或 plant 已推进。`--duration` 默认 5 个 QEMU 虚拟秒，范围为 1 ns–3600 s。

上位机 `command` 是原样 argv，不展开 shell；程序在配置准入时解析为绝对可执行路径，
工作目录固定为 YAML 所在目录，其余参数原样保留。最多 8 个上位机，每个最多 128 个参数、
单参数 4096 UTF-8 字节、总计 16 KiB（含终止符及解析后的程序路径）。相对 PATH 项也按该
工作目录查找。上位机可使用自身已有的标准接口；配置不自动提供 CAN、UART 或视觉订阅。

worker 独占 co-sim 与 FDCAN1，运行既有 v2、1 kHz、realtime 四轮 profile；父进程只观察
原生 QMP 和 ready UART。`--qemu` 覆盖二进制；`--startup-timeout` 默认 10 秒、
`--timeout` 默认 30 秒，两者从启动 QEMU 前开始计入。startup 包含连接、子进程启动及
实际 ready 等待；active host 期限包含 startup，但不包含各资源独立的有界清理。进程退出
（包括上位机提前返回 0）、超时、中断及清理失败均非零，成功报告仅在清理结束后发布。
报告保留每个子进程 stdout/stderr 的采样尾部，各最多 12000 字节；持续输出可能遇到 pipe 背压。

`--qmp /tmp/robot-qmp.sock` 提供独立的原生 QMP monitor，路径须不存在、父目录须存在、
总长度最多 100 字节；正常退出由 QEMU 清理该 socket，强制 KILL 后可能留下路径，重试前
应核对并删除旧 socket。`--gdb-port 1234` 使用原生 GDB；Robot 自动恢复运行，没有
`--paused` 模式。GDB 暂停仍受 worker ACK 与 host 期限约束。

本入口验证进程组合与声明的运行边界。CPU、worker 和 plant epoch 各自独立，UART ready、
ACK/DONE 和 realtime pacing 不建立全局时间屏障。实际四轮固件控制见下文 ChassisTest；
持续四轮实时性能、实板标定、多板、DR16、裁判系统和赛场组合仍未由该入口支持。

## Arena 配置准入

`sim.arena.manifest.load_manifest()` 可读取固定双底盘的 schema 1 配置，复用两个
完整的 Robot 配置。将下面的 `arena.yaml` 放在仓库根目录：

```yaml
schema: 1
arena: {name: two-four-wheel}
world: {backend: mujoco, model: models/two_four_wheel.xml}
robots:
  west:
    manifest: robots/bravo.yaml
    prefix: bravo/
    physics: shared-world
  east:
    manifest: robots/alpha.yaml
    prefix: alpha/
    physics: shared-world
```

两个子文件使用上文的 Robot schema，各自的 `physics.model` 必须解析到本仓库的
`models/four_wheel.xml`；例如在 `robots/alpha.yaml` 中写 `../models/four_wheel.xml`。
固件路径和上位机工作目录仍由子文件目录决定，argv 参数原样保留。ready 保留各自
实际 UART 标记，省略时仍为 `None`。局部板、总线和电机标签可以相同；上位机合计最多 8 个，
重复引用同一 Robot 文件也分别计数。

```python
from sim.arena.manifest import load_manifest

arena = load_manifest("arena.yaml")
for member in arena.robots:  # 固定按 alpha/、bravo/ 返回，保留 east/west 标签
    print(member.name, member.prefix, member.robot.board.firmware)
```

当前只接受上述两个前缀、显式 `shared-world`、canonical 双底盘世界与原始四轮模型。
指向 canonical 文件的符号链接可用，复制文件或自定义模型会被拒绝；路径一致仅选择
固定 profile，模型 binding 仍由 native consumer 校验。准入返回不可变记录，只读取配置，
不启动 QEMU、上位机或物理后端。已验证准入数据驱动同一个 MuJoCo 世界、两条独立
CAN 总线和八个 M3508 的直接 consumer。

可选 `referees` 先提供裁判配置准入，每个成员最多一份，键使用已有的成员名称：

```yaml
referees:
  east:
    uart: main.uart7
    initial:
      game: {game_type: 1, game_progress: 2, stage_remain_time: 1, sync_timestamp: 0}
      robot: {robot_id: 3, robot_level: 2, current_hp: 100, maximum_hp: 200,
              cooling_per_s: 3, heat_limit: 100, chassis_power_limit: 80, power_outputs: 5}
      power_heat: {buffer_energy_j: 40, heat_17mm: 10, heat_42mm: 20}
      buff: {recovery_percent: 5, cooling_per_s: 9, defence_percent: 7,
             vulnerability_percent: 11, attack_percent: 13, remaining_energy: 254}
    durations: {countdown_s: 3, battle_s: 300}
    cooling_42mm_per_s: 2
    events:
      - {offset_ns: 500000000, hit: {damage_hp: 30}}
```

`uart` 必须精确对应该成员子配置的板名称和 `uart7`。初始四组和时长必须完整，
数值沿用裁判公共类型的校验；`sync_timestamp` 明确填写，不从世界时间推导。
`events` 默认空，每项包含 `offset_ns` 及 `hit`、`shot`、`game`、`robot`、
`power_heat`、`buff` 中恰好一种完整数据；至多 1024 项，偏移非递减，同时间保持原顺序。
`cooling_42mm_per_s` 默认零。返回的 `arena.referees` 是按成员顺序排列的不可变记录。
准入不创建裁判引擎或 UART endpoint；运行时的 ready 条件与发布方式见下文。
省略 `referees` 继续使用原运行路径。

命令行运行同一配置：

```bash
uv run --frozen --extra mujoco python -m sim arena run arena.yaml \
  --duration 1 --report build/test-results/arena.json
```

`tools/sim arena run` 使用相同入口。`--qemu`、`--startup-timeout` 和 `--timeout`
分别选择 QEMU 和设置主机期限；默认值及参数可用 `arena run --help` 查看。
UART 文本带成员名称，例如 `UART east.uart1 poll_sample_ns=... b'...'`，字节以
转义形式显示。`poll_sample_ns` 属于该 QEMU 的采样时间；结果中的 `origin_ns`、
`end_ns` 是共享世界时间。JSON 包含各板独立的 ready 状态和时间，省略 ready
仍为 `not_waited`。运行、清理或报告写入失败返回非零；中断返回 130，并保留错误诊断。

可通过 Python 运行这两个配置成员，共享一个世界 owner：

```python
from sim.arena.manifest import load_manifest
from sim.arena.runtime import run_manifest

result = run_manifest(load_manifest("arena.yaml"), duration=1.0)
print(result.origin_ns, result.end_ns, result.boards)
```

使用上文的 MuJoCo extra 和已构建的 QEMU。`qemu` 可显式覆盖二进制。
`startup_timeout` 默认 10 秒、`timeout` 默认 30 秒，从启动第一进程前计入；
后续 QEMU、连接、上位机和 ready 等待使用同一期限。模型 binding 在进程启动前准入。
运行层直接使用两组原生 QMP/co-sim/CAN/UART 连接和唯一 shared-world runtime，
上位机保持各子配置的原样 argv、工作目录和独立进程组。

`duration` 是所有声明的 UART ready 标记匹配之后的共享世界时间，范围 1 ns–3600 s；
省略所有标记时从世界时间 0 开始。ready 等待期间继续提供世界输入，匹配后不再次 reset。
每次世界推进 1 ms，最后一段使用精确余量。结果分别保留每块 QEMU 的 RESET 原点、
ready 读取后时间上界和最后观察时间，不能将这些时间与共享世界 epoch 混用。
`emit` 可接收带成员标签的 `ArenaEvent`，`on_response` 可观察实际 co-sim 响应；
响应观察不构成完整 wire trace，也不保证每步样本消费。

配置裁判时，对应子 Robot 必须声明实际应用 `ready` 标记；该标记应由固件在
UART7 接收初始化后输出。每个成员匹配自己的标记后，发布裁判的最新完成状态，
随后默认每 100 ms 共享逻辑时间发布一批四命令快照。Python 调用可用
`referee_period_ns` 指定 1 ms–60 s 的整数周期；既有 world tick 将发布时刻向后
量化至多 1 ms。这是快照发送策略，未实现官方各命令的独立周期。

脚本仍从共同 RESET 原点计算，首次发布可能已包含 startup 期间的事件；ready
不会重置世界或裁判。UART 观察只读，发送和后缀 flush 由 SharedWorld 独占。
队列容量不足会终止本次 Arena 运行，保留原物理后缀；不会重步或丢弃旧快照。
结果的 `referees` 是不可变的 `ArenaRefereeResult` 记录，包含成员、UART、
`period_ns`、快照准入次数 `publications`、首末共享时间
`first_logical_ns`/`last_logical_ns` 和下一个发送序号 `next_sequence`。
这些记录不证明固件实际消费或 CPU 时间屏障。

提前退出（包括退出码 0）、期限、RESET/EOF、QMP、transport、solver、输出或清理错误
均使本次运行失败。成功只在独立清理世界、上位机、连接和两组 QEMU 后返回。
native 准入及各资源的有界清理不计入 active host 期限，阻塞 native 调用仍需外层 timeout。
该固定 profile 尚不提供独立 QMP/GDB 参数、wall-clock pacing 或 CPU 时间屏障。

实际进程与数据交付的验证入口为：

```bash
uv run --frozen --extra mujoco bash tools/run-qemu-shared-world-smoke.sh --arena-runtime
```

该默认关闭的夹具生成两份 Robot 配置、一份 Arena 配置和专用 bare-MMIO 固件，
唯一执行入口是上述 `run_manifest()`。两个固件各自在完成 CAN/SPI 读取及最终
sensortime 写回后输出实际 UART ready；随后共享世界运行 50 ms。报告保留具名
SI 样本、实际 float32 STEP、八路 CAN 反馈、DONE masks、原始 UART、独立 QEMU
时间和进程清理结果，并验证上位机的原样 argv 与工作目录。raw 数据包含实际
滤波历史；独立首样本量化验证仍由 `--two-chassis` 门完成。

追加 `--reset`、`--peer-exit` 或 `--upper-exit` 可分别验证单板复位、单板退出和
上位机退出。负例返回非零，保留 failed JSON、停止步进和资源回收证据；QMP
断连可能先于进程退出检测，清理错误也保留在报告中。缺少 MuJoCo 或 ARM
工具链时明确返回 SKIP 77。该夹具尚不证明 HAL/BSP 闭环或实物标定。

命令行入口与 JSON 报告的原生验证使用同一夹具：

```bash
uv run --frozen --extra mujoco bash tools/run-qemu-shared-world-smoke.sh --arena-cli
```

它在既有 smoke 进程中执行标准 `sim` 模块入口，保留测试观察以核验单次 runtime
调用、实际参数、输出与完整报告。该门属于带观察的模块运行。`--reset`、
`--peer-exit` 和 `--upper-exit` 继续保留实际失败报告；`--interrupt` 发送 SIGTERM，
要求返回 130，并验证世界、QEMU 与上位机已清理，关闭后的 owner 拒绝再次步进。

## Referee protocol

已有共享世界 owner 可以组合具名裁判引擎和 UART endpoint：

```python
from sim.robots.shared_world import RefereeBinding, SharedWorldRuntime
from sim.devices.referee import ScriptEvent, Hit

world = SharedWorldRuntime(plant, boards, referees=(
    RefereeBinding("east", east_engine, east_uart_endpoint),
    RefereeBinding("west", west_engine, west_uart_endpoint),
))
world.start(session, wire_sessions, deadline=deadline)
world.queue_referee_events("east", (ScriptEvent(0, Hit(5)),),
                           session_id=session.session_id)
world.step(1_000_000, deadline=deadline)  # 由原有步进处理到期事件
# 调用者先观察相应固件的实际 UART7 ready 事件，再发布：
wire = world.publish_referee("east", deadline=deadline)
world.service(deadline=deadline)  # 发送已排队的 suffix，不推进世界
```

`plant`、板绑定、引擎和 endpoint 由调用者预先创建；构造成功后，world 拥有 plant、
引擎及 endpoint 的执行和清理，socket 与 byte transport 仍由调用者清理。
所有引擎使用同一个 Session 和 StepContext，`world.step()` 推进它们；发布只消费
已完成状态。启动及步进不会自动发布。启动保留 endpoint 当前序号及既有排队字节，
不能用逻辑 reset 清除旧 UART 数据。

`queue_referee_events(name, events, session_id=...)` 只向具名引擎交付动态事件，
使用 host Session ID 和相对 reset 的偏移，沿用引擎的整批准入、顺序及 1024 项
pending 限制。该调用不推进时间或发布 UART；名称、输入、session、顺序或容量
拒绝均保留原状态，可以随后正常准入及 step。启动前、关闭后、执行失败后或
活动回调中的调用会被拒绝。调用者负责事件来源和去重。

配置消费者可调用 `sim.arena.manifest.parse_referee_events(value, label=...)`，
将与 Arena `events` 相同的列表转换为不可变 `ScriptEvent` 元组。每项必须包含
`offset_ns` 和一个 `hit`、`shot`、`game`、`robot`、`power_heat` 或 `buff` 值；
最多 1024 项，偏移非递减，等时顺序保留。该函数只解析和校验，不读取文件或
创建运行资源；成员及 session 由调用者校验。已有类型值可直接通过
`sim.devices.referee.validate_referee_events(events)` 校验，类型、范围和顺序规则
与引擎一致。

`sim.manifest.parse_time(value, label, zero=True)` 供配置消费者复用 Lab 的时间语法：
整数 `ns/us/ms/s` 字符串，最多 13 位十进制数字，结果不超过 3600 秒。
`zero=False` 拒绝零值；不接受浮点、负数或其它单位，不推进任何时钟。

Scenario 的首版文件准入接口为 `sim.scenario.load_manifest(path)`：

```yaml
schema: 1
arena: arena.yaml
duration: 1s
referee_events:
  east:
    - at: 100ms
      hit: {damage_hp: 10}
  west:
    - at: 100ms
      shot: {heat_17mm: 5, heat_42mm: 7}
```

`arena` 相对 Scenario 文件解析，目标必须是该 Arena 中已配置 referee 的成员。
`referee_events` 可省略；事件复用上面的六种 typed 值，合计最多 1024 项。
每个成员的 `at` 必须非递减且不超过 `duration`，等时顺序保留。完整文件校验后
返回不可变记录，可将各 `record.events` 交给共享世界的 `queue_referee_events`。

Scenario 也可声明 CAN 延迟动作，无需配置 referee：

```yaml
schema: 1
arena: arena.yaml
duration: 1s
events:
  - at: 500us
    action: can_delay
    target: east.chassis_can
    value: 5ms
  - at: 1s
    action: can_delay
    target: east.chassis_can
    value: 0ns
```

每项必须且只能包含 `at/action/target/value`，当前只接受 `can_delay`。
`target` 精确匹配 Arena 成员名与其 Robot 配置中的总线名，格式为 `成员.总线`；
不接受未限定的总线名、板卡 FDCAN endpoint 或其它成员的总线名称。
`at/value` 沿用整数 `ns/us/ms/s` 字符串，范围 0..3600 秒；
`at` 不超过 `duration`，整个列表非递减，同刻跨成员和重复动作保留输入顺序。
CAN 动作独立限制为 1024 项，不占用 referee 的 1024 项额度。
完整动作形状和值在读取引用的 Arena 前校验，实际成员与总线关联在配置加载后校验。
加载结果的 `can_delays` 是不可变 `ScenarioCanDelay(offset_ns, member, delay_ns)` 元组。
执行入口将这些记录按原顺序转换为下面 Arena Python 入口的动作元组，
由同一 Arena 调度器完成准入和执行；空列表保持原有调用方式。

`at` 从 shared host Session reset 起算；`duration` 沿用 Arena 在全部请求的 ready
之后计算的活动时长，两者的起点不同。启动期间的事件可以先改变裁判状态；
`at: 0ns` 的裁判动态事件在下一次成功 step 才生效。加载接口只读取配置。
通过 `sim.scenario.runtime.run_manifest(scenario)` 执行时，时长来自该配置，
裁判事件与 CAN 动作交给同一 Arena 执行器；返回现有 `ArenaResult`，资源清理完成后才返回。
命令行使用同一执行入口，时长由 Scenario 配置决定：

```bash
uv run --frozen --extra mujoco python -m sim scenario run scenario.yaml --report build/scenario.json
```

`--qemu` 可覆盖 QEMU 二进制；`--startup-timeout` 默认 10 秒，`--timeout`
默认 30 秒，沿用 Arena 的 host 期限和清理规则。UART 输出带成员标签并保留原始
字节表示，JSON 使用现有 `ArenaResult`。本入口不提供 `--duration`、独立 QMP/GDB
或暂停参数。CAN 延迟动作支持加载和执行；其它故障注入、assertions 和 replay 尚未支持。

Arena 的 Python 运行入口接受 `referee_events=((member, events), ...)`：
整批类型、具名 referee、顺序、偏移和合计 1024 项限制在创建资源前校验。
事件元组在共享世界 start 成功后、恢复 QEMU 或首次 step 前原样交付一次，
使用实际 host Session ID；任一运行时拒绝均终止整个 Arena 并清理资源。
队列与规则执行仍由既有裁判引擎拥有，发布继续遵循每个成员的实际 ready 和原周期。

Arena 的 Python 入口也接受 `can_delays=((offset_ns, member, delay_ns), ...)`，例如
`can_delays=((0, "east", 2_000_000), (1_500_000, "east", 0))`。
列表及各项必须为普通元组，最多 1024 项；成员必须准确匹配 Arena 名称。
偏移和延迟为普通无符号 64 位整数，偏移非递减且不超过运行时长，同刻按输入顺序执行。
完整列表在创建资源前校验；最大延迟可以配置，但后续帧时间相加溢出会终止运行。

偏移从实际 host Session RESET 原点计算，ready 等待期间也执行；
`ArenaResult.origin_ns` 仍是所有请求的 ready 达成后的活动时长起点。
零偏移动作在 referee 批次入队后、恢复 QEMU 前执行。
其余动作将原有最多 1 ms 的步进拆到准确截止时间，在成功 step 后、UART 发布或等待前执行。
同刻裁判事件已在该 step 内处理；偏移恰等于时长的动作也执行一次。
已入队命令和该 step 的反馈保留原截止时间，ACK 等待期间准入的输入也使用旧策略；
随后才读到的输入使用新策略。帧自带时间戳不选择延迟策略，另一成员的总线不受影响。
拆分会改变 native 积分区间及 STEP 观察次数，不能保证轨迹与未拆分时相同。
动作、存活检查或步进失败沿用整个 Arena 的终止及清理规则，保留先前效果。
Python、Scenario 和 CLI 共用上述调度与失败规则，均不建立 QEMU CPU 时间屏障、
wall-clock pacing 或物理 CAN 时序保证。

配置消费者可调用 `sim.arena.manifest.validate_can_delays(actions, duration_ns=...)`，
复用上述普通元组、成员字符串、无符号 64 位时间、顺序、额度和截止时间校验。
该纯函数不创建资源或查询拓扑，允许零时长校验范围；实际运行仍要求 1 ns..3600 秒，
成员与总线关联由调用者检查。Python 延迟值保持完整 64 位范围，YAML 值遵循 3600 秒配置上限。

快照容量不足抛出 `BufferError`，不提交新快照或序号；可服务旧队列后重试。
发布、步进或服务中的其它 IO、引擎、期限或存活检查错误冻结整个 owner，要求重新创建。
`close()` 独立尝试所有 owned endpoint 和 participant，保留清理异常，
不关闭借用的 transport。此接口已通过共享时间和实际 socketpair 字节消费测试；
单板共享路径已通过实际 RefereeTest UART7/HAL/RTOS 消费验证，入口见下文。
Arena YAML 的裁判配置使用上述 ready 与快照发布策略；
脚本状态也不代表根据世界碰撞进行实时裁决或实现官方独立消息发送周期。

裁判系统帧封装可独立使用，运行前执行 `uv sync --frozen`：

```python
from sim.devices.referee import encode_frame, decode_frame

wire = encode_frame(command_id=0x0201, payload=robot_status_bytes, sequence=0)
frame = decode_frame(wire)
assert frame.command_id == 0x0201
```

帧封装依据官方 RoboMaster 2026 通信协议 V1.1.0（20251217）的第 4 页及
第 77–82 页；官方版本入口见 [RoboMaster 文档](https://bbs.robomaster.com/wiki/20204847/811363)。
接口使用帧头 `0xa5`、小端 payload 长度、8-bit 序号、CRC8、
小端 command ID、payload 与 CRC16。CRC 算法来自锁定的 MIT `crccheck 1.3.1`，
CRC8 初值为 `0xff`，CRC16 初值为 `0xffff`，无最终 XOR。
当前支持完整帧最多 128 字节（payload 最多 119 字节）；这是公开的模型准入范围。
参数必须为整数或不可变 `bytes`，bool、越界、截断、多余尾部及 CRC 错误直接拒绝。

`payload` 保持不透明，调用者须按所选协议版本填入准确的数据。接口不判断 command
是否受固件支持，不自动递增序号，不解析连续 UART 流，也不拥有 UART/socket。
不能把帧校验通过作为规则正确或真实裁判系统的验证。

四种 outgoing payload 提供固定 2026 V1.1.0 schema：

```python
from sim.devices.referee import GameStatus, RobotStatus, PowerHeat, Buff, encode_message

wire = encode_message(GameStatus(1, 4, 300, 0), sequence=7)
# 0x0201：ID、等级、当前/最大 HP、冷却值、热量上限、功率上限、输出 bitmask
status = RobotStatus(3, 1, 200, 200, 10, 100, 80, 7)
wire = encode_message(status)
wire = encode_message(PowerHeat(60, 0, 0))  # J、17mm 热量、42mm 热量
wire = encode_message(Buff(0, 0, 0, 0, 0, 0x80))
```

比赛类型支持 1–5，阶段支持 0–5，剩余时间以秒填写；64-bit UNIX timestamp 必须显式
提供，不从 QEMU/plant 时间推算。`RobotStatus.power_outputs` 使用 bits 0/1/2 表示
gimbal/chassis/shooter；其余字段按官方 unsigned wire 宽度编码，不夹取数值，也不裁决
机器人 ID、HP、百分比或赛事规则。`PowerHeat` 的前 8 个保留字节固定输出零。

`Buff` 的 `remaining_energy` 是显式协议 bitmap，默认全量能量可填写 `0x80`；
编码使用官方第 17 页的 8-byte packed struct，最后字段位于 offset 7。
第 16 页表格将该偏移写为 6，与总长度/结构冲突；当前消费者结构也支持 offset 7。
这四类编码不包含接收 payload parser、定时发送或规则状态。
`0x0206` 等其它命令和赛场仍待独立接入。

独立脚本引擎可用于无赛场的状态回放和固件输入准备：

```python
from sim.core import Coordinator, Session
from sim.devices.referee import (
    RefereeEngine, RefereeState, MatchDurations, ScriptEvent, Hit, Shot,
)

initial = RefereeState(
    GameStatus(1, 2, 1, 0),  # 当前自检阶段剩余 1 秒；timestamp 显式填写
    RobotStatus(3, 1, 200, 200, 10, 100, 80, 7),
    PowerHeat(60, 0, 0), Buff(0, 0, 0, 0, 0, 0x80),
)
engine = RefereeEngine(initial, durations=MatchDurations(countdown_s=3, battle_s=300),
    events=(ScriptEvent(1_000_000_000, Hit(20)),
            ScriptEvent(1_000_000_000, Shot(40, 0))))
coordinator = Coordinator({"referee": engine})
coordinator.reset(Session(1))
coordinator.step(2_000_000_000)
state = engine.get_state()
assert state.robot.current_hp == 180 and state.power_heat.heat_17mm == 30
frames = [encode_message(message, sequence=i) for i, message in enumerate(
    (state.game, state.robot, state.power_heat, state.buff))]
engine.queue_events((ScriptEvent(2_000_000_000, Hit(5)),), session_id=1)
assert engine.get_state() is state  # 准入不修改已完成快照
coordinator.step(1_000_000_000)
assert engine.get_state().robot.current_hp == 175
coordinator.close()
```

事件必须为至多 1024 项的不可变 tuple，时间是相对每次 reset 的非递减纳秒偏移；
同时间保持输入顺序，零偏移事件在 reset 时执行。事件接受 `Hit`、`Shot` 或上述四种
消息；消息事件替换对应状态组。`Hit` 使用显式伤害值，HP 最低为零，输入 HP 不得超过
最大 HP；`Shot` 使用显式热量增量。17mm 冷却率来自 `RobotStatus.cooling_per_s`，
42mm 使用构造参数 `cooling_42mm_per_s`，默认零。热量保留分数并向下取整输出；
超过 65535 热量会拒绝整个步进并冻结，需 reset 后才能继续。`PowerHeat` 替换会重置
热量余量；`Buff` 只更新消息数据。getter 返回不可变的最近完成快照，不推进时间。

运行中的引擎可通过 `queue_events(events, session_id=...)` 接收动态事件，
复用同一 `ScriptEvent` 格式与校验。入口需要成功 reset、匹配的整数 session ID；
调用者负责来源时间、session 标识及去重，并顺序调用准入和步进。偏移不得早于
最近完成时间或最后排队事件，禁止自动重排。同时间静态脚本先执行，再按准入顺序
处理动态事件，冷却和阶段到期先于两者。等于最近完成时间的事件在下一次成功
step 的起点生效，已有快照保持可读。

每批至多 1024 项，pending 总数也至多 1024 项；整批准入失败保持原队列。
有效批次因总容量不足而抛出 `BufferError`，之后可以正常 step 消费到期事件并释放
空间。只有成功 step 删除已消费项；计算失败冻结引擎，保留最近快照和 pending。
成功 reset 或 close 清除动态事件，reset 仍从新原点重放静态脚本。
此入口接收显式输入；自动命中、射弹和赛场事件来源仍需单独接入。

配置工具可以先调用 `validate_referee_configuration(initial, durations=...,
events=..., cooling_42mm_per_s=...)`。该公共函数与引擎构造器使用相同校验，
成功时返回 `None`，不创建引擎或执行 reset/step，也不获取外部资源。

阶段 2→3→4→5 按绝对虚拟截止时间推进，当前剩余时间由 `GameStatus` 提供，后续
倒计时和比赛时长由 `MatchDurations` 明确配置。阶段 2/3/4 的输入剩余秒数与配置时长
必须为正，输出剩余秒数向上取整；阶段到期先于同时间脚本事件。其它阶段保留显式数据。
reset 会从新 session 原点重放原脚本，`sync_timestamp` 不随仿真时间自动改变。

热量计算窄移植自 [rm_control](https://github.com/rm-controls/rm_control/blob/c5205eb193a8cda6147efe760eafedd0a3ed0104/rm_common/include/rm_common/decision/heat_limit.h)
（BSD-3-Clause），阶段转换参考 [XDU referee mock](https://github.com/XDU-IRobot/rm_referee_ros2/blob/45da10a5317d47cc4b5d5968ec553946c43ebe0d/rm_referee_mock/rm_referee_mock/match_control_timing.py)
（Apache-2.0）；许可证随模块提供。配置时长和脚本数据是测试输入，不能作为官方赛事
规则常量。自动装甲/弹丸判定、过热扣血、复活、增益叠加和功率惩罚仍未实现；引擎不拥有
UART、Gazebo、物理时钟或发送序号，不建立 CPU/plant 全局锁步。

UART 输出可借用已有的非阻塞连接：

```python
from sim.transports import StreamTransport
from sim.devices.referee import RefereeUartEndpoint

# connected_socket 由调用者连接、设为 nonblocking，并负责关闭。
transport = StreamTransport(connected_socket, max_pending=16)
endpoint = RefereeUartEndpoint(transport, sequence=254)
wire = endpoint.send_state(state)  # 四帧，序号 254/255/0/1，共 82 bytes
assert endpoint.next_sequence == 2
endpoint.flush()                  # False 表示仍有未发 suffix
transport.drain(timeout=1.0)      # 可选，有界 host 等待；不代表 MCU 已消费
endpoint.close()                  # 不关闭、不 drain 借用的 socket
```

每份快照以 `GameStatus/RobotStatus/PowerHeat/Buff` 顺序排入一个 transport queue slot；
所有字段在任何发送前完成校验。队列满时 `BufferError` 拒绝新批次并保留序号，调用者
可 flush 后重试；容量检查可能已经发送旧队列的字节。其它发送/flush 异常可能发生在
部分发送后，endpoint 会冻结，禁止自动重发。显式 `reset(sequence=0)` 只恢复逻辑
序号和状态，保留原队列及 partial suffix；调用者须明确接受该逻辑流重启，协议本身
没有 session ID。失败后仍须由连接 owner 清理 socket，`close()` 只停用 endpoint。

该 endpoint 只输出显式快照，调用者负责完成的状态、发送频率、读取和连接生命周期，
并独占该裁判 TX 流的序号所有权。默认 transport 为 256 slots，纯快照最多保留
20,992 bytes，调用者可以设置更小上限。快照批次没有实现官方不同命令的独立周期
（`0x0001` 1 Hz、`0x0201/0202` 10 Hz、`0x0204` 3 Hz）；周期组合仍待接入。
真实固件 UART 消费可用下列专用测试验证。

默认关闭的专用固件测试复用实际 `robomaster::basic`、HAL UART7 RX DMA、BSP
回调与 FreeRTOS 消息执行器：

```bash
# 在 trobot 中，使用已有 ARM 工具链环境构建
cmake --preset RefereeTestDebug
cmake --build --preset RefereeTestDebug
# 在 dm-mc02-qemu 中，先完成 QEMU 构建及 uv sync --locked --group dev
tools/run-trobot-referee-test.sh ../trobot/build/RefereeTestDebug/trobot.elf \
  build/test-results/referee-debug ../trobot
```

可将 preset 换成 `RefereeTestRelease`。测试开关需要 `TROBOT_SELFTEST`，与 Motor、
Dji、Chassis 测试互斥，普通 Debug/Release 均保持关闭。结果目录必须不存在；
可用 `--qemu` 指定 QEMU。报告保留真实源码、ELF 和 QEMU 哈希、失败及缺失项，
只有进程清理完成且输入未变后才发布 `passed`。启动总期限为 10 秒，运行总期限为
20 秒，均从 QEMU 启动前起算，清理有各自的短期限。

固定脚本输出两份四命令快照，检查 17 个固件通过项和一个 `plant.physics` SKIP。
第二批先发送 19 字节，固件在 100 ms 内观察旧状态，然后主机补发原队列的 63 字节
尾部，固件检查四组新 payload 和新时间戳。它验证完整帧消费，不能证明 UART 前缀的
准确到达时间、操作系统背压、官方独立发送周期或 CPU/plant 全局屏障。当前 `0x0206`
真实一字节格式与固件两字节结构不符，保持未支持，测试不填充字节掩盖差异。

追加 `--shared-world` 可用同一个固件验证共享 owner 的裁判发布路径：

```bash
tools/run-trobot-referee-test.sh ../trobot/build/RefereeTestDebug/trobot.elf \
  build/test-results/referee-shared-debug ../trobot --shared-world
```

此默认关闭的单板夹具使用既有四轮 MuJoCo toy plant、CAN1 和 co-sim transport。
真实 RESET_ACK 后由 `SharedWorldRuntime` 初始化世界及裁判；固件实际 UART7 ready
事件触发初始快照，第二阶段由同一个 owner 推进逻辑时间，再发布第二批快照。
剩余 63 字节由原服务接口发送，保持原来的固件观察窗口和协议预期。
报告分别保留共享逻辑时间、QEMU ready/RESET/STEP 观测、原生世界调用和清理结果。
此夹具使用一次 1 秒的 MuJoCo step，不是 1000 次 1 ms 积分；它验证完成状态的
发布与消费，不能用于判断底盘运动精度。

该夹具用于检验实际 UART7/HAL/RTOS 消费边界，原 `plant.physics` SKIP 继续保留。
它尚不验证底盘固件闭环、双板 Arena 裁判接线、实物标定或 CPU 时间屏障；
MuJoCo 可选依赖需要先按环境准备中的说明安装。

双板周期消费者使用默认关闭的 `ArenaRefereeTestDebug` 或
`ArenaRefereeTestRelease` 固件，以及同一 runner 的 `--arena`：

```bash
# 在 trobot 中构建
cmake --preset ArenaRefereeTestDebug
cmake --build --preset ArenaRefereeTestDebug
# 在 dm-mc02-qemu 的 MuJoCo 环境中运行
tools/run-trobot-referee-test.sh ../trobot/build/ArenaRefereeTestDebug/trobot.elf \
  build/test-results/referee-arena-debug ../trobot --arena
```

该 profile 调用同一 Arena runtime，两个 QEMU 使用同一 ELF，分别消费 ID 3/103
的固定状态。各板在 UART7 初始化后输出 ready，检查三次完整新样本、四组
payload 及各组实际/前次 BSP 时间戳；报告保留缺失项和 `plant.physics` SKIP。
共享世界运行 1 秒，快照周期为 100 ms，startup/active host 期限为 10/20 秒。
固件的 1000 ms 等待属于各自 guest 时间，不能由此推断 CPU 与世界同步。
`--arena` 与 `--shared-world` 互斥，原握手及 partial-frame 测试仍使用原 preset。

定时输入的固件消费者使用默认关闭的 `ScenarioRefereeTestDebug` 或
`ScenarioRefereeTestRelease`，并选择 `--scenario`：

```bash
# 在 trobot 中构建
cmake --preset ScenarioRefereeTestDebug
cmake --build --preset ScenarioRefereeTestDebug
# 在 dm-mc02-qemu 的 MuJoCo 环境中运行
tools/run-trobot-referee-test.sh ../trobot/build/ScenarioRefereeTestDebug/trobot.elf \
  build/test-results/referee-scenario-debug ../trobot --scenario
```

夹具生成 Scenario 配置，通过同一 Scenario/Arena 入口运行。共同 reset 后
200 ms 对 east 注入伤害 30 HP、热量 5/7 和 Buff 替换；west 保持原状态。
两板首先验证完整初始状态，east 随后验证改变及刷新，west 在 host 已观察到
east 的完整改变报告后，通过既有应用握手验证两次原状态刷新；第二次观察要求
四组时间戳相对握手返回后读取的状态均已更新。握手只控制应用
观察，不改变世界、事件时间或发布周期。初始报告迟于事件、缺失或清理失败均失败。
该门沿用 100 ms 发布、1 秒活动时长和原 guest 等待；报告保留实际时间戳、
缺失项和物理 SKIP，不建立 CPU 时间屏障。三个 runner profile 互斥。

同一夹具也可验证实际 CLI 入口，在上面的运行命令末尾添加
`--cli`。它执行
`sim scenario run`，同时检查 CLI 的一次运行调用、返回结果和 JSON 报告。
该选项仅用于 Scenario 测试，沿用相同事件、固件及清理检查；入口在测试进程中
受观测，独立启动的 `python -m sim` 仍按前述普通使用命令运行。

## Contents

- [项目范围](#project-scope)
- [环境准备](#environment)
- [获取源码](#get-the-source)
- [构建](#build)
- [首次验证](#first-validation)
- [运行固件](#run-firmware)
- [Robot 配置](#robot-manifest)
- [裁判帧封装](#referee-protocol)
- [Flash 镜像](#flash-images)
- [串口、QMP 与 GDB](#serial-qmp-and-gdb)
- [外部仿真 worker](#external-simulation-worker)
- [测试与性能](#testing-and-performance)
- [更新与源码包](#update-and-source-package)
- [故障排查](#troubleshooting)
- [目录](#repository-map)

## Project scope

项目基于固定的 QEMU v8.2.2 fork，提供 STM32H723 与 DM-MC02 的数字外设模型，面向固件功能验证。

STM32H723 profile 为原生 NVIC 配置 ST CMSIS 定义的 4 位优先级；这使未经修改的
FreeRTOS 优先级探测与 HAL 分组设置一致。通用 QEMU 板卡保留原有默认值。该有限
边界不代表完整 CPU 优先级寄存器或整机 migration 已验证，也不提供实板验证。

项目不依赖 Renode，也不分发 `trobot/` 固件。模型不覆盖完整芯片、电气/物理总线时序或整机 snapshot/live migration。USB 的 DM-MC02 profile 使用 Device 角色，不提供该板的 USB Host 或设备透传。外部后端示例用于连接与协议验证；真实机构闭环和具体固件的完整业务就绪仍需用户验证。

## Environment

支持 **Linux x86_64**，推荐使用下面的 Nix 环境；也提供 Ubuntu 24.04 系统依赖方案。Windows、macOS 和 ARM 宿主原生构建未验证。

### Nix

按 [Nix 官方安装说明](https://nix.dev/install-nix.html)安装 Nix。若尚未启用 flakes，在 `~/.config/nix/nix.conf` 中加入以下配置，保留已有的其他设置：

```ini
experimental-features = nix-command flakes
```

获取源码后运行：

```bash
nix develop
bash tools/build-qemu.sh
```

也可以不进入交互 shell，直接构建：

```bash
nix develop --command bash tools/build-qemu.sh
```

`flake.lock` 固定 nixpkgs 的确切提交。Nix 提供 C/C++ 编译器、GLib、zlib、Python 3.11、Ninja、CMake、ARM guest 工具链、ripgrep 和 uv；无需为它们再安装 apt 包。Nix 默认下载可用的二进制缓存，缺少缓存时自行构建依赖。构建目录仍是本地 `build/`，此入口是 Nix shell，项目暂未提供 `nix build` 包。

构建和运行都在 `nix develop` 内执行，包括 QEMU、测试、worker 和 Python 环境同步。`nix develop` 中的 `PYTHON` 指向固定的 Nix Python；同步项目测试环境使用：

```bash
uv sync --locked --group dev --python "$PYTHON"
```

退出 shell 用 `exit`。更换机器后使用同一 `flake.lock` 可恢复相同工具和依赖版本。普通 Nix profile 中只有编译器并不足以构建本项目；请使用这里的完整 shell，使编译器、GLib、zlib 与 pkg-config 保持一致。MuJoCo Python 包通过项目 extra 按需安装；Nix shell 提供其 C++ 运行库，并默认使用 `MUJOCO_GL=disable` 运行无渲染后端。ROS 2 需要另行安装和配置。

### Ubuntu

以下最小依赖已在干净 Ubuntu Base 24.04.5 容器中完成从零构建与 machine 启动验证。

构建 QEMU 本身所需的系统包：

```bash
sudo apt-get update
sudo apt-get install -y \
  build-essential \
  pkg-config \
  libglib2.0-dev \
  zlib1g-dev \
  python3 \
  python3-venv \
  ninja-build \
  git
```

这里 `build-essential` 提供 C/C++ 编译器和链接工具；GLib、zlib 是当前 ARM QEMU profile 的必需开发库；`ninja-build` 提供构建后端；Git 用于检出仓库与固定的 QEMU 子模块。

`zlib1g` 只是运行库；编译所需的 `zlib.h`、链接文件和 pkg-config 元数据由 **`zlib1g-dev`** 提供。DM-MC02 构建脚本先检查元数据，再用所选 `CC`、`CFLAGS`、`LDFLAGS` 与 `PKG_CONFIG` 的 zlib 参数实际编译/链接探针；探针不执行，失败会在 QEMU configure 前显示编译器诊断。

在 Nix shell 外，两个 QEMU 构建脚本使用 `/usr/bin:/bin`，默认 `CC=/usr/bin/cc`、`CXX=/usr/bin/c++`、`PKG_CONFIG=/usr/bin/pkg-config` 和 `PYTHON=/usr/bin/python3`；在 Nix shell 内则保留 Nix 的工具与依赖环境。显式 `CC` 等覆盖值会保留，须使用与其匹配的开发库。

QEMU `configure` 会在 `build/qemu/pyvenv` 创建 Python 环境，并从源码携带的 wheel 安装固定的 Meson 1.2.3。Ubuntu 的 `python3-venv` 用于这一配置过程；Nix shell 已提供可用 Python，无需该 apt 包。项目级 Meson 命令通过 `tools/meson` 调用 QEMU 自己的版本。

因此，**只构建并运行 QEMU 不需要 `uv`**。`uv` 管理的是项目 Python 测试环境和可选 worker 依赖；当前 dev group 只安装 pytest，不重复安装 Meson 或 Ninja。需要运行完整回归门禁时，额外安装：

```bash
sudo apt-get install -y \
  cmake \
  ripgrep \
  gcc-arm-none-eabi \
  binutils-arm-none-eabi \
  gdb-multiarch
```

CMake/CTest 构建 Host 测试，ARM GCC/binutils 构建测试 guest，GDB 验证原生调试连接，ripgrep 是完整门禁检查项使用的命令。然后按 [uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装 `uv`，在仓库根目录创建锁定的 Python 测试环境：

```bash
uv sync --locked --group dev --python /usr/bin/python3
```

当前 profile 使用内部 FDT，并关闭 Capstone、slirp、文档及 SDL/GTK 图形界面，因此不需要对应系统开发包或 Sphinx。Pixman 是可选依赖：没有 `libpixman-1-dev` 时，QEMU 会关闭依赖 pixman 的通用显示设备；DM-MC02 的默认 headless 使用不依赖它。如需这些通用显示设备，可额外安装：

```bash
sudo apt-get install -y libpixman-1-dev
```

启用 MuJoCo 时再安装项目的可选 Python extra。首次同步 pytest/MuJoCo 或下载所需的 QEMU Meson wraps 需要网络，`/tmp` 必须可写。

## Get the source

```bash
git clone --branch main https://github.com/lym12321/qemu-dm-mc02.git
cd qemu-dm-mc02
git submodule update --init --depth 1 qemu/upstream
```

也可以使用 SSH 地址：

```bash
git clone --branch main git@github.com:lym12321/qemu-dm-mc02.git
```

`main` 保存共享模型、工具和文档；同一仓库中的 `dm-mc02/v8.2.2` 分支保存 QEMU fork，外层子模块固定到一个确切提交。`qemu.lock` 记录上游 v8.2.2 身份、fork 基线和 fork 提交；子模块 gitlink 与 `fork_commit` 必须一致。不要使用 `git submodule update --remote` 替代锁定版本。

首次构建不需要递归获取 ROM 子模块。**GitHub 的 main 源码 ZIP 不包含 QEMU 子模块源码，不能直接构建**；请使用 Git clone 并初始化子模块。通过本文快速命令获得的浅子模块足以构建和运行，源码打包时需要的完整历史见[源码包章节](#update-and-source-package)。

## Build

以下命令均从仓库根目录执行：

```bash
bash tools/build-qemu.sh
```

Nix shell 已包含测试所需的 CMake、ARM 工具链、ripgrep 和 uv；Ubuntu 用户请先安装上一节的额外依赖。构建 Host 测试程序：

```bash
uv sync --locked --group dev --python "${PYTHON:-/usr/bin/python3}"
cmake -S . -B build/host -DCMAKE_BUILD_TYPE=Release
cmake --build build/host --parallel 4
```

如果 `build/host` 之前使用了另一套工具链，CMake 会保留缓存中的编译器。先保留旧目录再重新配置，例如 `mv -T build/host "build/host.backup-$(date +%Y%m%d-%H%M%S)"`，然后运行上面的 CMake 命令。全新 checkout 无需这一步。

构建产物：

| 路径 | 用途 |
| --- | --- |
| `build/qemu/qemu-system-arm` | 带 `dm-mc02` machine 的 QEMU |
| `build/host/` | Host 协议与桥接测试程序 |
| `build/qemu/pyvenv/` | QEMU configure 从源码 wheel 创建的锁定 Meson 环境 |
| `.venv/` | 由 `uv` 管理的 pytest 和项目 Python 工具环境 |

QEMU 默认构建为 Release、`arm-softmmu` 和 DM-MC02 设备 profile。调试构建可运行 `QEMU_BUILD_TYPE=debugoptimized bash tools/build-qemu.sh`；性能测量前应重建 Release。`tools/build-qemu-generic.sh` 用于检查通用 ARM 复用边界，不是运行 DM-MC02 固件的入口。

构建目录中的 `.dm-toolchain` 记录 configure 输入。旧目录没有此记录、所选工具/flags 改变或设置 `QEMU_RECONFIGURE=1` 时，脚本重新执行完整 QEMU configure，以更新缓存 compiler 和 Meson 入口；通常无需手动删除构建目录。

## First validation

不需要外部业务固件即可检查 machine 注册。下面的 guest integration 还需要 ARM 工具链：

```bash
build/qemu/qemu-system-arm -machine help
bash tools/run-mc02-smoke.sh
bash tools/run-ospi-smoke.sh
```

smoke 脚本会编译并运行仓库中的最小 guest。修改 QEMU 模型后先重新构建。`tools/run-qemu.sh --smoke` 只探测通用空 machine，不会启动业务固件。

## Run firmware

输入 ELF 必须面向 DM-MC02 地址空间。先记录用于复现的文件身份，再通过统一入口启动：

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
sha256sum "$DM_MC02_ELF"
bash tools/run-qemu.sh
```

也可传入 `--elf PATH`，它优先于 `DM_MC02_ELF`。入口默认使用 `build/qemu/qemu-system-arm` 和 `dm-mc02` machine；`--qemu PATH` 或 `QEMU_SYSTEM_ARM` 可以覆盖 QEMU。运行环境与构建时保持一致。

默认 USART1 与 QEMU monitor 共用终端（原生 `mon:stdio`）。`Ctrl-a c` 切换 UART/monitor，`Ctrl-a h` 查看帮助。monitor 中可使用 `info status`、`stop`、`cont`、`system_reset` 和 `quit`；`quit` 会走正常关闭路径。用 `--uart USART2` 等名称选择其它 UART，或用 `--uart none` 仅显示 monitor。当前 `trobot` Release 关闭了 logger/terminal，串口没有文本并不代表固件未运行。

常用参数以 `bash tools/run-qemu.sh --help` 为准：

| 参数 | 用途 |
| --- | --- |
| `--elf PATH` | 固件 ELF；默认读取 `DM_MC02_ELF` |
| `--debug`、`--gdb-port PORT` | 从复位暂停，启用原生 GDB；默认 `127.0.0.1:1234` |
| `--uart NAME` | USART1（默认）、USART2、USART3、UART5、UART7、USART10 或 `none` |
| `--qmp PATH` | 创建 QMP Unix socket，`server=on,wait=off` |
| `--cosim PATH` | 创建 co-sim Unix socket，`server=on,wait=off`；worker 单独启动 |
| `--qemu PATH`、`--machine NAME` | 覆盖 QEMU 或 `dm-mc02` machine 属性 |

查看 machine 属性：

```bash
build/qemu/qemu-system-arm -machine dm-mc02,help
```

具体固件可能等待传感器或外部输入。当前 `trobot` 的可复现启动流程见下文静止输入示例；launcher 不替固件判断应用就绪。

## Flash images

在 machine 参数中配置 raw 镜像：

```text
-machine dm-mc02,flash-file=/absolute/internal.raw,ospi2-flash-file=/absolute/external.raw
```

| 属性 | 镜像长度 | 尺寸或 I/O 错误 |
| --- | ---: | --- |
| `flash-file` | 1 MiB（1,048,576 字节） | 拒绝 machine 初始化 |
| `ospi2-flash-file` | 8 MiB（8,388,608 字节） | 输出 warning，保留擦除态 |

空路径禁用磁盘 I/O。指定路径但文件不存在时保留 `0xff` 擦除态。初始化后路径不可修改；正常 QEMU shutdown 才保存完整镜像，崩溃或 `kill -9` 不保证保存，也没有掉电原子性。

内部 Flash 镜像先加载，之后 `-kernel` 仍会装载 ELF 段，因此 ELF 覆盖范围内的持久化字节可能被改写。请将需保留的数据放在 ELF 段以外。零填充文件不是擦除态镜像。

内部 Flash 编程支持 H723 HAL 使用的 256-bit Flash word：启用 `PG` 后，从 32 字节对齐地址连续写入八个 32-bit word，收齐并校验后提交。部分写入不会改变持久化内容；非连续、跨 word、其它写宽、force-write、未擦除 word 的重复编程和 0→1 编程会报错。`SR1` 只读，使用 `CCR1` 清除已建模 flags。编程完成同步执行；ECC、真实编程延迟、完整 `QW/WBNE/FW` 行为和掉电恢复未实现。由于没有 ECC 记录，已写入全 `0xff` 的 word 无法与擦除态区分。

外部 NOR 使用项目独立的 `dm-w25q64` 器件。命令同步完成，quad 使用字节级映射；保护、真实忙时序及器件迁移未支持。

## Serial, QMP and GDB

### GDB

终端一启动 QEMU，CPU 在复位处暂停：

```bash
bash tools/run-qemu.sh --elf "$DM_MC02_ELF" --debug
```

终端二加载同一 ELF 并连接。Nix 的固定 ARM 工具链已提供 `arm-none-eabi-gdb`；Ubuntu 用户可使用自己的 ARM 工具链 GDB，或执行 `sudo apt-get install -y gdb-multiarch` 并将下面命令的程序名替换为它：

```bash
arm-none-eabi-gdb /absolute/path/to/trobot.elf
```

```gdb
target remote 127.0.0.1:1234
break main
continue
```

`--debug --gdb-port 2345` 可改变监听端口，GDB 的连接地址相应改变。QEMU 原生对应参数为 `-S -gdb tcp:127.0.0.1:1234`。调试暂停、trace 和外部 worker 都会改变运行场景，不属于默认 Release 性能基准。

### UART and FDCAN slots

重复的 `-serial` 参数按顺序接入 machine 槽位：

| 槽位（从 0 开始） | 连接对象 |
| ---: | --- |
| 0 | 二进制 co-sim 控制/采样通道 |
| 1–6 | USART1、USART2、USART3、UART5、UART7、USART10 |
| 7–9 | FDCAN1、FDCAN2、FDCAN3 |

默认 DM-MC02 profile 的 UART5 仅接收；选择 `--uart UART5` 可输入数据，但不会输出文本。

例如将 USART1 输出到文件，monitor 留在终端：

```bash
mkdir -p build/runtime
build/qemu/qemu-system-arm -machine dm-mc02 -kernel "$DM_MC02_ELF" \
  -nodefaults -display none -monitor stdio \
  -serial none -serial file:build/runtime/usart1.log
```

FDCAN 使用固定 84 字节二进制帧，经项目 chardev/socket 连接；它不是文本串口，也不是直接的 SocketCAN 设备。使用仓库的 worker 处理该通道，接入 Linux CAN 时使用 worker 的 `--socketcan` 参数。

### QMP

自动化控制可为每个进程创建独立、较短的 Unix socket 路径：

```bash
dm_run_dir=$(mktemp -d /tmp/dm-qemu.qmp.XXXXXX)
bash tools/run-qemu.sh --elf "$DM_MC02_ELF" --qmp "$dm_run_dir/qmp.sock"
rm -f -- "$dm_run_dir/qmp.sock"
rmdir -- "$dm_run_dir"
```

项目 Python 工具通过 `tools/dm_mc02_qmp.py` 复用固定版本的 QEMU Python client。不要另开第二个 reader 读取同一 QMP 连接。深层临时目录可能超过 `sockaddr_un` 路径限制；Linux smoke 应使用脚本管理的 `/tmp/dm-qemu.*.XXXXXX` 私有临时目录。

实验性原生 QMP 执行接口可让一个 CPU 在指定的绝对虚拟时间暂停。直接启动 QEMU 时使用
`-accel tcg,thread=single -S -icount shift=0,align=off,sleep=off`，保留原生 QMP 连接。
该接口要求一个 CPU、固定 icount、paused/prelaunch，且没有 replay、block node/backend、
用户 IOThread、活动 migration 或后台 dump。默认 launcher 和 Arena 暂未接入此接口。

```json
{"execute":"x-run-to-virtual-time","arguments":{"target-ns":1000000}}
{"execute":"x-query-virtual-execution"}
{"execute":"query-status"}
```

`target-ns` 是严格晚于当前时间的有符号 64 位纳秒值。执行请求的返回只表示准入成功；
轮询查询，直到 `state` 为 `reached`，`current-ns` 等于 `target-ns`，且原生
`query-status` 为 `paused`。状态还可能为 `idle`、`running` 或 `cancelled`；
后续普通执行会改变 `current-ns`，历史 `reached` 不能当作当前暂停状态。
请求期间调用方须串行控制执行并保持配置不变，不能发送 `cont`、通过 GDB resume/step，
或热插拔／重配 CPU、时钟、器件、block 和 IOThread。提前停止或其它原因停止会取消请求；
复位总会使结果失效，并保留 QEMU 正常复位运行状态，调用方负责停止和清理。
请求属于进程，发起 monitor 断连后有限目标继续执行并暂停，前提是其它控制方不干预。

固定 shift=0 表示每条指令计一纳秒，不表示 H723 的真实周期。完成只保证 CPU 虚拟时间
到达目标且没有执行超出目标的指令；同时间 IRQ handler、所有器件 timer、CAN/UART
socket 输出完成、plant 同步和实时性能需要分别验证。原生 SysTick 的分数时钟首次 reload
存在逐段纳秒取整，例如 64 MHz、reload 63 的首次中断可在 999 ns；当前执行测试使用
正常 RCC 配置的 8 MHz 整数周期，不能据此声明分数时钟或实板时序已验证。

## External simulation worker

先运行仓库提供的 co-sim 和 motor fixtures，再连接自己的 plant：

```bash
bash tools/run-qemu-worker-smoke.sh
bash tools/run-qemu-v2-motor-smoke.sh
bash tools/run-worker.sh --help
```

手动启动时，入口的 `--cosim PATH` 将 QEMU 第一个 serial 槽位连接到 co-sim Unix socket，UART 仍由 `--uart` 选择。当前 `trobot` 的具体启动步骤见下文。

### trobot stationary startup

当前未经修改的 `trobot` Release 会先完成 INS 初始化，再进入 `example_task`。它需要静止 IMU 样本；未接外部输入时，当前 BMI088 复位寄存器 fallback 不是静止 1 g，不能据此期待 INS 就绪。现有 NullEngine 提供零角速度和 `(0, 0, 1)` g 加速度，可用于启动 fixture；这不是经过校准的真实 plant。

在三个终端中均进入仓库根目录和相同 Nix 环境。终端一启动 QEMU，输出的临时目录供终端二使用：

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
dm_run_dir=$(mktemp -d /tmp/dm-qemu.trobot.XXXXXX)
printf '%s\n' "$dm_run_dir"
bash tools/run-qemu.sh --elf "$DM_MC02_ELF" --debug \
  --cosim "$dm_run_dir/cosim.sock"
rm -f -- "$dm_run_dir/cosim.sock"
rmdir -- "$dm_run_dir"
```

终端二将目录替换为终端一打印的实际值，启动已有 worker：

```bash
dm_run_dir=/tmp/dm-qemu.trobot.XXXXXX
bash tools/run-worker.sh --cosim "$dm_run_dir/cosim.sock" \
  --engine null --protocol v1 --rate 1000 --realtime
```

终端三用同一 ELF 启动 GDB，设置应用任务和错误处理断点后继续：

```bash
arm-none-eabi-gdb /absolute/path/to/trobot.elf
```

```gdb
target remote 127.0.0.1:1234
break Error_Handler
break example_task
continue
```

命中 `example_task` 后，使用 `x/b &'ins::inited'` 读取初始化标志；本场景已观察到值为 `1`。Release ELF 可能只有符号而没有类型调试信息，直接 `print ins::inited` 会报类型未知。这些符号属于当前 `trobot`，其它固件应选择自己的应用就绪位置。此结果证明该固件在静止输入下进入应用任务，不证明电机、真实机构或所有业务行为完成验证。退出时先关闭 GDB，在 QEMU monitor 中输入 `quit`，再停止 worker。

### trobot Release business fixture

普通 `trobot` Release 的 `example_task` 使用 PI 速度控制，依次给定 `0→6→0 rad/s`。
下面的入口自动启动 QEMU 与 MuJoCo worker，验证这一固定业务场景并保存报告。
先准备普通 Release ELF 和项目 QEMU；入口不会编译固件，也不使用专用 MotorTest 固件。
在构建 QEMU 时所选的 Nix 或系统环境中同步项目 `.venv`，并确保有 `arm-none-eabi-nm`：

```bash
uv sync --locked --group dev --extra mujoco --python "${PYTHON:-/usr/bin/python3}"
bash tools/run-trobot-business.sh
```

该场景需要实际 MuJoCo Python 包。常用参数如下；完整帮助可运行
`bash tools/run-trobot-business.sh --help`：

| 参数 | 默认值与用途 |
| --- | --- |
| `--elf PATH` | 仓库相邻的 `../trobot/build/Release/trobot.elf`；必须保留 `xTickCount` 符号 |
| `--qemu PATH` | `build/qemu/qemu-system-arm`；设置 `QEMU_SYSTEM_ARM` 可覆盖默认值 |
| `--output DIR` | 新建 `build/test-results/trobot-business-<UTC时间>/`；已有目录会被拒绝，原文件保留 |
| `--performance` | 三轮固定 72000 step；每轮在实际 enable 后测量至少 60 秒 |

固定模型只有一个带阻尼转子：转动惯量 `Izz=0.02 kg·m²`、阻尼
`0.05 N·m·s/rad`、传动比 `1`。IMU 独立保持零角速度和 `(0, 0, 1) g`，
用于固件 INS 初始化。现有 v2 worker 使用 `--realtime` 节奏，执行 14000 个
1 ms plant step。应用就绪来自固件实际发送的 ID 1、classic CAN、8 字节 enable 命令。

八项轨迹门保持固定，窗口以转子 enable 时刻为零点，包含起点、不含终点：

| 窗口（秒） | 检查 | 通过条件 |
| --- | --- | --- |
| 0.5–1.0 | 静止阶段最大绝对速度 | ≤ 0.05 rad/s |
| 1.0–4.0 | 加速阶段峰值速度 | ≤ 6.5 rad/s |
| 1.5–4.0 | 对 6 rad/s 的最大绝对误差 | ≤ 0.15 rad/s |
| 2.0–3.8 | 平均速度对 6 rad/s 的绝对误差 | ≤ 0.05 rad/s |
| 2.0–3.8 | 平均力矩对 0.3 N·m 的绝对误差 | ≤ 0.02 N·m |
| 4.0–6.0 | 停止阶段最低速度 | ≥ −0.5 rad/s |
| 4.5–6.0 | 停止后最大绝对速度 | ≤ 0.15 rad/s |
| 5.0–6.0 | 停止后平均速度的绝对值 | ≤ 0.05 rad/s |

入口打印 `report.json` 路径。报告保留每项测量、应用启动时间、tick/watchdog 与
co-sim 诊断、QEMU/worker 的 CPU 和 RSS 采样、PID/starttime 身份及输入文件哈希。
同目录的 `plant.jsonl` 保存完整 plant 轨迹，`can.bin` 保存固件发送的 CAN 帧，
`worker.stdout`、`worker.stderr` 和 `harness.log` 用于定位失败。CPU、RSS 与诊断
来自采样，不表示全程峰值；CAN 文件只记录 guest TX。哈希记录选定的 ELF、QEMU、profile
与工具输入，未覆盖完整依赖闭包。QEMU stderr 由 harness 暂存，失败时将最多 12 KiB
摘录写入 `harness.log`，成功时不永久保存。

从 QEMU 启动到应用 enable 的总期限为 10 秒，包含 socket 与 QMP 连接；业务运行
总期限为 22 秒，外层进程保护期限为 30 秒，之后执行有界清理。只有轨迹完整、八项门
通过、没有异常 reset/watchdog，`consume_dropped`、`rx_bad_frames`、`tx_dropped` 为零，
选定输入哈希未变化，且确认本轮进程
已清理，报告才写 `passed` 并返回零。运行失败或中断会保留 `failed` 报告并返回非零；
缺少项目 `.venv` 时，入口在启动前直接提示依赖错误。

这是固定参数的 14 秒功能夹具；转子与 IMU 未经实物标定。worker pacing 和 v2 响应
不建立 CPU/plant 全局锁步，CAN 时间戳也不安排延迟交付。该 14 秒结果不作为持续实时
结论；持续测量使用下述入口。

用同一入口执行持续 paced 性能门：

```bash
bash tools/run-trobot-business.sh --performance
```

每轮使用相同 ELF、worker、模型与原八项轨迹门，执行 72000 个 1 ms step；应用仍只执行
一次 `0→6→0`，随后持续运行 1 kHz 零目标控制。实际 enable 后的下一次完整观测作为
测量起点，host、firmware tick 与对应 plant 区间均须达到 60 秒。启动期限保持 10 秒，
每轮总期限为 85 秒，harness 保护期限为 95 秒，之后执行有界清理。

报告分别保留每轮精确的 firmware tick RTF 与 plant 轨迹推进 RTF。tick 查询前后记录
monotonic 时间，RTF 同时给出点估计和区间；保守下界须达到 `0.999x`。
plant 推进 RTF 须在 `0.999–1.001x`，相对测量起点的 plant/host 时间偏差，以及计入查询
与配对误差的采样 tick/plant 漂移均须不超过 100 ms，最长连续 step 间隔也须不超过
100 ms。停止后的持续轨迹沿用最大绝对速度 `0.15 rad/s`、平均速度绝对值 `0.05 rad/s`
门。这里的容差属于固定工具场景，不代表 1 ms 控制延迟保证。

输出根目录的 `report.json` 汇总三轮状态，`run-1/` 至 `run-3/` 保留各轮完整证据。
每轮还记录实际启动时间、PID/starttime、CPU 用量和采样最大 RSS、宿主身份/负载、
watchdog、最终暂停计数及输入哈希。CPU 以一个核心为 100%，可能超过 100%；RSS 为
采样最大值。三轮全部通过才返回零；中断保留失败，未执行的轮次明确标为跳过。

该门测量当前固定业务场景的 wall-clock pacing。plant 时间戳在 ACK 和 pacing sleep
之前记录，表示轨迹推进；无节流容量、完整 loop 完成延迟、CPU/plant 全局锁步及任意机构
性能仍需独立验证。

### Backend choices

手动运行 stationary startup 示例只建立 co-sim 控制/采样通道。要交换电机总线数据，还需将 QEMU FDCAN chardev socket 配好并给 worker 传入 `--fdcan`；完整 wiring 示例见 `tools/run-dm-motor-smoke.sh`，上面的业务入口已自动连接该通道。通道联通不等于机构闭环已验证。

worker 按 RESET 时的时间原点转换 QEMU 与 plant 时间，CAN 反馈携带 QEMU 时间戳。
当前 FDCAN 收到帧就立即交付；时间戳不会延迟未来帧，也未实现 guest RX timestamp。
`--realtime` 只控制 worker 的 wall-clock 节奏，v2 ACK 表示输入接受、DONE 表示 BMI088
消费完成；它们都不保证 CPU 与电机 plant 在同一时刻推进。

worker 支持的常用选择：

| 场景 | 入口 |
| --- | --- |
| NullEngine fixture | 默认 `--engine null`；静止 IMU 输入和简化电机响应，用于功能/传输验证 |
| MuJoCo adapter | `uv sync --locked --group dev --extra mujoco` 后运行 `bash tools/run-mujoco-worker-smoke.sh` |
| ROS 2 adapter | 安装并 source 系统 ROS 环境（验收环境为 Jazzy），运行 `bash tools/run-ros2-worker-smoke.sh` |
| 已有 Linux CAN 接口 | `--socketcan can0 --fdcan /path/to/fdcan.sock` |
| 自定义 plant | `--backend package:create_backend` 或 `--backend-registry package:register --engine name` |

MuJoCo adapter 在初始化、reset 和每次 `mj_step` 后调用原生 `mj_forward`，使姿态和
IMU derived state 对应当前关节状态；这一步不会增加 plant 时间。gyro/accelerometer
来自 MuJoCo site-local 传感器，worker 再转换成 deg/s 和 g。该观察边界仍不改变旧 worker
的 step-start wire 时间约定，也不建立 QEMU/plant 全局锁步；当前变更后的性能尚需重测。

MuJoCo worker smoke 验证 adapter 和测试模型；下文的 MotorTest 另验证实际固件电机组件的数据边界。普通 Release 的固定单转子业务使用上面的 [business fixture](#trobot-release-business-fixture) 入口。ROS 2 默认 topic 为 `/dm_mc02/imu`、`/dm_mc02/joint_states` 和 `/dm_mc02/motor_cmd`；adapter smoke 不等同于 Gazebo world/model 联调。运行时完整参数以 `bash tools/run-worker.sh --help` 为准。

具名物理接口使用锁定的 MuJoCo 3.12.0，可独立于 QEMU 和 CAN 调用：

```python
from sim.physics import MujocoPhysics

plant = MujocoPhysics.from_xml_path(
    "smoke/dm_mc02_mujoco_smoke.xml",
    joints={"test_joint": "motor0"},
    imus={"imu": ("imu_gyro", "imu_accel")},
)
plant.set_joint_effort("test_joint", 0.005)  # Nm
plant.step(1_000_000)  # ns, one native solver step
print(plant.get_joint_state("test_joint"))  # rad, rad/s
print(plant.get_body_pose("test_body"))  # world metres, body quaternion wxyz
print(plant.get_imu("imu"))  # site-local rad/s and proper acceleration m/s²
```

绑定只接受 hinge 与直接、单位 gear、即时且无隐式限幅的 motor；IMU 必须是同一 site 的
原生 gyro/accelerometer 配对，不支持延迟、历史采样、filter 或 noise。缺失或不支持的绑定
在运行前明确拒绝；旧 worker 沿用惯用传感器名，缺失传感器不再静默输出零。
getter 读取最近一次初始化、reset 或完成 step 的状态；改变力矩后须 step 才产生新的 IMU。
模型中其它原生负载或 actuator 仍会参与动力学，设置的力矩不代表总关节合力矩。
原生数值异常或无效目标状态会报错，后续推进与观察须先 reset；失败不提供回滚。
`time_s` 是原生浮点 plant 时间，`step_seconds` 为旧 worker 保留精确浮点步长，两者都
不建立 QEMU 时间屏障。四轮底盘、固件 IMU 闭环和物理标定仍需分别验证。
`validate_step(dt_ns)` 和 `validate_joint_effort(name, Nm)` 只检查准入，不推进模型或
改写力矩。`validate_steps((dt1_ns, dt2_ns, ...))` 按顺序检查未来 native 时间，提前
拒绝浮点时间分辨率不足的后续区间。`close()` 释放此实例持有的原生 model/data；重复关闭无害，关闭后操作拒绝。

### Gazebo 原生关节接口（可选）

这个独立接口使用 Gazebo Harmonic `gz-sim8 8.15.0`，通过原生 Server
读取完成的关节状态，验收使用默认 DART 求解器。当前 SDK profile 使用 ROS Jazzy 与 CPython 3.12；不要将模块
放入默认 Python 3.11 环境。先安装匹配的 SDK、系统 C++ 编译器、Python 开发头文件
和 pybind11 2.11.1、gz-sensors 8.2.2 IMU 组件，然后在仓库根目录运行标准构建：

```bash
source /opt/ros/jazzy/setup.bash
UV_PROJECT_ENVIRONMENT=build/venv-gazebo uv sync --frozen --python /usr/bin/python3
cmake -S sim/physics/gazebo_native -B build/gazebo-native \
  -DPython3_EXECUTABLE="$PWD/build/venv-gazebo/bin/python" \
  -DCMAKE_CXX_COMPILER=/usr/bin/c++ -DCMAKE_BUILD_TYPE=Release
cmake --build build/gazebo-native -j2
ctest --test-dir build/gazebo-native --output-on-failure
```

使用新的构建目录，并避免 Conda 等环境的 Protobuf 覆盖系统 SDK 依赖。若本机 CMake
仍搜索 `/opt/anaconda3`，可显式增加 `-DCMAKE_IGNORE_PREFIX_PATH=/opt/anaconda3`。
专用 CTest 使用这个 Python 环境；缺少模块、依赖或测试失败会返回非零。默认 QEMU
门使用原有环境，Gazebo SDK 门需要单独执行，不会将未执行的 Gazebo 测试记为通过。

设置 `PYTHONPATH="$PWD/build/gazebo-native"` 后，在上述 Python 环境中调用：

```python
from sim.physics import GazeboPhysics

with GazeboPhysics("models/gazebo_rotor.sdf",
                   joints={"shaft": ("rotor", "shaft")},
                   bodies={"rotor": ("rotor", "body")}) as plant:
    plant.set_joint_effort("shaft", 0.4)  # Nm
    plant.step(100_000_000)               # 100 native 1 ms ticks
    print(plant.time_ns, plant.get_joint_state("shaft"))  # ns, rad, rad/s
    print(plant.get_body_pose("rotor"))  # world/link frame: m, quaternion wxyz
    plant.reset()                       # completed native reset at time 0
```

首版接受一个 world、一个 physics profile、顶层动态 model 中的 revolute 关节，
以及显式 Physics plugin；启用下述渲染绑定时另需标准 Sensors plugin。绑定和 SDF 准入先于 Server 创建；model/visual
插件、nested model、actor 和未支持的 sensor 明确拒绝。关节需要显式有限的 effort limit
（负值表示不限制）；非有限力矩或超过正 limit 的输入拒绝，并保留原命令。
每个 native tick 写齐全部关节命令；getter 只读取完成状态。`timestep_ns` 沿用
Gazebo 原生浮点秒到整数时间的截断，不能假定 SDF 十进制值精确可表示；`step`
必须是实际步长的整数倍，每次 1–10000 ticks。初始化及 reset 不增加积分步。
`bodies` 绑定逻辑名称到 `(model, link)`，`get_body_pose` 返回完成状态的
`BodyPose(position_m, quaternion_wxyz)`；位姿由原生 Physics 更新，getter 不推进时间。
可只配置 bodies，至少需要一个 joint、body、IMU、camera 或 LiDAR 绑定。当前 body 观察只接顶层动态
model 的 link，缺失、重复和静态绑定拒绝。未由 Physics 发布的观察不返回默认零位姿。
运行错误冻结推进和观测，成功 reset 后恢复；失败不回滚。并发操作明确拒绝。
`close()` 停止并销毁服务器，原生析构负责线程释放；阻塞插件调用不提供抢占保证。
`validate_step(dt_ns)` 和 `validate_joint_effort(name, Nm)` 使用同一准入规则，只读检查
当前 native 时间、绑定和范围；检查通过不保证后续求解成功。
`validate_steps((dt1_ns, dt2_ns, ...))` 还检查每个后续区间及累计 native 时间范围。

要由现有 host Coordinator 统一 reset 和推进，可将已构造的 `GazeboPhysics`
或 `MujocoPhysics` 交给同一个 `PhysicsParticipant`：

```python
from sim.core import Coordinator, Session
from sim.physics import GazeboPhysics, PhysicsParticipant

physics = GazeboPhysics("models/gazebo_rotor.sdf",
                       joints={"shaft": ("rotor", "shaft")})
coordinator = Coordinator({"world": PhysicsParticipant(physics)})
try:
    coordinator.reset(Session(1, 7))
    physics.set_joint_effort("shaft", 0.4)
    result = coordinator.step(10_000_000)["world"]
    print(result.reached_time_ns, result.diagnostics)  # 10000007; native_time_s=.01
    print(physics.get_joint_state("shaft"))
finally:
    coordinator.close()
```

participant 成功构造后拥有该 physics 的 reset、step 和 close；构造失败时仍由
调用者清理。可借用 physics 设置 control 或读取观察，不再直接推进、重置，
也不能与拥有同一 physics 的 `M3508JointPlant` 同时使用。
每次 step 先复用后端的时长预检，再推进一次；后端失败冻结 participant，须成功 reset。
session 的整数起点与原生 reset 时间零分别保留，`native_time_s` 是原生浮点秒诊断，
不用于舍入或重建 session 时间。此 host barrier 不保证目标相机帧已交付或 MCU 同步；
无关节的渲染 world 同样可以使用这个 participant。

可通过原生 SDFormat include 组合本地静态场景，并用 `resource_paths` 指定
`model://` 的模型目录。目录以调用者当前路径解析，必须存在且不含冒号或 NUL；
配置只作用于本次加载，不修改全局环境。原生 SDFormat 的安装路径和 `SDF_PATH`
回退仍可参与查找。这是本地资源接口，不是文件系统沙箱或远程下载器。
模型 include、mesh、material script 和纹理文件的缺失在 Server 启动前拒绝；
纹理准入只检查路径和文件存在，不保证可解码或渲染。reset 使用首次准入的
SDF DOM，外部纹理、mesh 等文件仍需保留，不会随 DOM 保存其内容。

仓库提供固定社区版本的 RMUA 2019 静态场地及两个落体测试立方体：

```python
from pathlib import Path
from sim.physics import GazeboPhysics

models = Path("models")
with GazeboPhysics(models / "gazebo_rmua19.sdf",
                   resource_paths=(models / "vendor/rmua19/resource/models",),
                   bodies={"floor": ("floor_probe", "body"),
                           "obstacle": ("obstacle_probe", "body")}) as world:
    world.step(5_000_000_000)
    print(world.get_body_pose("floor"))
    print(world.get_body_pose("obstacle"))
    world.reset()
```

模型配置、几何和木纹图片原样复用；固定来源、哈希和 Apache-2.0 许可见
`models/vendor/rmua19/NOTICE`、`SOURCES.json` 和 `LICENSE`。场地仅作为历史社区
夹具，未验证当前比赛尺寸、实物接触参数或图片实际渲染；原生平面的碰撞范围
可能大于视觉尺寸。该组合只使用 Physics，不包含相机、LiDAR、机器人控制、
裁判规则或 Arena 调度。SDK 的 `gazebo-native-resources` 测试独立验证资源准入，
`gazebo-native-joints` 再验证实际接触、公共位姿和 reset。

IMU 使用原生 gz-sensors 的同步生成和公开 getter，配置为
`imus={逻辑名称: (model, link, sensor)}`：

```python
from sim.physics import GazeboPhysics

with GazeboPhysics("models/gazebo_imu_rotor.sdf",
                   joints={"shaft": ("rotor", "shaft")},
                   imus={"imu": ("rotor", "body", "imu")}) as plant:
    print(plant.get_imu("imu"))  # time 0: gyro (0,0,0), proper accel (0,0,9.80665)
    plant.set_joint_effort("shaft", 0.4)
    plant.step(100_000_000)
    print(plant.get_imu("imu"))  # sensor-local rad/s and proper acceleration m/s²
    plant.reset()
    print(plant.time_ns, plant.get_imu("imu"))  # fresh native time 0, torque cleared
```

首版所有 link sensor 都须显式绑定，且为动态 model 的 IMU；只接受
`update_rate=0`、无噪声、无 sensor 插件或 metrics 的配置。每个完成的 native tick
由 Physics 提供 sensor frame 的运动数据，再由原生 ImuSensor 处理重力并生成样本。
初始化和 reset 同样生成 time 0 快照，getter 只读已完成样本；这不等同于标准
Imu plugin 的 paused 行为或任意 ODR 调度。原生 sensor 仍拥有 publication/service，
此接口只证明本地样本生成，不保证 Gazebo transport 或 ROS 消息交付。
多 world 的同名 topic 隔离尚未验证。

`reset()` 销毁旧 Server、observer 和 sensor，再从首次准入的 SDF 快照重建完整
原生 producer；不重新读取文件，不增加积分步，避免沿用原生 in-place reset 后
残留的加速度。重建失败会释放部分资源并保持 failed，可再次 reset；
显式 close 后永久拒绝操作。reset 重建延迟未作性能承诺。

相机和 GPU LiDAR 的原生无窗口验证使用标准 Sensors plugin、Ogre2 EGL 和
Gazebo transport，不需要 X server。需要额外安装匹配的 gz-rendering 8.2.3
Ogre2、gz-transport 13.6.0、gz-msgs 10.4.0，以及可用的 EGL/OpenGL 驱动。
在上述 SDK 环境中显式启用独立测试：

```bash
cmake -S sim/physics/gazebo_native -B build/gazebo-native \
  -DPython3_EXECUTABLE="$PWD/build/venv-gazebo/bin/python" \
  -DCMAKE_CXX_COMPILER=/usr/bin/c++ -DCMAKE_BUILD_TYPE=Release \
  -DDM_GAZEBO_RENDERING_TESTS=ON
cmake --build build/gazebo-native -j2
ctest --test-dir build/gazebo-native -R '^gazebo-native-rendering$' --output-on-failure
```

测试使用本地红色立方体，核验两帧 64×48 RGB 图像、65×1 激光扫描、
2.5 m 目标距离和递增的原生消息时间戳。最多运行 1 秒虚拟时间；观察期限
12 秒，CTest 对整个进程（含驱动和清理）设 20 秒超时。缺少驱动、消息或
清理失败会返回非零。默认关闭此选项，普通关节测试通过不代表渲染测试通过。
这项测试验证固定场景的原生消息。公开相机/LiDAR 接口使用下面独立的构建选项；
资产渲染、任意场景的 ROS 2 交付和 MCU 时间同步仍需各自验证。

要在 `GazeboPhysics` 中读取相机和 LiDAR，启用默认关闭的生产接口选项：

```bash
cmake -S sim/physics/gazebo_native -B build/gazebo-native \
  -DDM_GAZEBO_RENDERING=ON
cmake --build build/gazebo-native -j2
ctest --test-dir build/gazebo-native -R '^gazebo-native-rendering-(admission|mailbox|consumer)$' --output-on-failure
```

它独立于 `BUILD_TESTING` 和 ROS 测试选项。沿用上述 SDK/Python 配置和 `PYTHONPATH`，
可用固定场景读取真实原生快照：

```python
from sim.physics import GazeboPhysics

with GazeboPhysics("sim/physics/gazebo_native/tests/rendering.sdf",
                   cameras={"front": ("sensors", "body", "camera")},
                   lidars={"scan": ("sensors", "body", "lidar")}) as world:
    world.step(50_000_000)
    print(world.get_camera("front"))  # CameraFrame or None; RGB8 bytes
    print(world.get_lidar("scan"))    # LidarScan or None; m/rad
```

渲染由标准 Sensors/Ogre2 EGL 管理。SDF 必须显式配置 Physics 和
`gz-sim-sensors-system` / `gz::sim::systems::Sensors`，后者只设置
`<render_engine>ogre2</render_engine>`。所有 sensor 都须绑定；camera/GPU LiDAR
可位于静态 link，同一 Server 也可同时消费关节、动态 body 和 IMU。
与渲染组合的 IMU 也须在 SDF 中给出显式、唯一的绝对 topic，以验证原生服务不会冲突。
首版最多 8 个渲染 sensor：无噪声 RGB8、宽高各 1–1024、最大 3 MiB 图像；
无噪声 GPU LiDAR、2–4096 水平 rays、单行、零垂直角、单位 scan resolution。
两者 SDF 频率为 1–1000 Hz，原生周期有毫秒截断。触发、保存、畸变、自定义 lens/
投影、visibility override 和其它相机类型明确拒绝；距离 resolution 配置不代表量化精度。

主话题必须显式、绝对且唯一，不自动修正名称。检查也覆盖 CameraInfo、
LiDAR `/points`、`/set_rate` 与标准 marker 名称；`camera_info_topic` 可显式指定，
否则由原生相机使用主话题的父路径加 `/camera_info`。调用者须独占这些话题、
保持 `GZ_PARTITION` 不变，并保持准入的配置，不调用原生 `set_rate` 服务。
本模块每进程只允许一个渲染 owner；外部进程内渲染器共存未支持。

getter 立即返回最新实际交付的不可变快照，未收到消息时返回 `None`，不等待或
推进物理时间。`timestamp_ns` 保留原生 sensor 时间（零合法）；`frame_id` 原样保留。
图像有 `width/height/step/data`，扫描保留完整 `ranges_m/intensities` 和角度/距离元数据；
原生无穷距离与单行 `vertical_angle_step_rad=NaN` 保留，NaN 距离拒绝。
最新快照覆盖中间观察，合法时间戳不递增的消息丢弃；新消息的格式错误冻结实例，须 reset。
该保留上限不限制原生 transport 的总队列。LaserScan 的 `world_pose` 不在首版接口中。
`step()` 完成不保证目标时刻图像已交付；需要消息时由调用者设置有限观察期限。
reset 重建 producer 和订阅，清空旧观察；close 完成 Server 线程与订阅清理后释放 owner。
驱动、RunOnce、Stop/join 为原生阻塞调用，进程外期限仍由调用者负责。

仓库提供带传感器的历史社区场地组合，使用同一接口和固定本地资源：

```python
from pathlib import Path
from sim.physics import GazeboPhysics

models = Path("models").resolve()
with GazeboPhysics(models / "gazebo_rmua19_sensors.sdf",
                   cameras={"front": ("sensors", "body", "camera")},
                   lidars={"scan": ("sensors", "body", "lidar")},
                   resource_paths=(models / "vendor/rmua19/resource/models",)) as world:
    world.step(50_000_000)
    print(world.get_camera("front"))
    print(world.get_lidar("scan"))
```

相机与 LiDAR 位于 `(3, 0, 0.25)`，朝向原场地墙面；中心扫描距离为 1 米。
`gazebo-native-rendering-consumer` 用真实快照验证木纹渲染，并与仅去掉贴图的临时
资源对照比较色彩和空间变化，同时核验原资产哈希。此证据只覆盖固定场景，
不代表精确像素、相机标定或当前官方赛场；读取仍可能返回 `None` 或较早帧。

要验证实际 ROS 2 消息交付，在同一构建目录额外启用 ROS 门；需要
ROS Jazzy 的 `ros_gz_bridge 1.0.24`、rclcpp 和 sensor_msgs：

```bash
cmake -S sim/physics/gazebo_native -B build/gazebo-native \
  -DPython3_EXECUTABLE="$PWD/build/venv-gazebo/bin/python" \
  -DCMAKE_CXX_COMPILER=/usr/bin/c++ -DCMAKE_BUILD_TYPE=Release \
  -DDM_GAZEBO_RENDERING=ON \
  -DDM_GAZEBO_RENDERING_TESTS=ON -DDM_GAZEBO_ROS_TESTS=ON
cmake --build build/gazebo-native -j2
ctest --test-dir build/gazebo-native -R '^gazebo-native-ros(-consumer)?$' --output-on-failure
```

测试启动官方 `bridge_node`，用 `ros_bridge.yaml` 将原生 image/scan topic
分别映射到 `/dm_render/ros_image`、`/dm_render/ros_scan`，单向桥接并设置
明确的 frame ID。测试双方再用标准 topic remap 加入唯一运行前缀，隔离并发实例。
等待实际 ROS publisher 匹配后启动场景；按原生时间戳
配对两帧，比较完整 RGB 字节和全部扫描值。ROS 使用 SensorDataQoS、深度 5；
测试每个方向最多保留 24 个样本，不保证无损交付。
Gazebo 侧 subscriber 队列由 SDK 管理，配置中的 `subscriber_queue` 不建立
该队列的容量保证。

`gazebo-native-ros` 验证原生场景夹具；`gazebo-native-ros-consumer` 使用独立 Python
进程中的实际 `GazeboPhysics`。后者在观察器建立两个原生订阅并匹配 ROS publisher
后启动 producer，验证公开 getter 的两份递增样本及原生/ROS 配对，然后等待 producer
正常关闭。ready 标记只证明订阅设置与 DDS 匹配；原生跨进程发现和图像交付仍为异步。
producer 最多推进 900 个 1 ms tick，观察期限为 10 秒。该门沿用固定红色方块场景，
不扩大为任意场景、导入纹理的 ROS 证据或 MCU 同步。

整个进程组合的执行期限为 20 秒，随后借用既有工具有界 TERM/KILL/reap，
CTest 总超时为 30 秒。报告保留子进程退出码、输入哈希和清理错误；本机固定
SDK 的 bridge 使用了 KILL 清理，不能据此宣称组件可通过公开 API 正常析构。
该门默认关闭，证明固定场景的真实 ROS 交付，不建立 ROS、渲染与 MCU 的全局时间屏障。

这个转子是未标定的验证夹具。可通过下文的 `M3508JointPlant` 使用既有 CAN 电机协议。
当前接口尚未接入 MCU 或 Robot manifest；ROS consumer 仅覆盖上述固定场景；
上述静态场地组合也不建立 CPU/plant 全局锁步或实时性能保证。

### MuJoCo 四轮底盘

`models/four_wheel.xml` 是独立的四轮物理夹具，沿用官方 car 示例的圆柱轮与原生地面接触，
绑定为 `wheel_fl → drive_fl`、`wheel_fr → drive_fr`、`wheel_rl → drive_rl`、
`wheel_rr → drive_rr`；`chassis` 提供 body pose，IMU 配对仍为 `imu_gyro/imu_accel`。
坐标为 +X 前、+Y 左、+Z 上；四轮等正力矩向前，右侧更快产生正 yaw。
模型总质量 0.6 kg、轮半径 0.03 m、轴距 0.14 m、轮距 0.12 m、轮轴阻尼 0.03 Nm·s/rad，
摩擦与接触参数使用锁定版本的原生默认值。这些参数未经实物标定；它是普通四轮夹具，
可通过下文的共享 M3508 joint adapter 驱动。BMI088 单样本输入已有下方 SPI 消费门；
真实四轮固件可使用下面的专用消费者测试。

同一四轮 plant 可通过专用 worker 同时产生 M3508 反馈和 BMI088 输入：

```bash
uv run --frozen --extra mujoco python tools/run-chassis-worker.py \
  --cosim /path/to/cosim.sock --fdcan /path/to/can.sock \
  --protocol v2 --rate 1000 --realtime
uv run --frozen --extra mujoco bash tools/run-qemu-worker-smoke.sh --chassis
```

ID 1–4 固定为左前、右前、左后、右后；默认模型为 `models/four_wheel.xml`，可用
`--chassis-model` 指定具有相同具名绑定的 XML。默认 command timeout 为 100 ms，
`--torque-per-amp .02` 是未标定的转子 Nm/A 系数，`--can-delay-ns 0` 无额外交付延迟。
每步先完成共享 plant 的推进和反馈交付，再读取该目标时刻的原生 IMU；RESET 只重置
一次 plant，重试不重新推进或采样。此入口要求默认 engine，不组合旧 motor backend。
原有 `run-worker.sh` 与 `run-can-worker.py` 保持原入口。
可选 smoke 用实际 SPI guest 核对第一个 1 ms 目标的接触瞬态、六轴量化与 DONE 消费；
它仍不证明真实四轮固件闭环、实物标定、CPU 锁步或持续实时性能。

`trobot` 的默认关闭 `ChassisTestDebug/Release` 使用四个真实 `motor::dji` M3508
对象、现有 PID 与 BSP IMU，测试静止、前进、右轮更快转向、停止和禁用：

```bash
cd ../trobot
cmake --preset ChassisTestRelease
cmake --build --preset ChassisTestRelease
cd ../dm-mc02-qemu
uv run --frozen --extra mujoco bash tools/run-trobot-chassis-test.sh \
  ../trobot/build/ChassisTestRelease/trobot.elf build/test-results/chassis-release
```

第二个参数是新结果目录，可选第三个参数指定对应的固件源码目录。结果保留 UART
逐项报告、公开 plant 状态、实际 CAN 命令、已排队反馈、原生 DONE 掩码及精确输入哈希；
已有结果不得覆盖。主机运行 7500 个 1 ms plant step，前四个固件阶段各持续 1000 ms，
检查最后 250 ms 的速度和 IMU，最后禁用 20 ms。固定 PID/模型参数和阈值用于 toy 回归。
反馈排队不能证明 guest 接收；固件 freshness 与实际更新另行检查。BSP accel 为 800 Hz，
每 1 ms 输入可能被拒绝，报告保留缺失 DONE 和未观察的响应，不要求每步完整消费。
UART 阶段窗口按主机读取时间分段，ACK/DONE 和 CAN 时间戳不建立 CPU/plant 时间屏障。
只有消费者、进程清理和输入哈希核对成功后报告才为 passed；物理标定明确 SKIP，
此功能测试不作持续实时或实板真实性声明。

可单独运行物理门：

```bash
uv run --frozen --extra mujoco python -m pytest -q tests/test_four_wheel_physics.py
```

`sim.devices.bmi088.imu_to_bmi088_units(plant.get_imu("imu"))` 将已完成的 native
site 采样转换为现有 BMI088 输入单位 `(gyro_deg_s, accel_g)`，其中 `1g=9.80665 m/s²`。
它保持三轴方向，不推进 plant、减去重力、添加 bias 或分配样本身份；已有器件负责
量程、ODR、filter 与 raw 字节，co-sim link 负责读取来源身份。

用同一四轮模型验证单个静止或转向采样的 QEMU SPI 读回：

```bash
uv run --frozen --extra mujoco bash tools/run-qemu-v2-step-done-smoke.sh --mujoco-source rest
uv run --frozen --extra mujoco bash tools/run-qemu-v2-step-done-smoke.sh --mujoco-source turn
```

该门先完成 500 ms 静止步进；`turn` 再通过四台 M3508 的实际 CAN 分组命令推进
500 ms。原生 readback guest 配置 ±1000°/s 和 ±6g，读取两颗 BMI088 的六轴 raw 字节，
按 float32 输入及官方量程核对量化区间，并要求 DONE consumed=3、missing=0 与相同重放。
输出 `PROFILE` 保存实际 SI、wire、raw 与独立的 plant/transport 时间。
这只覆盖全新器件的第一个物理采样，不声明后续 filter 时序、实物标定或 CPU 锁步。
无参数运行该 smoke 仍验证原有消费、拒绝、覆盖与 reset 行为。

BMI088 的完整 raw burst/FIFO data 消费通知携带实际样本身份；默认字节、部分读取或跨样本混合不产生完整消费确认。raw 目前逐字节读取寄存器，没有已验证的硬件 shadow snapshot。`--wait-step-done` 用于 BMI088 消费门：输入已接受后，器件未接收该样本或未读来源被覆盖/器件复位，会返回失败 DONE（status=2），并报告真实已消费和缺失掩码。严格等待会立即报错，适用于传感器已经配置、每个样本都应被完整读取的场景；默认模式继续按 ACK 推进。来源按实际读取身份匹配，跨会话重用外部序号不会误确认；完成响应按 ACK 顺序发送，并保留最近结果供重放。此门不提供 CPU/电机全局锁步。SPI 独立组件的 v1 状态恢复会清除缺失的来源身份，SPI/BMI088 组合的旧 v1 快照拒绝；这些组件状态不代表整机迁移。

v2 worker 对每个 STEP 的输入准入最多等待 2 秒。没有 ACK 时，每次等待 0.25 秒，初次发送后最多重试三次；收到 `QUEUE_FULL` 后按 1 毫秒间隔重试，仍受同一个 2 秒期限约束。重试沿用原始输入，不重复推进 plant；准入发送尚未完成时先完成原发送。等待 ACK、DONE 或队列恢复时，CAN/SocketCAN 仍可双向转发。`--wait-step-done` 另有每次 2 秒、最多四次的完成等待；超时重试仍计入次数。重放尚未发送完时，先继续原发送；发送队列空才重放同一 STEP，不重复推进 plant。完成等待的时间另计。准入超时报 `STEP_ACK admission timed out`，附带 STEP 编号、时间、最后状态、队列深度和静默重试次数，便于定位未回应或队列持续占满。

worker 每次从 co-sim/FDCAN stream 最多读取 4096 字节，返回完整帧并保留未完成帧；v2 暂存的已解码回复先处理，再读取新输入。因此持续可读输入期间仍有机会检查等待期限并服务其它就绪方向。此策略不保证各方向吞吐相同或固定的主机延迟；自定义 backend 的阻塞调用仍需自行限时。

需要自行连接多个 QEMU 时，可直接使用 `sim.transports.cosim` 的
`StepFramedSocket`、`wait_v2_response`、`send_v2_step`、`wait_v2_reset_ack`
和 `QemuTimestampMapper`。唯一 v2 codec 是 `sim.transports.step_codec`；
旧 `tools/dm_mc02_step_protocol.py` 和 worker 名称继续导出同一对象。
每个连接分别持有 directional validator、session ID 和 mapper；调用方负责
socket 的连接、非阻塞配置与清理。初始握手沿用实际 RESET/RESET_ACK：

```python
from sim.transports import step_codec as wire
from sim.transports.cosim import StepFramedSocket, wait_v2_response, QemuTimestampMapper

# cosim_socket 是调用方已连接的 socket；logical_origin_ns 由执行者选择。
cosim_socket.setblocking(False)
stream = StepFramedSocket(cosim_socket)
tx = wire.StepSessionValidator.host_to_qemu()
rx = wire.StepSessionValidator.qemu_to_host(require_step_done=False)
session_id = wire.generate_session_id()
reset = wire.decode_step_frame(wire.build_step_frame(wire.RESET, 0, 0,
                                                    session_id=session_id))
tx.accept(reset)
rx.begin_reset(0, 0, session_id)
stream.send(wire.encode_step_frame(reset))
ack_frame = wait_v2_response(stream, rx, wire.RESET_ACK, 0, 0)
ack = wire.decode_reset_ack_payload(ack_frame.payload)
mapper = QemuTimestampMapper(ack.virtual_time_ns, logical_origin_ns)
```

`send_v2_step` 接收调用方已经校验、编码的 STEP bytes，复用上述重试规则，
不会生成输入或推进 plant。`wait_v2_response` 收到非预期 RESET 时抛出
`V2SessionReset`；将它交给 `wait_v2_reset_ack(stream, rx, reset_event)`，
只会等待实际匹配 ACK 并返回最新 RESET 元数据。重复 RESET 不延长同一个
2 秒期限。握手不清除已有 RX tail、TX suffix 或已解码回复，也不重置 CAN、
器件、物理世界或 QEMU；这些效果由调用方明确决定。既有单板 worker 仍在
成功 ACK 后重置其独占 plant。多板共用一个 plant 可使用下方
`SharedWorldRuntime`；各板的 RESET epoch、STEP_ACK 和 BMI088 消费 DONE
仍分别表示时间映射、输入准入和传感器消费。
`bash tools/run-qemu-v2-step-smoke.sh` 验证旧 worker 和两个真实 QEMU 的独立
会话、时间原点，以及等待一块板 STEP_ACK 时发现另一块板的原生 RESET，
随后由调用方完成 RESET_ACK 握手。双板用例复用现有 Lab 测试 ELF；报告保留
实际 DONE 消费 masks，该用例未构造物理世界。

多连接调用方可用 `poll_v2_response(stream, rx)` 检查当前没有等待器读取的
连接。它非阻塞地返回一个 `(frame, payload)`，无完整回复时返回 `None`；
使用与等待器相同的会话和 payload 校验，并在校验队首前保留已解码后续帧。
RESET 原样返回其会话和时间元数据，STEP_DONE 保留实际 status 与消费 masks，
调用方据此决定是否冻结或握手。每条连接同一时刻只能由 poll 或等待器读取。
poll 不发送、等待或清理连接；未完成的 RX 字节继续由原 stream 保存。

QMP 或固件触发 machine reset 时，同一 co-sim 连接会先完成已开始发送的帧，再发送新会话 RESET、RESET_ACK 和状态快照；尚未开始发送的旧回复会取消。断开连接会清空旧输出，重连建立新会话，不重放丢失的回复。客户端主动发送 RESET 仍按原有队列顺序返回响应；使用严格会话校验的客户端需处理此前的旧会话回复。

同一连接的复位也会保留尚未接收完整的帧前缀。后缀到达后，旧 v2 STEP 按旧会话 ID 整帧拒绝，新会话 STEP 可继续接收；`cosim-diagnostics` 的 `rx_buffered` 表示当前保留的接收字节数。拒绝旧 STEP 会增加 `rx_bad_frames` 和 `rx_dropped_bytes`，不代表帧边界损坏。真正断连会清空旧接收字节。此边界不隔离旧客户端 RESET 帧或无会话 ID 的 v1 输入；会话 ID 完整回绕和客户端主动复用 ID 也不提供历史隔离保证。

v2 控制响应队列满时，已经接收完整但尚未处理的 STEP 也会留在接收缓冲区，不交付设备。读取回复释放队列后可以继续处理；期间发生复位时，该帧保持原字节，按旧会话 ID 拒绝，新会话 STEP 可继续接收。`tx_queue=8` 与非零 `rx_buffered` 可帮助确认此状态。遥测仍按既有拥塞策略丢弃或延后发送，`tx_dropped` 会记录相关事件；此恢复不提供丢失回复的重放。

worker 收到新 RESET 后立即停止旧 STEP 重试，等待对应 RESET_ACK 后重建 plant 会话。此握手另有 2 秒期限；期间再次 RESET 会切换到最新会话，期限保持不变。worker 保留队列中旧 STEP 的完整帧或未发送后缀，按原顺序完成发送，新 STEP 排在其后；QEMU 会按旧会话 ID 拒绝完整旧 STEP。该策略也适用于完成等待中尚未发送完的重放。握手超时、断连或恢复后的 STEP 准入超时会报错并清理资源。连接关闭会终止当前 worker，重新运行 worker 可建立新会话。

初始 RESET 超时、协议错误、运行失败或中断时，worker 会尝试关闭已成功获取的 backend 和所有连接。某项关闭失败时，继续关闭其它连接，并输出 `cleanup <资源>.close() failed`；原始运行错误保持不变。正常运行结束后的关闭失败也会返回非零。连接或配置尚未完成时失败，连接 helper 会尝试关闭它持有的 socket；Unix 连接关闭失败会停止重试，保留原始连接错误及清理诊断。SocketCAN 保留原有接口错误说明。

自定义 backend 返回后，worker 先尝试取得 `close()`，再检查方法；属性 getter 抛错或对象被拒绝时，会尝试调用已取得的关闭方法一次。准入失败且无法取得可调用的关闭方法时，诊断为 `backend cleanup unavailable`，不能保证插件私有资源已释放。自定义 factory 负责其返回前创建的私有资源；自定义 backend 的 getter、调用及关闭操作应及时返回，worker 不强制中断阻塞的插件。

## Testing and performance

日常修改优先运行受影响的原生测试，例如内部 Flash：

```bash
tools/meson test -C build/qemu --print-errorlogs qtest-arm/dm-mc02-memory-test
```

该命令复用已有 qtest，不需要业务固件。修改模型后先构建 QEMU；完整回归用于发布和涉及多个入口的变更。

### trobot HAL/BSP firmware test

外部 `trobot` 工作区可选择专用测试构建，普通业务 preset 默认关闭测试：

```bash
cd ../trobot
cmake --preset SelfTestDebug
cmake --build --preset SelfTestDebug --clean-first
cmake --preset SelfTestRelease
cmake --build --preset SelfTestRelease
cd ../dm-mc02-qemu
tools/run-trobot-selftest.sh ../trobot/build/SelfTestRelease/trobot.elf build/test-results/trobot-selftest.json
```

需要现有 ARM GCC、CMake/Ninja、Python3 和项目 QEMU；可继续使用本项目固定 Nix shell。
runner 复用既有测试生命周期，提供确定的按键、ADC 电压、BMI088 样本及六路 UART/三路
CAN 输入，检查精确数据和公开 PWM/LED 输出。它在 `iwdg-boot-grace-ms=0` 下实际执行
POR→软件复位→watchdog 复位并汇总所有启动的结果；FAIL、缺失项、错误输出或异常复位
返回非零，JSON 保存用例、各轮 `PHASE` 计数及 ELF/QEMU 哈希。任何一轮的失败计数都使
整套测试失败。USART1 的 `PHASE`/`DONE` 计数只属于当前
启动，整体结论以 JSON 为准。可传入 Debug ELF 或用 `QEMU_SYSTEM_ARM` 指定产物。

测试不连接用户持久化 image；内部 Flash 用例擦写未使用的末尾 sector 7，并检查镜像
边界；外部 KV 使用专用字段。该固件面向测试，不能用于含重要数据的实板。USB 真实
枚举、CORDIC 双参数、UART5 TX 及 plant 物理行为明确 SKIP，RNG 只检查功能和 refill。
当前固件仅16-bit ADC；低分辨率及其它芯片模式继续由各自下层门验证。此处结果是 HAL/BSP
consumer 回归，不能据此声明硅级精度、真实电气时序或整机 migration；本轮没有实板烧录。

FDCAN 当前发送侧支持 FIFO 模式（`NDTB=0`、`TFQM=0`），容量最多 32 帧；专用 TX
buffer、混合 buffer/FIFO 和按优先级排序的 TX queue 尚未支持。地址与容量解码修正后，
旧 FDCAN v1/v2 组件快照及共享 Message RAM link v1 快照会被拒绝；这不影响普通 ELF
启动。完整 machine migration 仍未支持。

FDCAN host socket 收到关闭事件后，会丢弃旧连接尚未完成的发送记录并取消重试；
已发送的前缀不会在新连接重放。关闭后新提交的完整帧
仍使用现有有界队列，重连后按顺序发送。`fdcan-host-ack` 和 TXBTO 沿用模型的准入及完成近似，
不能证明主机已收到完整帧。该规则不保证同一连接上的 reset/断电保留 partial frame，
也不提供 CPU 暂停时的任意输出排空。

发送遇到部分写入、零写入或 `EAGAIN` 时，支持原生可写通知的 chardev 使用一次性
可写事件继续发送保留的后缀，无需推进虚拟时间。当前使用默认主循环；不支持可写通知的
后端继续使用既有的 1 ms 虚拟时间重试。其它写入错误或 HUP 取消可写监听，保留虚拟
重试，连接关闭仍由原生 chardev 事件处理。成功发送导致的队列排空只表示后端接受了记录，不能证明
对端解码、CPU 中断处理或外部 plant 已完成。

可通过原生 QMP 读取当前主机发送队列：

```json
{"execute":"qom-get","arguments":{"path":"/machine","property":"fdcan-tx-state"}}
```

返回 `schema: 1`、QEMU 虚拟时间 `timestamp_ns` 和 `controllers`；控制器名称来自当前
板卡 profile，例如 `dm-mc02.fdcan1`。各控制器的 `pending_frames` 是尚未完成的记录数，
`pending_bytes` 是这些记录剩余的字节数；`tx_dropped`、`tx_short_writes` 是既有累计计数，
随控制器复位清零。此属性只读，查询不触发发送或推进虚拟时间，尚未初始化时返回错误。
`pending` 字段只描述当前仍待发送的记录；关闭连接、复位或断电也可能清空队列。
成功发送引起的排空仅表示后端接受，确认对端收到数据仍需实际读取并校验。查询也不能证明
此前没有发送错误，或 CPU、IRQ、外部 plant 已完成当前时刻的工作。

接收侧可查询同一 machine 的只读 `fdcan-rx-state`，返回 `schema: 1`、
`timestamp_ns` 和各控制器的 `completed_frames`、`partial_bytes`、`rx_dropped`。
`completed_frames` 在每个完整 84 字节 host wire 的同步处理返回后递增，包含格式错误、
过滤拒绝、INIT、断电或 FIFO 已满而丢弃的记录；部分记录和原生 CAN bus/内部 loopback
不计数。`partial_bytes` 是当前尚未组成完整记录的字节数（0–83）；查询不执行 IO、
改写寄存器或推进时间。主机发送队列为空本身不能证明 QEMU 已处理输入。

完成计数随控制器复位清零，不进入组件快照；关闭连接或断电保留计数并按既有规则清除
部分记录。计数在 `UINT64_MAX` 饱和，该值表示计数已耗尽，调用者发送前须保留严格低于
此值的余量。确认某一批输入需要原连接、唯一 writer、已知完整记录数量和发送前基线，
并排除额外输入、复位、restore、重连、断电及其它清理；这些事件使原确认失效。
`rx_dropped` 沿用既有累计/复位语义，完成计数不证明存储成功、固件中断或 BSP 消费，
也不按报文时间戳调度输入，不建立 CPU/world 时间屏障。

共享 chardev 发送 helper 在已写出部分字节后遇到阻塞或错误时，返回已写出的长度，
调用者可从剩余后缀继续；若首次写入即阻塞或失败，则仍返回错误。正数返回值只报告已接受的
字节数，不证明该次调用中未遇到后续错误，也不代表对端已消费。

主机代码可调用 `sim.boards.drain_paused_can(qmp, stream, sink, *, target_ns,
controller, chardev, tx_dropped, deadline, check)`，把已准入的 CAN 输出逐帧交给
`sink(frame)`；frame 沿用 `CanStream.receive()` 的 tuple。此操作要求原生执行目标已
`reached`、QEMU 保持 `paused`，并使用同一条普通非阻塞 AF_UNIX 连接、唯一 writer 和 reader，
不使用 mux、TLS 或 FD passing。
调用者必须在整个区间排除新发送、复位、断电、配置、重连和其它清队列事件，且在 producer
区间开始前建立 `tx_dropped` 基线。CPU 暂停本身不能证明这些前提。

`deadline` 是有限的 `time.monotonic()` 绝对期限；`check()` 检查进程存活，QMP 的单命令
超时仍由调用者配置，可复用 `deadline_qmp_call`。操作先观察发送队列为零，再要求一次新的
空读及空 framing tail，最后复核执行状态和队列。它每轮只读最多 4096 字节，不收集无限结果。
EOF、解码、状态变化、sink 或超时错误直接失败，保留部分效果和借用连接；调用者须停止该次
尝试。任意阻塞 callback 无法被此软期限抢占。此操作不推进 world，也不证明全局锁步、
同一时刻所有 timer/IRQ 已处理、任意复位恢复或真实 CAN 时序。

`sim.boards.wait_paused_can_input(qmp, stream, *, target_ns, controller, chardev,
completed_frames, deadline, check)` 使用相同的原连接、暂停目标及期限约束，刷新既有
发送队列，并等待原生接收计数精确达到 `completed_frames`。调用者必须在发送前取得
基线、知道本批完整记录数，并保证绝对目标严格小于 `UINT64_MAX`；不得用当前队列长度
推算记录数。整个尝试中须排除额外输入、复位、restore、断电、配置、重连或清队列。

先用既有 reader 排空 QEMU 输出。等待操作只对原 socket 做非消费 peek；存在未读输出、
framing tail 或原连接 EOF 时直接失败并保留字节，即使原生属性显示另一个 peer 已连接。
达到目标时还要求发送队列和原生部分记录均为空，再重新查询执行状态与接收计数。
返回最后一次原生控制器字典；`rx_dropped` 可以增长，因为处理完成包含被拒收的记录。
此返回值证明同步输入处理已返回，固件消费与存储成功仍须分别验证。错误保留已发生的
效果、后缀和借用资源，调用者须停止该次尝试；操作不负责恢复或推进 CPU/world。

尚未执行首个目标时，可用 `sim.boards.observe_initial_paused_can(qmp, stream, *,
timestamp_ns, controller, chardev, deadline, check)` 建立初始 CAN 基线。
它要求执行状态为 `idle`、没有目标且时间精确等于 RESET_ACK 的原点，QEMU 保持
`prelaunch` 或 `paused`；原连接、主机队列及原生发送和部分接收记录均须为空。
函数不读取或刷新字节，以非消费 peek 拒绝未读输出和 EOF，并复核两次完整状态，
返回最后的 `{"tx": ..., "rx": ...}` 控制器字典。期限、唯一 reader/writer 和排除
复位、断电、配置、重连等条件与上述函数相同。此操作只建立启动基线，不推进时间。

### trobot motor/MuJoCo test

在相同构建环境中同步项目 MuJoCo extra，再选择专用电机测试固件：

```bash
uv sync --locked --group dev --extra mujoco
cd ../trobot
cmake --preset MotorTestRelease
cmake --build --preset MotorTestRelease
cd ../dm-mc02-qemu
tools/run-dm-motor-smoke.sh ../trobot/build/MotorTestRelease/trobot.elf build/test-results/trobot-motor.json
```

也可使用 `MotorTestDebug`。runner 通过实际 `motor::dm`、HAL/BSP 和 FDCAN 交换 195 次
命令/反馈：禁用时发送控制、启用后小恒定扭矩、禁用滑行、再次启用反向制动和最终禁用。
既有 MuJoCo 无阻尼单转子模型与独立解析转动惯量核对位置/速度/力矩，固件检查实际回调、
raw 数据和物理单位。JSON 保存每帧轨迹及 ELF/QEMU/模型哈希；缺失或重复项、错误字节、
超时和 QMP/socket 清理错误均失败。`DM_MOTOR_DROP_FEEDBACK=1` 可故意丢弃第一帧反馈，
用同一命令确认失败会保留为 JSON 并返回非零。

测试夹具每条 control 固定推进 1 ms，独立于固件任务周期和 CAN 时间；这不是全局
lockstep、普通业务 PID、worker pacing 或实时门。四个阶段的解析速度恰好落在量化中点，
只接受两个相邻码，并在 JSON 写出 `expected_range`；位置与扭矩仍使用精确 raw 断言。
P/V/T=12.5/30/10 是测试 profile，状态 nibble=0 和温度=25°C 是固定反馈。`ff…fb` 为
达妙清除错误命令，既有 `reset()` API 不表示机械位置归零。真实物理标定仍显式 SKIP。
省略 ELF 参数运行原有最小 guest 与 NullEngine smoke，经 v2 RESET/STEP 交换反馈。

### Canonical test gate

完整门禁中的 MuJoCo Python 行为测试需要安装 `mujoco` extra；仅同步 `dev` 时，该测试会跳过并使完整门禁返回 BLOCKED。基础 QEMU 构建和最小 guest smoke 不需要此 extra：

```bash
uv sync --locked --group dev --extra mujoco --python "${PYTHON:-/usr/bin/python3}"
export DM_MC02_ELF=/absolute/path/to/trobot.elf
python3 tools/dm_mc02_test_gate.py --jobs 4
```

统一门禁执行 QEMU/Meson 测试、原生 Host CTest、完整 pytest 和 shell integration。它会创建 `build/test-results/qemu-gate/<run>/summary.json`，并保存各阶段日志、退出码、动态测试分母及测试前后的关键二进制 SHA-256。可通过 `--report-dir /absolute/path` 指定报告根目录。

这是回归门，不是硅级验证。寄存器、IRQ、时钟和虚拟时间用例优先使用 QEMU 原生 libqtest；外部进程测试共享 QEMU 启动、socket、超时和清理逻辑。关键硬件断言注明 ST CMSIS/HAL/LL 或 RM0468 来源；实板观测仍需独立核对。

| 退出码 | 含义 |
| ---: | --- |
| 0 | 回归 PASS |
| 1 | FAIL |
| 2 | 参数错误 |
| 78 | BLOCKED（依赖或环境不足） |

只有明确列出的 ROS 2 和 MuJoCo 可选 smoke 可以 SKIP。SKIP 不等于相应能力通过验收。`--no-build` 仅适用于源码与构建产物已同步的情况；`--smoke-only` 只运行 shell smoke，不代替完整门禁：

```bash
python3 tools/dm_mc02_test_gate.py --no-build --jobs 4
python3 tools/dm_mc02_test_gate.py --smoke-only --jobs 4
```

### Firmware RTF

短采样用于诊断固件 tick 推进和 watchdog 状态：

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
bash tools/collect-firmware-rtf.sh --warmup 0 --virtual-seconds 2 --ready-tick 250
```

这项采样依赖指定 ELF 暴露 `xTickCount` 与已知 watchdog 观测位置，不是任意固件的业务健康检查。正式 Release gate 从 reset 启动同一固件，在无外部 worker 的默认 DM-MC02 profile 下连续测量三轮、每轮 60 虚拟秒：

```bash
bash tools/run-release-rtf-gate.sh
```

`1.0x` 是目标；标准 gate 允许已记录的 `0.999x` 采样容差，但仍报告每轮精确 RTF、启动延迟、CPU、RSS 和 watchdog 结果。启动阶段默认使用 10 秒总期限；启动超时、复位、断连、watchdog 增长或采样不一致必须失败退出。短 smoke、空载吞吐或一次运行都不能替代该门，也不能证明面向用户的 wall-clock pacing。

性能结果只适用于本次的固件、宿主负载和运行配置；启用外部后端或更换固件后需要重新测量。

## Update and source package

### Update a checkout

```bash
git pull --ff-only
git submodule update --init --depth 1 qemu/upstream
```

子模块通常处于 detached HEAD，这是固定版本的正常状态。更新后重新运行构建脚本；切换 Nix 与 Ubuntu 依赖环境时，脚本会自动刷新 QEMU configure 元数据。

### Create and restore a source package

首次构建获取所需 Meson wraps 后，可以创建可校验源码包：

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
python3 tools/dm_mc02_source_package.py create build/source-packages/source.tar.gz
python3 tools/dm_mc02_source_package.py verify build/source-packages/source.tar.gz
python3 tools/dm_mc02_source_package.py restore \
  build/source-packages/source.tar.gz /absolute/path/to/new-restore
```

默认创建要求 QEMU fork 源码干净。若子模块是浅克隆，创建包前先补齐 fork 历史：

```bash
git -C qemu/upstream fetch --unshallow origin dm-mc02/v8.2.2
```

源码包不含 `.git`、ROM 子模块工作树、构建二进制或固件；ELF 仅按哈希识别。restore 的目标目录必须不存在，源码落在其 `project/` 子目录。`verify` 证明包内容与 manifest 一致，不代表已独立构建或测试；完整验收要在恢复环境重新配置、构建并执行测试门。

## Troubleshooting

| 现象 | 优先检查 |
| --- | --- |
| `unsupported machine type dm-mc02` | 启动 `build/qemu/qemu-system-arm`，不要使用系统 QEMU |
| 找不到 `qemu/upstream/configure` | 初始化固定 QEMU 子模块并确认其 gitlink 与 `qemu.lock` 一致 |
| QEMU 提示 Python venv/ensurepip 不可用 | Nix 使用本项目 shell；Ubuntu 安装 `python3-venv`，并将 `PYTHON` 指向 Python 3.11+ |
| QEMU 提示找不到 Ninja | Nix 使用本项目 shell；Ubuntu 安装 `ninja-build` |
| `fatal error: zlib.h: No such file or directory` 或 zlib 探针失败 | 检查编译器、sysroot 和 pkg-config 是否属于同一环境；Nix 使用完整 shell，Ubuntu 缺开发包时安装 `zlib1g-dev` |
| 已有 `/usr/include/zlib.h`，但 `cc` 来自 `.nix-profile/bin` | 编译器与 apt 开发库混用；使用完整 `nix develop` 环境，或改用 Ubuntu 系统工具链 |
| pytest 或项目 Python 环境不可用 | 使用所选环境的 Python 运行 `uv sync --locked --group dev --python "${PYTHON:-/usr/bin/python3}"` |
| 测试 guest 编译失败 | 检查 `arm-none-eabi-gcc`、`arm-none-eabi-objcopy` 与 `binutils-arm-none-eabi` |
| 终端没有 UART 文本 | 核对 `--uart` 与固件日志配置；当前 `trobot` Release 关闭 logger/terminal。原生 `-serial` 槽位 0 是二进制 co-sim |
| `trobot` 未进入 `example_task` | 用 GDB 找到首个阻塞位置；INS 初始化需要静止 IMU 输入，按 worker 示例复现 |
| Unix socket 无法连接 | 确认服务端、路径权限和路径长度；每个进程使用独立短路径 |
| RTF 采样失败 | 核对 ELF 哈希、`xTickCount`、watchdog、Release 构建、启动期限和宿主负载 |
| ROS 2/MuJoCo smoke 被跳过 | 安装相应可选依赖；ROS 2 还需 source 系统环境 |
| Flash 镜像没有保存 | 正常退出 QEMU，检查镜像精确尺寸、路径和 stderr |

仅当 Ubuntu/Debian 上缺少 zlib 开发包时，在仓库根目录执行：

```bash
sudo apt-get update
sudo apt-get install -y zlib1g-dev
PYTHON=/usr/bin/python3 QEMU_RECONFIGURE=1 bash tools/build-qemu.sh
```

若开发包已经安装，先保留现有构建树并收集实际失败命令；不要仅凭头文件报错推断缺包。在仓库根目录执行：

```bash
dpkg -L zlib1g-dev | grep '/zlib.h$'
command -v cc
cc --version
printf '#include <zlib.h>\n' | /usr/bin/cc -E -x c - >/dev/null
sed -n '/^\[binaries\]/,$p' build/qemu/config-meson.cross
ninja -C build/qemu -v -j1 qemu-system-arm > /tmp/dm-mc02-build.log 2>&1
```

系统 `/usr/bin/cc` 的探针与 QEMU 实际编译器是两个独立观测。保存日志中第一个 `FAILED:` 的完整命令与 include 栈，核对自定义 `CC`、`CFLAGS`、`LDFLAGS`、`PKG_CONFIG_PATH`、sysroot 和缓存 compiler。

当 `cc` 指向 `~/.nix-profile/bin/cc`，而 GLib/zlib 来自 `/usr` 时，说明构建混用了两套依赖。推荐更新源码后使用完整 Nix 环境：

```bash
nix develop --command bash tools/build-qemu.sh
```

脚本会重新选择编译器和开发库。运行生成的 QEMU 也使用 `nix develop`。如果选择 Ubuntu 系统工具链，可保留旧目录后重建：

```bash
mv -T build/qemu "build/qemu.nix-backup-$(date +%Y%m%d-%H%M%S)"
PATH=/usr/bin:/bin CC=/usr/bin/cc CXX=/usr/bin/c++ \
  PKG_CONFIG=/usr/bin/pkg-config PYTHON=/usr/bin/python3 \
  bash tools/build-qemu.sh
```

更新后的脚本会自动迁移 configure 元数据；也可用 `QEMU_RECONFIGURE=1` 明确请求完整配置。仅执行 `meson setup --reconfigure` 不保证重新选择编译器。确认新构建可用后，再删除自己保留的旧备份。

ADC1/2 完整读回三位 `CFGR.RES[4:2]`，目前仅支持编码 0–3（16/14/12/10-bit）。
编码 4–7 的转换启动被拒绝，并可通过 `-d guest_errors` 查看诊断；拒绝保留已有结果和标志。
任一转换组正在运行或等待触发时，RES 改写保留旧值。处理时间按 RM0468 第 228 表采用
8.5/7.5/6.5/5.5 ADC clocks，再加 SMPR 采样时间；虚拟纳秒截止时间向上取整。
结果从统一的 16 位输入按所选分辨率投影，14/12/10 位分别保留高 14/12/10 位，
DR/JDR 和 DMA 的范围一致。外部 raw 输入与电压输入仍使用原来的 16 位输入契约。
这些是数字行为；模拟采样、触发同步延迟、OFR、oversampling/LSHIFT 及优化分辨率模式仍未建模。

诊断 QEMU 未实现寄存器或 guest error 时，可创建输出目录后启用日志：

```bash
mkdir -p build/runtime
build/qemu/qemu-system-arm -machine dm-mc02 -kernel "$DM_MC02_ELF" \
  -nodefaults -display none -serial none -monitor stdio \
  -d unimp,guest_errors -D build/runtime/qemu.log
```

EXTI 与 GPIO/EXTI component 状态使用 v2；修正前的 v1 pending 布局被拒绝，
这仍不代表整机快照支持。CORDIC 使用官方 H723 地址 `0x48024400`。

## Repository map

| 路径 | 用途 |
| --- | --- |
| `qemu/upstream/` | 固定的 QEMU fork、芯片/板级模型和 QEMU 原生测试 |
| `cosim/` | 板卡无关的 C 模型、wire protocol 与 transport adapter |
| `sim/` | 通用 host coordinator、transport、CAN Fabric、器件和简单 plant |
| `tools/` | 构建、worker、QMP、RTF、源码包和测试 gate |
| `tests/` | Host C、Python 与协议测试 |
| `flake.nix`、`flake.lock` | Nix 环境与依赖锁定 |
| `build/` | 本地构建产物、测试报告和运行文件 |

## Virtual CAN and M3508

`sim/core` 提供板卡无关的 session/participant 协调器；`sim/transports` 保留有界的
stream/datagram 队列。`sim/fabric` 在主机侧按时间戳、固定延迟和准入顺序交付 CAN 帧，
可配置 loopback、drop predicate 和日志 sink。已有 worker 入口与默认电机协议保持兼容。

Python 的 `CanBus.delay_ns` 可在该总线的交付回调之外修改，接受
`0..2^64-1` 的普通 `int` 纳秒值（不含 `bool`）。
新值只影响后续入队帧；已经排队的截止时间和等时 FIFO 顺序保留，减小延迟时
新帧可以先于旧帧交付。无效值或该总线交付回调内的修改会拒绝，reset 保留当前配置。
交付仍由现有 step 完成；这是主机侧队列策略，不建立 QEMU CPU 时间屏障。
Scenario 的 `can_delay` 动作通过既有 Arena 和共享世界接口修改此策略。

连接已有 QEMU co-sim 和 FDCAN Unix socket 时，可使用专用 M3508 入口：

```bash
python3 tools/run-can-worker.py --cosim /path/to/cosim.sock --fdcan /path/to/can.sock \
  --protocol v2 --realtime --m3508-id 1 --m3508-id 5
python3 tools/run-can-worker.py --help
```

ESC ID 可重复指定，但必须互不重复且在 1–8；可添加 `--socketcan can0` 接入已有 Linux
CAN 接口。参数 `--can-delay-ns` 默认 0，`--m3508-timeout-ms` 默认 100，
`--torque-per-amp` 默认 0.02 Nm/A。配置在连接前校验；消息或发送队列耗尽时明确失败。

M3508 接收 C620 标准 DATA DLC8 的 `0x200`/`0x1ff` 分组电流命令，输出 `0x201`–`0x208`
转子角度、转子 RPM、电流与温度反馈。固件负责减速比换算。当前使用未标定的简单转子
(J=0.02 kg·m²、阻尼 0.08 Nm/(rad/s))、固定 25°C、命令电流代替实际电流测量。
默认每虚拟毫秒反馈，较大 step 只发送最新样本；100 ms 命令超时后零驱动并继续反馈。
这些是公开的模型近似；C620 手册没有提供这里使用的数值超时保证。

需要由外部 plant 驱动时，使用 `sim.devices.dji.M3508Device`：先
`prepare_step(ctx)` 取得转子力矩与命令截止时间，推进物理模型，再用
`complete_step(plan, position_rad, velocity_rad_s)` 提交转子采样。准备阶段不推进时间
或发送反馈；完成阶段只接受该器件返回的原 plan。一次只允许一个 pending plan，
reset/close 会取消它。发送失败保留已提交状态，调用方必须冻结并 reset，不能重复完成
或重复推进 plant。`M3508` 保留原简单转子用法，并复用这一协议边界。

`M3508JointPlant` 把多个协议器件绑定到同一个 `MujocoPhysics` 或 `GazeboPhysics`，取得这些器件与
物理模型的独占执行所有权。绑定必须使用 `M3508Device`，每个 joint 在 world 内唯一，
电机 ID 在各自 CAN bus 内唯一；独立总线可以复用同一 ID。四轮模型的完整组合如下：

```python
from sim.core import Coordinator, Session
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.fabric import CanBus
from sim.fabric.runtime import CanParticipant
from sim.physics import MujocoPhysics

corners = ("fl", "fr", "rl", "rr")
physics = MujocoPhysics.from_xml_path(
    "models/four_wheel.xml",
    joints={f"wheel_{c}": f"drive_{c}" for c in corners},
    imus={"imu": ("imu_gyro", "imu_accel")},
)
bus = CanBus()
devices = {M3508Device(bus, i): f"wheel_{corner}"
           for i, corner in enumerate(corners, 1)}
# 在 reset/step 前把主机或 QEMU CAN endpoint 接到 bus。
shared = M3508JointPlant(physics, devices)
coordinator = Coordinator({"can": CanParticipant(bus, [shared])})
coordinator.reset(Session(1))
coordinator.step(1_000_000)
coordinator.close()
```

`QemuCanEndpoint.submitted_frames` 是端点生命周期内发送调用正常返回的累计帧数，
包括仍留在 transport 队列中的完整帧或未发送后缀；更换 RESET mapper 或逻辑 reset
保留这个只读计数。它不表示 socket 接收、QEMU 处理、FIFO 接受或固件消费。
发送异常可能已经产生部分 I/O，但不增加计数；调用方须冻结，不能据此重放或恢复发送。

在 Gazebo SDK Python 环境中也可用同一 owner 绑定公开转子：

```python
from sim.core import Coordinator, Session
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.fabric import CanBus
from sim.fabric.runtime import CanParticipant
from sim.physics import GazeboPhysics

physics = GazeboPhysics("models/gazebo_rotor.sdf",
                        joints={"shaft": ("rotor", "shaft")})
bus = CanBus()
device = M3508Device(bus, 1)
try:
    shared = M3508JointPlant(physics, {device: "shaft"})
except BaseException:
    device.close()
    physics.close()
    raise
coordinator = Coordinator({"can": CanParticipant(bus, [shared])})
try:
    coordinator.reset(Session(1))
    coordinator.step(1_000_000)
finally:
    coordinator.close()  # closes owned devices and physics
```

默认减速比 `g=3591/187` 来自 [DJI M3508 P19 V1.0 手册](https://rm-static.djicdn.com/tem/17348/RoboMaster%20M3508%20P19%20Brushless%20DC%20Gear%20Motor%20V1.0.pdf)
的规格表。adapter 使用理想无损换算：转子角度和速度为 joint 的 `g` 倍，joint
力矩为转子力矩的 `g` 倍；XML motor 保持 gear=1。效率、背隙及转子惯量未经标定。
每个命令截止区间只推进一次共享物理模型，收齐全部终点采样后才生成反馈。
构造时先检查所有绑定、初始采样及 ±20 A 对应的最大 joint 力矩；失败时输入仍由
调用方拥有。每次推进先检查总步长、按顺序投影的全部超时分段和力矩，再写入控制。总步长必须
符合后端单次 step 的限制，Gazebo 为 1–10000 个实际 native ticks；每个超时分段也
须精确对齐。命令超时或分段不对齐会拒绝并冻结 owner，不能自动舍入；Session
绝对起点不需要对齐 native tick。反馈周期不用于物理分段。
reset 只重置一次物理模型；native 时间归零，CAN/session 时间仍以所给 Session 为原点。
任一运行失败会冻结这个 owner，直到成功 reset；已推进的物理状态和已排队反馈不回滚。
`close()` 独立尝试关闭所有拥有的器件与 physics，保留首个错误及其它清理诊断；重复
调用不重试。借用的 CAN bus 和 transport 由调用方负责。

多条独立 CAN bus 可共用一个 `M3508JointPlant`，所有命令截止时间仍在同一 world
内合并，每个区间只积分一次。plant 不推进或重置总线；用
`CanParticipant((bus_a, bus_b), [shared])` 统一交付起点命令、推进这个唯一 plant，
再交付目标反馈。总线集合须为非空 tuple，身份唯一；list、set 和 iterator 拒绝。
`.buses` 保留准入顺序，`.bus` 仅用于单总线。不要为每条总线构造拥有相同 physics
的 plant；`CanRuntime` 和固定 `FourWheelRuntime` 仍只接单条总线。

这个示例把未标定四轮夹具的前、后轮分别接到独立总线，每条总线使用 ID 1、2：

```python
from sim.core import Coordinator, Session
from sim.devices.dji import M3508Device, M3508JointPlant
from sim.fabric import CanBus
from sim.fabric.runtime import CanParticipant
from sim.physics import MujocoPhysics

corners = ("fl", "fr", "rl", "rr")
physics = MujocoPhysics.from_xml_path(
    "models/four_wheel.xml",
    joints={f"wheel_{c}": f"drive_{c}" for c in corners})
buses = (CanBus(), CanBus())
bindings = {M3508Device(buses[i // 2], i % 2 + 1): f"wheel_{c}"
            for i, c in enumerate(corners)}
try:
    shared = M3508JointPlant(physics, bindings)
except BaseException:
    for device in bindings:
        device.close()
    physics.close()
    raise
coordinator = Coordinator({"can": CanParticipant(buses, [shared])})
try:
    coordinator.reset(Session(1, 7))
    result = coordinator.step(1_000_000)["can"]
    print(result.reached_time_ns, result.diagnostics)  # 1000007; target broadcasts=4
finally:
    coordinator.close()
```

编排先校验目标 CAN 时间和全部总线起点，再按 tuple 顺序各交付一次起点帧。
全部总线的起点到期帧须清空后才推进器件。回调可向其它总线入队；后面的总线向已
访问总线入队起点帧时，本次 step 会在 plant 推进前失败，需成功 reset 才能继续。
回调不得自行推进或重置总线。目标时刻同样各交付一次，积压可留到下一步起点；
诊断的 `delivered_start`、`delivered_target` 是全部总线的广播数之和，`pending`
为总积压。失败保留已产生的效果，不回滚或重放。这一固定顺序不提供跨总线仲裁、
公平性或多 MCU 的虚拟时间屏障。

每个主机 step 先交付起点命令，再推进器件，最后交付目标时刻反馈；区间内才到期的
命令在目标时刻交付，并从下一区间生效。ACK/DONE 等待只收发和入队，不推进器件。
RESET 保留 transport 的 partial bytes，并更新 QEMU/worker 时间原点。CAN wire 没有
session 标识，因此不提供跨 reset 的 CAN 历史输入隔离。这个调度不提供位级仲裁、
真实电气总线、CPU 全局锁步或实物电机标定。

### Shared board/world runtime

`sim.robots.shared_world.SharedWorldRuntime` 将多个独立 QEMU 连接接到同一个
`M3508JointPlant`。调用方先构造各板的 `QemuCanEndpoint`，再用非空 tuple
绑定名称、endpoint、`StepFramedSocket` 和具名 IMU。运行时从 endpoint 的只读
`.stream` 取得实际 CAN transport；所有连接须已打开、非阻塞且互不重复，
plant 中的器件总线须与绑定总线完全一致。构造会检查全部具名 IMU，成功后
运行时独占 plant 的推进和关闭；构造失败时仍由调用方清理 plant。

```python
import time
from sim.core import Session
from sim.robots.shared_world import BoardBinding, SharedWorldRuntime

# plant、endpoint_a/b、cosim_a/b 已由调用方配置和连接。
runtime = SharedWorldRuntime(plant, (
    BoardBinding("a", endpoint_a, cosim_a, "imu_a"),
    BoardBinding("b", endpoint_b, cosim_b, "imu_b"),
))
try:
    reset_acks = runtime.start(Session(1, 7), (101, 102),
                               deadline=time.monotonic() + 2)
    runtime.set_can_delay("a", 5_000_000)
    step_acks = runtime.step(1_000_000, deadline=time.monotonic() + 2)
    runtime.service(deadline=time.monotonic() + 2, timeout=0)
finally:
    runtime.close()
    # 调用方另行关闭所有借用连接，并清理 QEMU/QMP。
```

上例由调用方拥有 CPU 执行。需要运行时分段推进所有 CPU 时，为每个绑定增加
`BoardExecution(bounded_command, "dm-mc02.fdcan1", "can1")`；必须全部提供或全部省略。
`bounded_command(name, arguments=None)` 借用原生 QMP，并由调用方限制每条命令耗时。
QEMU 须以 `-S -accel tcg,thread=single -icount shift=0,align=off,sleep=off`
启动。CAN 必须使用原始非阻塞 AF_UNIX socket 和标准 `CanStream`，运行时独占其
读写；期间保持连接、配置、dispatch budget 和复位状态不变，回调不得直接提交 CAN。

`start()` 只允许尝试一次：全部实际 RESET_ACK 到齐后，将各自 QEMU 时间原点
映射到所给 Session 原点，再统一重置总线和世界一次。启动期间保留物理 CAN
输入，不提前消费。每个 `step()` 先有界服务连接，再调用一次 Coordinator；
plant 沿用既有命令超时分段。全部终点 IMU 编码成功后才发送各板 STEP，返回
实际 ACK tuple；重试复用编码 bytes。`service()` 只收发和入队，不推进世界。

提供 `BoardExecution` 时，启动还检查各原点处的初始空 CAN 基线。每步先检查全部
目标和输入计数余量，再请求所有 CPU 执行到映射后的目标；实际 `reached/paused`
到齐并排空所有 CAN 输出后，才推进一次既有世界和发送 IMU。全部反馈达到精确的
原生输入处理计数后才返回，下一步 CPU 执行须等到此处。此模式的 `service()` 和
ACK 等待不额外读取或刷新 CAN。`can_input_states` 返回最近完成的各板原生 RX
快照副本；初始化、过滤或 FIFO 拒绝也计入处理数，`rx_dropped` 保留为诊断。
区间内命令仍在目标边界交付并从下一区间影响物理世界。CPU 目标完成不代表同刻
IRQ/器件事件全部闭合，输入处理也不代表固件消费；此组合不提供硅级时序或实时保证。

`set_can_delay(name, delay_ns)` 在 start 成功后修改指定成员总线的后续入队延迟。
值遵守 `CanBus.delay_ns` 的整数范围；命令和反馈共用该策略，已排队帧保留原截止时间。
尚未读取的物理输入在后续入队时使用新值。调用本身不收发、推进世界或发布裁判数据；
无效名称或数值保留原策略及运行状态，执行和清理回调内的调用拒绝。
关闭 owner 保留借用总线的配置。最大合法延迟可能使后续 step 的时间范围检查失败，
该执行失败仍冻结 owner。此接口不提供 Scenario 动作调度或 QEMU CPU 时间屏障。

可传入 `check_running()` 检查调用方拥有的进程；
`on_response(name, frame, decoded_payload)` 观察返回的 ACK 和闲置连接的回复，
DONE 保留实际 status/consumed/missing。既有主动等待器会跳过部分 advisory
回复，因此此回调不是完整录制接口。每条连接的 RX 由主动等待器或闲置 poll
独占。绝对 monotonic deadline 在收发及推进前后检查；阻塞 solver/callback
仍须由外层进程限时。ACK 服务期间的回调或 transport `TimeoutError` 会以
`RuntimeError` 传播并保留原 cause；只有 ACK 等待器自身的超时可以触发重试。
任一运行失败或观测到原生 RESET 都冻结 owner，须重新
创建完整 owner 和板卡进程；已推进世界和已发送数据保留。`close()` 仅关闭
拥有的 plant，不 flush 或关闭借用的连接和总线。

`bash tools/run-qemu-shared-world-smoke.sh` 使用两个真实 QEMU 和同一四 hinge
MuJoCo 夹具，验证独立会话、实际 CAN 电流命令/反馈、具名 IMU 和 BMI088 raw
消费。固件先经 ready/按键发送命令，主机保留完整 wire 后暂停 CPU，统一完成
RESET 和一个 10 ms world tick，再恢复 CPU 读取反馈与 SPI 数据。报告分别保留
实际 ACK、DONE masks、UART 完成标记以及失败/清理结果；这是固定 bare-MMIO
夹具，尚未验证 HAL/BSP 闭环、CPU 全局时间屏障或
实物标定。没有安装 MuJoCo 时该原生门明确返回 SKIP 77。

### MuJoCo 双底盘场景

`models/two_four_wheel.xml` 在同一个原生世界中组合两个现有四轮夹具，
名称分别以 `alpha/`、`bravo/` 开头。两个底盘的初始位置为 `(0, 0, .03)`
和 `(0, .6, .03)` 米，共用一个地面。模型直接引用 `four_wheel.xml`，
由 MuJoCo 3.12 的 `attach` 处理名称、关节、执行器和传感器引用；
`compiler conflict="error"` 拒绝子模型与世界的全局参数冲突。

既有物理接口可以直接加载该场景：

```python
from sim.physics.mujoco import MujocoPhysics

corners = ("fl", "fr", "rl", "rr")
names = ("alpha", "bravo")
physics = MujocoPhysics.from_xml_path(
    "models/two_four_wheel.xml",
    joints={f"{name}/wheel_{corner}": f"{name}/drive_{corner}"
            for name in names for corner in corners},
    imus={name: (f"{name}/imu_gyro", f"{name}/imu_accel") for name in names},
)
try:
    physics.step(1_000_000)
    for name in names:
        print(physics.get_body_pose(f"{name}/chassis"), physics.get_imu(name))
finally:
    physics.close()
```

接入电机时，一个 `M3508JointPlant` 拥有全部八个器件和这一个 physics，
一个 `CanParticipant` 统一服务两条总线；每条总线可独立使用 ID 1–4。
隔离和直接消费者测试可运行：

```bash
uv run --frozen --extra mujoco python -m pytest -q tests/test_four_wheel_physics.py
```

测试覆盖两个底盘的地面支撑、跨底盘接触、独立电流命令、运动、IMU、反馈和复位。
尺寸、质量、摩擦及齿轮参数仍未标定。

两个真实 QEMU 接入此固定场景的验证入口为：

```bash
uv run --frozen --extra mujoco bash tools/run-qemu-shared-world-smoke.sh --two-chassis
```

该选项默认关闭，两个 bare-MMIO 固件分别使用独立 CAN 总线上的 ID 1–4。
`alpha` 发送四路 `+64` 电流命令，`bravo` 左轮发送 `-21`、右轮发送 `-107`。
主机从全新原生复位状态推进一次 10 ms，记录两个底盘的位姿、八个关节、
实际反馈和各自具名 IMU。这里的一次调用只执行一次原生积分，不包含预热或
十次 1 ms 子步。实际 float32 STEP 与完成的 SI 样本逐轴对应，固件读取的
BMI088 raw 数据须落在各自半 LSB 区间，并排除另一底盘的来源。
报告同时要求两个真实 full-consumed DONE 和最终 sensortime 写回后的 UART 标记。

可追加 `--reset` 或 `--peer-exit` 验证单板复位或退出使共享 owner 冻结，
不再推进世界；这些负例按预期返回非零并保留失败 JSON。原四 hinge 默认门仍
独立验证其解析轨迹。此入口验证固定双底盘数据交付，HAL/BSP 闭环、
实物标定和 CPU 全局时间屏障仍需后续验证。

### trobot DJI driver test

使用默认关闭的专用固件验证实际 `motor::dji` 驱动。普通业务 preset 不启用它，
`DjiTest` 与 `MotorTest` 不能同时选择：

```bash
cd ../trobot
cmake --preset DjiTestDebug
cmake --build --preset DjiTestDebug --clean-first
cmake --preset DjiTestRelease
cmake --build --preset DjiTestRelease
cd ../dm-mc02-qemu
bash tools/run-trobot-dji-test.sh ../trobot/build/DjiTestRelease/trobot.elf \
  build/test-results/trobot-dji.json ../trobot
```

第三个参数是产生 ELF 的固件源码目录，报告保存实际文件哈希；使用独立构建副本时，
应传该副本。可用 Debug ELF 和 `QEMU_SYSTEM_ARM` 指定的 QEMU 重复验证。
runner 通过 ID1/ID5 覆盖两个电流命令组，独立 literal 反馈检查实际角度、转子 RPM、
减速后速度、电流和转矩解码，再检查正反向输出、禁用、重新启用、定向掉线与恢复。
掉线阶段只丢弃 `0x201`，保留 `0x205`；固件持续刷新命令，证明 50 ms 反馈超时
导致实际驱动任务清零。报告同时保留真实零电流 wire、冻结的反馈时间戳与新鲜命令时间。

夹具用 QEMU CAN 时间戳决定可推进的 1 ms host ticks，同一 tick 的两个命令组只推进
一次；tick 内到达的命令在下一边界交付，最多量化 1 ms。它不建立 CPU/plant 全局锁步。
观测与追赶有界，FAIL、缺失项、清理失败或输入哈希变化均返回非零并保留报告；实物
plant 校准显式 SKIP。此专用固件验证与普通 Release 业务、持续性能门分别执行。

Scenario 的 CAN 延迟消费者使用默认关闭的 `DjiScenarioTestDebug/Release`：

当前 Debug/Release 原生场景门尚未通过：QEMU CPU 虚拟时钟推进快于共享世界的
1ms 步，带未来时间戳的周期命令积压后触发 CAN 容量拒绝。该入口保留诊断报告；
CPU 与共享世界时间边界落实前，不能把这个夹具视为已验证的原生消费者。

```bash
cd ../trobot
cmake --preset DjiScenarioTestRelease
cmake --build --preset DjiScenarioTestRelease --clean-first
cd ../dm-mc02-qemu
.venv/bin/python tools/dm_mc02_dji_test.py --scenario \
  ../trobot/build/DjiScenarioTestRelease/trobot.elf \
  build/test-results/dji-scenario-release ../trobot \
  --qemu build/qemu/qemu-system-arm
```

夹具生成固定双机器人 Scenario，并通过同一 Scenario/Arena 入口运行。两个 MCU
使用相同 ELF，真实驱动 ID1/2 输出 `64/-64`；另外两个命令槽保持零。east 总线在
RESET 相对 0ms 设置 5ms 入队延迟，在 250ms 恢复零延迟，west 保持零延迟。
已排队帧保留原截止时间；报告分别记录来源、交付时间、原始 CAN 字节和固件复制的
反馈字段及 BSP 时间戳。相同反馈字节只能证明来源匹配，不能识别唯一一次固件回调。
两边第一次完整反馈报告必须早于 250ms，否则测试失败。

恢复延迟后，既有 UART1 握手释放第二次应用观察；握手不改变事件时间或推进世界。
结果目录必须不存在。缺失样本、FAIL、发送或清理失败、输入变化均保留失败报告。
该门不证明动态延迟下的 50ms 超时、物理 CAN 时序、CPU/plant 全局锁步或实时性能；
物理校准保持显式 SKIP。旧 ID1/ID5 独立掉线测试沿用上述入口。
