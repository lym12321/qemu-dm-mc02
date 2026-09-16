# DM-MC02 QEMU backend (M0-M3)

代码 review 结论见 [`REVIEW.md`](REVIEW.md)，长期分层、真实性、复用和性能门见
[`ARCHITECTURE.md`](ARCHITECTURE.md)，当前 v1 与最小 v2 的 QEMU/co-sim/plant 接口见
[`INTERFACES.md`](INTERFACES.md)。

这是一个独立的 QEMU 后端实验项目。它不会修改现有的 `simulation/`、`trobot/` 或其他目录。

当前版本已实现 M0/M1 的可运行基础路径、M2 的 SPI2/BMI088 最小链路，以及 M3 的二进制 co-sim codec/transport：

- 检查 `qemu-system-arm`、QEMU 版本和 ARM system target；
- 预留固定版本的 QEMU 源码目录；
- 提供编译进独立 QEMU 的 `dm-mc02` machine；
- 提供 Cortex-M7、H723 Flash/ITCM/DTCM/AXI SRAM/D2/D3 SRAM 地址映射；
- 提供 SoC 工厂 UID/校准数据只读窗口（`0x1FF1E000`），支持已知 UID、ADC 校准值读取并拒绝 guest 写入；
- 提供从 Flash 向量表加载 MSP/Reset_Handler 的真实 guest 启动 smoke；
- 提供 GPIOC 片选、SPI2 寄存器窗口和两个 BMI088 die 的芯片 ID/静止 raw 读取 smoke；
- 提供 PA15 active-low 用户键输入和 EXTI0--15 常用 pending/IRQ/W1C 路径，可通过 `user-key`
  machine/QMP 属性交互；
- 提供 `gpio-input=PORTPIN=0|1` 通用外部输入注入，输入经 GPIO IDR、SYSCFG EXTICR 和 EXTI
  进入 NVIC；
