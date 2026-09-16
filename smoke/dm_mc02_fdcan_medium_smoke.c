#include <stdint.h>

#define FDCAN1 0x4000a000u
#define FDCAN2 0x4000a400u
#define RCC_BASE 0x58024400u
#define REG(base, off) (*(volatile uint32_t *)((base) + (off)))
#define CCCR  0x018u
#define DBTP  0x00cu
#define NBTP  0x01cu
#define RXF0C 0x0a0u
#define RXF0S 0x0a4u
#define TXBC  0x0c0u
#define TXESC 0x0c8u
#define TXBAR 0x0d0u
#define TXBTO 0x0d8u
#define MSGRAM ((volatile uint32_t *)0x4000ac00u)
#define RESULT ((volatile uint32_t *)0x20000000u)

static void configure_fdcan_clock(void)
{
    /* HSE=24 MHz, PLL1 M/N/Q=2/40/4 => PLL1Q=120 MHz. */
    REG(RCC_BASE, 0x28) = 2u | (2u << 4);
    REG(RCC_BASE, 0x30) = 39u | (3u << 16);
    REG(RCC_BASE, 0x2c) = (1u << 16) | (1u << 17) | (1u << 18);
    REG(RCC_BASE, 0x00) = (1u << 0) | (1u << 16) | (1u << 24);
    REG(RCC_BASE, 0x50) = 1u << 28;
}

#define RESULT_DONE 0x4d454455u

static void configure_rx(uint32_t base, unsigned word)
{
    REG(base, CCCR) = 1;
    /* 120 MHz kernel clock, prescaler 3, TSEG1 29, TSEG2 10 = 1 Mbit/s.
     * These values exercise the optional duration-aware medium path. */
    REG(base, NBTP) = 2u | (28u << 8) | (9u << 16);
    REG(base, DBTP) = (2u << 16) | (28u << 8) | (9u << 4);
    REG(base, RXF0C) = word | (3u << 16);
    REG(base, 0x0bcu) = 0;
    REG(base, CCCR) = 0;
}

static void configure_tx(uint32_t base)
{
    REG(base, CCCR) = 1;
    REG(base, TXBC) = 3u << 16;
    REG(base, TXESC) = 0;
    REG(base, CCCR) = 0;
}

void Reset_Handler(void)
{
    configure_fdcan_clock();
    configure_rx(FDCAN1, 96);
    configure_rx(FDCAN2, 32);
    configure_tx(FDCAN1);

    /* One TXBAR write gives both frames the same QEMU virtual timestamp. */
    MSGRAM[0] = 0x300u << 18;
    MSGRAM[1] = 8u << 16;
    MSGRAM[2] = 0x11111111u;
    MSGRAM[4] = 0x100u << 18;
    MSGRAM[5] = 8u << 16;
    MSGRAM[6] = 0x22222222u;
    RESULT[8] = REG(FDCAN1, 0x0c4u);
    REG(FDCAN1, TXBAR) = 3;
    RESULT[9] = REG(FDCAN1, 0x0c4u);
    RESULT[10] = REG(FDCAN1, 0x0ccu);

    while ((REG(FDCAN1, TXBTO) & 3u) != 3u) {
    }
    while ((REG(FDCAN2, RXF0S) & 0x7fu) != 2u) {
    }
    RESULT[11] = REG(FDCAN1, 0x0c4u);
    RESULT[12] = REG(FDCAN1, 0x0ccu);

    RESULT[0] = MSGRAM[32];
    RESULT[1] = MSGRAM[34];
    RESULT[2] = MSGRAM[36];
    RESULT[3] = MSGRAM[38];
    RESULT[4] = MSGRAM[39];
    RESULT[5] = MSGRAM[40];
    RESULT[6] = REG(FDCAN1, RXF0S);
    RESULT[7] = RESULT_DONE;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[2] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
};
