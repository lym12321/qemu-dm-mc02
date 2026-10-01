/* Protocol-neutral timing helpers for the STM32H7 system window watchdog. */
#ifndef HW_ARM_DM_MC02_WWDG_TIMING_H
#define HW_ARM_DM_MC02_WWDG_TIMING_H

#include <stdint.h>

/* WWDG tick period is ceil(4096 * 2^WDGTB / clock_hz) nanoseconds. */
uint64_t dm_mc02_wwdg_tick_ns_for_config(uint64_t clock_hz,
                                         uint32_t wdgtb);

#endif
