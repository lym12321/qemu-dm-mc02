/* Runtime-sync stub for the isolated timer VMState contract test. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_tim2.h"

unsigned dm_mc02_tim2_sync_calls;

void dm_mc02_tim2_sync_runtime(DmMc02Tim2 *state)
{
    dm_mc02_tim2_sync_calls++;
    state->update_interval_ns = 0;
    state->irq_level_valid = true;
}

bool dm_mc02_tim2_validate_vmstate(const DmMc02Tim2 *state)
{
    if (!state ||
        (state->next_update_ns && state->next_update_ns < state->start_ns) ||
        (state->next_compare_ns &&
         state->next_compare_ns < state->start_ns)) {
        return false;
    }
    if (state->next_compare_ns) {
        for (unsigned channel = 1; channel <= 4; ++channel) {
            uint8_t bit = 1u << (channel - 1);
            bool active;

            switch (channel) {
            case 1:
                active = state->cc1_active;
                break;
            case 2:
                active = state->cc2_active;
                break;
            case 3:
                active = state->cc3_active;
                break;
            default:
                active = state->cc4_active;
                break;
            }
            if ((state->compare_channel_mask & bit) &&
                (!active || state->active_ccr[channel - 1] >
                 state->active_arr)) {
                return false;
            }
        }
    }
    return true;
}
