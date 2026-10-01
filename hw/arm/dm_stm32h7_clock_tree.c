/*
 * Board-independent STM32H7 D1 clock divider helpers.
 */
#include "hw/arm/dm_stm32h7_clock_tree.h"

#include <limits.h>

uint64_t dm_stm32h7_d1_core_divider(uint32_t d1cfgr)
{
    static const uint16_t dividers[16] = {
        1, 1, 1, 1, 2, 4, 8, 16,
        2, 4, 8, 16, 64, 128, 256, 512,
    };

    return dividers[(d1cfgr >> 8) & 0xfu];
}

uint64_t dm_stm32h7_d1_hpre_divider(uint32_t d1cfgr)
{
    static const uint16_t dividers[16] = {
        1, 1, 1, 1, 1, 1, 1, 1,
        2, 4, 8, 16, 64, 128, 256, 512,
    };

    return dividers[d1cfgr & 0xfu];
}

uint64_t dm_stm32h7_cpu_clock_hz(uint64_t sysclk_hz, uint32_t d1cfgr)
{
    return sysclk_hz / dm_stm32h7_d1_core_divider(d1cfgr);
}

uint64_t dm_stm32h7_hclk_hz(uint64_t sysclk_hz, uint32_t d1cfgr)
{
    return sysclk_hz / dm_stm32h7_d1_hpre_divider(d1cfgr);
}

static uint64_t dm_stm32h7_d2_apb_divider(uint32_t d2cfgr,
                                          unsigned shift)
{
    static const uint8_t dividers[8] = {
        1, 1, 1, 1, 2, 4, 8, 16,
    };

    return dividers[(d2cfgr >> shift) & 7u];
}

uint64_t dm_stm32h7_d2_apb1_divider(uint32_t d2cfgr)
{
    return dm_stm32h7_d2_apb_divider(d2cfgr, 4);
}

uint64_t dm_stm32h7_d2_apb2_divider(uint32_t d2cfgr)
{
    return dm_stm32h7_d2_apb_divider(d2cfgr, 8);
}

uint64_t dm_stm32h7_apb1_hz(uint64_t hclk_hz, uint32_t d2cfgr)
{
    return hclk_hz / dm_stm32h7_d2_apb1_divider(d2cfgr);
}

uint64_t dm_stm32h7_apb2_hz(uint64_t hclk_hz, uint32_t d2cfgr)
{
    return hclk_hz / dm_stm32h7_d2_apb2_divider(d2cfgr);
}

static uint64_t dm_stm32h7_timer_clock_hz(uint64_t hclk_hz,
                                          uint64_t apb_hz,
                                          uint64_t apb_divider,
                                          uint32_t cfgr)
{
    if (!(cfgr & (1u << 15))) { /* TIMPRE=0 */
        if (apb_divider == 1) {
            return apb_hz;
        }
        return apb_hz > UINT64_MAX / 2 ? UINT64_MAX : apb_hz * 2;
    }
    if (apb_divider <= 4) {
        return hclk_hz;
    }
    return apb_hz > UINT64_MAX / 4 ? UINT64_MAX : apb_hz * 4;
}

uint64_t dm_stm32h7_apb1_timer_clock_hz(uint64_t hclk_hz,
                                        uint32_t d2cfgr, uint32_t cfgr)
{
    return dm_stm32h7_timer_clock_hz(
        hclk_hz, dm_stm32h7_apb1_hz(hclk_hz, d2cfgr),
        dm_stm32h7_d2_apb1_divider(d2cfgr), cfgr);
}

uint64_t dm_stm32h7_apb2_timer_clock_hz(uint64_t hclk_hz,
                                        uint32_t d2cfgr, uint32_t cfgr)
{
    return dm_stm32h7_timer_clock_hz(
        hclk_hz, dm_stm32h7_apb2_hz(hclk_hz, d2cfgr),
        dm_stm32h7_d2_apb2_divider(d2cfgr), cfgr);
}
