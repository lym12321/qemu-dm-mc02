#include <stdint.h>

#define FDCAN1_CCCR  (*(volatile uint32_t *)0x4000a018u)
#define FDCAN1_IR    (*(volatile uint32_t *)0x4000a050u)
#define FDCAN1_IE    (*(volatile uint32_t *)0x4000a054u)
#define FDCAN1_ILS   (*(volatile uint32_t *)0x4000a058u)
#define FDCAN1_ILE   (*(volatile uint32_t *)0x4000a05cu)
#define FDCAN1_RXF0C (*(volatile uint32_t *)0x4000a0a0u)
#define FDCAN1_RXF0S (*(volatile uint32_t *)0x4000a0a4u)
#define FDCAN1_RXF0A (*(volatile uint32_t *)0x4000a0a8u)
#define FDCAN1_GFC   (*(volatile uint32_t *)0x4000a080u)
#define FDCAN1_SIDFC (*(volatile uint32_t *)0x4000a084u)
#define FDCAN1_TXBC  (*(volatile uint32_t *)0x4000a0c0u)
#define FDCAN1_TXESC (*(volatile uint32_t *)0x4000a0c8u)
#define FDCAN1_RXESC (*(volatile uint32_t *)0x4000a0bcu)
#define FDCAN1_TXBAR (*(volatile uint32_t *)0x4000a0d0u)
#define FDCAN1_TXBTO (*(volatile uint32_t *)0x4000a0d8u)
#define FDCAN1_HPMS  (*(volatile uint32_t *)0x4000a094u)
#define GPIOC_MODER  (*(volatile uint32_t *)0x58020800u)
#define GPIOC_ODR    (*(volatile uint32_t *)0x58020814u)
#define NVIC_ISER0   (*(volatile uint32_t *)0xe000e100u)
#define MSGRAM       ((volatile uint32_t *)0x4000ac00u)
#define RESULT       ((volatile uint32_t *)0x20000000u)

#define RESULT_TX 0x43414e31u /* CAN1 */
#define RESULT_RX 0x43414e32u
#define RESULT_IRQ 0x43414e33u

void FDCAN1_IT0_IRQHandler(void);
void FDCAN1_IT1_IRQHandler(void);

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[16u + 22u] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [16 + 19] = (uint32_t)(uintptr_t)FDCAN1_IT0_IRQHandler,
    [16 + 21] = (uint32_t)(uintptr_t)FDCAN1_IT1_IRQHandler,
};

void FDCAN1_IT0_IRQHandler(void)
{
    RESULT[9] = RESULT_IRQ;
    FDCAN1_IR = 1;
}

void FDCAN1_IT1_IRQHandler(void)
{
    RESULT[11] = 0x49543131u; /* IT11 */
    FDCAN1_IR = 1u << 8;
}

