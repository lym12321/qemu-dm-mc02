#include <stdint.h>

#define FDCAN1 0x4000a000u
#define FDCAN2 0x4000a400u
#define REG(base, off) (*(volatile uint32_t *)((base) + (off)))
#define CCCR  0x018u
#define ECR   0x040u
#define PSR   0x044u
#define IR    0x050u
#define RXF0C 0x0a0u
#define RXF0S 0x0a4u
#define RXESC 0x0bcu
#define TXBC  0x0c0u
#define TXESC 0x0c8u
#define TXBAR 0x0d0u
#define TXBTO 0x0d8u
#define MSGRAM ((volatile uint32_t *)0x4000ac00u)
#define RESULT ((volatile uint32_t *)0x20000000u)

#define IR_BO        (1u << 25)
#define PSR_BO       (1u << 7)
#define RESULT_DONE 0x424f3131u /* BO11 */

static void wait_tec(uint32_t target)
{
    while ((REG(FDCAN1, ECR) & 0xffu) < target) {
    }
}

static void configure_tx(void)
{
    REG(FDCAN1, CCCR) = 1u;
    REG(FDCAN1, TXBC) = 3u << 16;
    REG(FDCAN1, TXESC) = 0u;
    MSGRAM[0] = 0x321u << 18;
    MSGRAM[1] = 8u << 16;
    MSGRAM[2] = 0x44332211u;
    MSGRAM[3] = 0x88776655u;
    REG(FDCAN1, CCCR) = 0u;
}

static void configure_receiver(void)
{
    REG(FDCAN2, CCCR) = 1u;
    REG(FDCAN2, RXF0C) = 32u | (1u << 16);
    REG(FDCAN2, RXESC) = 0u;
    REG(FDCAN2, CCCR) = 0u;
}

void Reset_Handler(void)
{
    configure_tx();

    /* With no configured receiver, each frame completes as an ACK error.
     * Thirty-two errors take TEC from zero past 255. */
    for (unsigned i = 0; i < 32; ++i) {
        REG(FDCAN1, TXBAR) = 1u;
        wait_tec((i == 31u) ? 255u : (i + 1u) * 8u);
    }
    RESULT[0] = REG(FDCAN1, ECR);
    RESULT[1] = REG(FDCAN1, PSR);
    RESULT[2] = REG(FDCAN1, IR);
    RESULT[3] = REG(FDCAN1, CCCR);
    if ((RESULT[0] & 0xffu) != 255u ||
        !(RESULT[1] & PSR_BO) || !(RESULT[2] & IR_BO) ||
        !(RESULT[3] & 1u)) {
        RESULT[7] = 0x424f4641u; /* BOFA */
        for (;;) {
            __asm__ volatile ("wfi" ::: "memory");
        }
    }

    /* INIT -> normal is the explicit, coarse recovery contract. */
    REG(FDCAN1, CCCR) = 1u;
    REG(FDCAN1, CCCR) = 0u;
    RESULT[4] = REG(FDCAN1, ECR);
    RESULT[5] = REG(FDCAN1, PSR);
    RESULT[6] = REG(FDCAN1, CCCR);

    configure_receiver();
    REG(FDCAN1, TXBAR) = 1u;
    while ((REG(FDCAN2, RXF0S) & 0x7fu) == 0) {
    }
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
