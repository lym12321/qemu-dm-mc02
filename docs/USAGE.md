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

MuJoCo worker smoke 验证 adapter 和测试模型；下文的 MotorTest 另验证实际固件电机组件的数据边界。普通 Release 的固定单转子业务使用上面的 [business fixture](#trobot-release-business-fixture) 入口。ROS 2 默认 topic 为 `/dm_mc02/imu`、`/dm_mc02/joint_states` 和 `/dm_mc02/motor_cmd`；adapter smoke 不等同于 Gazebo world/model 联调。运行时完整参数以 `bash tools/run-worker.sh --help` 为准。

BMI088 的完整 raw burst/FIFO data 消费通知携带实际样本身份；默认字节、部分读取或跨样本混合不产生完整消费确认。raw 目前逐字节读取寄存器，没有已验证的硬件 shadow snapshot。`--wait-step-done` 用于 BMI088 消费门：输入已接受后，器件未接收该样本或未读来源被覆盖/器件复位，会返回失败 DONE（status=2），并报告真实已消费和缺失掩码。严格等待会立即报错，适用于传感器已经配置、每个样本都应被完整读取的场景；默认模式继续按 ACK 推进。来源按实际读取身份匹配，跨会话重用外部序号不会误确认；完成响应按 ACK 顺序发送，并保留最近结果供重放。此门不提供 CPU/电机全局锁步。SPI 独立组件的 v1 状态恢复会清除缺失的来源身份，SPI/BMI088 组合的旧 v1 快照拒绝；这些组件状态不代表整机迁移。

v2 worker 对每个 STEP 的输入准入最多等待 2 秒。没有 ACK 时，每次等待 0.25 秒，初次发送后最多重试三次；收到 `QUEUE_FULL` 后按 1 毫秒间隔重试，仍受同一个 2 秒期限约束。重试沿用原始输入，不重复推进 plant；准入发送尚未完成时先完成原发送。等待 ACK、DONE 或队列恢复时，CAN/SocketCAN 仍可双向转发。`--wait-step-done` 另有每次 2 秒、最多四次的完成等待；超时重试仍计入次数。重放尚未发送完时，先继续原发送；发送队列空才重放同一 STEP，不重复推进 plant。完成等待的时间另计。准入超时报 `STEP_ACK admission timed out`，附带 STEP 编号、时间、最后状态、队列深度和静默重试次数，便于定位未回应或队列持续占满。

worker 每次从 co-sim/FDCAN stream 最多读取 4096 字节，返回完整帧并保留未完成帧；v2 暂存的已解码回复先处理，再读取新输入。因此持续可读输入期间仍有机会检查等待期限并服务其它就绪方向。此策略不保证各方向吞吐相同或固定的主机延迟；自定义 backend 的阻塞调用仍需自行限时。

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

每个主机 step 先交付起点命令，再推进器件，最后交付目标时刻反馈；区间内才到期的
命令在目标时刻交付，并从下一区间生效。ACK/DONE 等待只收发和入队，不推进器件。
RESET 保留 transport 的 partial bytes，并更新 QEMU/worker 时间原点。CAN wire 没有
session 标识，因此不提供跨 reset 的 CAN 历史输入隔离。这个调度不提供位级仲裁、
真实电气总线、CPU 全局锁步或实物电机标定。

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
