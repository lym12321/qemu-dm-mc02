/* Minimal STM32H7 ADC register windows for the DM-MC02 machine. */
#ifndef HW_ARM_DM_MC02_ADC_H
#define HW_ARM_DM_MC02_ADC_H

#include "exec/memory.h"
#include "hw/arm/dm_mc02_dma.h"
#include "hw/arm/dm_mc02_trigger.h"
#include "hw/irq.h"
#include "migration/vmstate.h"
#include "qemu/timer.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_ADC_REGION_SIZE 0x100
#define DM_MC02_ADC_CHANNEL_COUNT 32
#define DM_MC02_ADC_IRQ 18
#define DM_MC02_ADC_LINEAR_CALIBRATION_WORDS 6

typedef struct DmMc02AdcRegularSample {
    /* Non-zero only for a common-owned regular ADC pair. */
    uint64_t conversion_id;
    uint32_t rank;
    uint16_t value;
    uint64_t timestamp_ns;
} DmMc02AdcRegularSample;

typedef void (*DmMc02AdcRegularSampleReady)(
    void *opaque, unsigned adc_index, const DmMc02AdcRegularSample *sample);
typedef bool (*DmMc02AdcRegularStartRequest)(void *opaque,
                                             unsigned adc_index);
typedef bool (*DmMc02AdcRegularExternalTriggerRequest)(
    void *opaque, unsigned adc_index, uint32_t trigger_source, bool rising,
    unsigned event_count, uint64_t timestamp_ns, uint64_t *conversion_id);

/* STM32H723 regular EXTSEL source IDs used by the shared trigger bus.  The
 * injected-group JEXTSEL values are mapped separately in dm_mc02_adc.c. */
#define DM_MC02_TRIGGER_TIM2_TRGO DM_MC02_TRIGGER_SOURCE_TIM2_TRGO
#define DM_MC02_TRIGGER_TIM3_TRGO DM_MC02_TRIGGER_SOURCE_TIM3_TRGO
#define DM_MC02_TRIGGER_TIM8_TRGO DM_MC02_TRIGGER_SOURCE_TIM8_TRGO
#define DM_MC02_TRIGGER_TIM8_TRGO2 DM_MC02_TRIGGER_SOURCE_TIM8_TRGO2
#define DM_MC02_TRIGGER_TIM1_TRGO DM_MC02_TRIGGER_SOURCE_TIM1_TRGO
#define DM_MC02_TRIGGER_TIM1_TRGO2 DM_MC02_TRIGGER_SOURCE_TIM1_TRGO2
/* Kept for callers written against the pre-trigger-bus API. */
#define DM_MC02_ADC_TRIGGER_TIM8_UPDATE DM_MC02_TRIGGER_TIM8_TRGO

