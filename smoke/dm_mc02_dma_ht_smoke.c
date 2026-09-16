#include <stdint.h>

#define DMA1_LISR   (*(volatile uint32_t *)0x40020000u)
#define DMA1_S0CR   (*(volatile uint32_t *)0x40020010u)
#define DMA1_S0NDTR (*(volatile uint32_t *)0x40020014u)
#define DMA1_S0PAR  (*(volatile uint32_t *)0x40020018u)
#define DMA1_S0M0AR (*(volatile uint32_t *)0x4002001cu)
#define RESULT      ((volatile uint32_t *)0x20000000u)

#define DMA_CR_EN       (1u << 0)
#define DMA_CR_DIR_M2M  (2u << 6)
#define DMA_CR_PINC     (1u << 9)
#define DMA_CR_MINC     (1u << 10)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    volatile uint32_t *source = (volatile uint32_t *)0x20001000u;
    volatile uint32_t *target = (volatile uint32_t *)0x20002000u;

    source[0] = 0x11223344u;
    source[1] = 0x55667788u;
    source[2] = 0x99aabbccu;
    source[3] = 0xddeeff00u;
    target[0] = target[1] = target[2] = target[3] = 0;
    RESULT[0] = 0x48544631u; /* "HTF1" */
    RESULT[2] = RESULT[3] = RESULT[4] = 0;

    DMA1_S0NDTR = 4;
    DMA1_S0PAR = (uint32_t)(uintptr_t)source;
    DMA1_S0M0AR = (uint32_t)(uintptr_t)target;
    /* HTIF must be recorded independently of the interrupt enable; HTIE is
     * a separate gate for the IRQ output. */
    DMA1_S0CR = DMA_CR_EN | DMA_CR_DIR_M2M | DMA_CR_PINC | DMA_CR_MINC |
                (2u << 11) | (2u << 13);
    RESULT[1] = DMA1_LISR;
    RESULT[2] = DMA1_LISR;
    /* The runner observes the latched status; clear semantics are covered by
     * the existing DMA smoke. */
    RESULT[3] = DMA1_LISR;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
