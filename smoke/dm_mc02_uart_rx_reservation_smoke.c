#include <stdint.h>

#define USART1_CR1       (*(volatile uint32_t *)0x40011000u)
#define USART1_CR3       (*(volatile uint32_t *)0x40011008u)
#define USART1_ISR       (*(volatile uint32_t *)0x4001101cu)
#define USART1_TDR       (*(volatile uint32_t *)0x40011028u)

#define DMA1_LISR        (*(volatile uint32_t *)0x40020000u)
#define DMA1_LIFCR       (*(volatile uint32_t *)0x40020008u)
#define DMA1_S0CR        (*(volatile uint32_t *)0x40020010u)
#define DMA1_S0NDTR      (*(volatile uint32_t *)0x40020014u)
#define DMA1_S0PAR       (*(volatile uint32_t *)0x40020018u)
#define DMA1_S0M0AR      (*(volatile uint32_t *)0x4002001cu)
#define DMAMUX1_C0CR     (*(volatile uint32_t *)0x40020800u)

#define DMA_CR_EN        (1u << 0)
#define DMA_CR_MINC      (1u << 10)
#define DMA_FLAG_TEIF_S0 (1u << 3)
#define DMAMUX_USART1_RX 41u
#define USART_CR1_RE     (1u << 2)
#define USART_CR1_TE     (1u << 3)
#define USART_CR3_DMAR   (1u << 6)
#define USART_ISR_RXNE   (1u << 5)
#define USART_ISR_TXE    (1u << 7)
#define RESULT           ((volatile uint32_t *)0x20000000u)
#define RX_BUFFER        ((volatile uint8_t *)0x20000100u)
#define DMA_DESTINATION  0x20000200u
#define DMA_INVALID_DEST 0x60000000u

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

static void send_byte(uint8_t byte)
{
    while (!(USART1_ISR & USART_ISR_TXE)) {
    }
    USART1_TDR = byte;
}

void Reset_Handler(void)
{
    RESULT[0] = 0x52454144u; /* "READ" */
    RX_BUFFER[0] = 0xa5u;

    /* Keep BRR at reset value: this selects the UART's immediate RX delivery
     * path and leaves the reservation test independent of frame scheduling. */
    USART1_CR1 = USART_CR1_RE | USART_CR1_TE;
    DMAMUX1_C0CR = DMAMUX_USART1_RX;
    DMA1_S0NDTR = 1;
    DMA1_S0PAR = 0x40011024u; /* RDR */
    DMA1_S0M0AR = DMA_INVALID_DEST;
    DMA1_S0CR = DMA_CR_EN | DMA_CR_MINC;

    /* Host sends one byte after seeing this marker. */
    send_byte('R');
    while (!(USART1_ISR & USART_ISR_RXNE)) {
    }

    /* The byte is already in the CPU-visible queue when DMAR is enabled.
     * This mirrors a normal firmware setup race while making the source
     * queue boundary deterministic for the invalid destination attempt. */
    USART1_CR3 = USART_CR3_DMAR;

    /* The failed target write must have aborted source consumption. */
    RESULT[1] = USART1_ISR;
    RESULT[2] = DMA1_S0NDTR;
    RESULT[3] = DMA1_S0CR;
    RESULT[4] = DMA1_LISR;
    send_byte('F');

    /* Clear the transfer error and retry the same source byte at valid RAM. */
    DMA1_LIFCR = DMA_FLAG_TEIF_S0;
    DMA1_S0M0AR = DMA_DESTINATION;
    DMA1_S0NDTR = 1;
    DMA1_S0CR = DMA_CR_EN | DMA_CR_MINC;

    RESULT[5] = ((volatile uint8_t *)DMA_DESTINATION)[0];
    RESULT[6] = DMA1_S0NDTR;
    RESULT[7] = USART1_ISR;
    RESULT[8] = DMA1_LISR;
    RESULT[0] = 0x444f4e45u; /* "DONE" */
    send_byte('D');
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
