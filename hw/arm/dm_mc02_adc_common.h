/* STM32H723 ADC1/ADC2 common-register block. */
#ifndef HW_ARM_DM_MC02_ADC_COMMON_H
#define HW_ARM_DM_MC02_ADC_COMMON_H

#include "exec/memory.h"
#include "migration/vmstate.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_ADC_COMMON_REGION_SIZE 0x100

#define DM_MC02_ADC_COMMON_CSR_OFFSET  0x00
#define DM_MC02_ADC_COMMON_CCR_OFFSET  0x08
#define DM_MC02_ADC_COMMON_CDR_OFFSET  0x0c
#define DM_MC02_ADC_COMMON_CDR2_OFFSET 0x10

/* STM32H723 ADC12_COMMON.CCR writable fields. */
#define DM_MC02_ADC_COMMON_CCR_DUAL_MASK   UINT32_C(0x0000001f)
#define DM_MC02_ADC_COMMON_CCR_DELAY_MASK  UINT32_C(0x00000f00)
#define DM_MC02_ADC_COMMON_CCR_DAMDF_MASK  UINT32_C(0x0000c000)
#define DM_MC02_ADC_COMMON_CCR_CKMODE_MASK UINT32_C(0x00030000)
#define DM_MC02_ADC_COMMON_CCR_PRESC_MASK  UINT32_C(0x003c0000)
#define DM_MC02_ADC_COMMON_CCR_VREFEN      UINT32_C(0x00400000)
#define DM_MC02_ADC_COMMON_CCR_TSEN        UINT32_C(0x00800000)
#define DM_MC02_ADC_COMMON_CCR_VBATEN      UINT32_C(0x01000000)
#define DM_MC02_ADC_COMMON_CCR_CKMODE_SHIFT 16
#define DM_MC02_ADC_COMMON_CCR_PRESC_SHIFT  18
#define DM_MC02_ADC_COMMON_CCR_DUAL_REG_SIMULTANEOUS UINT32_C(0x06)
#define DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED UINT32_C(0x07)
#define DM_MC02_ADC_COMMON_CCR_DUAL_REG_INTERLEAVED_INJ_SIMULT UINT32_C(0x03)
#define DM_MC02_ADC_COMMON_CCR_DAMDF_32_10           UINT32_C(0x02)
#define DM_MC02_ADC_COMMON_CCR_DAMDF_8              UINT32_C(0x03)
#define DM_MC02_ADC_COMMON_CCR_WRITABLE_MASK \
    (DM_MC02_ADC_COMMON_CCR_DUAL_MASK | \
     DM_MC02_ADC_COMMON_CCR_DELAY_MASK | \
     DM_MC02_ADC_COMMON_CCR_DAMDF_MASK | \
     DM_MC02_ADC_COMMON_CCR_CKMODE_MASK | \
     DM_MC02_ADC_COMMON_CCR_PRESC_MASK | \
     DM_MC02_ADC_COMMON_CCR_VREFEN | \
     DM_MC02_ADC_COMMON_CCR_TSEN | \
     DM_MC02_ADC_COMMON_CCR_VBATEN)

/* CSR uses the same bit positions as an ADCx.ISR for the modeled flags. */
#define DM_MC02_ADC_COMMON_STATUS_MASK UINT32_C(0x0000047f)
#define DM_MC02_ADC_COMMON_STATUS_OVR  UINT32_C(0x00000010)

typedef uint32_t (*DmMc02AdcCommonStatusRead)(void *opaque);
typedef void (*DmMc02AdcCommonClockChanged)(void *opaque, uint32_t ccr);
/* A packed regular result became available in CDR.  This is runtime wiring;
 * the common block does not depend on DMA or any particular consumer. */
typedef void (*DmMc02AdcCommonDataReady)(void *opaque, uint32_t data,
                                         uint64_t timestamp_ns);
/* A regular result became available in CDR2.RDATA_ALT.  The source is part
 * of the boundary because a CDR2 read acknowledges only that ADC's EOC. */
typedef void (*DmMc02AdcCommonCdr2DataReady)(void *opaque, uint32_t data,
                                             unsigned source,
                                             uint64_t timestamp_ns);
