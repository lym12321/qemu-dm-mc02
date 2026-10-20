#include "board.h"

volatile uint32_t ticks_ms;

void SysTick_Handler(void)
{
    ++ticks_ms;
}

void firmware_main(void)
{
    uint32_t previous = 2u;
    board_init();
    uart_text("SIM_READY minimal-board\n");
    for (;;) {
        const uint32_t level = (GPIOA_IDR >> 15) & 1u;
        if (level != previous) {
            GPIOB_BSRR = level ? (1u << 16) : 1u;
            uart_text(level ? "KEY released B0=0\n" : "KEY pressed B0=1\n");
            previous = level;
        }
        idle();
    }
}