typedef struct DmMc02Adc {
    MemoryRegion iomem;
    uint8_t regs[DM_MC02_ADC_REGION_SIZE];
    QEMUTimer *sample_timer;
    QEMUTimer *injected_timer;
    QEMUTimer *regulator_timer;
    /* Absolute virtual-time deadline for the currently scheduled rank. */
    uint64_t next_sample_ns;
    bool conversion_active;
    /* CFGR.AUTDLY holds regular conversion after EOC until ADC_DR is read. */
    bool regular_waiting_for_data;
    bool regular_sequence_complete;
    /* External regular discontinuous mode pauses between trigger groups while
     * retaining the next sequence rank and ADSTART arm state. */
    bool regular_discontinuous_paused;
    bool regular_discontinuous_pause_after_data;
    unsigned regular_discontinuous_remaining;
    bool injected_conversion_active;
    bool calibration_active;
    /* Strict H723 power sequencing is optional while legacy fixtures migrate
     * from the historical model that accepted ADEN immediately after reset. */
    bool power_model;
    bool regulator_ready;
    uint64_t regulator_ready_ns;
    /* Number of effective ADC clock cycles in the active calibration. */
    uint64_t calibration_cycles;
    uint64_t calibration_remaining_cycles;
    uint64_t calibration_last_ns;
    uint64_t calibration_clock_hz;
    /* H723 exposes a 160-bit linearity factor through six 30-bit windows.
     * The last window contains only ten meaningful bits. */
    uint32_t linear_calibration_words[
        DM_MC02_ADC_LINEAR_CALIBRATION_WORDS];
    unsigned linear_calibration_window;
    /* Progress of the currently scheduled regular rank. */
    uint64_t rank_remaining_half_cycles;
    uint64_t rank_last_ns;
    uint64_t rank_clock_hz;
    /* Progress of the currently scheduled injected rank. */
    uint64_t next_injected_sample_ns;
    uint64_t injected_rank_remaining_half_cycles;
    uint64_t injected_rank_last_ns;
    uint64_t injected_rank_clock_hz;
    /* JAUTO serializes a continuous regular sequence with its automatic
     * injected sequence. */
    bool regular_waiting_for_auto_injected;
    /* JDISCEN pauses an externally triggered injected sequence after each
     * rank while keeping JADSTART armed for the next edge. */
    bool injected_discontinuous_paused;
    /* Queue-enabled injected conversion state: one active context and one
     * pending context provide the H723 two-context FIFO without allocation. */
    bool injected_active_context_valid;
    bool injected_pending_context_valid;
    uint32_t injected_active_jsqr;
    uint32_t injected_pending_jsqr;
    /* Effective ADC conversion clock after the board prescaler. */
    uint64_t clock_hz;
    /* The default mode keeps continuous conversion traffic bounded. */
    bool accurate_timing;
    unsigned current_rank;
    unsigned current_injected_rank;
    uint64_t regular_sequence_id;
    uint64_t regular_active_sequence_id;
    /* True while this regular sequence belongs to a common-owned pair. */
    bool regular_shared_conversion;
    /* set_samples() keeps the pre-sequence two-rank compatibility mode. */
    bool legacy_samples;
    uint16_t sample_values[2];
    /* STM32H7 ADC channels are five-bit values in six-bit SQR fields. */
    uint16_t board_source_values[DM_MC02_ADC_CHANNEL_COUNT];
    uint16_t external_values[DM_MC02_ADC_CHANNEL_COUNT];
    uint32_t board_source_valid;
    uint32_t external_raw_valid;
    uint32_t external_pin_voltage_valid;
    /* 0 = no external override, 1 = raw, 2 = ADC-pin voltage. */
    uint8_t external_override_kind[DM_MC02_ADC_CHANNEL_COUNT];
    DmMc02Dma *dma;
    const DmMc02Dmamux *dmamux;
    uint32_t dma_request_id;
    hwaddr dma_peripheral_addr;
    /* Optional callback boundary for the ADC data register. */
    DmMc02DmaEndpoint dma_endpoint;
    bool dma_endpoint_enabled;
    /* Synchronous direct-P2M reservation.  ADC_DR/EOC remains observable
     * until the destination memory write commits. */
    bool dma_read_reserved;
    uint32_t dma_read_reserved_value;
    unsigned dma_read_reserved_size;
    /* Shared STM32H723 ADC1/ADC2 interrupt output. */
    qemu_irq irq;
    bool irq_level;
    bool irq_level_valid;
    /* Runtime consumer wiring; the callback itself is not migrated. */
    DmMc02AdcRegularSampleReady regular_sample_ready;
    void *regular_sample_ready_opaque;
    unsigned regular_sample_source;
    /* Runtime common-block admission callback; not migrated. */
    DmMc02AdcRegularStartRequest regular_start_request;
    void *regular_start_request_opaque;
    unsigned regular_start_source;
    /* Runtime common admission for regular external-trigger edges. */
    DmMc02AdcRegularExternalTriggerRequest regular_trigger_request;
    void *regular_trigger_request_opaque;
    unsigned regular_trigger_source;
} DmMc02Adc;

