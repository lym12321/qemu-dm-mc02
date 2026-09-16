#include "hw/arm/dm_stm32h7_clock_tree.h"

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static void check(bool condition, const char *expression, unsigned line)
{
    if (!condition) {
        fprintf(stderr, "clock tree smoke: line %u: %s\n", line,
                expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

static void test_hpre_table(void)
{
    static const uint32_t encodings[] = {
        0x0u, 0x8u, 0x9u, 0xau, 0xbu, 0xcu, 0xdu, 0xeu, 0xfu,
    };
    static const uint64_t dividers[] = {
        1, 2, 4, 8, 16, 64, 128, 256, 512,
    };

    for (size_t i = 0; i < sizeof(encodings) / sizeof(encodings[0]); ++i) {
        CHECK(dm_stm32h7_d1_hpre_divider(encodings[i]) == dividers[i]);
        CHECK(dm_stm32h7_hclk_hz(480000000, encodings[i]) ==
              480000000 / dividers[i]);
    }
}

static void test_core_and_hclk_are_independent(void)
{
    const uint32_t d1cfgr = (0xau << 8) | 0x8u;

    CHECK(dm_stm32h7_d1_core_divider(d1cfgr) == 8);
    CHECK(dm_stm32h7_d1_hpre_divider(d1cfgr) == 2);
    CHECK(dm_stm32h7_cpu_clock_hz(480000000, d1cfgr) == 60000000);
    CHECK(dm_stm32h7_hclk_hz(480000000, d1cfgr) == 240000000);
}

static void test_apb_and_timer_tables(void)
{
    static const uint32_t encodings[] = { 0, 4, 5, 6, 7 };
    static const uint64_t dividers[] = { 1, 2, 4, 8, 16 };
    const uint64_t hclk_hz = 240000000;

    for (size_t i = 0; i < sizeof(encodings) / sizeof(encodings[0]); ++i) {
        uint32_t d2cfgr = (encodings[i] << 4) | (encodings[i] << 8);
        uint64_t apb_hz = hclk_hz / dividers[i];

        CHECK(dm_stm32h7_d2_apb1_divider(d2cfgr) == dividers[i]);
        CHECK(dm_stm32h7_d2_apb2_divider(d2cfgr) == dividers[i]);
        CHECK(dm_stm32h7_apb1_hz(hclk_hz, d2cfgr) == apb_hz);
        CHECK(dm_stm32h7_apb2_hz(hclk_hz, d2cfgr) == apb_hz);
        CHECK(dm_stm32h7_apb1_timer_clock_hz(hclk_hz, d2cfgr, 0) ==
              (dividers[i] == 1 ? apb_hz : apb_hz * 2));
        CHECK(dm_stm32h7_apb2_timer_clock_hz(hclk_hz, d2cfgr, 0) ==
              (dividers[i] == 1 ? apb_hz : apb_hz * 2));
    }

    /* TIMPRE=1 selects HCLK through APB /1, /2 and /4, then 4*PCLK. */
    CHECK(dm_stm32h7_apb1_timer_clock_hz(hclk_hz, 0x40, 1u << 15) ==
          hclk_hz);
    CHECK(dm_stm32h7_apb1_timer_clock_hz(hclk_hz, 0x50, 1u << 15) ==
          hclk_hz);
    CHECK(dm_stm32h7_apb1_timer_clock_hz(hclk_hz, 0x50, 1u << 15) ==
          hclk_hz);
    CHECK(dm_stm32h7_apb1_timer_clock_hz(hclk_hz, 0x60, 1u << 15) ==
          hclk_hz / 2);
    CHECK(dm_stm32h7_apb1_timer_clock_hz(hclk_hz, 0x70, 1u << 15) ==
          hclk_hz / 4);
    CHECK(dm_stm32h7_apb2_timer_clock_hz(hclk_hz, 0x0700, 1u << 15) ==
          hclk_hz / 4);
}

int main(void)
{
    test_hpre_table();
    test_core_and_hclk_are_independent();
    test_apb_and_timer_tables();
    puts("clock tree smoke: PASS");
    return 0;
}