/* A supported CDR read acknowledged the regular result in its producer ADCs.
 * This is runtime wiring; the common block does not own ADC status. */
typedef void (*DmMc02AdcCommonCdrRead)(void *opaque);
/* A supported CDR2 read acknowledged the most recently published regular
 * result.  The common block supplies the producer source from its state. */
typedef void (*DmMc02AdcCommonCdr2Read)(void *opaque, unsigned source);
typedef bool (*DmMc02AdcCommonRegularStartPeer)(void *opaque,
                                                unsigned source,
                                                uint64_t conversion_id);
typedef bool (*DmMc02AdcCommonExternalTriggerPeer)(
    void *opaque, unsigned source, uint32_t trigger_source, bool rising,
    unsigned event_count, uint64_t timestamp_ns, uint64_t conversion_id);

/* Result of submitting one regular sample to the bounded pair matcher. */
typedef enum DmMc02AdcCommonRegularSampleResult {
    DM_MC02_ADC_COMMON_SAMPLE_UNSUPPORTED = 0,
    DM_MC02_ADC_COMMON_SAMPLE_PENDING,
    DM_MC02_ADC_COMMON_SAMPLE_PACKED,
    /* The producer completed one item of a DAMDF=3 four-byte word. */
    DM_MC02_ADC_COMMON_SAMPLE_PACKING,
    DM_MC02_ADC_COMMON_SAMPLE_MISMATCH,
    DM_MC02_ADC_COMMON_SAMPLE_DUPLICATE,
    /* One regular rank from either ADC was published through CDR2. */
    DM_MC02_ADC_COMMON_SAMPLE_ALTERNATED,
} DmMc02AdcCommonRegularSampleResult;

typedef struct DmMc02AdcCommon {
    MemoryRegion iomem;
    uint32_t ccr;
    uint32_t cdr;
    uint32_t cdr2;
    /* CDR is a one-word producer slot.  Validity is component state because
     * a failed direct DMA destination must leave the exact word available
     * for a later stream-enable retry.  The reservation itself is only
     * synchronous runtime state and is deliberately not migrated. */
    bool cdr_valid;
    bool cdr_read_reserved;
    /* DAMDF=3 packs four 8-bit regular values into one CDR word.  For
     * regular-simultaneous mode count is the number of completed pairs; for
     * regular-interleaved mode it is the number of completed values. */
    uint32_t damdf8_data;
    uint8_t damdf8_count;
    uint8_t damdf8_next_source;
    /* Maximum producer timestamp represented by damdf8_data.  Keeping this
     * across the four-value accumulation prevents a late callback from
     * making the published data-ready timestamp move backwards. */
    uint64_t damdf8_timestamp_ns;
    /* Source and validity of the CDR2 value that can be acknowledged by a
     * CDR2 read.  This is component state, not runtime wiring. */
    bool cdr2_valid;
    uint8_t cdr2_source;
    /* CDR2 direct-DMA reservation is synchronous runtime state.  Keep the
     * producer source while a destination transaction is in flight so the
     * commit path cannot acknowledge the wrong ADC. */
    bool cdr2_read_reserved;
    uint8_t cdr2_read_reserved_source;
    bool ccr_configured;
    /* Monotonic ID generator for common-owned regular ADC pair starts. */
    uint64_t next_conversion_id;

    /* One bounded pending sample per ADC.  These are component state because
     * an unmatched producer event changes the next CDR result. */
    bool master_sample_valid;
    uint64_t master_sample_conversion_id;
    uint32_t master_sample_rank;
    uint16_t master_sample_value;
    uint64_t master_sample_timestamp_ns;
    bool slave_sample_valid;
    uint64_t slave_sample_conversion_id;
    uint32_t slave_sample_rank;
    uint16_t slave_sample_value;
    uint64_t slave_sample_timestamp_ns;

    /* Status sources are runtime wiring and are deliberately not migrated. */
    DmMc02AdcCommonStatusRead master_status_read;
    void *master_status_opaque;
    DmMc02AdcCommonStatusRead slave_status_read;
    void *slave_status_opaque;

    /* Clock policy remains outside the reusable register block. */
    DmMc02AdcCommonClockChanged clock_changed;
    void *clock_changed_opaque;
    /* Packed CDR data consumer; not part of component state. */
    DmMc02AdcCommonDataReady data_ready;
    void *data_ready_opaque;
    /* CDR2 multimode DMA data consumer; not part of component state. */
    DmMc02AdcCommonCdr2DataReady cdr2_data_ready;
    void *cdr2_data_ready_opaque;
    /* CDR read side-effect consumer; not part of component state. */
    DmMc02AdcCommonCdrRead cdr_read;
    void *cdr_read_opaque;
    /* CDR2 read side-effect consumer; not part of component state. */
    DmMc02AdcCommonCdr2Read cdr2_read;
    void *cdr2_read_opaque;
    /* Regular multimode start is runtime wiring; the common block owns the
     * master/slave admission rule and asks its composition to start a peer. */
    DmMc02AdcCommonRegularStartPeer regular_start_peer;
    void *regular_start_peer_opaque;
    /* External regular-trigger admission is runtime wiring. */
    DmMc02AdcCommonExternalTriggerPeer external_trigger_peer;
    void *external_trigger_peer_opaque;
} DmMc02AdcCommon;

