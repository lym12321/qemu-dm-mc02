#include <stdint.h>

#define FDCAN1_BASE 0x4000a000u
#define FDCAN2_BASE 0x4000a400u
#define FDCAN3_BASE 0x4000d400u
#define FDCAN_CCCR  0x018u
#define FDCAN_ECR   0x040u
#define FDCAN_RXF0C 0x0a0u
#define FDCAN_RXF0S 0x0a4u
#define FDCAN_RXF0A 0x0a8u
#define FDCAN_RXESC 0x0bcu
#define FDCAN_TXBC  0x0c0u
#define FDCAN_TXBAR 0x0d0u
#define FDCAN_TXBTO 0x0d8u
#define MSGRAM      ((volatile uint32_t *)0x4000ac00u)
#define RESULT      ((volatile uint32_t *)0x20000000u)

#define RESULT_MARKER 0x434d4431u /* CMD1 */

static volatile uint32_t *reg(uint32_t base, uint32_t offset)
{
    return (volatile uint32_t *)(uintptr_t)(base + offset);
}

static void configure_node(uint32_t base, uint32_t rx_word,
                           uint32_t tx_word)
{
    *reg(base, FDCAN_CCCR) = 1u; /* INIT; model supplies CCE. */
    *reg(base, FDCAN_RXF0C) = rx_word | (3u << 16);
    *reg(base, FDCAN_RXESC) = 0;
    *reg(base, FDCAN_TXBC) = tx_word | (3u << 16);
    *reg(base, FDCAN_CCCR) = 0; /* enter normal operation after setup */
}

static uint32_t standard_id(volatile uint32_t *element)
{
    return element[0] >> 18;
}

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    volatile uint32_t *tx1 = MSGRAM + 0;
    volatile uint32_t *tx2 = MSGRAM + 16;
    volatile uint32_t *rx2 = MSGRAM + 32;
    volatile uint32_t *rx3 = MSGRAM + 64;
    volatile uint32_t *rx3_arb = MSGRAM + 80;
    uint32_t get_index;

    RESULT[0] = RESULT_MARKER;
    configure_node(FDCAN1_BASE, 0, 0);
    configure_node(FDCAN2_BASE, 32, 16);

    /* FDCAN1 -> FDCAN2. */
    tx1[0] = 0x321u << 18;
    tx1[1] = 1u << 16;
    tx1[2] = 0xa5a5a5a5u;
    *reg(FDCAN1_BASE, FDCAN_TXBAR) = 1;
    while ((*reg(FDCAN1_BASE, FDCAN_TXBTO) & 1u) == 0) {
    }
    while ((*reg(FDCAN2_BASE, FDCAN_RXF0S) & 0x7fu) == 0) {
    }
    RESULT[1] = standard_id(rx2);
    RESULT[2] = rx2[2];
    get_index = (*reg(FDCAN2_BASE, FDCAN_RXF0S) >> 8) & 0x3fu;
    *reg(FDCAN2_BASE, FDCAN_RXF0A) = get_index;

    /* FDCAN2 -> FDCAN3. */
    configure_node(FDCAN3_BASE, 64, 32);
    tx2[0] = 0x220u << 18;
    tx2[1] = 1u << 16;
    tx2[2] = 0x33333333u;
    *reg(FDCAN2_BASE, FDCAN_TXBAR) = 1;
    while ((*reg(FDCAN2_BASE, FDCAN_TXBTO) & 1u) == 0) {
    }
    while ((*reg(FDCAN3_BASE, FDCAN_RXF0S) & 0x7fu) == 0) {
    }
    RESULT[4] = standard_id(rx3);
    RESULT[5] = rx3[2];
    get_index = (*reg(FDCAN3_BASE, FDCAN_RXF0S) >> 8) & 0x3fu;
    *reg(FDCAN3_BASE, FDCAN_RXF0A) = get_index;

    /* Move FDCAN3's FIFO so the arbitration batch starts empty. */
    *reg(FDCAN3_BASE, FDCAN_CCCR) = 1;
    *reg(FDCAN3_BASE, FDCAN_RXF0C) = 80u | (3u << 16);
    *reg(FDCAN3_BASE, FDCAN_CCCR) = 0;

    /* QEMU's standard bus is an immediate broadcast. A single TXBAR write
     * reaches peers in controller submission order; it does not model CAN
     * bit-level arbitration. */
    tx1[0] = 0x500u << 18;
    tx1[1] = 1u << 16;
    tx1[2] = 0x11111111u;
    tx1[4] = 0x100u << 18;
    tx1[5] = 1u << 16;
    tx1[6] = 0x22222222u;
    *reg(FDCAN1_BASE, FDCAN_TXBAR) = 3;
    while ((*reg(FDCAN1_BASE, FDCAN_TXBTO) & 3u) != 3u) {
    }
    while ((*reg(FDCAN3_BASE, FDCAN_RXF0S) & 0x7fu) < 2) {
    }
    RESULT[7] = standard_id(rx3_arb);
    RESULT[8] = rx3_arb[2];
    RESULT[9] = standard_id(rx3_arb + 4);
    RESULT[10] = rx3_arb[6];
    RESULT[11] = *reg(FDCAN3_BASE, FDCAN_RXF0S);
    RESULT[12] = *reg(FDCAN1_BASE, FDCAN_ECR);
    RESULT[13] = *reg(FDCAN2_BASE, FDCAN_ECR);
    RESULT[14] = *reg(FDCAN3_BASE, FDCAN_ECR);
    RESULT[15] = *reg(FDCAN1_BASE, FDCAN_TXBTO);
    RESULT[16] = *reg(FDCAN2_BASE, FDCAN_TXBTO);
    RESULT[17] = 1;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
