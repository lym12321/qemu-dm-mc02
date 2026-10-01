/*
 * Minimal STM32H7 ADC model.
 *
 * The register window remains deliberately small and safe for firmware
 * probing.  When ADC1 is enabled and started, one virtual sample sequence is
 * produced at the configured ADC sampling rate.  The regular sequence is
 * decoded from the STM32H7 SQR1..SQR4 registers (ranks 1..16, six-bit fields
 * carrying five-bit channel values).  Conversion timing is deterministic and
 * follows SMPR1/SMPR2.
 */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_adc.h"

#define ADC_ISR  0x00
#define ADC_IER  0x04
#define ADC_CR   0x08
#define ADC_CFGR 0x0c
#define ADC_SMPR1 0x14
#define ADC_SMPR2 0x18
#define ADC_SQR1 0x30
#define ADC_SQR2 0x34
#define ADC_SQR3 0x38
#define ADC_SQR4 0x3c
#define ADC_JSQR 0x4c
#define ADC_DR   0x40
#define ADC_JDR1 0x80
#define ADC_JDR_COUNT 4u
#define ADC_DIFSEL 0xc0
#define ADC_CALFACT_RES13 0xc4
#define ADC_CALFACT2_RES14 0xc8

#define ADC_SQR1_L_SHIFT       0
#define ADC_SQR1_L_MASK        0x0fu
/* H723 SQR1 keeps bits 4:5 reserved, then uses 6-bit SQ fields at
 * 6, 12, 18 and 24 for ranks 1..4.  The high bit in each field is
 * reserved for the five-bit channel number but is part of the field stride. */
#define ADC_SQR1_SQ_SHIFT(rank) (6u + ((rank) - 1u) * 6u)
#define ADC_SQR1_SQ_MASK       0x1fu
#define ADC_SQR1_MAX_RANKS     4u
#define ADC_SQR2_SQ_SHIFT(rank) (((rank) - 5u) * 6u)
#define ADC_SQR3_SQ_SHIFT(rank) (((rank) - 10u) * 6u)
#define ADC_SQR4_SQ_SHIFT(rank) (((rank) - 15u) * 6u)
#define ADC_REGULAR_MAX_RANKS   16u

#define ADC_ISR_ADRDY (1u << 0)
#define ADC_ISR_EOC   (1u << 2)
#define ADC_ISR_EOS   (1u << 3)
#define ADC_ISR_OVR   (1u << 4)
#define ADC_ISR_JEOC  (1u << 5)
#define ADC_ISR_JEOS  (1u << 6)
#define ADC_ISR_JQOVF (1u << 10)

#define ADC_IER_EOCIE (1u << 2)
#define ADC_IER_EOSIE (1u << 3)
#define ADC_IER_OVRIE (1u << 4)
#define ADC_IER_JEOCIE (1u << 5)
#define ADC_IER_JEOSIE (1u << 6)
#define ADC_IER_JQOVFIE (1u << 10)

#define ADC_CR_ADEN    (1u << 0)
#define ADC_CR_ADDIS   (1u << 1)
#define ADC_CR_ADSTART (1u << 2)
#define ADC_CR_JADSTART (1u << 3)
#define ADC_CR_ADSTP   (1u << 4)
#define ADC_CR_JADSTP  (1u << 5)
#define ADC_CR_ADCALLIN (1u << 16)
#define ADC_CR_LINCALRDY_MASK (0x3fu << 22)
#define ADC_CR_ADCALDIF (1u << 30)
#define ADC_CR_ADCAL   (1u << 31)
#define ADC_CR_ADVREGEN (1u << 28)
#define ADC_CR_DEEPPWD  (1u << 29)

#define ADC_CALFACT_OFFSET_MASK ((UINT32_C(0x7ff)) | \
                                 (UINT32_C(0x7ff) << 16))
#define ADC_CALFACT2_LINEAR_MASK UINT32_C(0x3fffffff)
#define ADC_DIFSEL_MASK UINT32_C(0x000fffff)
#define ADC_LINEAR_WORD_MASK ADC_CALFACT2_LINEAR_MASK
#define ADC_LINEAR_LAST_WORD_MASK UINT32_C(0x000003ff)
#define ADC_LINEAR_Q30_SIGN UINT32_C(0x20000000)
#define ADC_LINEAR_Q30_MODULUS UINT32_C(0x40000000)

/* STM32H7 ADC_CFGR uses DMA[1:0] at bits 1:0 for data management. */
#define ADC_CFGR_DMA_MASK    (3u << 0)
#define ADC_CFGR_RES_SHIFT   2
#define ADC_CFGR_RES_MASK    (3u << ADC_CFGR_RES_SHIFT)
#define ADC_CFGR_CONT        (1u << 13)
#define ADC_CFGR_AUTDLY      (1u << 14)
#define ADC_CFGR_OVRMOD      (1u << 12)
#define ADC_CFGR_DISCEN      (1u << 16)
#define ADC_CFGR_DISCNUM_SHIFT 17
#define ADC_CFGR_DISCNUM_MASK  (7u << ADC_CFGR_DISCNUM_SHIFT)
#define ADC_CFGR_JDISCEN       (1u << 20)
#define ADC_CFGR_JQM           (1u << 21)
#define ADC_CFGR_JAUTO         (1u << 25)
#define ADC_CFGR_CIRCULAR    2u
#define ADC_CFGR_EXTSEL_SHIFT 5
#define ADC_CFGR_EXTSEL_MASK  (0x1fu << ADC_CFGR_EXTSEL_SHIFT)
#define ADC_CFGR_EXTEN_SHIFT 10
#define ADC_CFGR_EXTEN_MASK  (3u << ADC_CFGR_EXTEN_SHIFT)
#define ADC_CFGR_JQDIS       (1u << 31)

/* STM32H723 injected sequence layout.  The five-bit channel fields use the
 * same six-bit rank stride as SQR1, with the low bit of each stride reserved. */
#define ADC_JSQR_JL_MASK        0x3u
#define ADC_JSQR_JEXTSEL_SHIFT  2
#define ADC_JSQR_JEXTSEL_MASK   (0x1fu << ADC_JSQR_JEXTSEL_SHIFT)
#define ADC_JSQR_JEXTEN_SHIFT   7
#define ADC_JSQR_JEXTEN_MASK    (3u << ADC_JSQR_JEXTEN_SHIFT)
#define ADC_JSQR_JSQ_SHIFT(rank) (9u + ((rank) - 1u) * 6u)
#define ADC_JSQR_JSQ_MASK       0x1fu

/* The board's RCC configuration supplies a 96 MHz ADC kernel clock followed
 * by the configured asynchronous DIV64 prescaler (1.5 MHz).  Conversion time
 * is represented in half-cycles to avoid host floating point behavior. */
#define ADC_CLOCK_HZ 1500000u
#define ADC_SEQUENCE_MIN_INTERVAL_NS 1000000u
#define ADC_CALIBRATION_OFFSET_CYCLES  1280u
#define ADC_CALIBRATION_LINEAR_CYCLES 16384u
#define ADC_REGULATOR_STARTUP_NS 10000u

static void adc_stop_timer(DmMc02Adc *s);
static void adc_stop_injected_timer(DmMc02Adc *s);
static void adc_continue_after_data_read(DmMc02Adc *s);
static uint32_t adc_consume_regular_data(DmMc02Adc *s, hwaddr offset,
                                         unsigned size);
static bool adc_dma_endpoint_read(void *opaque, uint8_t *data, unsigned size,
                                  uint64_t timestamp_ns);
static DmMc02DmaEndpointResult adc_dma_endpoint_read_prepare(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns);
static void adc_dma_endpoint_read_commit(void *opaque);
static void adc_dma_endpoint_read_abort(void *opaque);
static void adc_regulator_tick(void *opaque);

static void adc_begin_regular_sequence(DmMc02Adc *s)
{
    s->regular_sequence_id++;
    if (!s->regular_sequence_id) {
        /* Sequence zero is reserved as the invalid/uninitialized marker. */
        s->regular_sequence_id = 1;
    }
    s->regular_active_sequence_id = s->regular_sequence_id;
    s->current_rank = 0;
}

static void adc_notify_regular_sample(DmMc02Adc *s, unsigned rank,
                                       uint16_t value, uint64_t timestamp_ns)
{
    DmMc02AdcRegularSample sample;

    if (!s->regular_sample_ready) {
        return;
    }
    if (!s->regular_active_sequence_id) {
        /* This can only occur for a hand-built test state.  Keep the event
         * contract valid without changing the ordinary start path. */
        adc_begin_regular_sequence(s);
    }
    sample = (DmMc02AdcRegularSample) {
        .conversion_id = s->regular_shared_conversion ?
                          s->regular_active_sequence_id : 0,
        .rank = rank,
        .value = value,
        .timestamp_ns = timestamp_ns,
    };
    s->regular_sample_ready(s->regular_sample_ready_opaque,
                            s->regular_sample_source, &sample);
}

static uint64_t adc_deadline_after(uint64_t base, uint64_t delay)
{
    return delay > UINT64_MAX - base ? UINT64_MAX : base + delay;
}

static const uint16_t adc_sampling_half_cycles[] = {
    3, 5, 17, 33, 65, 129, 775, 1621,
};
#define ADC_PIN_FULL_SCALE_UV  3300000u

enum {
    ADC_EXTERNAL_OVERRIDE_NONE = 0,
    ADC_EXTERNAL_OVERRIDE_RAW = 1,
    ADC_EXTERNAL_OVERRIDE_PIN_VOLTAGE = 2,
};

static uint16_t adc_pin_voltage_to_raw(uint32_t voltage_uv)
{
    uint64_t numerator;

    /* A source above the ADC reference is a saturated converter input. */
    voltage_uv = MIN(voltage_uv, ADC_PIN_FULL_SCALE_UV);
    numerator = (uint64_t)voltage_uv * UINT16_MAX;
    /* Integer half-up rounding is deterministic and avoids host FP modes. */
    numerator += ADC_PIN_FULL_SCALE_UV / 2;
    return numerator / ADC_PIN_FULL_SCALE_UV;
}

static uint32_t adc_load(const DmMc02Adc *s, hwaddr offset, unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; ++i) {
        value |= (uint32_t)s->regs[offset + i] << (i * 8);
    }
    return value;
}

static void adc_store(DmMc02Adc *s, hwaddr offset, uint64_t value,
                      unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        s->regs[offset + i] = value >> (i * 8);
    }
}

static bool adc_range_overlaps(hwaddr offset, unsigned size,
                               hwaddr register_offset)
{
    return offset < register_offset + sizeof(uint32_t) &&
           offset + size > register_offset;
}

static bool adc_regular_conversion_ongoing(const DmMc02Adc *s)
{
    return s->conversion_active ||
           (adc_load(s, ADC_CR, sizeof(uint32_t)) & ADC_CR_ADSTART);
}

static bool adc_injected_conversion_ongoing(const DmMc02Adc *s)
{
    return adc_load(s, ADC_CR, sizeof(uint32_t)) & ADC_CR_JADSTART;
}

static bool adc_calibration_factor_write_allowed(const DmMc02Adc *s)
{
    uint32_t cr = adc_load(s, ADC_CR, sizeof(uint32_t));

    /* This is the condition documented by HAL_ADCEx_Calibration_SetValue():
     * ADC enabled, no regular conversion and no calibration in progress. */
    return (cr & ADC_CR_ADEN) && !adc_regular_conversion_ongoing(s) &&
           !adc_injected_conversion_ongoing(s) && !s->calibration_active;
}

static bool adc_power_ready(const DmMc02Adc *s)
{
    uint32_t cr;

    if (!s->power_model) {
        return true;
    }
    cr = adc_load(s, ADC_CR, sizeof(uint32_t));
    return !(cr & ADC_CR_DEEPPWD) && (cr & ADC_CR_ADVREGEN) &&
           s->regulator_ready;
}

static void adc_stop_regulator_timer(DmMc02Adc *s)
{
    s->regulator_ready = false;
    s->regulator_ready_ns = 0;
    if (s->regulator_timer) {
        timer_del(s->regulator_timer);
    }
}

