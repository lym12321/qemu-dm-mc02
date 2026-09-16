#include <stdint.h>

#define RCC_BASE 0x58024400u
#define REG(base, off) (*(volatile uint32_t *)((base) + (off)))
#define RESULT ((volatile uint32_t *)0x20000000u)
#define CONFIGURED_MARKER 0x4644434cu

#ifndef FDCAN_SOURCE
#define FDCAN_SOURCE 1u
#endif

static void configure_clock(void)
{
    /* HSE=24 MHz, PLL1 M/N/Q=2/40/4 and PLL2 M/N/Q=2/16/2. */
    REG(RCC_BASE, 0x28) = 2u | (2u << 4) | (2u << 12);
    REG(RCC_BASE, 0x30) = 39u | (3u << 16);
    REG(RCC_BASE, 0x38) = 15u | (1u << 16);
    REG(RCC_BASE, 0x2c) = (1u << 16) | (1u << 17) | (1u << 18) |
                          (1u << 20);
    REG(RCC_BASE, 0x00) = (1u << 0) | (1u << 16) | (1u << 24) |
                          (1u << 26);
    REG(RCC_BASE, 0x50) = (uint32_t)FDCAN_SOURCE << 28;
    RESULT[0] = CONFIGURED_MARKER;
    RESULT[1] = FDCAN_SOURCE;
}

void Reset_Handler(void)
{
    configure_clock();
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[2] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
};
