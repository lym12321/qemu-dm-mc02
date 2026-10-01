# DM-MC02：基于 QEMU 的 STM32H723 仿真工程

在 Linux 上运行面向 DM-MC02 的 Cortex-M7 固件，调试已建模的 STM32H723 外设，
通过确定性虚拟时间接口连接电机、IMU 和外部仿真后端。项目不依赖 Renode；
固件是只读外部输入，不随仓库分发。

这是可构建、运行和回归验证的工程版本，**不是完整芯片数字孪生**。
支持范围与证据统一见 [CAPABILITIES.md](CAPABILITIES.md)。完整业务 ready、真实机构
闭环、整机 snapshot/live migration 尚未验证或支持。

## 导航

- [安装与构建](#安装与构建)
- [首次验证](#首次验证)
- [运行固件](#运行固件)
- [Flash 持久化](#flash-持久化)
- [串口与外部仿真](#串口与外部仿真)
- [测试与性能](#测试与性能)
- [更新与源码包](#更新与源码包)
- [常见问题](#常见问题)
- [目录与文档](#目录与文档)

## 安装与构建

### 1. 宿主依赖

已验证宿主为 **Ubuntu 24.04 x86_64**，需要 Python ≥ 3.11。Windows/macOS 原生
构建不在当前验收范围。Ubuntu 安装命令：

```bash
sudo apt-get update
sudo apt-get install -y build-essential pkg-config python3 python3-venv \
  libglib2.0-dev zlib1g-dev cmake git ripgrep \
  gcc-arm-none-eabi binutils-arm-none-eabi
```

另外按 [uv 官方说明](https://docs.astral.sh/uv/getting-started/installation/) 安装 uv，
确认 `uv --version` 可用。Meson、Ninja、pytest 由 `uv.lock` 固定；ARM 工具链用于
编译仓库自带的测试 guest。首次安装 Python 依赖及 QEMU wraps 需要网络，`/tmp` 必须可写。

### 2. 获取完整源码

```bash
git clone --branch main https://github.com/lym12321/qemu-dm-mc02.git
cd qemu-dm-mc02
git submodule update --init --depth 1 qemu/upstream
```

SSH 用户可将 clone URL 替换为 `git@github.com:lym12321/qemu-dm-mc02.git`。
子模块使用相对 URL，沿用父仓库的 HTTPS/SSH 访问方式；私有仓库需先配置对应凭据。
`main` 保存共享模型、工具和文档；同仓库的 `dm-mc02/v8.2.2` 分支保存 QEMU fork，
通过子模块固定到确切提交。为控制首次发布的历史体积，fork 保留官方 v8.2.2 的
完整源文件树作为本地基线快照，并在其上单独提交项目改动；不包含 v8.2.2 之前的
QEMU 历史。`qemu.lock` 同时记录官方提交/树哈希、本地基线和 fork 提交，打包器检查
基线树身份及 fork 差异。
**main ZIP 不含子模块源码，不能直接构建。** 无需递归下载全部 ROM 子模块；当前
ARM profile 所需 Meson wraps 由构建脚本获取。

### 3. 构建

后续命令均从项目根目录执行：

```bash
uv sync --locked --group dev
PYTHON=/usr/bin/python3 bash tools/build-qemu.sh
cmake -S . -B build/host -DCMAKE_BUILD_TYPE=Release
cmake --build build/host --parallel 4
```

主程序为 `build/qemu/qemu-system-arm`，Host 测试/桥接程序在 `build/host/`。
默认 Release、`arm-softmmu`、项目设备 profile；发行版自带的 QEMU 没有本项目
`dm-mc02` machine。`.venv/` 和 `build/` 都不入库。

调试构建可用 `QEMU_BUILD_TYPE=debugoptimized bash tools/build-qemu.sh`；性能测试前
切回 Release。`tools/build-qemu-generic.sh` 只检查通用 ARM 复用边界，不是本板运行入口。

## 首次验证

不需要外部业务固件：

```bash
build/qemu/qemu-system-arm -machine help | rg 'dm-mc02'
bash tools/run-mc02-smoke.sh
bash tools/run-ospi-smoke.sh
bash tools/run-flash-smoke.sh
```

脚本编译仓库内最小 guest，消费已经构建的 QEMU；模型修改后应先重新构建。
`tools/run-qemu.sh --smoke` 只是通用空 machine 探测，不是固件启动器。

## 运行固件

提供适用于 DM-MC02 地址空间的 ARM ELF；其他 MCU/板卡的固件不会自动适配。
本项目验证使用外部 `trobot` Release ELF，不修改或分发它。

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
sha256sum "$DM_MC02_ELF"
build/qemu/qemu-system-arm -machine dm-mc02 \
  -kernel "$DM_MC02_ELF" -nodefaults -display none \
  -serial none -monitor stdio
```

终端进入 QEMU monitor：`info status` 查看状态，`stop`/`cont` 暂停/继续，
`system_reset` 请求普通系统复位，`quit` 正常退出。没有 UART 后端时不会自动显示
串口文字。`-serial none` 保留二进制 co-sim 槽位，避免协议数据污染终端。

查看可配置属性：`build/qemu/qemu-system-arm -machine dm-mc02,help`。
GDB 调试可加 `-S -gdb tcp:127.0.0.1:1234`，使用 ARM GDB 加载同一个 ELF，执行
`target remote localhost:1234`。调试暂停与 trace 不属于默认性能场景。

自动化控制可加 `-qmp unix:/tmp/dm-mc02-qmp.sock,server=on,wait=off`；每个进程使用
独立短路径。项目 Python 工具经 `tools/dm_mc02_qmp.py` 复用固定 QEMU Python client。

## Flash 持久化

把启动命令的 machine 参数替换为：

```text
-machine dm-mc02,flash-file=/absolute/internal.raw,ospi2-flash-file=/absolute/external.raw
```

| 属性 | raw 大小 | 尺寸/I/O 错误 |
|---|---:|---|
| `flash-file` | 1 MiB（1048576 字节） | 拒绝启动 |
| `ospi2-flash-file` | 8 MiB（8388608 字节） | warning，保留擦除态 |

空路径禁用磁盘 I/O；缺失文件保留 `0xff` 擦除态。路径初始化后不可修改。
只有正常 shutdown 才保存完整镜像，崩溃/`kill -9` 不保证保存，没有掉电原子性。
内部镜像加载后，`-kernel` 仍会加载 ELF 段，因此程序区可能被覆盖；持久参数应避开
ELF 段。不要用零填充文件冒充擦除态。

外部 NOR 为独立 `dm-w25q64`，复用 QEMU QOM/SSI 和项目存储核心；官方
`m25p80.c`、`flash.h` 保持 v8.2.2 内容。命令为功能级同步模型，quad 用字节 token
近似；QE/保护/SFDP、真实忙时序和器件迁移未实现，详见能力矩阵。

## 串口与外部仿真

### UART 与 FDCAN

`-serial` 参数顺序决定连接位置：

| 槽位（从 0 开始） | 用途 |
|---:|---|
| 0 | 二进制 co-sim |
| 1–6 | USART1、USART2、USART3、UART5、UART7、USART10 |
| 7–9 | FDCAN1、FDCAN2、FDCAN3 |

例如 USART1 输出到文件，monitor 留在终端：

```bash
mkdir -p build/runtime
build/qemu/qemu-system-arm -machine dm-mc02 -kernel "$DM_MC02_ELF" \
  -nodefaults -display none -monitor stdio \
  -serial none -serial file:build/runtime/usart1.log
```

FDCAN 是固定 84 字节帧，不是文本串口或直接的 SocketCAN socket。
wire 布局、时间 owner、背压和复位规则见 [INTERFACES.md](INTERFACES.md)。

### Worker

先验证仓库自带连接 fixture：

```bash
bash tools/run-qemu-worker-smoke.sh
bash tools/run-qemu-v2-motor-smoke.sh
bash tools/run-worker.sh --help
```

手工连接时，把 QEMU 的第一个 `-serial none` 替换成
`-chardev socket,id=cosim,path=/tmp/dm-mc02-cosim.sock,server=on,wait=off -serial chardev:cosim`，
然后另一个终端运行：

```bash
bash tools/run-worker.sh --cosim /tmp/dm-mc02-cosim.sock
```

此命令只接 co-sim 控制/采样通道；电机总线还需 FDCAN socket 与 `--fdcan`。
完整 wiring 可参照 `tools/run-qemu-worker-smoke.sh`，仅控制通道连通不代表机构闭环。

- MuJoCo：`uv sync --locked --group dev --extra mujoco`，再执行
  `bash tools/run-mujoco-worker-smoke.sh`；实际模型、初始条件仍需按接口准备。
- ROS 2：安装并 source 系统环境（验收宿主为 Jazzy），运行
  `bash tools/run-ros2-worker-smoke.sh`。`--engine ros2` 使用系统 ROS Python。
  默认 topics 是 `/dm_mc02/imu`、`/dm_mc02/joint_states`、`/dm_mc02/motor_cmd`。
  adapter smoke 不等于 Gazebo world/model 联调，后者尚未验证。
- SocketCAN：worker 的 `--socketcan can0` 配合 `--fdcan` 连接已有 Linux CAN 接口，
  不模拟电气位时序、真实 ACK 或物理错误。
- 自定义 plant：`--backend package:create_backend` 或
  `--backend-registry package:register --engine name`。factory 的方法与拒绝契约见
  接口文档；plant 不应重复实现 wire 解析与 DM-MIT 映射。

## 测试与性能

### 权威工程门

```bash
PYTHON=/usr/bin/python3 python3 tools/dm_mc02_test_gate.py --jobs 4
# 仅在源码与构建产物已同步时：
python3 tools/dm_mc02_test_gate.py --no-build
```

按 Meson、原生 Host CTest、完整 pytest、shell smoke 四个集合验证。
报告保存到 `build/test-results/qemu-gate/<run>/summary.json`，含动态分母、日志、退出码
及测试前后二进制 SHA-256；可用 `--report-dir /absolute/path` 修改目录。
退出码 0/1/2/78 分别为 PASS/FAIL/参数错误/仅 BLOCKED。
只有命名的 ROS2、MuJoCo 可选后端允许 SKIP，跳过不代表验收通过。

2026-10-01 NOR 收敛后的结果为 Meson 66/66、Host 51/51、pytest 319/319、smoke 94/94。
发布工具后续回归见 [PLAN.md](PLAN.md)；计数不是覆盖率或项目完成率。

### 固件调度和实时性能

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
bash tools/collect-firmware-rtf.sh --warmup 0 --virtual-seconds 2 --ready-tick 250
bash tools/run-release-rtf-gate.sh
```

采样针对已记录的 `trobot` FreeRTOS tick/watchdog 观测接口，不是任意 ELF 的业务
健康检查。两秒命令用于诊断；正式门从 reset 启动，执行三次各 60 虚拟秒采样。
目标 `1.0x`，允许已记录的 `0.999x` QMP/墙钟抖动容差，打印精确 RTF、启动延迟、
CPU、RSS 和 watchdog。默认 startup 总期限 10 秒，超时/复位/断连必须失败退出。

2026-09-29 独立重建版本三轮 RTF 为 `0.999993x / 0.999961x / 0.999999x`，见
[冻结交付记录](reports/2026-09-29-engineering-release/README.md)。该证据限定默认板卡、
指定 ELF、无外部 worker，不代表新宿主、其它固件、无节流吞吐或 plant pacing。
tick 推进不等于完整业务 ready。

## 更新与源码包

日常更新：

```bash
git pull --ff-only
git submodule update --init --depth 1 qemu/upstream
```

子模块通常是 detached HEAD；修改前在其中创建工作分支。先提交 QEMU 模型/测试，
再同步外层 gitlink 与 `qemu.lock` 的 `fork_commit`。不要用 `update --remote` 替代固定
版本，也不要把官方 tag 当成本项目 fork。上游基线为 QEMU v8.2.2；升级需审查差异，
通过下层、消费者和完整工程门。

需要包含已下载 wraps 的可校验源码包时：

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
python3 tools/dm_mc02_source_package.py create build/source-packages/source.tar.gz
python3 tools/dm_mc02_source_package.py verify build/source-packages/source.tar.gz
python3 tools/dm_mc02_source_package.py restore \
  build/source-packages/source.tar.gz /absolute/path/to/new-restore
```

恢复目录必须不存在，源码在其 `project/` 下；按本文重新安装依赖、构建与测试。
包不含 `.git`、ROM 工作树、二进制或固件，ELF 只记录哈希。应先完成首次构建获取 wraps。
如果按快速开始使用了浅子模块，打包前还需执行
`git -C qemu/upstream fetch --unshallow origin dm-mc02/v8.2.2`，
使 packager 能检查项目提交相对 v8.2.2 基线的差异；普通构建和运行不需要完整历史。
默认拒绝未提交 QEMU 修改；开发快照可显式 `create --allow-worktree`，manifest 保存
真实差异和逐文件哈希。verify 证明内容完整，不代替独立构建与运行测试。

## 常见问题

| 现象 | 处理 |
|---|---|
| `unsupported machine type dm-mc02` | 使用 `build/qemu/qemu-system-arm`，不是系统 QEMU |
| 缺少 `qemu/upstream/configure` | 初始化固定子模块；main ZIP 不含其源码 |
| Meson/Ninja/venv 缺失 | 从根目录执行 `uv sync --locked --group dev` |
| guest 编译失败 | 检查 `arm-none-eabi-gcc` 和 `arm-none-eabi-objcopy` |
| 无串口文字 | 槽位 0 是 co-sim；核对 UART 顺序和固件配置 |
| socket 连接失败 | 检查服务端、短路径、路径冲突和 `/tmp` 权限 |
| RTF 失败 | 核对 ELF 哈希、RESET/watchdog、startup deadline、Release 配置及宿主负载 |
| ROS2/MuJoCo SKIP | 安装对应依赖；ROS2 还需 source 系统环境 |
| Flash 未保存 | monitor `quit` 正常退出；检查尺寸、路径与 stderr |

可加 `-d unimp,guest_errors -D build/runtime/qemu.log` 诊断未知寄存器/命令，先创建目录。
先定位第一处错误状态，不用固件特判或上层脚本掩盖尚未理解的下层缺陷。

## 目录与文档

| 路径 | 内容 |
|---|---|
| `qemu/upstream/` | QEMU fork、H723 外设、DM-MC02 machine、NOR、qtest/unit |
| `cosim/` | 板卡无关 C 模型、协议与 adapter |
| `tools/` | 构建、worker、QMP、性能采样、源码包与统一门禁 |
| `tests/` | Host C、Python 和 smoke guest fixtures |
| `reports/`、`docs/history/` | 冻结验收与历史审查；旧结果不代表当前版本 |
| [CAPABILITIES.md](CAPABILITIES.md) | 唯一当前能力矩阵 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 分层、复用与真实性裁决 |
| [INTERFACES.md](INTERFACES.md) | 公共接口、时间与错误语义 |
| [PLAN.md](PLAN.md)、[REVIEW.md](REVIEW.md) | 当前进度、下一步与残余风险 |
| [AGENTS.md](AGENTS.md) | 逐层修改和验收约束 |

依赖方向为 `STM32H723 → DM-MC02 → 器件/driver → external plant → tooling`。
每次只推进一个边界，先隔离测试，再直接消费者测试，最后工程门。
QEMU 许可证见 [fork 的 COPYING](https://github.com/lym12321/qemu-dm-mc02/blob/dm-mc02/v8.2.2/COPYING) 与源码文件声明；
第三方代码保留各自版权和许可证，本仓库不重新许可这些依赖，也不包含固件授权。
