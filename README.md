# DM-MC02 QEMU

**基于 QEMU 的 STM32H723 / DM-MC02 固件仿真工程**

在 Linux 上构建并运行面向 DM-MC02 的 Cortex-M7 固件，验证已建模的芯片与板级行为，并通过确定性虚拟时间接口连接外部仿真后端。

| 快速入口 | 项目文档 |
| --- | --- |
| [用户与开发手册](docs/USER_AND_DEVELOPMENT_GUIDE.md) | [当前能力与证据](CAPABILITIES.md) |
| [快速构建](#快速开始) | [架构契约](ARCHITECTURE.md) |
| [测试方法](#验证) | [接口契约](INTERFACES.md) |
| [开发进度](PLAN.md) | [审查与残余风险](REVIEW.md) |

> [!IMPORTANT]
> 这是可构建、可运行并带回归门禁的工程版本，不是完整 STM32H723 数字孪生。支持范围、证据和限制以 [`CAPABILITIES.md`](CAPABILITIES.md) 为准。完整业务 ready、真实机构闭环和整机 snapshot/live migration 尚未验证或支持。

## 快速开始

以下命令适用于 Ubuntu 24.04 x86_64。QEMU 构建的最小系统依赖和完整测试依赖分开列在[手册的环境准备](docs/USER_AND_DEVELOPMENT_GUIDE.md#environment)。单独构建 QEMU 不需要 `uv`。

```bash
git clone --branch main https://github.com/lym12321/qemu-dm-mc02.git
cd qemu-dm-mc02
git submodule update --init --depth 1 qemu/upstream
PYTHON=/usr/bin/python3 bash tools/build-qemu.sh
```

构建后，项目专用 QEMU 位于 `build/qemu/qemu-system-arm`。系统自带的 QEMU 不含 `dm-mc02` machine。

## 验证

机器列表检查不需要业务固件。smoke 和完整门禁需要手册[环境准备](docs/USER_AND_DEVELOPMENT_GUIDE.md#environment)中列出的测试工具链与 `uv` 环境。

```bash
build/qemu/qemu-system-arm -machine help
bash tools/run-mc02-smoke.sh
uv sync --locked --group dev --python /usr/bin/python3
python3 tools/dm_mc02_test_gate.py --jobs 4
```

统一门禁覆盖 QEMU/Meson 测试、Host CTest、pytest 和 shell smoke，并生成带日志与二进制身份的报告。完整说明见[手册中的测试与性能章节](docs/USER_AND_DEVELOPMENT_GUIDE.md#testing-and-performance)。

## 运行固件

固件由用户单独提供；仓库不分发或修改 `trobot/` 中的固件。

```bash
export DM_MC02_ELF=/absolute/path/to/trobot.elf
build/qemu/qemu-system-arm -machine dm-mc02 \
  -kernel "$DM_MC02_ELF" -nodefaults -display none \
  -serial none -monitor stdio
```

串口映射、Flash 镜像、GDB/QMP 调试和外部 worker 的配置见[用户与开发手册](docs/USER_AND_DEVELOPMENT_GUIDE.md)。

## 项目结构

| 路径 | 内容 |
| --- | --- |
| `qemu/upstream/` | 固定版本的 QEMU fork、STM32H723/DM-MC02 模型与 QEMU 原生测试 |
| `cosim/` | 板卡无关的协议、数据模型与 transport adapter |
| `tools/` | 构建、QMP、worker、性能采样、源码包和统一测试门禁 |
| `tests/` | Host、Python 与协议测试 |
| `reports/`、`docs/history/` | 冻结验收证据和历史审查记录 |

依赖方向为 `STM32H723 → DM-MC02 → 器件/驱动 → 外部 plant → 工具`。项目不依赖 Renode；设计约束和当前未完成工作分别见 [`ARCHITECTURE.md`](ARCHITECTURE.md) 与 [`PLAN.md`](PLAN.md)。

## 开发与许可

修改前请阅读工作区和项目级 [`AGENTS.md`](AGENTS.md)，并按层推进：先验证器件或芯片边界，再接入直接消费者，最后运行相关工程门。完整工作流见[开发手册](docs/USER_AND_DEVELOPMENT_GUIDE.md#development-workflow)。

QEMU 许可证和源码声明见 [QEMU fork 的 COPYING 文件](https://github.com/lym12321/qemu-dm-mc02/blob/dm-mc02/v8.2.2/COPYING)。第三方代码保留其各自版权和许可证；本项目不重新许可这些依赖，也不包含外部固件授权。
