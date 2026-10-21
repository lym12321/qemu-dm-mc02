# DM-MC02 QEMU

**可组合的嵌入式固件仿真工具，首个机器后端为 QEMU STM32H723 / DM-MC02**

通过一个 `Simulation` 组合真实 ELF、机器、CAN 总线和外部设备。Python 核心可独立安装；运行原生固件需要本仓库的 QEMU 构建。当前支持切点采样执行，不声称跨板事件级锁步。

| 快速入口 | 项目文档 |
| --- | --- |
| [Simulation 入门](#simulation) | [API、场景与时间语义](docs/USAGE.md#simulation) |
| [快速构建](#快速开始) | [详细使用手册](docs/USAGE.md) |
| [运行固件](#运行固件) | [串口与调试](docs/USAGE.md#serial-qmp-and-gdb) |
| [验证](#验证) | [故障排查](docs/USAGE.md#troubleshooting) |
| [Virtual Lab：YAML 与交互命令](docs/USAGE.md#virtual-lab) | [GPIO/UART/CAN/ADC 示例](examples/labs/board-io.yaml) |
| [HAL 教学：按键与 GPIO](docs/USAGE.md#hal-teaching) | [独立固件工程](examples/teaching/) |
| [未迁移 Robot：固件、四轮模型与上位机](docs/USAGE.md#robot-manifest) | `uv run --extra mujoco tools/sim-unmigrated robot run robot.yaml` |

> [!IMPORTANT]
> 项目面向固件功能验证，采用数字外设模型和虚拟时间。它不提供完整 STM32H723 硅级时序、模拟电气行为或整机 snapshot/live migration。真实机构闭环和完整业务就绪需要针对具体固件与外部后端验证。

<a id="simulation"></a>
## 安装与运行 Simulation

Python 3.11+，基础依赖只有 PyYAML。安装、帮助和场景校验无需 QEMU、MuJoCo 或 Nix：

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
sim --help
```

有 ARM GCC 和 CMake 后，构建一个独立裸机示例：

```bash
cmake -S examples/minimal-board -B examples/minimal-board/build -DCMAKE_BUILD_TYPE=Debug
cmake --build examples/minimal-board/build
sim validate examples/minimal-board/scene.yaml
sim run examples/minimal-board/scene.yaml --report build/minimal-board.json
```

最后一条命令需要下文构建的项目 QEMU。可用 `--qemu /path/to/qemu-system-arm` 或
`QEMU_SYSTEM_ARM` 指定二进制；它必须包含当前子模块的原生执行和 CAN 观测接口。
`examples/can-motor` 使用相同构建命令，把目录名替换即可；该例连接一台 M3508 的显式固定转子状态，
不需要四轮底盘或物理引擎。两个例子的真实 UART/GPIO、FDCAN 收发与超时验收：

```bash
bash tools/run-simulation-smoke.sh --case all
```

可选的[转子反馈闭环](docs/USAGE.md#rotor-loop)复用同一示例和现有刚体转子：
`bash tools/run-simulation-smoke.sh --case rotor-loop` 验证实际反馈触发后续零电流命令与滑行。
该动力学模型的参数未标定。

[使用手册](docs/USAGE.md#simulation)包含完整 YAML、Python API、交互命令与时间限制。
Lab、Robot、Arena、Scenario 和原有物理组合尚未迁移，保留在
`tools/sim-unmigrated` 以运行既有功能和回归；新 `sim` 入口只运行 `sim/v1`。

## 快速开始

推荐在 **Linux x86_64 的 Nix 环境**中构建。仓库的 `flake.lock` 固定编译器、GLib、zlib、Python 和构建/测试工具的来源；无需安装对应的 apt 开发包。先[安装 Nix 并启用 flakes](docs/USAGE.md#nix)，然后运行：

```bash
git clone --branch main https://github.com/lym12321/qemu-dm-mc02.git
cd qemu-dm-mc02
git submodule update --init --depth 1 qemu/upstream
nix develop --command bash tools/build-qemu.sh
```

构建后，项目专用 QEMU 位于 `build/qemu/qemu-system-arm`。系统自带的 QEMU 不含 `dm-mc02` machine。

进入 `nix develop` 后，编译器和开发库均来自 Nix；运行时也使用同一环境。已有系统构建缓存会自动重新配置。也可按手册的 [Ubuntu 构建方法](docs/USAGE.md#ubuntu)使用系统依赖。只构建 QEMU 无需运行 `uv sync`。

## 验证

先进入 Nix 环境。机器列表和最小 guest smoke 不需要业务固件：

```bash
nix develop
build/qemu/qemu-system-arm -machine help
bash tools/run-mc02-smoke.sh
```

完整门禁需要外部固件 ELF：

```bash
uv sync --locked --group dev --extra mujoco --python "$PYTHON"
export DM_MC02_ELF=/absolute/path/to/trobot.elf
python3 tools/dm_mc02_test_gate.py --jobs 4
```

门禁执行回归验证并在本地生成日志与报告。硬件语义以 ST 资料及实板观测为依据，测试通过不代表完整芯片准确性。完整说明见[测试与性能](docs/USAGE.md#testing-and-performance)。

工作区的 `trobot` 可编译独立 HAL/BSP 测试固件，也可验证实际电机组件与 MuJoCo 单转子的数据边界。构建、报告和明确未支持项见[固件测试](docs/USAGE.md#trobot-halbsp-firmware-test)。

## 运行固件

可构建仓库内的 [HAL 教学工程](docs/USAGE.md#hal-teaching)，或使用外部工程的 ELF；本仓库不分发 `trobot` 固件。

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
bash tools/run-qemu.sh
```

在前面进入的 Nix shell 中运行。也可直接传入 `--elf /absolute/path/to/firmware.elf`。默认将 USART1 与 QEMU monitor 接到终端；`Ctrl-a c` 切换两者，monitor 中输入 `quit` 正常关闭。

用 `bash tools/run-qemu.sh --debug` 从复位暂停，再用 ARM GDB 连接 `127.0.0.1:1234`。当前 `trobot` 需要静止 IMU 输入才能完成 INS 初始化；未经修改的 Release 固件启动复现见[手册](docs/USAGE.md#trobot-stationary-startup)。串口、Flash 镜像、GDB/QMP 和外部 worker 的详细配置也见手册。

已有普通 Release ELF 时，可用固定单转子 MuJoCo 场景自动验证 `0→6→0 rad/s` 速度控制并保存报告：

```bash
bash tools/run-trobot-business.sh
```

先同步 MuJoCo extra；默认 ELF、参数与报告说明见[业务仿真](docs/USAGE.md#trobot-release-business-fixture)。
同一入口加 `--performance` 可运行三轮持续 paced 性能门，详见手册。

## 项目结构

| 路径 | 内容 |
| --- | --- |
| `qemu/upstream/` | 固定版本的 QEMU fork、STM32H723/DM-MC02 模型与 QEMU 原生测试 |
| `cosim/` | 板卡无关的协议、数据模型与 transport adapter |
| `sim/api`、`sim/runtime`、`sim/config`、`sim/fabric` | 公开契约、唯一 Simulation、场景准入与 CAN 传输 |
| `sim/machines/qemu` | 原生进程、QMP、端点与机器 profile |
| `sim/devices`、`sim/physics` | 协议设备与独立物理后端；原有物理组合待迁移 |
| `examples/minimal-board`、`examples/can-motor` | 统一 Runtime 的真实固件示例 |
| `tools/` | 构建、QMP、worker、性能采样、源码包和统一测试门禁 |
| `tests/` | Host、Python 与协议测试 |
| `flake.nix`、`flake.lock` | 固定的 Nix 构建与测试环境 |
| `docs/USAGE.md` | 环境、运行、验证和故障排查 |

项目基于固定 QEMU v8.2.2 fork，不依赖 Renode。QEMU 源码由子模块提供，GitHub 的源码 ZIP 不包含该子模块，请使用 Git clone。

## 许可

QEMU 许可证和源码声明见 [QEMU fork 的 COPYING 文件](https://github.com/lym12321/qemu-dm-mc02/blob/dm-mc02/v8.2.2/COPYING)。第三方代码保留其各自版权和许可证；本项目不重新许可这些依赖，也不包含外部固件授权。
