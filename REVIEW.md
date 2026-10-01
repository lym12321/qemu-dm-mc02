# 当前审查与残余风险

更新于 2026-10-01。功能见 [CAPABILITIES.md](CAPABILITIES.md)，验收与任务见
[PLAN.md](PLAN.md)。历史首错分析仅保留于 [归档](docs/history/REVIEW-through-20261001.md)
和各 reports，不在活动文档重复完整交付日志。

## 发布整理

- 原外层忽略整个 QEMU fork，单独推送会丢模型。改用同仓库 QEMU 分支与固定子模块，
  lock/gitlink 必须一致，新克隆构建是直接消费者门。
- 删除下载官方 tag 的 bootstrap、仅检查的伪 patch、优先依赖系统 QEMU 的探测入口；
  统一标准 Git 与项目构建脚本。
- 使用说明合并到 README；当前计划精简，历史审查归档。产物、外部固件、ROM 改动不入库。
- packager 排除外层 gitlink，由内层 canonical 清单收集字节；固件身份可通过
  `DM_MC02_ELF` 指定。本次未改变芯片数据面。

## 残余边界

| 风险 | 下一门 / 限制 |
|---|---|
| 完整业务 ready 未验证 | QEMU-13 固定独立观测量，tick 不作为替代 |
| ADC 14-bit processing clocks 为相邻 vendor 数据推导 | 补 RM0468/实机 oracle，仍为 approximation |
| NOR 同步完成与 quad token | QE/保护/SFDP、真实忙时序和 migration 另设器件切片 |
| 组件 VMState 不等于整机迁移 | 先验证 RAM/CPU/IRQ/外设恢复顺序 |
| FIFO 立即消费兼容路径无 rollback | direct 单 beat reservation 不代表 FIFO 多 beat 原子性 |
| 真实 plant/Gazebo 闭环未验证 | adapter smoke 不替代 world/model 验收 |
| 性能依赖宿主、固件与场景 | 固定身份并三轮测量，旧结果不能覆盖新场景 |
| 持久化仅正常退出保存 | 无崩溃恢复/掉电原子性保证 |

已关闭的 ADC 过早 EOC、NOR reset 意外提交、深路径 socket、worker admission、Flash
exact-size 和 RTF 生命周期问题有对应测试与历史记录，后续不得重新引入上层 workaround。