static void adc_schedule_regulator(DmMc02Adc *s)
{
    uint64_t now;

    if (!s->power_model || !s->regulator_timer ||
        (adc_load(s, ADC_CR, sizeof(uint32_t)) &
         (ADC_CR_DEEPPWD | ADC_CR_ADVREGEN)) != ADC_CR_ADVREGEN) {
        return;
    }
    if (!s->regulator_ready && s->regulator_ready_ns) {
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->regulator_ready = false;
    s->regulator_ready_ns = adc_deadline_after(now,
                                                ADC_REGULATOR_STARTUP_NS);
    timer_mod(s->regulator_timer, s->regulator_ready_ns);
}

static void adc_regulator_tick(void *opaque)
{
    DmMc02Adc *s = opaque;
    uint32_t cr;

    if (!s->power_model) {
        return;
    }
    cr = adc_load(s, ADC_CR, sizeof(uint32_t));
    if ((cr & (ADC_CR_DEEPPWD | ADC_CR_ADVREGEN)) != ADC_CR_ADVREGEN) {
        adc_stop_regulator_timer(s);
        return;
    }
    s->regulator_ready = true;
    s->regulator_ready_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
}

static void adc_apply_power_control(DmMc02Adc *s, uint32_t old_cr,
                                    uint32_t *cr)
{
    bool old_deep;
    bool new_deep;
    bool old_regulator;
    bool new_regulator;

    if (!s->power_model) {
        return;
    }
    old_deep = (old_cr & ADC_CR_DEEPPWD) != 0;
    new_deep = (*cr & ADC_CR_DEEPPWD) != 0;
    old_regulator = (old_cr & ADC_CR_ADVREGEN) != 0;
    new_regulator = (*cr & ADC_CR_ADVREGEN) != 0;

    if (new_deep) {
        /* DEEPPWD disables the internal regulator and the converter. */
        *cr &= ~(ADC_CR_ADVREGEN | ADC_CR_ADEN | ADC_CR_ADSTART |
                 ADC_CR_JADSTART);
        adc_stop_regulator_timer(s);
        adc_stop_timer(s);
        adc_stop_injected_timer(s);
        adc_store(s, ADC_ISR,
                  adc_load(s, ADC_ISR, sizeof(uint32_t)) &
                      ~ADC_ISR_ADRDY,
                  sizeof(uint32_t));
        return;
    }
    if (!new_regulator) {
        /* Turning off the regulator while enabled is a power-down edge. */
        adc_stop_regulator_timer(s);
        if (old_regulator || old_deep || (old_cr & ADC_CR_ADEN)) {
            *cr &= ~(ADC_CR_ADEN | ADC_CR_ADSTART | ADC_CR_JADSTART);
            adc_stop_timer(s);
            adc_stop_injected_timer(s);
            adc_store(s, ADC_ISR,
                      adc_load(s, ADC_ISR, sizeof(uint32_t)) &
                          ~ADC_ISR_ADRDY,
                      sizeof(uint32_t));
        }
        return;
    }
    if (old_deep || !old_regulator || !s->regulator_ready) {
        adc_schedule_regulator(s);
    }
}

static uint64_t adc_cycles_to_ns(const DmMc02Adc *s, uint64_t cycles)
{
    __uint128_t numerator;
    __uint128_t result;

    if (!s->clock_hz) {
        return UINT64_MAX;
    }
    numerator = (__uint128_t)cycles * UINT64_C(1000000000);
    result = (numerator + s->clock_hz - 1) / s->clock_hz;
    return result > UINT64_MAX ? UINT64_MAX : MAX((uint64_t)result,
                                                  UINT64_C(1));
}

static uint64_t adc_elapsed_cycles(uint64_t elapsed_ns, uint64_t clock_hz)
{
    __uint128_t cycles;

    if (!clock_hz || !elapsed_ns) {
        return 0;
    }
    if (clock_hz <= UINT64_MAX / elapsed_ns) {
        return elapsed_ns * clock_hz / UINT64_C(1000000000);
    }
    cycles = (__uint128_t)elapsed_ns * clock_hz / UINT64_C(1000000000);
    return cycles > UINT64_MAX ? UINT64_MAX : (uint64_t)cycles;
}

static uint64_t adc_elapsed_half_cycles(uint64_t elapsed_ns,
                                        uint64_t clock_hz)
{
    __uint128_t cycles;

    if (!clock_hz || !elapsed_ns) {
        return 0;
    }
    if (clock_hz <= UINT64_MAX / 2 &&
        elapsed_ns <= UINT64_MAX / (clock_hz * 2)) {
        return elapsed_ns * (clock_hz * 2) / UINT64_C(1000000000);
    }
    cycles = (__uint128_t)elapsed_ns * clock_hz * 2 /
             UINT64_C(1000000000);
    return cycles > UINT64_MAX ? UINT64_MAX : (uint64_t)cycles;
}

static uint64_t adc_half_cycles_to_ns(const DmMc02Adc *s,
                                      uint64_t half_cycles)
{
    __uint128_t numerator;
    __uint128_t denominator;
    __uint128_t result;

    if (!s->clock_hz) {
        return UINT64_MAX;
    }
    if (s->clock_hz <= UINT64_MAX / 2 &&
        half_cycles <= (UINT64_MAX - (s->clock_hz * 2 - 1)) /
                       UINT64_C(1000000000)) {
        uint64_t fast_denominator = s->clock_hz * 2;
        uint64_t fast_numerator = half_cycles * UINT64_C(1000000000);

        return MAX((fast_numerator + fast_denominator - 1) /
                   fast_denominator,
                   UINT64_C(1));
    }
    numerator = (__uint128_t)half_cycles * UINT64_C(1000000000);
    denominator = (__uint128_t)s->clock_hz * 2;
    result = (numerator + denominator - 1) / denominator;
    return result > UINT64_MAX ? UINT64_MAX : MAX((uint64_t)result,
                                                  UINT64_C(1));
}

static unsigned adc_sampling_code(const DmMc02Adc *s, unsigned channel);
static unsigned adc_sequence_channel(const DmMc02Adc *s, unsigned rank);
static unsigned adc_injected_sequence_channel(const DmMc02Adc *s,
                                              unsigned rank);

static unsigned adc_conversion_half_cycles(const DmMc02Adc *s)
{
    /* H723 ADC1/2 RES encodes 16, 14, 12 and 10 bits respectively.  The
     * processing time is resolution + 0.5 ADC clocks (ST HAL/LL ADC). */
    static const uint8_t cycles[] = { 33, 29, 25, 21 };
    unsigned resolution = (adc_load(s, ADC_CFGR, 4) & ADC_CFGR_RES_MASK) >>
                          ADC_CFGR_RES_SHIFT;

    return cycles[resolution];
}

static uint64_t adc_rank_half_cycles(const DmMc02Adc *s, unsigned rank)
{
    unsigned channel = s->legacy_samples ? rank :
                        adc_sequence_channel(s, rank + 1u);
    unsigned code = adc_sampling_code(s, channel);

    return adc_conversion_half_cycles(s) + adc_sampling_half_cycles[code];
}

static uint64_t adc_injected_rank_half_cycles(const DmMc02Adc *s,
                                             unsigned rank)
{
    unsigned channel = adc_injected_sequence_channel(s, rank + 1u);
    unsigned code = adc_sampling_code(s, channel);

    return adc_conversion_half_cycles(s) + adc_sampling_half_cycles[code];
}

static void adc_account_calibration(DmMc02Adc *s, uint64_t now)
{
    uint64_t elapsed;
    uint64_t consumed;

    if (!s->calibration_active || now <= s->calibration_last_ns) {
        return;
    }
    elapsed = now - s->calibration_last_ns;
    consumed = adc_elapsed_cycles(elapsed, s->calibration_clock_hz);
    s->calibration_remaining_cycles =
        consumed >= s->calibration_remaining_cycles ? 0 :
        s->calibration_remaining_cycles - consumed;
    s->calibration_last_ns = now;
}

static void adc_account_rank(DmMc02Adc *s, uint64_t now)
{
    uint64_t elapsed;
    uint64_t consumed;

    if (!s->conversion_active || !s->rank_remaining_half_cycles ||
        now <= s->rank_last_ns) {
        return;
    }
    elapsed = now - s->rank_last_ns;
    consumed = adc_elapsed_half_cycles(elapsed, s->rank_clock_hz);
    s->rank_remaining_half_cycles =
        consumed >= s->rank_remaining_half_cycles ? 0 :
        s->rank_remaining_half_cycles - consumed;
    s->rank_last_ns = now;
}

static void adc_account_injected_rank(DmMc02Adc *s, uint64_t now)
{
    uint64_t elapsed;
    uint64_t consumed;

    if (!s->injected_conversion_active ||
        !s->injected_rank_remaining_half_cycles ||
        now <= s->injected_rank_last_ns) {
        return;
    }
    elapsed = now - s->injected_rank_last_ns;
    consumed = adc_elapsed_half_cycles(elapsed, s->injected_rank_clock_hz);
    s->injected_rank_remaining_half_cycles =
        consumed >= s->injected_rank_remaining_half_cycles ? 0 :
        s->injected_rank_remaining_half_cycles - consumed;
    s->injected_rank_last_ns = now;
}

static void adc_schedule_calibration(DmMc02Adc *s)
{
    uint64_t delay_ns;

    if (!s->sample_timer || !s->calibration_active ||
        !s->calibration_remaining_cycles) {
        return;
    }
    delay_ns = adc_cycles_to_ns(s, s->calibration_remaining_cycles);
    if (delay_ns == UINT64_MAX) {
        /* A stopped ADC kernel clock cannot make calibration progress.  Keep
         * ADCAL asserted and wait for the clock callback to reschedule it. */
        timer_del(s->sample_timer);
        s->next_sample_ns = 0;
        return;
    }
    s->next_sample_ns = adc_deadline_after(
        qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL), delay_ns);
    timer_mod(s->sample_timer, s->next_sample_ns);
}

static void adc_finish_calibration(DmMc02Adc *s)
{
    uint32_t cr;

    if (!s->calibration_active) {
        return;
    }
    cr = adc_load(s, ADC_CR, 4) & ~ADC_CR_ADCAL;
    if (cr & ADC_CR_ADCALLIN) {
        /* The six ready-word bits are the hand-off used by the LL
         * linear-factor accessors.  The factor itself remains an explicitly
         * programmed value; this model has no analog calibration source from
         * which to derive one. */
        cr |= ADC_CR_LINCALRDY_MASK;
    }
    adc_store(s, ADC_CR, cr, 4);
    s->calibration_active = false;
    s->calibration_cycles = 0;
    s->calibration_remaining_cycles = 0;
    s->calibration_last_ns = 0;
    s->calibration_clock_hz = 0;
    s->next_sample_ns = 0;
    if (s->sample_timer) {
        timer_del(s->sample_timer);
    }
}

static void adc_mask_calibration_registers(DmMc02Adc *s)
{
    uint32_t linear_mask = s->linear_calibration_window == 5 ?
                           ADC_LINEAR_LAST_WORD_MASK :
                           ADC_LINEAR_WORD_MASK;

    adc_store(s, ADC_CALFACT_RES13,
              adc_load(s, ADC_CALFACT_RES13, sizeof(uint32_t)) &
                  ADC_CALFACT_OFFSET_MASK,
              sizeof(uint32_t));
    adc_store(s, ADC_CALFACT2_RES14,
              adc_load(s, ADC_CALFACT2_RES14, sizeof(uint32_t)) &
                  linear_mask,
              sizeof(uint32_t));
}

static uint16_t adc_apply_calibration(const DmMc02Adc *s, uint16_t raw,
                                      unsigned channel)
{
    uint32_t offset_register;
    uint32_t offset;
    uint32_t linear_word;
    int64_t result;
    int64_t linear_delta;
    int64_t correction;

    /* The offset factor is selected by the channel's input mode.  The
     * register stores the factor as a positive magnitude; the converter
     * applies it by subtracting from the unsigned conversion result. */
    offset_register = adc_load(s, ADC_CALFACT_RES13, sizeof(uint32_t));
    if (channel < 20 &&
        (adc_load(s, ADC_DIFSEL, sizeof(uint32_t)) & ADC_DIFSEL_MASK &
         (UINT32_C(1) << channel))) {
        offset = (offset_register >> 16) & UINT32_C(0x7ff);
    } else {
        offset = offset_register & UINT32_C(0x7ff);
    }
    result = raw > offset ? (int64_t)raw - offset : 0;

    /* A real H723 applies a 160-bit capacitor-linearity trim in the analog
     * core.  This compact ADC has no capacitor-level SAR model, so use the
     * lowest 30-bit word as a signed Q0.30 gain delta.  Zero is identity;
     * all six words are still retained and exposed through the real window
     * protocol, allowing a higher-fidelity analog core to replace this pure
     * data transform without changing the guest-facing interface. */
    linear_word = s->linear_calibration_words[0] & ADC_LINEAR_WORD_MASK;
    linear_delta = linear_word & ADC_LINEAR_Q30_SIGN ?
                   (int64_t)linear_word - ADC_LINEAR_Q30_MODULUS :
                   (int64_t)linear_word;
    correction = result * linear_delta;
    correction += correction >= 0 ? INT64_C(1) << 29 : -(INT64_C(1) << 29);
    result += correction / (INT64_C(1) << 30);

    return result <= 0 ? 0 : result >= UINT16_MAX ? UINT16_MAX :
           (uint16_t)result;
}

static void adc_process_linear_window_write(DmMc02Adc *s, uint32_t old_cr,
                                            uint32_t *cr,
                                            uint32_t write_mask)
{
    uint32_t touched = write_mask & ADC_CR_LINCALRDY_MASK;

    if (!touched) {
        return;
    }
    if (!(old_cr & ADC_CR_ADEN) ||
        (old_cr & (ADC_CR_ADSTART | ADC_CR_JADSTART)) ||
        s->conversion_active || s->injected_conversion_active ||
        s->calibration_active) {
        /* The real register only permits this protocol while the ADC is
         * enabled and both conversion groups are idle. */
        *cr = (*cr & ~ADC_CR_LINCALRDY_MASK) |
              (old_cr & ADC_CR_LINCALRDY_MASK);
        return;
    }

    for (unsigned word = 0; word < DM_MC02_ADC_LINEAR_CALIBRATION_WORDS;
         ++word) {
        uint32_t bit = UINT32_C(1) << (22u + word);
        uint32_t mask = word == 5 ? ADC_LINEAR_LAST_WORD_MASK :
                        ADC_LINEAR_WORD_MASK;
        bool was_set = (old_cr & bit) != 0;
        bool is_set = (*cr & bit) != 0;

        if (was_set && !is_set) {
            /* Read path: clearing a ready bit selects that saved word in
             * CALFACT2.  The H723 exposes only ten bits for word six. */
            s->linear_calibration_window = word;
            adc_store(s, ADC_CALFACT2_RES14,
                      s->linear_calibration_words[word] & mask, 4);
        } else if (!was_set && is_set) {
            /* Write path: the current CALFACT2 window is committed when its
             * ready bit is set. */
            s->linear_calibration_window = word;
            s->linear_calibration_words[word] =
                adc_load(s, ADC_CALFACT2_RES14, 4) & mask;
        }
    }
}

