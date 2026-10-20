#include "board.h"

volatile uint32_t ticks_ms;

void SysTick_Handler(void)
{
    ++ticks_ms;
}

void firmware_main(void)
{
    uint32_t previous = 2u;
#ifdef SIM_KEY_PULSE_TEST
    unsigned pulse_phase = 0u;
#endif
    board_init();
    uart_text("SIM_READY minimal-board\n");
    for (;;) {
        const uint32_t level = (GPIOA_IDR >> 15) & 1u;
        if (level != previous) {
            GPIOB_BSRR = level ? (1u << 16) : 1u;
#ifdef SIM_KEY_PULSE_TEST
            /* The normal initial transition UART occupies the 3..4ms pulse.
             * Sample and drive GPIO immediately; print only the three actual
             * observed transitions after release, without blocking the pulse.
             */
            if (pulse_phase == 0u && level == 1u) {
                pulse_phase = 1u;
            } else if (pulse_phase == 1u && level == 0u) {
                pulse_phase = 2u;
            } else if (pulse_phase == 2u && level == 1u) {
                pulse_phase = 3u;
                uart_text("KEY released B0=0\nKEY pressed B0=1\nKEY released B0=0\n");
            } else {
                uart_text("KEY_FAIL unexpected pulse transition\n");
                for (;;) { idle(); }
            }
#else
            uart_text(level ? "KEY released B0=0\n" : "KEY pressed B0=1\n");
#endif
            previous = level;
        }
        idle();
    }
}
