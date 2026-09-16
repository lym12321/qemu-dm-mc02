#include <stdint.h>

#define TIM2_BASE       0x40000000u
#define TIM2_CR1        (*(volatile uint32_t *)(TIM2_BASE + 0x00u))
#define TIM2_DIER       (*(volatile uint32_t *)(TIM2_BASE + 0x0cu))
#define TIM2_SR         (*(volatile uint32_t *)(TIM2_BASE + 0x10u))
#define TIM2_EGR        (*(volatile uint32_t *)(TIM2_BASE + 0x14u))
#define TIM2_CNT       (*(volatile uint32_t *)(TIM2_BASE + 0x24u))
#define TIM2_PSC       (*(volatile uint32_t *)(TIM2_BASE + 0x28u))
#define TIM2_ARR       (*(volatile uint32_t *)(TIM2_BASE + 0x2cu))
#define TIM2_CCR1      (*(volatile uint32_t *)(TIM2_BASE + 0x34u))
#define TIM3_CR2       (*(volatile uint32_t *)0x40000404u)
#define NVIC_ISER0     (*(volatile uint32_t *)0xe000e100u)
#define RESULT         ((volatile uint32_t *)0x20000000u)

#define TIM_CR1_CEN    (1u << 0)
#define TIM_DIER_CC1IE (1u << 1)
#define TIM_SR_UIF     (1u << 0)
#define TIM_SR_CC1IF   (1u << 1)
#define TIM_EGR_UG     (1u << 0)
#define TIM2_IRQ       28u

#define START          0x45564e54u /* EVNT */
#define IRQ_MARKER     0x43433149u /* CC1I */
#define DONE            0x444f4e45u

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
    uint32_t status = TIM2_SR;

    RESULT[1] = IRQ_MARKER;
    RESULT[2] = status;
    RESULT[3] = TIM2_CNT;
    TIM2_SR = ~TIM_SR_CC1IF;
    RESULT[4] = TIM2_SR;
    TIM2_CR1 = 0;
    RESULT[9] = TIM2_CNT;
    RESULT[5] = DONE;
}

void Reset_Handler(void)
{
    RESULT[0] = START;
    RESULT[1] = 0;
    RESULT[2] = 0;
    RESULT[3] = 0;
    RESULT[4] = 0;
    RESULT[5] = 0;
    RESULT[6] = 0;
    RESULT[7] = 0;
    RESULT[8] = 0;
    RESULT[9] = 0;

    /* UG is an update event even while the counter is stopped. */
    TIM2_PSC = 31;
    TIM2_ARR = 999;
    TIM2_CNT = 123;
    TIM2_EGR = TIM_EGR_UG;
    RESULT[6] = TIM2_CNT;
    RESULT[7] = TIM2_SR;
    TIM2_SR = ~(TIM_SR_UIF | TIM_SR_CC1IF);

    /* A general-purpose timer has no MMS2 field on this model. */
    TIM3_CR2 = 2u << 20;
    RESULT[8] = TIM3_CR2;

    /* CC1IE alone must create a compare event and assert the timer IRQ. */
    TIM2_PSC = 31;
    TIM2_ARR = 3199;
    TIM2_CCR1 = 100;
    TIM2_DIER = TIM_DIER_CC1IE;
    NVIC_ISER0 = 1u << TIM2_IRQ;
    TIM2_CR1 = TIM_CR1_CEN;
    RESULT[9] = TIM2_CR1;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
