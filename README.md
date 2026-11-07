# DM-MC02 QEMU

**可组合的嵌入式固件协同仿真工具，当前真实机器为 STM32H723 / DM-MC02。**

用 `sim firmware app.elf` 直接运行固件，或用一份 `sim/v1` 场景和一个 `Simulation`
组合真实 ELF、机器、CAN/UART、虚拟设备与可选物理世界。
Python 核心可独立安装；原生固件运行需要固定的项目 QEMU。模型用于功能验证，时间和精度随观察来源明确标注，
不提供完整硅级、电气时序、整机 migration 或硬实时保证。

| 入口 | 用途 |
| --- | --- |
| [使用手册](docs/USAGE.md) | 安装、场景、实验、应用预设、原生调试和验证 |
| [真实单板 ELF](examples/minimal-board/) | SysTick、USART1 与按键 GPIO |
| [UART 外设连线](examples/uart-link/) | 字节流外设与真实固件因果响应 |
| [CAN 故障实验](examples/can-faults/host-only.yaml) | 无原生依赖的延迟、定向丢帧和恢复 |
| [共享世界与异构驱动器](examples/shared-world/) | 单一 MuJoCo World、M3508 和非 DJI joint driver |
| [HAL 教学](docs/USAGE.md#hal-teaching) | 固定 HAL/CMSIS 的独立工程 |

## 构建并运行真实固件

Linux x86_64 推荐使用固定 Nix 环境；[Ubuntu 依赖](docs/USAGE.md#ubuntu)也有说明。
先在本地构建项目 QEMU，并在同一环境安装 Python 3.11+ 命令行工具：

```bash
git clone --branch main https://github.com/lym12321/qemu-dm-mc02.git
cd qemu-dm-mc02
git submodule update --init --depth 1 qemu/upstream
nix develop
bash tools/build-qemu.sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

运行已有 ELF，只需 `sim firmware /path/to/app.elf`。也可用 ARM GCC/CMake 构建真实裸机示例：

```bash
cmake -S examples/minimal-board -B examples/minimal-board/build -DCMAKE_BUILD_TYPE=Debug
cmake --build examples/minimal-board/build
sim firmware examples/minimal-board/build/firmware.elf
```

默认 UART1 与原生 monitor 共用终端；`Ctrl-a c` 切换，`Ctrl-a h` 查看帮助。
在 monitor 输入 `quit` 退出。调试时从复位暂停并打开 `127.0.0.1:1234`：

```bash
sim firmware examples/minimal-board/build/firmware.elf --debug
```

另一个终端用同一 ELF 连接 GDB：

```bash
arm-none-eabi-gdb examples/minimal-board/build/firmware.elf
```

```gdb
target remote 127.0.0.1:1234
break firmware_main
continue
```

直接运行不需要 YAML、Python QMP client 或 MuJoCo。QEMU 选择顺序为 `--qemu`、
`QEMU_SYSTEM_ARM`、checkout 中的 `build/qemu/qemu-system-arm`。
系统 QEMU 不含本项目板卡；GitHub 源码 ZIP 不含子模块，请使用 Git clone。
当前提供本地 Nix 构建环境，未提供通用发行二进制。其它 UART、machine 属性、QMP 和外部输入
见[原生接口](docs/USAGE.md#native-debug)。

## 自动场景

自动场景继续使用 `sim run scene.yaml`。安装[固定 QMP client](docs/USAGE.md#原生后端与可安装-qmp-client)
后，可运行上面固件的有限实验：

```bash
sim validate examples/minimal-board/scene.yaml
sim run examples/minimal-board/scene.yaml --report build/minimal-board.json
```

安装、帮助和静态校验不要求 QEMU、MuJoCo 或 Nix。以下 Host 场景也不需要原生后端：

```bash
sim --help
sim validate examples/can-faults/host-only.yaml
sim run examples/can-faults/host-only.yaml --report build/host-only.json
```

这个例子只验证 Host CAN 交付，不运行 MCU。`mujoco`、`trace`、`referee` 是按需 extra。
有限实验、命名 ready、多板预设、故障输入和离线断言复核均使用同一[场景入口](docs/USAGE.md#simulation)。
原 Robot/Arena/Scenario schema 与独立执行器已被普通场景替代；
原生自由运行交互与独特的 Gazebo/ROS/MIT worker 保留明确边界，见手册。

## 验证

```bash
uv sync --locked --group dev
uv run pytest -q tests/test_topology.py tests/test_simulation.py tests/test_scene_cli.py
bash tools/run-simulation-smoke.sh --case all
bash tools/run-simulation-smoke.sh --case dual-board
```

原生 smoke 需要兼容 QEMU、ARM 工具链及对应可选依赖；缺依赖为 BLOCKED，未执行或 skip 不算 PASS。
轻量 PR gate 验证安装、纯 Python 合同和 checkout 外 CLI；固件与物理门单独运行。
完整测试、ELF/QEMU 身份、清理证据与性能测量见[验证手册](docs/USAGE.md#testing-and-performance)。
测试通过不等于实板校准，当前文档不宣称所有原生场景已通过。

## 目录与许可

`sim/api`、`runtime`、`config`、`fabric` 是通用核心；`machines`、`devices`、`physics` 是显式 adapter；
`apps`、`trace` 是可选消费者。`examples`、`smoke`、`tests` 承载可运行场景和分层证据。
`qemu/upstream` 是由 `qemu.lock` 固定的 QEMU v8.2.2 fork，原生器件和板卡模型独立测试。

QEMU 许可证见 [COPYING](https://github.com/lym12321/qemu-dm-mc02/blob/dm-mc02/v8.2.2/COPYING)。
第三方源码与模型保留各自来源、版权和许可证；本仓库不分发外部 `trobot` 固件。
