#include <stdint.h>

#define USART1_CR1       (*(volatile uint32_t *)0x40011000u)
#define USART1_CR3       (*(volatile uint32_t *)0x40011008u)
#define USART1_RDR       (*(volatile uint32_t *)0x40011024u)
#define DMA1_S0CR        (*(volatile uint32_t *)0x40020010u)
#define DMA1_S0NDTR      (*(volatile uint32_t *)0x40020014u)
#define DMA1_S0PAR       (*(volatile uint32_t *)0x40020018u)
#define DMA1_S0M0AR      (*(volatile uint32_t *)0x4002001cu)
#define DMA1_S1CR        (*(volatile uint32_t *)0x40020028u)
#define DMA1_S1NDTR      (*(volatile uint32_t *)0x4002002cu)
#define DMA1_S1PAR       (*(volatile uint32_t *)0x40020030u)
#define DMA1_S1M0AR      (*(volatile uint32_t *)0x40020034u)
#define DMAMUX1_C0CR     (*(volatile uint32_t *)0x40020800u)
#define DMAMUX1_C1CR     (*(volatile uint32_t *)0x40020804u)
#define RESULT           ((volatile uint32_t *)0x20000000u)

#define USART_CR1_RE     (1u << 2)
#define USART_CR1_TE     (1u << 3)
#define USART_CR3_DMAR   (1u << 6)
#define USART_CR3_DMAT   (1u << 7)
#define DMA_CR_EN        (1u << 0)
#define DMA_CR_DIR_M2P   (1u << 6)
#define DMA_CR_MINC      (1u << 10)
#define DMA_CR_PL_HIGH   (3u << 16)
#define ARB_TX_READY     0x41524231u
#define ARB_RX_READY     0x41524232u
#define ARB_DONE         0x41524233u

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

/* Keep both competing sources in guest RAM so the DMA path is exercised for
 * both streams instead of being reduced to a register-only test. */
__attribute__((section(".dma_buffer")))
static uint8_t tx_stream0[] = { 'A', 'B' };
__attribute__((section(".dma_buffer")))
static uint8_t tx_stream1[] = { 'C', 'D' };
__attribute__((section(".dma_buffer")))
static volatile uint8_t rx_stream0[2];
__attribute__((section(".dma_buffer")))
static volatile uint8_t rx_stream1[2];

static void configure_tx(void)
{
    DMAMUX1_C0CR = 42;
    DMAMUX1_C1CR = 42;
    DMA1_S0NDTR = 2;
    DMA1_S0PAR = 0x40011028u;
    DMA1_S0M0AR = (uint32_t)(uintptr_t)tx_stream0;
    /* Stream 1 wins even though stream 0 has the lower number. */
    DMA1_S0CR = DMA_CR_EN | DMA_CR_DIR_M2P | DMA_CR_MINC;
    DMA1_S1NDTR = 2;
    DMA1_S1PAR = 0x40011028u;
    DMA1_S1M0AR = (uint32_t)(uintptr_t)tx_stream1;
    DMA1_S1CR = DMA_CR_EN | DMA_CR_DIR_M2P | DMA_CR_MINC | DMA_CR_PL_HIGH;
}

static void configure_rx(void)
{
    DMA1_S0CR = 0;
    DMA1_S1CR = 0;
    DMAMUX1_C0CR = 41;
    DMAMUX1_C1CR = 41;
    DMA1_S0NDTR = 2;
    DMA1_S0PAR = (uint32_t)(uintptr_t)&USART1_RDR;
    DMA1_S0M0AR = (uint32_t)(uintptr_t)rx_stream0;
    DMA1_S0CR = DMA_CR_EN | DMA_CR_MINC;
    DMA1_S1NDTR = 2;
    DMA1_S1PAR = (uint32_t)(uintptr_t)&USART1_RDR;
    DMA1_S1M0AR = (uint32_t)(uintptr_t)rx_stream1;
    DMA1_S1CR = DMA_CR_EN | DMA_CR_MINC | DMA_CR_PL_HIGH;
}

void Reset_Handler(void)
{
    unsigned timeout;

    for (unsigned i = 0; i < 8; ++i) {
        RESULT[i] = 0;
    }
    USART1_CR1 = USART_CR1_RE | USART_CR1_TE;
    configure_tx();
    USART1_CR3 = USART_CR3_DMAT;
    RESULT[0] = ARB_TX_READY;

    timeout = 1000000u;
    while (timeout-- && (DMA1_S0NDTR || DMA1_S1NDTR)) {
    }
    if (DMA1_S0NDTR || DMA1_S1NDTR) {
        RESULT[7] = 0x54494d45u; /* TIME */
        for (;;) {
            __asm__ volatile ("wfi" ::: "memory");
        }
    }

    configure_rx();
    USART1_CR3 = USART_CR3_DMAR;
    RESULT[1] = ARB_RX_READY;
    for (;;) {
        if (!DMA1_S0NDTR && !DMA1_S1NDTR) {
            RESULT[3] = (uint32_t)rx_stream0[0] |
                        ((uint32_t)rx_stream0[1] << 8);
            RESULT[4] = (uint32_t)rx_stream1[0] |
                        ((uint32_t)rx_stream1[1] << 8);
            RESULT[2] = ARB_DONE;
            break;
        }
    }
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
