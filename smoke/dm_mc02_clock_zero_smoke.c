#include <stdint.h>

#define RCC_BASE 0x58024400u
#define RCC_CR   (*(volatile uint32_t *)(RCC_BASE + 0x00u))
#define RCC_CFGR (*(volatile uint32_t *)(RCC_BASE + 0x10u))
#define RCC_D2CCIP1R (*(volatile uint32_t *)(RCC_BASE + 0x50u))
#define RESULT   ((volatile uint32_t *)0x20000000u)

#define PLL1_SOURCE 3u
#define MARKER_START 0x434c5a30u /* CLZ0 */

void Reset_Handler(void)
{
    RESULT[0] = MARKER_START;
    RESULT[1] = RCC_CFGR;
    RESULT[2] = RCC_CR;

    /* Observe a board-level clock consumer whose normal source is PLL1Q. */
    RCC_D2CCIP1R = 1u << 28;

    /* Request PLL1 while it is unavailable, then disable the currently
     * effective HSI source.  RCC keeps the PLL1 request visible, but its
     * effective system clock and APB timer clocks must become zero.  The
     * marker is written before the transition because a zero-rate CPU clock
     * may stop execution immediately after the RCC write. */
    RCC_CFGR = PLL1_SOURCE;
    RESULT[1] = RCC_CFGR;
    RESULT[2] = RCC_CR;
    RCC_CR = 0;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[2] = {
    [0] = 0x20020000u,
    [1] = (uint32_t)(uintptr_t)Reset_Handler,
};
