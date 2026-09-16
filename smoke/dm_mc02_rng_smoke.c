#include <stdint.h>

#define RNG_CR   (*(volatile uint32_t *)0x48021800u)
#define RNG_SR   (*(volatile uint32_t *)0x48021804u)
#define RNG_DR   (*(volatile uint32_t *)0x48021808u)
#define NVIC_ISER2 (*(volatile uint32_t *)0xe000e108u)
#define RESULT ((volatile uint32_t *)0x20000000u)

#define RNG_CR_RNGEN      (1u << 2)
#define RNG_CR_IE         (1u << 3)
#define RNG_SR_DRDY       (1u << 0)
#define IRQ_MARKER        0x49524e47u /* IRNG */
#define DONE_MARKER       0x444f4e45u /* DONE */

void Reset_Handler(void);
void RNG_IRQHandler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[16u + 80u + 1u] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
    [16u + 80u] = (uint32_t)(uintptr_t)RNG_IRQHandler,
};

void RNG_IRQHandler(void)
{
    uint32_t count = RESULT[23] + 1u;

    RESULT[23] = count;
    if (count == 1u) {
        RESULT[11] = IRQ_MARKER;
        RESULT[12] = RNG_SR;
        RNG_CR &= ~RNG_CR_IE;
        RESULT[13] = RNG_CR;
    } else {
        RESULT[20] = IRQ_MARKER;
        RESULT[21] = RNG_SR;
        RNG_CR &= ~RNG_CR_IE;
        RESULT[22] = RNG_CR;
    }
}

void Reset_Handler(void)
{
    RESULT[0] = 0x524e4731u; /* RNG1 */
    RESULT[1] = RNG_CR;
    RESULT[2] = RNG_SR;
    RESULT[3] = RNG_DR;

    NVIC_ISER2 = 1u << (80u - 64u);
    RNG_CR = RNG_CR_RNGEN | RNG_CR_IE;
    RESULT[4] = RNG_CR;
    while (!(RNG_SR & RNG_SR_DRDY)) {
    }
    RESULT[5] = RNG_SR;
    RESULT[6] = RNG_DR;
    RESULT[7] = RNG_DR;
    RESULT[8] = RNG_DR;
    RESULT[9] = RNG_DR;
    RESULT[10] = RNG_SR;
    RESULT[14] = RNG_SR;
    RESULT[15] = RNG_DR;
    RESULT[16] = RNG_SR;
    RNG_CR |= RNG_CR_IE;
    RESULT[17] = RNG_DR;
    RESULT[18] = RNG_SR;
    RESULT[19] = DONE_MARKER;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
