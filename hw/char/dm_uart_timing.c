/* Board-independent STM32H7 USART baud and frame timing helpers. */
#include "hw/char/dm_uart_timing.h"

#define DM_UART_TIMING_BRR_MAX UINT32_C(0xffff)
#define DM_UART_TIMING_MIN_DIVIDER UINT16_C(16)
#define DM_UART_TIMING_FRAME_BITS UINT32_C(10)
#define DM_UART_TIMING_NS_PER_SECOND UINT64_C(1000000000)

static const uint16_t dm_uart_prescalers[] = {
    1, 2, 4, 6, 8, 10, 12, 16, 32, 64, 128, 256,
};

static DmUartTiming dm_uart_timing_invalid(DmUartTimingError error)
{
    DmUartTiming result = { 0 };

    result.error = error;
    return result;
}

DmUartTiming dm_uart_timing_calculate(uint64_t kernel_clock_hz,
                                      uint32_t raw_brr,
                                      uint8_t raw_presc_code,
                                      bool over8)
{
    DmUartTiming result = { 0 };
    __uint128_t frame_numerator;
    __uint128_t frame_denominator;

    if (kernel_clock_hz == 0) {
        return dm_uart_timing_invalid(
            DM_UART_TIMING_ERROR_ZERO_KERNEL_CLOCK);
    }
    if (raw_presc_code >= sizeof(dm_uart_prescalers) /
                         sizeof(dm_uart_prescalers[0])) {
        return dm_uart_timing_invalid(
            DM_UART_TIMING_ERROR_INVALID_PRESC_CODE);
    }

    result.prescaler = dm_uart_prescalers[raw_presc_code];
    result.prescaled_clock_hz = kernel_clock_hz / result.prescaler;
    if (result.prescaled_clock_hz == 0) {
        return dm_uart_timing_invalid(
            DM_UART_TIMING_ERROR_ZERO_PRESCALED_CLOCK);
    }
    if (raw_brr > DM_UART_TIMING_BRR_MAX) {
        return dm_uart_timing_invalid(DM_UART_TIMING_ERROR_BRR_OUT_OF_RANGE);
    }
    if (over8 && (raw_brr & UINT32_C(0x0008))) {
        return dm_uart_timing_invalid(
            DM_UART_TIMING_ERROR_INVALID_OVER8_BRR);
    }

    if (over8) {
        result.divider = (uint16_t)((raw_brr & UINT32_C(0xfff0)) |
                                    ((raw_brr & UINT32_C(0x000f)) << 1));
    } else {
        result.divider = (uint16_t)raw_brr;
    }
    if (result.divider < DM_UART_TIMING_MIN_DIVIDER) {
        return dm_uart_timing_invalid(DM_UART_TIMING_ERROR_INVALID_DIVIDER);
    }

    result.baud_numerator = (__uint128_t)result.prescaled_clock_hz;
    if (over8) {
        result.baud_numerator *= 2;
    }
    result.baud_denominator = result.divider;

    /* A frame must not be scheduled before all ten 8N1 bits have elapsed. */
    frame_numerator = (__uint128_t)DM_UART_TIMING_FRAME_BITS *
                      DM_UART_TIMING_NS_PER_SECOND *
                      result.baud_denominator;
    frame_denominator = result.baud_numerator;
    result.frame_duration_ns = (uint64_t)((frame_numerator +
                                           frame_denominator - 1) /
                                          frame_denominator);
    result.valid = true;
    result.error = DM_UART_TIMING_ERROR_NONE;
    return result;
}
