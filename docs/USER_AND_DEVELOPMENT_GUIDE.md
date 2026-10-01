# DM-MC02 QEMU 用户与开发手册

本手册说明如何获取、构建、运行和验证项目，以及如何按现有分层继续开发。README 是项目入口；当前功能状态和证据只维护在 [`CAPABILITIES.md`](../CAPABILITIES.md)，本手册中的命令示例不构成额外的能力声明。

## Contents

- [项目范围](#project-scope)
- [环境准备](#environment)
- [获取源码](#get-the-source)
- [构建](#build)
- [首次验证](#first-validation)
- [运行固件](#run-firmware)
- [Flash 镜像](#flash-images)
- [串口、QMP 与 GDB](#serial-qmp-and-gdb)
- [外部仿真 worker](#external-simulation-worker)
- [测试与性能](#testing-and-performance)
- [更新与源码包](#update-and-source-package)
- [开发流程](#development-workflow)
- [故障排查](#troubleshooting)
- [目录与权威文档](#repository-map)

## Project scope

项目基于固定的 QEMU v8.2.2 fork，提供 STM32H723 与 DM-MC02 的分层仿真模型。依赖链为 `STM32H723 → DM-MC02 → 器件/驱动 → 外部 plant → 工具`；通用行为留在可复用层，板级组合由 DM-MC02 profile 负责。

本项目不依赖 Renode。`trobot/` 是只读外部固件输入，不随仓库发布，也不应为绕过仿真缺陷而修改。芯片完整性、物理总线、闭环和迁移等边界请查阅[能力矩阵](../CAPABILITIES.md)，不要从单个 smoke 或组件测试推断未覆盖能力。

## Environment

已验证宿主为 **Ubuntu 24.04 x86_64**，要求 Python 3.11 或更新版本。Windows 和 macOS 原生构建不在当前验收范围。

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

QEMU `configure` 会在 `build/qemu/pyvenv` 创建自己的 Python 环境，并从 QEMU 源码携带的 wheel 安装锁定 Meson 1.2.3。干净 Ubuntu 上 `python3-venv` 提供 venv/ensurepip bootstrap，系统已有 pip 与 setuptools 时 QEMU 可以跳过 ensurepip。Meson 不从 apt 或项目 Python 环境安装。项目级 Meson 命令通过 `tools/meson` 调用这份 QEMU 锁定版本，避免为同一个 build tree 再装一份 Meson。

因此，**只构建并运行 QEMU 不需要 `uv`**。`uv` 管理的是项目 Python 测试环境和可选 worker 依赖；当前 dev group 只安装 pytest，不重复安装 Meson 或 Ninja。需要运行完整回归门禁时，额外安装：

```bash
sudo apt-get install -y \
  cmake \
  ripgrep \
  gcc-arm-none-eabi \
  binutils-arm-none-eabi
```

CMake/CTest 构建 Host 测试，ARM GCC/binutils 构建测试 guest，ripgrep 是完整门禁检查项使用的命令。然后按 [uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装 `uv`，在仓库根目录创建锁定的 Python 测试环境：

```bash
uv sync --locked --group dev --python /usr/bin/python3
```

当前 profile 使用 `--enable-fdt=internal`、`--disable-capstone`、`--disable-slirp` 和 `--disable-docs`，因此不需要系统 libfdt、Capstone、slirp 或 Sphinx。Pixman 是可选依赖：没有 `libpixman-1-dev` 时，QEMU 会关闭依赖 pixman 的通用显示设备；DM-MC02 的默认 headless 使用不依赖它。如需这些通用显示设备，可额外安装：

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
PYTHON=/usr/bin/python3 bash tools/build-qemu.sh
```

运行完整工程测试还需要上节的 CMake、ARM 工具链、ripgrep 和 `uv` 环境：

```bash
uv sync --locked --group dev --python /usr/bin/python3
cmake -S . -B build/host -DCMAKE_BUILD_TYPE=Release
cmake --build build/host --parallel 4
```

构建产物：

| 路径 | 用途 |
| --- | --- |
| `build/qemu/qemu-system-arm` | 带 `dm-mc02` machine 的 QEMU |
| `build/host/` | Host 协议与桥接测试程序 |
| `build/qemu/pyvenv/` | QEMU configure 从源码 wheel 创建的锁定 Meson 环境 |
| `.venv/` | 由 `uv` 管理的 pytest 和项目 Python 工具环境 |

QEMU 默认构建为 Release、`arm-softmmu` 和 DM-MC02 设备 profile。调试构建可运行 `QEMU_BUILD_TYPE=debugoptimized bash tools/build-qemu.sh`；性能测量前应重建 Release。`tools/build-qemu-generic.sh` 用于检查通用 ARM 复用边界，不是运行 DM-MC02 固件的入口。

## First validation

不需要外部业务固件即可检查 machine 注册。后面三个 guest smoke 还需要完整测试依赖中的 ARM 工具链：

```bash
build/qemu/qemu-system-arm -machine help
bash tools/run-mc02-smoke.sh
bash tools/run-ospi-smoke.sh
bash tools/run-flash-smoke.sh
```

smoke 脚本会编译并运行仓库中的最小 guest。修改 QEMU 模型后先重新构建。`tools/run-qemu.sh --smoke` 只探测通用空 machine，不会启动业务固件。

## Run firmware

输入 ELF 必须面向 DM-MC02 地址空间。先记录用于复现的文件身份，再从项目 QEMU 启动：

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
sha256sum "$DM_MC02_ELF"
build/qemu/qemu-system-arm -machine dm-mc02 \
  -kernel "$DM_MC02_ELF" -nodefaults -display none \
  -serial none -monitor stdio
```

终端进入 QEMU monitor 后，可使用 `info status`、`stop`、`cont`、`system_reset` 和 `quit`。`quit` 会走正常关闭路径。没有 UART 后端时不会自动显示串口内容；`-serial none` 将默认第一个串口槽位留给二进制 co-sim，避免协议数据污染终端。

查看 machine 属性：

```bash
build/qemu/qemu-system-arm -machine dm-mc02,help
```

固件调试可增加 `-S -gdb tcp:127.0.0.1:1234`，再从 ARM GDB 连接：

```gdb
target remote localhost:1234
```

调试暂停、trace 和外部 worker 都会改变运行场景，不属于默认 Release 性能基准。

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

外部 NOR 是项目独立的 `dm-w25q64`，通过 QEMU QOM/SSI 和共享存储核心接入；官方 `m25p80.c` 与 `flash.h` 保持上游内容。当前命令执行是同步模型，quad 字节映射属于近似；保护、真实忙时序及器件迁移等限制以[能力矩阵](../CAPABILITIES.md)为准。

## Serial, QMP and GDB

### UART and FDCAN slots

重复的 `-serial` 参数按顺序接入 machine 槽位：

| 槽位（从 0 开始） | 连接对象 |
| ---: | --- |
| 0 | 二进制 co-sim 控制/采样通道 |
| 1–6 | USART1、USART2、USART3、UART5、UART7、USART10 |
| 7–9 | FDCAN1、FDCAN2、FDCAN3 |

例如将 USART1 输出到文件，monitor 留在终端：

```bash
mkdir -p build/runtime
build/qemu/qemu-system-arm -machine dm-mc02 -kernel "$DM_MC02_ELF" \
  -nodefaults -display none -monitor stdio \
  -serial none -serial file:build/runtime/usart1.log
```

FDCAN 使用固定 84 字节帧，经项目 chardev/socket 连接；它不是文本串口，也不是直接的 SocketCAN 设备。帧布局、时间戳 owner、排队与背压规则见 [`INTERFACES.md`](../INTERFACES.md)。

### QMP

自动化控制可为每个进程创建独立、较短的 Unix socket 路径：

```bash
build/qemu/qemu-system-arm -machine dm-mc02 -kernel "$DM_MC02_ELF" \
  -nodefaults -display none -serial none \
  -qmp unix:/tmp/dm-mc02-qmp.sock,server=on,wait=off
```

项目 Python 工具通过 `tools/dm_mc02_qmp.py` 复用固定版本的 QEMU Python client。不要另开第二个 reader 读取同一 QMP 连接。深层临时目录可能超过 `sockaddr_un` 路径限制；Linux smoke 应使用脚本管理的 `/tmp/dm-qemu.*.XXXXXX` 私有临时目录。

## External simulation worker

先运行仓库提供的 co-sim 和 motor fixtures，再连接自己的 plant：

```bash
bash tools/run-qemu-worker-smoke.sh
bash tools/run-qemu-v2-motor-smoke.sh
bash tools/run-worker.sh --help
```

手动启动时，在 QEMU 的第一个 serial 槽位挂接 co-sim Unix socket：

```bash
build/qemu/qemu-system-arm -machine dm-mc02 -kernel "$DM_MC02_ELF" \
  -nodefaults -display none -monitor stdio \
  -chardev socket,id=cosim,path=/tmp/dm-mc02-cosim.sock,server=on,wait=off \
  -serial chardev:cosim
```

另一个终端启动 worker：

```bash
bash tools/run-worker.sh --cosim /tmp/dm-mc02-cosim.sock
```

这只建立 co-sim 控制/采样通道。要交换电机总线数据，还需将 QEMU FDCAN chardev socket 配好并给 worker 传入 `--fdcan`；完整 wiring 示例见 `tools/run-qemu-worker-smoke.sh`。通道联通不等于机构闭环已验证。

worker 支持的常用选择：

| 场景 | 入口 |
| --- | --- |
| NullEngine 协议/连接 fixture | 默认 `--engine null`；仅用于工具与传输验证 |
| MuJoCo adapter | `uv sync --locked --group dev --extra mujoco` 后运行 `bash tools/run-mujoco-worker-smoke.sh` |
| ROS 2 adapter | 安装并 source 系统 ROS 环境（验收环境为 Jazzy），运行 `bash tools/run-ros2-worker-smoke.sh` |
| 已有 Linux CAN 接口 | `--socketcan can0 --fdcan /path/to/fdcan.sock` |
| 自定义 plant | `--backend package:create_backend` 或 `--backend-registry package:register --engine name` |

MuJoCo 示例只验证 adapter 和测试模型，不代表已完成 Release 固件闭环。ROS 2 默认 topic 为 `/dm_mc02/imu`、`/dm_mc02/joint_states` 和 `/dm_mc02/motor_cmd`；adapter smoke 不等同于 Gazebo world/model 联调。自定义 backend 的必需方法、参数校验、错误释放和时间语义见 [`INTERFACES.md`](../INTERFACES.md)。运行时完整参数以 `bash tools/run-worker.sh --help` 为准。

## Testing and performance

### Canonical test gate

```bash
PYTHON=/usr/bin/python3 python3 tools/dm_mc02_test_gate.py --jobs 4
```

统一门禁执行 QEMU/Meson 测试、原生 Host CTest、完整 pytest 和 shell smoke。它会创建 `build/test-results/qemu-gate/<run>/summary.json`，并保存各阶段日志、退出码、动态测试分母及测试前后的关键二进制 SHA-256。可通过 `--report-dir /absolute/path` 指定报告根目录。

| 退出码 | 含义 |
| ---: | --- |
| 0 | PASS |
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

已冻结的性能证据及其具体固件/宿主条件见 [`reports/2026-09-29-engineering-release/README.md`](../reports/2026-09-29-engineering-release/README.md)；条件不同的测量不能沿用其中结论。

## Update and source package

### Update a checkout

```bash
git pull --ff-only
git submodule update --init --depth 1 qemu/upstream
```

子模块通常处于 detached HEAD。修改 QEMU 前，在 `qemu/upstream` 中建立工作分支。先提交 QEMU fork 中的模型和测试，再更新外层仓库的子模块 gitlink 与 `qemu.lock` 中的 `fork_commit`，并核对两者指向同一提交。上游基线固定为 QEMU v8.2.2；升级版本需要重新审阅差异并通过相关下层、消费者和工程测试门。

### Create and restore a source package

首次构建获取所需 Meson wraps 后，可以创建可校验源码包：

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
python3 tools/dm_mc02_source_package.py create build/source-packages/source.tar.gz
python3 tools/dm_mc02_source_package.py verify build/source-packages/source.tar.gz
python3 tools/dm_mc02_source_package.py restore \
  build/source-packages/source.tar.gz /absolute/path/to/new-restore
```

默认创建要求 QEMU fork 源码干净。开发中的未提交改动只有在明确创建 worktree 快照时才可打包：

```bash
python3 tools/dm_mc02_source_package.py create \
  build/source-packages/source-worktree.tar.gz --allow-worktree
```

worktree manifest 记录嵌套 HEAD 基线、tracked diff、新增/删除路径和逐文件哈希。若子模块是浅克隆，创建包前先补齐 fork 历史：

```bash
git -C qemu/upstream fetch --unshallow origin dm-mc02/v8.2.2
```

源码包不含 `.git`、ROM 子模块工作树、构建二进制或固件；ELF 仅按哈希识别。restore 的目标目录必须不存在，源码落在其 `project/` 子目录。`verify` 证明包内容与 manifest 一致，不代表已独立构建或测试；完整验收要在恢复环境重新配置、构建并执行测试门。

## Development workflow

### Before changing code

1. 读取从工作区根目录继承的 `AGENTS.md` 和本项目的 [`AGENTS.md`](../AGENTS.md)，并检查 `git status`。保留无关工作区改动。
2. 根据 [`ARCHITECTURE.md`](../ARCHITECTURE.md) 确认代码所属层和依赖方向；根据 [`CAPABILITIES.md`](../CAPABILITIES.md)、[`INTERFACES.md`](../INTERFACES.md) 和 [`PLAN.md`](../PLAN.md) 确认公开契约、当前证据与下一道验证门。
3. 选择一个层或一个 producer/boundary/consumer 切片，记录最窄隔离测试、直接消费者测试和预期限制。不要先堆端到端 workaround。

### Make and verify a change

依赖顺序是 STM32H723 通用芯片层、DM-MC02 板级组合、可复用器件/驱动、外部 plant、用户工具。先在行为所属层修复首个错误状态，再验证直接消费者；端到端 smoke 不能取代这两道下层门。

QEMU 源码位于固定子模块。外层仓库记录 gitlink，不直接收纳第二份 QEMU 源码；嵌套改动提交后要同步 `qemu.lock` 的 fork 身份。Python QMP 工具复用 `tools/dm_mc02_qmp.py` 和 QEMU 自带 client；生产模型复用 QEMU QOM、IRQ、timer、SSI、NOR、USB/CAN 等既有设施，不能平行实现同一 production 行为。

按风险先跑最窄测试和直接消费者门，再运行完整工程门：

```bash
PYTHON=/usr/bin/python3 python3 tools/dm_mc02_test_gate.py --jobs 4
```

如果行为、支持状态、证据或限制发生变化，同步更新 `CAPABILITIES.md`。公共 wire 或时间语义变化时更新 `INTERFACES.md`；架构边界变化时更新 `ARCHITECTURE.md`；未完成事项和下一门写入 `PLAN.md`，残余审查问题写入 `REVIEW.md`。测试结果必须对应实际构建产物，不能把 fixture、组件 VMState 或历史报告升级成更高层能力声明。

### Project layer map

| 层 | 主要位置 | 职责 |
| --- | --- | --- |
| STM32H723 与 QEMU target | `qemu/upstream/target/arm/`、`qemu/upstream/hw/arm/` | 芯片、CPU、寄存器、时钟、IRQ 与通用外设 |
| DM-MC02 machine/profile | `qemu/upstream/hw/arm/dm_mc02*` | 外设实例、板级连线、电源与 pin map |
| 公共模型与接口 | `cosim/`、相关 QEMU 可复用模块 | 协议中立数据模型、存储与 adapter 边界 |
| 外部后端和工具 | `tools/` | worker、plant adapter、QMP、报告和验收工具 |
| 隔离与集成证据 | `tests/`、`qemu/upstream/tests/`、`reports/` | 单元/边界/smoke 测试和冻结结果 |

## Troubleshooting

| 现象 | 优先检查 |
| --- | --- |
| `unsupported machine type dm-mc02` | 启动 `build/qemu/qemu-system-arm`，不要使用系统 QEMU |
| 找不到 `qemu/upstream/configure` | 初始化固定 QEMU 子模块并确认其 gitlink 与 `qemu.lock` 一致 |
| QEMU 提示 Python venv/ensurepip 不可用 | 安装 `python3-venv`，并将 `PYTHON` 指向 Python 3.11+ |
| QEMU 提示找不到 Ninja | 安装 `ninja-build`，确认 `ninja` 在 `PATH` 中 |
| pytest 或项目 Python 环境不可用 | 安装 `uv` 后运行 `uv sync --locked --group dev --python /usr/bin/python3` |
| 测试 guest 编译失败 | 检查 `arm-none-eabi-gcc`、`arm-none-eabi-objcopy` 与 `binutils-arm-none-eabi` |
| 终端没有 UART 文本 | 核对 `-serial` 顺序；槽位 0 是二进制 co-sim |
| Unix socket 无法连接 | 确认服务端、路径权限和路径长度；每个进程使用独立短路径 |
| RTF 采样失败 | 核对 ELF 哈希、`xTickCount`、watchdog、Release 构建、启动期限和宿主负载 |
| ROS 2/MuJoCo smoke 被跳过 | 安装相应可选依赖；ROS 2 还需 source 系统环境 |
| Flash 镜像没有保存 | 正常退出 QEMU，检查镜像精确尺寸、路径和 stderr |

诊断 QEMU 未实现寄存器或 guest error 时，可创建输出目录后启用日志：

```bash
mkdir -p build/runtime
build/qemu/qemu-system-arm -machine dm-mc02 -kernel "$DM_MC02_ELF" \
  -nodefaults -display none -serial none -monitor stdio \
  -d unimp,guest_errors -D build/runtime/qemu.log
```

先定位首个错误状态，再判断 producer、边界、consumer 和测试预期是否一致；不要用固件特判或上层 fallback 隐藏尚未理解的下层问题。

## Repository map

| 路径 | 用途 |
| --- | --- |
| `qemu/upstream/` | 固定的 QEMU fork、芯片/板级模型和 QEMU 原生测试 |
| `cosim/` | 板卡无关的 C 模型、wire protocol 与 transport adapter |
| `tools/` | 构建、worker、QMP、RTF、源码包和测试 gate |
| `tests/` | Host C、Python 与协议测试 |
| `reports/`、`docs/history/` | 冻结验收记录与历史审查，不是当前支持矩阵 |

项目权威文档：

- [`CAPABILITIES.md`](../CAPABILITIES.md)：唯一当前能力与证据矩阵。
- [`ARCHITECTURE.md`](../ARCHITECTURE.md)：分层、复用和真实性裁决。
- [`INTERFACES.md`](../INTERFACES.md)：公共协议、时间与错误语义。
- [`PLAN.md`](../PLAN.md) 与 [`REVIEW.md`](../REVIEW.md)：进度、后续工作和残余风险。
- [`AGENTS.md`](../AGENTS.md)：项目级修改及验收约束。
