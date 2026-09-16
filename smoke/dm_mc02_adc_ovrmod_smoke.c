#include <stdint.h>

#define ADC1_ISR  (*(volatile uint32_t *)0x40022000u)
#define ADC1_IER  (*(volatile uint32_t *)0x40022004u)
#define ADC1_CR   (*(volatile uint32_t *)0x40022008u)
#define ADC1_CFGR (*(volatile uint32_t *)0x4002200cu)
#define ADC1_SMPR1 (*(volatile uint32_t *)0x40022014u)
#define ADC1_DR   (*(volatile uint32_t *)0x40022040u)
#define NVIC_ISER0 (*(volatile uint32_t *)0xe000e100u)
#define RESULT ((volatile uint32_t *)0x20001000u)

#define ADC_ISR_EOC (1u << 2)
#define ADC_ISR_OVR (1u << 4)
#define ADC_IER_OVRIE (1u << 4)
#define ADC_CR_ADEN (1u << 0)
#define ADC_CR_ADSTART (1u << 2)
#define ADC_CR_ADDIS (1u << 1)
#define ADC_CFGR_CONT (1u << 13)
#define ADC_CFGR_OVRMOD (1u << 12)
#define ADC_IRQ 18u

#define MARKER 0x414f5631u /* AOV1 */
#define IRQ_MARKER 0x49525131u /* IRQ1 */
#define DONE 0x444f4e45u

void Reset_Handler(void);
void ADC1_IRQHandler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[16u + ADC_IRQ + 1u] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [16u + ADC_IRQ] = (uint32_t)(uintptr_t)ADC1_IRQHandler,
};

void ADC1_IRQHandler(void)
{
    uint32_t isr = ADC1_ISR;

    RESULT[1] = IRQ_MARKER;
    RESULT[2] = isr;
    if (isr & ADC_ISR_OVR) {
        /* OVRMOD=1 must expose the second sample, while DR read only
         * acknowledges EOC.  The OVR flag is cleared independently below. */
        RESULT[3] = ADC1_DR;
        RESULT[4] = ADC1_ISR;
        ADC1_ISR = ADC_ISR_OVR;
        RESULT[5] = ADC1_ISR;
        ADC1_CR = ADC_CR_ADDIS;
        RESULT[6] = DONE;
    }
}

void Reset_Handler(void)
{
    RESULT[0] = MARKER;
    RESULT[1] = 0;
    RESULT[2] = 0;
    RESULT[3] = 0;
    RESULT[4] = 0;
    RESULT[5] = 0;
    RESULT[6] = 0;

    /* The default compatibility source has two samples: 0x0100 then
     * 0x0200.  Leave EOC pending so the second conversion overruns it. */
    /* Use the longest sample time for legacy channels 0 and 1.  This keeps
     * the third conversion from overtaking the IRQ handler, so the observed
     * ADC_DR value is the conversion that raised OVR. */
    ADC1_SMPR1 = (7u << 0) | (7u << 3);
    ADC1_CFGR = ADC_CFGR_CONT | ADC_CFGR_OVRMOD;
    ADC1_IER = ADC_IER_OVRIE;
    NVIC_ISER0 = 1u << ADC_IRQ;
    ADC1_CR = ADC_CR_ADEN;
    ADC1_CR = ADC_CR_ADEN | ADC_CR_ADSTART;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
