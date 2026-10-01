/* Minimal STM32H723 timer model used by the DM-MC02 machine. */
#ifndef HW_ARM_DM_MC02_TIM2_H
#define HW_ARM_DM_MC02_TIM2_H

#include "exec/memory.h"
#include "hw/clock.h"
#include "migration/vmstate.h"
#include "qemu/timer.h"

#include <stdbool.h>
#include <stdint.h>

typedef void DmMc02TimChanged(void *opaque);
typedef void DmMc02TimUpdateCallback(void *opaque);
typedef void DmMc02TimCompareCallback(void *opaque, unsigned events,
                                      uint64_t timestamp_ns);

typedef enum DmMc02TimMasterOutput {
    DM_MC02_TIM_TRGO,
    DM_MC02_TIM_TRGO2,
} DmMc02TimMasterOutput;

typedef enum DmMc02TimMasterEventKind {
    DM_MC02_TIM_MASTER_RESET,
    DM_MC02_TIM_MASTER_ENABLE,
    DM_MC02_TIM_MASTER_UPDATE,
    DM_MC02_TIM_MASTER_COMPARE1,
    DM_MC02_TIM_MASTER_OC1REF,
    DM_MC02_TIM_MASTER_OC2REF,
    DM_MC02_TIM_MASTER_OC3REF,
    DM_MC02_TIM_MASTER_OC4REF,
} DmMc02TimMasterEventKind;

typedef struct DmMc02TimMasterEvent {
    DmMc02TimMasterOutput output;
    DmMc02TimMasterEventKind kind;
    bool rising;
    /* Number of contiguous hardware events represented by this callback.
     * timestamp_ns is the planned time of the last represented event. */
    unsigned event_count;
    uint64_t timestamp_ns;
} DmMc02TimMasterEvent;

typedef void DmMc02TimMasterEventCallback(
    void *opaque, const DmMc02TimMasterEvent *event);

typedef struct DmMc02TimPwmOutputState {
    bool main_enabled;
    bool main_level;
    bool complementary_supported;
    bool complementary_enabled;
    bool complementary_level;
    /* DTG decoded in tDTS ticks, then converted to timer input cycles. */
    uint32_t dead_time_ticks;
    uint32_t dead_time_cycles;
    uint64_t dead_time_ns;
} DmMc02TimPwmOutputState;

static inline uint32_t dm_mc02_tim2_decode_dead_time_ticks(uint32_t dtg)
{
    dtg &= 0xffu;
    if (!(dtg & 0x80u)) {
        return dtg & 0x7fu;
    }
    if ((dtg & 0xc0u) == 0x80u) {
        return (64u + (dtg & 0x3fu)) * 2u;
    }
    if ((dtg & 0xe0u) == 0xc0u) {
        return (32u + (dtg & 0x1fu)) * 8u;
    }
    return (32u + (dtg & 0x1fu)) * 16u;
}

#define DM_MC02_TIM2_REGION_SIZE 0x400

typedef struct DmMc02Tim2 {
    MemoryRegion iomem;
    uint32_t regs[DM_MC02_TIM2_REGION_SIZE / sizeof(uint32_t)];
    /* CPU-visible timer configuration is the shadow side.  These values are
     * the active counter/compare configuration used by virtual time. */
    uint32_t active_psc;
    uint32_t active_arr;
    uint32_t active_ccr[4];
    uint32_t active_rcr;
    uint32_t repetition_remaining;
    uint64_t start_ns;
    /* Direction at start_ns for the lazy triangular phase. */
    bool start_counting_down;
    /* Absolute virtual-time deadline for the next abstract update event.
     * Keeping the phase outside the timer callback avoids accumulating host
     * scheduling delay over long runs. */
    uint64_t next_update_ns;
    /* Cached virtual interval for one qualified update callback.  Zero means
     * the PSC/ARR/RCR/clock/batch combination needs recalculation. */
    uint64_t update_interval_ns;
    QEMUTimer *update_timer;
    QEMUTimer *compare_timer;
    uint64_t next_compare_ns;
    uint8_t compare_channel_mask;
    bool cc1_active;
    bool cc2_active;
    bool cc3_active;
    bool cc4_active;
    /* Persistent OCxREF state for frozen/active/inactive/toggle modes. */
    uint8_t ocref_level_mask;
    bool repetition_supported;
    bool mms2_supported;
    bool break_supported;
    bool complementary_supported;
    /* Physical BKIN level.  Reset high is inactive for active-low BKP. */
    bool break_input_level;
    bool break_latched;
    qemu_irq irq;
    bool irq_level;
    bool irq_level_valid;
    /* Number of timer periods represented by one update callback. */
    unsigned update_batch;
    /* Number of CC1 compare events represented by one compare callback.
     * Master compare outputs always force this to one. */
    unsigned compare_batch;
    Clock *clock;
    DmMc02TimUpdateCallback *update_callback;
    void *update_opaque;
    DmMc02TimCompareCallback *compare_callback;
    void *compare_opaque;
    DmMc02TimMasterEventCallback *event_callback;
    void *event_opaque;
    DmMc02TimChanged *changed;
    void *changed_opaque;
} DmMc02Tim2;