- 提供 PWR/RCC 最小寄存器模型，支持电压 ready、HSI/PLL 状态和时钟使能寄存器路径，并通过 bare-metal smoke；
- 启动阶段显式记录 H723 `RCC_RSR.PORRSTF` 上电原因；普通 `system_reset` 保留已建模原因，`RMVF` 可清除；
- 提供可复用的 H723 D1 时钟树派生：`D1CPRE` 独立影响 CPU，`HPRE` 支持 `/1`、`/2`、`/4`、`/8`、`/16`、`/64`、`/128`、`/256`、`/512` 并影响 HCLK；ADC synchronous `CKMODE=01/10/11` 使用 `HCLK/{1,2,4}`，覆盖 helper 与 ADC 边界测试；完整 APB/D2/D3 分频和 kernel source matrix 仍未实现；
- 提供虚拟时间对齐的 little-endian IMU/telemetry/ADC frame codec，带坏帧、NaN、整数电压边界、序号和时间单调性拒绝；
- v1 frame 的 header/body 编解码和 typed payload 校验由 `cosim/dm_mc02_wire.[ch]` 在 host 与 QEMU link 间共享；transport/chardev 仍独立管理队列、时钟和回调；
- v2 frame 的 36-byte header、section 结构和 compact IMU 判别由 `cosim/dm_mc02_v2_wire.[ch]` 共享；C/Python 使用同一组完整 golden vectors 做字节级 parity，typed payload/session 状态仍由各自 consumer 校验；
- v2 固定响应、板级遥测和 ADC 输入 section 的长度、little-endian 字段、保留位、flags 及消费 mask 由 `cosim/dm_mc02_v2_payload.[ch]` 共享；QEMU link 不再维护这些 payload 的私有字节偏移，IMU/MotorCommand/MotorState 变长 sections 仍由消费者校验；
- 提供 Unix-domain/TCP stream adapter，支持 4-byte little-endian 长度前缀、partial I/O 和阻塞/非阻塞模式；
- 提供 QEMU-native chardev 双向 link：host IMU frame 可经 BMI088 raw register 由 guest 原有 SPI2 路径读回，ADC_INPUT frame 可按 channel 注入 ADC1 raw 值，ADC_VOLTAGE type 6 可按 ADC pin 电压注入，并在连接建立及板级输出变化时发送 telemetry；
- 提供可选 v2 step 控制面：`--protocol v2` 可通过同一 chardev 执行 `RESET/RESET_ACK` 和带 `ImuSampleV2`/ADC sections 的 `STEP/STEP_ACK`，需要 guest 消费栅栏时可加 `--wait-step-done` 等待 BMI088 raw burst/FIFO 读取完成，并提供基础 diagnostics；v1 仍为默认。v2 还定义了可插拔的 `MotorCommand -> MOTOR_STATE` endpoint，但默认 `dm-mc02` machine 不绑定具体 plant，默认电机闭环仍走 FDCAN；
- 提供默认关闭的 `cosim-motor-loopback=on` 确定性 v2 endpoint fixture，用于验证 MotorCommand/MotorState、重复 STEP 和非法 payload 拒绝；它不模拟真实电机动力学，也不改变默认 FDCAN 路径；
- 提供只读 QMP `cosim-diagnostics`，查询 co-sim 收发、短写、丢帧和有界队列占用；该诊断面不改变 v1 wire；v1 不提供 ACK/重传，v2 STEP 由 worker 提供有限 ACK 重发；
- QEMU machine reset 时会在已打开的 co-sim chardev 上发送出站 RESET 和新 telemetry session，避免出站 sequence 回退；序号/时间单调性不因此放宽。
- 提供可选 UART1/2/3、UART5、UART7、USART10 chardev 字节通道，含 polling TX/RX、RX FIFO、RXNE/RDR，以及按 STM32H723 request ID 接线的最小 UART DMA TX/RX；UART1/USART2 默认使用可复用 DMA endpoint，`uart-dma-endpoint=off` 保留旧 MMIO 路径；
- TIM12_CH2/PB15 的 PWM 输出按虚拟时间惰性观察，并通过 `/machine` QOM 只读属性提供使能、电平、频率和占空比；不为 PWM 边沿创建 host 定时器；
- 提供可复用 timer 的 `CR2.MMS/MMS2` 主触发选择：`EGR.UG` reset、`CEN` enable、update 和 CC1 compare pulse 事件按虚拟时间输出；芯片层同时支持 CC1..CC4 compare flag、IRQ 和 W0C，H723 TIM1/TIM8 profile 显式打开 `MMS2`，并支持 `OC1REF..OC4REF` 的 master event；支持 edge-aligned、up-counting 的 PWM1/PWM2 以及 forced active/inactive，`PSC/ARR/CCR1..4` 的 active/shadow 更新语义已接入；组合模式仍未实现；
- 将 timer 的 `TRGO`/`TRGO2` source ID 收敛到可复用的 board route，并接入 TIM2/TIM3/TIM8/TIM1 的 ADC 触发映射：TIM2 regular/injected 使用 `EXTSEL=11`/`JEXTSEL=2`，TIM3 `TRGO` 使用 `4/12`，TIM3 `OC4REF` 使用 `EXTSEL=15`/`JEXTSEL=4`，TIM8 使用 `7/9`、`8/10`，TIM1 使用 `9/0`、`10/8`；正、负路径已通过 ADC qtest 和 trigger smoke；其它 timer source matrix、组合模式仍未实现；
- UART host TX 使用有界 4096-byte 队列，支持 chardev 短写后的虚拟时间重试；队列满时丢弃最新字节并保留内部计数；USART kernel clock 已按 H723 `D2CCIP2R` 的 USART16/USART234578 两组 source mux 动态派生；当 `BRR/PRESC/OVER8` 配置有效时，TX 按默认 8N1 帧长度使用 QEMU 虚拟时间逐字节发送，`TC` 在最后一帧完成后置位；
- 提供可选 FDCAN1/2/3 chardev：真实 M_CAN message RAM TX/RX FIFO、64 个 dedicated Rx Buffer、扩展/标准过滤器、CAN/CAN-FD DLC、虚拟时间戳和 `ILS/ILE` 双中断线；
- FDCAN 生产收发接入 QEMU 标准 `CanBusState`：支持 `-object can-bus,id=canbus -machine dm-mc02,canbus=canbus`，未指定时自动创建板内 bus；标准 bus 即时广播、发送者不回环，并以可接收 peer 作为 ACK 结果。旧固定 84-byte chardev 仍是显式 host wire；它保留外部时间戳但不提供标准 bus 的 CAN ID 仲裁、物理位时序、物理 ACK、error frame 或完整 bus-off recovery；旧 virtual-time medium 仅保留为 test-only fixture，不进入生产 ARM target；
- 提供 SPI2 DMA1 Stream4/3 的最小 DMAMUX request TX/RX 链路，可将 BMI088 返回字节直接写回 guest buffer；默认使用可复用 DMA endpoint，也可用 `spi-dma-endpoint=off` 保留旧 MMIO 路径；
- 提供 DMA1/2 Stream 的 TC/TE level-sensitive IRQ 接线到 Cortex-M NVIC，并支持 LIFCR/HIFCR 清除；
- 提供基础 peripheral-request DMA 双缓冲：支持 `M1AR`、`DBM`、`CT`、M0/M1 交替、每 buffer `NDTR` 重装和 TC/HT 状态；DMA FCR/DME 支持 direct-mode 宽度错误、4-word FIFO 的 packing/unpacking、`FTH` 阈值和动态 `FS`，同一 request 的竞争 stream 已按 `SxCR.PL` 仲裁，完整 FIFO 错误触发条件和总线时序仍未实现；
- DMA 还提供兼容的 `dm_mc02_dma_request_batch()`：保留每个 item 的地址空间读写和外设 MMIO 副作用，同时将匹配查找与 level-sensitive IRQ fan-out 聚合到批次边界，并对每个 item 独立仲裁；UART TX 已使用该路径，需要逐事件中断边界时仍使用单 item API；
- DMA stream 在达到半传输边界时始终锁存 HTIF，HTIE 仅控制 IRQ；DMA memory/request、循环和 IRQ smoke 均覆盖 HT/TC；
- 提供 ADC1 request 9 的最小连续 half-word 数据面：支持从 `SQR1` 读取 1--4 个
  regular rank、按 channel 提供 raw/ADC-pin-voltage 输入，按 `SMPR1/SMPR2`
  逐 rank 推进，并支持软件启动、TIM2/TIM3/TIM8/TIM1 更新及 TIM3 `OC4REF` 外部触发、ADC IRQ18 的 EOC/EOS/OVR
  状态与 W1C/DR 清除、`CFGR.OVRMOD` 保留/覆盖模式，以及 DMA circular NDTR/地址
  回绕；默认模式为实时友好的
  1 ms 连续序列限流，`accurate-timing=on` 按板级有效 ADC 时钟调度转换；ADC common
  `CCR.CKMODE/PRESC` 及 PLL2P/PLL3R/CLKP 的当前选择路径参与时钟计算；
  ADC 校准数据路径按 `DIFSEL` 选择单端/差分 `CALFACT` offset，并将
  `ADC_CALFACT2` word 1 作为 signed Q0.30 线性增益近似应用于 regular/injected
  16-bit 结果，同时保留六个 `LINCALRDYW` 窗口；TIM2/TIM3/TIM8/TIM1 update 及 TIM3
  `OC4REF` 外部触发已按 regular `EXTSEL` 与 injected `JEXTSEL` 的真实 mux 编码分别映射
  （包括 TIM3 `TRGO` 的 `EXTSEL=4`/`JEXTSEL=12` 和 `OC4REF` 的 `15/4`）；真实 H723 SAR
  电容级线性补偿仍未建模。