void dm_mc02_adc_common_init(DmMc02AdcCommon *state, Object *owner,
                             const char *name);
void dm_mc02_adc_common_reset(DmMc02AdcCommon *state);

void dm_mc02_adc_common_set_status_sources(
    DmMc02AdcCommon *state, DmMc02AdcCommonStatusRead master,
    void *master_opaque, DmMc02AdcCommonStatusRead slave,
    void *slave_opaque);
void dm_mc02_adc_common_set_clock_callback(
    DmMc02AdcCommon *state, DmMc02AdcCommonClockChanged callback,
    void *opaque);
void dm_mc02_adc_common_set_data_ready_callback(
    DmMc02AdcCommon *state, DmMc02AdcCommonDataReady callback,
    void *opaque);
void dm_mc02_adc_common_set_cdr2_data_ready_callback(
    DmMc02AdcCommon *state, DmMc02AdcCommonCdr2DataReady callback,
    void *opaque);
void dm_mc02_adc_common_set_cdr_read_callback(
    DmMc02AdcCommon *state, DmMc02AdcCommonCdrRead callback, void *opaque);
void dm_mc02_adc_common_set_cdr2_read_callback(
    DmMc02AdcCommon *state, DmMc02AdcCommonCdr2Read callback, void *opaque);
/* Notify the common block that a consumer completed a CDR data read.  The
 * endpoint DMA path uses this explicit hook because it does not traverse the
 * common MemoryRegion read callback. */
void dm_mc02_adc_common_notify_cdr_read(DmMc02AdcCommon *state);
/* The CDR source reservation is protocol-neutral: it borrows one current
 * word without acknowledging producer EOC flags.  A direct DMA adapter must
 * pair every successful prepare with exactly one commit or abort. */
bool dm_mc02_adc_common_cdr_data_pending(const DmMc02AdcCommon *state);
/* DMA admission includes the common OVR gate; CPU read ack does not. */
bool dm_mc02_adc_common_cdr_dma_request_pending(
    const DmMc02AdcCommon *state);
bool dm_mc02_adc_common_cdr_read_prepare(DmMc02AdcCommon *state,
                                         uint8_t *data, unsigned size);
void dm_mc02_adc_common_cdr_read_commit(DmMc02AdcCommon *state);
void dm_mc02_adc_common_cdr_read_abort(DmMc02AdcCommon *state);
/* CDR2 has a separate one-result reservation and source-specific read ack. */
bool dm_mc02_adc_common_cdr2_data_pending(
    const DmMc02AdcCommon *state);
bool dm_mc02_adc_common_cdr2_read_prepare(DmMc02AdcCommon *state,
                                          uint8_t *data, unsigned size);
void dm_mc02_adc_common_cdr2_read_commit(DmMc02AdcCommon *state);
void dm_mc02_adc_common_cdr2_read_abort(DmMc02AdcCommon *state);
/* Legacy FIFO consumers acknowledge immediately, before destination writes.
 * These calls never reserve data and cannot roll back source consumption. */
