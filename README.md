# DM-MC02 QEMU

**可组合的嵌入式固件协同仿真工具，当前真实机器为 STM32H723 / DM-MC02。**

用 `sim firmware app.elf` 运行固件，或用一份 `sim/v1` 场景组合真实 ELF、机器、
CAN/UART、虚拟设备与原生物理世界。宿主 CLI、运行时和 API 使用 Rust；运行产品不需要 Python。
QEMU、MuJoCo、Gazebo 与 ROS 2 继续使用各自的原生实现。
模型用于功能验证，不提供完整硅级、电气时序、整机 migration 或硬实时保证。

| 入口 | 用途 |
| --- | --- |
| [使用手册](docs/USAGE.md) | 安装、场景、实验、Rust API、原生调试与验证 |
| [真实单板 ELF](examples/minimal-board/) | SysTick、USART1 与按键 GPIO |
| [UART 外设连线](examples/uart-link/) | 字节流外设与真实固件因果响应 |
| [CAN 故障实验](examples/can-faults/host-only.yaml) | 无原生依赖的延迟、定向丢帧和恢复 |
| [共享世界与异构驱动器](examples/shared-world/) | 单一 MuJoCo World、M3508 和独立 joint driver |
| [HAL 教学](docs/USAGE.md#hal-teaching) | 固定 HAL/CMSIS 的独立工程 |
| [Web Lab](docs/USAGE.md#web-lab) | `sim lab web` 本地网页实验台 |

## 下载并运行

[Native packages 工作流](https://github.com/lym12321/qemu-dm-mc02/actions/workflows/native-packages.yml)
提供以下产品；下载需要登录 GitHub，Artifacts 保留 30 天。

| Artifact | 平台与范围 |
| --- | --- |
| `dm-mc02-windows-x86_64` | Windows x86_64 ZIP，固件终端和原生 GDB 监听 |
| `dm-mc02-linux-firmware-x86_64` | Ubuntu 24.04+ x86_64 tar.gz，轻量固件入口 |
| `dm-mc02-linux-x86_64` | Ubuntu 24.04+ x86_64 tar.gz，场景、Lab/Web、worker、MuJoCo/Gazebo/ROS 2 |

Linux 下载后解开 Artifact 外层，再用 `tar -xzf` 解开其中的 tar.gz，保留执行权限和插件符号链接。
轻量 Linux 包需要系统 GLib/zlib；完整包保留宿主 glibc≥2.39、Linux pidfd 与 EGL/OpenGL 驱动边界。
完整运行时本地候选约 351.8 MiB，另附源码与许可证；不能将其与轻量固件包的体积混为一谈。

已通过的 Actions 运行 [42501982549](https://github.com/lym12321/qemu-dm-mc02/actions/runs/42501982549)
验证的是轻量固件产品。新的完整 Linux 打包工作流尚未执行；本地开发包已在隐藏 SDK、
checkout 外和空应用 PATH 条件下验证原生依赖、固件/GDB、场景及 MCAP 重放。
下载时以所选运行记录和随包报告为准。

Windows PowerShell：

```powershell
.\sim.exe firmware .\example.elf
.\sim.exe firmware .\example.elf --debug
```

Linux，在解压目录执行：

```bash
./sim firmware example.elf
./sim firmware example.elf --debug
```

将 `example.elf` 换为自己的 DM-MC02 ELF。默认 UART1 与原生 monitor 共用终端；
`Ctrl-a c` 切换，`Ctrl-a h` 查看帮助，在 monitor 输入 `quit` 退出。
`--debug` 从复位暂停并监听 `127.0.0.1:1234`；另装 ARM GDB 后连接：

```bash
arm-none-eabi-gdb example.elf
```

```gdb
target remote 127.0.0.1:1234
break firmware_main
continue
```

QEMU 选择顺序为 `--qemu`、`QEMU_SYSTEM_ARM`、发行目录 `bin/` 中的 QEMU。
系统 QEMU 不含本项目板卡；GitHub 源码 ZIP 不含子模块，请使用 Git clone。
其它 UART、machine 属性、QMP 和外部输入见[原生接口](docs/USAGE.md#native-debug)。

## 源码构建与场景

使用固定工具链和 Cargo.lock，在 Linux 或 WSL2 中构建宿主功能：

```bash
git clone --branch refactor/composable-platform https://github.com/lym12321/qemu-dm-mc02.git
cd qemu-dm-mc02
git submodule update --init --depth 1 qemu/upstream
cargo build --release --locked -p sim-host --features host-runtime
export PATH="$PWD/target/release:$PATH"
sim validate examples/can-faults/host-only.yaml
sim run examples/can-faults/host-only.yaml --report build/host-only.json
```

这个例子只验证 Host CAN 交付，不运行 MCU。真实固件需先按[环境准备](docs/USAGE.md#ubuntu)
安装构建工具，再构建固定 QEMU 和 ARM ELF：

```bash
bash tools/build-qemu.sh
export QEMU_SYSTEM_ARM="$PWD/build/qemu/qemu-system-arm"
cmake -S examples/minimal-board -B examples/minimal-board/build -DCMAKE_BUILD_TYPE=Debug
cmake --build examples/minimal-board/build
sim run examples/minimal-board/scene.yaml --report build/minimal-board.json
sim lab web --elf examples/minimal-board/build/firmware.elf
```

默认 Cargo 构建只提供轻量 `firmware`；`host-runtime` 提供不含原生物理 SDK 的宿主功能。
完整 Linux 构建用 `bash tools/build-linux-sdk.sh`、`bash tools/build-linux-runtime.sh`；
细分 Cargo features、原生 SDK 与 Rust API 见[安装](docs/USAGE.md#installation)。
原 Robot/Arena/Scenario schema 与独立执行器已由普通场景替代。

## 验证与目录

```bash
cargo test --locked --workspace --features sim-host/host-runtime
cargo clippy --locked --workspace --features sim-host/host-runtime --all-targets --no-deps -- -D warnings
cargo run --locked -p sim-host --features simulation --example host_can
```

Python 仅用于构建和独立测试驱动；其环境安装及原生门见[验证手册](docs/USAGE.md#testing-and-performance)。
Host gate 不运行 MCU 或物理引擎。历史 Robot 2s 场景在原 10s host 预算下仍 FAIL；
原生、浏览器及固件消费者的证据分别记录，测试通过不等于实板校准。

`crates/sim-core` 拥有通用生命周期、时间和调度合同；`crates/sim-host` 提供配置、互连、
机器/设备/物理 adapter、实验、Lab 和 CLI。`cosim` 保留唯一 C wire codec，
`sim/physics/gazebo_native` 保留原生 C++ Gazebo owner；`tools`、`smoke`、`tests` 承载构建与测试。
`qemu/upstream` 是由 `qemu.lock` 固定的 QEMU v8.2.2 fork。

QEMU 许可证见 [COPYING](https://github.com/lym12321/qemu-dm-mc02/blob/dm-mc02/v8.2.2/COPYING)。
第三方源码与模型保留各自来源、版权和许可证；本仓库不分发外部 `trobot` 固件。
