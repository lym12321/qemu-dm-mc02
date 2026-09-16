#include <stdint.h>

#ifndef ADC_TRIGGER_MODE
#define ADC_TRIGGER_MODE 0
#endif

#define ADC1_CR          (*(volatile uint32_t *)0x40022008u)
#define ADC1_CFGR        (*(volatile uint32_t *)0x4002200cu)
#define ADC1_SQR1        (*(volatile uint32_t *)0x40022030u)
#define DMA1_S2CR        (*(volatile uint32_t *)0x40020040u)
#define DMA1_S2NDTR      (*(volatile uint32_t *)0x40020044u)
#define DMA1_S2PAR       (*(volatile uint32_t *)0x40020048u)
#define DMA1_S2M0AR      (*(volatile uint32_t *)0x4002004cu)
#define DMAMUX1_C2CR     (*(volatile uint32_t *)0x40020808u)
#define TIM8_CR1        (*(volatile uint32_t *)0x40010400u)
#define TIM8_CR2        (*(volatile uint32_t *)0x40010404u)
#define TIM8_EGR        (*(volatile uint32_t *)0x40010414u)
#define TIM8_CNT        (*(volatile uint32_t *)0x40010424u)
#define TIM8_PSC        (*(volatile uint32_t *)0x40010428u)
#define TIM8_ARR        (*(volatile uint32_t *)0x4001042cu)
#define TIM8_CCR1       (*(volatile uint32_t *)0x40010434u)
#define TIM3_CR1        (*(volatile uint32_t *)0x40000400u)
#define TIM3_CR2        (*(volatile uint32_t *)0x40000404u)
#define TIM3_CCMR2      (*(volatile uint32_t *)0x4000041cu)
#define TIM3_ARR        (*(volatile uint32_t *)0x4000042cu)
#define TIM3_CCR4       (*(volatile uint32_t *)0x40000440u)
#define RESULT           ((volatile uint32_t *)0x20000000u)
#define SAMPLES          ((volatile uint16_t *)0x20000100u)