bool dm_mc02_adc_common_cdr_read_consuming(DmMc02AdcCommon *state,
                                          uint8_t *data, unsigned size);
bool dm_mc02_adc_common_cdr2_read_consuming(DmMc02AdcCommon *state,
                                           uint8_t *data, unsigned size);
/* Notify the common block that a consumer completed a CDR2 data read.  The
 * endpoint DMA path uses this explicit hook because it does not traverse the
 * common MemoryRegion read callback. */
void dm_mc02_adc_common_notify_cdr2_read(DmMc02AdcCommon *state);
void dm_mc02_adc_common_set_regular_start_peer(
    DmMc02AdcCommon *state, DmMc02AdcCommonRegularStartPeer callback,
    void *opaque);
void dm_mc02_adc_common_set_external_trigger_peer(
    DmMc02AdcCommon *state, DmMc02AdcCommonExternalTriggerPeer callback,
    void *opaque);

/* Admit one ADC regular-start request.  In regular simultaneous and regular
 * interleaved modes the master request allocates a shared conversion_id and
 * starts the slave through the peer callback; a slave request is rejected.
 * Other DUAL values retain independent-ADC admission behavior. */
bool dm_mc02_adc_common_admit_regular_start(DmMc02AdcCommon *state,
                                            unsigned source);

/* Admit one external regular-trigger edge.  In regular simultaneous and
 * regular interleaved modes only ADC1/master may own the edge; the composition
 * callback starts the enabled ADC2/slave with the same common-generated
 * conversion ID.  In other DUAL modes this leaves conversion_id zero and
 * permits the ADC-local path. */
bool dm_mc02_adc_common_admit_external_trigger(
    DmMc02AdcCommon *state, unsigned source, uint32_t trigger_source,
    bool rising, unsigned event_count, uint64_t timestamp_ns,
    uint64_t *conversion_id);

/* Explicit fixture hook for loading read-only data registers. */
void dm_mc02_adc_common_set_data(DmMc02AdcCommon *state, uint32_t cdr,
                                 uint32_t cdr2);

/* Submit one completed regular rank. Source 0 is ADC1/master and source 1 is
 * ADC2/slave. A CDR update occurs only for an exact non-zero
 * conversion_id/rank match in a supported regular-simultaneous data format;
 * DAMDF=3 waits for two regular-simultaneous pairs, or four ordered
 * regular-interleaved values, before publishing its four-byte CDR word.
 * Supported regular-interleaved modes publish 32-bit results in CDR2 for
 * DAMDF=2, or four alternating 8-bit results in CDR for DAMDF=3. The
 * published timestamp is the maximum timestamp represented by the word;
 * producer timestamps are diagnostic and need not be identical. */
DmMc02AdcCommonRegularSampleResult dm_mc02_adc_common_submit_regular_sample(
    DmMc02AdcCommon *state, unsigned source, uint64_t conversion_id,
    uint32_t rank, uint16_t value, uint64_t timestamp_ns);

uint32_t dm_mc02_adc_common_get_ccr(const DmMc02AdcCommon *state);
unsigned dm_mc02_adc_common_get_dual(const DmMc02AdcCommon *state);
unsigned dm_mc02_adc_common_get_delay_code(const DmMc02AdcCommon *state);
bool dm_mc02_adc_common_regular_interleaved(
    const DmMc02AdcCommon *state);
/* Return whether the configured dual mode emits the modeled CDR2 DMA beat. */
bool dm_mc02_adc_common_cdr2_dma_supported(
    const DmMc02AdcCommon *state);
bool dm_mc02_adc_common_ccr_configured(const DmMc02AdcCommon *state);
void dm_mc02_adc_common_sync_runtime(DmMc02AdcCommon *state);
bool dm_mc02_adc_common_state_valid(const DmMc02AdcCommon *state);

/* Component-only state contract; machine-level migration is not registered. */
const VMStateDescription *dm_mc02_adc_common_vmstate(void);
/* Child description for an enclosing ADC/common composite.  It validates
 * state but deliberately does not invoke the clock callback. */
extern const VMStateDescription vmstate_dm_mc02_adc_common_raw;

#endif
