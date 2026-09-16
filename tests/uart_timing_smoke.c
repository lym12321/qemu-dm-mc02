#include "hw/char/dm_uart_timing.h"

#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>

#define CHECK(condition)                                                     \
    do {                                                                      \
        if (!(condition)) {                                                   \
            fprintf(stderr, "FAIL: %s:%d: %s\n", __FILE__, __LINE__,       \
                    #condition);                                             \
            return EXIT_FAILURE;                                              \
        }                                                                     \
    } while (0)

static int expect_invalid(uint64_t kernel_clock_hz, uint32_t raw_brr,
                          uint8_t raw_presc_code, bool over8,
                          DmUartTimingError error)
{
    DmUartTiming result = dm_uart_timing_calculate(
        kernel_clock_hz, raw_brr, raw_presc_code, over8);

    CHECK(!result.valid);
    CHECK(result.error == error);
    return EXIT_SUCCESS;
}

static int test_prescaler_table(void)
{
    static const uint16_t expected[] = {
        1, 2, 4, 6, 8, 10, 12, 16, 32, 64, 128, 256,
    };

    for (uint8_t code = 0; code < 12; ++code) {
        DmUartTiming result = dm_uart_timing_calculate(
            UINT64_C(65536), UINT32_C(16), code, false);

        CHECK(result.valid);
        CHECK(result.prescaler == expected[code]);
        CHECK(result.prescaled_clock_hz == UINT64_C(65536) / expected[code]);
        CHECK(result.divider == 16);
        CHECK(result.baud_numerator ==
              (__uint128_t)(UINT64_C(65536) / expected[code]));
        CHECK(result.baud_denominator == 16);
    }
    return EXIT_SUCCESS;
}

static int test_oversampling(void)
{
    DmUartTiming result = dm_uart_timing_calculate(
        UINT64_C(64000000), UINT32_C(0x0457), 0, true);

    CHECK(result.valid);
    CHECK(result.divider == UINT16_C(0x045e));
    CHECK(result.baud_numerator == (__uint128_t)UINT64_C(128000000));
    CHECK(result.baud_denominator == UINT16_C(0x045e));
    CHECK(result.frame_duration_ns == UINT64_C(87344));

    result = dm_uart_timing_calculate(UINT64_C(64000000), UINT32_C(555),
                                      0, false);
    CHECK(result.valid);
    CHECK(result.divider == 555);
    CHECK(result.baud_numerator == (__uint128_t)UINT64_C(64000000));
    CHECK(result.baud_denominator == 555);
    CHECK(result.frame_duration_ns == UINT64_C(86719));
    return EXIT_SUCCESS;
}

static int test_boundaries_and_invalid_values(void)
{
    DmUartTiming result = dm_uart_timing_calculate(
        UINT64_C(1), UINT32_C(16), 0, false);

    CHECK(result.valid);
    CHECK(result.frame_duration_ns == UINT64_C(160000000000));

    result = dm_uart_timing_calculate(UINT64_MAX, UINT32_C(0xffff), 0,
                                      false);
    CHECK(result.valid);
    CHECK(result.divider == UINT16_C(0xffff));
    CHECK(result.baud_numerator == (__uint128_t)UINT64_MAX);

    result = dm_uart_timing_calculate(UINT64_MAX, UINT32_C(0xfff7), 0,
                                      true);
    CHECK(result.valid);
    CHECK(result.divider == UINT16_C(0xfffe));
    CHECK(result.baud_numerator == (__uint128_t)UINT64_MAX * 2);

    CHECK(expect_invalid(0, 16, 0, false,
                         DM_UART_TIMING_ERROR_ZERO_KERNEL_CLOCK) ==
          EXIT_SUCCESS);
    CHECK(expect_invalid(64, 16, 12, false,
                         DM_UART_TIMING_ERROR_INVALID_PRESC_CODE) ==
          EXIT_SUCCESS);
    CHECK(expect_invalid(64, 16, 15, false,
                         DM_UART_TIMING_ERROR_INVALID_PRESC_CODE) ==
          EXIT_SUCCESS);
    CHECK(expect_invalid(255, 16, 11, false,
                         DM_UART_TIMING_ERROR_ZERO_PRESCALED_CLOCK) ==
          EXIT_SUCCESS);
    CHECK(expect_invalid(64, 0, 0, false,
                         DM_UART_TIMING_ERROR_INVALID_DIVIDER) ==
          EXIT_SUCCESS);
    CHECK(expect_invalid(64, 15, 0, false,
                         DM_UART_TIMING_ERROR_INVALID_DIVIDER) ==
          EXIT_SUCCESS);
    CHECK(expect_invalid(64, 7, 0, true,
                         DM_UART_TIMING_ERROR_INVALID_DIVIDER) ==
          EXIT_SUCCESS);
    CHECK(expect_invalid(64, UINT32_C(0x10000), 0, false,
                         DM_UART_TIMING_ERROR_BRR_OUT_OF_RANGE) ==
          EXIT_SUCCESS);
    CHECK(expect_invalid(64, UINT32_C(0x045f), 0, true,
                         DM_UART_TIMING_ERROR_INVALID_OVER8_BRR) ==
          EXIT_SUCCESS);
    CHECK(expect_invalid(64, UINT32_C(0xffff), 0, true,
                         DM_UART_TIMING_ERROR_INVALID_OVER8_BRR) ==
          EXIT_SUCCESS);
    return EXIT_SUCCESS;
}

int main(void)
{
    if (test_prescaler_table() != EXIT_SUCCESS ||
        test_oversampling() != EXIT_SUCCESS ||
        test_boundaries_and_invalid_values() != EXIT_SUCCESS) {
        return EXIT_FAILURE;
    }
    puts("RESULT: UART timing smoke passed");
    return EXIT_SUCCESS;
}
