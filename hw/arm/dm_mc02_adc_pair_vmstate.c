/* Composite VMState for the reusable STM32H723 ADC pair. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_adc_pair.h"
#include "migration/vmstate.h"

static bool dm_mc02_adc_pair_shared_ids_valid(
    const DmMc02AdcPair *state)
{
    unsigned dual = dm_mc02_adc_common_get_dual(&state->common);
    bool common_owned =
        dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS ||
        dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED ||
        dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT;
    bool master_pending = state->common.master_sample_valid;
    bool slave_pending = state->common.slave_sample_valid;

    if (!common_owned) {
        return !master_pending && !slave_pending;
    }
    if (state->adc[0].regular_shared_conversion !=
        state->adc[1].regular_shared_conversion) {
        return false;
    }
    if ((master_pending || slave_pending) &&
        !state->adc[0].regular_shared_conversion) {
        return false;
    }
    if (master_pending &&
        state->common.master_sample_conversion_id !=
        state->adc[0].regular_active_sequence_id) {
        return false;
    }
    if (slave_pending &&
        state->common.slave_sample_conversion_id !=
        state->adc[1].regular_active_sequence_id) {
        return false;
    }
    return true;
}

bool dm_mc02_adc_pair_state_valid(const DmMc02AdcPair *state)
{
    return state && dm_mc02_adc_common_state_valid(&state->common) &&
           dm_mc02_adc_state_valid(&state->adc[0]) &&
           dm_mc02_adc_state_valid(&state->adc[1]) &&
           dm_mc02_adc_pair_shared_ids_valid(state);
}

void dm_mc02_adc_pair_sync_runtime(DmMc02AdcPair *state)
{
    if (!state) {
        return;
    }

    /* The common callback derives the effective ADC kernel clock.  Both ADC
     * schedulers must see that projection before their saved deadlines are
     * re-armed. */
    dm_mc02_adc_common_sync_runtime(&state->common);
    dm_mc02_adc_sync_runtime(&state->adc[0]);
    dm_mc02_adc_sync_runtime(&state->adc[1]);
}

static int dm_mc02_adc_pair_post_load(void *opaque, int version_id)
{
    DmMc02AdcPair *state = opaque;

    if (!state || version_id != 1 ||
        !dm_mc02_adc_pair_state_valid(state)) {
        return -EINVAL;
    }
    dm_mc02_adc_pair_sync_runtime(state);
    return 0;
}

static const VMStateDescription vmstate_dm_mc02_adc_pair = {
    .name = "dm-mc02-adc-pair",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_adc_pair_post_load,
    .fields = (VMStateField[]) {
        /* Common is the producer of shared conversion IDs and CDR state. */
        VMSTATE_STRUCT(common, DmMc02AdcPair, 0,
                       vmstate_dm_mc02_adc_common_raw, DmMc02AdcCommon),
        VMSTATE_STRUCT_ARRAY(adc, DmMc02AdcPair, DM_MC02_ADC_PAIR_COUNT, 0,
                             vmstate_dm_mc02_adc_raw, DmMc02Adc),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_adc_pair_vmstate(void)
{
    return &vmstate_dm_mc02_adc_pair;
}
