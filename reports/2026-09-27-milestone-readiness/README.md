# 工程基线提交就绪审查

日期：2026-09-27。范围：交付记录和临时文件清理；本轮未修改生产代码、固件或公共接口。

## 已完成

- QEMU-01～04：修复 ADC 测试构建，统一四集合门禁，约束 RTF reset epoch、tick wrap、启动/QMP 超时及进程清理。
- QEMU-05～08：建立唯一能力矩阵；实现带文件哈希的源码打包/恢复；在 Ubuntu 24.04 x86_64 完成独立配置、构建与测试；建立三轮 Release 性能基线。
- QEMU-09～11：internal Flash 复用 exact-size persistence adapter；worker DM-MIT 映射统一到 DmMotorBusAdapter；backend 配置与方法集在 RESET/step 前校验并清理拒绝路径资源。
- QEMU-12：芯片 ADC 按分辨率计算 regular/injected deadline，修复 Release 16-bit rank 提前 5,333 ns 发布结果；ADC 隔离测试及直接 DMA consumer、TIM2 回归均通过。

## 当前验证证据

核对 `build/test-results/qemu-gate/20260927T133048.217419Z-31236/summary.json`：
Meson 65/65、Host CTest 51/51、pytest 318/318、shell smoke 94/94，overall PASS、exit 0；
ROS2/MuJoCo 实际通过，52 个二进制身份检查通过。集合分母不能相加当覆盖率。

本轮重新核对当前 QEMU SHA-256：
`be6e27b43605c5e9926dc9d54a55ae44a2ea8bc17fc86de293042830557db23f`。
三轮 Release 60 虚拟秒 RTF 为 0.999995x / 0.999981x / 0.999993x，
启动 0.524100–0.525158 s，CPU 113.6–113.8%，RSS 50036–50436 KiB，watchdog 错误均为零。
按 1.0x 目标及已记录的 0.999x 采样容差通过。原始性能日志 SHA-256：
`2f3fe64414f5303174ca901ca89d6585b8769dbe75113b0b3630b22a56a00604`。
逐轮详情见 [QEMU-12](../2026-09-26-qemu-12-observation/README.md)。
本轮只改记录和清理临时文件，未重复全量执行；外层及 nested QEMU `git diff --check` 通过。

## 清理

确认没有 pytest/QEMU 测试进程，并检查可读取的 `/proc/*/fd` 与 cwd 无引用后，删除：

- `/tmp/pytest-of-lab`：两轮项目测试临时目录。
- `/tmp/dm-mc02-{adc-dma,adc-jauto-dma,can-medium,spi2-dma,tim8-approx-dma,tim8-dbm,tim8-dma}.log`：七个空 smoke 日志。

共 73 个普通文件，281655 字节逻辑内容（pytest 目录清理前磁盘占用约 868 KiB）。
历史报告、成功/失败门禁日志、源码包、构建目录、旧 config.log、ROM 子模块改动和固件均保留。
根文件系统 124G，约 61G 可用，无需为了空间删除重建证据。无遗留子代理。

## 何时可以提交

现在可以进入“QEMU 工程基线”里程碑收尾，不必等待 QEMU-13 或完整产品能力。
当前尚不能把一次外层 commit 当作完整快照：`qemu/upstream/` 被外层忽略，
`qemu.lock` 仍指向 `73ac4b85cfcd16a31211e2158fcd9788b7d8c429`，
Flash/ADC 的五个源码和测试文件仍仅在 nested 工作树中。

提交顺序与验收条件：

1. 复核并单独提交 nested 的 `hw/arm/dm_mc02.c`、`hw/arm/dm_mc02_adc.c`、
   `tests/qtest/dm-mc02-memory-test.c`、`tests/qtest/dm-mc02-adc-test.c`、
   `tests/qtest/dm-mc02-tim2-test.c`。不要把现有 `roms/*` gitlink/脏目录混入此次提交。
2. 外层 `qemu.lock` 更新为该真实新 commit，纳入当前源码、能力矩阵和 QEMU-05～12 报告。
   工作区根不是 Git 仓库，根 PLAN/AGENTS/PROGRESS_REPORT 不会自动进入外层提交；
   本报告已在项目内保存独立可读的交接结论。
3. 为最终源码重新生成、verify 并恢复源码包。QEMU-07 的独立重建通过属于旧快照，
   不能冒充 QEMU-09～12 的最终源码包证据。若交付声明为“本版本可独立重建”，
   必须在最终恢复目录重新构建并运行 canonical gate；留存新 manifest/hash 与结果。
4. 检查最终 staged diff 和锁定身份，再提交外层工程基线。仅更改文档/commit pin 不要求重复
   性能采样；生产输入或二进制改变则重新执行受影响测试和性能门。

本轮未执行 commit、tag 或 push。建议采用工程基线/预发布命名，暂不声明完整产品 v1.0。
真正产品完成仍缺 APP-01 业务 ready 的独立证据、实机时序/模拟行为验证、外部 plant
闭环实时门以及整机 migration 等。14-bit ADC processing 的 14.5-cycle 值仍是资料推导，
需要直接手册或实机证据；这些限制不应因测试全绿而消失。

下一交付动作是完成上述双仓库冻结；功能开发下一切片仍是 QEMU-13 的独立观测量筛选。
本轮未引入新层、接口或验证机制，检查后无需修改 AGENTS/INTERFACES/ARCHITECTURE。
