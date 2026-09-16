#include <stdint.h>

#define ADC1_ISR         (*(volatile uint32_t *)0x40022000u)
#define ADC1_CR          (*(volatile uint32_t *)0x40022008u)
#define ADC1_CFGR        (*(volatile uint32_t *)0x4002200cu)
#define ADC1_SQR1        (*(volatile uint32_t *)0x40022030u)
#define DMA1_LISR        (*(volatile uint32_t *)0x40020000u)
#define DMA1_LIFCR       (*(volatile uint32_t *)0x40020008u)
#define DMA1_S2CR        (*(volatile uint32_t *)0x40020040u)
#define DMA1_S2NDTR      (*(volatile uint32_t *)0x40020044u)
#define DMA1_S2PAR       (*(volatile uint32_t *)0x40020048u)
#define DMA1_S2M0AR      (*(volatile uint32_t *)0x4002004cu)
#define DMAMUX1_C2CR     (*(volatile uint32_t *)0x40020808u)

#define ADC_ISR_EOC      (1u << 2)
#define ADC_CR_ADEN      (1u << 0)
#define ADC_CR_ADSTART   (1u << 2)
#define ADC_CFGR_DMAEN   (1u << 0)
#define ADC_CFGR_AUTDLY  (1u << 14)
#define DMA_CR_EN        (1u << 0)
#define DMA_CR_MINC      (1u << 10)
#define DMA_CR_PSIZE_16  (1u << 11)
#define DMA_CR_MSIZE_16  (1u << 13)
#define DMA_FLAG_TEIF_S2 (1u << (16 + 3))
#define DMA_FLAG_ALL_S2  (0x3du << 16)

#define RESULT           ((volatile uint32_t *)0x20000000u)
#define RX_BUFFER        ((volatile uint16_t *)0x20000100u)
#define DMA_INVALID_DEST 0x60000000u

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    unsigned timeout = 1000000;

    RESULT[0] = 0x41525231u; /* "ARR1" */
    RX_BUFFER[0] = 0xa5a5u;

    /* ADC1 -> DMAMUX1 request 9, DMA1 Stream2.  AUTDLY holds the one
     * conversion result until the direct DMA retry commits it. */
    DMAMUX1_C2CR = 9;
    DMA1_S2NDTR = 1;
    DMA1_S2PAR = 0x40022040u;
    DMA1_S2M0AR = DMA_INVALID_DEST;
    DMA1_S2CR = DMA_CR_EN | DMA_CR_MINC | DMA_CR_PSIZE_16 |
                DMA_CR_MSIZE_16;

    /* Select one real regular rank.  A board-created ADC starts in the
     * two-sample compatibility mode until firmware writes SQR1. */
    ADC1_SQR1 = 0;
    ADC1_CFGR = ADC_CFGR_DMAEN | ADC_CFGR_AUTDLY;
    ADC1_CR = ADC_CR_ADEN;
    ADC1_CR = ADC_CR_ADEN | ADC_CR_ADSTART;

    while (timeout-- && !(DMA1_LISR & DMA_FLAG_TEIF_S2)) {
    }
    RESULT[1] = ADC1_ISR;
    RESULT[2] = DMA1_S2NDTR;
    RESULT[3] = DMA1_S2CR;
    RESULT[4] = DMA1_LISR;
    RESULT[5] = timeout != 0;

    /* A valid re-enable must consume the same ADC_DR result; no second ADC
     * conversion or software ADC_DR read is allowed to make progress. */
    DMA1_LIFCR = DMA_FLAG_ALL_S2;
    DMA1_S2M0AR = (uint32_t)(uintptr_t)RX_BUFFER;
    DMA1_S2NDTR = 1;
    DMA1_S2CR = DMA_CR_EN | DMA_CR_MINC | DMA_CR_PSIZE_16 |
                DMA_CR_MSIZE_16;
    timeout = 1000000;
    while (timeout-- && DMA1_S2NDTR) {
    }

    RESULT[6] = RX_BUFFER[0];
    RESULT[7] = DMA1_S2NDTR;
    RESULT[8] = ADC1_ISR;
    RESULT[9] = DMA1_LISR;
    RESULT[10] = timeout != 0;
    RESULT[0] = 0x444f4e45u; /* "DONE" */
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
