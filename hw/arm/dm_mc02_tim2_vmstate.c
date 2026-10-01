/* Component-only VMState contract for the board-independent timer model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_tim2.h"
#include "migration/vmstate.h"

#define TIM2_VMSTATE_CR1_OFFSET 0x00
#define TIM2_VMSTATE_CR1_CEN    (1u << 0)

static int dm_mc02_tim2_post_load(void *opaque, int version_id)
{
    DmMc02Tim2 *state = opaque;
    uint32_t cr1 = ldl_le_p((uint8_t *)state->regs +
                            TIM2_VMSTATE_CR1_OFFSET);

    if (version_id != 1 ||
        state->active_psc > UINT16_MAX ||
        state->active_rcr > UINT16_MAX ||
        state->repetition_remaining > state->active_rcr ||
        !state->update_batch ||
        !state->compare_batch ||
        state->compare_channel_mask & ~0x0fu ||
        state->ocref_level_mask & ~0x0fu ||
        state->start_ns > INT64_MAX ||
        state->next_update_ns > INT64_MAX ||
        state->next_compare_ns > INT64_MAX ||
        (state->next_compare_ns && !state->compare_channel_mask) ||
        (!(cr1 & TIM2_VMSTATE_CR1_CEN) &&
         (state->next_update_ns || state->next_compare_ns)) ||
        !dm_mc02_tim2_validate_vmstate(state)) {
        return -EINVAL;
    }

    /* QEMUTimer objects, Clock, IRQ and callbacks are runtime wiring.  Only
     * reschedule them after the complete producer state has passed checks. */
    dm_mc02_tim2_sync_runtime(state);
    /* Board consumers may cache the projected PWM/trigger state.  The
     * callback is runtime wiring, so notify it only after a successful load
     * and runtime reconstruction. */
    if (state->changed) {
        state->changed(state->changed_opaque);
    }
    return 0;
}

static const VMStateDescription vmstate_dm_mc02_tim2 = {
    .name = "dm-mc02-tim2",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_tim2_post_load,
    .fields = (VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, DmMc02Tim2,
                             DM_MC02_TIM2_REGION_SIZE / sizeof(uint32_t)),
        VMSTATE_UINT32(active_psc, DmMc02Tim2),
        VMSTATE_UINT32(active_arr, DmMc02Tim2),
        VMSTATE_UINT32_ARRAY(active_ccr, DmMc02Tim2, 4),
        VMSTATE_UINT32(active_rcr, DmMc02Tim2),
        VMSTATE_UINT32(repetition_remaining, DmMc02Tim2),
        VMSTATE_UINT64(start_ns, DmMc02Tim2),
        VMSTATE_BOOL(start_counting_down, DmMc02Tim2),
        VMSTATE_UINT64(next_update_ns, DmMc02Tim2),
        VMSTATE_UINT64(next_compare_ns, DmMc02Tim2),
        VMSTATE_UINT8(compare_channel_mask, DmMc02Tim2),
        VMSTATE_BOOL(cc1_active, DmMc02Tim2),
        VMSTATE_BOOL(cc2_active, DmMc02Tim2),
        VMSTATE_BOOL(cc3_active, DmMc02Tim2),
        VMSTATE_BOOL(cc4_active, DmMc02Tim2),
        VMSTATE_UINT8(ocref_level_mask, DmMc02Tim2),
        VMSTATE_BOOL(break_input_level, DmMc02Tim2),
        VMSTATE_BOOL(break_latched, DmMc02Tim2),
        VMSTATE_UINT32(update_batch, DmMc02Tim2),
        VMSTATE_UINT32(compare_batch, DmMc02Tim2),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_tim2_vmstate(void)
{
    return &vmstate_dm_mc02_tim2;
}
