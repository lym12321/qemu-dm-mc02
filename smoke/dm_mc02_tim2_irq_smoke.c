#include <stdint.h>

#define TIM2_CR1  (*(volatile uint32_t *)0x40000000u)
#define TIM2_DIER (*(volatile uint32_t *)0x4000000cu)
#define TIM2_SR   (*(volatile uint32_t *)0x40000010u)
#define TIM2_PSC  (*(volatile uint32_t *)0x40000028u)
#define TIM2_ARR  (*(volatile uint32_t *)0x4000002cu)
#define NVIC_ISER0 (*(volatile uint32_t *)0xe000e100u)
#define RESULT ((volatile uint32_t *)0x20000000u)

#define TIM_CR1_CEN  (1u << 0)
#define TIM_DIER_UIE (1u << 0)
#define TIM_SR_UIF   (1u << 0)
#define TIM2_IRQ 28u

#define MARKER     0x54494d31u /* TIM1 */
#define IRQ_MARKER 0x49525132u /* IRQ2 */
#define DONE       0x444f4e45u

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

    RESULT[1]++;
    RESULT[2] = IRQ_MARKER;
    RESULT[3] = status;
    /* TIM status flags are write-zero-to-clear. */
    TIM2_SR = ~TIM_SR_UIF;
    RESULT[4] = TIM2_SR;
    TIM2_CR1 = 0;
    RESULT[5] = DONE;
}

void Reset_Handler(void)
{
    RESULT[0] = MARKER;
    RESULT[1] = 0;
    /* Reset HCLK/TIM2 is 64 MHz; this gives a one-millisecond update. */
    TIM2_PSC = 63;
    TIM2_ARR = 999;
    TIM2_DIER = TIM_DIER_UIE;
    NVIC_ISER0 = 1u << TIM2_IRQ;
    TIM2_CR1 = TIM_CR1_CEN;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