- `CFGR.JAUTO` regular-to-injected 自动序列和 `JQM=1` context 清空已通过
  `tools/run-adc-jauto-dma-smoke.sh` 与 regular circular DMA 的 guest/QEMU 边界回归；
  当前覆盖 regular/injected 各两个 rank 的成功路径，不代表完整 injected queue、discontinuous 组合或
  injected trigger matrix。
- ADC 芯片层可通过 `adc-power-model=on` 复现 H723 `DEEPPWD`/`ADVREGEN` 的复位、
  10 us 内部稳压启动和 `ADEN` ready gate；默认 `off` 仅保留旧直接启动 fixture 的
  兼容行为，严格 guest/板级验证应显式开启。
- 提供板级离散电源模型：默认 VIN=24 V，ADC channel 4 按 VIN/11 分压，channel 19
  以 LCD 按键 NONE 为默认值，并根据 GPIOC PC13/PC14/PC15 派生输出使能和电源状态；
  VIN=0 会在两种供电策略下复位并暂停 Cortex-M7，恢复有效 VIN 后从复位入口启动；
  `/machine` QOM 只读属性 `mcu-power-good` 可用于工具侧确认 MCU 电源状态；
- 提供 TIM8 `CC1DE`/request 47 到 DMA2 Stream6 的最小 compare request 路径，可将 D2 SRAM PWM 序列通过真实 DMA 外设写入 `TIM8_CCR1`；默认近似模式合并固定端点的批量写入并保留 DMA 状态，`accurate-timing=on` 仍保留逐 item 外设副作用；
- 提供 OCTOSPI2 的 W25Q64 数据路径：生产 QEMU 通过板卡无关 SSI 适配器使用标准 `w25q64/m25p80`，支持 JEDEC/status/WREN、普通/fast/quad read、page program、4K/32K/64K/chip erase、memory-mapped read、DMA endpoint 和 raw-image persistence；旧 `DmNorFlash` 仅用于显式 host test fixture，真实 OSPI DMA request/line timing/DTR 仍未完整建模；
- 提供 CORDIC Q1.31 sine/cosine 功能子集，支持配对结果和不支持配置的明确诊断；BMI088 信号层支持静态 bias、噪声、ODR、带宽、温漂系数和可复现 bias random walk；
- 提供 CRC 常用计算子集：32/16/8/7-bit polynomial、INIT/RESET、字节/半字/字输入及输入/输出 bit-reversal；
- 提供 USB OTG HS/DWC2 controller-ready 寄存器路径和复位完成状态；可选 serial slot 10 提供 FIFO0 虚拟 CDC 字节管道；另有板卡无关的 endpoint packet queue、EP0 control-transfer core、request-level QEMU USB adapter、transaction dispatcher、synthetic upstream host 和最小 DWC2 device-mode core，内部 harness 覆盖 descriptor、CDC line coding、EP1..EP5、DWC2 EP1 FIFO/计数/IRQ、公共 FIFO count 查询、上电 TX FIFO 寄存器、64-byte 分包、PID toggle、NAK、STALL、控制传输阶段和 bulk packetization；control unit `7/7`、transaction unit `5/5`、host unit `4/4`、DWC2 unit `7/7`、USB qtest `10/10`；DWC2 已接入 DM-MC02 USB MMIO 窗口和 IRQ 77/NVIC，仍未实现真实 USB bus attachment、DMA/FIFO 时序、PHY、SOF 和宿主机枚举；
- 新增可复用 `DmUsbDwc2ControlLink`，以 `control -> DWC2 raw` 顺序联合保存控制传输中间态和 DWC2 endpoint/FIFO 状态；`test-dm-usb-dwc2-control-link-vmstate` `7/7` 验证 DATA_IN 续传、SET_ADDRESS pending、request-level consumer、错误 child 顺序和失败前无 runtime sync；该组件尚未注册为整机 migration；
- 提供独立 `stm32h723-usb-host` reference profile：将可复用 H723 host controller、QEMU USB root port 和 IRQ 77 组合为单-root-port fixture；同时提供不依赖 QEMU API 或 DM-MC02 board wiring 的 Cortex-M7 EP0 polling client、configuration descriptor parser、endpoint pipe/data-toggle state、completion-driven channel operation、controller-independent async completion/cancel record、single-packet PIO client 和 bulk packet adapter。以直接绑定 root port 的 `usb-kbd,bus=usb-bus.0,port=1` 完成 `GET_DESCRIPTOR -> SET_ADDRESS(5) -> GET_STATUS -> GET_CONFIGURATION(header/full) -> HID endpoint pipe/state -> SET_CONFIGURATION(1) -> interrupt-IN NAK` bare-metal smoke；另以 `usb-serial` 的 64-byte bulk endpoint 完成 130-byte OUT 和 12-byte IN（2-byte FTDI 状态头加 10-byte chardev 输入）的 H723 PIO/QEMU transfer smoke，并校验最终 `HCCHAR/HCTSIZ`、PID、lease 释放和 chardev payload。该 profile 不改变 `dm-mc02` 的 USB Device-mode 角色，也不表示 DM-MC02 支持 USB Host；
- 提供可写的 1 MiB 内部 Flash backing，支持固件直接编程和 FLASH_R 解锁/sector erase 快速路径；擦除时序、ECC、option bytes 语义仍未实现；
- `stm32h723-usb-host` reference profile 默认启用 QEMU virtual-clock deferred completion；
  通用 `dm-stm32h7-otg-host-qemu` 可通过 `completion-scheduler=false|true` 选择同步或
  scheduler 路径，并可在设备初始化前用 `completion-delay-ns=<ns>` 设置确定性虚拟延迟。
  零延迟仍是 virtual-timer 事件，system reset 会取消尚未投递的 completion。
