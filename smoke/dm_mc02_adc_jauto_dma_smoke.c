#include <stdint.h>

#define ADC1_ISR        (*(volatile uint32_t *)0x40022000u)
#define ADC1_CR         (*(volatile uint32_t *)0x40022008u)
#define ADC1_CFGR       (*(volatile uint32_t *)0x4002200cu)
#define ADC1_SQR1       (*(volatile uint32_t *)0x40022030u)
#define ADC1_JSQR       (*(volatile uint32_t *)0x4002204cu)
#define ADC1_JDR1       (*(volatile uint32_t *)0x40022080u)
#define ADC1_JDR2       (*(volatile uint32_t *)0x40022084u)
#define DMA1_LISR       (*(volatile uint32_t *)0x40020000u)
#define DMA1_S2CR       (*(volatile uint32_t *)0x40020040u)
#define DMA1_S2NDTR     (*(volatile uint32_t *)0x40020044u)
#define DMA1_S2PAR      (*(volatile uint32_t *)0x40020048u)
#define DMA1_S2M0AR     (*(volatile uint32_t *)0x4002004cu)
#define DMAMUX1_C2CR    (*(volatile uint32_t *)0x40020808u)

#define RESULT          ((volatile uint32_t *)0x20000000u)
#define REGULAR_DMA     ((volatile uint16_t *)0x20000100u)

#define ADC_ISR_JEOC    (1u << 5)
#define ADC_ISR_JEOS    (1u << 6)
#define ADC_CR_ADEN     (1u << 0)
#define ADC_CR_ADSTART  (1u << 2)
#define ADC_CFGR_DMAEN  (1u << 0)
#define ADC_CFGR_CONT   (1u << 13)
#define ADC_CFGR_JQM    (1u << 21)
#define ADC_CFGR_JAUTO  (1u << 25)
#define DMA_CR_EN       (1u << 0)
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
    RESULT[0] = 0x4a444d31u; /* JDM1 */
    for (unsigned i = 0; i < 4; ++i) {
        REGULAR_DMA[i] = 0xa5a5u;
    }

    /* ADC1 regular request 9 -> DMA1 Stream2.  The injected group does not
     * use DMA on H723; its JDR1/JDR2 results are the independent consumer. */
    DMAMUX1_C2CR = 9;
    DMA1_S2NDTR = 4;
    DMA1_S2PAR = 0x40022040u;
    DMA1_S2M0AR = (uint32_t)(uintptr_t)REGULAR_DMA;
    DMA1_S2CR = DMA_CR_EN | DMA_CR_CIRC | DMA_CR_MINC |
                DMA_CR_PSIZE_16 | DMA_CR_MSIZE_16;

    /* Two regular ranks and a two-rank injected context.  JQM makes the
     * context empty after the automatic injected sequence completes, while
     * CONT requires the regular DMA stream to resume for another sequence. */
    ADC1_SQR1 = 1u | (0u << 6) | (1u << 12);
    ADC1_CFGR = ADC_CFGR_DMAEN | ADC_CFGR_CONT | ADC_CFGR_JAUTO |
                ADC_CFGR_JQM;
    ADC1_JSQR = 1u | (0u << 9) | (1u << 15);
    ADC1_CR = ADC_CR_ADEN;
    ADC1_CR = ADC_CR_ADEN | ADC_CR_ADSTART;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
