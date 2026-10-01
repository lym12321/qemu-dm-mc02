/* Board-independent STM32H7 USART baud and frame timing helpers. */
#ifndef HW_CHAR_DM_UART_TIMING_H
#define HW_CHAR_DM_UART_TIMING_H

#include <stdbool.h>
#include <stdint.h>

typedef enum DmUartTimingError {
    DM_UART_TIMING_ERROR_NONE,
    DM_UART_TIMING_ERROR_ZERO_KERNEL_CLOCK,
    DM_UART_TIMING_ERROR_INVALID_PRESC_CODE,
    DM_UART_TIMING_ERROR_ZERO_PRESCALED_CLOCK,
    DM_UART_TIMING_ERROR_BRR_OUT_OF_RANGE,
    DM_UART_TIMING_ERROR_INVALID_OVER8_BRR,
    DM_UART_TIMING_ERROR_INVALID_DIVIDER,
} DmUartTimingError;

typedef struct DmUartTiming {
    bool valid;
    DmUartTimingError error;
    uint16_t prescaler;
    uint64_t prescaled_clock_hz;
    uint16_t divider;
    __uint128_t baud_numerator;
    uint16_t baud_denominator;
    uint64_t frame_duration_ns;
} DmUartTiming;

/* baud_hz = baud_numerator / baud_denominator; frame duration is rounded up. */
DmUartTiming dm_uart_timing_calculate(uint64_t kernel_clock_hz,
                                      uint32_t raw_brr,
                                      uint8_t raw_presc_code,
                                      bool over8);

#endif
