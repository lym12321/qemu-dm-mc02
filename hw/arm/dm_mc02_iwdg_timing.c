/* Deterministic, board-independent STM32H723 IWDG timing helpers. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_iwdg_timing.h"

#define NANOSECONDS_PER_SECOND UINT64_C(1000000000)

static const uint32_t iwdg_prescaler[] = { 4, 8, 16, 32, 64, 128, 256 };

bool dm_mc02_iwdg_lsi_config_valid(uint32_t nominal_hz, int32_t error_ppm)
{
    return nominal_hz != 0 &&
           error_ppm >= DM_MC02_IWDG_MIN_LSI_ERROR_PPM &&
           error_ppm <= DM_MC02_IWDG_MAX_LSI_ERROR_PPM;
}

static uint64_t dm_mc02_iwdg_lsi_scale(int32_t error_ppm)
{
    return (uint64_t)((int64_t)DM_MC02_IWDG_PPM_SCALE + error_ppm);
}

uint64_t dm_mc02_iwdg_effective_lsi_hz(uint32_t nominal_hz,
                                      int32_t error_ppm)
{
    __uint128_t numerator;
    uint64_t scale;

    if (!dm_mc02_iwdg_lsi_config_valid(nominal_hz, error_ppm)) {
        return 0;
    }
    scale = dm_mc02_iwdg_lsi_scale(error_ppm);
    numerator = (__uint128_t)nominal_hz * scale +
                DM_MC02_IWDG_PPM_SCALE / 2;
    return (uint64_t)(numerator / DM_MC02_IWDG_PPM_SCALE);
}

uint64_t dm_mc02_iwdg_status_update_delay_ns_for_config(uint32_t nominal_hz,
                                                        int32_t error_ppm)
{
    __uint128_t numerator;
    __uint128_t denominator;
    __uint128_t delay;
    uint64_t scale;

    if (!dm_mc02_iwdg_lsi_config_valid(nominal_hz, error_ppm)) {
        return INT64_MAX;
    }

    scale = dm_mc02_iwdg_lsi_scale(error_ppm);
    numerator = (__uint128_t)DM_MC02_IWDG_STATUS_UPDATE_LSI_CYCLES *
                NANOSECONDS_PER_SECOND * DM_MC02_IWDG_PPM_SCALE;
    denominator = (__uint128_t)nominal_hz * scale;
    delay = (numerator + denominator - 1) / denominator;
    if (delay > INT64_MAX) {
        return INT64_MAX;
    }
    return MAX(UINT64_C(1), (uint64_t)delay);
}

uint64_t dm_mc02_iwdg_timeout_ns_for_config(uint32_t nominal_hz,
                                           int32_t error_ppm,
                                           uint32_t prescaler_index,
                                           uint32_t reload)
{
    __uint128_t numerator;
    __uint128_t denominator;
    __uint128_t timeout;
    uint64_t scale;
    uint64_t ticks;

    if (!dm_mc02_iwdg_lsi_config_valid(nominal_hz, error_ppm) ||
        prescaler_index >= ARRAY_SIZE(iwdg_prescaler) || reload > 0xfff) {
        return INT64_MAX;
    }

    scale = dm_mc02_iwdg_lsi_scale(error_ppm);
    ticks = ((uint64_t)reload + 1) * iwdg_prescaler[prescaler_index];
    /* timeout = (reload + 1) * prescaler * 1e9 * 1e6 /
     *          (nominal_hz * (1e6 + error_ppm)). */
    numerator = (__uint128_t)ticks * NANOSECONDS_PER_SECOND *
                DM_MC02_IWDG_PPM_SCALE;
    denominator = (__uint128_t)nominal_hz * scale;
    timeout = (numerator + denominator - 1) / denominator;
    if (timeout > INT64_MAX) {
        return INT64_MAX;
    }
    return MAX(UINT64_C(1), (uint64_t)timeout);
}

uint32_t dm_mc02_iwdg_counter_for_deadline(uint32_t nominal_hz,
                                           int32_t error_ppm,
                                           uint32_t prescaler_index,
                                           uint32_t reload,
                                           uint64_t deadline_ns,
                                           uint64_t now_ns)
{
    __uint128_t numerator;
    __uint128_t denominator;
    uint64_t scale;
    uint64_t remaining_ns;
    uint64_t remaining_ticks;

    if (!dm_mc02_iwdg_lsi_config_valid(nominal_hz, error_ppm) ||
        prescaler_index >= ARRAY_SIZE(iwdg_prescaler) || reload > 0xfff) {
        return reload;
    }
    /* A zero deadline is the component's invalid/not-started sentinel, not
     * an already elapsed deadline.  Keep this distinction observable to
     * callers that use the helper directly at virtual time zero. */
    if (!deadline_ns) {
        return reload;
    }
    if (now_ns >= deadline_ns) {
        return 0;
    }

    scale = dm_mc02_iwdg_lsi_scale(error_ppm);
    remaining_ns = deadline_ns - now_ns;
    /* remaining ticks = remaining_ns * nominal_hz * scale /
     *                   (1e6 * prescaler * 1e9). */
    numerator = (__uint128_t)remaining_ns * nominal_hz * scale;
    denominator = (__uint128_t)DM_MC02_IWDG_PPM_SCALE *
                  iwdg_prescaler[prescaler_index] * NANOSECONDS_PER_SECOND;
    if (numerator > (__uint128_t)(reload + 1) * denominator) {
        remaining_ticks = (uint64_t)reload + 1;
    } else {
        remaining_ticks = (uint64_t)((numerator + denominator - 1) /
                                     denominator);
    }

    return remaining_ticks ? remaining_ticks - 1 : 0;
}