- 可通过 `flash-file` 选择性启用内部 Flash 文件持久化，默认不产生磁盘 I/O；
- BMI088 输入支持可复现的 gyro/accel 噪声、三轴零偏、随机种子、量程灵敏度联动与可配置温度；固件配置 ODR 后，host 输入按其 frame 时间戳采样，并更新 DRDY 与加速度计 25.6 kHz sensor-time；`ACC_PWR_CONF/ACC_PWR_CTRL/GYRO_LPM1` 会门控断电或 suspend 状态下的新样本；加速度计 1024-byte FIFO 与陀螺仪 100-frame/8-byte FIFO 可经真实 SPI FIFO 寄存器读取；默认仍为零噪声/零偏、25°C；
- 提供 `tools/dm_mc02_sim_worker.py` 外部仿真桥：NullEngine 和 MuJoCo 可进行 DM-MIT 电机控制/反馈联调，MuJoCo 通过 uv optional extra 接入，ROS2 engine 通过标准 IMU/力矩话题连接 Gazebo，支持固定步长和 `--realtime` 时间对齐；
- 提供独立 host-side `DmMotorBusAdapter`：将 DM-MIT/legacy float CAN wire 映射为协议中立的电机命令和状态，worker、NullEngine、MuJoCo、ROS2 共享同一映射策略；默认仍保持 guest FDCAN wire-faithful 闭环；
- 提供协议中立的 backend registry：内建 Null/MuJoCo/ROS2 使用统一 factory，用户可通过 `--backend MODULE:FACTORY` 直接加载 backend，或通过 `--backend-registry MODULE:INITIALIZER --engine NAME` 注册并选择自定义 backend，不需修改 worker 核心文件；
- 提供 `tools/collect-performance-baseline.sh`，以固定 10000 帧采集 NullEngine 吞吐、worker 启动延迟和 DM-MC02 启动 smoke 时间；
- 提供 `tools/collect-firmware-rtf.sh`，直接启动 Release 固件并读取 ELF 中的 `xTickCount`，报告真实固件 RTF、有效 tick/s、QEMU CPU 利用率和 RSS；
- 提供可独立编译的 host-side machine capability probe；
- 提供不依赖 guest 固件的 `-machine none` smoke 检查（在本机安装 QEMU 后可执行）。
- 提供初始化前可选、初始化后锁定的 `board-profile` machine 属性；timer/UART/FDCAN
  route 的地址与 IRQ 绑定由 profile 内聚管理，便于复用同 SoC 芯片模型。
