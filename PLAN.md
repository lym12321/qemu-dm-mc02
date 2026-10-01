# 项目进度与长期计划

更新时间：2026-10-01。范围为 QEMU STM32H723 / DM-MC02；Renode 不进入构建验收，
固件保持只读。当前能力唯一入口为 [CAPABILITIES.md](CAPABILITIES.md)。

## 当前切片：GitHub 完整源码发布（进行中）

用户已授权提交并推送 `git@github.com:lym12321/qemu-dm-mc02.git`。
所属层：源码交付/工具；producer：已提交 QEMU fork；boundary：同仓库分支、
固定 gitlink 和 qemu.lock；consumer：新克隆构建、打包与测试。

- [x] 核对空远端，补全官方 ancestry，保留 ROM 工作树。
- [x] 标准子模块取代官方 bootstrap、伪 patch 和系统 QEMU 探测脚本。
- [x] README 合并使用入口，移除重复 RELEASE，历史审查归档。
- [x] 固定 QEMU 提交，验证 packager 边界与完整工程门。
- [ ] 干净 checkout 构建验证，推送两个分支。
- [ ] GitHub 克隆复核 lock/gitlink/源码与运行入口，记录结果。

最小门为 source-package pytest；直接消费者门为干净 clone/build/package；集成门为
四集合 canonical gate。系统依赖与外部 ELF 不随 Git 分发，发布不新增功能支持声明。

已验证：QEMU 提交 `b54f49e4be05c4114d667bf5de11f17f63ea59db`；packager 隔离
pytest 6/6；工程门 Meson 66/66、Host 51/51、pytest 320/320、smoke 94/94，
无失败/跳过/阻断。本地报告：
`build/test-results/github-publication/20261001T123034.807679Z-167341/summary.json`。
基于外层 `71b80ef` 的源码包已 create/verify/restore，11,618 个路径通过哈希验证；
包 SHA-256 为 `193226b463bcd72c23af5fcd12bf8b2efa32c6cf68b8e39eebb4311a18f46ebb`。
纯净 checkout 从零下载 wraps 并构建成功，独立目录全门也通过 66/51/320/94，
零失败/跳过/阻断；报告为
`build/test-results/github-clean-checkout/20261001T123348.760680Z-189336/summary.json`。
远端子模块复核继续进行；relative URL 沿用父仓库凭据，适用于 HTTPS/SSH。

## 已完成

| 范围 | 结果 |
|---|---|
| QEMU-01～04 | ADC 隔离构建、统一门、RTF epoch/回绕及有界生命周期 |
| QEMU-05～08 | 能力矩阵、源码包、独立重建与 Release 性能基线 |
| QEMU-09～11 | 内部 Flash 持久化复用、DM-MIT 映射收敛、backend admission |
| QEMU-12 | 按分辨率修复 ADC deadline；[证据](reports/2026-09-26-qemu-12-observation/README.md) |
| 独立 NOR | dm-w25q64、reset-before-CS、恢复官方 NOR 文件；[冻结验收](reports/2026-09-29-engineering-release/README.md) |
| NOR 收敛 | 删除单实现 type selector、容量 cache、初始化 JEDEC 自检；真实 SSI 6 项及 OSPI/board-profile 通过 |

NOR 收敛全门：Meson 66/66、Host 51/51、pytest 319/319、smoke 94/94，无失败/跳过。
本地原始证据为 `build/test-results/nor-simplify-20261001/20261001T121553.757107Z-158792/summary.json`。
9 月 29 日包保持冻结，不把新源码冒充旧包重建/性能身份。历史分析见
[审查归档](docs/history/REVIEW-through-20261001.md)，旧任务状态不覆盖本计划。

## 下一功能切片：QEMU-13（待开始）

1. 固定 Release ELF 哈希，选择一个可复现业务/外设观测量与实机/vendor oracle。
2. 明确 H723 producer、公开边界和直接 consumer，找首个错误状态；无 oracle 则保留 unverified。
3. 仅在所属层修复，补隔离和直接消费者测试，不用固件特判绕过。
4. 同步矩阵/契约/审查并运行工程门；影响性能时重跑三轮 Release 门。

后续候选：ADC 14-bit 官方/实机证据、真实机构闭环、NOR 未支持命令、跨组件恢复顺序。
整机 migration、物理模拟精度与完整业务行为没有承诺日期。每次仅一个切片进行中，
结束记录文件范围、接口、测试、限制及下一步。