static void adc_update_irq(DmMc02Adc *s)
{
    uint32_t isr = adc_load(s, ADC_ISR, 4);
    uint32_t ier = adc_load(s, ADC_IER, 4);
    bool pending = ((isr & ADC_ISR_EOC) && (ier & ADC_IER_EOCIE)) ||
                   ((isr & ADC_ISR_EOS) && (ier & ADC_IER_EOSIE)) ||
                   ((isr & ADC_ISR_OVR) && (ier & ADC_IER_OVRIE)) ||
                   ((isr & ADC_ISR_JEOC) && (ier & ADC_IER_JEOCIE)) ||
                   ((isr & ADC_ISR_JEOS) && (ier & ADC_IER_JEOSIE)) ||
                   ((isr & ADC_ISR_JQOVF) && (ier & ADC_IER_JQOVFIE));

    if (s->irq && (!s->irq_level_valid || s->irq_level != pending)) {
        /* ADC interrupt is a level, not a pulse: flags and enables are the
         * complete source of truth, including changes made by firmware. */
        s->irq_level = pending;
        s->irq_level_valid = true;
        qemu_set_irq(s->irq, pending);
    }
}

static bool adc_dma_configured(const DmMc02Adc *s)
{
    uint32_t cfgr = adc_load(s, ADC_CFGR, 4);

    return (cfgr & ADC_CFGR_DMA_MASK) != 0;
}

static unsigned adc_sequence_length(const DmMc02Adc *s)
{
    uint32_t sqr1;
    unsigned length;

    if (s->legacy_samples) {
        return ARRAY_SIZE(s->sample_values);
    }
    sqr1 = adc_load(s, ADC_SQR1, 4);
    length = ((sqr1 >> ADC_SQR1_L_SHIFT) & ADC_SQR1_L_MASK) + 1u;
    /* SQR1..SQR4 contain SQ1..SQ16. */
    return MIN(length, ADC_REGULAR_MAX_RANKS);
}

static bool adc_regular_discontinuous_external(const DmMc02Adc *s)
{
    uint32_t cfgr = adc_load(s, ADC_CFGR, 4);

    /* H723 forbids CONT and DISCEN together.  If a raw register writer
     * creates that combination, retain the continuous path as the dominant
     * behavior instead of inventing two competing schedulers. */
    return (cfgr & ADC_CFGR_DISCEN) &&
           (cfgr & ADC_CFGR_EXTEN_MASK) &&
           !(cfgr & ADC_CFGR_CONT);
}

static unsigned adc_regular_discontinuous_count(const DmMc02Adc *s)
{
    uint32_t cfgr = adc_load(s, ADC_CFGR, 4);

    return ((cfgr & ADC_CFGR_DISCNUM_MASK) >> ADC_CFGR_DISCNUM_SHIFT) + 1u;
}

static uint32_t adc_injected_context_register(const DmMc02Adc *s)
{
    return s->injected_active_context_valid ? s->injected_active_jsqr :
           adc_load(s, ADC_JSQR, 4);
}

static unsigned adc_sequence_channel(const DmMc02Adc *s, unsigned rank)
{
    uint32_t sequence;
    unsigned shift;

    if (rank <= ADC_SQR1_MAX_RANKS) {
        sequence = adc_load(s, ADC_SQR1, 4);
        shift = ADC_SQR1_SQ_SHIFT(rank);
    } else if (rank <= 9u) {
        sequence = adc_load(s, ADC_SQR2, 4);
        shift = ADC_SQR2_SQ_SHIFT(rank);
    } else if (rank <= ADC_REGULAR_MAX_RANKS) {
        if (rank <= 14u) {
            sequence = adc_load(s, ADC_SQR3, 4);
            shift = ADC_SQR3_SQ_SHIFT(rank);
        } else {
            sequence = adc_load(s, ADC_SQR4, 4);
            shift = ADC_SQR4_SQ_SHIFT(rank);
        }
    } else {
        return 0;
    }

    return (sequence >> shift) & ADC_SQR1_SQ_MASK;
}

static unsigned adc_injected_sequence_length(const DmMc02Adc *s)
{
    uint32_t jsqr = adc_injected_context_register(s);

    return ((jsqr & ADC_JSQR_JL_MASK) + 1u);
}

static bool adc_injected_discontinuous(const DmMc02Adc *s)
{
    uint32_t cfgr = adc_load(s, ADC_CFGR, 4);
    uint32_t jsqr = adc_injected_context_register(s);

    /* JDISCEN has an observable effect only for a multi-rank injected
     * sequence selected by an external trigger; software JADSTART runs the
     * complete sequence.  The HAL documents the single-rank configuration
     * as a discarded setting. */
    return (cfgr & ADC_CFGR_JDISCEN) &&
           (jsqr & ADC_JSQR_JEXTEN_MASK) &&
           adc_injected_sequence_length(s) > 1u;
}

static bool adc_automatic_injected_enabled(const DmMc02Adc *s)
{
    uint32_t cfgr = adc_load(s, ADC_CFGR, 4);
    uint32_t jsqr = adc_injected_context_register(s);

    /* JAUTO is valid for an injected software trigger only.  HAL also
     * rejects both discontinuous modes with automatic injection; retaining
     * that boundary here keeps raw register configurations deterministic. */
    return (cfgr & ADC_CFGR_JAUTO) &&
           !(cfgr & (ADC_CFGR_DISCEN | ADC_CFGR_JDISCEN)) &&
           !(jsqr & ADC_JSQR_JEXTEN_MASK) &&
           s->injected_active_context_valid;
}

static void adc_injected_context_flush(DmMc02Adc *s)
{
    s->injected_active_context_valid = false;
    s->injected_pending_context_valid = false;
    s->injected_active_jsqr = 0;
    s->injected_pending_jsqr = 0;
    s->current_injected_rank = 0;
    s->injected_discontinuous_paused = false;
    adc_store(s, ADC_JSQR, 0, 4);
}

static void adc_injected_context_commit(DmMc02Adc *s, uint32_t jsqr)
{
    uint32_t cfgr = adc_load(s, ADC_CFGR, 4);
    uint32_t isr;

    if (cfgr & ADC_CFGR_JQDIS) {
        if (adc_injected_conversion_ongoing(s)) {
            return;
        }
        s->injected_active_jsqr = jsqr;
        s->injected_active_context_valid = true;
        s->injected_pending_context_valid = false;
        s->injected_pending_jsqr = 0;
        adc_store(s, ADC_JSQR, jsqr, 4);
        return;
    }

    if (!s->injected_active_context_valid) {
        s->injected_active_jsqr = jsqr;
        s->injected_active_context_valid = true;
        adc_store(s, ADC_JSQR, jsqr, 4);
        return;
    }
    if (!s->injected_pending_context_valid) {
        s->injected_pending_jsqr = jsqr;
        s->injected_pending_context_valid = true;
        adc_store(s, ADC_JSQR, jsqr, 4);
        return;
    }

    /* A full queue rejects the new context and reports only the queue
     * admission failure; neither existing snapshot may be disturbed. */
    isr = adc_load(s, ADC_ISR, 4) | ADC_ISR_JQOVF;
    adc_store(s, ADC_ISR, isr, 4);
    adc_update_irq(s);
}

static unsigned adc_injected_sequence_channel(const DmMc02Adc *s,
                                              unsigned rank)
{
    uint32_t jsqr = adc_injected_context_register(s);
    unsigned shift = ADC_JSQR_JSQ_SHIFT(rank);

    return (jsqr >> shift) & ADC_JSQR_JSQ_MASK;
}

static unsigned adc_sampling_code(const DmMc02Adc *s, unsigned channel)
{
    hwaddr offset;
    unsigned shift;

    /* H723 exposes channels 0..9 in SMPR1 and 10..19 in SMPR2.  Channels
     * outside that documented subset use the reset/default shortest time. */
    if (channel >= 20) {
        return 0;
    }
    offset = channel < 10 ? ADC_SMPR1 : ADC_SMPR2;
    shift = (channel % 10) * 3;
    return (adc_load(s, offset, 4) >> shift) & 7u;
}

static uint64_t adc_rank_period_ns(const DmMc02Adc *s, unsigned rank)
{
    /* Round up so every conversion advances virtual time by at least one ns. */
    return adc_half_cycles_to_ns(s, adc_rank_half_cycles(s, rank));
}

static unsigned adc_resolution_index(const DmMc02Adc *s)
{
    /* H7 encoding is 00=16-bit, 01=14-bit, 10=12-bit, 11=10-bit. */
    return (adc_load(s, ADC_CFGR, 4) & ADC_CFGR_RES_MASK) >>
           ADC_CFGR_RES_SHIFT;
}

static uint64_t adc_interleaved_delay_half_cycles(const DmMc02Adc *s,
                                                  unsigned delay_code)
{
    /* Table 236 of RM0468.  Entries are expressed in half ADC-kernel clocks
     * so the 1.5T, 2.5T, ... semantics stay exact in the integer scheduler.
     * Reserved codes saturate at the largest valid delay for the selected
     * resolution, matching the compact model's explicit bounded policy. */
    static const uint8_t delay_half_cycles[4][9] = {
        { 3, 5, 7, 9, 11, 13, 15, 17, 17 }, /* 16-bit */
        { 3, 5, 7, 9, 11, 13, 15, 15, 15 }, /* 14-bit */
        { 3, 5, 7, 9, 11, 13, 13, 13, 13 }, /* 12-bit */
        { 3, 5, 7, 9, 11, 11, 11, 11, 11 }, /* 10-bit */
    };
    unsigned resolution = adc_resolution_index(s);

    resolution = MIN(resolution, ARRAY_SIZE(delay_half_cycles) - 1);
    delay_code = MIN(delay_code, ARRAY_SIZE(delay_half_cycles[0]) - 1);
    return delay_half_cycles[resolution][delay_code];
}

uint64_t dm_mc02_adc_interleaved_slave_start_ns(
    const DmMc02Adc *state, uint64_t master_start_ns, unsigned delay_code)
{
    unsigned channel;
    unsigned sampling_code;
    uint64_t phase_half_cycles;

    if (!state || !state->clock_hz) {
        return UINT64_MAX;
    }
    channel = state->legacy_samples ? 0 :
              adc_sequence_channel(state, state->current_rank + 1u);
    sampling_code = adc_sampling_code(state, channel);
    phase_half_cycles = adc_sampling_half_cycles[sampling_code] +
                        adc_interleaved_delay_half_cycles(state, delay_code);
    return adc_deadline_after(
        master_start_ns, adc_half_cycles_to_ns(state, phase_half_cycles));
}

static uint16_t adc_channel_value(const DmMc02Adc *s, unsigned channel,
                                  unsigned fallback_rank)
{
    if (s->external_override_kind[channel] != ADC_EXTERNAL_OVERRIDE_NONE) {
        return s->external_values[channel];
    }
    if (s->board_source_valid & (UINT32_C(1) << channel)) {
        return s->board_source_values[channel];
    }
    /* Deterministic compatibility fallback: the first two ranks retain the
     * values historically supplied by set_samples(); additional ranks read
     * zero.  No host randomness or wall-clock state is involved. */
    return fallback_rank < ARRAY_SIZE(s->sample_values) ?
           s->sample_values[fallback_rank] : 0;
}

static uint16_t adc_rank_value(const DmMc02Adc *s, unsigned rank)
{
    unsigned channel;
    uint16_t raw;

    if (s->legacy_samples) {
        channel = rank;
        raw = s->sample_values[rank];
    } else {
        channel = adc_sequence_channel(s, rank + 1u);
        raw = adc_channel_value(s, channel, rank);
    }
    return adc_apply_calibration(s, raw, channel);
}

static uint16_t adc_injected_rank_value(const DmMc02Adc *s, unsigned rank)
{
    unsigned channel = adc_injected_sequence_channel(s, rank + 1u);

    return adc_apply_calibration(s, adc_channel_value(s, channel, rank),
                                 channel);
}

static void adc_stop_timer(DmMc02Adc *s)
{
    s->conversion_active = false;
    s->regular_waiting_for_data = false;
    s->regular_sequence_complete = false;
    s->regular_discontinuous_paused = false;
    s->regular_discontinuous_pause_after_data = false;
    s->regular_discontinuous_remaining = 0;
    s->regular_waiting_for_auto_injected = false;
    s->regular_shared_conversion = false;
    s->calibration_active = false;
    s->calibration_cycles = 0;
    s->calibration_remaining_cycles = 0;
    s->calibration_last_ns = 0;
    s->calibration_clock_hz = 0;
    s->current_rank = 0;
    s->next_sample_ns = 0;
    s->rank_remaining_half_cycles = 0;
    s->rank_last_ns = 0;
    s->rank_clock_hz = 0;
    if (s->sample_timer) {
        timer_del(s->sample_timer);
    }
}

static void adc_stop_injected_timer(DmMc02Adc *s)
{
    s->injected_conversion_active = false;
    s->injected_discontinuous_paused = false;
    s->current_injected_rank = 0;
    s->next_injected_sample_ns = 0;
    s->injected_rank_remaining_half_cycles = 0;
    s->injected_rank_last_ns = 0;
    s->injected_rank_clock_hz = 0;
    if (s->injected_timer) {
        timer_del(s->injected_timer);
    }
}