- 板级连接描述集中在 QEMU 内部的 `dm_mc02_board` profile：UART/FDCAN/TIM/ADC
  地址、serial slot、DMAMUX request、DMA 控制器、IRQ vector 和 RS485 DE 引脚由
  静态 wiring 数据提供；DMA、ADC、UART、FDCAN 等芯片模型不依赖 DM-MC02 板级文件。
  更换板卡时可复用芯片模型和 machine 组装流程，只替换 profile。
- 已注册的备用 profile `STM32H723-EVAL` 可通过
  `-machine dm-mc02,board-profile=STM32H723-EVAL` 选择。它是用于复用验收的虚拟
  评估板配置，不声称对应某一块真实商业板卡；当前提供 2 个 UART、1 个 FDCAN 和
  独立的 GPIO/DMA wiring。

这不是完整 STM32H723 模型。ARMv7-M 的 CPU/NVIC/SysTick 基础由 QEMU 提供；当前 H723 外设已加入 GPIO、SPI2/BMI088、PWR/RCC、DMA/DMAMUX、定时器窗口、UART/FDCAN/ADC、OCTOSPI2、CORDIC、USB FIFO0 虚拟管道及若干启动兼容窗口，但其中许多仍是寄存器安全模型。QEMU chardev link 已接入 IMU 注入和板级输出 telemetry：PA7 活动投影为白色 LED，TIM8 DMA2 Stream6 的真实 firmware buffer 可解码为 WS2812 RGB，TIM12_CH2/PB15 投影为蜂鸣器，PC13/PC15 投影为 24 V/5 V 标志；BMI088 已支持量程、ODR 下采样、轻量带宽滤波、DRDY、accel sensor-time、参数化温漂/random walk 及 accel/gyro FIFO SPI 数据面（含 accel config/skip/sensortime 基础帧），但不包含 INT tag、sample-drop frame、FIFO 中断引脚、完整器件级滤波校准或动态温度物理源；UART/FDCAN 已有可选主机通道，UART TX 支持有界缓存和短写重试，FDCAN1/2/3 通过 QEMU standard CAN bus 连接，固定 84-byte chardev 仍是独立 host wire，USART2/USART3 支持 GPIO/AF 两种 RS485 DE 方向模式。外部 worker 已支持 Null/MuJoCo，并提供 ROS2 话题桥接 Gazebo，同时可选地通过 SocketCAN host bridge 接入 Linux CAN/CAN-FD；`tools/run-worker.sh` 会为 Null/MuJoCo 选择 uv 环境，为 ROS2 选择已 source 的系统 ROS Python。`system_reset` 默认是 warm reset，也可用 `cold-reset=on` 清空片上 SRAM；其它 DMA 外设 request/定时器波形、未接出 timer 的 TRGO/TRGO2 source、timer 组合触发、完整 USB 枚举/端点/总线、Flash ECC/option bytes、SocketCAN 原生 QEMU 后端仍 pending。Release 固件已能持续运行并驱动 FreeRTOS tick，但尚不能声称完整业务启动。未实现功能会继续报告为 pending。

## 快速检查

