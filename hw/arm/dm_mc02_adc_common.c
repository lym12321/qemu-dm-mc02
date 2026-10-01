/* Reusable STM32H723 ADC1/ADC2 common-register block. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_adc_common.h"

static uint32_t dm_mc02_adc_common_csr(const DmMc02AdcCommon *s)
{
    uint32_t master = s->master_status_read ?
        s->master_status_read(s->master_status_opaque) : 0;
    uint32_t slave = s->slave_status_read ?
        s->slave_status_read(s->slave_status_opaque) : 0;

    return (master & DM_MC02_ADC_COMMON_STATUS_MASK) |
           ((slave & DM_MC02_ADC_COMMON_STATUS_MASK) << 16);
}

static uint32_t dm_mc02_adc_common_reg(const DmMc02AdcCommon *s,
                                       hwaddr aligned)
{
    switch (aligned) {
    case DM_MC02_ADC_COMMON_CSR_OFFSET:
        return dm_mc02_adc_common_csr(s);
    case DM_MC02_ADC_COMMON_CCR_OFFSET:
        return s->ccr;
    case DM_MC02_ADC_COMMON_CDR_OFFSET:
        return s->cdr;
    case DM_MC02_ADC_COMMON_CDR2_OFFSET:
        return s->cdr2;
    default:
        return 0;
    }
}

static void dm_mc02_adc_common_clear_pending(DmMc02AdcCommon *s)
{
    s->master_sample_valid = false;
    s->master_sample_conversion_id = 0;
    s->master_sample_rank = 0;
    s->master_sample_value = 0;
    s->master_sample_timestamp_ns = 0;
    s->slave_sample_valid = false;
    s->slave_sample_conversion_id = 0;
    s->slave_sample_rank = 0;
    s->slave_sample_value = 0;
    s->slave_sample_timestamp_ns = 0;
}

static void dm_mc02_adc_common_clear_damdf8(DmMc02AdcCommon *s)
{
    s->damdf8_data = 0;
    s->damdf8_count = 0;
    s->damdf8_next_source = 0;
    s->damdf8_timestamp_ns = 0;
}

static bool dm_mc02_adc_common_format_supported(const DmMc02AdcCommon *s,
                                                unsigned *format)
{
    uint32_t ccr = s->ccr;
    unsigned dual = ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK;
    unsigned damdf = (ccr & DM_MC02_ADC_COMMON_CCR_DAMDF_MASK) >> 14;

    if (dual != DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS ||
        (damdf != DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 &&
         damdf != DM_MC02_ADC_COMMON_CCR_DAMDF_8)) {
        return false;
    }
    if (format) {
        *format = damdf;
    }
    return true;
}

static bool dm_mc02_adc_common_cdr8_supported(
    const DmMc02AdcCommon *s)
{
    unsigned dual = s->ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK;
    unsigned damdf = (s->ccr & DM_MC02_ADC_COMMON_CCR_DAMDF_MASK) >> 14;

    return damdf == DM_MC02_ADC_COMMON_CCR_DAMDF_8 &&
           (dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS ||
            dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED ||
            dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT);
}

static bool dm_mc02_adc_common_alternated_data_supported(
    const DmMc02AdcCommon *s)
{
    unsigned dual = s->ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK;
    unsigned damdf = (s->ccr & DM_MC02_ADC_COMMON_CCR_DAMDF_MASK) >> 14;

    /* RM0468 exposes CDR2.RDATA_ALT for regular interleaved operation.  Keep
     * the existing polling model for DAMDF=0 and admit DAMDF=2 for the
     * dedicated 32-bit CDR2 DMA path. */
    return (dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED ||
            dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT) &&
           (damdf == 0 || damdf == DM_MC02_ADC_COMMON_CCR_DAMDF_32_10);
}

bool dm_mc02_adc_common_cdr2_dma_supported(
    const DmMc02AdcCommon *s)
{
    unsigned dual;
    unsigned damdf;

    if (!s) {
        return false;
    }
    dual = s->ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK;
    damdf = (s->ccr & DM_MC02_ADC_COMMON_CCR_DAMDF_MASK) >> 14;
    return (dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED ||
            dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT) &&
           damdf == DM_MC02_ADC_COMMON_CCR_DAMDF_32_10;
}