static void adc_schedule(DmMc02Adc *s)
{
    uint64_t now;

    if (!s->sample_timer || !s->conversion_active ||
        s->regular_waiting_for_data || !s->clock_hz) {
        if (s->sample_timer && !s->clock_hz) {
            timer_del(s->sample_timer);
        }
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->rank_remaining_half_cycles =
        adc_rank_half_cycles(s, s->current_rank);
    s->rank_last_ns = now;
    s->rank_clock_hz = s->clock_hz;
    s->next_sample_ns = adc_deadline_after(
        now, adc_half_cycles_to_ns(s, s->rank_remaining_half_cycles));
    timer_mod(s->sample_timer, s->next_sample_ns);
}

static void adc_schedule_at(DmMc02Adc *s, uint64_t start_ns)
{
    if (!s->sample_timer || !s->conversion_active ||
        s->regular_waiting_for_data) {
        return;
    }
    s->rank_remaining_half_cycles =
        adc_rank_half_cycles(s, s->current_rank);
    s->rank_last_ns = start_ns;
    s->rank_clock_hz = s->clock_hz;
    if (!s->clock_hz) {
        s->next_sample_ns = 0;
        timer_del(s->sample_timer);
        return;
    }
    s->next_sample_ns = adc_deadline_after(
        start_ns, adc_half_cycles_to_ns(s, s->rank_remaining_half_cycles));
    timer_mod(s->sample_timer, s->next_sample_ns);
}

static void adc_schedule_after(DmMc02Adc *s, uint64_t delay_ns)
{
    uint64_t now;

    if (!s->sample_timer || !s->conversion_active ||
        s->regular_waiting_for_data || !s->clock_hz) {
        if (s->sample_timer && !s->clock_hz) {
            timer_del(s->sample_timer);
        }
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->rank_remaining_half_cycles =
        adc_rank_half_cycles(s, s->current_rank);
    s->rank_last_ns = now;
    s->rank_clock_hz = s->clock_hz;
    s->next_sample_ns = adc_deadline_after(now, delay_ns);
    timer_mod(s->sample_timer, s->next_sample_ns);
}

static void adc_schedule_next(DmMc02Adc *s)
{
    uint64_t now;
    uint64_t base;

    if (!s->sample_timer || !s->conversion_active ||
        s->regular_waiting_for_data || !s->clock_hz) {
        if (s->sample_timer && !s->clock_hz) {
            timer_del(s->sample_timer);
        }
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    base = s->next_sample_ns > now ? s->next_sample_ns : now;
    s->rank_remaining_half_cycles =
        adc_rank_half_cycles(s, s->current_rank);
    s->rank_last_ns = base;
    s->rank_clock_hz = s->clock_hz;
    s->next_sample_ns = adc_deadline_after(
        base, adc_half_cycles_to_ns(s, s->rank_remaining_half_cycles));
    timer_mod(s->sample_timer, s->next_sample_ns);
}

static void adc_resume_regular_after_auto_injected(DmMc02Adc *s)
{
    uint32_t cr;
    uint32_t cfgr;

    if (!s->regular_waiting_for_auto_injected) {
        return;
    }
    s->regular_waiting_for_auto_injected = false;
    cr = adc_load(s, ADC_CR, 4);
    cfgr = adc_load(s, ADC_CFGR, 4);
    if ((cr & (ADC_CR_ADEN | ADC_CR_ADSTART)) !=
            (ADC_CR_ADEN | ADC_CR_ADSTART) ||
        !(cfgr & ADC_CFGR_CONT) || s->regular_waiting_for_data) {
        return;
    }
    s->conversion_active = true;
    adc_begin_regular_sequence(s);
    adc_schedule(s);
}

static void adc_reschedule_active(DmMc02Adc *s)
{
    uint64_t now;

    if (!s->sample_timer || !s->conversion_active ||
        s->regular_waiting_for_data) {
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (!s->accurate_timing) {
        /* The compatibility cadence is an intentional host-load bound, not
         * a clock-derived rank deadline.  Keep its existing phase. */
        if (!s->clock_hz) {
            s->rank_clock_hz = 0;
            s->rank_last_ns = now;
            timer_del(s->sample_timer);
        } else if (s->next_sample_ns > now) {
            s->rank_clock_hz = s->clock_hz;
            s->rank_last_ns = now;
            timer_mod(s->sample_timer, s->next_sample_ns);
        } else {
            adc_schedule(s);
        }
        return;
    }
    if (s->accurate_timing) {
        adc_account_rank(s, now);
    }
    if (!s->clock_hz) {
        s->rank_clock_hz = 0;
        if (s->rank_last_ns <= now) {
            s->rank_last_ns = now;
        }
        timer_del(s->sample_timer);
        return;
    }
    s->rank_clock_hz = s->clock_hz;
    if (s->rank_last_ns > now) {
        /* An external trigger may have armed a future conversion.  Its rank
         * has not consumed any cycles yet; only recompute the future deadline
         * from the trigger timestamp. */
        s->next_sample_ns = adc_deadline_after(
            s->rank_last_ns,
            adc_half_cycles_to_ns(s, s->rank_remaining_half_cycles));
        timer_mod(s->sample_timer, s->next_sample_ns);
        return;
    }
    s->rank_last_ns = now;
    if (!s->rank_remaining_half_cycles) {
        s->next_sample_ns = now;
        timer_mod(s->sample_timer, now);
        return;
    }
    s->next_sample_ns = adc_deadline_after(
        now, adc_half_cycles_to_ns(s, s->rank_remaining_half_cycles));
    timer_mod(s->sample_timer, s->next_sample_ns);
}

static void adc_schedule_injected(DmMc02Adc *s)
{
    uint64_t now;

    if (!s->injected_timer || !s->injected_conversion_active ||
        !s->clock_hz) {
        if (s->injected_timer && !s->clock_hz) {
            timer_del(s->injected_timer);
        }
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->injected_rank_remaining_half_cycles =
        adc_injected_rank_half_cycles(s, s->current_injected_rank);
    s->injected_rank_last_ns = now;
    s->injected_rank_clock_hz = s->clock_hz;
    s->next_injected_sample_ns = adc_deadline_after(
        now, adc_half_cycles_to_ns(s,
                                   s->injected_rank_remaining_half_cycles));
    timer_mod(s->injected_timer, s->next_injected_sample_ns);
}

static bool adc_start_automatic_injected_conversion(DmMc02Adc *s)
{
    uint32_t cr;

    if (!adc_automatic_injected_enabled(s) ||
        s->injected_conversion_active) {
        return false;
    }
    cr = adc_load(s, ADC_CR, 4);
    if (!(cr & ADC_CR_ADEN) || (cr & ADC_CR_ADCAL) ||
        s->calibration_active) {
        return false;
    }
    s->current_injected_rank = 0;
    s->injected_conversion_active = true;
    adc_store(s, ADC_CR, cr | ADC_CR_JADSTART, 4);
    adc_schedule_injected(s);
    return true;
}

static void adc_schedule_injected_next(DmMc02Adc *s)
{
    uint64_t now;
    uint64_t base;

    if (!s->injected_timer || !s->injected_conversion_active ||
        !s->clock_hz) {
        if (s->injected_timer && !s->clock_hz) {
            timer_del(s->injected_timer);
        }
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    base = s->next_injected_sample_ns > now ? s->next_injected_sample_ns : now;
    s->injected_rank_remaining_half_cycles =
        adc_injected_rank_half_cycles(s, s->current_injected_rank);
    s->injected_rank_last_ns = base;
    s->injected_rank_clock_hz = s->clock_hz;
    s->next_injected_sample_ns = adc_deadline_after(
        base, adc_half_cycles_to_ns(s,
                                   s->injected_rank_remaining_half_cycles));
    timer_mod(s->injected_timer, s->next_injected_sample_ns);
}

static bool adc_injected_finish_context(DmMc02Adc *s)
{
    uint32_t cfgr = adc_load(s, ADC_CFGR, 4);

    if (cfgr & ADC_CFGR_JQDIS) {
        return false;
    }
    if (s->injected_pending_context_valid) {
        s->injected_active_jsqr = s->injected_pending_jsqr;
        s->injected_active_context_valid = true;
        s->injected_pending_context_valid = false;
        s->injected_pending_jsqr = 0;
        adc_store(s, ADC_JSQR, s->injected_active_jsqr, 4);
        return true;
    }
    if (cfgr & ADC_CFGR_JQM) {
        /* End-empty mode keeps JADSTART armed, but removes the active
         * context so external triggers remain ignored until a new JSQR write. */
        s->injected_active_context_valid = false;
        s->injected_active_jsqr = 0;
        adc_store(s, ADC_JSQR, 0, 4);
        return true;
    }
    /* Last-active mode retains the snapshot, but the completed conversion
     * still needs a new JADSTART command before it can be triggered again. */
    return false;
}

static void adc_reschedule_injected_active(DmMc02Adc *s)
{
    uint64_t now;

    if (!s->injected_timer || !s->injected_conversion_active) {
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    adc_account_injected_rank(s, now);
    if (!s->clock_hz) {
        s->injected_rank_clock_hz = 0;
        if (s->injected_rank_last_ns <= now) {
            s->injected_rank_last_ns = now;
        }
        timer_del(s->injected_timer);
        return;
    }
    s->injected_rank_clock_hz = s->clock_hz;
    if (s->injected_rank_last_ns > now) {
        s->next_injected_sample_ns = adc_deadline_after(
            s->injected_rank_last_ns,
            adc_half_cycles_to_ns(s,
                                  s->injected_rank_remaining_half_cycles));
        timer_mod(s->injected_timer, s->next_injected_sample_ns);
        return;
    }
    s->injected_rank_last_ns = now;
    if (!s->injected_rank_remaining_half_cycles) {
        s->next_injected_sample_ns = now;
        timer_mod(s->injected_timer, now);
        return;
    }
    s->next_injected_sample_ns = adc_deadline_after(
        now, adc_half_cycles_to_ns(s,
                                   s->injected_rank_remaining_half_cycles));
    timer_mod(s->injected_timer, s->next_injected_sample_ns);
}

static void adc_emit_sample(DmMc02Adc *s, uint16_t sample)
{
    uint32_t isr;
    bool dma_blocked;
    bool overrun;

    isr = adc_load(s, ADC_ISR, 4);
    /* RM0468: once OVR is set, regular ADC DMA requests remain stopped
     * until software clears OVR.  Capture the entry state because the
     * current conversion may set OVR below, and because the common CDR
     * producer has a separate request path which must not be gated here. */
    dma_blocked = (isr & ADC_ISR_OVR) != 0;
    overrun = (isr & ADC_ISR_EOC) != 0;
    if (overrun) {
        /* A new regular result arrived while the previous EOC was still
         * pending.  Preserve or overwrite the data register according to
         * H723 CFGR.OVRMOD; trobot selects the reset/preserved behavior. */
        isr |= ADC_ISR_OVR;
    }
    /* DR is a 32-bit register, but the configured data path intentionally
     * emits half-word samples.  In preserved mode an overrun leaves the
     * previous result visible; overwrite mode exposes the newest sample. */
    if (!overrun || (adc_load(s, ADC_CFGR, 4) & ADC_CFGR_OVRMOD)) {
        adc_store(s, ADC_DR, sample, sizeof(sample));
    }
    isr |= ADC_ISR_EOC;
    adc_store(s, ADC_ISR, isr, 4);

    if (!dma_blocked && !overrun && adc_dma_configured(s) &&
        s->dma && s->dmamux) {
        /* Let a synchronous DMA DR read acknowledge EOC before presenting
         * the final level to NVIC.  This avoids a transient empty IRQ when
         * EOCIE is enabled alongside DMA. */
        if (s->dma_endpoint_enabled) {
            (void)dm_mc02_dma_request_endpoint(
                s->dma, s->dmamux, s->dma_request_id,
                s->dma_peripheral_addr, &s->dma_endpoint,
                qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
        } else {
            (void)dm_mc02_dma_request(s->dma, s->dmamux,
                                      s->dma_request_id,
                                      s->dma_peripheral_addr);
        }
    }
    adc_update_irq(s);
}

static void adc_continue_after_data_read(DmMc02Adc *s)
{
    uint32_t cr;
    uint32_t cfgr;
    bool start_next_sequence;

    if (!s->regular_waiting_for_data) {
        return;
    }
    s->regular_waiting_for_data = false;
    if (s->regular_discontinuous_pause_after_data) {
        /* The last rank of this external-trigger subgroup was held by
         * AUTDLY.  Reading DR releases the data boundary, but the next
         * subgroup still requires a new external edge. */
        s->regular_discontinuous_pause_after_data = false;
        s->regular_discontinuous_paused = true;
        s->regular_discontinuous_remaining = 0;
        s->conversion_active = false;
        s->next_sample_ns = 0;
        if (s->sample_timer) {
            timer_del(s->sample_timer);
        }
        return;
    }
    if (!s->conversion_active) {
        s->regular_sequence_complete = false;
        return;
    }

    cr = adc_load(s, ADC_CR, 4);
    cfgr = adc_load(s, ADC_CFGR, 4);
    start_next_sequence = s->regular_sequence_complete;
    s->regular_sequence_complete = false;
    if (!start_next_sequence) {
        /* The rank index was advanced when its data became available. */
        adc_schedule(s);
        return;
    }
    if (!(cr & ADC_CR_ADEN) || !(cr & ADC_CR_ADSTART) ||
        !(cfgr & ADC_CFGR_CONT)) {
        adc_stop_timer(s);
        return;
    }
    if (s->regular_waiting_for_auto_injected) {
        /* The automatic injected group owns the next conversion slot.  Its
         * completion callback will resume the regular sequence unless this
         * read already released only the AUTDLY boundary. */
        return;
    }
    adc_begin_regular_sequence(s);
    /* AUTDLY starts the next sequence from the data-register read. */
    adc_schedule(s);
}

/* Keep the endpoint and MMIO paths on one ADC_DR consumption implementation.
 * DMA must observe the same status and timing side effects as a guest read. */
static uint32_t adc_consume_regular_data(DmMc02Adc *s, hwaddr offset,
                                         unsigned size)
{
    uint32_t value = adc_load(s, offset, size);

    adc_store(s, ADC_ISR, adc_load(s, ADC_ISR, 4) & ~ADC_ISR_EOC, 4);
    adc_continue_after_data_read(s);
    adc_update_irq(s);
    return value;
}

void dm_mc02_adc_acknowledge_regular_data(DmMc02Adc *s)
{
    if (!s) {
        return;
    }
    adc_store(s, ADC_ISR, adc_load(s, ADC_ISR, 4) & ~ADC_ISR_EOC, 4);
    adc_continue_after_data_read(s);
    adc_update_irq(s);
}

static bool adc_dma_endpoint_read(void *opaque, uint8_t *data, unsigned size,
                                  uint64_t timestamp_ns)
{
    DmMc02Adc *s = opaque;
    uint32_t value;

    (void)timestamp_ns;
    if (!s || !data || (size != 1 && size != 2 && size != 4)) {
        return false;
    }
    value = adc_consume_regular_data(s, ADC_DR, size);
    for (unsigned i = 0; i < size; ++i) {
        data[i] = value >> (i * 8);
    }
    return true;
}

static DmMc02DmaEndpointResult adc_dma_endpoint_read_prepare(
    void *opaque, uint8_t *data, unsigned size, uint64_t timestamp_ns)
{
    DmMc02Adc *s = opaque;

    (void)timestamp_ns;
    if (!s || !data || (size != 1 && size != 2 && size != 4) ||
        s->dma_read_reserved ||
        !(adc_load(s, ADC_ISR, sizeof(uint32_t)) & ADC_ISR_EOC)) {
        return DM_MC02_DMA_ENDPOINT_ERROR;
    }
    s->dma_read_reserved = true;
    s->dma_read_reserved_value = adc_load(s, ADC_DR, size);
    s->dma_read_reserved_size = size;
    for (unsigned i = 0; i < size; ++i) {
        data[i] = s->dma_read_reserved_value >> (i * 8);
    }
    return DM_MC02_DMA_ENDPOINT_ACCEPTED;
}

static void adc_dma_endpoint_read_commit(void *opaque)
{
    DmMc02Adc *s = opaque;

    if (!s || !s->dma_read_reserved) {
        return;
    }
    /* The reservation is synchronous; no conversion can replace ADC_DR
     * between prepare and commit.  Reuse the guest-visible read path so EOC,
     * AUTDLY and the IRQ projection have exactly one consumer. */
    (void)adc_consume_regular_data(s, ADC_DR, s->dma_read_reserved_size);
    s->dma_read_reserved = false;
    s->dma_read_reserved_value = 0;
    s->dma_read_reserved_size = 0;
}

static void adc_dma_endpoint_read_abort(void *opaque)
{
    DmMc02Adc *s = opaque;

    if (s) {
        /* prepare never acknowledges EOC; abort only releases the
         * reservation and leaves ADC_DR available for a later retry. */
        s->dma_read_reserved = false;
        s->dma_read_reserved_value = 0;
        s->dma_read_reserved_size = 0;
    }
}

static void adc_sample_tick(void *opaque)
{
    DmMc02Adc *s = opaque;
    uint32_t cr;
    uint32_t isr;
    unsigned sequence_length;
    bool discontinuous;
    uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (!s->conversion_active) {
        if (s->calibration_active) {
            adc_account_calibration(s, now);
            adc_finish_calibration(s);
        }
        return;
    }

    adc_account_rank(s, now);
    s->rank_remaining_half_cycles = 0;
    sequence_length = adc_sequence_length(s);
    if (s->current_rank >= sequence_length) {
        s->current_rank = 0;
    }
    {
        unsigned rank = s->current_rank;
        uint16_t value = adc_rank_value(s, rank);

        adc_emit_sample(s, value);
        adc_notify_regular_sample(s, rank, value, now);
    }
    discontinuous = adc_regular_discontinuous_external(s);
    if (discontinuous && s->regular_discontinuous_remaining) {
        --s->regular_discontinuous_remaining;
    }
    /* With AUTDLY set, DMA may have consumed DR synchronously above.  Only a
     * still-latched EOC puts the converter into the data-read wait state. */
    if ((adc_load(s, ADC_CFGR, 4) & ADC_CFGR_AUTDLY) &&
        (adc_load(s, ADC_ISR, 4) & ADC_ISR_EOC)) {
        if (s->current_rank + 1u < sequence_length) {
            ++s->current_rank;
            if (discontinuous && !s->regular_discontinuous_remaining) {
                s->regular_discontinuous_pause_after_data = true;
            }
            s->regular_waiting_for_data = true;
            timer_del(s->sample_timer);
            s->next_sample_ns = 0;
            adc_update_irq(s);
            return;
        }
        isr = adc_load(s, ADC_ISR, 4) | ADC_ISR_EOS;
        adc_store(s, ADC_ISR, isr, 4);
        adc_update_irq(s);
        s->regular_sequence_complete = true;
        if (adc_start_automatic_injected_conversion(s)) {
            s->regular_waiting_for_auto_injected =
                (adc_load(s, ADC_CFGR, 4) & ADC_CFGR_CONT) != 0;
        }
        cr = adc_load(s, ADC_CR, 4);
        if (!(cr & ADC_CR_ADEN) || !(cr & ADC_CR_ADSTART) ||
            !(adc_load(s, ADC_CFGR, 4) & ADC_CFGR_CONT)) {
            cr &= ~ADC_CR_ADSTART;
            adc_store(s, ADC_CR, cr, 4);
            adc_stop_timer(s);
            return;
        }
        s->regular_waiting_for_data = true;
        timer_del(s->sample_timer);
        s->next_sample_ns = 0;
        return;
    }
    if (s->current_rank + 1u < sequence_length) {
        ++s->current_rank;
        if (discontinuous && !s->regular_discontinuous_remaining) {
            /* A subgroup has completed.  Keep ADSTART armed for the next
             * matching edge, but do not retain a host timer between edges. */
            s->conversion_active = false;
            s->regular_discontinuous_paused = true;
            s->next_sample_ns = 0;
            timer_del(s->sample_timer);
            return;
        }
        adc_schedule_next(s);
        return;
    }

    isr = adc_load(s, ADC_ISR, 4) | ADC_ISR_EOS;
    adc_store(s, ADC_ISR, isr, 4);
    adc_update_irq(s);

    if (adc_start_automatic_injected_conversion(s)) {
        s->regular_waiting_for_auto_injected =
            (adc_load(s, ADC_CFGR, 4) & ADC_CFGR_CONT) != 0;
    }

    cr = adc_load(s, ADC_CR, 4);
    if (!(cr & ADC_CR_ADEN) || !(cr & ADC_CR_ADSTART)) {
        adc_stop_timer(s);
        return;
    }
    if (!(adc_load(s, ADC_CFGR, 4) & ADC_CFGR_CONT)) {
        /* A non-continuous start produces one selected regular sequence. */
        cr &= ~ADC_CR_ADSTART;
        adc_store(s, ADC_CR, cr, 4);
        adc_stop_timer(s);
        return;
    }
    adc_begin_regular_sequence(s);
    if (s->regular_waiting_for_auto_injected) {
        timer_del(s->sample_timer);
        s->next_sample_ns = 0;
        return;
    }
    /* Preserve the board's deterministic 1 kHz sequence cadence.  The
     * per-rank sampling time still controls spacing inside a sequence; this
     * floor avoids making a continuously enabled ADC dominate a QEMU run. */
    adc_schedule_after(s, s->accurate_timing ? adc_rank_period_ns(s, 0) :
                        MAX((uint64_t)ADC_SEQUENCE_MIN_INTERVAL_NS,
                            adc_rank_period_ns(s, 0)));
}

static void adc_injected_sample_tick(void *opaque)
{
    DmMc02Adc *s = opaque;
    uint32_t isr;
    uint32_t cr;
    unsigned sequence_length;
    unsigned rank;
    bool discontinuous;
    bool keep_jadstart;
    bool resume_regular;
    uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (!s->injected_conversion_active) {
        return;
    }

    adc_account_injected_rank(s, now);
    s->injected_rank_remaining_half_cycles = 0;
    sequence_length = adc_injected_sequence_length(s);
    discontinuous = adc_injected_discontinuous(s);
    if (s->current_injected_rank >= sequence_length) {
        s->current_injected_rank = 0;
    }
    rank = s->current_injected_rank;
    /* JDRx is a full-width data register; the model's configured 16-bit ADC
     * result is zero-extended so stale upper bits cannot leak between ranks. */
    adc_store(s, ADC_JDR1 + rank * sizeof(uint32_t),
              adc_injected_rank_value(s, rank), sizeof(uint32_t));
    isr = adc_load(s, ADC_ISR, 4) | ADC_ISR_JEOC;
    adc_store(s, ADC_ISR, isr, 4);
    adc_update_irq(s);

    if (rank + 1u < sequence_length) {
        s->current_injected_rank = rank + 1u;
        if (discontinuous) {
            /* JDISCEN subdivides an injected sequence at one rank.  Keep
             * JADSTART set so the next matching external edge resumes from
             * the saved rank; no trigger is queued while paused. */
            s->injected_conversion_active = false;
            s->injected_discontinuous_paused = true;
            s->next_injected_sample_ns = 0;
            timer_del(s->injected_timer);
            return;
        }
        adc_schedule_injected_next(s);
        return;
    }

    isr = adc_load(s, ADC_ISR, 4) | ADC_ISR_JEOS;
    adc_store(s, ADC_ISR, isr, 4);
    keep_jadstart = adc_injected_finish_context(s);
    resume_regular = s->regular_waiting_for_auto_injected;
    cr = adc_load(s, ADC_CR, 4);
    if (!keep_jadstart) {
        cr &= ~ADC_CR_JADSTART;
    }
    adc_store(s, ADC_CR, cr, 4);
    adc_stop_injected_timer(s);
    adc_update_irq(s);

    /* JAUTO serializes continuous regular conversion with its automatic
     * injected sequence.  AUTDLY may still hold regular conversion at the
     * DR boundary; its read path resumes it after the data is consumed. */
    if (resume_regular) {
        adc_resume_regular_after_auto_injected(s);
    }
}

static void adc_start_conversion(DmMc02Adc *s)
{
    uint32_t cr = adc_load(s, ADC_CR, 4);

    if (!(cr & ADC_CR_ADEN) || (cr & ADC_CR_ADCAL) ||
        s->calibration_active) {
        return;
    }
    /* EXTEN=0 is the software-start path.  For an external trigger, ADSTART
     * arms the converter and the timer callback starts it later. */
    if ((adc_load(s, ADC_CFGR, 4) & ADC_CFGR_EXTEN_MASK) == 0) {
        if (!s->conversion_active) {
            adc_begin_regular_sequence(s);
        }
        s->conversion_active = true;
        adc_schedule(s);
    }
}

static bool adc_injected_start_allowed(const DmMc02Adc *s)
{
    uint32_t cfgr = adc_load(s, ADC_CFGR, 4);
    uint32_t jsqr = adc_injected_context_register(s);

    /* With no external edge selected, H723 requires the injected context
     * queue to be disabled before a software JADSTART is accepted. */
    return ((jsqr & ADC_JSQR_JEXTEN_MASK) != 0) ||
           (cfgr & ADC_CFGR_JQDIS);
}

static void adc_start_injected_conversion(DmMc02Adc *s)
{
    uint32_t cr = adc_load(s, ADC_CR, 4);
    uint32_t cfgr = adc_load(s, ADC_CFGR, 4);

    if (!(cr & ADC_CR_ADEN) || (cr & ADC_CR_ADCAL) ||
        s->calibration_active || (cfgr & ADC_CFGR_JAUTO) ||
        !adc_injected_start_allowed(s)) {
        return;
    }
    if ((adc_injected_context_register(s) & ADC_JSQR_JEXTEN_MASK) != 0) {
        /* JADSTART arms an external injected trigger; the trigger bus starts
         * the rank timer after a matching edge arrives. */
        return;
    }
    s->current_injected_rank = 0;
    s->injected_conversion_active = true;
    adc_schedule_injected(s);
}

static bool adc_external_trigger_event(DmMc02Adc *s,
                                       const DmMc02TriggerEvent *event,
                                       uint64_t shared_conversion_id,
                                       uint64_t conversion_start_ns)
{
    uint32_t cfgr;
    uint32_t cr;
    unsigned exten;
    unsigned sequence_length;

    if (!s || !event) {
        return false;
    }
    cfgr = adc_load(s, ADC_CFGR, 4);
    cr = adc_load(s, ADC_CR, 4);
    exten = (cfgr & ADC_CFGR_EXTEN_MASK) >> ADC_CFGR_EXTEN_SHIFT;
    if (!exten ||
        ((cfgr & ADC_CFGR_EXTSEL_MASK) >> ADC_CFGR_EXTSEL_SHIFT) !=
            event->source_id ||
        !(cr & ADC_CR_ADEN) || !(cr & ADC_CR_ADSTART) ||
        (cr & ADC_CR_ADCAL) || s->calibration_active) {
        return false;
    }
    /* 01=rising, 10=falling, 11=both. */
    if ((exten == 1 && !event->rising) ||
        (exten == 2 && event->rising)) {
        return false;
    }
    if (s->conversion_active) {
        return false;
    }
    if (!shared_conversion_id && s->regular_trigger_request) {
        if (!s->regular_trigger_request(
                s->regular_trigger_request_opaque,
                s->regular_trigger_source, event->source_id, event->rising,
                event->event_count, event->timestamp_ns,
                &shared_conversion_id)) {
            return false;
        }
    }
    if (shared_conversion_id) {
        /* adc_begin_regular_sequence() increments before publishing the
         * active ID.  The common admission has already established the
         * shared conversion identity for this edge and for its peer. */
        s->regular_sequence_id = shared_conversion_id - 1;
        s->regular_shared_conversion = true;
    }
    /* A coalesced edge batch still arms one regular sequence.  Subsequent
     * edges in the same host callback cannot be queued by this compact ADC
     * model; exact per-edge delivery remains available with timer batching
     * disabled. */
    if (!adc_regular_discontinuous_external(s) ||
        !s->regular_discontinuous_paused) {
        adc_begin_regular_sequence(s);
    }
    sequence_length = adc_sequence_length(s);
    s->regular_discontinuous_remaining =
        adc_regular_discontinuous_external(s) ?
        MIN(adc_regular_discontinuous_count(s),
            sequence_length - MIN(s->current_rank, sequence_length)) : 0;
    s->regular_discontinuous_pause_after_data = false;
    s->regular_discontinuous_paused = false;
    s->conversion_active = true;
    if (!s->sample_timer || !s->clock_hz ||
        conversion_start_ns <= qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)) {
        adc_schedule(s);
    } else {
        adc_schedule_at(s, conversion_start_ns);
    }
    return true;
}

static bool adc_injected_external_trigger_event(
    DmMc02Adc *s, const DmMc02TriggerEvent *event)
{
    uint32_t jsqr;
    uint32_t cr;
    unsigned exten;
    unsigned injected_source;
    bool discontinuous;

    if (!s || !event) {
        return false;
    }
    jsqr = adc_injected_context_register(s);
    cr = adc_load(s, ADC_CR, 4);
    exten = (jsqr & ADC_JSQR_JEXTEN_MASK) >> ADC_JSQR_JEXTEN_SHIFT;
    switch (event->source_id) {
    case DM_MC02_TRIGGER_TIM2_TRGO:
        injected_source = 2;
        break;
    case DM_MC02_TRIGGER_TIM3_TRGO:
        injected_source = 12;
        break;
    case DM_MC02_TRIGGER_TIM8_TRGO:
        injected_source = 9;
        break;
    case DM_MC02_TRIGGER_TIM8_TRGO2:
        injected_source = 10;
        break;
    case DM_MC02_TRIGGER_TIM1_TRGO:
        injected_source = 0;
        break;
    case DM_MC02_TRIGGER_TIM1_TRGO2:
        injected_source = 8;
        break;
    case DM_MC02_TRIGGER_SOURCE_TIM3_CH4:
        injected_source = 4;
        break;
    default:
        injected_source = UINT_MAX;
        break;
    }
    if (!exten ||
        ((jsqr & ADC_JSQR_JEXTSEL_MASK) >> ADC_JSQR_JEXTSEL_SHIFT) !=
            injected_source ||
        !(cr & ADC_CR_ADEN) || !(cr & ADC_CR_JADSTART) ||
        !s->injected_active_context_valid ||
        (cr & ADC_CR_ADCAL) || s->calibration_active ||
        s->injected_conversion_active) {
        return false;
    }
    /* 01=rising, 10=falling, 11=both. */
    if ((exten == 1 && !event->rising) ||
        (exten == 2 && event->rising)) {
        return false;
    }

    discontinuous = adc_injected_discontinuous(s);
    if (!discontinuous || !s->injected_discontinuous_paused) {
        s->current_injected_rank = 0;
    }
    s->injected_conversion_active = true;
    s->injected_discontinuous_paused = false;
    if (!s->injected_timer || !s->clock_hz ||
        event->timestamp_ns <= qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)) {
        adc_schedule_injected(s);
    } else {
        uint64_t period = adc_half_cycles_to_ns(
            s, adc_injected_rank_half_cycles(s, s->current_injected_rank));

        s->injected_rank_remaining_half_cycles =
            adc_injected_rank_half_cycles(s, s->current_injected_rank);
        s->injected_rank_last_ns = event->timestamp_ns;
        s->injected_rank_clock_hz = s->clock_hz;
        s->next_injected_sample_ns =
            adc_deadline_after(event->timestamp_ns, period);
        timer_mod(s->injected_timer, s->next_injected_sample_ns);
    }
    return true;
}

bool dm_mc02_adc_external_trigger(DmMc02Adc *s, uint32_t source, bool rising)
{
    DmMc02TriggerEvent event = {
        .source_id = source,
        .rising = rising,
        .timestamp_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL),
    };

    bool regular = adc_external_trigger_event(s, &event, 0,
                                              event.timestamp_ns);
    bool injected = adc_injected_external_trigger_event(s, &event);

    return regular || injected;
}

bool dm_mc02_adc_external_trigger_with_id(
    DmMc02Adc *s, uint32_t source, bool rising, unsigned event_count,
    uint64_t timestamp_ns, uint64_t conversion_id)
{
    return conversion_id && dm_mc02_adc_external_trigger_with_id_at(
        s, source, rising, event_count, timestamp_ns, timestamp_ns,
        conversion_id);
}

bool dm_mc02_adc_external_trigger_with_id_at(
    DmMc02Adc *s, uint32_t source, bool rising, unsigned event_count,
    uint64_t timestamp_ns, uint64_t conversion_start_ns,
    uint64_t conversion_id)
{
    DmMc02TriggerEvent event = {
        .source_id = source,
        .rising = rising,
        .event_count = event_count ? event_count : 1,
        .timestamp_ns = timestamp_ns,
    };

    return s && conversion_id &&
           adc_external_trigger_event(s, &event, conversion_id,
                                       conversion_start_ns);
}

void dm_mc02_adc_trigger_sink(void *opaque,
                              const DmMc02TriggerEvent *event)
{
    (void)adc_external_trigger_event(opaque, event, 0,
                                     event ? event->timestamp_ns : 0);
    (void)adc_injected_external_trigger_event(opaque, event);
}

static uint64_t dm_mc02_adc_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Adc *s = opaque;

    if (size > 4 || offset >= DM_MC02_ADC_REGION_SIZE ||
        size > DM_MC02_ADC_REGION_SIZE - offset) {
        return 0;
    }
    if (offset == ADC_ISR && size == 4) {
        return adc_load(s, offset, size);
    }
    if (offset < ADC_JDR1 + ADC_JDR_COUNT * sizeof(uint32_t) &&
        offset + size > ADC_JDR1) {
        uint32_t value = adc_load(s, offset, size);

        /* H723 reading any injected data register acknowledges JEOC, while
         * JEOS remains latched until software clears ISR. */
        adc_store(s, ADC_ISR, adc_load(s, ADC_ISR, 4) & ~ADC_ISR_JEOC, 4);
        adc_update_irq(s);
        return value;
    }
    if (offset < ADC_DR + sizeof(uint32_t) &&
        offset + size > ADC_DR) {
        /* Reading the regular data register acknowledges the current EOC;
         * EOS/OVR are independent status flags and remain latched. */
        if (s->dma_read_reserved) {
            return adc_load(s, offset, size);
        }
        return adc_consume_regular_data(s, offset, size);
    }
    return adc_load(s, offset, size);
}

static void dm_mc02_adc_write(void *opaque, hwaddr offset, uint64_t value,
                              unsigned size)
{
    DmMc02Adc *s = opaque;
    uint32_t cr_write_mask = 0;
    uint32_t cr_write_value = 0;
    uint32_t old_cr;
    uint32_t cr;
    uint32_t commands;
    bool start_regular = false;
    bool stop_regular = false;
    bool start_injected = false;
    bool stop_injected = false;
    bool regular_start_accepted = true;
    bool regular_external_trigger;

    if (size > 4 || offset >= DM_MC02_ADC_REGION_SIZE ||
        size > DM_MC02_ADC_REGION_SIZE - offset) {
        return;
    }
    if (offset < ADC_ISR + sizeof(uint32_t) &&
        offset + size > ADC_ISR) {
        unsigned first = MAX(offset, (hwaddr)ADC_ISR);
        unsigned last = MIN(offset + size, (hwaddr)(ADC_ISR + 4));
        unsigned width = last - first;
        uint32_t clear = (uint32_t)(value >> ((first - offset) * 8));
        uint32_t mask = width == 4 ? UINT32_MAX :
                        ((UINT32_C(1) << (width * 8)) - 1u);

        /* ADC flags are cleared by writing one; support byte/half-word
         * accesses as well as the normal 32-bit HAL access. */
        clear = (clear & mask) << ((first - ADC_ISR) * 8);
        adc_store(s, ADC_ISR, adc_load(s, ADC_ISR, 4) & ~clear, 4);
        adc_update_irq(s);
        return;
    }
    if (!adc_range_overlaps(offset, size, ADC_CR)) {
        if (adc_range_overlaps(offset, size, ADC_CALFACT_RES13) ||
            adc_range_overlaps(offset, size, ADC_CALFACT2_RES14)) {
            /* H723 permits software replacement of calibration factors only
             * while the enabled ADC is idle.  Do not let a generic register
             * window turn a rejected write into persistent state. */
            if (!adc_calibration_factor_write_allowed(s)) {
                return;
            }
            adc_store(s, offset, value, size);
            adc_mask_calibration_registers(s);
            if (adc_range_overlaps(offset, size, ADC_CALFACT2_RES14)) {
                unsigned window = s->linear_calibration_window;
                uint32_t mask = window == 5 ? ADC_LINEAR_LAST_WORD_MASK :
                                ADC_LINEAR_WORD_MASK;

                s->linear_calibration_words[window] =
                    adc_load(s, ADC_CALFACT2_RES14, 4) & mask;
            }
            return;
        }
        if (offset == ADC_JSQR && size == sizeof(uint32_t)) {
            /* A full JSQR write is the H723 context admission boundary. */
            adc_injected_context_commit(s, (uint32_t)value);
            return;
        }
        if (adc_range_overlaps(offset, size, ADC_JSQR) &&
            !(adc_load(s, ADC_CFGR, 4) & ADC_CFGR_JQDIS)) {
            /* Do not turn partial writes into an implicit queue commit. */
            return;
        }
        if (adc_range_overlaps(offset, size, ADC_SQR1) ||
            adc_range_overlaps(offset, size, ADC_SQR2) ||
            adc_range_overlaps(offset, size, ADC_SQR3) ||
            adc_range_overlaps(offset, size, ADC_SQR4)) {
            /* A real SQR1..SQR4 write selects the regular sequence.  This
             * also handles byte/half-word MMIO writes without special cases. */
            s->legacy_samples = false;
        }
        if (adc_range_overlaps(offset, size, ADC_CFGR)) {
            uint32_t old_cfgr = adc_load(s, ADC_CFGR, 4);

            adc_store(s, offset, value, size);
            if ((old_cfgr ^ adc_load(s, ADC_CFGR, 4)) & ADC_CFGR_JQDIS) {
                if (adc_regular_conversion_ongoing(s) ||
                    adc_injected_conversion_ongoing(s)) {
                    uint32_t cfgr = adc_load(s, ADC_CFGR, 4);

                    if (old_cfgr & ADC_CFGR_JQDIS) {
                        cfgr |= ADC_CFGR_JQDIS;
                    } else {
                        cfgr &= ~ADC_CFGR_JQDIS;
                    }
                    adc_store(s, ADC_CFGR, cfgr, 4);
                } else {
                    adc_injected_context_flush(s);
                }
            }
        } else {
            adc_store(s, offset, value, size);
        }
        if (offset <= ADC_IER && ADC_IER < offset + size) {
            adc_update_irq(s);
        }
        return;
    }

    /* MMIO accesses to a register may be byte or half-word wide.  Merge only
     * the lanes actually written so that a write to CR[31:16] cannot replay
     * a stale command from CR[15:0]. */
    for (unsigned byte = 0; byte < sizeof(uint32_t); ++byte) {
        hwaddr register_byte = ADC_CR + byte;

        if (register_byte >= offset && register_byte < offset + size) {
            unsigned source_shift = (register_byte - offset) * 8;

            cr_write_mask |= UINT32_C(0xff) << (byte * 8);
            cr_write_value |= ((uint32_t)(value >> source_shift) & 0xffu) <<
                              (byte * 8);
        }
    }
    old_cr = adc_load(s, ADC_CR, 4);
    cr = (old_cr & ~cr_write_mask) | (cr_write_value & cr_write_mask);
    commands = cr_write_value & cr_write_mask;

    /* Power-control bits are ordinary read/write state.  Apply their
     * analogue-side effect before command bits are interpreted, so an ADEN
     * written in the same transaction as ADVREGEN is accepted only when the
     * regulator was already ready. */
    adc_apply_power_control(s, old_cr, &cr);

    if (s->calibration_active) {
        /* ADCAL is a self-clearing operation.  While it is active, writes to
         * the command and calibration-mode bits cannot enable or start the
         * converter, nor can they cancel the in-flight operation. */
        cr &= ~(ADC_CR_ADEN | ADC_CR_ADDIS | ADC_CR_ADSTART |
                ADC_CR_JADSTART | ADC_CR_ADSTP | ADC_CR_JADSTP |
                ADC_CR_ADCAL | ADC_CR_ADCALLIN | ADC_CR_ADCALDIF);
        cr |= old_cr & (ADC_CR_ADCAL | ADC_CR_ADCALLIN | ADC_CR_ADCALDIF);
        adc_store(s, ADC_CR, cr, 4);
        return;
    }

    if (commands & ADC_CR_ADCAL) {
        /* Calibration is accepted only while the ADC is disabled and the
         * regular group is idle.  ADCALDIF selects the offset mode; ADCALLIN
         * selects the linearity part of the operation. */
        if (!(old_cr & ADC_CR_ADEN) && adc_power_ready(s) &&
            !(commands & ADC_CR_ADEN) &&
            !(old_cr & (ADC_CR_ADSTART | ADC_CR_JADSTART)) &&
            !s->conversion_active) {
            cr &= ~(ADC_CR_ADEN | ADC_CR_ADSTART | ADC_CR_JADSTART |
                    ADC_CR_ADSTP | ADC_CR_JADSTP);
            cr |= ADC_CR_ADCAL;
            if (cr & ADC_CR_ADCALLIN) {
                cr &= ~ADC_CR_LINCALRDY_MASK;
            }
            s->calibration_active = true;
            s->calibration_cycles = (cr & ADC_CR_ADCALLIN) ?
                                     ADC_CALIBRATION_LINEAR_CYCLES :
                                     ADC_CALIBRATION_OFFSET_CYCLES;
            s->calibration_remaining_cycles = s->calibration_cycles;
            s->calibration_last_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
            s->calibration_clock_hz = s->clock_hz;
            adc_schedule_calibration(s);
        } else {
            cr &= ~ADC_CR_ADCAL;
        }
    }

    /* ADSTART and JADSTART are read/set command/status bits: a zero in a
     * normal write does not clear a state which hardware has already set.
     * ADDIS and the corresponding stop command are handled below. */
    if (!(commands & (ADC_CR_ADDIS | ADC_CR_ADSTP))) {
        cr |= old_cr & ADC_CR_ADSTART;
    }
    if (!(commands & (ADC_CR_ADDIS | ADC_CR_JADSTP))) {
        cr |= old_cr & ADC_CR_JADSTART;
    }

    /* ADEN, ADDIS, ADSTART and ADSTP are command-like bits on STM32H7.
     * Keep ADEN latched until ADDIS, while retaining ordinary read/write
     * behavior for the rest of this minimal register window. */
    if ((old_cr & ADC_CR_ADEN) && !(commands & ADC_CR_ADDIS) &&
        adc_power_ready(s)) {
        cr |= ADC_CR_ADEN;
    }
    if (commands & ADC_CR_ADEN) {
        if (adc_power_ready(s)) {
            cr |= ADC_CR_ADEN;
            adc_store(s, ADC_ISR,
                      adc_load(s, ADC_ISR, 4) | ADC_ISR_ADRDY, 4);
        } else {
            cr &= ~(ADC_CR_ADEN | ADC_CR_ADSTART | ADC_CR_JADSTART);
        }
    }
    if (commands & ADC_CR_ADDIS) {
        cr &= ~(ADC_CR_ADEN | ADC_CR_ADSTART | ADC_CR_JADSTART |
                ADC_CR_ADDIS);
        adc_store(s, ADC_ISR,
                  adc_load(s, ADC_ISR, 4) & ~ADC_ISR_ADRDY, 4);
        adc_stop_timer(s);
        adc_stop_injected_timer(s);
    }
    if (commands & ADC_CR_ADSTP) {
        cr &= ~(ADC_CR_ADSTART | ADC_CR_ADSTP);
        stop_regular = true;
    }
    if (commands & ADC_CR_JADSTP) {
        cr &= ~(ADC_CR_JADSTART | ADC_CR_JADSTP);
        stop_injected = true;
    }
    if (!(cr & ADC_CR_ADEN)) {
        cr &= ~(ADC_CR_ADSTART | ADC_CR_JADSTART);
        if (!s->calibration_active) {
            stop_regular = true;
            stop_injected = true;
        }
    } else {
        /* Regular and injected starts are separate group commands.  The
         * groups have independent timers, status flags, and stop commands. */
        if ((commands & ADC_CR_JADSTART) &&
            (adc_load(s, ADC_CFGR, 4) & ADC_CFGR_JAUTO)) {
            /* In automatic injected mode the regular group owns the
             * injected start/stop boundary; a direct JADSTART is ignored. */
            cr &= ~ADC_CR_JADSTART;
        } else if ((commands & ADC_CR_JADSTART) &&
            !(commands & ADC_CR_JADSTP) &&
            !(old_cr & ADC_CR_JADSTART) &&
            !(cr & ADC_CR_ADCAL) && adc_injected_start_allowed(s)) {
            cr |= ADC_CR_JADSTART;
            start_injected = true;
        } else if ((commands & ADC_CR_JADSTART) &&
                   !(commands & ADC_CR_JADSTP) &&
                   !adc_injected_start_allowed(s)) {
            /* A software injected start with an enabled context queue is a
             * configuration error on H723; do not leave a phantom busy bit. */
            cr &= ~ADC_CR_JADSTART;
        }
        if ((commands & ADC_CR_ADSTART) &&
            !(old_cr & ADC_CR_ADSTART) &&
            !s->calibration_active &&
            !(cr & ADC_CR_ADCAL)) {
            cr |= ADC_CR_ADSTART;
            start_regular = true;
        }
    }
    if (stop_regular) {
        adc_stop_timer(s);
    }
    if (stop_injected) {
        adc_stop_injected_timer(s);
    }
    adc_process_linear_window_write(s, old_cr, &cr, cr_write_mask);
    adc_store(s, ADC_CR, cr, 4);
    if (stop_injected) {
        /* JADSTP also aborts an automatic injected group.  It must release
         * the regular group which JAUTO paused, otherwise a continuous ADC
         * can remain permanently armed but idle after the abort. */
        adc_resume_regular_after_auto_injected(s);
    }
    if (start_regular) {
        regular_external_trigger =
            (adc_load(s, ADC_CFGR, 4) & ADC_CFGR_EXTEN_MASK) != 0;
        if (s->regular_start_request && !regular_external_trigger) {
            regular_start_accepted = s->regular_start_request(
                s->regular_start_request_opaque, s->regular_start_source);
        }
        if (regular_start_accepted) {
            adc_start_conversion(s);
        } else {
            cr = adc_load(s, ADC_CR, 4) & ~ADC_CR_ADSTART;
            adc_store(s, ADC_CR, cr, 4);
        }
    }
    if (start_injected) {
        adc_start_injected_conversion(s);
    }
}

static const MemoryRegionOps dm_mc02_adc_ops = {
    .read = dm_mc02_adc_read,
    .write = dm_mc02_adc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_adc_init(DmMc02Adc *state, Object *owner, const char *name)
{
    memset(state, 0, sizeof(*state));
    state->dma_endpoint.read = adc_dma_endpoint_read;
    state->dma_endpoint.read_prepare = adc_dma_endpoint_read_prepare;
    state->dma_endpoint.read_commit = adc_dma_endpoint_read_commit;
    state->dma_endpoint.read_abort = adc_dma_endpoint_read_abort;
    state->dma_endpoint.opaque = state;
    state->dma_endpoint_enabled = true;
    state->sample_values[0] = 0x0100;
    state->sample_values[1] = 0x0200;
    state->legacy_samples = true;
    state->clock_hz = ADC_CLOCK_HZ;
    state->sample_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                       adc_sample_tick, state);
    state->injected_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                         adc_injected_sample_tick, state);
    state->regulator_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                          adc_regulator_tick, state);
    memory_region_init_io(&state->iomem, owner, &dm_mc02_adc_ops, state, name,
                          DM_MC02_ADC_REGION_SIZE);
}

void dm_mc02_adc_reset(DmMc02Adc *s)
{
    DmMc02Adc *adc = s;
    DmMc02Dma *dma;
    const DmMc02Dmamux *dmamux;
    uint32_t request_id;
    hwaddr peripheral_addr;
    qemu_irq irq;
    DmMc02AdcRegularSampleReady regular_sample_ready;
    void *regular_sample_ready_opaque;
    unsigned regular_sample_source;
    DmMc02AdcRegularStartRequest regular_start_request;
    void *regular_start_request_opaque;
    unsigned regular_start_source;
    DmMc02AdcRegularExternalTriggerRequest regular_trigger_request;
    void *regular_trigger_request_opaque;
    unsigned regular_trigger_source;
    bool legacy;
    uint64_t clock_hz;
    bool accurate_timing;
    uint16_t samples[2];
    uint16_t board[DM_MC02_ADC_CHANNEL_COUNT];
    uint16_t external[DM_MC02_ADC_CHANNEL_COUNT];
    uint32_t board_valid, external_valid, pin_valid;
    uint8_t kinds[DM_MC02_ADC_CHANNEL_COUNT];
    bool power_model;
    bool dma_endpoint_enabled;

    if (!adc) {
        return;
    }
    /* Preserve board wiring and injected source configuration; reset only
     * converter state and status, as a peripheral reset does. */
    dma = adc->dma;
    dmamux = adc->dmamux;
    request_id = adc->dma_request_id;
    peripheral_addr = adc->dma_peripheral_addr;
    irq = adc->irq;
    regular_sample_ready = adc->regular_sample_ready;
    regular_sample_ready_opaque = adc->regular_sample_ready_opaque;
    regular_sample_source = adc->regular_sample_source;
    regular_start_request = adc->regular_start_request;
    regular_start_request_opaque = adc->regular_start_request_opaque;
    regular_start_source = adc->regular_start_source;
    regular_trigger_request = adc->regular_trigger_request;
    regular_trigger_request_opaque = adc->regular_trigger_request_opaque;
    regular_trigger_source = adc->regular_trigger_source;
    legacy = adc->legacy_samples;
    clock_hz = adc->clock_hz;
    accurate_timing = adc->accurate_timing;
    power_model = adc->power_model;
    dma_endpoint_enabled = adc->dma_endpoint_enabled;
    memcpy(samples, adc->sample_values, sizeof(samples));
    memcpy(board, adc->board_source_values, sizeof(board));
    memcpy(external, adc->external_values, sizeof(external));
    memcpy(kinds, adc->external_override_kind, sizeof(kinds));
    board_valid = adc->board_source_valid;
    external_valid = adc->external_raw_valid;
    pin_valid = adc->external_pin_voltage_valid;
    memset(adc->regs, 0, sizeof(adc->regs));
    adc->dma = dma;
    adc->dmamux = dmamux;
    adc->dma_request_id = request_id;
    adc->dma_peripheral_addr = peripheral_addr;
    adc->irq = irq;
    adc->regular_sample_ready = regular_sample_ready;
    adc->regular_sample_ready_opaque = regular_sample_ready_opaque;
    adc->regular_sample_source = regular_sample_source;
    adc->regular_start_request = regular_start_request;
    adc->regular_start_request_opaque = regular_start_request_opaque;
    adc->regular_start_source = regular_start_source;
    adc->regular_trigger_request = regular_trigger_request;
    adc->regular_trigger_request_opaque = regular_trigger_request_opaque;
    adc->regular_trigger_source = regular_trigger_source;
    adc->irq_level = false;
    adc->irq_level_valid = false;
    adc->legacy_samples = legacy;
    adc->clock_hz = clock_hz;
    adc->accurate_timing = accurate_timing;
    adc->power_model = power_model;
    adc->dma_endpoint_enabled = dma_endpoint_enabled;
    adc->dma_read_reserved = false;
    adc->dma_read_reserved_value = 0;
    adc->dma_read_reserved_size = 0;
    memcpy(adc->sample_values, samples, sizeof(samples));
    memcpy(adc->board_source_values, board, sizeof(board));
    memcpy(adc->external_values, external, sizeof(external));
    memcpy(adc->external_override_kind, kinds, sizeof(kinds));
    adc->board_source_valid = board_valid;
    adc->external_raw_valid = external_valid;
    adc->external_pin_voltage_valid = pin_valid;
    adc->injected_active_context_valid = false;
    adc->injected_pending_context_valid = false;
    adc->injected_active_jsqr = 0;
    adc->injected_pending_jsqr = 0;
    adc->regular_sequence_id = 0;
    adc->regular_active_sequence_id = 0;
    adc->regular_shared_conversion = false;
    if (power_model) {
        adc_store(adc, ADC_CR, ADC_CR_DEEPPWD, sizeof(uint32_t));
    }
    adc->regulator_ready = false;
    adc->regulator_ready_ns = 0;
    memset(adc->linear_calibration_words, 0,
           sizeof(adc->linear_calibration_words));
    adc->linear_calibration_window = 0;
    adc_stop_timer(adc);
    adc_stop_injected_timer(adc);
    adc_stop_regulator_timer(adc);
    adc_update_irq(adc);
}

void dm_mc02_adc_set_clock_hz(DmMc02Adc *state, uint64_t clock_hz)
{
    uint64_t now;

    if (!state || state->clock_hz == clock_hz) {
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (state->calibration_active) {
        /* Account for the old frequency before replacing it.  A paused
         * kernel clock contributes no cycles, and restoring it resumes from
         * the remaining calibration work. */
        adc_account_calibration(state, now);
    }
    state->clock_hz = clock_hz;
    if (state->calibration_active) {
        state->calibration_clock_hz = clock_hz;
        if (!state->calibration_remaining_cycles) {
            adc_finish_calibration(state);
        } else {
            adc_schedule_calibration(state);
        }
    } else if (state->conversion_active) {
        /* adc_reschedule_active accounts the rank using the old clock before
         * scheduling its remaining half-cycles at the new frequency. */
        adc_reschedule_active(state);
    }
    if (state->injected_conversion_active) {
        adc_reschedule_injected_active(state);
    }
}

void dm_mc02_adc_set_accurate_timing(DmMc02Adc *state, bool enabled)
{
    if (!state) {
        return;
    }
    state->accurate_timing = enabled;
    if (state->conversion_active) {
        adc_reschedule_active(state);
    }
}

void dm_mc02_adc_set_power_model(DmMc02Adc *state, bool enabled)
{
    uint32_t cr;

    if (!state || state->power_model == enabled) {
        return;
    }
    state->power_model = enabled;
    if (!enabled) {
        adc_stop_regulator_timer(state);
        return;
    }
    cr = adc_load(state, ADC_CR, sizeof(uint32_t));
    cr |= ADC_CR_DEEPPWD;
    cr &= ~(ADC_CR_ADVREGEN | ADC_CR_ADEN | ADC_CR_ADSTART |
            ADC_CR_JADSTART);
    adc_store(state, ADC_CR, cr, sizeof(uint32_t));
    adc_stop_regulator_timer(state);
    adc_stop_timer(state);
    adc_stop_injected_timer(state);
    adc_store(state, ADC_ISR,
              adc_load(state, ADC_ISR, sizeof(uint32_t)) &
                  ~ADC_ISR_ADRDY,
              sizeof(uint32_t));
    adc_update_irq(state);
}

void dm_mc02_adc_set_irq(DmMc02Adc *state, qemu_irq irq)
{
    state->irq = irq;
    state->irq_level_valid = false;
    adc_update_irq(state);
}

void dm_mc02_adc_set_regular_sample_callback(
    DmMc02Adc *state, unsigned adc_index, DmMc02AdcRegularSampleReady callback,
    void *opaque)
{
    if (!state) {
        return;
    }
    state->regular_sample_ready = callback;
    state->regular_sample_ready_opaque = opaque;
    state->regular_sample_source = adc_index;
}

void dm_mc02_adc_set_regular_start_callback(
    DmMc02Adc *state, unsigned adc_index,
    DmMc02AdcRegularStartRequest callback, void *opaque)
{
    if (!state) {
        return;
    }
    state->regular_start_request = callback;
    state->regular_start_request_opaque = opaque;
    state->regular_start_source = adc_index;
}

void dm_mc02_adc_set_regular_trigger_callback(
    DmMc02Adc *state, unsigned adc_index,
    DmMc02AdcRegularExternalTriggerRequest callback, void *opaque)
{
    if (!state) {
        return;
    }
    state->regular_trigger_request = callback;
    state->regular_trigger_request_opaque = opaque;
    state->regular_trigger_source = adc_index;
}

void dm_mc02_adc_set_dma(DmMc02Adc *state, DmMc02Dma *dma,
                         const DmMc02Dmamux *dmamux, uint32_t request_id,
                         hwaddr peripheral_addr)
{
    state->dma = dma;
    state->dmamux = dmamux;
    state->dma_request_id = request_id;
    state->dma_peripheral_addr = peripheral_addr;
}

void dm_mc02_adc_set_dma_endpoint(DmMc02Adc *state, bool enabled)
{
    if (!state) {
        return;
    }
    state->dma_endpoint_enabled = enabled;
}

void dm_mc02_adc_dma_stream_enabled(DmMc02Adc *state)
{
    if (!state || !state->dma || !state->dmamux ||
        !state->dma_endpoint_enabled || !adc_dma_configured(state) ||
        !dm_mc02_dma_endpoint_read_reservation_supported(
            &state->dma_endpoint) ||
        !(adc_load(state, ADC_ISR, sizeof(uint32_t)) & ADC_ISR_EOC)) {
        return;
    }

    /* ADC_DR is a level-like source while EOC is pending.  A failed P2M
     * destination write leaves the source reservation aborted and the EOC
     * bit set, so enabling a stream is a safe retry point even when no new
     * conversion event is generated.  DMA remains responsible for selecting
     * the stream, matching DMAMUX/PAR and reporting the transfer result. */
    (void)dm_mc02_dma_request_endpoint(
        state->dma, state->dmamux, state->dma_request_id,
        state->dma_peripheral_addr, &state->dma_endpoint,
        qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}

void dm_mc02_adc_set_samples(DmMc02Adc *state, uint16_t first,
                             uint16_t second)
{
    state->sample_values[0] = first;
    state->sample_values[1] = second;
    state->legacy_samples = true;
}

void dm_mc02_adc_set_channel_raw(DmMc02Adc *state, uint16_t channel,
                                 uint16_t raw)
{
    if (channel >= DM_MC02_ADC_CHANNEL_COUNT) {
        return;
    }
    state->external_values[channel] = raw;
    state->external_raw_valid |= UINT32_C(1) << channel;
    state->external_pin_voltage_valid &= ~(UINT32_C(1) << channel);
    state->external_override_kind[channel] = ADC_EXTERNAL_OVERRIDE_RAW;
}

void dm_mc02_adc_set_channel_pin_voltage_uv(DmMc02Adc *state,
                                            uint16_t channel,
                                            uint32_t voltage_uv)
{
    if (channel >= DM_MC02_ADC_CHANNEL_COUNT) {
        return;
    }
    state->external_values[channel] = adc_pin_voltage_to_raw(voltage_uv);
    state->external_pin_voltage_valid |= UINT32_C(1) << channel;
    state->external_raw_valid &= ~(UINT32_C(1) << channel);
    state->external_override_kind[channel] =
        ADC_EXTERNAL_OVERRIDE_PIN_VOLTAGE;
}

void dm_mc02_adc_clear_channel_override(DmMc02Adc *state, uint16_t channel)
{
    if (channel >= DM_MC02_ADC_CHANNEL_COUNT) {
        return;
    }
    state->external_raw_valid &= ~(UINT32_C(1) << channel);
    state->external_pin_voltage_valid &= ~(UINT32_C(1) << channel);
    state->external_override_kind[channel] = ADC_EXTERNAL_OVERRIDE_NONE;
}

bool dm_mc02_adc_channel_has_override(const DmMc02Adc *state,
                                      uint16_t channel)
{
    return channel < DM_MC02_ADC_CHANNEL_COUNT &&
           state->external_override_kind[channel] !=
               ADC_EXTERNAL_OVERRIDE_NONE;
}

void dm_mc02_adc_set_board_source_raw(DmMc02Adc *state, uint16_t channel,
                                      uint16_t raw)
{
    if (channel >= DM_MC02_ADC_CHANNEL_COUNT) {
        return;
    }
    state->board_source_values[channel] = raw;
    state->board_source_valid |= UINT32_C(1) << channel;
}

void dm_mc02_adc_set_board_source_pin_voltage_uv(DmMc02Adc *state,
                                                 uint16_t channel,
                                                 uint32_t voltage_uv)
{
    dm_mc02_adc_set_board_source_raw(state, channel,
                                     adc_pin_voltage_to_raw(voltage_uv));
}

void dm_mc02_adc_clear_board_source(DmMc02Adc *state, uint16_t channel)
{
    if (channel >= DM_MC02_ADC_CHANNEL_COUNT) {
        return;
    }
    state->board_source_valid &= ~(UINT32_C(1) << channel);
}

void dm_mc02_adc_start(DmMc02Adc *state)
{
    uint32_t cr = adc_load(state, ADC_CR, 4);

    cr |= ADC_CR_ADEN | ADC_CR_ADSTART;
    adc_store(state, ADC_CR, cr, 4);
    adc_store(state, ADC_ISR, adc_load(state, ADC_ISR, 4) | ADC_ISR_ADRDY,
              4);
    adc_start_conversion(state);
}

bool dm_mc02_adc_start_regular_at(DmMc02Adc *state, uint64_t conversion_id,
                                  uint64_t start_ns)
{
    uint32_t cr;
    uint64_t now;

    if (!state || !conversion_id) {
        return false;
    }
    cr = adc_load(state, ADC_CR, 4);
    if (!(cr & ADC_CR_ADEN) || (cr & ADC_CR_ADCAL) ||
        state->calibration_active || state->conversion_active ||
        (adc_load(state, ADC_CFGR, 4) & ADC_CFGR_EXTEN_MASK)) {
        return false;
    }

    /* This is the peer half of a common-owned start.  It deliberately does
     * not call the ADC-local start admission callback a second time. */
    dm_mc02_adc_set_regular_conversion_id(state, conversion_id);
    cr |= ADC_CR_ADSTART;
    adc_store(state, ADC_CR, cr, 4);
    adc_store(state, ADC_ISR, adc_load(state, ADC_ISR, 4) | ADC_ISR_ADRDY,
              4);
    adc_begin_regular_sequence(state);
    state->conversion_active = true;
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (start_ns <= now) {
        adc_schedule(state);
    } else {
        adc_schedule_at(state, start_ns);
    }
    return true;
}

void dm_mc02_adc_stop(DmMc02Adc *state)
{
    uint32_t cr = adc_load(state, ADC_CR, 4);

    cr &= ~(ADC_CR_ADEN | ADC_CR_ADSTART);
    adc_store(state, ADC_CR, cr, 4);
    adc_store(state, ADC_ISR,
              adc_load(state, ADC_ISR, 4) & ~ADC_ISR_ADRDY, 4);
    adc_stop_timer(state);
    adc_stop_injected_timer(state);
}

bool dm_mc02_adc_enabled(const DmMc02Adc *state)
{
    return state && (adc_load(state, ADC_CR, 4) & ADC_CR_ADEN);
}

bool dm_mc02_adc_regular_dma_enabled(const DmMc02Adc *state)
{
    return state && adc_dma_configured(state);
}

void dm_mc02_adc_set_regular_conversion_id(DmMc02Adc *state,
                                           uint64_t conversion_id)
{
    uint32_t cfgr;

    if (!state || !conversion_id) {
        return;
    }
    cfgr = adc_load(state, ADC_CFGR, 4);
    state->regular_shared_conversion =
        (cfgr & ADC_CFGR_EXTEN_MASK) == 0;
    if (state->regular_shared_conversion) {
        /* adc_begin_regular_sequence() increments before publishing the
         * active ID.  Seeding the local counter this way lets both ADCs use
         * the common-generated ID and then advance together in CONT mode. */
        state->regular_sequence_id = conversion_id - 1;
    }
}

uint32_t dm_mc02_adc_status(const DmMc02Adc *state)
{
    if (!state) {
        return 0;
    }
    /* H723 places the modeled ADCx.ISR flags at the same positions in the
     * master/slave halves of ADC12_COMMON.CSR. */
    return adc_load(state, ADC_ISR, 4) &
           (ADC_ISR_ADRDY | ADC_ISR_EOC | ADC_ISR_EOS | ADC_ISR_OVR |
            ADC_ISR_JEOC | ADC_ISR_JEOS | ADC_ISR_JQOVF);
}

static void adc_rearm_deadline(QEMUTimer *timer, uint64_t *deadline,
                               uint64_t now)
{
    if (!timer || !deadline || !*deadline) {
        if (timer) {
            timer_del(timer);
        }
        return;
    }
    if (*deadline < now) {
        *deadline = now;
    }
    timer_mod(timer, *deadline);
}

static void adc_rearm_remaining_rank(DmMc02Adc *s, QEMUTimer *timer,
                                     uint64_t *deadline, uint64_t anchor_ns,
                                     uint64_t remaining_half_cycles,
                                     uint64_t now)
{
    uint64_t base;
    uint64_t delay_ns;

    if (!timer || !deadline || !s->clock_hz) {
        if (timer) {
            timer_del(timer);
        }
        if (deadline) {
            *deadline = 0;
        }
        return;
    }

    /* A zero deadline is the serialized representation of a rank paused by
     * a stopped kernel clock.  The remaining half-cycles, rather than the
     * stale source-clock deadline, are the authoritative progress state. */
    base = anchor_ns > now ? anchor_ns : now;
    delay_ns = adc_half_cycles_to_ns(s, remaining_half_cycles);
    if (delay_ns == UINT64_MAX) {
        timer_del(timer);
        *deadline = 0;
        return;
    }
    *deadline = adc_deadline_after(base, delay_ns);
    timer_mod(timer, *deadline);
}

static void adc_rearm_remaining_calibration(DmMc02Adc *s, uint64_t now)
{
    uint64_t base;
    uint64_t delay_ns;

    if (!s->sample_timer || !s->clock_hz ||
        !s->calibration_remaining_cycles) {
        if (s->sample_timer) {
            timer_del(s->sample_timer);
        }
        s->next_sample_ns = 0;
        return;
    }

    /* As with a rank, a zero deadline means the old clock was stopped. */
    base = s->calibration_last_ns > now ? s->calibration_last_ns : now;
    delay_ns = adc_cycles_to_ns(s, s->calibration_remaining_cycles);
    if (delay_ns == UINT64_MAX) {
        timer_del(s->sample_timer);
        s->next_sample_ns = 0;
        return;
    }
    s->next_sample_ns = adc_deadline_after(base, delay_ns);
    timer_mod(s->sample_timer, s->next_sample_ns);
}

void dm_mc02_adc_sync_runtime(DmMc02Adc *s)
{
    uint64_t now;

    if (!s) {
        return;
    }
    if (s->sample_timer) {
        timer_del(s->sample_timer);
    }
    if (s->injected_timer) {
        timer_del(s->injected_timer);
    }
    if (s->regulator_timer) {
        timer_del(s->regulator_timer);
    }
    s->irq_level_valid = false;
    adc_update_irq(s);
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (s->power_model && !s->regulator_ready) {
        adc_rearm_deadline(s->regulator_timer, &s->regulator_ready_ns, now);
    } else {
        s->regulator_ready_ns = s->regulator_ready ?
            s->regulator_ready_ns : 0;
    }

    /* A single sample timer is shared by calibration and regular conversion;
     * validation guarantees that these states are mutually exclusive. */
    if (s->calibration_active) {
        if (!s->clock_hz) {
            s->next_sample_ns = 0;
            s->calibration_clock_hz = 0;
            s->calibration_last_ns = now;
        } else if (!s->next_sample_ns) {
            s->calibration_clock_hz = s->clock_hz;
            adc_rearm_remaining_calibration(s, now);
        } else {
            adc_rearm_deadline(s->sample_timer, &s->next_sample_ns, now);
        }
    } else if (s->conversion_active &&
               !s->regular_waiting_for_data &&
               !s->regular_discontinuous_paused &&
               !s->regular_waiting_for_auto_injected) {
        if (!s->clock_hz) {
            s->next_sample_ns = 0;
            s->rank_clock_hz = 0;
            s->rank_last_ns = now;
        } else if (!s->next_sample_ns) {
            s->rank_clock_hz = s->clock_hz;
            adc_rearm_remaining_rank(s, s->sample_timer,
                                     &s->next_sample_ns, s->rank_last_ns,
                                     s->rank_remaining_half_cycles, now);
        } else {
            adc_rearm_deadline(s->sample_timer, &s->next_sample_ns, now);
        }
    } else {
        s->next_sample_ns = 0;
    }

    if (s->injected_conversion_active) {
        if (!s->clock_hz) {
            s->next_injected_sample_ns = 0;
            s->injected_rank_clock_hz = 0;
            s->injected_rank_last_ns = now;
        } else if (!s->next_injected_sample_ns) {
            s->injected_rank_clock_hz = s->clock_hz;
            adc_rearm_remaining_rank(s, s->injected_timer,
                                     &s->next_injected_sample_ns,
                                     s->injected_rank_last_ns,
                                     s->injected_rank_remaining_half_cycles,
                                     now);
        } else {
            adc_rearm_deadline(s->injected_timer,
                               &s->next_injected_sample_ns, now);
        }
    } else {
        s->next_injected_sample_ns = 0;
    }
}
