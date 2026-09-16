#include <stdint.h>

#define USART1_CR1       (*(volatile uint32_t *)0x40011000u)
#define USART1_CR3       (*(volatile uint32_t *)0x40011008u)

#define DMA1_S0CR        (*(volatile uint32_t *)0x40020010u)
#define DMA1_S0NDTR      (*(volatile uint32_t *)0x40020014u)
#define DMA1_S0PAR       (*(volatile uint32_t *)0x40020018u)
#define DMA1_S0M0AR      (*(volatile uint32_t *)0x4002001cu)
#define DMA1_S1CR        (*(volatile uint32_t *)0x40020028u)
#define DMA1_S1NDTR      (*(volatile uint32_t *)0x4002002cu)
#define DMA1_S1PAR       (*(volatile uint32_t *)0x40020030u)
#define DMA1_S1M0AR      (*(volatile uint32_t *)0x40020034u)
#define DMA1_S1FCR       (*(volatile uint32_t *)0x4002003cu)
#define DMAMUX1_C0CR     (*(volatile uint32_t *)0x40020800u)
#define DMAMUX1_C1CR     (*(volatile uint32_t *)0x40020804u)

#define DMA_CR_EN        (1u << 0)
#define DMA_CR_DIR_M2P   (1u << 6)
#define DMA_CR_MINC      (1u << 10)
#define DMA_CR_MBURST_INCR4 (1u << 23)
#define DMA_FCR_DMDIS    (1u << 2)
#define DMA_FCR_FTH_HALF (1u << 0)
#define USART_CR1_RE     (1u << 2)
#define USART_CR1_TE     (1u << 3)
#define USART_CR3_DMAR   (1u << 6)
#define USART_CR3_DMAT   (1u << 7)
#define RESULT           ((volatile uint32_t *)0x20000000u)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    static const uint8_t tx[] = { 'D', 'M', 'A' };
    static volatile uint8_t rx[1] __attribute__((section(".dma_buffer")));
    unsigned timeout = 1000000;

    RESULT[0] = 0x55444d41u; /* "UDMA" */
    rx[0] = 0xa5u;

    /* Leave one received byte pending while DMA is being configured. */
    USART1_CR1 = USART_CR1_RE | USART_CR1_TE;
    USART1_CR3 = 0;
    *(volatile uint32_t *)0x40011028u = 'P';
    USART1_CR1 = USART_CR1_RE | USART_CR1_TE;
    while ((*(volatile uint32_t *)0x4001101cu & (1u << 5)) == 0) {
    }

    /* USART1 RX is DMAMUX request 41 / DMA1 Stream0; TX is request 42 / S1. */
    DMAMUX1_C0CR = 41;
    DMAMUX1_C1CR = 42;

    DMA1_S0NDTR = 1;
    DMA1_S0PAR = 0x40011024u; /* RDR */
    DMA1_S0M0AR = (uint32_t)(uintptr_t)rx;
    DMA1_S0CR = DMA_CR_EN | DMA_CR_MINC;

    DMA1_S1NDTR = 3;
    DMA1_S1PAR = 0x40011028u; /* TDR */
    DMA1_S1M0AR = (uint32_t)(uintptr_t)tx;
    DMA1_S1FCR = DMA_FCR_DMDIS | DMA_FCR_FTH_HALF;
    DMA1_S1CR = DMA_CR_EN | DMA_CR_DIR_M2P | DMA_CR_MINC |
                DMA_CR_MBURST_INCR4;

    USART1_CR3 = USART_CR3_DMAR | USART_CR3_DMAT;

    while (timeout-- && (DMA1_S0NDTR || DMA1_S1NDTR)) {
    }

    RESULT[1] = rx[0];
    RESULT[2] = 0;
    RESULT[3] = DMA1_S0NDTR;
    RESULT[4] = DMA1_S1NDTR;
    RESULT[5] = DMA1_S0CR;
    RESULT[6] = DMA1_S1CR;
    RESULT[7] = DMA1_S0M0AR - (uint32_t)(uintptr_t)rx;
    RESULT[8] = DMA1_S1M0AR - (uint32_t)(uintptr_t)tx;
    RESULT[9] = timeout != 0;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