```bash
bash tools/check-qemu.sh --report-only
cmake -S . -B build/host
cmake --build build/host
build/host/dm_mc02_probe || test $? -eq 2
bash tools/build-smoke.sh
bash tools/build-qemu.sh
bash tools/run-mc02-smoke.sh
bash tools/run-cold-reset-smoke.sh
bash tools/run-iwdg-smoke.sh
bash tools/run-bmi088-smoke.sh
bash tools/run-bmi088-fifo-smoke.sh
build/host/dm_mc02_bmi088_chip_smoke
bash tools/run-cosim-smoke.sh
bash tools/run-uart-smoke.sh
bash tools/run-uart-dma-smoke.sh
bash tools/run-uart2-dma-smoke.sh
bash tools/run-uart-idle-smoke.sh
bash tools/run-uart-rx-timing-smoke.sh
bash tools/run-fdcan-smoke.sh
bash tools/run-fdcan-clock-smoke.sh
bash tools/run-fdcan-ext-smoke.sh
bash tools/run-fdcan-rx-buffer-smoke.sh
bash tools/run-fdcan-busoff-smoke.sh
bash tools/run-can-medium-smoke.sh
bash tools/run-dm-motor-smoke.sh
bash tools/run-sim-worker-smoke.sh
bash tools/run-mujoco-worker-smoke.sh
bash tools/run-ros2-worker-smoke.sh
bash tools/run-qemu-worker-smoke.sh
bash tools/run-backend-plugin-smoke.sh
bash tools/run-qemu-v2-step-done-smoke.sh
bash tools/run-dma-smoke.sh
bash tools/run-dma-ht-smoke.sh
bash tools/run-ws2812-smoke.sh
bash tools/run-spi2-dma-smoke.sh
bash tools/run-spi2-dma-smoke.sh off
bash tools/run-spi2-dma-legacy-smoke.sh
bash tools/run-dma-irq-smoke.sh
bash tools/run-adc-dma-smoke.sh
bash tools/run-adc-jauto-dma-smoke.sh
bash tools/run-adc-input-smoke.sh
bash tools/run-adc-analog-smoke.sh
bash tools/run-adc-power-smoke.sh
bash tools/run-power-boundary-smoke.sh
bash tools/run-tim8-dma-smoke.sh
bash tools/run-tim8-dbm-smoke.sh
bash tools/run-tim8-approx-dma-smoke.sh
bash tools/run-tim2-clock-smoke.sh
bash tools/run-clock-zero-smoke.sh
bash tools/run-tim3-irq-smoke.sh
bash tools/run-ospi-smoke.sh
bash tools/run-cordic-smoke.sh
bash tools/run-crc-smoke.sh
bash tools/run-usb-smoke.sh
bash tools/run-usb-pipe-smoke.sh
bash tools/run-stm32h723-usb-host-smoke.sh
bash tools/run-stm32h723-usb-host-bulk-smoke.sh
bash tools/run-peripheral-reset-smoke.sh
bash tools/run-flash-smoke.sh
bash tools/run-rs485-smoke.sh
build/host/dm_mc02_transport_smoke
bash tools/run-cosim-link-readback-smoke.sh
bash tools/run-cosim-link-timing-smoke.sh
bash tools/run-socketcan-smoke.sh
```

真实固件性能基线（只读使用 `trobot/build/Release/trobot.elf`，不会修改固件）：

```bash
bash tools/collect-firmware-rtf.sh --warmup 0.25 --duration 1.0
```

RTF 定义为固件虚拟时间（FreeRTOS tick，1 tick = 1 ms）除以宿主墙钟时间；该指标与
NullEngine 的协议吞吐不可直接比较。

标准 Release gate（从 reset 开始，60 秒虚拟时间，3 次，默认 `0.999x` 测量容差）：

```bash
DM_MC02_ELF=/home/lab/sim/trobot/build/Release-current/trobot.elf \
  bash tools/run-release-rtf-gate.sh
```

probe 返回 `2` 表示发现了尚未实现的外设能力；M1 的 CPU、内存映射和 ARMv7-M 基础路径会报告为 ready。

若安装了 QEMU，可运行：

```bash
bash tools/check-qemu.sh
bash tools/run-qemu.sh --smoke
```

源码构建的 QEMU 位于 `build/qemu/qemu-system-arm`，也可以显式指定：

```bash
QEMU_SYSTEM_ARM="$PWD/build/qemu/qemu-system-arm" bash tools/run-mc02-smoke.sh
```

## 准备 QEMU 源码

默认脚本只做探测，不联网。明确需要下载时才使用：

```bash
bash tools/bootstrap-qemu.sh --download --ref <固定tag或commit>
bash tools/build-qemu.sh
# 可选：构建不含 DM-MC02/H723/协同仿真的通用 ARM QEMU
bash tools/build-qemu-generic.sh
```

