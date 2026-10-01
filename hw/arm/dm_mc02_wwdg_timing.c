/* Deterministic WWDG timing conversion, shared by the component and tests. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_wwdg.h"
#include "hw/arm/dm_mc02_wwdg_timing.h"

uint64_t dm_mc02_wwdg_tick_ns_for_config(uint64_t clock_hz,
                                         uint32_t wdgtb)
{
    __uint128_t divider;
    __uint128_t numerator;
    uint64_t result;

    if (!clock_hz || wdgtb > 7) {
        return 0;
    }
    divider = (__uint128_t)DM_MC02_WWDG_INTERNAL_DIVIDER << wdgtb;
    numerator = divider * UINT64_C(1000000000) + clock_hz - 1;
    result = numerator / clock_hz;
    return result > INT64_MAX ? INT64_MAX : MAX(result, UINT64_C(1));
}
