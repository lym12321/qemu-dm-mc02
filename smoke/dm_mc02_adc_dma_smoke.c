#include <stdint.h>

#define ADC1_CR          (*(volatile uint32_t *)0x40022008u)
#define ADC1_CFGR        (*(volatile uint32_t *)0x4002200cu)
#define DMA1_LISR        (*(volatile uint32_t *)0x40020000u)
#define DMA1_S2CR        (*(volatile uint32_t *)0x40020040u)
#define DMA1_S2NDTR      (*(volatile uint32_t *)0x40020044u)
#define DMA1_S2PAR       (*(volatile uint32_t *)0x40020048u)
#define DMA1_S2M0AR      (*(volatile uint32_t *)0x4002004cu)
#define DMAMUX1_C2CR     (*(volatile uint32_t *)0x40020808u)
#define RESULT           ((volatile uint32_t *)0x20000000u)
#define SAMPLES          ((volatile uint16_t *)0x20000100u)

#define ADC_CR_ADEN      (1u << 0)
#define ADC_CR_ADSTART   (1u << 2)
#define ADC_CFGR_DMAEN   (1u << 0)
#define ADC_CFGR_CONT    (1u << 13)
#define DMA_CR_EN        (1u << 0)
#define DMA_CR_CIRC      (1u << 8)
#define DMA_CR_MINC      (1u << 10)
#define DMA_CR_PSIZE_16  (1u << 11)
#define DMA_CR_MSIZE_16  (1u << 13)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    RESULT[0] = 0x41444331u; /* ADC1 */
    for (unsigned i = 0; i < 8; ++i) {
        SAMPLES[i] = 0xa5a5u;
    }

    /* ADC1 -> DMAMUX1 request 9, DMA1 Stream2, two half-word ranks. */
    DMAMUX1_C2CR = 9;
    DMA1_S2NDTR = 4;
    DMA1_S2PAR = 0x40022040u;
    DMA1_S2M0AR = (uint32_t)(uintptr_t)SAMPLES;
    DMA1_S2CR = DMA_CR_EN | DMA_CR_CIRC | DMA_CR_MINC |
                DMA_CR_PSIZE_16 | DMA_CR_MSIZE_16;

    ADC1_CFGR = ADC_CFGR_DMAEN | ADC_CFGR_CONT;
    ADC1_CR = ADC_CR_ADEN;
    ADC1_CR = ADC_CR_ADEN | ADC_CR_ADSTART;

    RESULT[1] = DMA1_S2NDTR;
    RESULT[2] = DMA1_S2CR;
    RESULT[3] = DMA1_S2PAR;
    RESULT[4] = DMA1_S2M0AR;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}