源码、构建目录和锁定信息都限制在本目录下。`qemu.lock` 会记录版本、探测结果和 M2 smoke 状态。Python 工具环境由本目录的 `pyproject.toml` 和 `uv` 管理；QEMU 8.2.2 的 Meson/Ninja 构建依赖也只在本目录的环境中使用。DM-MC02 构建位于 `build/qemu`，通用 ARM 构建位于独立的 `build/qemu-generic`，后者不包含项目专用 feature symbol 或源对象。

```bash
uv sync --group dev
# 可选 MuJoCo 引擎：uv sync --group dev --extra mujoco
```

## 当前阻塞项

在没有 `qemu-system-arm` 或 QEMU 源码的机器上，不能声称 M-profile/M7 smoke 已通过。请查看：

```bash
bash tools/check-qemu.sh --report-only
```

M1 已完成内存/复位/向量表路径和 PWR/RCC 最小模型，M2 已完成 polled SPI2/BMI088 子集、SPI2 byte DMA TX/RX request、UART DMA 基础路径、ADC1 channel/rank raw input、ADC pin voltage input、SMPR1/SMPR2 rank-level 采样时间、软件启动、TIM8 更新外部触发、ADCAL 虚拟完成、EOC/EOS/OVR IRQ18 中断、默认 24 V/VIN 分压与 LCD NONE 源和 circular DMA、最小 USART IDLE IRQ、DMA TC/TE IRQ、UART、FDCAN chardev 通道、QEMU standard CAN bus、DMA memory-to-memory 搬运、WS2812 buffer observer、OCTOSPI2/W25Q64 最小路径、OCTOSPI2 板卡无关 DMA endpoint 与 synthetic DMA 边界测试、CORDIC Q1.31 sine/cosine 子集和 USART2/USART3 RS485 DE，M3 host transport、QEMU chardev IMU/ADC link、输出 telemetry、SocketCAN host bridge 和外部仿真 worker 已完成最小垂直切片。Release 固件已能持续运行；下一步是 Gazebo/MuJoCo 具体模型联调、时间戳对齐和完整电机映射，再补齐其它 DMA 外设 request、完整 HAL ReceiveToIdle event-size 语义、完整 SPI/DMA 状态机、ADC 完整外部触发选择矩阵和定时器波形。

UART chardev 按 QEMU serial 后端顺序映射：`serial_hd(0)` 保留给二进制 co-sim，
`serial_hd(1)` 至 `serial_hd(6)` 依次对应 USART1、USART2、USART3、UART5、UART7、
USART10。因此命令行应先放一个 `-serial none`，再放 UART 的 `-serial chardev:<id>`；
不提供这些后端时，UART 仍可用于固件初始化和轮询寄存器测试；UART DMA request
映射仍在板内生效。当前 IDLE IRQ 已支持最小标志/清除路径，但尚未实现完整
`ReceiveToIdle_DMA` event-size 回调语义。

FDCAN chardev 沿用同一 serial 后端序列：`serial_hd(7)` 至 `serial_hd(9)` 依次对应
FDCAN1、FDCAN2、FDCAN3。每帧固定 84 字节，布局为
`can_id:u32 flags:u32 dlc:u8 reserved[3] virtual_time_ns:u64 data[64]`。

可选 SocketCAN bridge 运行在 host worker 侧，不改变 QEMU 的固定 84 字节接口：
`python tools/dm_mc02_sim_worker.py --cosim <socket> --fdcan <socket> --socketcan can0`。
QEMU 发出的 FDCAN 帧会转发到 Linux `CAN_RAW` socket，来自 SocketCAN 的 classic
CAN/CAN-FD 帧会带 worker 当前虚拟时间转回 QEMU。需要 Linux `AF_CAN` 和相应接口
（例如预先创建的 `vcan0`）；未指定 `--socketcan` 时行为不变。该桥接不模拟 CAN
物理层、位时序、真实 ACK 或总线错误恢复。

Gazebo/ROS2 模式使用 `--engine ros2`：默认订阅 `/dm_mc02/imu`
（`sensor_msgs/Imu`）和 `/dm_mc02/joint_states`（`sensor_msgs/JointState`），并发布
`/dm_mc02/motor_cmd`（`std_msgs/Float64MultiArray`）。JointState 按数组索引提供电机的
position/velocity/effort，worker 据此执行 DM-MIT 控制项并返回反馈；没有状态消息时会
退化为零状态，仍保持 enable 门控。具体 Gazebo 模型需提供这两个传感器话题；可用
`--ros-joint-state-topic` 改名。

