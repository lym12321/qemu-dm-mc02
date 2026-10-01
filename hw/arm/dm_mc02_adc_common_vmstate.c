/* Component-only VMState contract for the STM32H723 ADC common block. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_adc_common.h"
#include "migration/vmstate.h"

static bool dm_mc02_adc_common_fields_valid(
    const DmMc02AdcCommon *state)
{
    unsigned dual;
    unsigned damdf;
    unsigned max_count = 0;
    bool master_valid;
    bool slave_valid;

    if (!state) {
        return false;
    }
    dual = state->ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK;
    damdf = (state->ccr & DM_MC02_ADC_COMMON_CCR_DAMDF_MASK) >> 14;
    master_valid = state->master_sample_valid;
    slave_valid = state->slave_sample_valid;

    if (damdf == DM_MC02_ADC_COMMON_CCR_DAMDF_8 &&
        (dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS ||
         dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED ||
         dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT)) {
        max_count = dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS ?
                    1 : 3;
    }
    if ((state->ccr & ~DM_MC02_ADC_COMMON_CCR_WRITABLE_MASK) ||
        (state->cdr2_valid && state->cdr2_source > 1) ||
        (state->cdr2_valid &&
         !dm_mc02_adc_common_cdr2_dma_supported(state)) ||
        (state->damdf8_count > max_count) ||
        (state->damdf8_count == 0 && state->damdf8_timestamp_ns != 0) ||
        (state->damdf8_count != 0 &&
         state->damdf8_timestamp_ns > INT64_MAX) ||
        (state->damdf8_count &&
         (damdf != DM_MC02_ADC_COMMON_CCR_DAMDF_8 || !max_count)) ||
        ((dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED ||
          dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT) &&
         state->damdf8_count &&
         state->damdf8_next_source != (state->damdf8_count & 1u)) ||
        (state->damdf8_next_source > 1) ||
        (master_valid && (state->master_sample_conversion_id == 0 ||
                          state->master_sample_rank > 15 ||
                          state->master_sample_timestamp_ns > INT64_MAX)) ||
        (slave_valid && (state->slave_sample_conversion_id == 0 ||
                         state->slave_sample_rank > 15 ||
                         state->slave_sample_timestamp_ns > INT64_MAX)) ||
        ((master_valid || slave_valid) &&
         ((state->ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK) !=
              DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS ||
          (((state->ccr & DM_MC02_ADC_COMMON_CCR_DAMDF_MASK) >> 14) !=
               DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 &&
           ((state->ccr & DM_MC02_ADC_COMMON_CCR_DAMDF_MASK) >> 14) !=
               DM_MC02_ADC_COMMON_CCR_DAMDF_8)))) {
        return false;
    }

    return true;
}

bool dm_mc02_adc_common_state_valid(const DmMc02AdcCommon *state)
{
    uint64_t max_pending_conversion_id = 0;
    bool master_valid;
    bool slave_valid;

    if (!dm_mc02_adc_common_fields_valid(state)) {
        return false;
    }
    master_valid = state->master_sample_valid;
    slave_valid = state->slave_sample_valid;
    if (master_valid) {
        max_pending_conversion_id =
            MAX(max_pending_conversion_id,
                state->master_sample_conversion_id);
    }
    if (slave_valid) {
        max_pending_conversion_id =
            MAX(max_pending_conversion_id,
                state->slave_sample_conversion_id);
    }
    return max_pending_conversion_id <= state->next_conversion_id;
}

static int dm_mc02_adc_common_validate_post_load(void *opaque,
                                                   int version_id)
{
    DmMc02AdcCommon *state = opaque;
    uint64_t max_pending_conversion_id = 0;
    bool master_valid;
    bool slave_valid;

    if (!state || version_id < 1 || version_id > 6) {
        return -EINVAL;
    }
    if (version_id < 3) {
        /* CDR2 source/validity did not exist in older component streams.
         * Clear the destination-side fields before validating the remaining
         * state so a reused destination cannot leak stale runtime metadata. */
        state->cdr2_valid = false;
        state->cdr2_source = 0;
    }
    if (version_id < 4) {
        /* DAMDF=3 used no component accumulator before this version. */
        state->damdf8_data = 0;
        state->damdf8_count = 0;
        state->damdf8_next_source = 0;
    }
    if (version_id < 5) {
        /* v4 could restore the packed bytes and source cursor, but had no
         * aggregate timestamp.  Do not retain stale destination state. */
        state->damdf8_timestamp_ns = 0;
    }
    if (version_id < 6) {
        /* Earlier component streams did not distinguish an unread CDR word
         * from its retained register value.  Do not invent a retryable
         * producer item when loading one of those streams. */
        state->cdr_valid = false;
    }
    /* A direct source reservation is synchronous runtime state.  It must
     * never cross the component restore boundary. */
    state->cdr_read_reserved = false;
    state->cdr2_read_reserved = false;
    state->cdr2_read_reserved_source = 0;

    if (!dm_mc02_adc_common_fields_valid(state)) {
        return -EINVAL;
    }
    master_valid = state->master_sample_valid;
    slave_valid = state->slave_sample_valid;
    if (master_valid) {
        max_pending_conversion_id =
            MAX(max_pending_conversion_id,
                state->master_sample_conversion_id);
    }
    if (slave_valid) {
        max_pending_conversion_id =
            MAX(max_pending_conversion_id,
                state->slave_sample_conversion_id);
    }
    if (version_id == 1) {
        /* v1 called this field sequence, but it occupied the same wire
         * position.  Advance the newly introduced generator past any
         * restored legacy pending event before a new master start can use
         * the ID.  UINT64_MAX cannot be advanced without colliding after
         * the zero marker is skipped, so reject that unrepresentable legacy
         * continuation rather than risking a false pair. */
        if (max_pending_conversion_id == UINT64_MAX) {
            return -EINVAL;
        }
        state->next_conversion_id = max_pending_conversion_id;
    } else if (max_pending_conversion_id > state->next_conversion_id) {
        /* A v2-or-newer stream must preserve the generator frontier. */
        return -EINVAL;
    }
    return 0;
}

