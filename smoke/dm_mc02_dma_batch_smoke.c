#include <stdint.h>

#define DMA1_LISR       (*(volatile uint32_t *)0x40020000u)
#define DMA1_S1CR       (*(volatile uint32_t *)0x40020028u)
#define DMA1_S1NDTR     (*(volatile uint32_t *)0x4002002cu)
#define DMA1_S1PAR      (*(volatile uint32_t *)0x40020030u)
#define DMA1_S1M0AR     (*(volatile uint32_t *)0x40020034u)
#define DMAMUX1_C1CR    (*(volatile uint32_t *)0x40020804u)
#define USART1_CR1      (*(volatile uint32_t *)0x40011000u)
#define USART1_CR3      (*(volatile uint32_t *)0x40011008u)
#define RESULT          ((volatile uint32_t *)0x20000000u)

#define USART_CR1_TE    (1u << 3)
#define USART_CR3_DMAT  (1u << 7)
#define DMA_CR_EN       (1u << 0)
#define DMA_CR_DIR_M2P  (1u << 6)
#define DMA_CR_CIRC     (1u << 8)
#define DMA_CR_MINC     (1u << 10)
#define DMA_CR_HTIE     (1u << 3)
#define DMA_CR_TCIE     (1u << 4)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

__attribute__((section(".dma_buffer")))
static const uint8_t tx[] = { 'B', 'A', 'T', 'C' };

void Reset_Handler(void)
{
    RESULT[0] = 0x42415431u; /* BAT1 */

    /* USART1_TX is DMAMUX request 42 on DMA1 Stream1.  A four-item circular
     * transfer makes a bounded batch cross both the half and full boundaries. */
    DMAMUX1_C1CR = 42;
    DMA1_S1NDTR = 4;
    DMA1_S1PAR = 0x40011028u; /* USART1 TDR */
    DMA1_S1M0AR = (uint32_t)(uintptr_t)tx;
    DMA1_S1CR = DMA_CR_EN | DMA_CR_DIR_M2P | DMA_CR_CIRC | DMA_CR_MINC |
                DMA_CR_HTIE | DMA_CR_TCIE;
    USART1_CR1 = USART_CR1_TE;
    USART1_CR3 = USART_CR3_DMAT;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
