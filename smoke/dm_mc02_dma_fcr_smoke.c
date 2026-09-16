#include <stdint.h>

#define DMA1_LISR       (*(volatile uint32_t *)0x40020000u)
#define DMA1_LIFCR      (*(volatile uint32_t *)0x40020008u)
#define DMA1_S0CR       (*(volatile uint32_t *)0x40020010u)
#define DMA1_S0NDTR     (*(volatile uint32_t *)0x40020014u)
#define DMA1_S0PAR      (*(volatile uint32_t *)0x40020018u)
#define DMA1_S0M0AR     (*(volatile uint32_t *)0x4002001cu)
#define DMA1_S0FCR      (*(volatile uint32_t *)0x40020024u)
#define DMA1_S1CR       (*(volatile uint32_t *)0x40020028u)
#define DMA1_S1NDTR     (*(volatile uint32_t *)0x4002002cu)
#define DMA1_S1PAR      (*(volatile uint32_t *)0x40020030u)
#define DMA1_S1M0AR     (*(volatile uint32_t *)0x40020034u)
#define DMA1_S1FCR      (*(volatile uint32_t *)0x4002003cu)
#define DMA1_S2CR       (*(volatile uint32_t *)0x40020040u)
#define DMA1_S2NDTR     (*(volatile uint32_t *)0x40020044u)
#define DMA1_S2PAR      (*(volatile uint32_t *)0x40020048u)
#define DMA1_S2M0AR     (*(volatile uint32_t *)0x4002004cu)
#define DMA1_S2FCR      (*(volatile uint32_t *)0x40020054u)
#define DMAMUX1_C0CR    (*(volatile uint32_t *)0x40020800u)
#define DMAMUX1_C1CR    (*(volatile uint32_t *)0x40020804u)
#define DMAMUX1_C2CR    (*(volatile uint32_t *)0x40020808u)
#define USART1_CR1      (*(volatile uint32_t *)0x40011000u)
#define USART1_CR3      (*(volatile uint32_t *)0x40011008u)
#define NVIC_ISER0      (*(volatile uint32_t *)0xe000e100u)
#define RESULT          ((volatile uint32_t *)0x20000000u)

#define USART_CR1_TE    (1u << 3)
#define USART_CR1_RE    (1u << 2)
#define USART_CR3_DMAT  (1u << 7)
#define USART_CR3_DMAR  (1u << 6)
#define DMA_CR_EN       (1u << 0)
#define DMA_CR_DMEIE    (1u << 1)
#define DMA_CR_DIR_M2P  (1u << 6)
#define DMA_CR_CIRC     (1u << 8)
#define DMA_CR_MINC     (1u << 10)
#define DMA_CR_PSIZE_8  (0u << 11)
#define DMA_CR_MSIZE_16 (1u << 13)
#define DMA_CR_S0_DMEIF (1u << 2)
#define DMA_IRQ         11u
#define IRQ_MARKER      0x49525146u /* IRQF */

void Reset_Handler(void);
void DMA1_Stream0_IRQHandler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[16u + DMA_IRQ + 1u] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [16u + DMA_IRQ] = (uint32_t)(uintptr_t)DMA1_Stream0_IRQHandler,
};

void DMA1_Stream0_IRQHandler(void)
{
    RESULT[10] = IRQ_MARKER;
    RESULT[11] = DMA1_LISR;
    DMA1_LIFCR = DMA_CR_S0_DMEIF;
}

void Reset_Handler(void)
{
    volatile uint16_t *source = (volatile uint16_t *)0x20000300u;
    unsigned timeout = 1000000u;

    source[0] = 0x0141u;
    source[1] = 0x0242u;
    source[2] = 0x0343u;
    for (unsigned i = 0; i < 24; ++i) {
        RESULT[i] = 0;
    }
    RESULT[0] = 0x46435231u; /* FCR1 */

    /* FTH, DMDIS and FEIE are writable.  FS and all reserved bits must not
     * survive a register write. */
    DMA1_S0FCR = 0xffffffffu;
    RESULT[1] = DMA1_S0FCR;
    DMA1_S0FCR = 0;
    DMA1_S1FCR = (1u << 0) | (1u << 2) | (7u << 3) |
                 (1u << 7) | (1u << 8) | (1u << 31);
    RESULT[2] = DMA1_S1FCR;

    /* Stream0 is direct mode with PSIZE=8 and MSIZE=16.  DMEIE starts
     * disabled, proving that the error flag is independent of its gate. */
    DMAMUX1_C0CR = 42;
    DMA1_S0NDTR = 1;
    DMA1_S0PAR = 0x40011028u;
    DMA1_S0M0AR = (uint32_t)(uintptr_t)source;
    DMA1_S0CR = DMA_CR_EN | DMA_CR_DIR_M2P | DMA_CR_MINC |
                DMA_CR_PSIZE_8 | DMA_CR_MSIZE_16;

    /* Stream1 uses FIFO mode for the same request and converts 16-bit memory
     * items into 8-bit UART TDR writes. */
    DMAMUX1_C1CR = 42;
    DMA1_S1NDTR = 3;
    DMA1_S1PAR = 0x40011028u;
    DMA1_S1M0AR = (uint32_t)(uintptr_t)source;
    DMA1_S1CR = DMA_CR_EN | DMA_CR_DIR_M2P | DMA_CR_MINC |
                DMA_CR_PSIZE_8 | DMA_CR_MSIZE_16;
    DMA1_S1FCR = (1u << 0) | (1u << 2) | (1u << 7);

    /* Stream2 exercises the reverse FIFO direction.  Four UART bytes are
     * accumulated as two 16-bit memory beats at the quarter-full threshold. */
    DMAMUX1_C2CR = 41;
    DMA1_S2NDTR = 4;
    DMA1_S2PAR = 0x40011024u;
    DMA1_S2M0AR = 0x20000400u;
    DMA1_S2CR = DMA_CR_EN | DMA_CR_CIRC | DMA_CR_MINC | DMA_CR_PSIZE_8 |
                DMA_CR_MSIZE_16;
    DMA1_S2FCR = (1u << 2);

    NVIC_ISER0 = 1u << DMA_IRQ;
    USART1_CR1 = USART_CR1_TE | USART_CR1_RE;
    USART1_CR3 = USART_CR3_DMAT | USART_CR3_DMAR;

    while ((DMA1_S1NDTR || !(DMA1_LISR & (1u << 21))) && --timeout) {
        __asm__ volatile ("nop" ::: "memory");
    }
    RESULT[3] = DMA1_LISR;
    RESULT[4] = DMA1_S0CR;
    RESULT[5] = DMA1_S1NDTR;
    RESULT[6] = DMA1_S1CR;
    RESULT[7] = RESULT[10];

    /* Enabling DMEIE exposes the latched error; the handler acknowledges it
     * with the stream's write-one-to-clear flag register. */
    DMA1_S0CR = DMA_CR_DMEIE | DMA_CR_DIR_M2P | DMA_CR_MINC |
                DMA_CR_PSIZE_8 | DMA_CR_MSIZE_16;
    RESULT[8] = DMA1_LISR;
    RESULT[9] = DMA1_S0CR;
    RESULT[12] = DMA1_LISR;
    RESULT[13] = *(volatile uint32_t *)0x40011028u;
    RESULT[14] = DMA1_LISR;
    RESULT[15] = DMA1_S2NDTR;
    RESULT[16] = DMA1_S2CR;
    RESULT[17] = DMA1_S2FCR;
    RESULT[18] = *(volatile uint32_t *)0x20000400u;
    RESULT[19] = *(volatile uint32_t *)0x20000404u;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