static int dm_mc02_adc_common_post_load(void *opaque, int version_id)
{
    DmMc02AdcCommon *state = opaque;
    int ret = dm_mc02_adc_common_validate_post_load(opaque, version_id);

    if (ret) {
        return ret;
    }
    dm_mc02_adc_common_sync_runtime(state);
    return 0;
}

static int dm_mc02_adc_common_pre_save(void *opaque)
{
    DmMc02AdcCommon *state = opaque;

    return state && !state->cdr_read_reserved &&
           !state->cdr2_read_reserved ? 0 : -EINVAL;
}

const VMStateDescription vmstate_dm_mc02_adc_common_raw = {
    .name = "dm-mc02-adc-common",
    .version_id = 6,
    .minimum_version_id = 1,
    .pre_save = dm_mc02_adc_common_pre_save,
    .post_load = dm_mc02_adc_common_validate_post_load,
    .fields = (VMStateField[]) {
        VMSTATE_UINT32(ccr, DmMc02AdcCommon),
        VMSTATE_UINT32(cdr, DmMc02AdcCommon),
        VMSTATE_BOOL_V(cdr_valid, DmMc02AdcCommon, 6),
        VMSTATE_UINT32(cdr2, DmMc02AdcCommon),
        VMSTATE_UINT32_V(damdf8_data, DmMc02AdcCommon, 4),
        VMSTATE_UINT8_V(damdf8_count, DmMc02AdcCommon, 4),
        VMSTATE_UINT8_V(damdf8_next_source, DmMc02AdcCommon, 4),
        VMSTATE_UINT64_V(damdf8_timestamp_ns, DmMc02AdcCommon, 5),
        VMSTATE_BOOL_V(cdr2_valid, DmMc02AdcCommon, 3),
        VMSTATE_UINT8_V(cdr2_source, DmMc02AdcCommon, 3),
        VMSTATE_BOOL(ccr_configured, DmMc02AdcCommon),
        VMSTATE_UINT64_V(next_conversion_id, DmMc02AdcCommon, 2),
        VMSTATE_BOOL(master_sample_valid, DmMc02AdcCommon),
        VMSTATE_UINT64(master_sample_conversion_id, DmMc02AdcCommon),
        VMSTATE_UINT32(master_sample_rank, DmMc02AdcCommon),
        VMSTATE_UINT16(master_sample_value, DmMc02AdcCommon),
        VMSTATE_UINT64(master_sample_timestamp_ns, DmMc02AdcCommon),
        VMSTATE_BOOL(slave_sample_valid, DmMc02AdcCommon),
        VMSTATE_UINT64(slave_sample_conversion_id, DmMc02AdcCommon),
        VMSTATE_UINT32(slave_sample_rank, DmMc02AdcCommon),
        VMSTATE_UINT16(slave_sample_value, DmMc02AdcCommon),
        VMSTATE_UINT64(slave_sample_timestamp_ns, DmMc02AdcCommon),
        VMSTATE_END_OF_LIST()
    },
};

static const VMStateDescription vmstate_dm_mc02_adc_common = {
    .name = "dm-mc02-adc-common",
    .version_id = 6,
    .minimum_version_id = 1,
    .pre_save = dm_mc02_adc_common_pre_save,
    .post_load = dm_mc02_adc_common_post_load,
    .fields = vmstate_dm_mc02_adc_common_raw.fields,
};

const VMStateDescription *dm_mc02_adc_common_vmstate(void)
{
    return &vmstate_dm_mc02_adc_common;
}
