#include <stdint.h>

#define DMA1_LISR   (*(volatile uint32_t *)0x40020000u)
#define DMA1_LIFCR  (*(volatile uint32_t *)0x40020008u)
#define DMA1_S0CR   (*(volatile uint32_t *)0x40020010u)
#define DMA1_S0NDTR (*(volatile uint32_t *)0x40020014u)
#define DMA1_S0PAR  (*(volatile uint32_t *)0x40020018u)
#define DMA1_S0M0AR (*(volatile uint32_t *)0x4002001cu)
#define DMA1_S1CR   (*(volatile uint32_t *)0x40020028u)
#define DMA1_S1NDTR (*(volatile uint32_t *)0x4002002cu)
#define DMA1_S1PAR  (*(volatile uint32_t *)0x40020030u)
#define DMA1_S1M0AR (*(volatile uint32_t *)0x40020034u)
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
    volatile uint32_t *source32 = (volatile uint32_t *)0x20000100u;
    volatile uint32_t *dest32 = (volatile uint32_t *)0x20000200u;
    volatile uint8_t *source8 = (volatile uint8_t *)0x20000300u;
    volatile uint16_t *dest16 = (volatile uint16_t *)0x20000400u;

    source32[0] = 0x11223344u;
    source32[1] = 0x55667788u;
    source32[2] = 0x99aabbccu;
    source32[3] = 0xddeeff00u;
    dest32[0] = dest32[1] = dest32[2] = dest32[3] = 0;
    source8[0] = 0x11;
    source8[1] = 0x22;
    source8[2] = 0x33;
    dest16[0] = dest16[1] = dest16[2] = 0;

    RESULT[0] = 0x444d4131u; /* DMA1 */

    DMA1_S0NDTR = 4;
    DMA1_S0PAR = (uint32_t)(uintptr_t)source32;
    DMA1_S0M0AR = (uint32_t)(uintptr_t)dest32;
    DMA1_S0CR = DMA_CR_EN | DMA_CR_DIR_M2M | DMA_CR_PINC | DMA_CR_MINC |
                (2u << 11) | (2u << 13);

    RESULT[1] = dest32[0];
    RESULT[2] = dest32[1];
    RESULT[3] = dest32[2];
    RESULT[4] = dest32[3];
    RESULT[5] = DMA1_S0NDTR;
    RESULT[6] = DMA1_LISR;
    RESULT[7] = DMA1_S0CR;
    RESULT[8] = DMA1_S0PAR;
    RESULT[9] = DMA1_S0M0AR;

    DMA1_S1NDTR = 3;
    DMA1_S1PAR = (uint32_t)(uintptr_t)source8;
    DMA1_S1M0AR = (uint32_t)(uintptr_t)dest16;
    DMA1_S1CR = DMA_CR_EN | DMA_CR_DIR_M2M | DMA_CR_PINC | DMA_CR_MINC |
                (0u << 11) | (1u << 13);
    RESULT[10] = dest16[0] | ((uint32_t)dest16[1] << 16);
    RESULT[11] = dest16[2];
    RESULT[12] = DMA1_LISR;

    DMA1_LIFCR = (1u << 5) | (1u << 11);
    RESULT[13] = DMA1_LISR;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