static bool dm_mc02_adc_common_cdr_read_supported(
    const DmMc02AdcCommon *s)
{
    unsigned dual;
    unsigned damdf;

    dual = s->ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK;
    damdf = (s->ccr & DM_MC02_ADC_COMMON_CCR_DAMDF_MASK) >> 14;
    /* DAMDF=2/3 uses CDR for regular-simultaneous data.  DAMDF=3 also uses
     * CDR for the four-value interleaved 8-bit format; DAMDF=2 interleaved
     * data lives in CDR2 and is handled by its separate read hook. */
    return (dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS &&
            (damdf == DM_MC02_ADC_COMMON_CCR_DAMDF_32_10 ||
             damdf == DM_MC02_ADC_COMMON_CCR_DAMDF_8)) ||
           ((dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED ||
             dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT) &&
           damdf == DM_MC02_ADC_COMMON_CCR_DAMDF_8);
}

static bool dm_mc02_adc_common_cdr_dma_blocked(
    const DmMc02AdcCommon *s)
{
    unsigned dual;
    unsigned damdf;
    uint32_t master;
    uint32_t slave;

    if (!s) {
        return false;
    }
    dual = s->ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK;
    damdf = (s->ccr & DM_MC02_ADC_COMMON_CCR_DAMDF_MASK) >> 14;
    /* The common overrun/request coupling is deliberately scoped to the
     * regular-simultaneous 32-bit CDR format.  DAMDF=3 has a separate
     * multi-pair accumulator, and an ADC EOC may remain asserted while that
     * valid partial word is being formed. */
    if (dual != DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS ||
        damdf != DM_MC02_ADC_COMMON_CCR_DAMDF_32_10) {
        return false;
    }
    master = s->master_status_read ?
        s->master_status_read(s->master_status_opaque) : 0;
    slave = s->slave_status_read ?
        s->slave_status_read(s->slave_status_opaque) : 0;
    return (master | slave) & DM_MC02_ADC_COMMON_STATUS_OVR;
}

bool dm_mc02_adc_common_cdr_data_pending(const DmMc02AdcCommon *state)
{
    return state && state->cdr_valid && !state->cdr_read_reserved &&
           dm_mc02_adc_common_cdr_read_supported(state);
}

bool dm_mc02_adc_common_cdr_dma_request_pending(
    const DmMc02AdcCommon *state)
{
    return dm_mc02_adc_common_cdr_data_pending(state) &&
           !dm_mc02_adc_common_cdr_dma_blocked(state);
}

void dm_mc02_adc_common_notify_cdr_read(DmMc02AdcCommon *state)
{
    if (dm_mc02_adc_common_cdr_data_pending(state)) {
        state->cdr_valid = false;
        if (state->cdr_read) {
            state->cdr_read(state->cdr_read_opaque);
        }
    }
}

bool dm_mc02_adc_common_cdr_read_prepare(DmMc02AdcCommon *state,
                                         uint8_t *data, unsigned size)
{
    if (!data || (size != 1 && size != 2 && size != 4) ||
        !dm_mc02_adc_common_cdr_data_pending(state)) {
        return false;
    }
    state->cdr_read_reserved = true;
    for (unsigned byte = 0; byte < size; ++byte) {
        data[byte] = state->cdr >> (byte * 8);
    }
    return true;
}

void dm_mc02_adc_common_cdr_read_commit(DmMc02AdcCommon *state)
{
    if (!state || !state->cdr_read_reserved) {
        return;
    }
    state->cdr_read_reserved = false;
    dm_mc02_adc_common_notify_cdr_read(state);
}

void dm_mc02_adc_common_cdr_read_abort(DmMc02AdcCommon *state)
{
    if (state) {
        state->cdr_read_reserved = false;
    }
}

