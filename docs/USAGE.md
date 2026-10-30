# DM-MC02 QEMU 使用手册

项目以真实 MCU ELF 为输入，用一个 `Simulation` 组合机器、通信网络、虚拟设备和物理世界。
当前唯一真实 Machine Profile 是 DM-MC02 / STM32H723；改变 `model` 名称不能增加 MCU 支持。
Python 3.11+ 核心、静态场景校验和纯 Python 设备不要求 QEMU 或 MuJoCo。完整原生运行以
Linux x86_64 为交付环境；其它宿主的原生构建尚未验证。

命令默认从仓库根目录执行。按需要阅读：

- [安装](#installation)与[原生构建](#build)
- [sim/v1 场景与唯一运行时](#simulation)
- [互连与精度](#connections)
- [有限实验、时间线与离线复核](#experiments)
- [四轮与多板应用预设](#application-presets)
- [HAL 教学](#hal-teaching)与[保留的交互 Lab](#virtual-lab)
- [原生调试与接口](#native-debug)、[外部 worker](#external-simulation-worker)
- [验证与性能](#testing-and-performance)、[故障排查](#troubleshooting)

<a id="installation"></a>

## 安装

```bash
git clone --branch main https://github.com/lym12321/qemu-dm-mc02.git
cd qemu-dm-mc02
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
sim --help
sim validate examples/can-faults/host-only.yaml
sim run examples/can-faults/host-only.yaml --report build/host-only.json
```

这个三端点 CAN 实验没有固件或物理求解器，验证延迟、定向丢帧及恢复；不能作为原生后端证据。
基础安装只依赖锁定的 PyYAML。可选依赖分别安装：

```bash
python -m pip install '.[mujoco]'       # 原生 MuJoCo 世界
python -m pip install '.[trace]'        # MCAP 记录与离线复核
python -m pip install '.[referee]'      # 固定版本裁判协议 fixture
```

开发测试使用 `uv sync --locked --group dev`；需要哪种可选后端，再添加对应 `--extra`。
`sim --help`、`sim validate` 不加载 QMP client、物理求解器、MCAP 或裁判 CRC 库。

### 原生后端与可安装 QMP client

原生固件运行先[初始化子模块并构建](#get-the-source)项目 QEMU，再构建所需 ELF。
默认二进制为 `build/qemu/qemu-system-arm`，也可使用 `QEMU_SYSTEM_ARM` 或 `sim run --qemu BIN`。
普通系统 QEMU 没有本项目板卡和执行能力，不能替代。后端要求固定的 QMP client、
`x-run-to-virtual-time`、`x-query-virtual-execution` 及 CAN execution QOM 能力；不满足会明确报错。
静态校验不会启动它们，也不证明 ELF 或二进制可运行。

从当前固定子模块构建完整的上游 Python distribution 与许可证：

```bash
uv venv build/qmp-package-env --python 3.11
uv pip install --python build/qmp-package-env/bin/python setuptools==75.8.0 wheel==0.45.1
python tools/dm_mc02_qemu_bundle.py build --python build/qmp-package-env/bin/python --output build/qemu-bundle
python tools/dm_mc02_qemu_bundle.py verify build/qemu-bundle
uv pip install --python .venv/bin/python build/qemu-bundle/python/qemu-*.whl
```

输出目录必须不存在。运行时使用安装包的版本和源码指纹，不从 checkout 偷加载 QMP，
不复制私有 client，也不接受任意同名 PyPI 包。显式给出兼容二进制后可在 checkout 外运行；
`--backend-root` 只帮助寻找默认二进制。fork 升级须同步 client 来源并重跑原生合同。

可用 `--qemu BIN --binary-provenance FILE` 把二进制装入 bundle；provenance 要求
`schema: dm-mc02/qemu-binary/1`、固定 `source_commit` 和实际 `sha256`。
bundle 保存调用者提供的来源、字节哈希、命令和许可证；动态运行库由部署环境提供，
来源清单不代替原生验收。

<a id="simulation"></a>

## sim/v1 场景与唯一运行时

先构建真实裸机 ELF，再运行同一场景：

```bash
cmake -S examples/minimal-board -B examples/minimal-board/build -DCMAKE_BUILD_TYPE=Debug
cmake --build examples/minimal-board/build
sim validate examples/minimal-board/scene.yaml
sim run examples/minimal-board/scene.yaml --report build/minimal-board.json
```

`examples/minimal-board` 的 Cortex-M7 固件实际使用 SysTick、USART1 和 A15→B0。
`examples/can-motor` 用同样构建方式，实际收发 FDCAN1/C620 数据。
场景只有一套 `schema: sim/v1`：

```yaml
schema: sim/v1
runtime: {mode: stepped, quantum: 1ms, duration: 200ms}
nodes:
  main:
    kind: machine.qemu
    model: dm-mc02
    firmware: ./build/firmware.elf
    uarts: [uart1]
  can0: {kind: bus.can}
  motor0:
    kind: device.dji-m3508
    motor_id: 1
    plant: fixed-rotor
    position_rad: 0
    velocity_rad_s: 0
links:
  - [main.fdcan1, can0.can]
  - [motor0.can, can0.can]
```

文件路径相对 YAML 目录。省略网络与设备可运行单板；核心不依赖机器人、比赛或教学应用。
准入拒绝未知字段、重复 key、anchor/alias、不支持的 kind/model/端口、方向/能力不匹配和重复连接。
文件最多 1 MiB、64 个节点、256 条连接；时间使用整数 `ns/us/ms/s`，配置时长最多 3600s。
实验因增加一个无资源观察节点，最多接收 63 个用户节点。`validate` 不创建进程、socket 或求解器。

Python API 与 CLI 共用 Factory admission 和执行路径：

```python
from sim.composition import builtin_registry
from sim.config.scene import load_scene
from sim.runtime import Simulation

registry = builtin_registry()
scene = load_scene("examples/minimal-board/scene.yaml", registry)
with Simulation(scene.topology, registry, quantum_ns=scene.quantum_ns,
                on_event=lambda event: print(event.kind, event.mapped_time_ns)) as simulation:
    simulation.schedule_input("main.key", True, at_ns=40_000_000)
    simulation.schedule_input("main.key", False, at_ns=90_000_000)
    simulation.run_until(scene.duration_ns)
```

显式 Registry 注册 Factory；不自动发现或导入配置里的 Python 路径。
`describe(spec)` 返回纯 `NodeDescription`/`PortSpec`，`create(spec, context)` 才取得资源。
`context.attach(port, endpoint)` 借用连接 Owner 的协议句柄；设备不拥有总线、机器或其它节点。
连接、binding owner、其它组件按组构建，组内保留声明顺序，循环资源依赖提前拒绝。
`QemuFactory(profile=...)` 注入板级端口、输入规则和外设 adapter；Backend 独占进程、QMP、
socket 和 CPU request/finish，Profile 不能推进 CPU。Fake Profile 只证明接口合同，不代表第二颗 MCU。

### 时间、状态与失败合同

一次非零区间从完成切点 `t` 开始：

1. `settle(t)` 按入队顺序应用同刻输入，按 Owner 声明顺序派发到期工作；新到期工作继续受有限预算约束。
   稳定后对 `input_commit` 组件提交当前输入批次。
2. 下一切点为目标、`t + quantum`、已排定输入和 Owner 已知截止时间的最小值。
   settle 后仍不晚于 `t` 的 deadline 是错误，不能回退或零步自旋。
3. 先预检全部 Machine 的目标，再请求全部 Machine，完成全部 CPU，随后推进非 Machine 组件。
4. 采样完成的源状态，交付目标切点输入与网络事件，提交输入批次，最后提交 Simulation 时间和 step ID。

初始化只采样一次。plant 使用区间开始已生效的控制；本区间产生的 CAN/IMU/GPIO 最早在
区间末交付，只能影响下一段 CPU/物理执行。已知输入可精确切分区间；GPIO 快照无法恢复
采样点之间未记录的短脉冲。输入 transport 接受、原生 FIFO 处理和固件应用消费是不同证据。

Event 保留 session、稳定序号、node/port、`source_time_ns`、`mapped_time_ns`、
`delivery_time_ns` 和 `exact/sampled/lower_bound/upper_bound`。CAN 保留实际 wire 来源，
UART 使用读取后的完成态上界；`can-input-processed` 仅表示原生 transport 处理已返回。
默认历史只保留最近 256 个事件并公开丢弃数；完整记录使用回调或 trace。

静态输入拒绝可修正；执行队列超限、I/O、组件或期限错误使当前 Simulation FAILED 并冻结，
保留部分效果，不重步、重放或伪回滚。默认外部输入容量 256、全局派发预算 4096，Owner 各有有限预算。
健康暂停态 `reset()` 重建 owned 组件并递增 session，host 总期限不重新计时；失败态应关闭并新建。
`close()` 独立清理资源并保留主错误与辅助诊断；只有清理完成才发布成功。

嵌入已有会话可传 `initial_session=Session(id, origin_ns)`。借用有限绝对 host deadline 时，
`start` 和后续每次 `step/run_until/reset` 都须显式传 `deadline_monotonic`。
同步第三方调用无法由 Python 抢占，组件必须遵守借用期限。

`runtime.mode: interactive` 使用同一切点，支持 `run`、`pause`、`step 1ms`、`state`、
`inject main.key true`、`inject main.uart1 "text"`、`reset`、`quit`。
每轮至多一条命令与一个 step，输入按 4096 字节有界读取；`run` 只提供 host pacing。
默认启动预算 10s、stepped 总预算 30s、interactive 总预算 300s；用 `--startup-timeout/--timeout` 调整。
单机器可使用 `--gdb-port`、`--qmp`，手动 GDB resume/step 会破坏 Runtime 的切点前提。

<a id="connections"></a>

## 互连与精度

### UART 字节流与遥控器 RX

`net.uart` 连接两个双向 `uart` endpoint；`device.uart-echo` 是独立的无帧回显外设。
字节保持有序，主机分片不代表消息边界；单次最多4096字节，默认待交付上限为256块、
65536字节，`max_pending`、`max_pending_bytes`、`dispatch_budget` 和 `delay_ns` 可显式配置。
队列耗尽、断连或部分写入失败会冻结本次执行，不重放；关闭网络不关闭借用的 endpoint。
连入网络的机器端口拒绝额外的 `schedule_input`，没有隐式仲裁。

```bash
cmake -S examples/uart-link -B examples/uart-link/build -DCMAKE_BUILD_TYPE=Debug
cmake --build examples/uart-link/build
sim validate examples/uart-link/scene.yaml
bash tools/run-simulation-smoke.sh --case uart-link
bash tools/run-simulation-smoke.sh --case uart-link --build-type Release
```

验收检查真实固件的二进制请求、双向交付、实际 RX 后的校验和与 GPIO 变化。
原生 UART 事件的时间是完成切点的读取上界；交付只表示 transport 接受，不表示固件已消费。
这里不模拟波特率、逐位时刻或电气波形。

DM-MC02 的 UART5 只引出遥控器 RX。机器配置 `remote: true` 暴露专用的 `main.remote`
原始 `bytes` 输入；`uarts` 只接受 uart1/2/3/7/10。remote 不提供 TX、文本转换或
`net.uart` 连线，也尚不提供 SBUS-A/DR16 编码与反相电平模型。

### 数字电平与时间戳边沿

`net.digital` 读取每个完成切点的 GPIO 电平；两个快照之间的短脉冲可能完全不可见。
缩小 quantum 不能恢复后端没有记录的边沿。

独立的 `net.digital-edge` 只接受声明 `event-output` 的显式事件源，连接一个输出和至少一个输入。
源发送 `DigitalEdge(level, source_time_ns, mapped_time_ns)`，接收端实现 `receive_edge(edge)`。
源负责时钟映射和观测依据；网络按单调时间及入队序号保留每个事件，同时间的事件不合并。
公开 delivery 事件另记实际 `delivery_time_ns`，其下限为发现事件时已经完成的 CPU 切点，
因此历史源时间不能追溯影响已完成的区间。`exact` 表示显式事件源提供的时间，不能用于 GPIO 快照推断。
默认容量256个事件、每切点 dispatch budget256，最多256个 endpoint；耗尽或接收失败会冻结，
不重放部分交付。close 不关闭借用的源和接收端。

```bash
python examples/edge-events/main.py
python -m pytest -q tests/test_edge_fabric.py
```

示例的纯 Python 源在同一1ms区间产生四次翻转（含相邻及同时间事件），全部在完成切点有序交付。
当前 QEMU 仅公开完成态 GPIO 快照，没有可信原生边沿通道，因此不提供 GPIO `event-output`；
原生边沿验收为 BLOCKED，需先增加并独立验证 QEMU 观察 hook。
电气网络、开漏争用、PWM 负载和 GPIO bit-banging 均不在此能力内。

### 共享 MuJoCo 世界

`physics.mujoco` 将具名原生世界作为独立 step 节点接入同一 Simulation。
`model` 路径相对场景目录解析，`joints` 将关节名映射到执行器名。
配置校验无需加载原生包；执行需要锁定的 `mujoco` extra。
[双关节示例](../examples/shared-world/README.md)提供 CLI 和 Python 使用命令。

`world.effort` 接收非空的 `{关节名: 转矩Nm}` 映射，整批校验后才写入控制量。
部分更新保留其他关节的控制，reset 重建世界并清零。
启动及每个完成切点，`world.state` 发布 `physics.state`，包含不可变的
`JointState` 记录和原生浮点 `native_time_s`；事件使用 Simulation 整数切点、
`sampled` 时间质量，不制造源时间戳。控制变化从交付切点之后影响积分，
所有关节在每个区间共享一次世界推进。

`control: bound` 将驱动器的 `joint` 连到 `world.drives`，每个 joint 仅允许一个驱动器，
并禁用外部 effort 输入。公共 `JointDriveBinding`/`JointDrivePlan` 使用 joint SI 转矩与
有效截止时间；世界收齐全部计划后按所有 expiry 分段，每段写齐控制并只推进一次原生求解器。
全部目标状态通过校验后才完成反馈。设备 adapter 负责协议、齿轮换算与命令时效，不能推进世界。
`python examples/shared-world/mixed_drivers.py` 演示同一双关节世界中的 M3508 与独立理想转矩驱动器，
分别在3ms、7ms到期；扩展驱动器只需显式注册 Factory，不修改世界的类型分支。
M3508 默认理想减速比为3591/187，可用正数 `gear_ratio` 显式替换，不模拟效率、背隙或转子惯量。

直接转矩、异构关节绑定与完成态 IMU 可使用同一世界；应用迁移另见后文。
缺少原生依赖返回 `blocked`（退出码78），模型、绑定或运行错误
返回 `failed`（退出码1）。示例参数未标定，不声明实时性能或 CPU/世界全局锁步。

### 单电机转子反馈闭环

`device.dji-m3508` 的 `plant` 可显式选择 `fixed-rotor` 或 `rigid-rotor`；
两者使用相同 C620 协议与 CAN Fabric。前者保持配置的位置、速度，后者复用既有刚体转子。
`position_rad`、`velocity_rad_s` 是必填的初始转子状态；两种选择都支持相同的超时、反馈周期和温度配置。
刚体参数固定为惯量 0.02 kg·m²、阻尼 0.08 N·m·s/rad、转矩系数 0.02 N·m/A，均未经过实物标定。

现有 can-motor 的默认关闭变体通过真实 FDCAN 读取运动反馈后，才发送零电流命令：

```bash
cmake -S examples/can-motor -B examples/can-motor/build-rotor-loop -DCMAKE_BUILD_TYPE=Debug -DSIM_ROTOR_LOOP_TEST=ON
cmake --build examples/can-motor/build-rotor-loop
sim validate examples/can-motor/rotor-loop.yaml
sim run examples/can-motor/rotor-loop.yaml
bash tools/run-simulation-smoke.sh --case rotor-loop
# 同一验收器重新构建并验证 Release：
bash tools/run-simulation-smoke.sh --case rotor-loop --build-type Release
```

场景采用 1ms quantum、80ms 时长。guest 先发送 raw4096（理想 5A），实际读取电流4096、
RPM≥2、编码器非零后立即发送一次零电流，再记录 `ROTOR_MOVING`。随后读取零电流且仍运动的反馈，
才记录 `ROTOR_COAST`、`ROTOR_PASS`；75ms 内未完成则失败。原固定转子示例继续验证100ms超时。

验收器分别检查 CAN 来源与交付、对应 native receipt、guest 后续命令和有序 UART，
并按实际命令交付切点，用独立闭式解校验完成的转子位置、速度。receipt 证明传输处理，
UART 时间是观察上界；guest 消费证据还依赖实际读取分支和随后输出。区间末命令只影响下一区间积分。
当前模型采用半隐式 Euler 近似，反馈按完成的 step 合并；电流是命令值，未模拟电气动态或齿轮损耗。
`--case all` 包含此门，结果只在资源清理和输入哈希核对后发布，不代表实物精度或全局 CPU/plant 锁步。

### CAN 故障实验

`bus.can` 可以预声明故障策略，通过同一 Simulation 输入队列定时切换。下面的策略只阻断
`host.can` 到 `main.fdcan1` 的 ID `0x321`，其他接收端和 CAN ID 继续交付：

```yaml
nodes:
  can0:
    kind: bus.can
    fault_profiles:
      delayed:
        delay_ns: 7000000
      receive_loss:
        drop:
          - {source: host.can, destination: main.fdcan1, can_id: 801}
# 其余节点与 CAN links 按普通场景配置。
experiment:
  inputs:
    - {at: 20ms, endpoint: can0.fault, value: receive_loss}
    - {at: 50ms, endpoint: can0.fault, value: normal}
    - {at: 60ms, endpoint: can0.fault, value: delayed}
    - {at: 80ms, endpoint: can0.fault, value: normal}
```

每条总线最多声明 16 个策略，所有策略合计最多 64 条丢弃规则。策略名遵循节点名的 ASCII
identifier 规则，`normal` 是保留的恢复名称。策略可省略 `delay_ns` 或 `drop`，分别使用
总线初始延迟和空规则；每次切换都完整替换声明式策略。健康 Simulation reset 后也恢复初始状态。
Python 中同样使用 `simulation.schedule_input("can0.fault", "receive_loss", at_ns=...)`。

规则只接受 `source`、`destination` 和 `can_id`，至少指定一项；同一规则内取“且”，多条规则取“或”。
端点必须实际连接在该总线上，未知或跨总线端点在静态校验时拒绝。`can_id` 精确比较数值 ID，
不区分相同数值的标准帧和扩展帧，也不改写 flags、DLC 或数据。`fault` 是外部控制端口，不能接内部连线。
原有 Python drop callback 继续逐目标执行一次，与声明式规则取“或”；`normal` 不撤销该 callback。

时间语义沿用总线合同：延迟只影响切换之后入队的帧，已经排队的 due time 不变；后发帧可能先到。
丢弃在 dispatch 时判断，所以切换也会影响尚未交付的旧 pending。相同时刻外部输入按声明/入队顺序执行，
然后 dispatch；若 host 帧要使用新延迟，应先排策略输入再排发送输入。该切点之前 CPU 区间产生、
已经入队的帧仍使用旧延迟，但可受新丢弃策略影响。恢复不补发已丢弃帧，不追溯影响已完成的 CPU。

`can.fault.applied` 记录实际应用的策略与切点；`can.delivery` 保留逐目标 `dropped` 和原始帧、due、
source/mapped/delivery 时间。`input.delivered` 或发送成功只证明输入处理，不能证明固件消费。
当前 `can_seen` 不表达指定目标的一段静默期；故障验收检查逐目标交付记录和实际接收端响应。
容量、callback 或 I/O 失败会冻结当前 Simulation，不重放已有副作用。

无需原生依赖的三端点实例：

```bash
sim validate examples/can-faults/host-only.yaml
sim run examples/can-faults/host-only.yaml --report build/can-fault-host.json
```

真实固件门使用 ARM GCC、CMake 和项目 QEMU，分别验证 Debug/Release 的实际 CAN 序号应答、
心跳超时、恢复及不匹配 ID 的持续通信，不需要 MuJoCo：

```bash
bash tools/run-simulation-smoke.sh --case can-faults --build-type Debug --qemu /path/to/qemu-system-arm
bash tools/run-simulation-smoke.sh --case can-faults --build-type Release --qemu /path/to/qemu-system-arm
```

这是主机通信交付层的确定性故障，未模拟电气仲裁、ACK 错误、错误计数器或 bus-off，也不证明物理校准、
持续实时性能或 CPU/World 全局屏障。故障输入、裁判脚本和断言均通过同一个场景组合。

<a id="experiments"></a>

## 有限实验与观察

`sim.apps.experiment.run_experiment` 使用同一个 Simulation 的输入队列、公开 Event 和纯断言 evaluator。
单板、外设、多机器均可使用 `experiment`，输入与断言各最多 1024 项：

```yaml
experiment:
  ready:
    - {name: west, endpoint: west.uart1, text: "WEST_READY"}
    - {name: east, endpoint: east.uart1, text: "EAST_READY"}
  inputs:
    - {at: 20ms, endpoint: west.key, value: true, origin: session}
    - {at: 0ns, endpoint: referee.enable, value: true, origin: "marker:west"}
    - {at: 50ms, endpoint: east.key, value: true}
  assertions:
    - before: 200ms
      origin: session
      uart_contains: {endpoint: west.uart1, text: "WEST_READY"}
    - before: 100ms
      digital_equals: {endpoint: east.gpio.B0, value: 1}
```

上例是接在已声明 west/east/referee 节点上的实验片段。ready 为最多 64 个具名 UART 标记，名称唯一；
不声明时原点为 session 0。每个 `marker:<name>` 输入从自己的实际匹配交付切点起算，
不等待其它板。全部标记达成的最后切点建立 ready 活动原点，`duration` 从此开始。
输入 `origin` 为 `session`、`ready`（默认）或已声明的 `marker:<name>`；
同刻 session 输入先入队，随后解锁的输入按原声明顺序进入同一队列，没有第二个时钟。

断言 `origin` 只接受 `session` 或 `ready`（默认）。session 包含启动与匹配 ready 的整个切点；
ready 断言排除整个最后匹配切点，只消费之后的观察。`before` 是严格截止时间，不能超过
scene duration；不能借 ready 等待将 session 断言的截止时间延后。
支持 `uart_contains`、`digital_equals`、`can_seen`，endpoint 始终使用完整节点端口名。
UART 跨块匹配只保留必要后缀；仅下界质量不能证明严格截止。发送通知不能替代收到的帧或实际快照。

`at=duration` 仅进行最后交付，没有额外响应窗口。ready 等待与资源创建共用 startup host 期限，
总预算不重新计时。结果在清理后返回，包含断言状态、逐标记 `ready_receipts`、匹配 Event 的
完整时间质量，以及各 Machine 的 native epoch、首末 source/mapped 时间和 snapshot 批次数。
这些字段描述真实捕获，不把 CPU 暂停、传输交付或输入接受合并为固件消费保证。

Python 可传 `ExperimentInput(at_ns, endpoint, payload, origin="ready")` 和
`ready=(ExperimentReady(name, endpoint, text), ...)`；文件与 Python 值共用纯准入。
`on_cut(cut_ns, simulation)` 借用源采样之后、目标交付之前的现有观察点，可向同一队列
排定当前切点输入；不能重入 step/run/reset/close，也不能声称当前交付已完成。
初始采样在 NEW 状态，不能从该回调注入。回调错误终止当前执行并清理。

```bash
python examples/experiment/main.py --qemu build/qemu/qemu-system-arm \
  --firmware examples/minimal-board/build/firmware.elf
```

<a id="experiment-recording"></a>

### 实验记录与离线断言复核

```bash
sim run examples/can-faults/host-only.yaml --trace observations.mcap --report live.json
sim replay observations.mcap --report offline.json
sim replay observations.mcap --assertions assertions.yaml --report revised.json
```

需要 `trace` extra。`sim/experiment/2` 记录原输入声明、命名 ready、断言时间线、时长、公开能力、
每个 Event 原始 source/mapped/delivery/quality、空切点和 snapshot 批次；清理完成后写结束记录。
无需 ELF、原场景、Factory、QEMU 或求解器即可复核。替换文件只有一个 `assertions` 列表，
沿用相同 endpoint、origin、before 和断言语法，不能为未记录能力制造观察。

在线与离线共用消费者，不按源时间重排。离线重新构建 ready receipts、每板 epoch 与 batch 汇总，
并与结束记录核对；两遍完整校验 MCAP CRC/footer/EOF、计数和字节 SHA256 后才返回。
离线只重新评估捕获观察，**不会重跑固件、输入或物理演化**，也不认证文件作者或硬件真实性。

Python 使用借用的 binary stream：`run_experiment(..., trace=output)` 与
`replay_experiment(source, assertions=None)` 返回同一 `ExperimentResult`。
输入需 `read/seek/tell`、从字节 0 开始且调用期间不变；函数不关闭借用流。
CLI 排他创建 trace/report，不覆盖已有文件。失败断言也可得到完整记录；执行、清理、
写入或晚期 flush/close 错误均不发布成功，已发生 I/O 不回滚。

MCAP 固定 1.5.0；typed 配置上限 8 MiB，按 32 KiB 分段，其它单记录上限 64 KiB；
嵌套最多 32 层、整数最多 256bit，时间遵循 uint64 ns。bytes、CAN/IMU/JointState 显式编码，
不使用 pickle 或动态导入。首版只支持单 session 的有限 stepped 实验，不支持 reset/interactive 录制。

<a id="application-presets"></a>

## 四轮与多板应用预设

Robot/Arena/Scenario 的有限运行已经表达为普通 sim/v1 图；不再提供独立 schema1、专用 CLI 或运行时。
`sim.apps.presets` 只构造数据，不取得资源或推进时间：

```python
from pathlib import Path
import yaml
from sim.apps.presets import four_wheel_document, add_can_faults

scene = four_wheel_document(
    {"west": Path("west.elf"), "east": Path("east.elf")},
    Path("models/two_four_wheel.xml"),
    ready={"west": "WEST_READY", "east": "EAST_READY"}, duration="1s",
    assertions=[{"origin": "session", "before": "300ms",
                 "uart_contains": {"endpoint": "west.uart1", "text": "WEST_READY"}}],
)
add_can_faults(scene, "west", delays=((0, 5_000_000), (250_000_000, 0)),
               feedback=((300_000_000, 1, False), (400_000_000, 1, True)))
Path("scene.yaml").write_text(yaml.safe_dump(scene, sort_keys=False))
```

将 ELF 和 ready 文本替换为实际固件值，再运行 `sim validate scene.yaml`、`sim run scene.yaml`。
单板使用一个 firmware 与 `models/four_wheel.xml`。双板按调用者顺序映射 `alpha/`、`bravo/`，
共享一个 MuJoCo Native Solver；各自四个 M3508、CAN 和 IMU link 独立，ID1..4 为 FL/FR/RL/RR。
`add_can_faults` 生成 session 原点的 CAN 策略输入；反馈缺失只阻断指定电机到所属 MCU 的路由，
不能模拟断电，恢复不补发丢弃帧。等时 delay 先于 feedback，输入顺序明确保留。
添加其它驱动器使用 Adapter/Factory 和 joint plan，不修改 Simulation 或 MuJoCo 的厂商分支。

多板原生验收使用统一的场景消费者；shell 负责编译真实 ELF、保留结果与清理，不拥有世界时钟：

```bash
PYTHON=.venv/bin/python bash tools/run-qemu-shared-world-smoke.sh
PYTHON=.venv/bin/python bash tools/run-qemu-shared-world-smoke.sh --cli
PYTHON=.venv/bin/python bash tools/run-qemu-shared-world-smoke.sh --can-deadlines
```

`--peer-exit`、`--reset`、`--upper-exit`、`--interrupt` 分别检查失败冻结和独立清理。
缺依赖的这个可选 shell gate 返回 77 并输出 BLOCKED；不能按退出 0 的通过结果统计。
真实裁判与 DJI consumer 使用后文专用固件门，普通共享世界门不能替代其消费证据。

### 显式可信的上位机程序

需要外部程序时，在同一场景中声明：

```yaml
nodes:
  upper:
    kind: app.host-processes
    commands:
      observer: {command: [./observer, literal-argument]}
```

`sim validate` 只准入命令；运行必须显式 `sim run scene.yaml --allow-host-processes`。
Python 同样需 `builtin_registry(allow_host_processes=True)`。配置可执行本机程序，
此选择只用于可信场景；不可信 ELF/manifest 的隔离属于外部沙箱系统。
argv 使用 `shell=False`，不展开 shell；工作目录是场景目录，程序在准入时解析为绝对路径。
最多 8 个子进程、每个 128 个参数、每参数 4096 UTF-8 bytes、总计 16 KiB（含终止符）。
Simulation 检查子进程和 host deadline；任何提前退出，包括退出码 0，均失败。
stdout/stderr 有界采样尾部各最多 12000 bytes，持续输出可能背压。
清理独立 TERM/KILL/reap 各组并关闭管道，失败不能发布成功。
程序的绝对启动路径保留符号链接，确保虚拟环境的 Python 仍使用其自身依赖。

`bash tools/run-robot-smoke.sh` 验证实际 UART ready、上位机原生 QMP 和清理。
该 busy-MMIO 裸机夹具在指令计数时钟下运行完整 2 秒，功能门使用 120 秒 Host 预算；
迁移时原自由运行路径的 10 秒预算已超时，尚未恢复该性能水平。
此结果与 DJI 固件保持不变的 20 秒验收门分别记录，不代表实时执行能力。

### 裁判 fixture

`device.referee` 使用 `net.uart` 接入机器 UART7；引擎只处理显式脚本，与物理碰撞无关。
状态从 session reset 开始演化，`period` 默认 100ms（准入 1ms..60s），只有 `enable: true`
输入后才发布完成态快照。配合 `origin: marker:<board>`，每板在自己的实际 ready 后启用，
无需等待另一板。四组 payload 以一个 82-byte 批次排队，使用一个序号 Owner；
`referee.batch.admitted` 仅证明字节队列准入，实际固件消费必须另取 UART/HAL/BSP 证据。

节点 `initial` 要求 `game/robot/power_heat/buff` 四组全部字段，`durations` 显式给出
`countdown_s/battle_s`；可选 `events` 使用 `{offset_ns, hit|shot|game|robot|power_heat|buff}`。
事件以 reset 相对时间非递减排列，同刻保持顺序；最多 1024 项。typed 编码与状态引擎位于
`sim.devices.referee`，固定 RoboMaster 2026 V1.1.0 的 0001/0201/0202/0204，使用同一 CRC 封装。
当前 0206 固件结构不匹配而保持未支持，不填充字节掩盖差异；不支持官方各命令独立发送周期、
自动命中裁决、增益叠加、过热扣血或功率惩罚。toy 世界与规则均不作实物标定声明。

引擎热量算法窄移植自固定 [rm_control](https://github.com/rm-controls/rm_control/blob/c5205eb193a8cda6147efe760eafedd0a3ed0104/rm_common/include/rm_common/decision/heat_limit.h)
（BSD-3-Clause），阶段转换参考固定 [XDU referee mock](https://github.com/XDU-IRobot/rm_referee_ros2/blob/45da10a5317d47cc4b5d5968ec553946c43ebe0d/rm_referee_mock/rm_referee_mock/match_control_timing.py)
（Apache-2.0）；来源和许可证随模块保留，配置时长不代表官方赛事常量。

<a id="referee-protocol"></a>

#### 纯裁判编码

```python
from sim.devices.referee import encode_frame, decode_frame, encode_message, GameStatus

wire = encode_message(GameStatus(1, 4, 300, 0), sequence=7)
assert decode_frame(wire).command_id == 0x0001
wire = encode_frame(command_id=0x0201, payload=b"opaque", sequence=0)
```

完整帧最多 128 bytes，payload 最多 119；固定反射 CRC8(init FF)/CRC16(init FFFF,xor0)
来自锁定的 MIT `crccheck 1.3.1`。opaque codec 不判断 command 支持、HP、ID、规则或时钟，
不拥有 UART/sequence。typed 编码拒绝 bool、越界和未知字段；PowerHeat 保留 8 bytes 输出 0。
Buff 按官方 8-byte packed struct 在 offset7 编码 remaining_energy；文档表格 offset6
与结构/长度冲突，未静默借用其它版本。官方版本入口为 [RoboMaster 文档](https://bbs.robomaster.com/wiki/20204847/811363)。

<a id="hal-teaching"></a>

## HAL 教学：按键控制 GPIO

第一课使用独立的 C 工程，通过 ST HAL 读取 DM-MC02 的 A15 按键，并控制 B0 GPIO：
松开为 0，按下为 1。它不依赖 `trobot`、FreeRTOS 或物理仿真后端。
B0 是本课观察的数字输出；板载 A7 是 WS2812 数据线，普通 GPIO 高电平不等同于驱动该 LED。

先按[环境准备](#environment)及[构建](#build)得到项目 QEMU，再进入 `nix develop`。
官方 STM32CubeH7 v1.12.1 所需 HAL/CMSIS 源码随仓库固定，来源和逐文件校验值见
`third_party/stm32h7/SOURCES.json`，构建时不下载 SDK，也不需要 CubeMX。

```bash
uv sync --frozen
cmake -S examples/teaching -B build/teaching/Debug -G Ninja -DCMAKE_BUILD_TYPE=Debug
cmake --build build/teaching/Debug
uv run tools/sim run examples/teaching/gpio-key.yaml --report build/teaching/gpio-key.json
```

Debug ELF 保存在 `build/teaching/Debug/gpio_key.elf`。Release 使用相同命令，
将构建目录中的 `Debug` 和 `CMAKE_BUILD_TYPE` 都改为 `Release`。
修改 `examples/teaching/gpio_key.c` 后再次执行 `cmake --build` 即可。
初始化使用 HSI 64 MHz、LDO、官方启动代码和 HAL SysTick；主循环读取按键后调用 `HAL_Delay(1)`。
本课学习轮询 GPIO，后续课程再增加中断、DMA 和通信外设。

自动实验注入按键，并要求实际 B0 快照在截止时间前出现高电平。
`before` 断言第一次匹配就记为通过，不能单靠两个高低电平断言证明完整的按下/释放顺序。
可用同一个 ELF 交互观察：

```bash
uv run tools/sim-unmigrated lab interactive --elf build/teaching/Debug/gpio_key.elf
```

依次输入 `state`、`key press`、`state`、`key release`、`state`，最后输入 `quit`。
在 JSON 的 `key.level` 中查看松开 1、按下 0；`gpio.B.sampled` 的最低位就是 B0，
按下为 1、释放为 0。`gpio.B.output_mask` 的最低位为 1，表示 B0 已配置为输出。
本课不发送 UART 文本，控制台连接提示也不表示固件初始化完成。

要调试同一份源码，在终端一启动：

```bash
uv run tools/sim-unmigrated lab interactive --elf build/teaching/Debug/gpio_key.elf \
  --gdb-port 1234 --paused --startup-timeout 120 --timeout 300
```

终端二使用原生 GDB：

```bash
arm-none-eabi-gdb build/teaching/Debug/gpio_key.elf
```

```gdb
target remote 127.0.0.1:1234
hbreak lesson_step
continue
list
info registers pc
delete breakpoints
detach
quit
```

断点命中实际固件函数。`detach` 后回到终端一操作按键，输入 `quit` 结束实验。
调试时间计入启动和总期限，超时后需要重新启动实验。

练习：把 `HAL_GPIO_WritePin` 的输出值改为恒定 `GPIO_PIN_RESET`，重新构建并运行同一个
YAML。应得到非零退出码和 `failed` JSON，失败断言指向 `gpio.B0`。
恢复按键决定输出的代码后再次构建，实验应通过。

完整的教学验收入口为：

```bash
bash tools/run-teaching-smoke.sh
```

该门构建 Debug/Release，并用公开快照顺序验证初始状态、按下和释放，另外执行实际交互、
GDB 断点和恒低错误版本的拒绝检查。它验证教学固件与数字模型的边界，不代表实板电气校准。
当前只交付第一课；UART、ADC、CAN 实验将沿用同一工程逐课增加。

<a id="virtual-lab"></a>

## 保留的交互 Lab

有限自动实验使用同一 Simulation：

```bash
uv run tools/sim validate path/to/scene.yaml
uv run tools/sim run path/to/scene.yaml --report build/lab-result.json
# 自动编译真实专用固件，保留直接通道及原生交互/GDB验收：
nix develop -c bash tools/run-lab-channels-smoke.sh
```

[board-io.yaml](../examples/labs/board-io.yaml) 展示 UART、按键/LED、GPIO、CAN 和 ADC1。
`firmware` 相对 YAML 目录；ready 必须是应用真实输出，不以 socket 出现或发送成功代替。
UART 分片保留最小匹配后缀，ready 原点取读后观察的交付切点，可能晚于实际字节产生。

```yaml
schema: sim/v1
runtime: {duration: 250ms, quantum: 1ms}
nodes:
  main: {kind: machine.qemu, firmware: build/student.elf, uarts: [uart1, uart2]}
experiment:
  ready:
    - {name: main, endpoint: main.uart1, text: READY}
  inputs:
    - {at: 100ms, endpoint: main.uart1, value: hello}
  assertions:
    - before: 200ms
      uart_contains: {endpoint: main.uart2, text: received 5 bytes}
    - before: 250ms
      digital_equals: {endpoint: main.led, value: 1}
```

输入端口使用 qualified `main.key`（布尔）、`main.gpio.B0`（0/1）、`main.uart1`（UTF-8或Python bytes）。
UART5 只由 `remote: true` 暴露为 `main.remote` 遥控器 RX 原始字节输入；不提供 TX 或 SBUS/DR16解析。
CAN 刺激使用 `device.can-host` 的 `tx`，其 `can` 与机器 `fdcan1/2/3` 连接同一 `bus.can`；
值为 `{id: 0x321, flags: 0, dlc: 2, data_hex: '4142'}`。它复用总线队列，发送时间取实际交付切点。
`can_seen` 可约束 ID/flags/DLC/完整数据，接收固件证据需依赖实际 guest 应答。

机器 `adc: true` 启用 `main.adc1.ch0` 至 `ch31` 外部输入，值为恰好一个
`{raw: 12345}` 或 `{voltage_uv: 1650000}`，范围分别0..65535、0..3300000。
同切点每通道最多一次，最多16通道；与 `net.imu` 的同切点样本合并为唯一 v2 STEP，
最大payload512字节。ADC不能在绝对Session原点0提交，也不能重开已发送STEP的切点，
不伪造+1ns。ADC-only ACK只证明native输入准入；IMU DONE需另观察实际die消费。
[adc-imu-readback](../examples/adc-imu-readback/scene.yaml) 用真实DR/SPI/sensortime及DONE验证联合输入。
ADC2、ADC网络互连及精确GPIO边沿未支持。

`--timeout` 默认30秒、`--startup-timeout` 默认10秒，均包含资源创建；ready不重启总预算。
报告只在清理结束后创建，拒绝覆盖；状态为passed/failed/blocked，断言失败退出1、缺后端78。
报告保留相对原点、具名ready receipts及匹配事件source/mapped/delivery/quality，不把ACK等同于应用消费。
不提供CPU/plant全局锁步或实板校准保证。

原生自由运行、手动GDB与交互仍使用已有连接owner：

```bash
uv run tools/sim-unmigrated board run --elf build/student.elf --debug --qmp /tmp/student-qmp.sock
```

交互入口复用同一运行环境，默认连接 UART1，按需要增加 CAN 和 ADC：

```bash
uv run tools/sim-unmigrated lab interactive --elf build/student.elf --uart uart1 --can can1 --adc
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



<a id="environment"></a>

## 原生环境

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

因此，**只构建并运行 QEMU 不需要 `uv`**。`uv` 管理的是项目 Python 测试环境和可选 worker 依赖；当前 dev group 安装 pytest 与纯 Python 协议/记录测试依赖，不重复安装 Meson 或 Ninja。需要运行完整回归门禁时，额外安装：

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

<a id="get-the-source"></a>

## 获取固定原生源码

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

<a id="build"></a>

## 构建

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

### 原生基础检查

不需要外部业务固件即可检查 machine 注册。下面的 guest integration 还需要 ARM 工具链：

```bash
build/qemu/qemu-system-arm -machine help
bash tools/run-mc02-smoke.sh
bash tools/run-ospi-smoke.sh
```

smoke 脚本会编译并运行仓库中的最小 guest。修改 QEMU 模型后先重新构建。`tools/run-qemu.sh --smoke` 只探测通用空 machine，不会启动业务固件。

<a id="native-debug"></a>

## 原生固件、调试与观察接口

### 直接运行固件

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

<a id="flash-images"></a>

### Flash 镜像

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

<a id="serial-qmp-and-gdb"></a>

### 串口、QMP 与 GDB

#### GDB

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

#### UART 与 FDCAN 槽位

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

#### QMP

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
用户 IOThread、活动 migration 或后台 dump。Simulation 的 QEMU Backend
使用该接口；直接启动的调用方自行拥有执行顺序。

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

### 只读板级观察与底层通道

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

`QmpSession.command_many(((name, arguments), ...))` 可在同一连接上查询 1–8 个
互相独立的命令，按输入顺序返回结果元组；参数为 `dict` 或 `None`，整组须先通过
JSON 序列化检查，并配置有限的正超时；组等待和取消清理分别使用该超时。
它不是原子快照；错误按输入顺序选择首个异常，
其余异常附入 notes。超时或中断会取消并等待本组请求清理，但已发送的命令仍可能执行。
失败后应停止当前操作并关闭连接，不得重试整组或跨 I/O、CPU、world 边界复用结果。

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

### 原生 CAN 的完成与收发观察

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

需要同时观察执行状态和 CAN 时，可读取只读的 `can-execution-state`：

```json
{"execute":"qom-get","arguments":{"path":"/machine","property":"can-execution-state"}}
```

返回 `schema: 1`，以及 `execution`、`status`、`fdcan-tx-state`、`fdcan-rx-state`。
这四项分别沿用 `x-query-virtual-execution`、`query-status` 和上述两个属性的完整返回值。
附加的 `fdcan-transports` 按控制器名称返回实际绑定的 chardev，例如
`{"dm-mc02.fdcan1":{"path":"/chardevs/can1","connected":true}}`。
`path` 是原生 QOM 路径；未绑定时为 `null`。`connected` 读取该对象原生的同名属性，
没有此属性的后端返回 `null`；后端存在不代表已经连接。调用方应核对路径与预期的连接身份。
查询不停止 CPU、不排空队列、不推进虚拟时间；任一子查询失败则整次读取失败。
各项依次取样，运行中的 CPU 或主机输入仍可改变状态，不能将它作为原子快照或时间屏障。
也可单独读取实际 transport 对应的 `/chardevs/<id>` 的 `connected` 属性进行核对。

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

这三个 CAN 函数在借用对象提供 `command_many` 时，每次观察只查询 `can-execution-state`。
返回后依次校验执行状态、暂停状态、实际绑定的 chardev 路径与调用方身份一致、连接状态
及本次所需的 TX/RX 统计，初始观察校验两个方向。它们要求 QEMU 提供包含
`fdcan-transports` 的上述组合属性；另一个已连接的 socket 不能代替实际 CAN 连接。
组调用前后检查存活和期限；读取、逐帧 sink、发送刷新及最终复核仍各自保留检查。
结果只用于当前观察，不跨这些 I/O 边界复用，也不是原子快照。批量调用失败直接终止，
不会退回逐命令重发。没有该方法或显式为 `None` 的借用对象继续使用原逐命令路径。

<a id="external-simulation-worker"></a>

## 保留的外部 worker 与独立原生后端

这部分保留独特的 legacy v1/v2、MIT/达妙、电机、ROS 2 与 Gazebo 连接能力，
不作为新 sim/v1 应用的第二个运行入口。新的 CAN/四轮 worker 已复用 Simulation，见下节。
配置中的 `--backend` / `--backend-registry` 是可信开发者手动选择的旧扩展入口，
sim/v1 不从不可信 manifest 动态导入路径，也不提供任意主机命令执行。

先运行仓库提供的 co-sim 和 motor fixtures，再连接自己的 plant：

```bash
bash tools/run-qemu-worker-smoke.sh
bash tools/run-qemu-v2-motor-smoke.sh
bash tools/run-worker.sh --help
```

手动启动时，入口的 `--cosim PATH` 将 QEMU 第一个 serial 槽位连接到 co-sim Unix socket，UART 仍由 `--uart` 选择。当前 `trobot` 的具体启动步骤见下文。

<a id="trobot-stationary-startup"></a>

### trobot 静止启动

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

<a id="trobot-release-business-fixture"></a>

### trobot Release 业务夹具

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

### Legacy worker 的后端边界

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
不建立 QEMU 时间屏障。固件 IMU 闭环和物理标定仍需分别验证。
`validate_step(dt_ns)` 和 `validate_joint_effort(name, Nm)` 只检查准入，不推进模型或
改写力矩。`validate_steps((dt1_ns, dt2_ns, ...))` 按顺序检查未来 native 时间，提前
拒绝浮点时间分辨率不足的后续区间。`close()` 释放此实例持有的原生 model/data；重复关闭无害，关闭后操作拒绝。

### CAN worker 会话

`tools/run-can-worker.py` and `tools/run-chassis-worker.py` now use the same
`Simulation`/Topology factories as `sim run`. The rotor worker requires the
default stationary IMU fixture; combining it with an independent legacy engine
is rejected before connection. The chassis worker preflights the complete
self-contained MJCF profile and closes that solver before opening transports.
Includes/assets are rejected by this fixed worker profile; ordinary MuJoCo
scenes retain their existing model support. Model changes after preflight fail.

Each matching co-sim RESET_ACK closes the previous graph and creates a new
Simulation with the acknowledged session ID and origin. CAN queues, devices and
the native World are new resources. Borrowed transport sockets, incomplete RX
records and queued TX suffixes survive unchanged. During RESET/ACK/DONE waits,
CAN ingress only queues; it does not advance the World. STEP retries reuse the
exact frame without another solver advance or IMU sample. A transport submission
count belongs to its endpoint/session and does not prove firmware consumption.

Known CAN deadlines can split a worker interval into several Simulation cuts.
The worker wire STEP ID identifies the requested completed target; it is not an
internal solver-step count. Native World seconds start at zero independently of
the session's integer epoch. Completed World getters expose SI IMU, joint and
body state without advancing physics.

`--frames 0` remains continuous operation. Graph startup borrows the finite
`--connect-timeout` budget; each target advance and its IMU observation borrow a
2-second host budget. ACK/DONE waits keep their existing separate deadlines.
Any execution/queue/I/O failure terminates the worker with independent cleanup;
failed effects are not retried or rolled back. `PYTHON=/path/to/python` selects
the interpreter for the chassis firmware runner as well as the DJI runner.

The native RESET/suffix regression is included in:

```sh
QEMU_SYSTEM_ARM=/path/to/pinned/qemu-system-arm \
  PATH=/path/to/native-env/bin:$PATH bash tools/run-qemu-worker-smoke.sh --chassis
```

该门补充真实 SPI readback 与 DJI/Chassis 固件消费者测试；它们不能证明全局 CPU/World 屏障、
物理标定或持续实时性能。

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

Gazebo 尚无 sim/v1 World Factory；独立 API 的调用者拥有其唯一 step/reset/close，
不能与其它拥有相同 solver 的执行者并用。原生 sensor 交付与 CPU 同步仍是独立边界。

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

这个转子是未标定的验证夹具。Gazebo 的 sim/v1 多设备组合尚未迁入；
ROS consumer 仅覆盖上述固定场景；
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

多 QEMU 的有限运行使用前述 sim/v1 图。底层 `sim.transports.cosim` 与唯一 STEP codec
继续承载旧 worker 协议，不给应用建立第二个 scheduler。RESET/ACK/DONE 和原始流尾部
各自保留其语义；不能从握手元数据推断 MCU、传感器和物理世界的全局锁步。

<a id="testing-and-performance"></a>

## 验证与性能

本轮或某个历史门通过，不代表所有原生 profile 通过。报告应分别记录命令、退出码、
环境、ELF SHA、实际 QEMU 路径/版本或能力、诊断与清理状态。缺依赖是 BLOCKED，
pytest 的 optional skip 不是该层 PASS；没有 Actions 运行记录时不能声称 CI 已通过。

| 层级 | 验证入口 | 证据与限制 |
| --- | --- | --- |
| Host core | `.github/workflows/host-core.yml` | 安装基础包、纯 Python 合同、checkout 外 CLI；不运行 MCU |
| 协议与记录 | 同 workflow 的纯 Python extras 阶段 | 实际字节/状态、MCAP 生命周期与离线复核 |
| 原生 Board | `bash tools/run-simulation-smoke.sh --case all` | ARM ELF + 固定 QEMU；缺依赖退出 78 |
| 多板 | 同脚本 `--case dual-board` | 独立机器与时间映射，非全局硬件锁步 |
| 物理 | `python -m pytest -q tests/test_simulation_joints.py tests/test_four_wheel_physics.py` | 需要 MuJoCo；单 solver、异构驱动器和数值状态 |
| 固件直连 | 下文 HAL/BSP、Motor、DJI、Chassis、Referee runner | 消费行为与清理；不代表实板标定 |

轻量 PR gate 不构建 QEMU、不安装 MuJoCo/ROS/Gazebo，也不依赖未追踪的本地示例。
原生门应在具备 Nix/ARM GCC/兼容二进制的 Linux 环境中单独执行，并保留构建缓存与产物身份。

日常修改优先运行受影响的原生测试，例如内部 Flash：

```bash
tools/meson test -C build/qemu --print-errorlogs qtest-arm/dm-mc02-memory-test
```

该命令复用已有 qtest，不需要业务固件。修改模型后先构建 QEMU；完整回归用于发布和涉及多个入口的变更。

<a id="trobot-halbsp-firmware-test"></a>

### trobot HAL/BSP 固件测试

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

<a id="trobot-motormujoco-test"></a>

### trobot Motor/MuJoCo 测试

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

### 完整回归门

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

### 固件实时系数

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

### Referee 固件消费者

在外部 trobot 工作区构建默认关闭的专用固件；普通业务 Debug/Release 不启用这些开关：

```bash
cd ../trobot
cmake --preset RefereeTestDebug
cmake --build --preset RefereeTestDebug
cd ../dm-mc02-qemu
tools/run-trobot-referee-test.sh ../trobot/build/RefereeTestDebug/trobot.elf \
  build/test-results/referee-debug ../trobot
```

固定两批共四种消息，第二批先发 19 bytes，固件保持旧状态观察 100ms 后才补发原 63-byte 后缀；
实际 UART7/HAL DMA/BSP/RTOS 消费检查四组 payload 与非零新时间戳。
结果目录须不存在，输入哈希与进程清理后才发布结果；物理标定保持 SKIP。
双板与脚本消费者分别构建 `ArenaRefereeTestDebug/Release`、`ScenarioRefereeTestDebug/Release`，
同 runner 使用 `--arena` 或 `--scenario`；后者可加 `--cli` 检查真正的 `sim run` 路径。
这些是 fixture profile 名称，内部均组合普通 sim/v1 场景；没有独立 Arena/Scenario runtime。
它们仍使用固定 1s 活动时长、100ms 发布和原 host 期限，不通过扩大期限掩盖失败。

<a id="trobot-dji-driver-test"></a>

### trobot DJI 驱动测试

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

该固定场景曾有 Release 复测在原 20 秒 host 期限内只完成 1090 步，最终暂停
watchdog 观察缺失，历史失败保留。逐切点交换收敛后，Debug/Release 在原期限下
各复测通过一次；单次通过不证明期限稳定性，也不构成实时性能证据。

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

夹具生成固定双板 sim/v1 场景，通过同一 Simulation 运行。两个 MCU
使用相同 ELF，真实驱动 ID1/2 输出 `64/-64`；另外两个命令槽保持零。east 总线在
RESET 相对 0ms 设置 5ms 入队延迟，在 250ms 恢复零延迟，west 保持零延迟。
已排队帧保留原截止时间；报告分别记录来源、交付时间、原始 CAN 字节和固件复制的
反馈字段及 BSP 时间戳。相同反馈字节只能证明来源匹配，不能识别唯一一次固件回调。
两边第一次完整反馈报告必须早于 250ms，否则测试失败。
命令观察逐条校验并计数；`transition_count` 保存实际切换总数，`transitions` 仅保留
最初 8 次切换，`last` 始终记录最后一条命令。该诊断摘要不提供完整命令轨迹。

恢复延迟后，既有 UART1 握手释放第二次应用观察；握手不改变事件时间或推进世界。
结果目录必须不存在。缺失样本、FAIL、发送或清理失败、输入变化均保留失败报告。
该门不证明动态延迟下的 50ms 超时、物理 CAN 时序、CPU/plant 全局锁步或实时性能；
物理校准保持显式 SKIP。旧 ID1/ID5 独立掉线测试沿用上述入口。

反馈丢失的固件消费者使用独立、默认关闭的 `DjiFeedbackTestDebug/Release`，
与 `DjiScenarioTest` 互斥：

```bash
cd ../trobot
cmake --preset DjiFeedbackTestRelease
cmake --build --preset DjiFeedbackTestRelease --clean-first
cd ../dm-mc02-qemu
.venv/bin/python tools/dm_mc02_dji_test.py --feedback-scenario \
  ../trobot/build/DjiFeedbackTestRelease/trobot.elf \
  build/test-results/dji-feedback-release ../trobot \
  --qemu build/qemu/qemu-system-arm
```

新结果目录必须不存在。固定场景在 RESET 后 200 ms 屏蔽两成员各自 ID1 的反馈，
350 ms 恢复，实际 ready 后运行 400 ms；启动期限 10 秒、host 总期限 20 秒。
固件持续请求 ID1/2 电流 `64/-64`，检查反馈时间戳冻结、年龄超过配置的 50 ms、
应用非零命令仍新鲜时驱动任务清零输出，以及恢复后新反馈和非零输出。
ID2 是各成员的持续在线对照。主机还检查真实 CAN 命令、器件来源、丢弃范围、
物理发送完成及最终 watchdog，清理和输入哈希核验完成后才发布通过。

这是反馈不可达测试；50 ms 是该驱动的应用配置，不能据此声明电机断电、精确中断
时刻、全局锁步或实时性能。物理校准保留 SKIP；本门不替代上面的固定 Release 场景门。

<a id="update-and-source-package"></a>

## 更新与源码包

### 更新 checkout

```bash
git pull --ff-only
git submodule update --init --depth 1 qemu/upstream
```

子模块通常处于 detached HEAD，这是固定版本的正常状态。更新后重新运行构建脚本；切换 Nix 与 Ubuntu 依赖环境时，脚本会自动刷新 QEMU configure 元数据。

### 创建与恢复源码包

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

<a id="troubleshooting"></a>

## 故障排查

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

## 能力边界与目录

- 当前真实 MCU 是 STM32H723 / DM-MC02。SPI/I2C/ADC 网络连接、第二块真实 MCU、Windows 原生全功能均未交付。
- `net.digital` 是完成态采样，QEMU GPIO `event-output` 原生门仍 BLOCKED；没有逐位 UART、电气争用或 PWM 负载求解。
- UART5 是遥控器 RX 原始输入，不提供 SBUS-A/DR16 编码、反相电平真实性或 TX。
- MuJoCo/四轮 toy 参数未标定；CAN 故障为主机交付策略，不模拟真实仲裁、bus-off 或电气错误。
- 固定 shift0 是虚拟指令计时，不是 H723 周期模型；CPU 切点、FIFO 到达和固件应用消费分别验证。
- 独立 Gazebo/ROS/MIT legacy worker 与原生自由运行 Lab 暂保留其独特能力，不给新场景复制运行时。
- 不提供完整硅级模型、整机 migration、硬实时保证、分布式仿真、OJ 服务或任意不可信 manifest 的主机沙箱。

| 路径 | 职责 |
| --- | --- |
| `sim/api`, `sim/runtime` | 纯公共合同、唯一生命周期/时间/调度 |
| `sim/config`, `sim/fabric` | 无资源场景准入、CAN/UART/数字/IMU 连接 Owner |
| `sim/machines/qemu` | 进程/QMP Backend、DM-MC02 Profile 与外设 adapter |
| `sim/devices`, `sim/physics` | 具体协议设备与单一 World / joint driver |
| `sim/apps`, `sim/trace` | 可选预设、可信进程与实验消费者；记录/离线复核 |
| `sim/lab` | 纯断言复用与保留的原生自由运行交互 |
| `examples`, `smoke`, `tests` | 自包含场景、真实 ELF 门、Python 合同 |
| `qemu/upstream`, `qemu.lock` | 固定 fork 与独立的原生器件/板级测试 |
| `models`, `third_party` | 固定物理/教学来源、许可证与来源声明 |
