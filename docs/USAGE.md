# DM-MC02 QEMU 使用手册

本手册说明如何获取、构建、运行和验证项目，以及排查常见环境问题。命令均从仓库根目录执行；固件 ELF 由用户另行提供。

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
- [故障排查](#troubleshooting)
- [目录](#repository-map)

## Project scope

项目基于固定的 QEMU v8.2.2 fork，提供 STM32H723 与 DM-MC02 的数字外设模型，面向固件功能验证。

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
  binutils-arm-none-eabi
```

CMake/CTest 构建 Host 测试，ARM GCC/binutils 构建测试 guest，ripgrep 是完整门禁检查项使用的命令。然后按 [uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装 `uv`，在仓库根目录创建锁定的 Python 测试环境：

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

内部 Flash 编程支持 H723 HAL 使用的 256-bit Flash word：启用 `PG` 后，从 32 字节对齐地址连续写入八个 32-bit word，收齐并校验后提交。部分写入不会改变持久化内容；非连续、跨 word、其它写宽、force-write、未擦除 word 的重复编程和 0→1 编程会报错。`SR1` 只读，使用 `CCR1` 清除已建模 flags。编程完成同步执行；ECC、真实编程延迟、完整 `QW/WBNE/FW` 行为和掉电恢复未实现。由于没有 ECC 记录，已写入全 `0xff` 的 word 无法与擦除态区分。

外部 NOR 使用项目独立的 `dm-w25q64` 器件。命令同步完成，quad 使用字节级映射；保护、真实忙时序及器件迁移未支持。

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

FDCAN 使用固定 84 字节二进制帧，经项目 chardev/socket 连接；它不是文本串口，也不是直接的 SocketCAN 设备。使用仓库的 worker 处理该通道，接入 Linux CAN 时使用 worker 的 `--socketcan` 参数。

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

MuJoCo 示例只验证 adapter 和测试模型，不代表已完成 Release 固件闭环。ROS 2 默认 topic 为 `/dm_mc02/imu`、`/dm_mc02/joint_states` 和 `/dm_mc02/motor_cmd`；adapter smoke 不等同于 Gazebo world/model 联调。运行时完整参数以 `bash tools/run-worker.sh --help` 为准。

## Testing and performance

日常修改优先运行受影响的原生测试，例如内部 Flash：

```bash
tools/meson test -C build/qemu --print-errorlogs qtest-arm/dm-mc02-memory-test
```

该命令复用已有 qtest，不需要业务固件。修改模型后先构建 QEMU；完整回归用于发布和涉及多个入口的变更。

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
| 终端没有 UART 文本 | 核对 `-serial` 顺序；槽位 0 是二进制 co-sim |
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

诊断 QEMU 未实现寄存器或 guest error 时，可创建输出目录后启用日志：

```bash
mkdir -p build/runtime
build/qemu/qemu-system-arm -machine dm-mc02 -kernel "$DM_MC02_ELF" \
  -nodefaults -display none -serial none -monitor stdio \
  -d unimp,guest_errors -D build/runtime/qemu.log
```

## Repository map

| 路径 | 用途 |
| --- | --- |
| `qemu/upstream/` | 固定的 QEMU fork、芯片/板级模型和 QEMU 原生测试 |
| `cosim/` | 板卡无关的 C 模型、wire protocol 与 transport adapter |
| `tools/` | 构建、worker、QMP、RTF、源码包和测试 gate |
| `tests/` | Host C、Python 与协议测试 |
| `flake.nix`、`flake.lock` | Nix 环境与依赖锁定 |
| `build/` | 本地构建产物、测试报告和运行文件 |
