#include "dm_mc02_machine.h"

#include <stdio.h>

static const DmMc02Capability capabilities[] = {
    { "machine-name", "ready", DM_MC02_MACHINE_NAME },
    { "target-cpu", "ready", DM_MC02_CPU_NAME },
    { "flash-and-sram-map", "ready", "STM32H723 ITCM/DTCM/AXI/D2/D3/Flash map" },
    { "nvic-systick", "ready", "provided by QEMU ARMv7-M container" },
    { "spi2-bmi088-polled", "ready", "GPIOC chip-select, SPI2 register window and both BMI088 dies" },
    { "uart-chardev", "ready", "optional USART1/2/3, UART5/7 and USART10 TX/RX byte channels" },
    { "fdcan-chardev", "ready", "FDCAN1/2/3 message-RAM FIFO bridge with timestamped fixed frames" },
    { "fdcan-medium", "ready", "in-process virtual-time FDCAN1/2/3 switch with deterministic arbitration" },
    { "cordic-q31", "ready", "H723 CORDIC sine/cosine Q1.31 functional subset" },
    { "usb-controller-ready", "ready", "DWC2 controller-ready registers and reset-complete status" },
    { "usb-virtual-cdc-pipe", "ready", "Optional FIFO0 byte pipe on serial slot 10" },
    { "internal-flash-program-erase", "ready", "Writable 1 MiB backing with unlock and sector erase" },
    { "stm32h723-peripherals", "pending", "functional GPIO/SPI/DMA/ADC/UART/FDCAN/OCTOSPI/CORDIC subsets exist; full H723 behavior remains pending" },
    { "dm-mc02-board-devices", "pending", "LED/buzzer/power telemetry and W25Q64 OCTOSPI2 path exist; electrical and waveform-accurate devices remain pending" },
};

const DmMc02Capability *dm_mc02_capabilities(size_t *count)
{
    if (count != NULL) {
        *count = sizeof(capabilities) / sizeof(capabilities[0]);
    }
    return capabilities;
}

int dm_mc02_machine_probe(void)
{
    size_t count = 0;
    const DmMc02Capability *items = dm_mc02_capabilities(&count);
    size_t pending = 0;

    puts("DM-MC02 QEMU M3 host probe");
    for (size_t i = 0; i < count; ++i) {
        printf("%-24s %-7s %s\n", items[i].name, items[i].state, items[i].detail);
        if (items[i].state[0] == 'p') {
            ++pending;
        }
    }
    printf("summary: %zu/%zu capabilities pending; peripheral work remains explicit\n",
           pending, count);
    return pending == 0 ? 0 : DM_MC02_STATUS_PENDING;
}

int main(void)
{
    return dm_mc02_machine_probe();
}