bool dm_mc02_adc_common_cdr2_data_pending(
    const DmMc02AdcCommon *state)
{
    return state && state->cdr2_valid && !state->cdr2_read_reserved &&
           dm_mc02_adc_common_cdr2_dma_supported(state);
}

bool dm_mc02_adc_common_cdr2_read_prepare(DmMc02AdcCommon *state,
                                          uint8_t *data, unsigned size)
{
    if (!data || (size != 1 && size != 2 && size != 4) ||
        !dm_mc02_adc_common_cdr2_data_pending(state)) {
        return false;
    }
    state->cdr2_read_reserved = true;
    state->cdr2_read_reserved_source = state->cdr2_source;
    for (unsigned byte = 0; byte < size; ++byte) {
        data[byte] = state->cdr2 >> (byte * 8);
    }
    return true;
}

void dm_mc02_adc_common_cdr2_read_commit(DmMc02AdcCommon *state)
{
    unsigned source;

    if (!state || !state->cdr2_read_reserved) {
        return;
    }
    source = state->cdr2_read_reserved_source;
    state->cdr2_read_reserved = false;
    state->cdr2_read_reserved_source = 0;
    /* The reservation is synchronous and the producer cannot interleave a
     * new CDR2 publication before this commit.  Keep the normal read hook as
     * the sole source-specific acknowledgement path. */
    if (state->cdr2_valid && state->cdr2_source == source) {
        dm_mc02_adc_common_notify_cdr2_read(state);
    }
}

void dm_mc02_adc_common_cdr2_read_abort(DmMc02AdcCommon *state)
{
    if (state) {
        state->cdr2_read_reserved = false;
        state->cdr2_read_reserved_source = 0;
    }
}

void dm_mc02_adc_common_notify_cdr2_read(DmMc02AdcCommon *state)
{
    unsigned source;

    if (!dm_mc02_adc_common_cdr2_data_pending(state)) {
        return;
    }
    source = state->cdr2_source;
    state->cdr2_valid = false;
    state->cdr2_source = 0;
    if (state->cdr2_read) {
        state->cdr2_read(state->cdr2_read_opaque, source);
    }
}

static uint64_t dm_mc02_adc_common_read(void *opaque, hwaddr offset,
                                        unsigned size)
{
    DmMc02AdcCommon *s = opaque;
    uint64_t value = 0;

    if (size > 4 || offset >= DM_MC02_ADC_COMMON_REGION_SIZE ||
        size > DM_MC02_ADC_COMMON_REGION_SIZE - offset) {
        return 0;
    }

    for (unsigned byte = 0; byte < size; ++byte) {
        hwaddr address = offset + byte;
        uint32_t reg = dm_mc02_adc_common_reg(s, address & ~3u);

        value |= (uint64_t)((reg >> ((address & 3u) * 8)) & 0xffu)
                 << (byte * 8);
    }
    if (offset < DM_MC02_ADC_COMMON_CDR_OFFSET + sizeof(uint32_t) &&
        offset + size > DM_MC02_ADC_COMMON_CDR_OFFSET) {
        dm_mc02_adc_common_notify_cdr_read(s);
    }
    if (offset < DM_MC02_ADC_COMMON_CDR2_OFFSET + sizeof(uint32_t) &&
        offset + size > DM_MC02_ADC_COMMON_CDR2_OFFSET) {
        dm_mc02_adc_common_notify_cdr2_read(s);
    }
    return value;
}

static bool dm_mc02_adc_common_read_consuming(DmMc02AdcCommon *state,
                                              hwaddr offset, uint8_t *data,
                                              unsigned size, bool pending)
{
    uint32_t value;

    if (!pending || !data || (size != 1 && size != 2 && size != 4)) {
        return false;
    }
    /* Reuse the register consumer's acknowledgement, including CDR2's
     * source-specific EOC.  No reservation survives this consuming read. */
    value = dm_mc02_adc_common_read(state, offset, size);
    for (unsigned byte = 0; byte < size; ++byte) {
        data[byte] = value >> (byte * 8);
    }
    return true;
}

