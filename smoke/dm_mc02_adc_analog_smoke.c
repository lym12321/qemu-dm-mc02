#include <stdint.h>

#define ADC1_CR          (*(volatile uint32_t *)0x40022008u)
#define ADC1_CFGR        (*(volatile uint32_t *)0x4002200cu)
#define ADC1_SQR1        (*(volatile uint32_t *)0x40022030u)
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
    RESULT[0] = 0x41445631u; /* "ADV1" */
    for (unsigned i = 0; i < 4; ++i) {
        SAMPLES[i] = 0xa5a5u;
    }

    /* rank 1 = VIN/channel 4, rank 2 = LCD ladder/channel 19 */
    ADC1_SQR1 = 1u | (4u << 6) | (19u << 12);
    DMAMUX1_C2CR = 9;
    DMA1_S2NDTR = 4;
    DMA1_S2PAR = 0x40022040u;
    DMA1_S2M0AR = (uint32_t)(uintptr_t)SAMPLES;
    DMA1_S2CR = DMA_CR_EN | DMA_CR_CIRC | DMA_CR_MINC |
                DMA_CR_PSIZE_16 | DMA_CR_MSIZE_16;
    ADC1_CFGR = ADC_CFGR_DMAEN | ADC_CFGR_CONT;
    ADC1_CR = ADC_CR_ADEN | ADC_CR_ADSTART;

    RESULT[1] = DMA1_S2NDTR;
    RESULT[2] = ADC1_SQR1;
    RESULT[3] = DMA1_S2PAR;
    RESULT[4] = DMA1_S2M0AR;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
