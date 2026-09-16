#include <stdint.h>

#define ADC1_ISR  (*(volatile uint32_t *)0x40022000u)
#define ADC1_IER  (*(volatile uint32_t *)0x40022004u)
#define ADC1_CR   (*(volatile uint32_t *)0x40022008u)
#define ADC1_CFGR (*(volatile uint32_t *)0x4002200cu)
#define ADC1_SMPR1 (*(volatile uint32_t *)0x40022014u)
#define ADC1_SMPR2 (*(volatile uint32_t *)0x40022018u)
#define ADC1_SQR1 (*(volatile uint32_t *)0x40022030u)
#define ADC1_DR   (*(volatile uint32_t *)0x40022040u)
#define NVIC_ISER0 (*(volatile uint32_t *)0xe000e100u)
#define RESULT ((volatile uint32_t *)0x20001000u)

#define ADC_ISR_EOC (1u << 2)
#define ADC_ISR_EOS (1u << 3)
#define ADC_ISR_OVR (1u << 4)
#define ADC_IER_EOCIE (1u << 2)
#define ADC_IER_EOSIE (1u << 3)
#define ADC_IER_OVRIE (1u << 4)
#define ADC_CR_ADEN (1u << 0)
#define ADC_CR_ADSTART (1u << 2)
#define ADC_CR_ADDIS (1u << 1)
#define ADC_CFGR_CONT (1u << 13)
#define ADC_CFGR_OVRMOD (1u << 12)
#define ADC_FLAGS (ADC_ISR_EOC | ADC_ISR_EOS | ADC_ISR_OVR)
#define ADC_IRQ 18u

#define MARKER 0x41495231u /* AIR1 */
#define IRQ_MARKER 0x49525131u /* IRQ1 */

#define PHASE (*(volatile uint32_t *)0x20000000u)

void Reset_Handler(void);
void ADC1_IRQHandler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[16u + ADC_IRQ + 1u] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [16u + ADC_IRQ] = (uint32_t)(uintptr_t)ADC1_IRQHandler,
};

static uint32_t flags(void)
{
    return ADC1_ISR & ADC_FLAGS;
}

static void begin_continuous(void)
{
    ADC1_CFGR = ADC_CFGR_CONT;
    ADC1_CR = ADC_CR_ADEN | ADC_CR_ADSTART;
    PHASE = 3;
}

void ADC1_IRQHandler(void)
{
    uint32_t isr = flags();

    RESULT[1]++;
    RESULT[12] = IRQ_MARKER;

    if (PHASE == 0 && (isr & ADC_ISR_EOC)) {
        RESULT[2] = isr;
        RESULT[20] = ADC1_DR;
        RESULT[3] = flags();
        if (isr & ADC_ISR_OVR) {
            ADC1_ISR = ADC_ISR_OVR;
        }
        ADC1_IER = ADC_IER_EOCIE | ADC_IER_EOSIE | ADC_IER_OVRIE;
        RESULT[15] = ADC1_IER;
        PHASE = 1;
        return;
    }
    if (PHASE == 1 && (isr & ADC_ISR_EOC)) {
        RESULT[4] = isr;
        (void)ADC1_DR;
        RESULT[5] = flags();
        PHASE = 2;
        return;
    }
    if (PHASE == 2 && (isr & ADC_ISR_EOS)) {
        RESULT[6] = isr;
        /* The final rank raises EOC before EOS; acknowledge that EOC first. */
        if (isr & ADC_ISR_EOC) {
            (void)ADC1_DR;
        }
        ADC1_ISR = ADC_ISR_EOS;
        RESULT[7] = flags();

        /* Reuse the same configured IRQ enables for the OVR phase. */
        begin_continuous();
        return;
    }
    if (PHASE == 3 && (isr & ADC_ISR_EOC)) {
        /* Deliberately do not read DR: the next conversion must overrun it. */
        RESULT[8] = isr;
        PHASE = 4;
        if (isr & ADC_ISR_EOS) {
            ADC1_ISR = ADC_ISR_EOS;
            RESULT[11] = flags();
        }
        if (isr & ADC_ISR_OVR) {
            RESULT[9] = isr;
            ADC1_ISR = ADC_ISR_OVR;
            RESULT[10] = flags();
            if (RESULT[11] != 0) {
                ADC1_CR = ADC_CR_ADDIS;
                RESULT[13] = 0x444f4e45u; /* DONE */
                PHASE = 6;
            } else {
                PHASE = 5;
            }
        }
        return;
    }
    if (PHASE == 4 && (isr & ADC_ISR_OVR)) {
        RESULT[9] = isr;
        /* Default OVRMOD=0 retains the older conversion in ADC_DR. */
        RESULT[16] = ADC1_DR;
        RESULT[17] = flags();
        RESULT[19] = ADC1_CFGR & ADC_CFGR_OVRMOD;
        ADC1_ISR = ADC_ISR_OVR;
        RESULT[18] = flags();
        if (isr & ADC_ISR_EOS) {
            ADC1_ISR = ADC_ISR_EOS;
            RESULT[11] = flags();
            ADC1_CR = ADC_CR_ADDIS;
            RESULT[13] = 0x444f4e45u; /* DONE */
            PHASE = 6;
        } else if (RESULT[11] != 0) {
            ADC1_CR = ADC_CR_ADDIS;
            RESULT[13] = 0x444f4e45u; /* DONE */
            PHASE = 6;
        } else {
            PHASE = 5;
        }
        return;
    }
    if (PHASE == 5 && (isr & ADC_ISR_EOS)) {
        if (isr & ADC_ISR_OVR) {
            ADC1_ISR = ADC_ISR_OVR;
            RESULT[10] = flags();
        }
        ADC1_ISR = ADC_ISR_EOS;
        RESULT[11] = flags();
        ADC1_CR = ADC_CR_ADDIS;
        RESULT[13] = 0x444f4e45u; /* DONE */
        PHASE = 6;
    }
}

void Reset_Handler(void)
{
    RESULT[0] = MARKER;
    RESULT[1] = 0;
    PHASE = 0;

    /* Three real H723 SQR1 ranks: SQ1 at 6, SQ2 at 12, SQ3 at 18. */
    ADC1_SQR1 = 2u | (4u << 6) | (19u << 12) | (4u << 18);
    /* Code 7 is the longest sample time; channel 19 uses SMPR2[29:27]. */
    ADC1_SMPR1 = 7u << 12;
    ADC1_SMPR2 = 7u << 27;
    ADC1_CFGR = 0;
    /* Start with EOCIE so the one-shot DR acknowledgement is isolated. */
    ADC1_IER = ADC_IER_EOCIE;
    RESULT[14] = ADC1_IER;
    NVIC_ISER0 = 1u << ADC_IRQ;

    /* One non-continuous sequence exercises EOC, DR acknowledgement and EOS. */
    ADC1_CR = ADC_CR_ADEN;
    ADC1_CR = ADC_CR_ADEN | ADC_CR_ADSTART;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