bool dm_mc02_adc_common_cdr_read_consuming(DmMc02AdcCommon *state,
                                          uint8_t *data, unsigned size)
{
    return dm_mc02_adc_common_read_consuming(
        state, DM_MC02_ADC_COMMON_CDR_OFFSET, data, size,
        dm_mc02_adc_common_cdr_data_pending(state));
}

bool dm_mc02_adc_common_cdr2_read_consuming(DmMc02AdcCommon *state,
                                           uint8_t *data, unsigned size)
{
    return dm_mc02_adc_common_read_consuming(
        state, DM_MC02_ADC_COMMON_CDR2_OFFSET, data, size,
        dm_mc02_adc_common_cdr2_data_pending(state));
}

static void dm_mc02_adc_common_write(void *opaque, hwaddr offset,
                                      uint64_t value, unsigned size)
{
    DmMc02AdcCommon *s = opaque;
    uint32_t ccr;
    bool touched = false;

    if (size > 4 || offset >= DM_MC02_ADC_COMMON_REGION_SIZE ||
        size > DM_MC02_ADC_COMMON_REGION_SIZE - offset) {
        return;
    }

    ccr = s->ccr;
    for (unsigned byte = 0; byte < size; ++byte) {
        hwaddr address = offset + byte;

        if (address >= DM_MC02_ADC_COMMON_CCR_OFFSET &&
            address < DM_MC02_ADC_COMMON_CCR_OFFSET + sizeof(uint32_t)) {
            unsigned shift = (address - DM_MC02_ADC_COMMON_CCR_OFFSET) * 8;

            ccr = (ccr & ~(UINT32_C(0xff) << shift)) |
                  (((uint32_t)(value >> (byte * 8)) & 0xffu) << shift);
            touched = true;
        }
    }
    if (!touched) {
        /* CSR/CDR/CDR2 are read-only and reserved locations have no effect. */
        return;
    }

    if ((s->ccr ^ ccr) & (DM_MC02_ADC_COMMON_CCR_DUAL_MASK |
                           DM_MC02_ADC_COMMON_CCR_DAMDF_MASK)) {
        /* A pair admitted under the previous mode must never be combined
         * with a sample produced after a mode change. */
        dm_mc02_adc_common_clear_pending(s);
        dm_mc02_adc_common_clear_damdf8(s);
        s->cdr_valid = false;
        s->cdr_read_reserved = false;
        s->cdr2_valid = false;
        s->cdr2_source = 0;
        s->cdr2_read_reserved = false;
        s->cdr2_read_reserved_source = 0;
    }
    s->ccr = ccr & DM_MC02_ADC_COMMON_CCR_WRITABLE_MASK;
    s->ccr_configured = true;
    if (s->clock_changed) {
        s->clock_changed(s->clock_changed_opaque, s->ccr);
    }
}