自定义 backend 使用同一个 worker 控制面。factory 接收完整的
`argparse.Namespace`，返回实现 `step(dt)`、`set_motor()`、`reset_motor()`、
`set_motor_enabled()`、`set_motor_dm()` 和 `motor_feedback()` 的对象；对象可选提供
`reset()`、`close()`，以及 `imu_timestamp_ns` 以启用外部时间戳映射。直接加载示例：

```bash
PYTHONPATH="$PWD/plugins" bash tools/run-worker.sh \
  --cosim /tmp/dm-mc02.sock --backend my_backend:create_backend
```

注册表 initializer 的形式如下，`--engine` 选择 initializer 注册的名称：

```bash
PYTHONPATH="$PWD/plugins" bash tools/run-worker.sh \
  --cosim /tmp/dm-mc02.sock \
  --backend-registry my_backends:register --engine my_plant
```

自定义 backend 默认使用项目的 uv Python；依赖 ROS2 的自定义 backend 可额外传
`--engine ros2` 让 `run-worker.sh` 使用已 source 的系统 ROS Python，同时仍由
`--backend` 加载指定 factory。backend registry 只负责构造对象，不改变 v1/v2 wire
协议、时间 owner 或 FDCAN adapter 语义。

## 2026-09-01 OSPI2 raw-image persistence

DM-MC02 machine 支持可选属性 `ospi2-flash-file`。属性必须在 machine 初始化前配置；
启动创建 OSPI2 后从该文件加载，正常退出时保存完整的 W25Q64 raw image。默认空路径
不做磁盘 I/O。文件是精确几何的原始镜像，当前 DM-MC02 OSPI2 镜像大小为 8 MiB，
不包含 header；文件尺寸不匹配不会加载，并继续使用擦除态镜像。缺失文件也保持擦除态，
其它加载/保存 I/O 错误以 warning 报告。

该功能由板卡无关的 `cosim/dm_nor_flash_persistence.[ch]` 提供，并由
`DmMc02Ospi` 以薄封装接入 OSPI backing storage。它只覆盖正常退出保存，不模拟真实
Flash latency、ECC、异常退出或掉电原子性。

验证：`dm_nor_flash_persistence_smoke`、OSPI 直接边界测试和
`bash tools/run-ospi-smoke.sh` 通过；后者验证 8 MiB 镜像的启动加载、guest 读回、
正常 `quit` 后保存及最终尺寸。

## 2026-09-01 H723 APB/timer clock model

QEMU 当前已按 H723 D2 clock tree 派生 APB1/APB2 和 timer kernel clock。`D2CFGR`
的 APB1/APB2 prescaler 与 `CFGR.TIMPRE` 会在 guest 改写后实时传播到对应定时器；
TIM2/3/12/24 使用 APB1，TIM1/8 使用 APB2。复位时 D2 APB prescaler 为 `/1`，
所以 H723 复位 HCLK/TIM clock 为 64 MHz；DM-MC02 固件配置
`HPRE=/2,D2PPRE1=/2,D2PPRE2=/2` 后，480 MHz SYSCLK 下得到 HCLK=240 MHz、
APB=120 MHz、timer=240 MHz。

验证：`dm_stm32h7_clock_tree_smoke`、定时器 qtest `22/22`、
`bash tools/run-tim2-clock-smoke.sh`、`bash tools/run-pwm-smoke.sh` 和 host CTest
`53/53` 通过。FDCAN 和 USART kernel-clock source 已分别接入独立边界；SPI、
其它 kernel-clock source 仍未扩展，也未模拟
oscillator/PLL settle、CSS、低功耗或完整 APB3/APB4/D3 clock semantics。

## 2026-09-01 H723 USART kernel-clock model

USART1/6/10 使用 `D2CCIP2R` 的 USART16 source mux，USART2/3/UART4/5/7/8
使用 USART234578 mux；APB source 分别来自 APB2 和 APB1，PLL2Q、PLL3Q、HSI/HSIDIV、
CSI、LSE 和 reserved source 的结果由 RCC readiness 规则派生。QEMU machine 为每个
group 和每个 routed UART 建立独立 `Clock` 对象，并通过只读 QOM 属性
`usart16-kernel-clock-hz`、`usart234578-kernel-clock-hz` 提供诊断值。该功能提供
kernel-clock 边界；可复用 UART consumer 现在会在有效 `BRR/PRESC/OVER8` 配置下按默认
8N1 帧长度使用虚拟时间逐字节发送，尚未覆盖 RX 位级采样。

验证：`bash tools/run-uart-clock-smoke.sh` 覆盖 9 个 source/APB divider case，完整
QEMU smoke `86/86` 和 host CTest `53/53` 通过。由于当前 QEMU HMP 没有可用的 `mw`
命令，source case 通过独立 guest 启动验证，运行中 RCC 改写仍是后续切片。
