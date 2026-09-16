#include <stdint.h>

#define ADC1_ISR  (*(volatile uint32_t *)0x40022000u)
#define ADC1_CR   (*(volatile uint32_t *)0x40022008u)
#define ADC1_CFGR (*(volatile uint32_t *)0x4002200cu)
#define ADC1_SMPR1 (*(volatile uint32_t *)0x40022014u)
#define ADC1_SQR1 (*(volatile uint32_t *)0x40022030u)
#define ADC1_DR   (*(volatile uint32_t *)0x40022040u)
#define RESULT    ((volatile uint32_t *)0x20000000u)

#define ADC_ISR_ADRDY (1u << 0)
#define ADC_ISR_EOC   (1u << 2)
#define ADC_ISR_EOS   (1u << 3)

#define ADC_CR_ADEN    (1u << 0)
#define ADC_CR_ADSTART (1u << 2)
#define ADC_CR_ADVREGEN (1u << 28)
#define ADC_CR_DEEPPWD  (1u << 29)

#define MARKER 0x41445031u /* ADP1 */
#define DONE   0x444f4e45u /* DONE */

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

static void finish(uint32_t status)
{
    RESULT[12] = status;
    RESULT[14] = DONE;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}

void Reset_Handler(void)
{
    uint32_t isr;
    uint32_t attempts;

    RESULT[0] = MARKER;

    /* The power-model reset state is externally observable through CR/ISR. */
    RESULT[1] = ADC1_CR;
    RESULT[2] = ADC1_ISR;

    /* Select one regular rank and leave CONT clear for a single sequence. */
    ADC1_SQR1 = 0;
    ADC1_CFGR = 0;
    /* Use the longest sample time for channel 0 so ADSTART remains
     * observable before the conversion completion edge. */
    ADC1_SMPR1 = 7u;

    /* Leave deep power-down and request the regulator.  The following
     * enable/start attempt is intentionally made immediately, before the
     * modeled 10 us regulator startup interval can elapse. */
    ADC1_CR = ADC_CR_ADVREGEN;
    ADC1_CR = ADC_CR_ADVREGEN | ADC_CR_ADEN | ADC_CR_ADSTART;
    RESULT[3] = ADC1_CR;
    RESULT[4] = ADC1_ISR;

    /* There is no guest-visible regulator-ready bit.  Re-issue ADEN while
     * observing ADRDY; the first successful write is the ready boundary. */
    attempts = 0;
    for (attempts = 1; attempts <= 200000u; ++attempts) {
        ADC1_CR = ADC_CR_ADVREGEN | ADC_CR_ADEN;
        isr = ADC1_ISR;
        if (isr & ADC_ISR_ADRDY) {
            break;
        }
    }
    if (attempts > 200000u) {
        RESULT[5] = ADC1_CR;
        RESULT[6] = ADC1_ISR;
        RESULT[7] = attempts;
        finish(1u);
    }
    RESULT[5] = ADC1_CR;
    RESULT[6] = ADC1_ISR;
    RESULT[7] = attempts;

    ADC1_CR = ADC_CR_ADVREGEN | ADC_CR_ADEN | ADC_CR_ADSTART;
    RESULT[8] = ADC1_CR;
    for (attempts = 1; attempts <= 200000u; ++attempts) {
        isr = ADC1_ISR;
        if (isr & ADC_ISR_EOC) {
            break;
        }
    }
    if (attempts > 200000u) {
        RESULT[9] = ADC1_ISR;
        RESULT[10] = attempts;
        finish(2u);
    }
    RESULT[9] = ADC1_ISR;
    RESULT[10] = ADC1_DR;
    RESULT[11] = ADC1_ISR;

    /* DEEPPWD must synchronously remove regulator, enable, start and ready. */
    ADC1_CR = ADC_CR_DEEPPWD;
    RESULT[12] = ADC1_CR;
    RESULT[13] = ADC1_ISR;
    RESULT[14] = DONE;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
