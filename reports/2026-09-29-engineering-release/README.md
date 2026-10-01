# 2026-09-29 工程版本源码冻结记录

用户选择独立 NOR 器件，并要求只准备待提交版本；不 commit/tag/push。
本报告记录源码冻结时已经完成的证据。冻结后的最终验收记录独立于源码包，
以 `build/source-packages/engineering-20260929.validation.json` 为准；
它关联最终包 SHA-256、独立恢复构建路径、完整门禁和逐轮性能日志，不回写源码包。

## NOR 资料与边界

器件参考：Winbond **W25Q64JV Revision J, March 27, 2018**，
https://www.winbond.com/resource-files/w25q64jv%20revj%2003272018%20plus.pdf 。
本轮已读取官方 PDF：印刷页 24/25（WREN/WRDI/status）、28–33（read/dummy）、
35–40（program/erase/CS/WEL）、49（JEDEC）。这是功能子集的资料来源，
不证明板上实际 BOM 后缀、封装和电气条件已经验证。
PDF SHA-256：`452d906c3be0da077fd751a2b8aa062629caf6081f8dd1eda04880854ce7c8f9`。

所属层：可复用器件；`dm-w25q64` 拥有存储、WEL 和命令状态。复用
`cosim/dm_nor_flash.c` 的存储操作与 QEMU QOM/SSI；不复制官方 m25p80 源码。
SSI 每次传送一个逻辑字节。0b/6b 使用一个 dummy token，eb 为 Fxh mode 加两个
dummy token；quad 只表达数据事务，不模拟 lane/QE/电气时钟。program/erase 在 CS
上升沿同步完成；不存在可观察的真实 busy 时长。页输入 latch 支持超过 256 字节的
覆盖/回绕，最后一次输入后才将最终 latch AND 到存储；控制器仍保留其既有单页 DLR 限制。
24-bit 地址映射到 8 MiB，读取在容量边界回绕。未知命令、continuous mode、截断和
多余固定命令字节不会修改存储；未知命令/continuous mode 有 LOG_UNIMP 诊断。
器件 reset 丢弃未完成事务、清 WEL、保留存储。保护/SFDP/软件 reset 命令、异步 WIP、
掉电原子性及器件/整机 migration 未支持，QOM VMState 明确 unmigratable。

## 当前证据

- 存储核心与 raw-image adapter：Host CTest 两目标通过。
- 新真实 SSI/QOM consumer 单测：6/6 通过，覆盖 ID、program、erase、read、abort/reset
  以及 adapter 配置拒绝与 reset-before-CS abort。
- 首次隔离链接缺 `vmstate_info_bool`；首错误是 SSI 的原生 VMState link dependency，
  补齐测试 migration/io 依赖后通过，未用假 SSI 或 stub 绕过。
- OCTOSPI persistence、alternate board profile、internal Flash smoke 全通过；
  OSPI component VMState 6/6、memory qtest 3/3 通过。
- 工作区 canonical gate：Meson 66/66、Host CTest 51/51、pytest 319/319、shell smoke
  94/94，overall PASS，身份无漂移，ROS2/MuJoCo 实际通过。路径：
  `build/test-results/release-20260929-workspace/20260929T091646.169349Z-64084/`。
- 实际 Release ELF 的 monitor `info status/stop/cont/quit` 检查通过，正常退出 0；
  日志 `build/test-results/release-20260929-interactive.log`。
- 工作区三轮 Release 60 虚拟秒性能门通过（无外部 worker、默认 board）：

| 轮次 | startup s | host / virtual s | RTF | CPU | RSS KiB | timeout/window |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 0.526966 | 60.039813 / 60.038000 | 0.999970x | 113.9% | 50436 | 0/0 |
| 2 | 0.526033 | 60.011315 / 60.011000 | 0.999995x | 113.7% | 50188 | 0/0 |
| 3 | 0.526089 | 60.026724 / 60.026000 | 0.999988x | 113.9% | 50308 | 0/0 |

日志：`build/test-results/release-20260929-workspace-rtf.log`。目标 1.0x，
使用已记录的 0.999x 采样容差；不宣称 unpaced capacity 或外部闭环性能。
工作区 QEMU SHA-256：`7c7c205f325287a44304423817f5fd822364bc5f25c158723d6655968f587595`。
固件 SHA-256：`40a67471aae051785b2523be761b30492f9b6ff8cfe34fbeca455f2f729d8a09`。
最终包独立重建结果见并列 validation.json；本节不提前宣称它通过。

## 独立恢复发现的工具缺陷

首次候选包可独立编译，但恢复根为
`build/release-restore-20260929/project` 时，多项 smoke 的 socket 路径超过 Linux
sockaddr_un 限长。首个失败状态是 QEMU/Python bind/connect 的 `UNIX socket path
is too long` / `AF_UNIX path too long`，并非 ADC/NOR guest 行为。
失败报告保留于该恢复目录的 `build/test-results/qemu-gate/20260929T142435.552700Z-94657/`。

修复所属层为测试运行工具：83 个 shell runtime 目录改用已有标准 mktemp 在短 `/tmp`
根下创建，保持每脚本 trap/资源所有权；无新增框架或 socket 协议。全脚本 bash -n 通过。
同一深恢复目录只同步精确的 83 个工具修复后，全门通过：66/51/319/94，零 skip；
报告 `20260929T142713.284037Z-110962/`。该诊断树是修复验证，不冒充最终包。
随后必须从 `engineering-20260929.tar.gz` 新恢复到 `/dev/shm/dm-release-final/project`，
不复制任何旧 build/.venv，独立安装锁定依赖、编译、全门和三轮 RTF。

## 最终交付物与可提交范围

- `build/source-packages/engineering-20260929.tar.gz`：待提交源码快照；manifest schema 2
  内含真实 base/HEAD、QEMU tracked diff hash、新增文件和逐文件内容哈希。
- 并列 `.validation.json` 与验收日志：最终门禁结论、包/固件/二进制身份；不由源码包
  自包含自己的哈希。README/RELEASE.md 是使用入口。
- 器件写集：新增 `hw/block/dm_w25q64.c`、其独立头文件和真实 SSI unit；SSI adapter、
  OCTOSPI byte mapping/reset、Kconfig/Meson 注册，以及撤销官方 NOR 两文件补丁。
- 交付工具写集：source package worktree mode 与测试、83 个 runtime 目录生成点、
  契约/能力/使用/交接文档。此前 Flash/ADC/worker 未提交切片一并交付且全门覆盖。
- 本轮不 commit/tag/push；后续 nested commit → 更新 qemu.lock → outer commit →
  新建提交版源码包。保留 ROM 用户改动，根工作区记录另行保管。

后续功能任务仍是 QEMU-13 的独立业务观测量；本工程交付不升级 APP-01、实机时序或
整机 migration 的能力等级。ADC 14-bit 的直接资料/实机 oracle 缺口继续记录。

## 提交边界

QEMU fork 与外层是两个仓库；官方 NOR 两文件已恢复固定 v8.2.2 基线（diff --exit-code 通过）。
ROM 子模块现有改动不属于交付写集。根目录 PLAN/AGENTS/PROGRESS_REPORT 不在外层 Git
仓库中，项目内契约与本报告必须可独立交接。最终快照显式记录未提交修改，不能冒称为
`qemu.lock` 所指 commit 的纯净源码。
