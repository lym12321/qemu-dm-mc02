/* Deterministic, board-independent STM32H723 IWDG timing helpers. */
#ifndef HW_ARM_DM_MC02_IWDG_TIMING_H
#define HW_ARM_DM_MC02_IWDG_TIMING_H

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_IWDG_PPM_SCALE           UINT32_C(1000000)
#define DM_MC02_IWDG_MIN_LSI_ERROR_PPM   (-999999)
#define DM_MC02_IWDG_MAX_LSI_ERROR_PPM   1000000
/* ST's H7 HAL documents a status-register update time of up to five LSI
 * periods.  The component uses that published upper bound rather than a
 * host-dependent synchronizer phase. */
#define DM_MC02_IWDG_STATUS_UPDATE_LSI_CYCLES UINT32_C(5)

/* Return false for a zero nominal clock or a scale that would not produce a
 * positive clock.  The ppm bounds are deliberately explicit so command-line
 * input cannot create an invalid rational denominator. */
bool dm_mc02_iwdg_lsi_config_valid(uint32_t nominal_hz, int32_t error_ppm);

/* Return the rounded effective frequency for diagnostics.  Zero means that
 * the configuration is invalid. */
uint64_t dm_mc02_iwdg_effective_lsi_hz(uint32_t nominal_hz,
                                      int32_t error_ppm);

/* Return the deterministic upper-bound delay for a PR/RLR/WINR update to
 * cross into the IWDG clock domain.  The delay uses the same fixed LSI error
 * model as the watchdog timeout; invalid input returns INT64_MAX. */
uint64_t dm_mc02_iwdg_status_update_delay_ns_for_config(uint32_t nominal_hz,
                                                        int32_t error_ppm);

/* Return the timeout in virtual nanoseconds for PR and RLR.  INT64_MAX is
 * returned for an invalid configuration or a timeout outside QEMU's signed
 * timer range. */
uint64_t dm_mc02_iwdg_timeout_ns_for_config(uint32_t nominal_hz,
                                           int32_t error_ppm,
                                           uint32_t prescaler_index,
                                           uint32_t reload);

/* Derive the visible down-counter from an absolute virtual deadline.  The
 * caller supplies a started watchdog's deadline; invalid input returns the
 * reload value and an elapsed deadline returns zero. */
uint32_t dm_mc02_iwdg_counter_for_deadline(uint32_t nominal_hz,
                                           int32_t error_ppm,
                                           uint32_t prescaler_index,
                                           uint32_t reload,
                                           uint64_t deadline_ns,
                                           uint64_t now_ns);

#endif
