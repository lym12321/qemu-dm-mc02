# DM-MC02 QEMU 使用手册

项目以真实 MCU ELF 为输入，用一个 Rust `Simulation` 组合机器、通信网络、虚拟设备和物理世界。
当前唯一真实 Machine Profile 是 DM-MC02 / STM32H723；改变 `model` 名称不能增加 MCU 支持。
产品运行不需要 Python。QEMU、MuJoCo、Gazebo 和 ROS 2 保留原生实现；Python 仅用于构建和独立测试驱动。
Windows 产品提供固件终端、交互/Web Lab 与原生 ARM GDB 调试；场景、worker 和 SocketCAN 使用 Linux。

下载包命令从解压目录执行；源码命令默认从仓库根目录执行。按需要阅读：

- [安装](#installation)、[原生构建](#build)与[直接运行 ELF](#native-debug)
- [sim/v1 场景与 Rust API](#simulation)、[互连与精度](#connections)
- [有限实验与离线复核](#experiments)、[应用预设](#application-presets)
- [HAL 教学](#hal-teaching)、[交互 Lab](#virtual-lab)与[Web Lab](#web-lab)
- [原生接口](#native-debug)、[外部 worker](#external-simulation-worker)
- [验证与性能](#testing-and-performance)、[故障排查](#troubleshooting)

<a id="installation"></a>

## 安装

### 下载原生包

登录 GitHub，打开 [Native packages](https://github.com/lym12321/qemu-dm-mc02/actions/workflows/native-packages.yml)，
在所选运行记录底部的 **Artifacts** 下载。包保留 30 天。

| Artifact | 平台与范围 |
| --- | --- |
| `dm-mc02-windows-x86_64` | Windows x86_64 ZIP，固件终端、交互/Web Lab 与原生 ARM GDB 调试 |
| `dm-mc02-linux-firmware-x86_64` | Ubuntu 24.04+ x86_64 tar.gz，轻量固件入口 |
| `dm-mc02-linux-x86_64` | Ubuntu 24.04+ x86_64 tar.gz，完整场景、Lab/Web、worker、MuJoCo/Gazebo/ROS 2 |

Linux Artifact 外层解开后，使用 `tar -xzf` 解开内层 tar.gz；它保留可执行权限和插件符号链接。
Windows ZIP 直接解压。运行已有 ELF 不需要 ARM 编译器，源码断点调试另装 ARM GDB。

Windows PowerShell：

```powershell
.\sim.exe firmware .\example.elf
.\sim.exe firmware .\example.elf --debug
.\sim.exe lab interactive --elf .\example.elf
.\sim.exe lab web --elf .\example.elf
```

Linux，在解压目录执行：

```bash
./sim firmware example.elf
./sim firmware example.elf --debug
```

将 `example.elf` 替换为自己的 DM-MC02 ELF。`--debug` 从复位暂停并监听 `127.0.0.1:1234`。
CLI 直接使用相邻 `bin/` 中的 QEMU，不解包临时可执行文件。
轻量 Linux 包需要系统 `libglib2.0-0t64 zlib1g`。
完整 Linux 包带 `lib/` 原生依赖、`plugins/` 相对符号链接、Gazebo/SDF/ament 资源、官方 ROS bridge、
`runtime-env.json`、`native-runtime.json`、来源哈希、对应源码和许可证；宿主提供 glibc≥2.39、
Linux pidfd 及 EGL/GLVND/Mesa 或显卡厂商驱动。运行不需要安装 Python 或物理 SDK。
完整 Linux 运行目录约 351.8 MiB，其中 Rust 入口为 8.64 MiB；对应源码另占约 141 MiB。

Ubuntu 24.04 的无桌面环境可安装以下宿主图形库，使用 Mesa 软件渲染：

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  libegl1 libgl1 libopengl0 libglx-mesa0 libgl1-mesa-dri
```

本地完整包已在干净 Ubuntu 24.04.3 容器、checkout 外和空应用 PATH 下，通过 367 个 ELF 的
依赖闭包、实际 MuJoCo 转子、Ogre2 相机/LiDAR、ROS context、固件 UART/monitor/reset-paused
GDB 监听，以及场景/完整 MCAP 重放验收；原生探针未访问外部 SDK 或 Python。
本次发布构建见 [Actions 42610464978](https://github.com/lym12321/qemu-dm-mc02/actions/runs/42610464978)。
平台可用性以运行和随包报告为准；Windows Lab 包的 `lab-smoke.json` 记录真实交互/Web 通道、
外部 QMP、GDB 断点/继续/单步/复位/寄存器与内存读写及退出清理。Windows 原生门运行于 Windows Server 2022。
实板校准仍未完成。

### 从源码构建

工具链由 `rust-toolchain.toml` 固定，Rust 依赖由 Cargo.lock 锁定：

```bash
git clone --branch refactor/composable-platform https://github.com/lym12321/qemu-dm-mc02.git
cd qemu-dm-mc02
git submodule update --init --depth 1 qemu/upstream
cargo build --release --locked -p sim-host --features host-runtime
export PATH="$PWD/target/release:$PATH"
sim --help
sim validate examples/can-faults/host-only.yaml
sim run examples/can-faults/host-only.yaml --report build/host-only.json
```

该三端点 CAN 实验无固件或求解器，验证延迟、定向丢帧及恢复。真实固件另按
[Ubuntu](#ubuntu)或可选 [Nix](#nix) 环境构建 QEMU 与 ARM ELF。
源码构建后，`firmware` 与 Lab 需显式 `--qemu` 或设置
`export QEMU_SYSTEM_ARM="$PWD/build/qemu/qemu-system-arm"`；打包产品自动使用相邻 `bin/`。
Cargo 的产品选择如下：

| features | 用途 |
| --- | --- |
| 默认 | 轻量 `sim firmware`，支持 Linux/Windows |
| `windows-lab` | 原生 Windows 固件终端、完整交互/Web Lab 和调试 |
| `scene` | YAML 准入、Host 场景与唯一 Simulation |
| `host-runtime` | 场景、trace、machine、Lab/Web、worker、设备、裁判、SocketCAN、显式上位机进程 |
| `mujoco` | MuJoCo 3.12.0，使用成熟的 mujoco-rs 绑定 |
| `gazebo` / `gazebo-rendering` | 原生 Gazebo CXX 接口 / Ogre2 相机与 LiDAR |
| `ros2` | 原生 ROS 2 CXX transport |
| `linux-runtime` | `host-runtime` 加全部上述原生物理/ROS 功能 |

Windows 原生 Lab（MSVC Rust 工具链，原生 QEMU 构建见 Windows CI）：

```powershell
cargo build --release --locked -p sim-host --features windows-lab
.\target\release\sim.exe lab web --elf .\app.elf --qemu .\build\qemu\qemu-system-arm.exe
```

仅固件入口：

```bash
cargo build --release --locked -p sim-host
target/release/sim firmware path/to/app.elf --qemu build/qemu/qemu-system-arm
```

完整 Linux 构建需先安装 `.github/workflows/native-packages.yml` 列出的 Ubuntu/ROS Jazzy SDK 依赖：

```bash
bash tools/build-linux-sdk.sh
bash tools/build-linux-runtime.sh
```

SDK 脚本固定 Gazebo Sim 8.15.0、禁用其 Python 绑定，并固定 MuJoCo 3.12.0。
`DM_GAZEBO_PREFIX`、`DM_ROS_PREFIX`、`DM_MUJOCO_PREFIX` 可选择原生 SDK 位置；
构建 helper 设置 Cargo/C++ 所需路径，产物仍是 Rust 可执行文件。
Python 开发依赖只安装到[独立测试环境](#test-environment)，不再使用项目 pip 安装或运行时 extras。

### 原生后端与 QMP

可使用下载包，或[初始化固定子模块并构建](#get-the-source)项目 QEMU。
QEMU 选择顺序为 `--qemu`、`QEMU_SYSTEM_ARM`、发行目录 `bin/`；源码场景还能通过
`--backend-root` 定位 checkout 的 `build/qemu/qemu-system-arm`。普通系统 QEMU 没有本项目板卡。

`sim run scene.yaml` 的原生后端通过 Rust QAPI client 使用固定 QMP schema，要求
`x-run-to-virtual-time`、`x-query-virtual-execution` 与 CAN execution QOM 能力；缺失会报错。
`sim validate` 不启动进程或求解器，也不证明 ELF 可运行。生产运行不需要安装 Python QMP client。
保留的 Python QMP client 仅供独立原生测试，从固定的 `qemu/upstream/python` 安装，
不接受任意同名 PyPI client，也不从测试结果推导新的原生能力。

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
机器描述只检查固件路径形式；文件存在且可读的检查在执行准入时完成，早于端口附着和原生进程启动。

Rust API 与 CLI 共用 `config::scene::load_scene` 和 `Scene::simulation`：

```rust
use sim_core::SimulationOptions;
use sim_host::config::scene::load_scene;

fn main() -> sim_core::Result<()> {
    let scene = load_scene("examples/minimal-board/scene.yaml")
        .map_err(|error| std::sync::Arc::new(error) as sim_core::Failure)?;
    let mut simulation = scene.simulation(SimulationOptions::default())?;
    let execution: sim_core::Result<()> = (|| {
        simulation.start(None)?;
        simulation.run_until(scene.duration_ns, None)?;
        Ok(())
    })();
    let cleanup = simulation.close();
    for note in simulation.failure_notes() {
        eprintln!("{note}");
    }
    execution.and(cleanup)
}
```

无原生依赖的完整 API 示例可直接编译运行：

```bash
cargo run --locked -p sim-host --features simulation --example host_can
```

通用调度合同由 `sim-core` 拥有，具体配置和 adapter 由 `sim-host` 提供。
配置不动态导入代码；Factory 的纯描述先于资源创建，设备借用连接 owner 的协议句柄。
QEMU Backend 独占进程、QMP、socket 和 CPU request/finish，板级 Profile 不推进 CPU。
Fake Profile 只证明接口合同，不代表第二颗 MCU。

Machine 完成采样及同步观察回调后，Backend 重新核验本次目标、原生时间、执行状态与暂停状态，
通过后才提交 Machine 完成时间。回调期间原生目标发生变化会使当前 Simulation 失败，已产生的输出保留。
非 CAN 执行观察复用批量 QMP 查询；批量不等于原子快照，CPU 运行控制仍由 Backend 独占。
Machine 的内部 QMP 连接在协商前关闭异步事件保留，避免每个 Cut 的 RESUME/STOP 持续积压；
Reset 创建的新连接使用同一策略。完成态仍由原生查询确认。需要检查 RESET/watchdog 事件的
独立工具连接继续接收事件；此策略只限制 Machine 内部连接的事件队列和历史。

### 时间、状态与失败合同

一次非零区间从完成切点 `t` 开始：

1. `settle(t)` 按入队顺序应用同刻输入，按 Owner 声明顺序派发到期工作；新到期工作继续受有限预算约束。
   稳定后对 `input_commit` 组件提交当前输入批次。
2. 下一切点为目标、`t + quantum`、已排定输入和 Owner 已知截止时间的最小值。
   settle 后仍不晚于 `t` 的 deadline 是错误，不能回退或零步自旋。
3. 先预检全部 Machine 的目标，再请求全部 Machine，完成全部 CPU，随后推进非 Machine 组件。
4. 采样完成的源状态，交付目标切点输入与网络事件，提交输入批次，最后提交 Simulation 时间和 step ID。

提交钩子只能产生未来输入或网络工作。每个钩子返回后，若任何 Owner 留有不晚于当前切点的工作，
立即冻结，后续钩子与逻辑时间均不提交；已经发生的 I/O 保留，不重新 settle 或重试提交。

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

嵌入已有会话可通过 `SimulationOptions::initial_session` 提供 `Session`。借用有限绝对 host deadline 时，
`start` 和后续每次 `step/run_until/reset` 都须显式传入同一期限。
同步原生第三方调用没有抢占保证，组件必须遵守借用期限。

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
cargo run --release --locked -p sim-host --features simulation --example edge_events
cargo test --locked -p sim-host --features simulation --test edge
```

示例的 Rust 事件源在同一1ms区间产生四次翻转（含相邻及同时间事件），全部在完成切点有序交付。
当前 QEMU 仅公开完成态 GPIO 快照，没有可信原生边沿通道，因此不提供 GPIO `event-output`；
原生边沿验收为 BLOCKED，需先增加并独立验证 QEMU 观察 hook。
电气网络、开漏争用、PWM 负载和 GPIO bit-banging 均不在此能力内。

### 共享 MuJoCo 世界

`physics.mujoco` 将具名原生世界作为独立 step 节点接入同一 Simulation。
`model` 路径相对场景目录解析，`joints` 将关节名映射到执行器名。
配置校验无需加载原生包；执行需要编译 `mujoco` feature 并提供 MuJoCo 3.12.0 原生库。
[双关节示例](../examples/shared-world/README.md)提供 CLI 和 Rust example 命令。

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
`cargo run --release --locked -p sim-host --features simulation,mujoco,devices --example mixed_drivers` 演示同一双关节世界中的 M3508 与独立理想转矩驱动器，
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
Rust API 使用 `HostInput::FaultProfile`，通过同一 `schedule_input` 队列调度。

规则只接受 `source`、`destination` 和 `can_id`，至少指定一项；同一规则内取“且”，多条规则取“或”。
端点必须实际连接在该总线上，未知或跨总线端点在静态校验时拒绝。`can_id` 精确比较数值 ID，
不区分相同数值的标准帧和扩展帧，也不改写 flags、DLC 或数据。`fault` 是外部控制端口，不能接内部连线。
显式 CAN drop callback 继续逐目标执行一次，与声明式规则取“或”；`normal` 不撤销该 callback。

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

`sim_host::apps::experiment::run_experiment` 使用同一个 Simulation 的输入队列、公开 Event 和纯断言 evaluator。
公共解析和求值由 `sim_host::config` / `sim_host::assertions` 拥有；调用者提供端口准入，在线运行与离线复核使用相同匹配规则。
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

Rust 的 `ExperimentInput`、`ReadyMarker` 与 `Origin` 位于 `sim_host::config::scene`；
CLI 与 Rust API 共用纯准入和实验消费者。运行现有编译示例：

```bash
cargo run --release --locked -p sim-host --features scene,machine --example experiment -- \
  --qemu build/qemu/qemu-system-arm --firmware examples/minimal-board/build/firmware.elf
```

<a id="experiment-recording"></a>

### 实验记录与离线断言复核

```bash
sim run examples/can-faults/host-only.yaml --trace observations.mcap --report live.json
sim replay observations.mcap --report offline.json
sim replay observations.mcap --assertions assertions.yaml --report revised.json
```

需要构建 `trace` feature（`host-runtime` 已包含）。`sim/experiment/2` 记录原输入声明、命名 ready、断言时间线、时长、公开能力、
每个 Event 原始 source/mapped/delivery/quality、空切点和 snapshot 批次；清理完成后写结束记录。
无需 ELF、原场景、Factory、QEMU 或求解器即可复核。替换文件只有一个 `assertions` 列表，
沿用相同 endpoint、origin、before 和断言语法，不能为未记录能力制造观察。

在线与离线共用消费者，不按源时间重排。离线重新构建 ready receipts、每板 epoch 与 batch 汇总，
并与结束记录核对；单遍读取完整校验 MCAP CRC/footer/EOF 和计数后才返回结果。
离线只重新评估捕获观察，**不会重跑固件、输入或物理演化**，也不认证文件作者或硬件真实性。

Rust 使用 `sim_host::apps::recording::{run_recorded, replay_experiment}`，
通过标准 `Read` / `Write` 接口记录或复核。复核从 MCAP 文件头读到 EOF，无需 seek；
CLI 排他创建 trace/report，不覆盖已有文件。失败断言也可得到完整记录；执行、清理、
写入或晚期 flush/close 错误均不发布成功，已发生 I/O 不回滚。

Rust MCAP reader/writer 使用锁定的 `mcap 0.26.0`，独立测试消费者使用 Python `mcap 1.5.0`；typed 配置上限 8 MiB，按 32 KiB 分段，其它单记录上限 64 KiB；
嵌套最多 32 层、整数最多 256bit，时间遵循 uint64 ns。bytes、CAN/IMU/JointState 显式编码，
不使用对象反序列化或动态导入。首版只支持单 session 的有限 stepped 实验，不支持 reset/interactive 录制。

<a id="application-presets"></a>

## 四轮与多板应用预设

Robot/Arena/Scenario 的有限运行已经表达为普通 sim/v1 图；不再提供独立 schema1、专用 CLI 或运行时。
`sim_host::apps::presets` 只构造数据，不取得资源或推进时间：

```rust
use sim_host::apps::presets::{four_wheel_document, add_can_faults, CanFault, FourWheelOptions};
use std::{collections::BTreeMap, path::PathBuf};

let mut document = four_wheel_document(FourWheelOptions {
    firmwares: vec![("west".into(), PathBuf::from("west.elf")),
                    ("east".into(), PathBuf::from("east.elf"))],
    model: PathBuf::from("models/two_four_wheel.xml"),
    ready: BTreeMap::from([("west".into(), "WEST_READY".into()),
                          ("east".into(), "EAST_READY".into())]),
    referees: BTreeMap::new(), duration_ns: 1_000_000_000, quantum_ns: 1_000_000,
    imus: true, inputs: vec![], assertions: vec![],
})?;
add_can_faults(&mut document, "west", vec![
    (0, CanFault::Delay(5_000_000)), (250_000_000, CanFault::Delay(0)),
    (300_000_000, CanFault::Feedback { motor_id: 1, enabled: false }),
    (400_000_000, CanFault::Feedback { motor_id: 1, enabled: true }),
])?;
// JSON 也是合法 YAML，直接写入普通 sim/v1 配置。
std::fs::write("scene.yaml", serde_json::to_vec_pretty(&document)?)?;
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

`--multi-cut` 验证公开 400µs 切点；`--peer-exit`、`--reset`、`--upper-exit`、`--interrupt`
原私有 Python 注入入口已移除，其失败冻结/清理由 Rust machine/process/physics 门验证。
缺依赖须按脚本的非零 BLOCKED/SKIP 状态记录，不能统计为 PASS。
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
Rust API 也必须显式设置该节点的 `allow_launch`。配置可执行本机程序，
此选择只用于可信场景；不可信 ELF/manifest 的隔离属于外部沙箱系统。
argv 作为字面参数直接交给原生进程，不展开 shell；工作目录是场景目录，程序在准入时解析为绝对路径。
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
`sim_host::devices::referee`，固定 RoboMaster 2026 V1.1.0 的 0001/0201/0202/0204，使用同一 CRC 封装。
当前 0206 固件结构不匹配而保持未支持，不填充字节掩盖差异；不支持官方各命令独立发送周期、
自动命中裁决、增益叠加、过热扣血或功率惩罚。toy 世界与规则均不作实物标定声明。

引擎热量算法窄移植自固定 [rm_control](https://github.com/rm-controls/rm_control/blob/c5205eb193a8cda6147efe760eafedd0a3ed0104/rm_common/include/rm_common/decision/heat_limit.h)
（BSD-3-Clause），阶段转换参考固定 [XDU referee mock](https://github.com/XDU-IRobot/rm_referee_ros2/blob/45da10a5317d47cc4b5d5968ec553946c43ebe0d/rm_referee_mock/rm_referee_mock/match_control_timing.py)
（Apache-2.0）；来源和许可证随模块保留，配置时长不代表官方赛事常量。

<a id="referee-protocol"></a>

#### 纯裁判编码

```rust
use sim_host::devices::referee::{encode_frame, decode_frame, encode_message, GameStatus};

let message = GameStatus {
    game_type: 1, game_progress: 4, stage_remain_time: 300, sync_timestamp: 0,
};
let wire = encode_message(message.into(), 7)?;
assert_eq!(decode_frame(&wire)?.command_id, 0x0001);
let opaque = encode_frame(0x0201, b"opaque", 0)?;
```

完整帧最多 128 bytes，payload 最多 119；固定反射 CRC8(init FF)/CRC16(init FFFF,xor0)
由锁定的 Rust `crc 3.4.0` 实现。opaque codec 不判断 command 支持、HP、ID、规则或时钟，
不拥有 UART/sequence。typed Rust 字段与配置准入拒绝类型错误、越界和未知字段；PowerHeat 保留 8 bytes 输出 0。
Buff 按官方 8-byte packed struct 在 offset7 编码 remaining_energy；文档表格 offset6
与结构/长度冲突，未静默借用其它版本。官方版本入口为 [RoboMaster 文档](https://bbs.robomaster.com/wiki/20204847/811363)。

<a id="hal-teaching"></a>

## HAL 教学：按键控制 GPIO

第一课使用独立的 C 工程，通过 ST HAL 读取 DM-MC02 的 A15 按键，并控制 B0 GPIO：
松开为 0，按下为 1。它不依赖 `trobot`、FreeRTOS 或物理仿真后端。
B0 是本课观察的数字输出；板载 A7 是 WS2812 数据线，普通 GPIO 高电平不等同于驱动该 LED。

先按 [Ubuntu 环境准备](#ubuntu)及[构建](#build)得到项目 QEMU，安装 ARM GCC、CMake 和 Ninja。
官方 STM32CubeH7 v1.12.1 所需 HAL/CMSIS 源码随仓库固定，来源和逐文件校验值见
`third_party/stm32h7/SOURCES.json`，构建时不下载 SDK，也不需要 CubeMX。

```bash
cmake -S examples/teaching -B build/teaching/Debug -G Ninja -DCMAKE_BUILD_TYPE=Debug
cmake --build build/teaching/Debug
sim run examples/teaching/gpio-key.yaml --report build/teaching/gpio-key.json
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
sim lab interactive --elf build/teaching/Debug/gpio_key.elf
```

依次输入 `state`、`key press`、`state`、`key release`、`state`，最后输入 `quit`。
在 JSON 的 `key.level` 中查看松开 1、按下 0；`gpio.B.sampled` 的最低位就是 B0，
按下为 1、释放为 0。`gpio.B.output_mask` 的最低位为 1，表示 B0 已配置为输出。
本课不发送 UART 文本，控制台连接提示也不表示固件初始化完成。

要调试同一份源码，在终端一启动：

```bash
sim lab interactive --elf build/teaching/Debug/gpio_key.elf \
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

## 交互与 Web Lab

有限自动实验使用同一 Simulation：

```bash
sim validate path/to/scene.yaml
sim run path/to/scene.yaml --report build/lab-result.json
# 自动编译真实专用固件，保留直接通道及原生交互/GDB验收：
bash tools/run-lab-channels-smoke.sh
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

输入端口使用 qualified `main.key`（布尔）、`main.gpio.B0`（0/1）、`main.uart1`（配置 UTF-8 文本或 Rust 字节输入）。
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
sim firmware build/student.elf --debug --qmp /tmp/student-qmp.sock
```

交互入口复用同一运行环境，默认连接 UART1，按需要增加 CAN 和 ADC：

```bash
sim lab interactive --elf build/student.elf --uart uart1 --can can1 --adc
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

Windows PowerShell 使用同一组命令；原生 GDB 调试从暂停启动：

```powershell
.\sim.exe lab interactive --elf .\example.elf --gdb-port 1234 --paused --timeout 3600
```

在另一终端用 ARM GDB 连接 `127.0.0.1:1234`，加载相同 ELF 后可设置断点、继续、单步、
读写寄存器/内存，或用 `monitor system_reset` 复位。`--qmp` 另开 QEMU 原生 Unix socket，
Windows 使用较短的新绝对路径；该选项的既有 100 字节路径上限也适用于 Windows。
外部调试器退出后，Lab 会话继续由原终端或 Web 管理；总期限包含调试停留时间。

Lab smoke 同时验证实际 GDB attach/detach 与第二 QMP monitor；需要 `arm-none-eabi-gdb`
或 `gdb-multiarch`，可用 `GDB_ARM` 覆盖。它验证数字固件消费者，不代表实板准确度。

<a id="web-lab"></a>

## Web Lab

构建项目 QEMU 后，在同一运行环境中启动本地网页实验台：

```bash
sim lab web
# 或预选已编译的 HAL 第一课：
sim lab web --elf build/teaching/Debug/gpio_key.elf
# 启用原生 GDB，调试期限改为两小时：
sim lab web --elf build/student.elf --gdb-port 1234 --timeout 7200
```

打开 `http://127.0.0.1:8765`，选择固件并点击“加载”，初始停在复位处；点击“运行”后
固件开始执行。“暂停”使用原生 QMP；“停止”完成进程和通道清理后返回。
“重新加载”清理旧实验，再从新进程的复位状态开始。上传文件最多 64 MiB，
停止后可替换；文件保留到替换或服务关闭。加载沿用 QEMU 的 ARM ELF/raw BIN 语义，
文件可读并不表示它适合本板或应用已经就绪。

同一服务只允许一个控制页面。关闭页面后实验继续运行，重新打开接回原会话，
按住的瞬时用户按键会在断开时释放；未收到结果的输入不会自动重发。
默认每次实验的 host 总期限为一小时，包含启动和暂停；`--timeout` 可调整，
`--startup-timeout` 默认 10 秒。`Ctrl-C` 或 TERM 关闭服务并清理实验。
`--port` 改变本地 HTTP 端口；服务仅绑定回环地址。

页面提供用户按键、LED、VIN、GPIO A–H 检查器，以及 UART1/2/3/5/7/10、CAN1/2/3
和 ADC1 通道 0–31。UART 支持文本、HEX 和显式追加换行；CAN 使用既有 ID/flags/DLC
准入；ADC 可输入 raw16 或 0–3300000 µV，暂停时明确拒绝。全部通道由同一 Lab owner
连接。输入“已接受”表示提交给现有 transport/Fabric；实际固件反应要看后续输出。

状态和原始 JSON 来自只读 native `board-state`，纳秒时间在浏览器中保留为十进制字符串。
GPIO 采样电平、ODR、输出掩码分开展示，AF 仅显示复用模式；模型引脚不等同于全部可接线引脚。
LED 明示 WS2812 DMA 波形或 GPIO latch 近似，ADC 输入值不是固件的转换读数。
UART 时间是轮询样本，CAN 保留 wire 时间；页面刷新不驱动仿真，也不建立全局锁步。
服务端历史最多保留 4096 条或 1 MiB，页面最多显示 600 条或 256 KiB，省略均可见。

板卡图为原创逻辑示意。当前快照未公开的 IMU、Flash、蜂鸣器和 USB 等内部状态没有网页读数。
当前固件若依赖 IMU 或电机环境，仍需使用已验证的对应 worker 入口；此板卡实验台尚未接入
物理场景。源码断点与变量继续使用原生 GDB/VS Code；启用 `--gdb-port` 后网页显示连接地址。

开发验证可安装测试专用 Chromium：

```bash
.venv/bin/python -m pip install aiohttp==3.14.3 playwright==1.56.0
.venv/bin/python -m playwright install chromium --only-shell
cargo test --locked -p sim-host --features host-runtime --test lab_session --test lab_command
```

真实浏览器门使用已构建的 HAL Debug/Release 和既有裸机通道夹具：

```bash
mkdir -p build/web-test
arm-none-eabi-gcc -mcpu=cortex-m7 -mthumb -O2 -Wall -Wextra -Werror \
  -ffreestanding -fno-builtin -fno-stack-protector -nostdlib -nostartfiles \
  -Wl,--gc-sections -Wl,--build-id=none -Wl,-T,smoke/dm_mc02_uart_smoke.ld \
  -o build/web-test/lab.elf smoke/dm_mc02_lab_smoke.c
.venv/bin/python smoke/dm_mc02_web_smoke.py --sim target/release/sim \
  --debug-elf build/teaching/Debug/gpio_key.elf \
  --release-elf build/teaching/Release/gpio_key.elf --lab-elf build/web-test/lab.elf \
  --output build/web-test/browser-run
```

当前 Rust Web 已通过这三个实际浏览器案例。输出目录必须是新路径；报告保存原始输入哈希、截图和清理结果。HAL GPIO 与裸机通道测试
属于不同消费者证据，不代表实板校准或完整业务固件就绪。

<a id="environment"></a>

## 原生环境

源码构建默认使用 **Ubuntu 24.04 x86_64** 系统工具链，Nix 是可选的固定环境。
Windows 下载包的范围与 CI 状态见[安装](#installation)；macOS 和 ARM 宿主原生构建尚未验证。

<a id="ubuntu"></a>

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

**构建并运行 QEMU 不需要 `uv`**；Rust 产品使用 Cargo。Python 测试环境与产品分离，
不重复安装 Meson 或 Ninja。需要运行完整回归门禁时，额外安装：

```bash
sudo apt-get install -y \
  cmake \
  ripgrep \
  gcc-arm-none-eabi \
  binutils-arm-none-eabi \
  gdb-multiarch
```

CMake/CTest 构建原生 Host 测试，ARM GCC/binutils 构建 guest，GDB 验证原生调试连接，
ripgrep 供完整门禁检查使用。Rust 构建还需安装 rustup，由仓库工具链文件选择 Rust 1.91.1。

<a id="test-environment"></a>

独立 Python 构建/测试环境使用与 CI 相同的安装方式：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install 'pytest>=8,<9' PyYAML==6.0.3 mcap==1.5.0 crccheck==1.3.1 setuptools==80.10.2 wheel
.venv/bin/python -m pip install --no-build-isolation qemu/upstream/python
```

PyYAML/MCAP、独立裁判 CRC oracle 与固定上游 QMP client 是测试消费者，不进入 Rust 产品。
`pyproject.toml` 的 `dev` group 也声明这些工具；QEMU 上游 Python distribution 的构建需
setuptools/wheel 和 `--no-build-isolation`，单独 `uv sync --group dev` 不能替代此构建前提。
浏览器门另装 `web-test` 所列工具；`native-test` 中的 Python MuJoCo 仅供确有独立数值对照需求的测试。

当前 profile 使用内部 FDT，并关闭 Capstone、slirp、文档及 SDL/GTK 图形界面，因此不需要对应系统开发包或 Sphinx。Pixman 是可选依赖：没有 `libpixman-1-dev` 时，QEMU 会关闭依赖 pixman 的通用显示设备；DM-MC02 的默认 headless 使用不依赖它。如需这些通用显示设备，可额外安装：

```bash
sudo apt-get install -y libpixman-1-dev
```

启用物理后端时按[安装](#installation)构建原生 SDK 与对应 Cargo feature。首次下载 Cargo crate、
测试工具或 QEMU Meson wraps 需要网络，`/tmp` 必须可写。

<a id="nix"></a>

### Nix（可选）

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

选择 Nix 时，QEMU 和对应测试在 `nix develop` 内执行，使编译器、GLib、zlib 与 pkg-config 保持一致。
`PYTHON` 指向固定的 Nix 构建解释器，独立测试环境仍按[测试环境](#test-environment)准备。
Rust 由仓库 `rust-toolchain.toml` 固定；完整 Linux 分发包使用 Ubuntu 系统工具与原生 SDK 构建，
打包会拒绝依赖 `/nix/store` 的可执行文件。MuJoCo、Gazebo、ROS 2 使用各自的原生 SDK，
不安装 Python runtime extras。退出 shell 用 `exit`。

<a id="get-the-source"></a>

## 获取固定原生源码

```bash
git clone --branch refactor/composable-platform https://github.com/lym12321/qemu-dm-mc02.git
cd qemu-dm-mc02
git submodule update --init --depth 1 qemu/upstream
```

也可以使用 SSH 地址：

```bash
git clone --branch refactor/composable-platform git@github.com:lym12321/qemu-dm-mc02.git
```

当前用户入口与打包工作流位于 `refactor/composable-platform`；`main` 保存此前版本。同一仓库中的 `dm-mc02/v8.2.2` 分支保存 QEMU fork，外层子模块固定到一个确切提交。`qemu.lock` 记录上游 v8.2.2 身份、fork 基线和 fork 提交；子模块 gitlink 与 `fork_commit` 必须一致。不要使用 `git submodule update --remote` 替代锁定版本。

首次构建不需要递归获取 ROM 子模块。**GitHub 的 main 源码 ZIP 不包含 QEMU 子模块源码，不能直接构建**；请使用 Git clone 并初始化子模块。通过本文快速命令获得的浅子模块足以构建和运行，源码打包时需要的完整历史见[源码包章节](#update-and-source-package)。

<a id="build"></a>

## 构建

以下命令均从仓库根目录执行：

```bash
bash tools/build-qemu.sh
export QEMU_SYSTEM_ARM="$PWD/build/qemu/qemu-system-arm"
```

后续源码 CLI 示例使用此 `QEMU_SYSTEM_ARM`；`firmware` 与 Lab 的默认查找是发行目录的相邻 `bin/`。

Ubuntu 用户请先安装[环境章节](#ubuntu)的额外依赖；可选 Nix shell 已包含 CMake、ARM 工具链、ripgrep 和 uv。构建 Host 测试程序：

```bash
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
| `target/release/sim` | 所选 Cargo features 的 Rust CLI |
| `.venv/` | 独立 Python 构建与测试驱动环境 |

QEMU 默认构建为 Release、`arm-softmmu` 和 DM-MC02 设备 profile。调试构建可运行 `QEMU_BUILD_TYPE=debugoptimized bash tools/build-qemu.sh`；性能测量前应重建 Release。`tools/build-qemu-generic.sh` 用于检查通用 ARM 复用边界，不是运行 DM-MC02 固件的入口。

构建目录中的 `.dm-toolchain` 记录 configure 输入。旧目录没有此记录、所选工具/flags 改变或设置 `QEMU_RECONFIGURE=1` 时，脚本重新执行完整 QEMU configure，以更新缓存 compiler 和 Meson 入口；通常无需手动删除构建目录。

### 原生基础检查

不需要外部业务固件即可检查 machine 注册。下面的 guest integration 还需要 ARM 工具链：

```bash
build/qemu/qemu-system-arm -machine help
bash tools/run-mc02-smoke.sh
bash tools/run-ospi-smoke.sh
```

smoke 脚本会编译并运行仓库中的最小 guest。修改 QEMU 模型后先重新构建。

<a id="native-debug"></a>

## 原生固件、调试与观察接口

### 直接运行固件

输入 ELF 必须面向 DM-MC02 地址空间。下载独立包，或从源码安装 CLI 并构建项目 QEMU 后，
直接传入已有 ELF；此入口不需要 YAML、Python QMP client 或 MuJoCo。
下面使用源码安装的 `sim`；下载包使用 `./sim` 或 `.\sim.exe`：

```bash
sim firmware /absolute/path/to/app.elf
```

ELF 是必填位置参数，不读取 `DM_MC02_ELF`。QEMU 选择顺序为 `--qemu PATH`、
`QEMU_SYSTEM_ARM`、可执行文件相邻 `bin/` 中的 QEMU；源码构建运行时
显式指定兼容二进制，或先设置 `QEMU_SYSTEM_ARM="$PWD/build/qemu/qemu-system-arm"`。
默认 machine 是 `dm-mc02`；源码构建的运行环境与构建时保持一致。

默认 UART1 与 QEMU monitor 共用终端（原生 `mon:stdio`）。`Ctrl-a c` 切换 UART/monitor，`Ctrl-a h` 查看帮助。monitor 中可使用 `info status`、`stop`、`cont`、`system_reset` 和 `quit`；`quit` 会走正常关闭路径。用 `--uart uart2` 等名称选择其它 UART，或用 `--uart none` 仅显示 monitor。UART5 只接收，选择 `--uart uart5` 可输入字节但没有 TX 文本。当前 `trobot` Release 关闭了 logger/terminal，串口没有文本并不代表固件未运行。

常用参数以 `sim firmware --help` 为准：

| 参数 | 用途 |
| --- | --- |
| `ELF` | 必填位置参数；面向 DM-MC02 的固件 ELF |
| `--debug` | 从复位暂停，启用原生 GDB；默认 `127.0.0.1:1234` |
| `--gdb-port PORT` | 配合 `--debug` 指定 GDB 端口，范围 1–65535 |
| `--uart NAME` | `uart1`（默认）、`uart2`、`uart3`、`uart5`、`uart7`、`uart10` 或 `none` |
| `--qmp PATH` | Linux/WSL2：创建 QMP Unix socket，`server=on,wait=off` |
| `--cosim PATH` | Linux/WSL2：创建 co-sim Unix socket，`server=on,wait=off`；worker 单独启动 |
| `--qemu PATH` | 指定兼容的项目 QEMU 二进制 |
| `--machine NAME` | `dm-mc02`，可附加逗号分隔的原生 machine 属性 |

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
sim firmware /absolute/path/to/app.elf --debug
```

终端二加载同一 ELF 并连接。Ubuntu 可执行 `sudo apt-get install -y gdb-multiarch`，并将下面的程序名替换为 `gdb-multiarch`；Windows 另装 ARM GDB，可选 Nix 环境已提供 `arm-none-eabi-gdb`：

```bash
arm-none-eabi-gdb /absolute/path/to/app.elf
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

默认 DM-MC02 profile 的 UART5 仅接收；选择 `--uart uart5` 可输入数据，但不会输出文本。

例如将 USART1 输出到文件，monitor 留在终端：

```bash
mkdir -p build/runtime
export DM_MC02_ELF=/absolute/path/to/trobot.elf
build/qemu/qemu-system-arm -machine dm-mc02 -kernel "$DM_MC02_ELF" \
  -nodefaults -display none -monitor stdio \
  -serial none -serial file:build/runtime/usart1.log
```

FDCAN 使用固定 84 字节二进制帧，经项目 chardev/socket 连接；它不是文本串口，也不是直接的 SocketCAN 设备。使用仓库的 worker 处理该通道，接入 Linux CAN 时使用 worker 的 `--socketcan` 参数。

#### QMP

自动化控制可为每个进程创建独立、较短的 Unix socket 路径：

```bash
dm_run_dir=$(mktemp -d /tmp/dm-qemu.qmp.XXXXXX)
sim firmware /absolute/path/to/app.elf --qmp "$dm_run_dir/qmp.sock"
rm -f -- "$dm_run_dir/qmp.sock"
rmdir -- "$dm_run_dir"
```

创建原生 QMP socket 不要求 Python QMP client。项目 Python 工具连接时通过 `tools/dm_mc02_qmp.py` 复用固定版本的 QEMU Python client。不要另开第二个 reader 读取同一 QMP 连接。深层临时目录可能超过 `sockaddr_un` 路径限制；Linux smoke 应使用脚本管理的 `/tmp/dm-qemu.*.XXXXXX` 私有临时目录。

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

Rust 自动化复用 `sim_host::boards::{validate_snapshot, digital_level}` 校验并读取同一原生 JSON。
`digital_level("gpio.B0", &state)` 等读取公开采样值，不从 guest 内存重建 GPIO。
输入和 pause/resume/reset 仍走原生 QMP；连接、有限期限、关闭和应用 ready 由调用方拥有。
生产 QMP 使用 `sim_host::qmp::QmpSession`，批量独立查询按输入顺序保留错误，
不提供原子快照或跨 I/O/CPU/world 边界的缓存。失败后停止当前操作并关闭连接，不重发整组。
独立 Python 原生测试仍复用 `tools.dm_mc02_test_harness.running_qemu()` 的启动和有界清理。

`sim_host::lab::channels::LabChannels` 可借用既有 UART、CAN 和 co-sim 连接：UART 使用无帧
`StreamTransport`，CAN 使用固定 84 字节 `CanStream` 与 CAN Fabric，ADC1 使用 v1
`FramedSocket`。它不打开或关闭连接，也不推进 CPU 或 plant。`service(timestamp_ns, flush_uart)`
每个连接只读一次有界数据；CAN 观察保留原始 QEMU 时间，UART 时间只是调用者的轮询样本。
需要截止时间判断时，应在读取后再查询 QMP 虚拟时间作为观察上界。
已有独立 UART TX owner 时可调用 `service(timestamp_ns, false)`，
继续读取并报告 UART 字节，同时保留其发送队列和后缀；TX owner 负责发送与 flush。
该布尔参数显式选择 UART flush；CAN 和 ADC 服务保持各自原语义。

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

Rust Machine 的 CAN adapter 通过同一 `can-execution-state` 边界完成输出 drain、输入 receipt 和初始基线观察。
观察要求原连接唯一 reader/writer、普通非阻塞 AF_UNIX、有限绝对 host 期限，原生执行目标 reached 且 paused；
初始基线则要求 idle、无目标、时间等于 RESET_ACK 原点且 prelaunch/paused。
整个区间须排除额外发送、复位、断电、restore、配置、重连和清队列，CPU 暂停本身不能证明这些前提。

输出先确认 native TX 队列为空，再取得新的空读和空 framing tail，最后复核执行状态、连接身份和 drop 基线。
输入 receipt 使用发送前的 completed_frames 基线与已知完整记录数，不能从当前队列长度推算；
等待只 peek 原连接，未读输出、framing tail、EOF 或连接身份变化均失败并保留字节。
达到目标时 native 部分记录和 host TX 队列也必须为空；rx_dropped 可以增加，因为处理完成包含被拒收的记录。
这些观察只证明 transport 处理，不证明固件消费或存储，也不推进 world。

每轮有界读取至多 4096 字节，读取、逐帧交付、flush 和最终查询均检查存活/期限。
sink、解码、I/O、状态变化或期限错误冻结当前尝试，保留部分效果与借用流后缀；不重试整个区间。
原生 QMP 查询不是原子快照，也不证明同刻所有 timer/IRQ、真实 CAN 时序或 CPU/plant 全局屏障。

<a id="external-simulation-worker"></a>

## 外部 worker 与原生后端

这部分保留独特的 legacy v1/v2、MIT/达妙、电机、ROS 2 与 Gazebo 连接能力，
所有生产 worker 均为编译后的 `sim worker`。CAN/四轮 worker 借用同一 Simulation，
MIT/达妙、ROS transport 与 v1/v2 保留各自的协议边界。
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

在三个终端中均进入仓库根目录，并使用构建 QEMU 时的相同系统或可选 Nix 环境。终端一启动 QEMU，输出的临时目录供终端二使用：

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
dm_run_dir=$(mktemp -d /tmp/dm-qemu.trobot.XXXXXX)
printf '%s\n' "$dm_run_dir"
sim firmware "$DM_MC02_ELF" --debug \
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
bash tools/build-linux-runtime.sh --example trobot_bench
bash tools/run-trobot-business.sh
```

该场景调用编译后的 `trobot_bench` 与 MuJoCo 3.12.0 原生库，Python 只负责采集和报告。常用参数如下；完整帮助可运行
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

Rust 迁移后在同一 i5-12400 宿主完成三轮 72 秒运行，每轮实际测量至少 60 秒，均通过上述原阈值：

| 测量 | 三轮范围 |
| --- | --- |
| plant 轨迹推进 RTF | 0.999999793–1.000000680 |
| firmware tick RTF | 0.999933121–0.999953588 |
| worker 采样 CPU / 最大 RSS | 14.26–15.60% / 13.46–13.63 MiB |
| QEMU 采样 CPU / 最大 RSS | 119.38–119.83% / 41.89–42.18 MiB |

该门测量当前固定业务场景的 wall-clock pacing。plant 时间戳在 ACK 和 pacing sleep
之前记录，表示轨迹推进；无节流容量、完整 loop 完成延迟、CPU/plant 全局锁步及任意机构
性能仍需独立验证。原 Robot 10 秒及 DJI 20 秒预算失败仍保留；本节业务 pacing 结果不覆盖它们。

### worker 的后端边界

手动运行 stationary startup 示例只建立 co-sim 控制/采样通道。要交换电机总线数据，还需将 QEMU FDCAN chardev socket 配好并给 worker 传入 `--fdcan`；完整 wiring 示例见 `tools/run-dm-motor-smoke.sh`，上面的业务入口已自动连接该通道。通道联通不等于机构闭环已验证。

worker 按 RESET 时的时间原点转换 QEMU 与 plant 时间，CAN 反馈携带 QEMU 时间戳。
当前 FDCAN 收到帧就立即交付；时间戳不会延迟未来帧，也未实现 guest RX timestamp。
`--realtime` 只控制 worker 的 wall-clock 节奏，v2 ACK 表示输入接受、DONE 表示 BMI088
消费完成；它们都不保证 CPU 与电机 plant 在同一时刻推进。

worker 支持的常用选择：

| 场景 | 入口 |
| --- | --- |
| NullEngine fixture | 默认 `--engine null`；静止 IMU 输入和简化电机响应，用于功能/传输验证 |
| MuJoCo adapter | 构建 `mujoco` feature 后运行 `bash tools/run-mujoco-worker-smoke.sh` |
| ROS 2 adapter | 安装并 source 系统 ROS 环境（验收环境为 Jazzy），运行 `bash tools/run-ros2-worker-smoke.sh` |
| 已有 Linux CAN 接口 | `--socketcan can0 --fdcan /path/to/fdcan.sock` |
| 自定义 plant | 在 Rust 中实现公开物理/worker 接口并显式编译，不动态导入插件 |

MuJoCo adapter 在初始化、reset 和每次 `mj_step` 后调用原生 `mj_forward`，使姿态和
IMU derived state 对应当前关节状态；这一步不会增加 plant 时间。gyro/accelerometer
来自 MuJoCo site-local 传感器，worker 再转换成 deg/s 和 g。该观察边界仍不改变旧 worker
的 step-start wire 时间约定，也不建立 QEMU/plant 全局锁步；固定业务 pacing 结果见上节。

MuJoCo worker smoke 验证 adapter 和测试模型；下文的 MotorTest 另验证实际固件电机组件的数据边界。普通 Release 的固定单转子业务使用上面的 [business fixture](#trobot-release-business-fixture) 入口。ROS 2 默认 topic 为 `/dm_mc02/imu`、`/dm_mc02/joint_states` 和 `/dm_mc02/motor_cmd`；adapter smoke 不等同于 Gazebo world/model 联调。运行时完整参数以 `bash tools/run-worker.sh --help` 为准。

具名物理接口使用锁定的 MuJoCo 3.12.0，可独立于 QEMU 和 CAN 调用：

```rust
use sim_host::physics::{JointPhysics, mujoco::{MujocoBindings, MujocoPhysics}};

let mut plant = MujocoPhysics::from_xml_path("smoke/dm_mc02_mujoco_smoke.xml", MujocoBindings {
    joints: vec![("test_joint".into(), "motor0".into())],
    imus: vec![("imu".into(), "imu_gyro".into(), "imu_accel".into())],
})?;
plant.set_joint_effort("test_joint", 0.005)?; // Nm
plant.step(1_000_000)?; // ns
println!("{:?}", plant.get_joint_state("test_joint")?);
println!("{:?}", plant.get_body_pose("test_body")?);
println!("{:?}", plant.get_imu("imu")?); // rad/s, proper m/s²
plant.close()?;
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
改写力矩。`validate_steps(&[dt1_ns, dt2_ns, ...])` 按顺序检查未来 native 时间，提前
拒绝浮点时间分辨率不足的后续区间。`close()` 释放此实例持有的原生 model/data；重复关闭无害，关闭后操作拒绝。

### CAN worker 会话

`sim worker --can-profile rotor` and `sim worker --can-profile four-wheel` use the same
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
failed effects are not retried or rolled back. `SIM_EXECUTABLE` selects the compiled worker for shell smoke consumers;
`PYTHON=/path/to/python` selects only the independent firmware test driver.

The native RESET/suffix regression is included in:

```sh
QEMU_SYSTEM_ARM=/path/to/pinned/qemu-system-arm \
  PATH=/path/to/native-env/bin:$PATH bash tools/run-qemu-worker-smoke.sh --chassis
```

该门补充真实 SPI readback 与 DJI/Chassis 固件消费者测试；它们不能证明全局 CPU/World 屏障、
物理标定或持续实时性能。

### Gazebo 原生关节接口

Rust 的 `sim_host::physics::gazebo` 使用 Gazebo Harmonic `gz-sim8 8.15.0` 与默认 DART 求解器。
CXX 只传递值、错误和生命周期，原 `NativeWorld` 继续拥有 Server、solver、传感器和线程。
生产 Python ABI 已移除；SDK 必须使用固定上游提交
`446a44335a45b704b4d36dabcc5508ee34eeb3d8` 的 `SKIP_PYBIND11=ON` 构建。
同版本但链接 libpython 的 ROS vendor gz-sim 不能作为无 Python 产品的替代品。
构建见[安装](#installation)，Cargo feature `gazebo` 启用关节/body/IMU，
`gazebo-rendering` 再启用原生相机/LiDAR。使用独立构建目录，避免 Conda 的 Protobuf 覆盖系统 SDK。

```rust
use sim_host::physics::{JointPhysics, gazebo::{GazeboBindings, GazeboPhysics}};

let mut plant = GazeboPhysics::from_sdf_path("models/gazebo_rotor.sdf", GazeboBindings {
    joints: vec![("shaft".into(), "rotor".into(), "shaft".into())],
    bodies: vec![("body".into(), "rotor".into(), "body".into())],
    ..Default::default()
}, &[])?;
plant.set_joint_effort("shaft", 0.4)?; // Nm
plant.step(100_000_000)?; // 100 native 1ms ticks
println!("{} {:?}", plant.time_ns()?, plant.get_joint_state("shaft")?);
println!("{:?}", plant.get_body_pose("body")?); // m, quaternion wxyz
plant.reset()?;
plant.close()?;
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
`validate_steps(&[dt1_ns, dt2_ns, ...])` 还检查每个后续区间及累计 native 时间范围。

`physics.gazebo` 与 `physics.mujoco` 共用同一个 NativeFactory，支持 direct effort、
共享 joint driver 与完成态 IMU；绑定保持后端显式类型。示例片段中的路径相对 YAML：

```yaml
nodes:
  world:
    kind: physics.gazebo
    model: gazebo_imu_rotor.sdf
    joints: {shaft: [rotor, shaft]}
    bodies: {body: [rotor, body]}
    imus: {inertial: [rotor, body, imu]}
experiment:
  inputs:
    - {at: 0ns, endpoint: world.effort, value: {shaft: 0.4}}
```

独立 API 调用者拥有其唯一 step/reset/close；使用场景时由唯一 Simulation world owner 执行。
原生 sensor 消息交付、MCU 消费与 CPU 同步仍是各自独立的边界。

可通过原生 SDFormat include 组合本地静态场景，并用 `resource_paths` 指定
`model://` 的模型目录。目录以调用者当前路径解析，必须存在且不含冒号或 NUL；
配置只作用于本次加载，不修改全局环境。原生 SDFormat 的安装路径和 `SDF_PATH`
回退仍可参与查找。这是本地资源接口，不是文件系统沙箱或远程下载器。
模型 include、mesh、material script 和纹理文件的缺失在 Server 启动前拒绝；
纹理准入只检查路径和文件存在，不保证可解码或渲染。reset 使用首次准入的
SDF DOM，外部纹理、mesh 等文件仍需保留，不会随 DOM 保存其内容。

仓库提供固定社区版本的 RMUA 2019 静态场地及两个落体测试立方体：

`GazeboPhysics::from_sdf_path` 的第三个参数传入 `&[PathBuf]` 资源目录，
`GazeboBindings::bodies` 可绑定 `floor_probe/body` 与 `obstacle_probe/body`。
`models/gazebo_rmua19.sdf` 使用同一公共 API；调用 `step(5_000_000_000)` 后读取完成位姿，再 reset。

模型配置、几何和木纹图片原样复用；固定来源、哈希和 Apache-2.0 许可见
`models/vendor/rmua19/NOTICE`、`SOURCES.json` 和 `LICENSE`。场地仅作为历史社区
夹具，未验证当前比赛尺寸、实物接触参数或图片实际渲染；原生平面的碰撞范围
可能大于视觉尺寸。该组合只使用 Physics，不包含相机、LiDAR、机器人控制、
裁判规则或 Arena 调度。SDK 的 `gazebo-native-resources` 测试独立验证资源准入，
Rust `physics_gazebo` 与 `physics_gazebo_consumers` 再验证实际接触、公共位姿和 reset。

IMU 使用原生 gz-sensors 的同步生成和公开 getter，配置为
`imus={逻辑名称: (model, link, sensor)}`：

`GazeboBindings::imus` 使用 `(alias, model, link, sensor)` 元组。
`models/gazebo_imu_rotor.sdf` 的 `("imu", "rotor", "body", "imu")` 绑定可通过
`get_imu("imu")` 读取完成态的 sensor-local rad/s 与 proper m/s²。
初始与 reset 的静止样本为零角速度、`(0,0,9.80665)` 加速度。

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
CMake 只构建独立的原生资源、mailbox 与 ROS 观察器测试；生产代码由 Cargo/CXX 构建。
在匹配的原生 SDK 环境中显式启用测试：

```bash
cmake -S sim/physics/gazebo_native -B build/gazebo-native \
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

生产接口使用 Cargo feature `gazebo-rendering`，独立于 CMake 测试开关：

```bash
bash tools/build-linux-runtime.sh --example gazebo_render_producer
```

准备同一 SDK 的 pkg-config、native library 和插件环境后，可用 Cargo 分层验证：

```bash
cargo test --locked -p sim-host --features scene,devices,gazebo-rendering,trace \
  --test physics_gazebo --test physics_gazebo_consumers --test physics_gazebo_rendering \
  --test physics_gazebo_simulation --test scene_gazebo --test scene_gazebo_cli
```

相机/LiDAR 使用 `GazeboBindings` 的四元组绑定与 `get_camera` / `get_lidar`。
其实际 RGB8/米/弧度值由原生快照提供；getter 可能尚无消息。

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

getter 立即返回最新实际交付的不可变快照，未收到消息时返回 `Ok(None)`，不等待或
推进物理时间。`timestamp_ns` 保留原生 sensor 时间（零合法）；`frame_id` 原样保留。
图像有 `width/height/step/data`，扫描保留完整 `ranges_m/intensities` 和角度/距离元数据；
原生无穷距离与单行 `vertical_angle_step_rad=NaN` 保留，NaN 距离拒绝。
最新快照覆盖中间观察，合法时间戳不递增的消息丢弃；新消息的格式错误冻结实例，须 reset。
该保留上限不限制原生 transport 的总队列。LaserScan 的 `world_pose` 不在首版接口中。
`step()` 完成不保证目标时刻图像已交付；需要消息时由调用者设置有限观察期限。
reset 重建 producer 和订阅，清空旧观察；close 完成 Server 线程与订阅清理后释放 owner。
驱动、RunOnce、Stop/join 为原生阻塞调用，进程外期限仍由调用者负责。

仓库提供带传感器的历史社区场地组合，使用同一接口和固定本地资源：

`models/gazebo_rmua19_sensors.sdf` 使用同一 `GazeboBindings` 与
`models/vendor/rmua19/resource/models` 资源目录；按上述 front/scan 绑定读取实际快照。

相机与 LiDAR 位于 `(3, 0, 0.25)`，朝向原场地墙面；中心扫描距离为 1 米。
Rust `physics_gazebo_rendering` 用真实快照验证木纹渲染，并与仅去掉贴图的临时
资源对照比较色彩和空间变化，同时核验原资产哈希。此证据只覆盖固定场景，
不代表精确像素、相机标定或当前官方赛场；读取仍可能返回 `Ok(None)` 或较早帧。

要验证实际 ROS 2 消息交付，在同一构建目录额外启用 ROS 门；需要
ROS Jazzy 的 `ros_gz_bridge 1.0.24`、rclcpp 和 sensor_msgs：

```bash
cmake -S sim/physics/gazebo_native -B build/gazebo-native \
  -DCMAKE_CXX_COMPILER=/usr/bin/c++ -DCMAKE_BUILD_TYPE=Release \
  -DDM_GAZEBO_RENDERING=ON \
  -DDM_GAZEBO_RENDERING_TESTS=ON -DDM_GAZEBO_ROS_TESTS=ON \
  -DDM_GAZEBO_RUST_PRODUCER="$PWD/target/release/examples/gazebo_render_producer"
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

`gazebo-native-ros` 验证原生场景夹具；`gazebo-native-ros-consumer` 使用编译后的 Rust
`gazebo_render_producer` 中的实际 `GazeboPhysics` / `PhysicsParticipant`。后者在观察器建立两个原生订阅并匹配 ROS publisher
后启动 producer，验证公开 getter 的两份递增样本及原生/ROS 配对，然后等待 producer
正常关闭。ready 标记只证明订阅设置与 DDS 匹配；原生跨进程发现和图像交付仍为异步。
producer 最多推进 900 个 1 ms tick，观察期限为 10 秒。该门沿用固定红色方块场景，
不扩大为任意场景、导入纹理的 ROS 证据或 MCU 同步。

整个进程组合的执行期限为 20 秒，随后借用既有工具有界 TERM/KILL/reap，
CTest 总超时为 30 秒。报告保留子进程退出码、输入哈希和清理错误；本机固定
SDK 的 bridge 使用了 KILL 清理，不能据此宣称组件可通过公开 API 正常析构。
该门默认关闭，证明固定场景的真实 ROS 交付，不建立 ROS、渲染与 MCU 的全局时间屏障。

这个转子是未标定的验证夹具。Gazebo 的共享 factory、YAML/CLI、离线 MCAP 与
实际 Rust producer → 官方 ROS bridge 门已分别通过；ROS consumer 仅覆盖上述固定场景；
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
sim worker --can-profile four-wheel --mujoco-model models/four_wheel.xml \
  --cosim /path/to/cosim.sock --fdcan /path/to/can.sock \
  --protocol v2 --rate 1000 --realtime
bash tools/run-qemu-worker-smoke.sh --chassis
```

ID 1–4 固定为左前、右前、左后、右后；模型必须通过 `--mujoco-model` 显式指定，并具有相同具名绑定。默认 command timeout 为 100 ms，
`--torque-per-amp .02` 是未标定的转子 Nm/A 系数，`--can-delay-ns 0` 无额外交付延迟。
每步先完成共享 plant 的推进和反馈交付，再读取该目标时刻的原生 IMU；RESET 只重置
一次 plant，重试不重新推进或采样。此入口要求默认 engine，不组合旧 motor backend。
`tools/run-worker.sh` 是同一 Rust CLI 的薄 shell 入口。
可选 smoke 用实际 SPI guest 核对第一个 1 ms 目标的接触瞬态、六轴量化与 DONE 消费；
它仍不证明真实四轮固件闭环、实物标定、CPU 锁步或持续实时性能。

`trobot` 的默认关闭 `ChassisTestDebug/Release` 使用四个真实 `motor::dji` M3508
对象、现有 PID 与 BSP IMU，测试静止、前进、右轮更快转向、停止和禁用：

```bash
cd ../trobot
cmake --preset ChassisTestRelease
cmake --build --preset ChassisTestRelease
cd ../dm-mc02-qemu
bash tools/build-linux-runtime.sh --example chassis_worker
PYTHON=.venv/bin/python bash tools/run-trobot-chassis-test.sh \
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
cargo test --locked -p sim-host --features simulation,mujoco,devices --test physics_mujoco --test physics_simulation --test m3508_joints
```

`sim_host::devices::bmi088::imu_to_bmi088_units(&plant.get_imu("imu")?)` 将已完成的 native
site 采样转换为现有 BMI088 输入单位 `(gyro_deg_s, accel_g)`，其中 `1g=9.80665 m/s²`。
它保持三轴方向，不推进 plant、减去重力、添加 bias 或分配样本身份；已有器件负责
量程、ODR、filter 与 raw 字节，co-sim link 负责读取来源身份。

用同一四轮模型验证单个静止或转向采样的 QEMU SPI 读回：

```bash
PYTHON=.venv/bin/python bash tools/run-qemu-v2-step-done-smoke.sh --mujoco-source rest
PYTHON=.venv/bin/python bash tools/run-qemu-v2-step-done-smoke.sh --mujoco-source turn
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

多 QEMU 的有限运行使用前述 sim/v1 图。底层 Rust co-sim transport 与唯一 C STEP codec
继续承载旧 worker 协议，不给应用建立第二个 scheduler。RESET/ACK/DONE 和原始流尾部
各自保留其语义；不能从握手元数据推断 MCU、传感器和物理世界的全局锁步。

<a id="testing-and-performance"></a>

## 验证与性能

本轮或某个历史门通过，不代表所有原生 profile 通过。报告应分别记录命令、退出码、
环境、ELF SHA、实际 QEMU 路径/版本或能力、诊断与清理状态。缺依赖是 BLOCKED，
pytest 的 optional skip 不是该层 PASS；没有 Actions 运行记录时不能声称 CI 已通过。

| 层级 | 验证入口 | 证据与限制 |
| --- | --- | --- |
| Host core | `cargo test --locked --workspace --features sim-host/host-runtime` | Rust 生命周期、协议、互连、配置、实验与 checkout 外 CLI；不运行 MCU |
| 独立工具 | `.venv/bin/python -m pytest -q` | 构建/打包与黑盒消费者；无旧 Python 生产模块 |
| 原生 Board | 下方手动 smoke / Cargo native gates | ARM ELF + 固定 QEMU；缺依赖按实际 BLOCKED/SKIP 记录 |
| 多板 | `tools/run-simulation-smoke.sh --case dual-board` | 独立机器与时间映射，非全局硬件锁步 |
| 物理 | Cargo `physics_mujoco` / `physics_simulation` / `physics_gazebo*` | 匹配 native feature 和 SDK；单 solver、共享驱动与完成态 |
| 固件直连 | 下文 HAL/BSP、Motor、DJI、Chassis、Referee runner | 编译后的 consumer 与清理；不代表实板标定 |
| 浏览器 | `smoke/dm_mc02_web_smoke.py --sim ...` | 实际 Rust Web + 原生 HAL/裸机三案例 |

`.github/workflows/host-core.yml` 使用 Rust host 合同、Clippy 和独立 CLI gate，明确报告 Native NOT RUN。
原生门需 ARM GCC、兼容 QEMU 及对应 SDK，单独记录命令、产物身份、PASS/FAIL/BLOCKED/SKIP。
`--case all` 只运行五个基础场景；完整外设观察需按影响选择对应门：

```bash
for build_type in Debug Release; do
  for case in minimal-board pulse digital-link can-motor rotor-loop dual-board joint-loop imu-readback uart-link can-faults; do
    .venv/bin/python smoke/simulation_smoke.py --build-type "$build_type" --case "$case"
  done
  .venv/bin/python smoke/automatic_lab_smoke.py --build-type "$build_type" --case all
done
```

每次报告记录实际 ELF 哈希、QEMU 路径/哈希/版本、输入与观察时间及清理结果。
`simulation_smoke.py` 为每份报告保留独立的
`<报告名去扩展名>-assets/<case>/<Debug或Release>/` 构建目录；HAL GPIO 的
`--hal-elf` 也先复制到该目录后运行。报告和资产目录必须都是新路径，已有路径
（包括悬空符号链接）会在构建或启动 QEMU 前被拒绝。保留 JSON 时应同时保留旁边
的资产目录；后续 Debug/Release 验收不会覆盖它。Linux 原生报告逐 Machine 记录
实际进程的 `-kernel` 参数、ELF 路径/哈希和清理身份；双板使用本次指定的同一 ELF。
失败或中断也保留本轮资产和已取得的观察证据，不自动重试。
ADC 接受不等于 DR 读取；IMU ACK 不等于完整消费 DONE；UART 是字节流，读取批次不代表消息边界。
GPIO 完成态快照不提供未记录的中间边沿。硬件和物理标定仍需独立证据。

性能工具也是编译后 CLI 的黑盒消费者，`--sim` 或 `SIM_EXECUTABLE` 指定实际 Rust 可执行文件：

```bash
.venv/bin/python tools/benchmark_simulation.py --case host --sim target/release/sim --report build/host-benchmark.json
.venv/bin/python tools/benchmark_simulation.py --case all --sim target/release/sim \
  --qemu /path/to/compatible/qemu-system-arm --report build/platform-benchmark.json
.venv/bin/python tools/benchmark_simulation.py --case attribution --repeat 3 --sim target/release/sim \
  --qemu /path/to/compatible/qemu-system-arm --report build/execution-cost.json
```

默认三次固定工作量，报告和相邻 assets 目录排他创建，保留实际 ELF、CLI/QEMU 身份、
观察、输入哈希与清理。Host idle 使用10000 cuts，CAN 使用1024-input准入内的1000个有序帧。
原生样本运行200ms、1ms quantum、shift0，比较WFI/忙MMIO、单Machine/四轮World，
输出为discard/file/mcap，共12格。三种模式都经过CLI JSON序列化；discard排空、file落盘，
mcap额外记录。报告采集CLI墙时和Linux进程CPU/RSS，不再声称分离私有QMP、World或encoder成本。
构建时间不计入样本，启动、输出和清理分别按报告字段解释；短样本不证明持续实时能力。

以下性能表及原生MMIO定位属于Rust迁移前的历史证据，不能当作Rust版新测值。
旧Python私有观察钩子与 `--capture mmio` 已移除；需要新trace时使用QEMU原生日志或外部profiler。
历史Robot10秒预算失败仍保留，当前迁移不宣称恢复该预算。

历史本地实测（Linux、i5-12400、Python 3.12.3、QEMU 8.2.2、MuJoCo 3.12.0），
每格三次独立运行的 active wall time 中位数如下，单位为秒；均完成 200 Cut：

| 固件 / 组合 | 仅统计 | JSON 编码 | CLI 文件输出 |
| --- | ---: | ---: | ---: |
| WFI / 单 Machine | 0.597 | 0.592 | 0.601 |
| WFI / 四轮单 World | 1.348 | 1.484 | 1.560 |
| 忙 MMIO / 单 Machine | 8.118 | 8.133 | 8.035 |
| 忙 MMIO / 四轮单 World | 8.853 | 9.048 | 9.107 |

忙 MMIO 四轮文件模式中，QEMU 进程采样 CPU 中位数为 9.75s，Machine 完成等待为
8.155s；World.step 为 0.094s（包含驱动器反馈及其同步观察），编码为 0.129s，
写入与 flush 为 0.020s，输出约 2.53MB。CPU 可与 Host 并行，QEMU CPU 包含
TCG、外设及 I/O 线程；它不是纯指令执行成本。观察回调计时还包含本工具的测量包装。
这组短样本说明当前忙 MMIO 的原生执行路径值得优先剖析，不能据此把差值视为独占成本。

另行复测既有 Robot 2s 忙 MMIO 夹具：原 10s host 预算仍 FAIL（完成 214ms），
既有 120s 功能预算 PASS，含 ready 的 2.001s 逻辑时间实际用时 93.34s。
后者 QEMU 采样 CPU 为 103.42s，主 CLI 为 16.17s，输出约 47.1MB。
两次使用同一 ELF，未改变 quantum、shift 或阈值；功能通过不代表原预算恢复。

当前单 Machine 黑盒测量入口为：

```bash
.venv/bin/python tools/benchmark_simulation.py --case native-cost --capture none --repeat 3 \
  --sim target/release/sim --qemu /path/to/compatible/qemu-system-arm --report build/native-cost.json
```

以下 `none/mmio` 窗口和日志配额记录历史诊断工具的条件；`mmio` 私有入口已退役。
两种模式均运行上述 WFI、忙 MMIO 固件，各三次独立 Session，仅统计观察，不创建物理世界。
`none` 完成 200ms／200 Cut；`mmio` 在 1、10、100、199ms 各记录 10µs，随后恢复整毫秒
切点，合计 204 Cut。后者包含日志与额外查询开销，耗时只用于诊断，不能当成无插桩基线。
两者都保持 shift0、1ms quantum、10s startup／30s host 期限。

MMIO 模式需要 Linux `prlimit` 和 QEMU log trace 后端。每个日志最多 8MiB／65,536 行，
单行最多 4KiB；达到文件字节上限、超限或截断均 FAIL。不会放宽继承的较低文件限制；
缺失工具能力为 BLOCKED/78。输出路径不得包含控制字符或 QEMU 的 `%` 文件名模板。
报告保留绝对 MMIO 地址、重执行 PC、窗口前后的原生暂停回执、JIT 原文、日志哈希及清理结果。
日志的可选时间前缀是 Host wall time，不是 guest 时间；零次 MMIO 是合法的窗口结果。
`TB count` 是驻留翻译块数，必须结合 flush／invalidation 理解，不能等同累计翻译次数。

可选系统工具 `gprofng` 可以跟踪黑盒测量工具启动的 Rust CLI 与 QEMU 子进程，采样与 trace 应分开运行：

```bash
gprofng collect app -o build/native-cpu.er -p 10 -F '=.*qemu-system-arm.*' -a off \
  .venv/bin/python tools/benchmark_simulation.py --case native-cost --capture none --repeat 3 \
  --sim target/release/sim --qemu /path/to/compatible/qemu-system-arm --report build/native-cpu.json
gprofng display text -header -statistics -functions /path/to/qemu-child.er
```

先用报告中的 PID、QEMU 命令和子实验元数据选定对应的 QEMU 实验，避免合并测试驱动、Rust CLI 或编译器
数据。保留原始实验、采集器警告和 `<Unknown>`；包含时间不能相加，未知符号不能全部归为 TCG。
`capture=none` 只表示工具未启用 trace，外部 profiler 仍会影响运行。
本机 gprofng 2.42 能采到 vCPU，但报告 SIGPROF 屏蔽和退出附近计时器改变的警告；
采集完成与定量精度是两回事，当前采样只辅助定位，不提供经校准的成本比例。

2026-11-07 同一 Linux／QEMU 二进制下，无 trace 的三次 active wall 中位数为 WFI 0.596s、
忙 MMIO 8.081s。忙固件三个稳态窗口各观察到 GPIO、UART、ADC、FDCAN 各 625 次读取及
对应的 icount 重执行，驻留 TB 为 139，flush／invalidation 为零；1ms 启动尾部则主要是 UART
等待。WFI 的稳态窗口各观察到一次 GPIO 读取。访问频率与 CPU 独占成本分开解释；
这些结果没有证明新的加速效果，也没有恢复上述 Robot 10s 预算。

随后对 `cpu_io_recompile()` 复用 QEMU 已有的 `CPUState.cc`，消除每次 MMIO 重执行中的
动态类型检查。新增原生 CPU 测试比较连续执行和多个指令边界切点下的 SysTick 普通读取与
COUNTFLAG 读后清除；不改变 icount、MMIO 重放或完成态校验。

同机以 GCC 14.3.0、Release/O3、关闭 LTO 构建基线与候选，所有编译选项一致；两者使用
完全相同的 ELF 字节，按 AB/BA/AB/BA/AB 顺序各执行五次上述无 trace 负载。
每次保持 200ms／200 Cut、shift0、1ms quantum 和原 host 期限，结果中位数如下：

| 固件 | 基线 active wall | 候选 active wall | 基线 QEMU CPU | 候选 QEMU CPU |
| --- | ---: | ---: | ---: | ---: |
| WFI | 0.605s | 0.589s | 0.50s | 0.47s |
| 忙 MMIO | 8.461s | 7.279s | 9.83s | 8.36s |

忙 MMIO 的 wall 中位数减少约 14%，进程 CPU 约 15%，五对均同时下降；未排除首个较慢的
基线样本。active wall 不含启动，CPU 是含启动的进程累计采样（10ms 分辨率），不是纯 guest
成本；采样 RSS 中位数约 44MB，启动中位数约 25ms。独立 trace 运行的八组对应窗口中，
归一化后的有序 MMIO/重执行记录、读值、原生切点回执和 JIT 计数一致。
这些短样本只支持本负载下的改善。原 Robot 2s 场景在未放宽的 10s host 预算下仍 FAIL，
本次完成 239ms 后超时并清理；不宣称恢复历史预算或达到持续实时能力。

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

先准备[独立测试环境](#test-environment)，构建 Rust consumer 和 MuJoCo 原生库，再选择专用固件：

```bash
bash tools/build-linux-runtime.sh --example firmware_motor
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

先准备独立 Python 测试工具、Cargo、固定 QEMU 与原生依赖。生产后端由 Rust feature 选择，
不再安装项目的 Python MuJoCo extra：

```bash
bash tools/build-linux-runtime.sh
export DM_MC02_ELF=/absolute/path/to/trobot.elf
.venv/bin/python tools/dm_mc02_test_gate.py --jobs 4
```

统一门禁执行 QEMU/Meson、原生 Host CTest、Cargo host 合同、独立 pytest 和 shell integration。它会创建 `build/test-results/qemu-gate/<run>/summary.json`，并保存各阶段日志、退出码、动态测试分母及测试前后的关键二进制 SHA-256。可通过 `--report-dir /absolute/path` 指定报告根目录。

这是回归门，不是硅级验证。寄存器、IRQ、时钟和虚拟时间用例优先使用 QEMU 原生 libqtest；外部进程测试共享 QEMU 启动、socket、超时和清理逻辑。关键硬件断言注明 ST CMSIS/HAL/LL 或 RM0468 来源；实板观测仍需独立核对。

| 退出码 | 含义 |
| ---: | --- |
| 0 | 回归 PASS |
| 1 | FAIL |
| 2 | 参数错误 |
| 78 | BLOCKED（依赖或环境不足） |

只有明确列出的 ROS 2 和 MuJoCo 可选 smoke 可以 SKIP。SKIP 不等于相应能力通过验收。`--no-build` 仅适用于源码与构建产物已同步的情况；`--smoke-only` 只运行 shell smoke，不代替完整门禁：

```bash
.venv/bin/python tools/dm_mc02_test_gate.py --no-build --jobs 4
.venv/bin/python tools/dm_mc02_test_gate.py --smoke-only --jobs 4
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
cargo build --release --locked -p sim-host --features host-runtime --example firmware_dji
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

旧 `--scenario` / `--feedback-scenario` 专用 DJI runner 已退役；CAN延迟、反馈缺失与恢复通过普通
sim/v1图表达，固定ID1/ID5固件消费者使用上面的Rust `firmware_dji`。
原Scenario Release曾在20秒host期限内只完成1090步，最终watchdog观察缺失；
后续Debug/Release在原期限下各通过一次。这些历史结果保留，不能证明期限稳定性或实时性能。

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
| pytest 或测试 QMP client 不可用 | 按[独立测试环境](#test-environment)安装工具及固定上游 client；QEMU client 构建需要 setuptools/wheel 和 `--no-build-isolation` |
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

当 `cc` 指向 `~/.nix-profile/bin/cc`，而 GLib/zlib 来自 `/usr` 时，说明构建混用了两套依赖。Ubuntu 用户安装上面的系统开发包后，可保留旧目录并用系统工具链重建：

```bash
mv -T build/qemu "build/qemu.nix-backup-$(date +%Y%m%d-%H%M%S)"
PATH=/usr/bin:/bin CC=/usr/bin/cc CXX=/usr/bin/c++ \
  PKG_CONFIG=/usr/bin/pkg-config PYTHON=/usr/bin/python3 \
  bash tools/build-qemu.sh
```

使用可选 Nix 环境的开发者，应在 `nix develop` 内构建并运行，保持编译器和开发库一致。

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
- Gazebo/MuJoCo共用场景world factory；ROS/MIT worker与自由运行Lab/Web使用各自明确的原生边界，不建立第二个场景时钟。
- 不提供完整硅级模型、整机 migration、硬实时保证、分布式仿真、OJ 服务或任意不可信 manifest 的主机沙箱。

| 路径 | 职责 |
| --- | --- |
| `crates/sim-core` | 纯公共合同、唯一生命周期/时间/调度 |
| `crates/sim-host/src/config`, `fabric` | 无资源场景准入、CAN/UART/数字/IMU连接owner |
| `crates/sim-host/src/machine`, `profile`, `qmp` | 原生进程/QMP Backend、DM-MC02 profile与外设adapter |
| `crates/sim-host/src/devices`, `physics` | 协议设备、原生world和joint driver |
| `crates/sim-host/src/apps`, `trace`, `processes` | 预设、实验、记录/重放、可信上位机程序 |
| `crates/sim-host/src/lab`, `worker` | Rust原生自由运行Lab/Web与co-sim worker |
| `sim/physics/gazebo_native`, `crates/sim-host/native/ros2` | 复用的Gazebo C++ owner与ROS CXX transport |
| `cosim` | 唯一C wire codec与原生协议测试 |
| `crates/sim-host/examples`, `examples`, `smoke`, `tests` | 编译示例、自包含场景、真实ELF门与独立测试驱动 |
| `qemu/upstream`, `qemu.lock` | 固定 fork 与独立的原生器件/板级测试 |
| `models`, `third_party` | 固定物理/教学来源、许可证与来源声明 |