void Reset_Handler(void)
{
    volatile uint32_t *tx = MSGRAM;
    volatile uint32_t *rx = MSGRAM + 32;
    volatile uint32_t *rx_fd = MSGRAM + 64;
    uint32_t get_index;

    /* This word is intentionally outside the result markers.  SRAM survives
     * a warm reset, so it lets the host distinguish a live transceiver rail
     * transition from an unintended MCU restart. */
    RESULT[15]++;

    /* Enable the switched 5 V rail for the strict electrical-power smoke. */
    GPIOC_MODER = (GPIOC_MODER & ~(3u << (15u * 2u))) |
                  (1u << (15u * 2u));
    GPIOC_ODR |= 1u << 15;

    RESULT[0] = RESULT_TX;
    FDCAN1_CCCR = 1;             /* INIT; the model supplies CCE */
    FDCAN1_TXBC = 3u << 16;      /* three TX FIFO elements at word 0 */
    FDCAN1_TXESC = 0;             /* eight data bytes per element */
    FDCAN1_CCCR = 0;              /* enter normal operation before TX */
    tx[0] = 0x123u << 18;         /* standard ID */
    tx[1] = 8u << 16;             /* DLC 8 */
    tx[2] = 0x44332211u;
    tx[3] = 0x88776655u;
    FDCAN1_TXBAR = 1;
    while ((FDCAN1_TXBTO & 1u) == 0) {
    }

    FDCAN1_CCCR = 1;              /* re-enter INIT to configure RX */
    FDCAN1_RXF0C = 32u | (3u << 16); /* RX FIFO at word 32 */
    FDCAN1_RXESC = 0;
    /* Two exact standard-ID filters at message-RAM word 100.  Reject
     * non-matching standard/extended and remote frames, matching the board's
     * HAL_FDCAN_ConfigGlobalFilter() policy. */
    FDCAN1_SIDFC = 100u | (2u << 16);
    MSGRAM[100] = (2u << 30) | (1u << 27) | (0x321u << 16) | 0x7ffu;
    MSGRAM[101] = (2u << 30) | (1u << 27) | (0x456u << 16) | 0x7ffu;
    FDCAN1_GFC = (2u << 4) | (2u << 2) | (1u << 1) | 1u;
    FDCAN1_IE = 1;                    /* RF0N notification */
    FDCAN1_ILE = 1;                   /* interrupt line 0 */
    FDCAN1_CCCR = 0;
    NVIC_ISER0 = 1u << 19;
    while ((FDCAN1_RXF0S & 0x7fu) == 0) {
    }
    RESULT[1] = rx[0];
    RESULT[2] = rx[1];
    RESULT[3] = rx[2];
    RESULT[4] = rx[3];
    get_index = (FDCAN1_RXF0S >> 8) & 0x3f;
    FDCAN1_RXF0A = get_index;
    RESULT[5] = RESULT_RX;
    FDCAN1_RXF0C = 64u | (3u << 16); /* move FIFO for a 64-byte FD element */
    FDCAN1_RXESC = 7;                 /* 64 data bytes per element */
    while ((FDCAN1_RXF0S & 0x7fu) == 0) {
    }
    RESULT[6] = rx_fd[2];             /* first four FD data bytes */
    RESULT[7] = rx_fd[17];            /* last four FD data bytes */
    RESULT[8] = FDCAN1_RXF0S;
    get_index = (FDCAN1_RXF0S >> 8) & 0x3f;
    FDCAN1_RXF0A = get_index;

    /* Deliberately use an 8-byte TX element with a 64-byte DLC.  The
     * peripheral must not read the following element while constructing the
     * host frame; this catches message-element boundary regressions. */
    FDCAN1_CCCR = 1;
    FDCAN1_TXESC = 0;
    tx[4] = 0x456u << 18;
    tx[5] = (1u << 21) | (1u << 20) | (15u << 16);
                                    /* CAN-FD + BRS, DLC 15, 64 bytes */
    tx[6] = 0x04030201u;
    tx[7] = 0x08070605u;
    tx[8] = 0xa5a5a5a5u;          /* next element must not be read */
    tx[9] = 0x5a5a5a5au;
    FDCAN1_CCCR = 0;
    FDCAN1_TXBAR = 1u << 1;
    while ((FDCAN1_TXBTO & (1u << 1)) == 0) {
    }

    /* Reuse the first standard filter as a priority-only filter.  SFEC=4
     * reports the hit through HPMS/HPM but deliberately does not consume RX
     * FIFO space. */
    FDCAN1_CCCR = 1;
    MSGRAM[100] = (2u << 30) | (4u << 27) | (0x321u << 16) | 0x7ffu;
    FDCAN1_IR = 0xffffffffu;
    FDCAN1_IE |= 1u << 8;          /* HPM notification */
    FDCAN1_ILS = 1u << 8;          /* route HPM to interrupt line 1 */
    FDCAN1_ILE = 3u;
    FDCAN1_CCCR = 0;
    NVIC_ISER0 = 1u << 21;
    RESULT[10] = 0x48504d31u;      /* host may now inject the priority frame */
    while (RESULT[11] != 0x49543131u) {
        __asm__ volatile ("wfi" ::: "memory");
    }

    /* Wait for the host to arm the switched-rail transition with one FIFO
     * frame.  This keeps the host's pre-transition observation deterministic. */
    while ((FDCAN1_RXF0S & 0x7fu) == 0) {
        __asm__ volatile ("wfi" ::: "memory");
    }
    get_index = (FDCAN1_RXF0S >> 8) & 0x3f;
    FDCAN1_RXF0A = get_index;

    /* Toggle only the switched CAN/RS485 5 V rail.  VIN remains valid, so the
     * MCU and FDCAN register state must survive this transition. */
    GPIOC_ODR &= ~(1u << 15);
    RESULT[12] = 0x354f4646u;      /* 5V off */
    for (volatile uint32_t delay = 0; delay < 10000000u; ++delay) {
        __asm__ volatile ("nop" ::: "memory");
    }
    GPIOC_ODR |= 1u << 15;
    RESULT[13] = 0x354f4e31u;      /* 5V on */
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
