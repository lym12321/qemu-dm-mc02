#include <stdint.h>

#define TIM8_CR1        (*(volatile uint32_t *)0x40010400u)
#define TIM8_DIER       (*(volatile uint32_t *)0x4001040cu)
#define TIM8_ARR        (*(volatile uint32_t *)0x4001042cu)
#define TIM8_CCR1       (*(volatile uint32_t *)0x40010434u)
#define DMA2_S6CR       (*(volatile uint32_t *)0x400204a0u)
#define DMA2_S6NDTR     (*(volatile uint32_t *)0x400204a4u)
#define DMA2_S6PAR      (*(volatile uint32_t *)0x400204a8u)
#define DMA2_S6M0AR     (*(volatile uint32_t *)0x400204acu)
#define DMAMUX1_C14CR   (*(volatile uint32_t *)0x40020838u)
#define RESULT          ((volatile uint32_t *)0x20000000u)
#define WAVEFORM        ((volatile uint16_t *)0x30000100u)

#define TIM_CR1_CEN     (1u << 0)
#define TIM_DIER_CC1DE  (1u << 9)
#define DMA_CR_EN       (1u << 0)
#define DMA_CR_DIR_M2P  (1u << 6)
#define DMA_CR_CIRC     (1u << 8)
#define DMA_CR_MINC     (1u << 10)
#define DMA_CR_PSIZE_16 (1u << 11)
#define DMA_CR_MSIZE_16 (1u << 13)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    RESULT[0] = 0x54384432u; /* T8D2 */
    WAVEFORM[0] = 168;
    WAVEFORM[1] = 84;
    WAVEFORM[2] = 168;
    WAVEFORM[3] = 84;
    RESULT[5] = WAVEFORM[0] | ((uint32_t)WAVEFORM[1] << 16);
    RESULT[6] = WAVEFORM[2] | ((uint32_t)WAVEFORM[3] << 16);

    /* TIM8_CH1 -> DMAMUX1 channel 14 -> DMA2 Stream6. */
    DMAMUX1_C14CR = 47;
    DMA2_S6NDTR = 4;
    DMA2_S6PAR = 0x40010434u;
    DMA2_S6M0AR = (uint32_t)(uintptr_t)WAVEFORM;
    DMA2_S6CR = DMA_CR_EN | DMA_CR_DIR_M2P | DMA_CR_CIRC | DMA_CR_MINC |
                DMA_CR_PSIZE_16 | DMA_CR_MSIZE_16;
    TIM8_ARR = 299;
    TIM8_DIER = TIM_DIER_CC1DE;
    TIM8_CR1 = TIM_CR1_CEN;

    RESULT[1] = DMA2_S6NDTR;
    RESULT[2] = DMA2_S6CR;
    RESULT[3] = DMA2_S6PAR;
    RESULT[4] = DMA2_S6M0AR;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
