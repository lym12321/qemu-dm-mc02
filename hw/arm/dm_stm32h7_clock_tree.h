/*
 * Board-independent STM32H7 clock divider helpers.
 *
 * The helpers operate only on effective clock roots and RCC divider values.
 * RCC source selection and board-specific kernel muxes remain owned by the
 * RCC model that calls them.
 */
#ifndef HW_ARM_DM_STM32H7_CLOCK_TREE_H
#define HW_ARM_DM_STM32H7_CLOCK_TREE_H

#include <stdint.h>

uint64_t dm_stm32h7_d1_core_divider(uint32_t d1cfgr);
uint64_t dm_stm32h7_d1_hpre_divider(uint32_t d1cfgr);
uint64_t dm_stm32h7_cpu_clock_hz(uint64_t sysclk_hz, uint32_t d1cfgr);
uint64_t dm_stm32h7_hclk_hz(uint64_t sysclk_hz, uint32_t d1cfgr);

uint64_t dm_stm32h7_d2_apb1_divider(uint32_t d2cfgr);
uint64_t dm_stm32h7_d2_apb2_divider(uint32_t d2cfgr);
uint64_t dm_stm32h7_apb1_hz(uint64_t hclk_hz, uint32_t d2cfgr);
uint64_t dm_stm32h7_apb2_hz(uint64_t hclk_hz, uint32_t d2cfgr);
uint64_t dm_stm32h7_apb1_timer_clock_hz(uint64_t hclk_hz,
                                        uint32_t d2cfgr, uint32_t cfgr);
uint64_t dm_stm32h7_apb2_timer_clock_hz(uint64_t hclk_hz,
                                        uint32_t d2cfgr, uint32_t cfgr);

#endif