void dm_mc02_adc_init(DmMc02Adc *state, Object *owner, const char *name);
void dm_mc02_adc_reset(DmMc02Adc *state);
void dm_mc02_adc_set_clock_hz(DmMc02Adc *state, uint64_t clock_hz);
void dm_mc02_adc_set_accurate_timing(DmMc02Adc *state, bool enabled);
void dm_mc02_adc_set_power_model(DmMc02Adc *state, bool enabled);

/* Connect the ADC's level-sensitive status interrupt output. */
void dm_mc02_adc_set_irq(DmMc02Adc *state, qemu_irq irq);

/* Connect the completed regular-rank producer to a reusable consumer. */
void dm_mc02_adc_set_regular_sample_callback(
    DmMc02Adc *state, unsigned adc_index, DmMc02AdcRegularSampleReady callback,
    void *opaque);

/* Connect the regular-start admission boundary used by ADC common modes. */
void dm_mc02_adc_set_regular_start_callback(
    DmMc02Adc *state, unsigned adc_index,
    DmMc02AdcRegularStartRequest callback, void *opaque);

/* Connect the regular external-trigger admission boundary. */
void dm_mc02_adc_set_regular_trigger_callback(
    DmMc02Adc *state, unsigned adc_index,
    DmMc02AdcRegularExternalTriggerRequest callback, void *opaque);

/* Attach the optional ADC data-path request to a DMA/DMAMUX stream. */
void dm_mc02_adc_set_dma(DmMc02Adc *state, DmMc02Dma *dma,
                         const DmMc02Dmamux *dmamux, uint32_t request_id,
                         hwaddr peripheral_addr);

/* Select the callback boundary for ADC DMA data reads.  The address-space
 * path remains available for compatibility and comparison. */
void dm_mc02_adc_set_dma_endpoint(DmMc02Adc *state, bool enabled);

/* Retry a pending ADC_DR result after the board observes a DMA1 stream
 * becoming enabled.  DMA still owns stream selection, addresses and status. */
void dm_mc02_adc_dma_stream_enabled(DmMc02Adc *state);

/* Set the deterministic values emitted for conversion ranks 1 and 2. */
void dm_mc02_adc_set_samples(DmMc02Adc *state, uint16_t first,
                             uint16_t second);

/* Inject one deterministic raw 16-bit value for an ADC channel.  Values for
 * channels which have not been injected use the documented rank fallback in
 * dm_mc02_adc.c; invalid (>31) channel numbers are ignored. */
void dm_mc02_adc_set_channel_raw(DmMc02Adc *state, uint16_t channel,
                                 uint16_t raw);

/* Set an external override at the ADC pin.  The voltage is in microvolts and
 * is converted to 16-bit ADC code with round-to-nearest and saturation at the
 * 0..3.3 V converter range.  This is distinct from a board source. */
void dm_mc02_adc_set_channel_pin_voltage_uv(DmMc02Adc *state,
                                            uint16_t channel,
                                            uint32_t voltage_uv);

/* Remove either kind of external override and reveal the board source. */
void dm_mc02_adc_clear_channel_override(DmMc02Adc *state, uint16_t channel);
bool dm_mc02_adc_channel_has_override(const DmMc02Adc *state,
                                      uint16_t channel);

/* Board models use these APIs so power updates never overwrite an external
 * raw/pin-voltage override. */
void dm_mc02_adc_set_board_source_raw(DmMc02Adc *state, uint16_t channel,
                                      uint16_t raw);
void dm_mc02_adc_set_board_source_pin_voltage_uv(DmMc02Adc *state,
                                                 uint16_t channel,
                                                 uint32_t voltage_uv);
void dm_mc02_adc_clear_board_source(DmMc02Adc *state, uint16_t channel);

