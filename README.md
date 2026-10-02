# DM-MC02 QEMU

**基于 QEMU 的 STM32H723 / DM-MC02 固件仿真工程**

在 Linux 上构建并运行面向 DM-MC02 的 Cortex-M7 固件，验证已建模的芯片与板级行为，并通过确定性虚拟时间接口连接外部仿真后端。

| 快速入口 | 项目文档 |
| --- | --- |
| [快速构建](#快速开始) | [详细使用手册](docs/USAGE.md) |
| [运行固件](#运行固件) | [串口与调试](docs/USAGE.md#serial-qmp-and-gdb) |
| [验证](#验证) | [故障排查](docs/USAGE.md#troubleshooting) |

> [!IMPORTANT]
> 项目面向固件功能验证，采用数字外设模型和虚拟时间。它不提供完整 STM32H723 硅级时序、模拟电气行为或整机 snapshot/live migration。真实机构闭环和完整业务就绪需要针对具体固件与外部后端验证。

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

门禁覆盖 QEMU/Meson 测试、Host CTest、pytest 和 shell smoke，并在本地生成日志与报告。完整说明见[测试与性能](docs/USAGE.md#testing-and-performance)。

## 运行固件

固件由用户单独提供；仓库不分发或修改 `trobot/` 中的固件。

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
build/qemu/qemu-system-arm -machine dm-mc02 \
  -kernel "$DM_MC02_ELF" -nodefaults -display none \
  -serial none -monitor stdio
```

在前面进入的 Nix shell 中执行上述命令。串口映射、Flash 镜像、GDB/QMP 调试和外部 worker 的配置见[使用手册](docs/USAGE.md)。

## 项目结构

| 路径 | 内容 |
| --- | --- |
| `qemu/upstream/` | 固定版本的 QEMU fork、STM32H723/DM-MC02 模型与 QEMU 原生测试 |
| `cosim/` | 板卡无关的协议、数据模型与 transport adapter |
| `tools/` | 构建、QMP、worker、性能采样、源码包和统一测试门禁 |
| `tests/` | Host、Python 与协议测试 |
| `flake.nix`、`flake.lock` | 固定的 Nix 构建与测试环境 |
| `docs/USAGE.md` | 环境、运行、验证和故障排查 |

项目基于固定 QEMU v8.2.2 fork，不依赖 Renode。QEMU 源码由子模块提供，GitHub 的源码 ZIP 不包含该子模块，请使用 Git clone。

## 许可

QEMU 许可证和源码声明见 [QEMU fork 的 COPYING 文件](https://github.com/lym12321/qemu-dm-mc02/blob/dm-mc02/v8.2.2/COPYING)。第三方代码保留其各自版权和许可证；本项目不重新许可这些依赖，也不包含外部固件授权。