#define ADC_CR_ADEN      (1u << 0)
#define ADC_CR_ADSTART   (1u << 2)
#define ADC_CR_ADCAL     (1u << 31)
#define ADC_CFGR_DMAEN   (1u << 0)
#define ADC_CFGR_CONT    (1u << 13)
#define ADC_CFGR_EXTSEL_SHIFT 5
#define ADC_CFGR_EXTEN_RISING (1u << 10)
#define ADC_CFGR_EXTEN_FALLING (2u << 10)
#define ADC_TRIGGER_TIM8_TRGO (7u << ADC_CFGR_EXTSEL_SHIFT)
#define ADC_TRIGGER_TIM8_TRGO2 (8u << ADC_CFGR_EXTSEL_SHIFT)
#define ADC_TRIGGER_TIM3_CH4 (15u << ADC_CFGR_EXTSEL_SHIFT)
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
    RESULT[0] = 0x41445431u; /* "ADT1" */
    for (unsigned i = 0; i < 4; ++i) {
        SAMPLES[i] = 0xa5a5u;
    }

    /* Two ranks make each DMA half visibly identify one virtual sequence. */
    ADC1_SQR1 = 1u | (4u << 6) | (19u << 12);
    if (ADC_TRIGGER_MODE < 2 || ADC_TRIGGER_MODE == 10) {
        DMAMUX1_C2CR = 9;
        DMA1_S2NDTR = 4;
        DMA1_S2PAR = 0x40022040u;
        DMA1_S2M0AR = (uint32_t)(uintptr_t)SAMPLES;
        DMA1_S2CR = DMA_CR_EN | DMA_CR_CIRC | DMA_CR_MINC |
                    DMA_CR_PSIZE_16 | DMA_CR_MSIZE_16;
    }

    ADC1_CFGR = (ADC_TRIGGER_MODE == 2 || ADC_TRIGGER_MODE == 6 ||
                 ADC_TRIGGER_MODE == 7 || ADC_TRIGGER_MODE == 8 ?
                 ADC_CFGR_EXTEN_RISING | ADC_CFGR_DMAEN |
                 ADC_TRIGGER_TIM8_TRGO :
                 (ADC_TRIGGER_MODE == 9 ? ADC_CFGR_EXTEN_RISING |
                  ADC_CFGR_DMAEN | ADC_TRIGGER_TIM8_TRGO2 :
                 (ADC_TRIGGER_MODE == 10 ? ADC_CFGR_EXTEN_RISING |
                  ADC_CFGR_DMAEN | ADC_TRIGGER_TIM3_CH4 :
                 (ADC_TRIGGER_MODE == 4 ? ADC_CFGR_EXTEN_RISING |
                  ADC_CFGR_DMAEN :
                 (ADC_TRIGGER_MODE == 5 ? ADC_CFGR_EXTEN_FALLING |
                  ADC_CFGR_DMAEN | ADC_TRIGGER_TIM8_TRGO :
                 (ADC_TRIGGER_MODE == 3 ? 0u :
                 ADC_CFGR_DMAEN |
                 (ADC_TRIGGER_MODE == 1 ? ADC_CFGR_CONT : 0u)))))));
    if (ADC_TRIGGER_MODE == 3) {
        /* Calibration must be started while ADC is disabled. */
        ADC1_CR = ADC_CR_ADCAL;
        RESULT[7] = ADC1_CR;
        while (ADC1_CR & ADC_CR_ADCAL) {
            __asm__ volatile ("nop" ::: "memory");
        }
        RESULT[8] = 0x43414c31u; /* ADCAL completed */
        ADC1_CFGR = ADC_CFGR_DMAEN;
        DMAMUX1_C2CR = 9;
        DMA1_S2NDTR = 4;
        DMA1_S2PAR = 0x40022040u;
        DMA1_S2M0AR = (uint32_t)(uintptr_t)SAMPLES;
        DMA1_S2CR = DMA_CR_EN | DMA_CR_CIRC | DMA_CR_MINC |
                    DMA_CR_PSIZE_16 | DMA_CR_MSIZE_16;
    }
    if (ADC_TRIGGER_MODE == 0 || ADC_TRIGGER_MODE == 1 ||
        ADC_TRIGGER_MODE == 3) {
        ADC1_CR = ADC_CR_ADEN;
        ADC1_CR = ADC_CR_ADEN | ADC_CR_ADSTART;
    }

    if (ADC_TRIGGER_MODE == 2 || ADC_TRIGGER_MODE == 4 ||
        ADC_TRIGGER_MODE == 5 || ADC_TRIGGER_MODE == 6 ||
        ADC_TRIGGER_MODE == 7 || ADC_TRIGGER_MODE == 8 ||
        ADC_TRIGGER_MODE == 9) {
        /* Configure the DMA sink before enabling the only conversion source. */
        TIM8_PSC = 239u;
        TIM8_ARR = 99u;
        TIM8_CR2 = ADC_TRIGGER_MODE == 6 ? 0u :
                   (ADC_TRIGGER_MODE == 7 ? (1u << 4) :
                   (ADC_TRIGGER_MODE == 8 ? (3u << 4) :
                   (ADC_TRIGGER_MODE == 9 ? (2u << 20) :
                   (2u << 4))));
        TIM8_CCR1 = ADC_TRIGGER_MODE == 8 ? 50u : 0u;
        DMA1_S2NDTR = 4;
        DMAMUX1_C2CR = 9;
        DMA1_S2PAR = 0x40022040u;
        DMA1_S2M0AR = (uint32_t)(uintptr_t)SAMPLES;
        DMA1_S2CR = DMA_CR_EN | DMA_CR_CIRC | DMA_CR_MINC |
                    DMA_CR_PSIZE_16 | DMA_CR_MSIZE_16;
        RESULT[8] = 0x444d4131u; /* DMA setup precedes TIM8 start */
        ADC1_CR = ADC_CR_ADEN;
        ADC1_CR = ADC_CR_ADEN | ADC_CR_ADSTART;
        if (ADC_TRIGGER_MODE == 6) {
            TIM8_EGR = 1u; /* MMS=reset: UG is the master trigger. */
        } else {
            TIM8_CR1 = 1u;
        }
        RESULT[7] = 0x54494d31u; /* TIM8 is now the conversion source */
        RESULT[9] = ADC_TRIGGER_MODE == 9 ? 0x45585438u :
                    0x45585437u; /* TRGO/TRGO2 source */
        RESULT[10] = TIM8_CR2;
        RESULT[11] = TIM8_EGR;
    }

    if (ADC_TRIGGER_MODE == 10) {
        /* TIM3 MMS=111 exposes OC4REF as the physical TIM3_CH4 trigger. */
        TIM3_CCMR2 = 6u << 12; /* PWM1 on channel 4 */
        TIM3_ARR = 31u;
        TIM3_CCR4 = 8u;
        TIM3_CR2 = 7u << 4;
        RESULT[8] = 0x444d4133u; /* DMA setup precedes TIM3 start */
        ADC1_CR = ADC_CR_ADEN;
        ADC1_CR = ADC_CR_ADEN | ADC_CR_ADSTART;
        TIM3_CR1 = 1u;
        RESULT[7] = 0x54494d33u; /* TIM3 is now the conversion source */
        RESULT[9] = 0x54433434u; /* TIM3_CH4 source */
        RESULT[10] = TIM3_CR2;
        RESULT[11] = TIM3_CCMR2;
    }

    RESULT[1] = ADC_TRIGGER_MODE;
    RESULT[2] = ADC1_CR;
    RESULT[3] = ADC1_CFGR;
    RESULT[4] = ADC1_SQR1;
    RESULT[5] = DMA1_S2NDTR;
    RESULT[6] = 0x544f4b31u; /* startup configuration is complete */
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