/* Start/stop model-side observation for a future machine integration layer. */
void dm_mc02_adc_start(DmMc02Adc *state);
/* Start a software regular conversion at an absolute virtual timestamp.
 * This is used by the ADC common interleaved boundary to model the delayed
 * slave sampling phase.  The ADC is armed immediately, but no conversion
 * cycles are consumed before start_ns. */
bool dm_mc02_adc_start_regular_at(DmMc02Adc *state, uint64_t conversion_id,
                                  uint64_t start_ns);
void dm_mc02_adc_stop(DmMc02Adc *state);
bool dm_mc02_adc_enabled(const DmMc02Adc *state);

/* Return whether regular conversion data management is configured for DMA.
 * The board composition uses this to gate a common CDR DMA request without
 * reaching into the ADC register mirror. */
bool dm_mc02_adc_regular_dma_enabled(const DmMc02Adc *state);

/* A common CDR read acknowledges the regular result in this ADC without
 * reading ADC_DR itself.  AUTDLY uses the same continuation boundary. */
void dm_mc02_adc_acknowledge_regular_data(DmMc02Adc *state);

/* Seed the next software regular sequence with a common-owned ID.  A
 * non-software-trigger configuration intentionally leaves the producer
 * unshared, so external trigger paths cannot enter the common matcher. */
void dm_mc02_adc_set_regular_conversion_id(DmMc02Adc *state,
                                           uint64_t conversion_id);

/* Return ADCx.ISR flags in the bit positions used by ADC12_COMMON.CSR.
 * Unsupported H723 status sources are returned as zero. */
uint32_t dm_mc02_adc_status(const DmMc02Adc *state);

/* Rebuild ADC-owned virtual timers and the level-sensitive IRQ projection
 * after component state has been restored.  DMA, trigger, clock, IRQ and
 * common-register callbacks remain runtime wiring. */
void dm_mc02_adc_sync_runtime(DmMc02Adc *state);
bool dm_mc02_adc_state_valid(const DmMc02Adc *state);

/* Component-only state contract; machine-level migration is not registered. */
const VMStateDescription *dm_mc02_adc_vmstate(void);
/* Child description for an enclosing ADC/common composite.  It validates
 * state but deliberately does not arm timers or project IRQs. */
extern const VMStateDescription vmstate_dm_mc02_adc_raw;

/* Deliver a deterministic edge from a supported board timer.  The edge is
 * consumed only when ADC_CFGR EXTSEL/EXTEN select this source and polarity. */
bool dm_mc02_adc_external_trigger(DmMc02Adc *state, uint32_t source,
                                  bool rising);

/* Start one regular external-trigger event after common admission.  This is
 * a composition boundary: it bypasses a second common admission callback and
 * carries the already allocated shared conversion ID to the peer ADC. */
bool dm_mc02_adc_external_trigger_with_id(
    DmMc02Adc *state, uint32_t source, bool rising, unsigned event_count,
    uint64_t timestamp_ns, uint64_t conversion_id);
/* As above, but conversion_start_ns is the already-resolved sampling start
 * rather than the trigger edge.  It is a composition boundary for the
 * interleaved slave and does not re-enter common trigger admission. */
bool dm_mc02_adc_external_trigger_with_id_at(
    DmMc02Adc *state, uint32_t source, bool rising, unsigned event_count,
    uint64_t timestamp_ns, uint64_t conversion_start_ns,
    uint64_t conversion_id);

/* Resolve the slave sampling start from a master trigger/start timestamp.
 * The result is UINT64_MAX when the ADC kernel clock is stopped. */
uint64_t dm_mc02_adc_interleaved_slave_start_ns(
    const DmMc02Adc *state, uint64_t master_start_ns,
    unsigned delay_code);

/* Connect an ADC to the generic synchronous trigger-event bus. */
void dm_mc02_adc_trigger_sink(void *opaque,
                              const DmMc02TriggerEvent *event);

#endif