static const MemoryRegionOps dm_mc02_adc_common_ops = {
    .read = dm_mc02_adc_common_read,
    .write = dm_mc02_adc_common_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_adc_common_init(DmMc02AdcCommon *state, Object *owner,
                             const char *name)
{
    memset(state, 0, sizeof(*state));
    memory_region_init_io(&state->iomem, owner, &dm_mc02_adc_common_ops,
                          state, name, DM_MC02_ADC_COMMON_REGION_SIZE);
}

void dm_mc02_adc_common_reset(DmMc02AdcCommon *state)
{
    DmMc02AdcCommonStatusRead master;
    DmMc02AdcCommonStatusRead slave;
    void *master_opaque;
    void *slave_opaque;
    DmMc02AdcCommonClockChanged clock_changed;
    void *clock_opaque;
    DmMc02AdcCommonDataReady data_ready;
    void *data_ready_opaque;
    DmMc02AdcCommonCdr2DataReady cdr2_data_ready;
    void *cdr2_data_ready_opaque;
    DmMc02AdcCommonCdrRead cdr_read;
    void *cdr_read_opaque;
    DmMc02AdcCommonCdr2Read cdr2_read;
    void *cdr2_read_opaque;
    DmMc02AdcCommonRegularStartPeer regular_start_peer;
    void *regular_start_peer_opaque;
    DmMc02AdcCommonExternalTriggerPeer external_trigger_peer;
    void *external_trigger_peer_opaque;

    if (!state) {
        return;
    }

    master = state->master_status_read;
    master_opaque = state->master_status_opaque;
    slave = state->slave_status_read;
    slave_opaque = state->slave_status_opaque;
    clock_changed = state->clock_changed;
    clock_opaque = state->clock_changed_opaque;
    data_ready = state->data_ready;
    data_ready_opaque = state->data_ready_opaque;
    cdr2_data_ready = state->cdr2_data_ready;
    cdr2_data_ready_opaque = state->cdr2_data_ready_opaque;
    cdr_read = state->cdr_read;
    cdr_read_opaque = state->cdr_read_opaque;
    cdr2_read = state->cdr2_read;
    cdr2_read_opaque = state->cdr2_read_opaque;
    regular_start_peer = state->regular_start_peer;
    regular_start_peer_opaque = state->regular_start_peer_opaque;
    external_trigger_peer = state->external_trigger_peer;
    external_trigger_peer_opaque = state->external_trigger_peer_opaque;

    state->ccr = 0;
    state->cdr = 0;
    state->cdr2 = 0;
    state->cdr_valid = false;
    state->cdr_read_reserved = false;
    dm_mc02_adc_common_clear_damdf8(state);
    state->cdr2_valid = false;
    state->cdr2_source = 0;
    state->cdr2_read_reserved = false;
    state->cdr2_read_reserved_source = 0;
    state->ccr_configured = false;
    state->next_conversion_id = 0;
    dm_mc02_adc_common_clear_pending(state);
    state->master_status_read = master;
    state->master_status_opaque = master_opaque;
    state->slave_status_read = slave;
    state->slave_status_opaque = slave_opaque;
    state->clock_changed = clock_changed;
    state->clock_changed_opaque = clock_opaque;
    state->data_ready = data_ready;
    state->data_ready_opaque = data_ready_opaque;
    state->cdr2_data_ready = cdr2_data_ready;
    state->cdr2_data_ready_opaque = cdr2_data_ready_opaque;
    state->cdr_read = cdr_read;
    state->cdr_read_opaque = cdr_read_opaque;
    state->cdr2_read = cdr2_read;
    state->cdr2_read_opaque = cdr2_read_opaque;
    state->regular_start_peer = regular_start_peer;
    state->regular_start_peer_opaque = regular_start_peer_opaque;
    state->external_trigger_peer = external_trigger_peer;
    state->external_trigger_peer_opaque = external_trigger_peer_opaque;
}

void dm_mc02_adc_common_set_status_sources(
    DmMc02AdcCommon *state, DmMc02AdcCommonStatusRead master,
    void *master_opaque, DmMc02AdcCommonStatusRead slave,
    void *slave_opaque)
{
    if (!state) {
        return;
    }
    state->master_status_read = master;
    state->master_status_opaque = master_opaque;
    state->slave_status_read = slave;
    state->slave_status_opaque = slave_opaque;
}

void dm_mc02_adc_common_set_clock_callback(
    DmMc02AdcCommon *state, DmMc02AdcCommonClockChanged callback,
    void *opaque)
{
    if (!state) {
        return;
    }
    state->clock_changed = callback;
    state->clock_changed_opaque = opaque;
}

void dm_mc02_adc_common_set_data_ready_callback(
    DmMc02AdcCommon *state, DmMc02AdcCommonDataReady callback,
    void *opaque)
{
    if (!state) {
        return;
    }
    state->data_ready = callback;
    state->data_ready_opaque = opaque;
}

void dm_mc02_adc_common_set_cdr2_data_ready_callback(
    DmMc02AdcCommon *state, DmMc02AdcCommonCdr2DataReady callback,
    void *opaque)
{
    if (!state) {
        return;
    }
    state->cdr2_data_ready = callback;
    state->cdr2_data_ready_opaque = opaque;
}

void dm_mc02_adc_common_set_cdr_read_callback(
    DmMc02AdcCommon *state, DmMc02AdcCommonCdrRead callback, void *opaque)
{
    if (!state) {
        return;
    }
    state->cdr_read = callback;
    state->cdr_read_opaque = opaque;
}

void dm_mc02_adc_common_set_cdr2_read_callback(
    DmMc02AdcCommon *state, DmMc02AdcCommonCdr2Read callback, void *opaque)
{
    if (!state) {
        return;
    }
    state->cdr2_read = callback;
    state->cdr2_read_opaque = opaque;
}

void dm_mc02_adc_common_set_regular_start_peer(
    DmMc02AdcCommon *state, DmMc02AdcCommonRegularStartPeer callback,
    void *opaque)
{
    if (!state) {
        return;
    }
    state->regular_start_peer = callback;
    state->regular_start_peer_opaque = opaque;
}

void dm_mc02_adc_common_set_external_trigger_peer(
    DmMc02AdcCommon *state, DmMc02AdcCommonExternalTriggerPeer callback,
    void *opaque)
{
    if (!state) {
        return;
    }
    state->external_trigger_peer = callback;
    state->external_trigger_peer_opaque = opaque;
}

bool dm_mc02_adc_common_admit_regular_start(DmMc02AdcCommon *state,
                                            unsigned source)
{
    unsigned dual;
    uint64_t conversion_id;

    if (!state || source > 1) {
        return false;
    }
    dual = state->ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK;
    if (dual != DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS &&
        dual != DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED &&
        dual != DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT) {
        return true;
    }
    if (source == 1) {
        return false;
    }
    conversion_id = ++state->next_conversion_id;
    if (!conversion_id) {
        /* Zero is reserved for ADC regular sequences not owned by common. */
        conversion_id = ++state->next_conversion_id;
    }
    return state->regular_start_peer &&
           state->regular_start_peer(state->regular_start_peer_opaque, 1,
                                     conversion_id);
}

bool dm_mc02_adc_common_admit_external_trigger(
    DmMc02AdcCommon *state, unsigned source, uint32_t trigger_source,
    bool rising, unsigned event_count, uint64_t timestamp_ns,
    uint64_t *conversion_id)
{
    unsigned dual;
    uint64_t id;

    if (conversion_id) {
        *conversion_id = 0;
    }
    if (!state || source > 1) {
        return false;
    }
    dual = state->ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK;
    if (dual != DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS &&
        dual != DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED &&
        dual != DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT) {
        return true;
    }
    if (source != 0 || !state->external_trigger_peer) {
        return false;
    }
    id = ++state->next_conversion_id;
    if (!id) {
        /* Zero is reserved for ADC-local/external paths. */
        id = ++state->next_conversion_id;
    }
    if (!state->external_trigger_peer(
            state->external_trigger_peer_opaque, 1, trigger_source, rising,
            event_count ? event_count : 1, timestamp_ns, id)) {
        return false;
    }
    if (conversion_id) {
        *conversion_id = id;
    }
    return true;
}

unsigned dm_mc02_adc_common_get_dual(const DmMc02AdcCommon *state)
{
    return state ? state->ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK : 0;
}

unsigned dm_mc02_adc_common_get_delay_code(const DmMc02AdcCommon *state)
{
    return state ? (state->ccr & DM_MC02_ADC_COMMON_CCR_DELAY_MASK) >> 8 : 0;
}

bool dm_mc02_adc_common_regular_interleaved(
    const DmMc02AdcCommon *state)
{
    unsigned dual = dm_mc02_adc_common_get_dual(state);

    return dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED ||
           dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT;
}

void dm_mc02_adc_common_set_data(DmMc02AdcCommon *state, uint32_t cdr,
                                 uint32_t cdr2)
{
    if (!state) {
        return;
    }
    state->cdr = cdr;
    state->cdr_valid = true;
    state->cdr_read_reserved = false;
    state->cdr2 = cdr2;
}

static void dm_mc02_adc_common_publish_cdr(DmMc02AdcCommon *state,
                                           uint32_t data,
                                           uint64_t timestamp_ns,
                                           bool apply_overrun_gate)
{
    state->cdr = data;
    state->cdr_valid = true;
    if (state->data_ready &&
        (!apply_overrun_gate ||
         dm_mc02_adc_common_cdr_dma_request_pending(state))) {
        state->data_ready(state->data_ready_opaque, data, timestamp_ns);
    }
}

DmMc02AdcCommonRegularSampleResult dm_mc02_adc_common_submit_regular_sample(
    DmMc02AdcCommon *state, unsigned source, uint64_t conversion_id,
    uint32_t rank, uint16_t value, uint64_t timestamp_ns)
{
    bool *valid;
    uint64_t *pending_conversion_id;
    uint32_t *pending_rank;
    uint16_t *pending_value;
    uint64_t *pending_timestamp_ns;
    bool *other_valid;
    uint64_t *other_conversion_id;
    uint32_t *other_rank;
    uint16_t *other_value;
    uint64_t *other_timestamp_ns;
    unsigned format;
    unsigned dual;
    uint64_t ready_timestamp_ns;
    uint32_t packed_data;
    uint32_t pair_data;

    if (!state || source > 1 || rank > 15) {
        return DM_MC02_ADC_COMMON_SAMPLE_UNSUPPORTED;
    }

    dual = state->ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK;
    if (dm_mc02_adc_common_alternated_data_supported(state)) {
        /* CDR2 is a single-result register: the most recently completed
         * rank wins.  DAMDF=2 additionally makes the result a DMA producer;
         * retain the source so a read can acknowledge only its EOC. */
        state->cdr2 = value;
        if (dm_mc02_adc_common_cdr2_dma_supported(state)) {
            state->cdr2_valid = true;
            state->cdr2_source = source;
            if (state->cdr2_data_ready) {
                state->cdr2_data_ready(state->cdr2_data_ready_opaque,
                                       value, source, timestamp_ns);
            }
        }
        return DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED;
    }

    if (dm_mc02_adc_common_cdr8_supported(state) &&
        (dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED ||
         dual == DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT)) {
        /* RM0468: DAMDF=3 emits one CDR DMA request after four consecutive
         * interleaved 8-bit values.  The source order is master, slave,
         * master, slave; reject an out-of-order producer rather than
         * synthesizing a word from a different cadence. */
        if (source != state->damdf8_next_source) {
            dm_mc02_adc_common_clear_damdf8(state);
            return DM_MC02_ADC_COMMON_SAMPLE_MISMATCH;
        }
        state->damdf8_timestamp_ns =
            MAX(state->damdf8_timestamp_ns, timestamp_ns);
        state->damdf8_data |= (uint32_t)(value & 0xffu)
                              << (state->damdf8_count * 8u);
        ++state->damdf8_count;
        state->damdf8_next_source ^= 1u;
        if (state->damdf8_count < 4) {
            return DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED;
        }
        packed_data = state->damdf8_data;
        ready_timestamp_ns = state->damdf8_timestamp_ns;
        dm_mc02_adc_common_clear_damdf8(state);
        dm_mc02_adc_common_publish_cdr(state, packed_data,
                                       ready_timestamp_ns, false);
        return DM_MC02_ADC_COMMON_SAMPLE_PACKED;
    }

    if (conversion_id == 0 ||
        !dm_mc02_adc_common_format_supported(state, &format)) {
        return DM_MC02_ADC_COMMON_SAMPLE_UNSUPPORTED;
    }

    if (source == 0) {
        valid = &state->master_sample_valid;
        pending_conversion_id = &state->master_sample_conversion_id;
        pending_rank = &state->master_sample_rank;
        pending_value = &state->master_sample_value;
        pending_timestamp_ns = &state->master_sample_timestamp_ns;
        other_valid = &state->slave_sample_valid;
        other_conversion_id = &state->slave_sample_conversion_id;
        other_rank = &state->slave_sample_rank;
        other_value = &state->slave_sample_value;
        other_timestamp_ns = &state->slave_sample_timestamp_ns;
    } else {
        valid = &state->slave_sample_valid;
        pending_conversion_id = &state->slave_sample_conversion_id;
        pending_rank = &state->slave_sample_rank;
        pending_value = &state->slave_sample_value;
        pending_timestamp_ns = &state->slave_sample_timestamp_ns;
        other_valid = &state->master_sample_valid;
        other_conversion_id = &state->master_sample_conversion_id;
        other_rank = &state->master_sample_rank;
        other_value = &state->master_sample_value;
        other_timestamp_ns = &state->master_sample_timestamp_ns;
    }

    if (*valid) {
        /* There is intentionally no unbounded queue at this boundary.  Keep
         * the newest event so a delayed peer can still form the next pair. */
        *pending_conversion_id = conversion_id;
        *pending_rank = rank;
        *pending_value = value;
        *pending_timestamp_ns = timestamp_ns;
        return DM_MC02_ADC_COMMON_SAMPLE_DUPLICATE;
    }

    if (!*other_valid) {
        *valid = true;
        *pending_conversion_id = conversion_id;
        *pending_rank = rank;
        *pending_value = value;
        *pending_timestamp_ns = timestamp_ns;
        return DM_MC02_ADC_COMMON_SAMPLE_PENDING;
    }

    if (*other_conversion_id != conversion_id || *other_rank != rank) {
        /* Retain the new event as the source's pending half.  The old peer
         * event is discarded at the explicit mismatch boundary. */
        *other_valid = false;
        *valid = true;
        *pending_conversion_id = conversion_id;
        *pending_rank = rank;
        *pending_value = value;
        *pending_timestamp_ns = timestamp_ns;
        return DM_MC02_ADC_COMMON_SAMPLE_MISMATCH;
    }

    if (format == DM_MC02_ADC_COMMON_CCR_DAMDF_32_10) {
        packed_data = source == 0 ?
            ((uint32_t)*other_value << 16 | value) :
            ((uint32_t)value << 16 | *other_value);
        ready_timestamp_ns = MAX(*other_timestamp_ns, timestamp_ns);
        dm_mc02_adc_common_clear_pending(state);
        dm_mc02_adc_common_publish_cdr(state, packed_data,
                                       ready_timestamp_ns, true);
        return DM_MC02_ADC_COMMON_SAMPLE_PACKED;
    } else {
        pair_data = source == 0 ?
            ((((uint32_t)*other_value & 0xffu) << 8) |
             (uint32_t)(value & 0xffu)) :
            ((((uint32_t)value & 0xffu) << 8) |
             (uint32_t)(*other_value & 0xffu));
        ready_timestamp_ns = MAX(*other_timestamp_ns, timestamp_ns);
        state->damdf8_timestamp_ns =
            MAX(state->damdf8_timestamp_ns, ready_timestamp_ns);
        state->damdf8_data |= pair_data << (state->damdf8_count * 16u);
        ++state->damdf8_count;
        dm_mc02_adc_common_clear_pending(state);
        if (state->damdf8_count < 2) {
            return DM_MC02_ADC_COMMON_SAMPLE_PACKING;
        }
        packed_data = state->damdf8_data;
        ready_timestamp_ns = state->damdf8_timestamp_ns;
        dm_mc02_adc_common_clear_damdf8(state);
    }
    dm_mc02_adc_common_publish_cdr(state, packed_data, ready_timestamp_ns,
                                   false);
    return DM_MC02_ADC_COMMON_SAMPLE_PACKED;
}

uint32_t dm_mc02_adc_common_get_ccr(const DmMc02AdcCommon *state)
{
    return state ? state->ccr : 0;
}

bool dm_mc02_adc_common_ccr_configured(const DmMc02AdcCommon *state)
{
    return state && state->ccr_configured;
}

void dm_mc02_adc_common_sync_runtime(DmMc02AdcCommon *state)
{
    if (state && state->ccr_configured && state->clock_changed) {
        state->clock_changed(state->clock_changed_opaque, state->ccr);
    }
}
