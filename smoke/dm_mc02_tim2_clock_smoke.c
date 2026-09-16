#include <stdint.h>

#define TIM2_BASE       0x40000000u
#define TIM2_CR1        (*(volatile uint32_t *)(TIM2_BASE + 0x00u))
#define TIM2_DIER       (*(volatile uint32_t *)(TIM2_BASE + 0x0cu))
#define TIM2_SR         (*(volatile uint32_t *)(TIM2_BASE + 0x10u))
#define TIM2_CNT        (*(volatile uint32_t *)(TIM2_BASE + 0x24u))
#define TIM2_PSC        (*(volatile uint32_t *)(TIM2_BASE + 0x28u))
#define TIM2_ARR        (*(volatile uint32_t *)(TIM2_BASE + 0x2cu))

#define RCC_BASE        0x58024400u
#define RCC_CR          (*(volatile uint32_t *)(RCC_BASE + 0x00u))
#define RCC_CFGR        (*(volatile uint32_t *)(RCC_BASE + 0x10u))
#define RCC_PLLCKSELR   (*(volatile uint32_t *)(RCC_BASE + 0x28u))
#define RCC_PLLCFGR     (*(volatile uint32_t *)(RCC_BASE + 0x2cu))
#define RCC_PLL1DIVR    (*(volatile uint32_t *)(RCC_BASE + 0x30u))

#define NVIC_ISER0      (*(volatile uint32_t *)0xe000e100u)
#define RESULT          ((volatile uint32_t *)0x20000000u)

#define TIM_CR1_CEN     (1u << 0)
#define TIM_DIER_UIE    (1u << 0)
#define TIM_SR_UIF      (1u << 0)
#define RCC_CR_HSION    (1u << 0)
#define RCC_CR_HSEON    (1u << 16)
#define RCC_CR_PLL1ON   (1u << 24)
#define RCC_PLLCFGR_PLL1DIVPEN (1u << 16)
#define TIM2_IRQ        28u

#define MARKER_SWITCHED 0x53574954u /* SWIT */
#define MARKER_DONE     0x444f4e45u

void Reset_Handler(void);
void TIM2_IRQHandler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[16u + TIM2_IRQ + 1u] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [16u + TIM2_IRQ] = (uint32_t)(uintptr_t)TIM2_IRQHandler,
};

void TIM2_IRQHandler(void)
{
    RESULT[3] = TIM2_CNT;
    RESULT[4] = TIM2_SR;
    TIM2_SR = ~TIM_SR_UIF;
    TIM2_CR1 = 0;
    RESULT[5] = MARKER_DONE;
}

void Reset_Handler(void)
{
    /* At reset HCLK/TIM2 is 64 MHz.  With PSC=31 and ARR=3999999, the
     * update period is 2 s.  Starting at CNT=2000000 leaves half a period.
     * The board PLL changes the reset-domain TIM2 clock to 480 MHz, so the
     * first update should arrive after about 133 ms, not after a fresh 267 ms
     * period.  The long
     * interval also leaves the QMP observer enough time to see the switch
     * marker before the virtual timer fires. */
    RESULT[0] = 0x434c4b30u; /* CLK0 */
    RESULT[1] = 0;
    RESULT[2] = 0;
    RESULT[3] = 0;
    RESULT[4] = 0;
    RESULT[5] = 0;

    TIM2_PSC = 31;
    TIM2_ARR = 3999999;
    TIM2_CNT = 2000000;
    TIM2_DIER = TIM_DIER_UIE;
    NVIC_ISER0 = 1u << TIM2_IRQ;
    TIM2_CR1 = TIM_CR1_CEN;

    /* HSE=24 MHz, PLL1 M=2/N=40/P=1, then select PLL1. */
    RCC_PLLCKSELR = 2u | (2u << 4);
    RCC_PLLCFGR = RCC_PLLCFGR_PLL1DIVPEN;
    RCC_PLL1DIVR = 39u;
    RCC_CR = RCC_CR_HSION | RCC_CR_HSEON | RCC_CR_PLL1ON;
    RCC_CFGR = 3u;

    RESULT[0] = MARKER_SWITCHED;
    RESULT[2] = TIM2_CNT;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
