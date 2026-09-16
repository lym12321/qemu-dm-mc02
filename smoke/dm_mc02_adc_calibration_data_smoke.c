#include <stdint.h>

#define ADC1_ISR       (*(volatile uint32_t *)0x40022000u)
#define ADC1_CR        (*(volatile uint32_t *)0x40022008u)
#define ADC1_CFGR      (*(volatile uint32_t *)0x4002200cu)
#define ADC1_SQR1      (*(volatile uint32_t *)0x40022030u)
#define ADC1_JSQR      (*(volatile uint32_t *)0x4002204cu)
#define ADC1_DR        (*(volatile uint32_t *)0x40022040u)
#define ADC1_JDR1      (*(volatile uint32_t *)0x40022080u)
#define ADC1_CALFACT   (*(volatile uint32_t *)0x400220c4u)
#define ADC1_CALFACT2  (*(volatile uint32_t *)0x400220c8u)
#define RESULT         ((volatile uint32_t *)0x20000000u)
#define BOOT_COUNT     (*(volatile uint32_t *)0x200000f0u)

#define ADC_ISR_EOC    (1u << 2)
#define ADC_ISR_EOS    (1u << 3)
#define ADC_ISR_JEOC   (1u << 5)
#define ADC_ISR_JEOS   (1u << 6)
#define ADC_CR_ADEN   (1u << 0)
#define ADC_CR_ADDIS  (1u << 1)
#define ADC_CR_ADSTART (1u << 2)
#define ADC_CR_JADSTART (1u << 3)
#define ADC_CFGR_JQDIS (1u << 31)
#define OFFSET_MASK   0x000007ffu
#define LINEAR_MASK   0x3fffffffu

#define RAW_REG       0x1234u
#define RAW_INJ       0x2a35u
#define OFFSET_FACTOR 0x0012u
#define LINEAR_FACTOR 0x01234567u

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = { 0x20020000u, (uint32_t)(uintptr_t)Reset_Handler };

static uint32_t wait_flag(uint32_t flag)
{
    for (uint32_t i = 0; i < 1000000u; ++i) {
        uint32_t isr = ADC1_ISR;
        if (isr & flag) {
            return isr;
        }
        __asm__ volatile ("nop" ::: "memory");
    }
    return 0;
}

static uint32_t regular_once(void)
{
    uint32_t isr = wait_flag(ADC_ISR_EOC);
    uint32_t value = isr ? ADC1_DR : 0xffffffffu;
    ADC1_ISR = ADC_ISR_EOS;
    return value;
}

static uint32_t injected_once(void)
{
    uint32_t isr = wait_flag(ADC_ISR_JEOC);
    uint32_t value = isr ? ADC1_JDR1 : 0xffffffffu;
    ADC1_ISR = ADC_ISR_JEOS;
    return value;
}

static void convert_pair(uint32_t result_index)
{
    ADC1_CR = ADC_CR_ADSTART;
    RESULT[result_index] = regular_once();
    ADC1_CR = ADC_CR_JADSTART;
    RESULT[result_index + 1u] = injected_once();
}

static void first_boot(void)
{
    RESULT[0] = 0x41434431u; /* "ACD1" */
    ADC1_SQR1 = 0u | (4u << 6); /* one regular rank: channel 4 */
    ADC1_CFGR = ADC_CFGR_JQDIS;
    ADC1_JSQR = 0u | (19u << 9); /* one injected rank: channel 19 */
    ADC1_CR = ADC_CR_ADEN;

    /* The four pairs are the application vectors consumed by the runner. */
    ADC1_CALFACT = 0u;
    ADC1_CALFACT2 = 0u;
    convert_pair(2u); /* uncalibrated raw */

    ADC1_CALFACT = OFFSET_FACTOR;
    ADC1_CALFACT2 = 0u;
    convert_pair(4u); /* offset-only */

    ADC1_CALFACT = 0u;
    ADC1_CALFACT2 = LINEAR_FACTOR;
    convert_pair(6u); /* linearity-only */

    ADC1_CALFACT = OFFSET_FACTOR;
    ADC1_CALFACT2 = LINEAR_FACTOR;
    convert_pair(8u); /* combined */

    /* Verify the writable fields saturate at their architectural widths. */
    ADC1_CALFACT = 0xffffffffu;
    ADC1_CALFACT2 = 0xffffffffu;
    RESULT[10] = ADC1_CALFACT;
    RESULT[11] = ADC1_CALFACT2;

    /* Zero is a meaningful calibration datum, not an ignored write. */
    ADC1_CALFACT = 0u;
    ADC1_CALFACT2 = 0u;
    RESULT[12] = ADC1_CALFACT;
    RESULT[13] = ADC1_CALFACT2;

    /* H723 factor writes while the ADC is disabled are rejected. */
    ADC1_CR = ADC_CR_ADDIS;
    ADC1_CALFACT = 0xffffffffu;
    ADC1_CALFACT2 = 0xffffffffu;
    RESULT[14] = ADC1_CALFACT;
    RESULT[15] = ADC1_CALFACT2;
    RESULT[16] = ADC1_SQR1;
    RESULT[17] = ADC1_JSQR;
    RESULT[18] = 0x444f4e45u; /* "DONE" */
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}

void Reset_Handler(void)
{
    ++BOOT_COUNT;
    if (BOOT_COUNT != 1u) {
        RESULT[19] = ADC1_CALFACT;
        RESULT[20] = ADC1_CALFACT2;
        RESULT[21] = 0x52535432u; /* reset readback */
        for (;;) {
            __asm__ volatile ("wfi" ::: "memory");
        }
    }
    first_boot();
}
