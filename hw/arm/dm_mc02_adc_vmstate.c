/* Component-only VMState contract for the board-independent ADC model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_adc.h"
#include "migration/vmstate.h"

#define ADC_VMSTATE_CR_OFFSET        0x08
#define ADC_VMSTATE_CFGR_OFFSET      0x0c
#define ADC_VMSTATE_SQR1_OFFSET      0x30
#define ADC_VMSTATE_JSQR_OFFSET      0x4c
#define ADC_VMSTATE_CR_ADEN          (1u << 0)
#define ADC_VMSTATE_CR_ADSTART       (1u << 2)
#define ADC_VMSTATE_CR_JADSTART      (1u << 3)
#define ADC_VMSTATE_CR_ADCAL         (1u << 31)
#define ADC_VMSTATE_CFGR_CONT        (1u << 13)
#define ADC_VMSTATE_CFGR_DISCEN      (1u << 16)
#define ADC_VMSTATE_CFGR_JDISCEN     (1u << 20)
#define ADC_VMSTATE_CFGR_JAUTO       (1u << 25)
#define ADC_VMSTATE_SQR1_LENGTH_MASK 0x0fu
#define ADC_VMSTATE_JSQR_LENGTH_MASK 0x03u
#define ADC_VMSTATE_JSQR_EXTEN_MASK  (3u << 7)
#define ADC_VMSTATE_CHANNEL_COUNT    DM_MC02_ADC_CHANNEL_COUNT
#define ADC_VMSTATE_MAX_REGULAR_RANKS 16u

static uint32_t adc_vmstate_load(const DmMc02Adc *state, hwaddr offset)
{
    uint32_t value = 0;

    for (unsigned byte = 0; byte < sizeof(uint32_t); ++byte) {
        value |= (uint32_t)state->regs[offset + byte] << (byte * 8);
    }
    return value;
}

static unsigned adc_vmstate_regular_length(const DmMc02Adc *state)
{
    if (state->legacy_samples) {
        return 2;
    }
    return MIN((adc_vmstate_load(state, ADC_VMSTATE_SQR1_OFFSET) &
                ADC_VMSTATE_SQR1_LENGTH_MASK) + 1u,
               ADC_VMSTATE_MAX_REGULAR_RANKS);
}

static unsigned adc_vmstate_injected_length(uint32_t jsqr)
{
    return (jsqr & ADC_VMSTATE_JSQR_LENGTH_MASK) + 1u;
}

static bool adc_vmstate_input_state_valid(const DmMc02Adc *state)
{
    for (unsigned channel = 0; channel < ADC_VMSTATE_CHANNEL_COUNT;
         ++channel) {
        uint32_t bit = UINT32_C(1) << channel;
        uint8_t kind = state->external_override_kind[channel];

        switch (kind) {
        case 0:
            if ((state->external_raw_valid | state->external_pin_voltage_valid) &
                bit) {
                return false;
            }
            break;
        case 1:
            if (!(state->external_raw_valid & bit) ||
                (state->external_pin_voltage_valid & bit)) {
                return false;
            }
            break;
        case 2:
            if (!(state->external_pin_voltage_valid & bit) ||
                (state->external_raw_valid & bit)) {
                return false;
            }
            break;
        default:
            return false;
        }
    }
    return true;
}

static bool adc_vmstate_context_valid(const DmMc02Adc *state,
                                      uint32_t jsqr)
{
    bool active = state->injected_active_context_valid;
    bool pending = state->injected_pending_context_valid;

    if (pending && !active) {
        return false;
    }
    if (pending) {
        if (jsqr != state->injected_pending_jsqr) {
            return false;
        }
    } else if (active) {
        if (jsqr != state->injected_active_jsqr) {
            return false;
        }
    } else if (jsqr) {
        return false;
    }
    return true;
}

static bool adc_vmstate_timing_valid(const DmMc02Adc *state,
                                     uint32_t cr, uint32_t cfgr,
                                     uint32_t active_jsqr)
{
    unsigned regular_length = adc_vmstate_regular_length(state);
    unsigned injected_length = adc_vmstate_injected_length(active_jsqr);
    bool regular_active = state->conversion_active;
    bool injected_active = state->injected_conversion_active;

    if (state->current_rank >= regular_length ||
        state->current_injected_rank >= injected_length ||
        state->regular_discontinuous_remaining > regular_length) {
        return false;
    }
    if (state->regular_waiting_for_data &&
        (!regular_active || state->next_sample_ns)) {
        return false;
    }
    if (state->regular_discontinuous_pause_after_data &&
        !state->regular_waiting_for_data) {
        return false;
    }
    if (state->regular_discontinuous_paused &&
        (regular_active || state->next_sample_ns)) {
        return false;
    }
    if (state->regular_sequence_complete &&
        !state->regular_waiting_for_data) {
        return false;
    }
    if (state->regular_waiting_for_auto_injected &&
        (!regular_active || !injected_active)) {
        return false;
    }
    if (regular_active &&
        (cr & (ADC_VMSTATE_CR_ADEN | ADC_VMSTATE_CR_ADSTART)) !=
            (ADC_VMSTATE_CR_ADEN | ADC_VMSTATE_CR_ADSTART)) {
        return false;
    }
    if (injected_active &&
        (cr & (ADC_VMSTATE_CR_ADEN | ADC_VMSTATE_CR_JADSTART)) !=
            (ADC_VMSTATE_CR_ADEN | ADC_VMSTATE_CR_JADSTART)) {
        return false;
    }
    if (state->injected_discontinuous_paused &&
        (injected_active || !(cr & ADC_VMSTATE_CR_JADSTART) ||
         !state->injected_active_context_valid ||
         state->next_injected_sample_ns)) {
        return false;
    }
    if (state->calibration_active) {
        if (!(cr & ADC_VMSTATE_CR_ADCAL) ||
            (cr & (ADC_VMSTATE_CR_ADEN | ADC_VMSTATE_CR_ADSTART |
                   ADC_VMSTATE_CR_JADSTART)) ||
            regular_active || injected_active ||
            state->regular_waiting_for_data ||
            state->calibration_cycles == 0 ||
            state->calibration_remaining_cycles == 0 ||
            state->calibration_remaining_cycles > state->calibration_cycles) {
            return false;
        }
        if (state->next_sample_ns &&
            state->next_sample_ns < state->calibration_last_ns) {
            return false;
        }
    } else if (state->calibration_cycles ||
               state->calibration_remaining_cycles ||
               state->calibration_last_ns || state->calibration_clock_hz) {
        return false;
    }
    if (state->next_sample_ns) {
        if (state->next_sample_ns > INT64_MAX) {
            return false;
        }
        if (!state->calibration_active &&
            (!regular_active || state->regular_waiting_for_data ||
             state->regular_discontinuous_paused ||
             state->regular_waiting_for_auto_injected ||
             state->next_sample_ns < state->rank_last_ns)) {
            return false;
        }
    }
    if (state->next_injected_sample_ns) {
        if (state->next_injected_sample_ns > INT64_MAX ||
            !injected_active ||
            state->next_injected_sample_ns < state->injected_rank_last_ns) {
            return false;
        }
    }
    if (state->regulator_ready_ns > INT64_MAX ||
        state->rank_last_ns > INT64_MAX ||
        state->injected_rank_last_ns > INT64_MAX ||
        state->calibration_last_ns > INT64_MAX) {
        return false;
    }
    if (state->rank_remaining_half_cycles &&
        !state->rank_clock_hz && state->next_sample_ns) {
        return false;
    }
    if (state->injected_rank_remaining_half_cycles &&
        !state->injected_rank_clock_hz && state->next_injected_sample_ns) {
        return false;
    }
    if (injected_active && !state->injected_active_context_valid) {
        return false;
    }
    if ((cfgr & ADC_VMSTATE_CFGR_JAUTO) &&
        (cfgr & (ADC_VMSTATE_CFGR_DISCEN | ADC_VMSTATE_CFGR_JDISCEN)) &&
        state->regular_waiting_for_auto_injected) {
        return false;
    }
    return true;
}

bool dm_mc02_adc_state_valid(const DmMc02Adc *state)
{
    uint32_t cr;
    uint32_t cfgr;
    uint32_t jsqr;

    if (!state) {
        return false;
    }
    cr = adc_vmstate_load(state, ADC_VMSTATE_CR_OFFSET);
    cfgr = adc_vmstate_load(state, ADC_VMSTATE_CFGR_OFFSET);
    jsqr = adc_vmstate_load(state, ADC_VMSTATE_JSQR_OFFSET);

    return state->linear_calibration_window <
        DM_MC02_ADC_LINEAR_CALIBRATION_WORDS &&
        !(state->linear_calibration_words[5] & ~UINT32_C(0x3ff)) &&
        !(state->regular_shared_conversion &&
          !state->regular_active_sequence_id) &&
        adc_vmstate_input_state_valid(state) &&
        adc_vmstate_context_valid(state, jsqr) &&
        adc_vmstate_timing_valid(state, cr, cfgr,
                                 state->injected_active_context_valid ?
                                 state->injected_active_jsqr : jsqr);
}

static int dm_mc02_adc_validate_post_load(void *opaque, int version_id)
{
    if (version_id < 1 || version_id > 2 ||
        !dm_mc02_adc_state_valid(opaque)) {
        return -EINVAL;
    }
    return 0;
}

static int dm_mc02_adc_post_load(void *opaque, int version_id)
{
    DmMc02Adc *state = opaque;
    int ret = dm_mc02_adc_validate_post_load(opaque, version_id);

    if (ret) {
        return ret;
    }

    dm_mc02_adc_sync_runtime(state);
    return 0;
}

const VMStateDescription vmstate_dm_mc02_adc_raw = {
    .name = "dm-mc02-adc",
    .version_id = 2,
    .minimum_version_id = 1,
    .post_load = dm_mc02_adc_validate_post_load,
    .fields = (VMStateField[]) {
        VMSTATE_UINT8_ARRAY(regs, DmMc02Adc, DM_MC02_ADC_REGION_SIZE),
        VMSTATE_BOOL(conversion_active, DmMc02Adc),
        VMSTATE_BOOL(regular_waiting_for_data, DmMc02Adc),
        VMSTATE_BOOL(regular_sequence_complete, DmMc02Adc),
        VMSTATE_BOOL(regular_discontinuous_paused, DmMc02Adc),
        VMSTATE_BOOL(regular_discontinuous_pause_after_data, DmMc02Adc),
        VMSTATE_UINT32(regular_discontinuous_remaining, DmMc02Adc),
        VMSTATE_BOOL(injected_conversion_active, DmMc02Adc),
        VMSTATE_BOOL(calibration_active, DmMc02Adc),
        VMSTATE_BOOL(regulator_ready, DmMc02Adc),
        VMSTATE_UINT64(regulator_ready_ns, DmMc02Adc),
        VMSTATE_UINT64(calibration_cycles, DmMc02Adc),
        VMSTATE_UINT64(calibration_remaining_cycles, DmMc02Adc),
        VMSTATE_UINT64(calibration_last_ns, DmMc02Adc),
        VMSTATE_UINT64(calibration_clock_hz, DmMc02Adc),
        VMSTATE_UINT32_ARRAY(linear_calibration_words, DmMc02Adc,
                             DM_MC02_ADC_LINEAR_CALIBRATION_WORDS),
        VMSTATE_UINT32(linear_calibration_window, DmMc02Adc),
        VMSTATE_UINT64(next_sample_ns, DmMc02Adc),
        VMSTATE_UINT64(rank_remaining_half_cycles, DmMc02Adc),
        VMSTATE_UINT64(rank_last_ns, DmMc02Adc),
        VMSTATE_UINT64(rank_clock_hz, DmMc02Adc),
        VMSTATE_UINT64(next_injected_sample_ns, DmMc02Adc),
        VMSTATE_UINT64(injected_rank_remaining_half_cycles, DmMc02Adc),
        VMSTATE_UINT64(injected_rank_last_ns, DmMc02Adc),
        VMSTATE_UINT64(injected_rank_clock_hz, DmMc02Adc),
        VMSTATE_BOOL(regular_waiting_for_auto_injected, DmMc02Adc),
        VMSTATE_BOOL(injected_discontinuous_paused, DmMc02Adc),
        VMSTATE_BOOL(injected_active_context_valid, DmMc02Adc),
        VMSTATE_BOOL(injected_pending_context_valid, DmMc02Adc),
        VMSTATE_UINT32(injected_active_jsqr, DmMc02Adc),
        VMSTATE_UINT32(injected_pending_jsqr, DmMc02Adc),
        VMSTATE_UINT32(current_rank, DmMc02Adc),
        VMSTATE_UINT32(current_injected_rank, DmMc02Adc),
        VMSTATE_UINT64(regular_sequence_id, DmMc02Adc),
        VMSTATE_UINT64(regular_active_sequence_id, DmMc02Adc),
        VMSTATE_BOOL_V(regular_shared_conversion, DmMc02Adc, 2),
        VMSTATE_BOOL(legacy_samples, DmMc02Adc),
        VMSTATE_UINT16_ARRAY(sample_values, DmMc02Adc, 2),
        VMSTATE_UINT16_ARRAY(board_source_values, DmMc02Adc,
                             DM_MC02_ADC_CHANNEL_COUNT),
        VMSTATE_UINT16_ARRAY(external_values, DmMc02Adc,
                             DM_MC02_ADC_CHANNEL_COUNT),
        VMSTATE_UINT32(board_source_valid, DmMc02Adc),
        VMSTATE_UINT32(external_raw_valid, DmMc02Adc),
        VMSTATE_UINT32(external_pin_voltage_valid, DmMc02Adc),
        VMSTATE_UINT8_ARRAY(external_override_kind, DmMc02Adc,
                            DM_MC02_ADC_CHANNEL_COUNT),
        VMSTATE_END_OF_LIST()
    },
};

static const VMStateDescription vmstate_dm_mc02_adc = {
    .name = "dm-mc02-adc",
    .version_id = 2,
    .minimum_version_id = 1,
    .post_load = dm_mc02_adc_post_load,
    .fields = vmstate_dm_mc02_adc_raw.fields,
};

const VMStateDescription *dm_mc02_adc_vmstate(void)
{
    return &vmstate_dm_mc02_adc;
}
