#include <stdint.h>

#define DMA1_LISR   (*(volatile uint32_t *)0x40020000u)
#define DMA1_LIFCR  (*(volatile uint32_t *)0x40020008u)
#define DMA1_S0CR   (*(volatile uint32_t *)0x40020010u)
#define DMA1_S0NDTR (*(volatile uint32_t *)0x40020014u)
#define DMA1_S0PAR  (*(volatile uint32_t *)0x40020018u)
#define DMA1_S0M0AR (*(volatile uint32_t *)0x4002001cu)
#define NVIC_ISER0  (*(volatile uint32_t *)0xe000e100u)
#define RESULT      ((volatile uint32_t *)0x20000000u)

#define DMA_CR_EN       (1u << 0)
#define DMA_CR_TCIE     (1u << 4)
#define DMA_CR_DIR_M2M  (2u << 6)
#define DMA_CR_PINC     (1u << 9)
#define DMA_CR_MINC     (1u << 10)
#define DMA_LISR_S0_TCIF (1u << 5)
#define DMA_LISR_S0_HTIF (1u << 4)

#define IRQ_DMA1_STREAM0 11u
#define IRQ_MARKER       0x49525131u /* "IRQ1" */

void Reset_Handler(void);
void DMA1_Stream0_IRQHandler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[28] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [16u + IRQ_DMA1_STREAM0] = (uint32_t)(uintptr_t)DMA1_Stream0_IRQHandler,
};

void DMA1_Stream0_IRQHandler(void)
{
    /* Capture evidence that both completion status events are present, then
     * acknowledge the level-sensitive DMA interrupt at its source. */
    RESULT[3] = IRQ_MARKER;
    RESULT[4] = DMA1_LISR;
    DMA1_LIFCR = DMA_LISR_S0_TCIF | DMA_LISR_S0_HTIF;
}

void Reset_Handler(void)
{
    volatile uint32_t *source = (volatile uint32_t *)0x20001000u;
    volatile uint32_t *target = (volatile uint32_t *)0x20002000u;

    source[0] = 0x12345678u;
    source[1] = 0x9abcdef0u;
    target[0] = 0;
    target[1] = 0;

    RESULT[0] = 0x44495251u; /* "DIRQ" */
    RESULT[3] = 0;
    RESULT[4] = 0;
    RESULT[5] = 0;
    RESULT[6] = 0;

    /* IRQ 11 is DMA1 Stream0 on STM32H7. */
    NVIC_ISER0 = 1u << IRQ_DMA1_STREAM0;

    DMA1_S0NDTR = 2;
    DMA1_S0PAR = (uint32_t)(uintptr_t)source;
    DMA1_S0M0AR = (uint32_t)(uintptr_t)target;
    DMA1_S0CR = DMA_CR_EN | DMA_CR_TCIE | DMA_CR_DIR_M2M |
                DMA_CR_PINC | DMA_CR_MINC | (2u << 11) | (2u << 13);

    /* The DMA operation and IRQ handler are synchronous from the guest's
     * point of view; these reads provide the post-handler observation. */
    RESULT[1] = target[0];
    RESULT[2] = target[1];
    RESULT[5] = DMA1_LISR;
    RESULT[6] = RESULT[3];
    RESULT[7] = DMA1_LISR;
    RESULT[8] = DMA1_S0NDTR;
    RESULT[9] = DMA1_S0CR;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