void dm_mc02_tim2_init(DmMc02Tim2 *state, Object *owner,
                       DmMc02TimChanged *changed, void *changed_opaque);

void dm_mc02_tim2_set_update_callback(DmMc02Tim2 *state,
                                       DmMc02TimUpdateCallback *callback,
                                       void *opaque);
/* Connect a consumer to CC1 compare events independently of CR2.MMS.  The
 * callback may receive a bounded coalesced event count for high-rate users;
 * timestamp_ns is the planned virtual deadline of the last event. */
void dm_mc02_tim2_set_compare_callback(
    DmMc02Tim2 *state, DmMc02TimCompareCallback *callback, void *opaque);
/* Connect the generic timer master-event output.  The callback is invoked
 * only for events selected by CR2.MMS or CR2.MMS2. */
void dm_mc02_tim2_set_event_callback(
    DmMc02Tim2 *state, DmMc02TimMasterEventCallback *callback,
    void *opaque);
/* MMS2 is an advanced-timer capability (TIM1/TIM8 on H723), not a property
 * of every instance that shares this compact register implementation. */
void dm_mc02_tim2_set_mms2_supported(DmMc02Tim2 *state, bool supported);
/* RCR is an advanced-timer capability (TIM1/TIM8 on H723).  When enabled,
 * update/preload events occur after REP + 1 counter cycles; compare matches
 * remain per-cycle. */
void dm_mc02_tim2_set_repetition_supported(DmMc02Tim2 *state, bool supported);
/* BDTR/MOE and BKIN handling are an advanced-timer capability (TIM1/TIM8 on
 * H723).  The break input level is the physical BKIN level; BDTR.BKP selects
 * which level is asserted.  Break only gates the external output stage, not
 * OCREF or internal compare/trigger events. */
void dm_mc02_tim2_set_break_supported(DmMc02Tim2 *state, bool supported);
void dm_mc02_tim2_set_break_input(DmMc02Tim2 *state, bool level);
/* TIM1/TIM8 expose CH1N..CH3N and the dead-time generator. */
void dm_mc02_tim2_set_complementary_supported(DmMc02Tim2 *state,
                                               bool supported);
void dm_mc02_tim2_set_update_batch(DmMc02Tim2 *state, unsigned batch);
unsigned dm_mc02_tim2_get_update_batch(const DmMc02Tim2 *state);
void dm_mc02_tim2_set_compare_batch(DmMc02Tim2 *state, unsigned batch);
unsigned dm_mc02_tim2_get_compare_batch(const DmMc02Tim2 *state);

/* Evaluate PWM lazily at the current virtual time.  No host timer is
 * scheduled per output edge, keeping high-frequency PWM cheap to observe. */
bool dm_mc02_tim2_get_pwm_state(const DmMc02Tim2 *state, unsigned channel,
                                bool *enabled, bool *level,
                                uint64_t *frequency_hz,
                                uint32_t *duty_permille);
/* Return the lazily evaluated external output stage for one PWM channel.
 * Main and complementary levels include CCxP/CCxNP polarity and the
 * programmed dead-time interval; OCREF itself remains an internal signal. */
bool dm_mc02_tim2_get_pwm_output_state(
    const DmMc02Tim2 *state, unsigned channel,
    DmMc02TimPwmOutputState *output);

/* Connect the peripheral clock.  A NULL clock retains the legacy 24 MHz
 * fallback for standalone users of this small model. */
void dm_mc02_tim2_set_clock(DmMc02Tim2 *state, Clock *clock);
/* Notify the instance after its connected clock frequency has changed. */
void dm_mc02_tim2_clock_changed(DmMc02Tim2 *state);
void dm_mc02_tim2_set_irq(DmMc02Tim2 *state, qemu_irq irq);

/* Rebuild QEMUTimer scheduling and the level-sensitive IRQ projection after
 * the component state has been restored.  Clock, timer objects, callbacks,
 * IRQ handles and profile capabilities remain caller-owned runtime wiring. */
void dm_mc02_tim2_sync_runtime(DmMc02Tim2 *state);

/* Validate the serialized scheduler state against the restored timer
 * configuration before any QEMUTimer or IRQ side effect is performed. */
bool dm_mc02_tim2_validate_vmstate(const DmMc02Tim2 *state);

/* Component-only state contract; runtime wiring is intentionally excluded. */
const VMStateDescription *dm_mc02_tim2_vmstate(void);

void dm_mc02_tim2_reset(DmMc02Tim2 *state);

#endif
