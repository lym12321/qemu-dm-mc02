# QEMU-01：ADC runtime-sync 测试链接依赖

日期：2026-09-10。状态：完成。关联 review：R-01。

## 变更与边界

仅在 `qemu/upstream/tests/unit/meson.build` 的 `test-dm-adc-runtime-sync`
source set 加入已有 `hw/arm/dm_mc02_dma_endpoint.c`。ADC core 为 producer，
Meson source set 为构建边界，既有 timer/IRQ/restore 测试为 consumer。
此前的 undefined reference 记录保存在
[原始失败日志](../2026-09-10-review/dm-review-qemu-tests.log)。

没有新增 helper、stub 或生产行为。两级 PLAN 已按 QEMU 专属主线整理；REVIEW
记录 R-01 关闭，审查报告保留历史快照并指向本次交付。

## 验证

全部命令在 `dm-mc02-qemu/` 下运行，退出码均为 0。

| 门 | 结果 | 日志 |
| --- | --- | --- |
| ADC runtime-sync 隔离 | 1 目标、3 子用例通过 | [adc-runtime-sync.log](adc-runtime-sync.log) |
| ADC 直接 qtest | 1 目标、75 子用例通过 | [adc-qtest.log](adc-qtest.log) |
| 完整 Meson 项目集合 | 65/65，54 unit + 11 qtest，无排除 | [meson-project.log](meson-project.log) |
| Host 后最终二进制复核 | 65/65，使用 --no-rebuild 保持身份 | [meson-final-binary.log](meson-final-binary.log) |
| Host 构建 | 通过 | [host-build.log](host-build.log) |
| Host CTest | 54/54，67.97 s | [host-ctest.log](host-ctest.log) |
| 内含 QEMU smoke | 92/92；MuJoCo、ROS2 实际 PASS | [host-detail.log](host-detail.log) |
| 完整 pytest | 256 passed，1 个上游 deprecation warning | [pytest.log](pytest.log) |

```bash
tools/meson test -C build/qemu --num-processes 1 --print-errorlogs test-dm-adc-runtime-sync
tools/meson test -C build/qemu --num-processes 1 --print-errorlogs qtest-arm/dm-mc02-adc-test
tools/meson test -C build/qemu --num-processes 1 --print-errorlogs \
  'test-dm-*' 'qtest-arm/dm-mc02-*' 'qtest-arm/stm32h723-usb-host-test'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONDONTWRITEBYTECODE=1 \
  .venv/bin/python -m pytest -q -p no:cacheprovider
cmake --build build/host -j4
ctest --test-dir build/host --output-on-failure
tools/meson test -C build/qemu --no-rebuild --num-processes 1 --print-errorlogs \
  'test-dm-*' 'qtest-arm/dm-mc02-*' 'qtest-arm/stm32h723-usb-host-test'
git -C qemu/upstream diff --check -- tests/unit/meson.build
```

Host smoke 前的源码、目标定义、构建选项、QEMU 二进制与固件 SHA-256 见
[inputs.txt](inputs.txt)，smoke 后的身份见 [inputs-after-host.txt](inputs-after-host.txt)。
上游 HEAD 是 `11aa0b1ff115b86160c4d37e7c37e6a6b13b77ea`；工作树有既有本地修改，
该 HEAD 本身不等于完整项目源码身份，也不是 clean rebuild 证明。

身份复核发现现有 `run-cosim-link-smoke.sh:21` 在套件中途调用 `build-qemu.sh`。
该脚本使用 venv Ninja 1.13.0，当前 Meson 测试解析到系统 Ninja 1.11.1；
Host 详细日志中有 `build log version is too old` 警告。Host 后 binary hash 改变，
已记录的源码、构建选项和 ELF hash 不变。因此不声称全部 smoke 使用同一二进制；
QEMU-02 将统一构建 owner/Ninja 来源并消除中途重建。
对 Host 后最终二进制使用 `--no-rebuild` 复跑完整 Meson 集合，65/65 通过；
复跑后再次核对后置身份清单。

## 限制与交接

- 本切片仅恢复构建依赖；既有三项 runtime-sync 测试分别覆盖 paused regular rank、
  injected rank、calibration 的运行态重建。直接 qtest 和完整集合另行通过。
- 不引入公共接口或新验证方法，无需修改 AGENTS/ARCHITECTURE/INTERFACES 契约。
- 没有修改或运行 Renode，没有修改固件，没有子代理。
- 没有重跑三轮性能门；不证明整机 migration、硅级时序或真实 plant 闭环。
- 下一步：QEMU-02，先核对现有 runner 清单及 smoke 状态传播，再实现薄聚合入口。
  其余审查问题继续在 PLAN/REVIEW 跟踪。
