#include <stdint.h>

#define TIM3_CR1  (*(volatile uint32_t *)0x40000400u)
#define TIM3_DIER (*(volatile uint32_t *)0x4000040cu)
#define TIM3_SR   (*(volatile uint32_t *)0x40000410u)
#define TIM3_PSC  (*(volatile uint32_t *)0x40000428u)
#define TIM3_ARR  (*(volatile uint32_t *)0x4000042cu)
#define NVIC_ISER0 (*(volatile uint32_t *)0xe000e100u)
#define RESULT ((volatile uint32_t *)0x20000000u)

#define TIM_CR1_CEN  (1u << 0)
#define TIM_DIER_UIE (1u << 0)
#define TIM_SR_UIF   (1u << 0)
#define TIM3_IRQ 29u

#define MARKER     0x54494d33u /* TIM3 */
#define IRQ_MARKER 0x49525133u /* IRQ3 */
#define DONE       0x444f4e45u

void Reset_Handler(void);
void TIM3_IRQHandler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[16u + TIM3_IRQ + 1u] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [16u + TIM3_IRQ] = (uint32_t)(uintptr_t)TIM3_IRQHandler,
};

void TIM3_IRQHandler(void)
{
    uint32_t status = TIM3_SR;

    RESULT[1]++;
    RESULT[2] = IRQ_MARKER;
    RESULT[3] = status;
    /* TIM status flags are write-zero-to-clear. */
    TIM3_SR = ~TIM_SR_UIF;
    RESULT[4] = TIM3_SR;
    TIM3_CR1 = 0;
    RESULT[5] = DONE;
}

void Reset_Handler(void)
{
    RESULT[0] = MARKER;
    RESULT[1] = 0;
    /* Reset HCLK/TIM3 is 64 MHz; this gives a one-millisecond update. */
    TIM3_PSC = 63;
    TIM3_ARR = 999;
    TIM3_DIER = TIM_DIER_UIE;
    NVIC_ISER0 = 1u << (TIM3_IRQ & 31u);
    TIM3_CR1 = TIM_CR1_CEN;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
