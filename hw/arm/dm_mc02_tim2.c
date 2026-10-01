/*
 * Minimal STM32 timer model shared by the DM-MC02 timer instances.
 *
 * Register accesses are retained and CNT is derived from QEMU virtual time
 * while CEN is set.  An optional virtual timer reports update flags and
 * interrupts.  PWM output state is evaluated lazily by the observation API;
 * individual output edges are intentionally not scheduled as host events.
 */
#include "qemu/osdep.h"
#include "qemu/timer.h"
#include "hw/irq.h"
#include "hw/arm/dm_mc02_tim2.h"

#define TIM_CR1   0x00
#define TIM_CR2   0x04
#define TIM_DIER  0x0c
#define TIM_SR    0x10
#define TIM_EGR   0x14
#define TIM_CCMR1 0x18
#define TIM_CCMR2 0x1c
#define TIM_CCER  0x20
#define TIM_CNT   0x24
#define TIM_PSC   0x28
#define TIM_ARR   0x2c
#define TIM_RCR   0x30
#define TIM_CCR1  0x34
#define TIM_CCR2  0x38
#define TIM_CCR3  0x3c
#define TIM_CCR4  0x40
#define TIM_BDTR  0x44

#define TIM_CR1_CEN (1u << 0)
#define TIM_CR1_DIR (1u << 4)
#define TIM_CR1_CMS_MASK (3u << 5)
#define TIM_CR1_ARPE (1u << 7)
#define TIM_CR1_CKD_SHIFT 8
#define TIM_CR1_CKD_MASK (3u << TIM_CR1_CKD_SHIFT)
#define TIM_CR2_MMS_SHIFT 4
#define TIM_CR2_MMS_MASK  (7u << TIM_CR2_MMS_SHIFT)
#define TIM_CR2_MMS2_SHIFT 20
#define TIM_CR2_MMS2_MASK  (0xfu << TIM_CR2_MMS2_SHIFT)
#define TIM_DIER_UIE (1u << 0)
#define TIM_DIER_CC1IE (1u << 1)
#define TIM_DIER_CC2IE (1u << 2)
#define TIM_DIER_CC3IE (1u << 3)
#define TIM_DIER_CC4IE (1u << 4)
#define TIM_SR_UIF   (1u << 0)
#define TIM_SR_CC1IF (1u << 1)
#define TIM_SR_CC2IF (1u << 2)
#define TIM_SR_CC3IF (1u << 3)
#define TIM_SR_CC4IF (1u << 4)
#define TIM_EGR_UG   (1u << 0)
#define TIM_CCMR_OC1PE (1u << 3)
#define TIM_CCMR_OC2PE (1u << 11)
#define TIM_CCMR_OC3PE (1u << 3)
#define TIM_CCMR_OC4PE (1u << 11)
#define TIM_OCM_FROZEN 0u
#define TIM_OCM_ACTIVE_ON_MATCH 1u
#define TIM_OCM_INACTIVE_ON_MATCH 2u
#define TIM_OCM_TOGGLE 3u
#define TIM_OCM_FORCE_INACTIVE 4u
#define TIM_OCM_FORCE_ACTIVE 5u
#define TIM_OCM_PWM1 6u
#define TIM_OCM_PWM2 7u
#define TIM2_CLOCK_HZ 24000000U

#define TIM_BDTR_DTG_MASK (0xffu)
#define TIM_BDTR_OSSI     (1u << 10)
#define TIM_BDTR_OSSR     (1u << 11)
#define TIM_BDTR_BKE      (1u << 12)
#define TIM_BDTR_BKP      (1u << 13)
#define TIM_BDTR_AOE      (1u << 14)
#define TIM_BDTR_MOE      (1u << 15)
#define TIM_BDTR_SUPPORTED_MASK \
    (TIM_BDTR_DTG_MASK | TIM_BDTR_OSSI | TIM_BDTR_OSSR | \
     TIM_BDTR_BKE | TIM_BDTR_BKP | TIM_BDTR_AOE | TIM_BDTR_MOE)

static void tim2_reschedule(DmMc02Tim2 *s);
static void tim2_schedule_update(DmMc02Tim2 *s, bool preserve_phase);
static void tim2_schedule_compare(DmMc02Tim2 *s);
static uint32_t tim2_load(const DmMc02Tim2 *s, hwaddr offset,
                          unsigned size);
static void tim2_store(DmMc02Tim2 *s, hwaddr offset, uint64_t value,
                       unsigned size);
static bool tim2_compare_channel_required(const DmMc02Tim2 *s,
                                          unsigned channel);
static bool tim2_pwm_registers(unsigned channel, hwaddr *ccmr,
                               unsigned *ocm_shift, hwaddr *ccr,
                               unsigned *ccer_shift);
static uint32_t tim2_active_psc_value(const DmMc02Tim2 *s);
static bool tim2_center_aligned(const DmMc02Tim2 *s);
static uint64_t tim2_counter_cycle_ticks(const DmMc02Tim2 *s);
static uint64_t tim2_phase_at(const DmMc02Tim2 *s, uint64_t now);
static bool tim2_counting_down_at(const DmMc02Tim2 *s, uint64_t now);
static uint64_t tim2_phase_distance(uint64_t phase, uint64_t target,
                                    uint64_t cycle);
static bool tim2_break_active(const DmMc02Tim2 *s);
static void tim2_refresh_break_state(DmMc02Tim2 *s, bool update_event);
static void tim2_generate_update_event(DmMc02Tim2 *s,
                                        uint64_t timestamp_ns,
                                        bool reset_counter,
                                        unsigned output_mask);
static uint16_t tim2_master_ocref_output_masks(const DmMc02Tim2 *s);
static void tim2_snapshot_ocref_levels(const DmMc02Tim2 *s,
                                       uint64_t counter,
                                       uint8_t *valid_mask,
                                       uint8_t *level_mask);
static void tim2_snapshot_ocref_compare(DmMc02Tim2 *s,
                                        unsigned channel_mask,
                                        bool counting_down,
                                        uint8_t *valid_mask,
                                        uint8_t *old_level_mask,
                                        uint8_t *new_level_mask);
static void tim2_emit_ocref_snapshot(DmMc02Tim2 *s, uint16_t output_masks,
                                     uint8_t valid_mask,
                                     uint8_t old_level_mask,
                                     uint8_t new_level_mask,
                                     unsigned event_count,
                                     uint64_t timestamp_ns);

static unsigned tim2_decode_ocref_mode(const DmMc02Tim2 *s,
                                       uint32_t ccmr, unsigned ocm_shift)
{
    unsigned mode = (ccmr >> ocm_shift) & 7u;

    /* H723 advanced timers extend OCxM with bit 16/24.  The generic timer
     * model uses the MMS2 capability as the profile-provided indicator for
     * those split fields; ordinary timers must retain their 3-bit layout. */
    if (s && s->mms2_supported) {
        mode |= ((ccmr >> (ocm_shift + 12)) & 1u) << 3;
    }
    return mode;
}

static unsigned tim2_ocref_mode(const DmMc02Tim2 *s, unsigned channel)
{
    hwaddr ccmr;
    hwaddr ccr;
    unsigned ocm_shift;
    unsigned ccer_shift;

    if (!s || !tim2_pwm_registers(channel, &ccmr, &ocm_shift, &ccr,
                                  &ccer_shift)) {
        return TIM_OCM_FROZEN;
    }
    return tim2_decode_ocref_mode(s, tim2_load(s, ccmr, 4), ocm_shift);
}

static bool tim2_ocref_stateful_mode(unsigned mode)
{
    return mode >= TIM_OCM_ACTIVE_ON_MATCH && mode <= TIM_OCM_TOGGLE;
}

static bool tim2_has_stateful_ocref(const DmMc02Tim2 *s)
{
    if (!s) {
        return false;
    }
    for (unsigned channel = 1; channel <= 4; ++channel) {
        if (tim2_compare_channel_required(s, channel) &&
            tim2_ocref_stateful_mode(tim2_ocref_mode(s, channel))) {
            return true;
        }
    }
    return false;
}

static void tim2_set_ocref_level(DmMc02Tim2 *s, unsigned channel,
                                 bool level)
{
    uint8_t bit;

    if (!s || channel < 1 || channel > 4) {
        return;
    }
    bit = 1u << (channel - 1);
    s->ocref_level_mask = (s->ocref_level_mask & ~bit) |
                          (level ? bit : 0);
}

static uint64_t tim2_add_deadline(uint64_t now, uint64_t delay)
{
    if (!delay || now > INT64_MAX || delay > (uint64_t)INT64_MAX - now) {
        return 0;
    }
    return now + delay;
}

static uint64_t tim2_saturating_add(uint64_t left, uint64_t right)
{
    return right > UINT64_MAX - left ? UINT64_MAX : left + right;
}

static uint64_t tim2_saturating_mul(uint64_t left, uint64_t right)
{
    return left && right > UINT64_MAX / left ? UINT64_MAX : left * right;
}

static uint32_t tim2_dead_time_cycles(const DmMc02Tim2 *s,
                                      uint32_t *dead_time_ticks)
{
    uint32_t bdtr;
    unsigned ckd;
    unsigned ckd_divider;
    uint32_t ticks;

    if (!s || !s->complementary_supported) {
        if (dead_time_ticks) {
            *dead_time_ticks = 0;
        }
        return 0;
    }
    bdtr = tim2_load(s, TIM_BDTR, 4);
    ticks = dm_mc02_tim2_decode_dead_time_ticks(bdtr);
    ckd = (tim2_load(s, TIM_CR1, 4) & TIM_CR1_CKD_MASK) >>
          TIM_CR1_CKD_SHIFT;
    /* CKD=11 is reserved on H723; keeping the largest defined divider makes
     * this unsupported encoding deterministic without changing counter time. */
    ckd_divider = ckd == 0 ? 1 : ckd == 1 ? 2 : 4;
    if (dead_time_ticks) {
        *dead_time_ticks = ticks;
    }
    return ticks * ckd_divider;
}

static uint64_t tim2_elapsed_clock_cycles(const DmMc02Tim2 *s, uint64_t now)
{
    uint64_t clock_hz;

    if (!s || now < s->start_ns) {
        return 0;
    }
    clock_hz = s->clock ? clock_get_hz(s->clock) : TIM2_CLOCK_HZ;
    if (!clock_hz) {
        return 0;
    }
    return (uint64_t)(((__uint128_t)(now - s->start_ns) * clock_hz) /
                      UINT64_C(1000000000));
}

static uint64_t tim2_phase_clock_cycles(const DmMc02Tim2 *s, uint64_t now)
{
    uint64_t counter_cycle_ticks;
    uint64_t prescaler;
    uint64_t period_cycles;
    uint64_t initial;
    __uint128_t phase;

    if (!s) {
        return 0;
    }
    counter_cycle_ticks = tim2_counter_cycle_ticks(s);
    prescaler = (uint64_t)tim2_active_psc_value(s) + 1;
    period_cycles = counter_cycle_ticks * prescaler;
    if (!period_cycles) {
        return 0;
    }
    initial = tim2_load(s, TIM_CNT, 4);
    if (tim2_center_aligned(s)) {
        initial = MIN(initial, (uint64_t)s->active_arr);
        if (s->start_counting_down && initial) {
            initial = counter_cycle_ticks - initial;
        }
    } else {
        initial %= counter_cycle_ticks;
    }
    phase = (__uint128_t)initial * prescaler +
            tim2_elapsed_clock_cycles(s, now);
    return (uint64_t)(phase % period_cycles);
}

static uint64_t tim2_cycles_to_ns(const DmMc02Tim2 *s, uint32_t cycles)
{
    uint64_t clock_hz;

    if (!s || !cycles) {
        return 0;
    }
    clock_hz = s->clock ? clock_get_hz(s->clock) : TIM2_CLOCK_HZ;
    if (!clock_hz) {
        return 0;
    }
    return (uint64_t)(((__uint128_t)cycles * UINT64_C(1000000000) +
                       clock_hz - 1) / clock_hz);
}

static bool tim2_break_active(const DmMc02Tim2 *s)
{
    uint32_t bdtr;
    bool active_high;

    if (!s || !s->break_supported) {
        return false;
    }
    bdtr = tim2_load(s, TIM_BDTR, 4);
    if (!(bdtr & TIM_BDTR_BKE)) {
        return false;
    }
    active_high = (bdtr & TIM_BDTR_BKP) != 0;
    return s->break_input_level == active_high;
}

/* Break is an output-stage fault.  It must not suppress OCREF, compare, or
 * trigger events consumed by another peripheral.  AOE only restores MOE at
 * the next qualified update after a latched break has been released. */
static void tim2_refresh_break_state(DmMc02Tim2 *s, bool update_event)
{
    uint32_t bdtr;

    if (!s || !s->break_supported) {
        return;
    }
    bdtr = tim2_load(s, TIM_BDTR, 4);
    if (tim2_break_active(s)) {
        s->break_latched = true;
        if (bdtr & TIM_BDTR_MOE) {
            tim2_store(s, TIM_BDTR, bdtr & ~TIM_BDTR_MOE, 4);
        }
        return;
    }
    if (!(bdtr & TIM_BDTR_BKE)) {
        s->break_latched = false;
    } else if (update_event && s->break_latched && (bdtr & TIM_BDTR_AOE)) {
        tim2_store(s, TIM_BDTR, bdtr | TIM_BDTR_MOE, 4);
        s->break_latched = false;
    }
}

static void tim2_set_irq(DmMc02Tim2 *s, bool level)
{
    if (!s->irq || (s->irq_level_valid && s->irq_level == level)) {
        return;
    }
    s->irq_level = level;
    s->irq_level_valid = true;
    qemu_set_irq(s->irq, level);
}

static void tim2_update_irq(DmMc02Tim2 *s)
{
    uint32_t dier = tim2_load(s, TIM_DIER, 4);
    uint32_t sr = tim2_load(s, TIM_SR, 4);

    tim2_set_irq(s, ((dier & TIM_DIER_UIE) && (sr & TIM_SR_UIF)) ||
                    ((dier & TIM_DIER_CC1IE) && (sr & TIM_SR_CC1IF)) ||
                    ((dier & TIM_DIER_CC2IE) && (sr & TIM_SR_CC2IF)) ||
                    ((dier & TIM_DIER_CC3IE) && (sr & TIM_SR_CC3IF)) ||
                    ((dier & TIM_DIER_CC4IE) && (sr & TIM_SR_CC4IF)));
}

static uint32_t tim2_load(const DmMc02Tim2 *s, hwaddr offset,
                          unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; ++i) {
        value |= ((uint32_t)((const uint8_t *)s->regs)[offset + i]) <<
                 (i * 8);
    }
    return value;
}

static void tim2_store(DmMc02Tim2 *s, hwaddr offset, uint64_t value,
                       unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        ((uint8_t *)s->regs)[offset + i] = value >> (i * 8);
    }
}

static uint32_t tim2_active_psc_value(const DmMc02Tim2 *s)
{
    return s->active_psc & 0xffffu;
}

static uint32_t tim2_active_arr_value(const DmMc02Tim2 *s)
{
    return s->active_arr;
}

static uint32_t tim2_active_rcr_value(const DmMc02Tim2 *s)
{
    return s->repetition_supported ? s->active_rcr & 0xffffu : 0;
}

static uint32_t tim2_active_ccr_value(const DmMc02Tim2 *s, unsigned channel)
{
    return channel >= 1 && channel <= 4 ? s->active_ccr[channel - 1] : 0;
}

static unsigned tim2_master_selection(const DmMc02Tim2 *s,
                                      DmMc02TimMasterOutput output)
{
    uint32_t cr2 = tim2_load(s, TIM_CR2, 4);

    if (output == DM_MC02_TIM_TRGO) {
        return (cr2 & TIM_CR2_MMS_MASK) >> TIM_CR2_MMS_SHIFT;
    }
    if (!s->mms2_supported) {
        return UINT_MAX;
    }
    return (cr2 & TIM_CR2_MMS2_MASK) >> TIM_CR2_MMS2_SHIFT;
}

static bool tim2_master_event_selected(const DmMc02Tim2 *s,
                                       DmMc02TimMasterOutput output,
                                       DmMc02TimMasterEventKind kind)
{
    /* H7 MMS/MMS2 values 0..7 are the event sources implemented by this
     * reusable model: UG/reset, CEN, update, CC1 compare pulse and OCxREF. */
    switch (tim2_master_selection(s, output)) {
    case 0:
        return kind == DM_MC02_TIM_MASTER_RESET;
    case 1:
        return kind == DM_MC02_TIM_MASTER_ENABLE;
    case 2:
        return kind == DM_MC02_TIM_MASTER_UPDATE;
    case 3:
        return kind == DM_MC02_TIM_MASTER_COMPARE1;
    case 4:
        return kind == DM_MC02_TIM_MASTER_OC1REF;
    case 5:
        return kind == DM_MC02_TIM_MASTER_OC2REF;
    case 6:
        return kind == DM_MC02_TIM_MASTER_OC3REF;
    case 7:
        return kind == DM_MC02_TIM_MASTER_OC4REF;
    default:
        return false;
    }
}

static uint16_t tim2_master_ocref_output_masks(const DmMc02Tim2 *s)
{
    uint16_t masks = 0;

    if (!s || !s->event_callback) {
        return 0;
    }
    for (DmMc02TimMasterOutput output = DM_MC02_TIM_TRGO;
         output <= DM_MC02_TIM_TRGO2; ++output) {
        unsigned selection = tim2_master_selection(s, output);

        if (selection >= 4 && selection <= 7) {
            unsigned channel = selection - 3;

            masks |= 1u << ((channel - 1) * 2 + output);
        }
    }
    return masks;
}

static bool tim2_channel_is_output(const DmMc02Tim2 *s, unsigned channel)
{
    hwaddr ccmr;
    hwaddr ccr;
    unsigned ocm_shift;
    unsigned ccer_shift;

    if (!s || !tim2_pwm_registers(channel, &ccmr, &ocm_shift, &ccr,
                                  &ccer_shift)) {
        return false;
    }
    return !((tim2_load(s, ccmr, 4) >> (ocm_shift - 4)) & 3u);
}

static bool tim2_update_required(const DmMc02Tim2 *s)
{
    return s->update_callback || s->irq ||
           (s->event_callback &&
            (tim2_master_event_selected(s, DM_MC02_TIM_TRGO,
                                        DM_MC02_TIM_MASTER_UPDATE) ||
             tim2_master_event_selected(s, DM_MC02_TIM_TRGO2,
                                        DM_MC02_TIM_MASTER_UPDATE) ||
             tim2_master_ocref_output_masks(s)));
}

static bool tim2_compare_required(const DmMc02Tim2 *s)
{
    return s->cc1_active || s->cc2_active || s->cc3_active || s->cc4_active ||
           (s->irq && (tim2_load(s, TIM_DIER, 4) &
                       (TIM_DIER_CC1IE | TIM_DIER_CC2IE |
                        TIM_DIER_CC3IE | TIM_DIER_CC4IE))) ||
           s->compare_callback ||
           (s->event_callback &&
            ((s->cc1_active &&
              (tim2_master_event_selected(s, DM_MC02_TIM_TRGO,
                                           DM_MC02_TIM_MASTER_COMPARE1) ||
               tim2_master_event_selected(s, DM_MC02_TIM_TRGO2,
                                           DM_MC02_TIM_MASTER_COMPARE1))) ||
             tim2_master_ocref_output_masks(s)));
}

static unsigned tim2_master_output_mask(const DmMc02Tim2 *s,
                                        DmMc02TimMasterEventKind kind)
{
    unsigned mask = 0;

    if (!s || !s->event_callback) {
        return mask;
    }
    for (DmMc02TimMasterOutput output = DM_MC02_TIM_TRGO;
         output <= DM_MC02_TIM_TRGO2; ++output) {
        if (tim2_master_event_selected(s, output, kind)) {
            mask |= 1u << output;
        }
    }
    return mask;
}

static void tim2_emit_master_event(DmMc02Tim2 *s, unsigned output_mask,
                                   DmMc02TimMasterEventKind kind,
                                   bool rising, unsigned event_count,
                                   uint64_t timestamp_ns)
{
    DmMc02TimMasterEventCallback *callback;
    void *callback_opaque;

    if (!s || !s->event_callback || !output_mask || !event_count) {
        return;
    }
    /* Keep the callback target stable if a sink disconnects itself while
     * handling the first selected output. */
    callback = s->event_callback;
    callback_opaque = s->event_opaque;
    for (DmMc02TimMasterOutput output = DM_MC02_TIM_TRGO;
         output <= DM_MC02_TIM_TRGO2; ++output) {
        if (output_mask & (1u << output)) {
            DmMc02TimMasterEvent event = {
                .output = output,
                .kind = kind,
                .rising = rising,
                .event_count = event_count,
                .timestamp_ns = timestamp_ns,
            };

            callback(callback_opaque, &event);
        }
    }
}

static bool tim2_center_aligned(const DmMc02Tim2 *s)
{
    return s && (tim2_load(s, TIM_CR1, 4) & TIM_CR1_CMS_MASK);
}

static uint64_t tim2_counter_cycle_ticks(const DmMc02Tim2 *s)
{
    uint64_t arr = tim2_active_arr_value(s);

    if (!tim2_center_aligned(s)) {
        return arr + 1;
    }
    return arr ? arr * 2 : 1;
}

static uint64_t tim2_elapsed_ticks(const DmMc02Tim2 *s, uint64_t now)
{
    uint64_t clock_hz = s->clock ? clock_get_hz(s->clock) : TIM2_CLOCK_HZ;
    uint64_t psc = tim2_active_psc_value(s);

    if (!clock_hz || now < s->start_ns) {
        return 0;
    }
    return muldiv64(now - s->start_ns, clock_hz, 1000000000ULL) /
           (psc + 1);
}

/* Return the position within one timer cycle.  Edge-aligned mode uses the
 * familiar ARR+1 sawtooth; center-aligned mode uses a 0..ARR..0 triangle. */
static uint64_t tim2_phase_at(const DmMc02Tim2 *s, uint64_t now)
{
    uint64_t cycle = tim2_counter_cycle_ticks(s);
    uint64_t initial = tim2_load(s, TIM_CNT, 4);
    uint64_t phase;

    if (!cycle) {
        return 0;
    }
    initial %= cycle;
    if (!tim2_center_aligned(s)) {
        return (initial + tim2_elapsed_ticks(s, now)) % cycle;
    }
    if (initial > s->active_arr) {
        initial = s->active_arr;
    }
    if (s->start_counting_down && initial) {
        initial = cycle - initial;
    }
    phase = (initial + tim2_elapsed_ticks(s, now)) % cycle;
    return phase;
}

static bool tim2_counting_down_at(const DmMc02Tim2 *s, uint64_t now)
{
    uint64_t arr;
    uint64_t phase;

    if (!tim2_center_aligned(s)) {
        return false;
    }
    arr = tim2_active_arr_value(s);
    if (!arr) {
        return false;
    }
    phase = tim2_phase_at(s, now);
    return phase > arr;
}

static bool tim2_compare_direction_enabled(const DmMc02Tim2 *s,
                                           bool counting_down)
{
    unsigned cms;

    if (!tim2_center_aligned(s)) {
        return !counting_down;
    }
    /* OCxREF transitions are available on both halves of the triangle.  The
     * CMS filter applies to compare flag/DMA matches, unless a master OCREF
     * route explicitly consumes the internal reference level. */
    if (s->event_callback && tim2_master_ocref_output_masks(s)) {
        return true;
    }
    cms = (tim2_load(s, TIM_CR1, 4) & TIM_CR1_CMS_MASK) >> 5;
    if (cms == 1) {
        return counting_down;
    }
    if (cms == 2) {
        return !counting_down;
    }
    return true;
}

static uint32_t tim2_counter_at(const DmMc02Tim2 *s, uint64_t now)
{
    uint32_t cr1 = tim2_load(s, TIM_CR1, 4);
    uint32_t arr = tim2_active_arr_value(s);
    uint64_t phase;

    if (!(cr1 & TIM_CR1_CEN)) {
        return tim2_load(s, TIM_CNT, 4);
    }
    phase = tim2_phase_at(s, now);
    if (!tim2_center_aligned(s) || !arr) {
        return (uint32_t)phase;
    }
    return (uint32_t)(phase <= arr ? phase : 2 * (uint64_t)arr - phase);
}

static uint32_t tim2_counter(const DmMc02Tim2 *s)
{
    return tim2_counter_at(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
}

static bool tim2_accesses(hwaddr offset, unsigned size, hwaddr reg)
{
    return offset < reg + sizeof(uint32_t) &&
           offset + size > reg;
}

static uint64_t tim2_update_period_ns(const DmMc02Tim2 *s)
{
    uint64_t psc = tim2_active_psc_value(s);
    uint64_t timer_ticks = (psc + 1) * tim2_counter_cycle_ticks(s);

    uint64_t clock_hz = s->clock ? clock_get_hz(s->clock) : TIM2_CLOCK_HZ;

    if (!clock_hz) {
        return 0;
    }
    /* The 64-bit tick product keeps the full STM32 PSC/ARR range valid. */
    return MAX(muldiv64(timer_ticks, 1000000000U, clock_hz), UINT64_C(1));
}

static uint64_t tim2_qualified_update_period_ns(const DmMc02Tim2 *s)
{
    uint64_t period = tim2_update_period_ns(s);
    uint64_t repetitions = (uint64_t)tim2_active_rcr_value(s) + 1;

    return period ? tim2_saturating_mul(period, repetitions) : 0;
}

static unsigned tim2_effective_update_batch(const DmMc02Tim2 *s)
{
    if (s->event_callback &&
        (tim2_master_event_selected(s, DM_MC02_TIM_TRGO,
                                    DM_MC02_TIM_MASTER_UPDATE) ||
         tim2_master_event_selected(s, DM_MC02_TIM_TRGO2,
                                    DM_MC02_TIM_MASTER_UPDATE) ||
         tim2_master_ocref_output_masks(s))) {
        return 1;
    }
    return MAX(s->update_batch, 1u);
}

static uint64_t tim2_remaining_interval_ns(const DmMc02Tim2 *s)
{
    uint64_t psc = tim2_active_psc_value(s);
    uint64_t counter = tim2_center_aligned(s) ?
                       tim2_phase_at(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)) :
                       tim2_load(s, TIM_CNT, 4);
    uint64_t period = tim2_counter_cycle_ticks(s);
    uint64_t remaining;
    uint64_t clock_hz = s->clock ? clock_get_hz(s->clock) : TIM2_CLOCK_HZ;
    uint64_t period_ns;
    uint64_t qualified_period_ns;
    uint64_t delay_ns;

    if (!clock_hz) {
        return 0;
    }
    if (counter >= period) {
        counter %= period;
    }
    remaining = period - counter;
    delay_ns = muldiv64(remaining * (psc + 1), 1000000000U, clock_hz);
    period_ns = tim2_update_period_ns(s);
    if (!period_ns) {
        return 0;
    }
    if (s->repetition_remaining) {
        delay_ns = tim2_saturating_add(
            delay_ns,
            tim2_saturating_mul(period_ns, s->repetition_remaining));
    }
    qualified_period_ns = tim2_qualified_update_period_ns(s);
    if (tim2_effective_update_batch(s) > 1) {
        uint64_t extra = tim2_saturating_mul(qualified_period_ns,
                                             tim2_effective_update_batch(s) - 1);

        delay_ns = tim2_saturating_add(delay_ns, extra);
    }
    return MAX(delay_ns, 1);
}

static uint64_t tim2_update_interval_ns(DmMc02Tim2 *s)
{
    uint64_t interval;

    if (s->update_interval_ns) {
        return s->update_interval_ns;
    }
    interval = tim2_qualified_update_period_ns(s);
    if (interval && tim2_effective_update_batch(s) > 1) {
        interval = tim2_saturating_mul(interval,
                                       tim2_effective_update_batch(s));
    }
    s->update_interval_ns = interval;
    return s->update_interval_ns;
}

static unsigned tim2_effective_compare_batch(const DmMc02Tim2 *s)
{
    /* A coalesced callback can describe a level change, but it cannot encode
     * the alternating edges of toggle-on-match.  Keep all stateful OC modes
     * at one virtual match per callback so the reusable master-event ABI
     * remains lossless. */
    if (tim2_center_aligned(s) || tim2_has_stateful_ocref(s) ||
        (s->cc1_active && !s->compare_callback) ||
        s->cc2_active || s->cc3_active || s->cc4_active ||
        (s->irq && (tim2_load(s, TIM_DIER, 4) &
                    (TIM_DIER_CC1IE | TIM_DIER_CC2IE |
                     TIM_DIER_CC3IE | TIM_DIER_CC4IE)))) {
        return 1;
    }
    if (s->event_callback &&
        ((s->cc1_active &&
          (tim2_master_event_selected(s, DM_MC02_TIM_TRGO,
                                       DM_MC02_TIM_MASTER_COMPARE1) ||
           tim2_master_event_selected(s, DM_MC02_TIM_TRGO2,
                                       DM_MC02_TIM_MASTER_COMPARE1))) ||
         tim2_master_ocref_output_masks(s))) {
        return 1;
    }
    return MAX(s->compare_batch, 1u);
}

static bool tim2_compare_channel_required(const DmMc02Tim2 *s,
                                          unsigned channel)
{
    uint32_t dier_bit;
    bool active;
    uint32_t dier;

    if (!s || channel < 1 || channel > 4) {
        return false;
    }
    if (!tim2_channel_is_output(s, channel)) {
        return false;
    }
    dier = tim2_load(s, TIM_DIER, 4);
    if (channel == 1) {
        return s->cc1_active || (s->irq && (dier & TIM_DIER_CC1IE)) ||
               s->compare_callback ||
               (s->event_callback && s->cc1_active &&
                (tim2_master_event_selected(s, DM_MC02_TIM_TRGO,
                                             DM_MC02_TIM_MASTER_COMPARE1) ||
                 tim2_master_event_selected(s, DM_MC02_TIM_TRGO2,
                                             DM_MC02_TIM_MASTER_COMPARE1) ||
                 tim2_master_event_selected(s, DM_MC02_TIM_TRGO,
                                             DM_MC02_TIM_MASTER_OC1REF) ||
                 tim2_master_event_selected(s, DM_MC02_TIM_TRGO2,
                                             DM_MC02_TIM_MASTER_OC1REF)));
    }
    switch (channel) {
    case 2:
        dier_bit = TIM_DIER_CC2IE;
        active = s->cc2_active;
        break;
    case 3:
        dier_bit = TIM_DIER_CC3IE;
        active = s->cc3_active;
        break;
    case 4:
        dier_bit = TIM_DIER_CC4IE;
        active = s->cc4_active;
        break;
    default:
        return false;
    }
    if (active || (s->irq && (dier & dier_bit))) {
        return true;
    }
    switch (channel) {
    case 2:
        return s->event_callback && active &&
               (tim2_master_event_selected(s, DM_MC02_TIM_TRGO,
                                            DM_MC02_TIM_MASTER_OC2REF) ||
                tim2_master_event_selected(s, DM_MC02_TIM_TRGO2,
                                            DM_MC02_TIM_MASTER_OC2REF));
    case 3:
        return s->event_callback && active &&
               (tim2_master_event_selected(s, DM_MC02_TIM_TRGO,
                                            DM_MC02_TIM_MASTER_OC3REF) ||
                tim2_master_event_selected(s, DM_MC02_TIM_TRGO2,
                                            DM_MC02_TIM_MASTER_OC3REF));
    case 4:
        return s->event_callback && active &&
               (tim2_master_event_selected(s, DM_MC02_TIM_TRGO,
                                            DM_MC02_TIM_MASTER_OC4REF) ||
                tim2_master_event_selected(s, DM_MC02_TIM_TRGO2,
                                            DM_MC02_TIM_MASTER_OC4REF));
    default:
        return false;
    }
}

static bool tim2_compare_ticks(const DmMc02Tim2 *s, unsigned channel,
                               uint64_t counter, uint64_t *ticks)
{
    uint64_t arr;
    uint64_t ccr;
    uint64_t period;

    if (!ticks || channel < 1 || channel > 4 ||
        !tim2_compare_channel_required(s, channel)) {
        return false;
    }
    arr = tim2_active_arr_value(s);
    ccr = tim2_active_ccr_value(s, channel);
    if (ccr > arr) {
        return false;
    }
    if (tim2_center_aligned(s)) {
        uint64_t down_target;
        uint64_t distance;
        bool up_enabled = tim2_compare_direction_enabled(s, false);
        bool down_enabled = tim2_compare_direction_enabled(s, true);

        period = tim2_counter_cycle_ticks(s);
        if (!period) {
            return false;
        }
        /* A match at either endpoint is shared by the up/down halves.  An
         * interior compare value has one match in each direction. */
        distance = UINT64_MAX;
        if (up_enabled) {
            distance = tim2_phase_distance(counter, ccr, period);
        }
        if (down_enabled && ccr && ccr < arr) {
            down_target = period - ccr;
            distance = MIN(distance,
                           tim2_phase_distance(counter, down_target, period));
        } else if (down_enabled && !up_enabled) {
            down_target = ccr ? (ccr == arr ? arr : period - ccr) : 0;
            distance = tim2_phase_distance(counter, down_target, period);
        }
        if (distance == UINT64_MAX) {
            return false;
        }
        *ticks = distance;
        return true;
    }
    period = arr + 1;
    if (counter >= period) {
        counter %= period;
    }
    *ticks = ccr > counter ? ccr - counter : period - counter + ccr;
    return true;
}

static uint64_t tim2_compare_delay_ns(const DmMc02Tim2 *s,
                                      unsigned *channel_mask)
{
    uint64_t psc = tim2_active_psc_value(s);
    uint64_t counter;
    uint64_t ticks;
    uint64_t best_ticks = UINT64_MAX;
    uint64_t delay;
    uint64_t clock_hz = s->clock ? clock_get_hz(s->clock) : TIM2_CLOCK_HZ;
    unsigned mask = 0;

    if (channel_mask) {
        *channel_mask = 0;
    }
    if (!clock_hz) {
        return UINT64_MAX;
    }
    counter = tim2_center_aligned(s) ?
              tim2_phase_at(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)) :
              tim2_counter(s);
    for (unsigned channel = 1; channel <= 4; ++channel) {
        if (!tim2_compare_ticks(s, channel, counter, &ticks)) {
            continue;
        }
        if (ticks < best_ticks) {
            best_ticks = ticks;
            mask = 1u << (channel - 1);
        } else if (ticks == best_ticks) {
            mask |= 1u << (channel - 1);
        }
    }
    if (best_ticks == UINT64_MAX) {
        return UINT64_MAX;
    }
    ticks = best_ticks;
    if (ticks > UINT64_MAX / (psc + 1)) {
        return UINT64_MAX;
    }
    delay = MAX(muldiv64(ticks * (psc + 1), 1000000000U, clock_hz), 1);
    if (mask == 1 && tim2_effective_compare_batch(s) > 1) {
        uint64_t period_ns = tim2_update_period_ns(s);
        uint64_t extra = tim2_saturating_mul(
            period_ns, tim2_effective_compare_batch(s) - 1);

        delay = tim2_saturating_add(delay, extra);
    }
    if (channel_mask) {
        *channel_mask = mask;
    }
    return delay;
}

static uint64_t tim2_phase_distance(uint64_t phase, uint64_t target,
                                    uint64_t cycle)
{
    if (target > phase) {
        return target - phase;
    }
    if (target < phase) {
        return cycle - phase + target;
    }
    return cycle;
}

/* Find the first compare event strictly after base_timestamp.  Each channel
 * is evaluated independently so a second channel cannot be skipped merely
 * because the first one fired first. */
static uint64_t tim2_next_compare_deadline_at(const DmMc02Tim2 *s,
                                              uint64_t base_timestamp,
                                              unsigned *channel_mask)
{
    uint64_t psc = tim2_active_psc_value(s);
    uint64_t period = tim2_counter_cycle_ticks(s);
    uint64_t period_ns = tim2_update_period_ns(s);
    uint64_t clock_hz = s->clock ? clock_get_hz(s->clock) : TIM2_CLOCK_HZ;
    uint64_t counter;
    uint64_t best = UINT64_MAX;
    unsigned mask = 0;

    if (channel_mask) {
        *channel_mask = 0;
    }
    if (!period_ns || !period || !clock_hz) {
        return 0;
    }
    counter = tim2_center_aligned(s) ? tim2_phase_at(s, base_timestamp) :
                                      tim2_counter_at(s, base_timestamp);
    for (unsigned channel = 1; channel <= 4; ++channel) {
        uint64_t ticks;
        uint64_t delay;
        uint64_t candidate;

        if (!tim2_compare_ticks(s, channel, counter, &ticks)) {
            continue;
        }
        if (ticks > UINT64_MAX / (psc + 1)) {
            continue;
        }
        delay = MAX(muldiv64(ticks * (psc + 1), 1000000000U,
                            clock_hz), UINT64_C(1));
        candidate = tim2_add_deadline(base_timestamp, delay);
        if (!candidate) {
            continue;
        }
        if (candidate < best) {
            best = candidate;
            mask = 1u << (channel - 1);
        } else if (candidate == best) {
            mask |= 1u << (channel - 1);
        }
    }
    if (channel_mask) {
        *channel_mask = mask;
    }
    return best == UINT64_MAX ? 0 : best;
}

/* Find the first compare event strictly after base_timestamp and skip any
 * events which are already behind the host's current virtual time.  This is
 * the coalesced path used by stateless compare consumers. */
static uint64_t tim2_next_compare_deadline(const DmMc02Tim2 *s,
                                           uint64_t base_timestamp,
                                           uint64_t now,
                                           unsigned *channel_mask)
{
    uint64_t psc = tim2_active_psc_value(s);
    uint64_t period = tim2_counter_cycle_ticks(s);
    uint64_t period_ns = tim2_update_period_ns(s);
    uint64_t clock_hz = s->clock ? clock_get_hz(s->clock) : TIM2_CLOCK_HZ;
    uint64_t counter;
    uint64_t best = UINT64_MAX;
    unsigned mask = 0;

    if (channel_mask) {
        *channel_mask = 0;
    }
    if (!period_ns || !period || !clock_hz) {
        return 0;
    }
    counter = tim2_center_aligned(s) ? tim2_phase_at(s, base_timestamp) :
                                      tim2_counter_at(s, base_timestamp);
    for (unsigned channel = 1; channel <= 4; ++channel) {
        uint64_t ticks;
        uint64_t delay;
        uint64_t candidate;

        if (!tim2_compare_ticks(s, channel, counter, &ticks)) {
            continue;
        }
        if (ticks > UINT64_MAX / (psc + 1)) {
            continue;
        }
        delay = MAX(muldiv64(ticks * (psc + 1), 1000000000U,
                            clock_hz), UINT64_C(1));
        candidate = tim2_add_deadline(base_timestamp, delay);
        if (!candidate) {
            continue;
        }
        if (candidate <= now) {
            uint64_t current_position = tim2_center_aligned(s) ?
                tim2_phase_at(s, now) : tim2_counter_at(s, now);
            uint64_t current_ticks;

            if (!tim2_compare_ticks(s, channel, current_position,
                                    &current_ticks) ||
                current_ticks > UINT64_MAX / (psc + 1)) {
                continue;
            }
            delay = MAX(muldiv64(current_ticks * (psc + 1), 1000000000U,
                                clock_hz), UINT64_C(1));
            candidate = tim2_add_deadline(now, delay);
        }
        if (candidate < best) {
            best = candidate;
            mask = 1u << (channel - 1);
        } else if (candidate == best) {
            mask |= 1u << (channel - 1);
        }
    }
    if (channel_mask) {
        *channel_mask = mask;
    }
    return best == UINT64_MAX ? 0 : best;
}

/* Find the next individual compare event while preserving the original
 * virtual phase.  The current callback deadline denotes the last event in
 * its batch; missed is the number of individual events after that deadline
 * which are already due when the callback runs. */
static uint64_t tim2_compare_next_event(uint64_t deadline, uint64_t now,
                                        uint64_t period, uint64_t *missed)
{
    uint64_t elapsed = 0;
    uint64_t elapsed_events = 0;
    uint64_t advance;

    if (period && now > deadline) {
        elapsed = now - deadline;
        elapsed_events = elapsed / period;
    }
    if (missed) {
        *missed = elapsed_events;
    }
    if (!period || elapsed_events == UINT64_MAX ||
        elapsed_events + 1 > UINT64_MAX / period ||
        deadline > UINT64_MAX - (elapsed_events + 1) * period) {
        return tim2_add_deadline(now, period);
    }
    advance = (elapsed_events + 1) * period;
    return tim2_add_deadline(deadline, advance);
}

static void tim2_schedule_compare(DmMc02Tim2 *s)
{
    uint64_t now;
    uint64_t delay;
    unsigned channel_mask = 0;

    if (!s->compare_timer || !tim2_compare_required(s) ||
        !(tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN)) {
        if (s->compare_timer) {
            timer_del(s->compare_timer);
        }
        s->next_compare_ns = 0;
        return;
    }
    delay = tim2_compare_delay_ns(s, &channel_mask);
    if (delay == UINT64_MAX) {
        timer_del(s->compare_timer);
        s->next_compare_ns = 0;
        return;
    }
    s->compare_channel_mask = channel_mask;
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->next_compare_ns = tim2_add_deadline(now, delay);
    if (!s->next_compare_ns) {
        timer_del(s->compare_timer);
        s->next_compare_ns = 0;
        return;
    }
    timer_mod(s->compare_timer, s->next_compare_ns);
}

/* Deliver one compare deadline without coalescing it.  Stateful OCREF modes
 * need this path because a batch cannot represent a sequence of alternating
 * or one-shot level transitions with a single rising flag. */
static bool tim2_deliver_compare_event(DmMc02Tim2 *s,
                                       unsigned compare_channel_mask,
                                       uint64_t timestamp_ns,
                                       uint64_t scheduled_deadline)
{
    unsigned master_outputs;
    uint16_t ocref_output_masks;
    uint8_t ocref_valid_mask;
    uint8_t ocref_old_level_mask;
    uint8_t ocref_new_level_mask;
    uint32_t sr;

    if (!s || !(tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN)) {
        return false;
    }
    /* Snapshot routing and OCREF before the compare consumer runs.  A
     * consumer may reconfigure the timer, but that write belongs to the next
     * compare event. */
    master_outputs = tim2_master_output_mask(
        s, DM_MC02_TIM_MASTER_COMPARE1);
    if (!(compare_channel_mask & 1u)) {
        master_outputs = 0;
    }
    ocref_output_masks = tim2_master_ocref_output_masks(s);
    tim2_snapshot_ocref_compare(s, compare_channel_mask,
                                 tim2_counting_down_at(s, timestamp_ns),
                                 &ocref_valid_mask,
                                 &ocref_old_level_mask,
                                 &ocref_new_level_mask);

    sr = tim2_load(s, TIM_SR, 4);
    if (compare_channel_mask & 1u) {
        sr |= TIM_SR_CC1IF;
    }
    if (compare_channel_mask & 2u) {
        sr |= TIM_SR_CC2IF;
    }
    if (compare_channel_mask & 4u) {
        sr |= TIM_SR_CC3IF;
    }
    if (compare_channel_mask & 8u) {
        sr |= TIM_SR_CC4IF;
    }
    tim2_store(s, TIM_SR, sr, 4);
    tim2_update_irq(s);
    if (s->compare_callback && (compare_channel_mask & 1u)) {
        s->compare_callback(s->compare_opaque, 1, timestamp_ns);
    }
    tim2_emit_master_event(s, master_outputs,
                           DM_MC02_TIM_MASTER_COMPARE1, true, 1,
                           timestamp_ns);
    tim2_emit_ocref_snapshot(s, ocref_output_masks, ocref_valid_mask,
                             ocref_old_level_mask, ocref_new_level_mask, 1,
                             timestamp_ns);

    return s->next_compare_ns == scheduled_deadline &&
           (tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN);
}

static void tim2_compare_timer(void *opaque)
{
    DmMc02Tim2 *s = opaque;
    uint64_t scheduled_deadline = s->next_compare_ns;
    uint64_t timestamp_ns = scheduled_deadline;
    uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint64_t period_ns;
    uint64_t missed = 0;
    uint64_t next_event;
    uint64_t next_deadline;
    unsigned batch = tim2_effective_compare_batch(s);
    uint64_t event_count;
    unsigned events;
    unsigned master_outputs;
    unsigned compare_channel_mask = s->compare_channel_mask;
    uint16_t ocref_output_masks;
    uint8_t ocref_valid_mask;
    uint8_t ocref_old_level_mask;
    uint8_t ocref_new_level_mask;

    if (tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN) {
        period_ns = tim2_update_period_ns(s);
        if (tim2_has_stateful_ocref(s)) {
            uint64_t event_deadline = scheduled_deadline;
            unsigned event_channel_mask = compare_channel_mask;

            /* Unlike PWM batching, stateful OCREF events must retain their
             * individual virtual order.  Replay all deadlines already
             * reached by this callback, including mixed CC1..CC4 phases. */
            for (;;) {
                if (!tim2_deliver_compare_event(s, event_channel_mask,
                                                event_deadline,
                                                scheduled_deadline)) {
                    return;
                }
                next_deadline = tim2_next_compare_deadline_at(
                    s, event_deadline, &event_channel_mask);
                if (!next_deadline) {
                    timer_del(s->compare_timer);
                    s->next_compare_ns = 0;
                    return;
                }
                if (next_deadline > now) {
                    s->compare_channel_mask = event_channel_mask;
                    s->next_compare_ns = next_deadline;
                    timer_mod(s->compare_timer, next_deadline);
                    return;
                }
                event_deadline = next_deadline;
            }
        }
        /* Snapshot routing before the compare callback.  The callback is
         * allowed to update CCR/CR2, but those writes affect the next event,
         * not the event already being delivered. */
        master_outputs = tim2_master_output_mask(
            s, DM_MC02_TIM_MASTER_COMPARE1);
        /* MMS=3 is specifically the CC1 compare pulse.  A CC2..CC4 match
         * may share this timer deadline but must not masquerade as CC1. */
        if (!(compare_channel_mask & 1u)) {
            master_outputs = 0;
        }
        ocref_output_masks = tim2_master_ocref_output_masks(s);
        tim2_snapshot_ocref_compare(s, compare_channel_mask,
                                     tim2_counting_down_at(s, timestamp_ns),
                                     &ocref_valid_mask,
                                     &ocref_old_level_mask,
                                     &ocref_new_level_mask);
        if (tim2_center_aligned(s)) {
            unsigned next_mask;

            /* Center-aligned channels match on both halves of the triangle;
             * the next event is not one full update period away. */
            next_event = tim2_next_compare_deadline_at(
                s, scheduled_deadline, &next_mask);
            missed = 0;
        } else {
            next_event = tim2_compare_next_event(
                scheduled_deadline, now, period_ns, &missed);
        }
        events = 0;
        if (missed && period_ns != UINT64_MAX &&
            missed <= UINT64_MAX / period_ns &&
            scheduled_deadline <= UINT64_MAX - missed * period_ns) {
            timestamp_ns = scheduled_deadline + missed * period_ns;
        } else if (missed) {
            /* The virtual timeline is outside the representable deadline
             * range.  Do not expose a huge count paired with an unrelated
             * old timestamp or make the master path iterate it. */
            missed = 0;
            timestamp_ns = now;
            events = 1;
        }
        if (!events) {
            event_count = missed > UINT64_MAX - batch ? UINT64_MAX :
                          (uint64_t)batch + missed;
            events = event_count > UINT_MAX ? UINT_MAX :
                     (unsigned)event_count;
        }
        uint32_t sr = tim2_load(s, TIM_SR, 4);

        if (compare_channel_mask & 1u) {
            sr |= TIM_SR_CC1IF;
        }
        if (compare_channel_mask & 2u) {
            sr |= TIM_SR_CC2IF;
        }
        if (compare_channel_mask & 4u) {
            sr |= TIM_SR_CC3IF;
        }
        if (compare_channel_mask & 8u) {
            sr |= TIM_SR_CC4IF;
        }
        tim2_store(s, TIM_SR, sr, 4);
        tim2_update_irq(s);
        if (s->compare_callback && (compare_channel_mask & 1u)) {
            s->compare_callback(s->compare_opaque, events, timestamp_ns);
        }
        if (s->next_compare_ns == scheduled_deadline &&
            (tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN)) {
            /* The callback did not reconfigure compare timing.  Keep the
             * original phase and skip only deadlines that elapsed while the
             * emulator was busy; the callback count already represents them.
             * A new batch, if selected by a later callback, begins at the
             * next individual event. */
            if (tim2_compare_channel_required(s, 2) ||
                tim2_compare_channel_required(s, 3) ||
                tim2_compare_channel_required(s, 4)) {
                unsigned next_mask = 0;

                next_deadline = tim2_next_compare_deadline(
                    s, scheduled_deadline, now, &next_mask);
                s->compare_channel_mask = next_mask;
            } else {
                unsigned next_batch = tim2_effective_compare_batch(s);
                uint64_t extra = period_ns && next_batch - 1 <=
                                 UINT64_MAX / period_ns ?
                                 (uint64_t)(next_batch - 1) * period_ns :
                                 UINT64_MAX;

                next_deadline = extra != UINT64_MAX && next_event &&
                                next_event <= UINT64_MAX - extra ?
                                next_event + extra : 0;
                if (next_deadline && next_deadline <= now) {
                    next_deadline = tim2_add_deadline(now, period_ns);
                }
            }
            if (next_deadline) {
                s->next_compare_ns = next_deadline;
                timer_mod(s->compare_timer, next_deadline);
            } else {
                timer_del(s->compare_timer);
                s->next_compare_ns = 0;
            }
        }
        /* The count is delivered in one bounded callback.  Consumers that
         * need every edge can disable batching; normal TIM8/ADC paths only
         * need the final virtual timestamp and count for a coalesced slice. */
        tim2_emit_master_event(s, master_outputs,
                               DM_MC02_TIM_MASTER_COMPARE1, true, events,
                               timestamp_ns);
        tim2_emit_ocref_snapshot(s, ocref_output_masks, ocref_valid_mask,
                                 ocref_old_level_mask, ocref_new_level_mask,
                                 events, timestamp_ns);
        return;
    }
    tim2_schedule_compare(s);
}

static bool tim2_pwm_registers(unsigned channel, hwaddr *ccmr,
                               unsigned *ocm_shift, hwaddr *ccr,
                               unsigned *ccer_shift)
{
    switch (channel) {
    case 1:
        *ccmr = TIM_CCMR1;
        *ocm_shift = 4;
        *ccr = TIM_CCR1;
        *ccer_shift = 0;
        return true;
    case 2:
        *ccmr = TIM_CCMR1;
        *ocm_shift = 12;
        *ccr = TIM_CCR2;
        *ccer_shift = 4;
        return true;
    case 3:
        *ccmr = TIM_CCMR2;
        *ocm_shift = 4;
        *ccr = TIM_CCR3;
        *ccer_shift = 8;
        return true;
    case 4:
        *ccmr = TIM_CCMR2;
        *ocm_shift = 12;
        *ccr = TIM_CCR4;
        *ccer_shift = 12;
        return true;
    default:
        return false;
    }
}

static bool tim2_channel_preload_enabled(const DmMc02Tim2 *s,
                                         unsigned channel)
{
    hwaddr ccmr;
    hwaddr ccr;
    unsigned ocm_shift;
    unsigned ccer_shift;
    unsigned preload_shift;

    if (!s || !tim2_pwm_registers(channel, &ccmr, &ocm_shift, &ccr,
                                  &ccer_shift)) {
        return false;
    }
    preload_shift = ocm_shift - 1;
    return (tim2_load(s, ccmr, 4) & (1u << preload_shift)) != 0;
}

/* Transfer the CPU-visible shadow configuration at an update event. */
static bool tim2_transfer_preloads(DmMc02Tim2 *s)
{
    bool changed = false;
    uint32_t shadow_psc;
    uint32_t shadow_arr;
    uint32_t shadow_rcr;

    shadow_psc = tim2_load(s, TIM_PSC, 4) & 0xffffu;
    if (s->active_psc != shadow_psc) {
        s->active_psc = shadow_psc;
        changed = true;
    }
    if (tim2_load(s, TIM_CR1, 4) & TIM_CR1_ARPE) {
        shadow_arr = tim2_load(s, TIM_ARR, 4);
        if (s->active_arr != shadow_arr) {
            s->active_arr = shadow_arr;
            changed = true;
        }
    }
    if (s->repetition_supported) {
        shadow_rcr = tim2_load(s, TIM_RCR, 4) & 0xffffu;
        if (s->active_rcr != shadow_rcr) {
            s->active_rcr = shadow_rcr;
            changed = true;
        }
    }
    for (unsigned channel = 1; channel <= 4; ++channel) {
        hwaddr ccmr;
        hwaddr ccr;
        unsigned ocm_shift;
        unsigned ccer_shift;
        uint32_t shadow_ccr;

        if (!tim2_channel_preload_enabled(s, channel) ||
            !tim2_pwm_registers(channel, &ccmr, &ocm_shift, &ccr,
                                &ccer_shift)) {
            continue;
        }
        shadow_ccr = tim2_load(s, ccr, 4);
        if (s->active_ccr[channel - 1] != shadow_ccr) {
            s->active_ccr[channel - 1] = shadow_ccr;
            changed = true;
        }
    }
    if (changed) {
        s->update_interval_ns = 0;
    }
    return changed;
}

/* Apply writes which are immediately active.  PSC and enabled preload
 * channels intentionally remain pending until the next update/UG event. */
static bool tim2_update_active_from_write(DmMc02Tim2 *s, hwaddr offset,
                                          unsigned size, bool was_enabled)
{
    bool changed = false;
    uint32_t cr1;

    if (!s) {
        return false;
    }
    cr1 = tim2_load(s, TIM_CR1, 4);
    if (tim2_accesses(offset, size, TIM_PSC) && !was_enabled) {
        uint32_t value = tim2_load(s, TIM_PSC, 4) & 0xffffu;

        if (s->active_psc != value) {
            s->active_psc = value;
            changed = true;
        }
    }
    if (s->repetition_supported &&
        tim2_accesses(offset, size, TIM_RCR) && !was_enabled) {
        uint32_t value = tim2_load(s, TIM_RCR, 4) & 0xffffu;

        if (s->active_rcr != value || s->repetition_remaining != value) {
            s->active_rcr = value;
            s->repetition_remaining = value;
            changed = true;
        }
    }
    if ((tim2_accesses(offset, size, TIM_ARR) ||
         tim2_accesses(offset, size, TIM_CR1)) &&
        !(cr1 & TIM_CR1_ARPE)) {
        uint32_t value = tim2_load(s, TIM_ARR, 4);

        if (s->active_arr != value) {
            s->active_arr = value;
            changed = true;
        }
    }
    for (unsigned channel = 1; channel <= 4; ++channel) {
        hwaddr ccmr;
        hwaddr ccr;
        unsigned ocm_shift;
        unsigned ccer_shift;
        bool write_ccr;
        bool write_ccmr;

        if (!tim2_pwm_registers(channel, &ccmr, &ocm_shift, &ccr,
                                &ccer_shift)) {
            continue;
        }
        write_ccr = tim2_accesses(offset, size, ccr);
        write_ccmr = tim2_accesses(offset, size, ccmr);
        if ((write_ccr || write_ccmr) &&
            !tim2_channel_preload_enabled(s, channel)) {
            uint32_t value = tim2_load(s, ccr, 4);

            if (s->active_ccr[channel - 1] != value) {
                s->active_ccr[channel - 1] = value;
                changed = true;
            }
        }
    }
    if (changed) {
        s->update_interval_ns = 0;
    }
    return changed;
}

static DmMc02TimMasterEventKind tim2_ocref_event_kind(unsigned channel)
{
    switch (channel) {
    case 1:
        return DM_MC02_TIM_MASTER_OC1REF;
    case 2:
        return DM_MC02_TIM_MASTER_OC2REF;
    case 3:
        return DM_MC02_TIM_MASTER_OC3REF;
    case 4:
        return DM_MC02_TIM_MASTER_OC4REF;
    default:
        return DM_MC02_TIM_MASTER_OC1REF;
    }
}

/* Return the internal OCxREF level for the edge-aligned modes used by the
 * trigger contract.  CCxE is intentionally not consulted: OCxREF exists
 * before the pin output stage and can feed TRGO/TRGO2 with CCxE clear. */
static bool tim2_ocref_level_at_counter(const DmMc02Tim2 *s,
                                        unsigned channel, uint64_t counter,
                                        bool *level)
{
    hwaddr ccmr;
    hwaddr ccr_offset;
    unsigned ocm_shift;
    unsigned ccer_shift;
    uint32_t cr1;
    uint32_t mode;
    uint32_t ccr;
    uint64_t period;

    if (!s || !level || !tim2_pwm_registers(channel, &ccmr, &ocm_shift,
                                             &ccr_offset, &ccer_shift)) {
        return false;
    }
    if (!tim2_channel_is_output(s, channel)) {
        return false;
    }
    cr1 = tim2_load(s, TIM_CR1, 4);
    if ((cr1 & TIM_CR1_DIR) && !tim2_center_aligned(s)) {
        /* Edge-aligned down-counting is not part of this compact model. */
        return false;
    }
    mode = tim2_decode_ocref_mode(s, tim2_load(s, ccmr, 4), ocm_shift);
    ccr = tim2_active_ccr_value(s, channel);
    period = tim2_counter_cycle_ticks(s);
    counter %= period;

    switch (mode) {
    case TIM_OCM_FROZEN:
    case TIM_OCM_ACTIVE_ON_MATCH:
    case TIM_OCM_INACTIVE_ON_MATCH:
    case TIM_OCM_TOGGLE:
        *level = (s->ocref_level_mask & (1u << (channel - 1))) != 0;
        return true;
    case TIM_OCM_FORCE_INACTIVE:
        /* Force inactive. */
        *level = false;
        return true;
    case TIM_OCM_FORCE_ACTIVE: /* Force active. */
        *level = true;
        return true;
    case TIM_OCM_PWM1: /* PWM mode 1. */
        *level = counter < ccr;
        return true;
    case TIM_OCM_PWM2: /* PWM mode 2. */
        *level = counter >= ccr;
        return true;
    default:
        return false;
    }
}

static void tim2_snapshot_ocref_levels(const DmMc02Tim2 *s,
                                       uint64_t counter,
                                       uint8_t *valid_mask,
                                       uint8_t *level_mask)
{
    uint16_t output_masks;

    if (!valid_mask || !level_mask) {
        return;
    }
    *valid_mask = 0;
    *level_mask = 0;
    output_masks = tim2_master_ocref_output_masks(s);
    if (!output_masks) {
        return;
    }
    for (unsigned channel = 1; channel <= 4; ++channel) {
        bool level;
        uint8_t channel_bit = 1u << (channel - 1);

        if (!((output_masks >> ((channel - 1) * 2)) & 3u) ||
            !tim2_ocref_level_at_counter(s, channel, counter, &level)) {
            continue;
        }
        *valid_mask |= channel_bit;
        if (level) {
            *level_mask |= channel_bit;
        }
    }
}

static void tim2_snapshot_ocref_compare(DmMc02Tim2 *s,
                                        unsigned channel_mask,
                                        bool counting_down,
                                        uint8_t *valid_mask,
                                        uint8_t *old_level_mask,
                                        uint8_t *new_level_mask)
{
    uint32_t arr;
    uint16_t output_masks;

    if (!valid_mask || !old_level_mask || !new_level_mask) {
        return;
    }
    *valid_mask = 0;
    *old_level_mask = 0;
    *new_level_mask = 0;
    output_masks = tim2_master_ocref_output_masks(s);
    arr = tim2_active_arr_value(s);
    for (unsigned channel = 1; channel <= 4; ++channel) {
        uint32_t ccr;
        unsigned mode;
        bool routed;
        bool old_level;
        bool new_level;
        uint8_t channel_bit = 1u << (channel - 1);

        if (!(channel_mask & channel_bit) ||
            !tim2_channel_is_output(s, channel)) {
            continue;
        }
        ccr = tim2_active_ccr_value(s, channel);
        if (ccr > arr) {
            continue;
        }
        mode = tim2_ocref_mode(s, channel);
        if (tim2_ocref_stateful_mode(mode)) {
            old_level = (s->ocref_level_mask & channel_bit) != 0;
            switch (mode) {
            case TIM_OCM_ACTIVE_ON_MATCH:
                new_level = true;
                break;
            case TIM_OCM_INACTIVE_ON_MATCH:
                new_level = false;
                break;
            case TIM_OCM_TOGGLE:
                new_level = !old_level;
                break;
            case TIM_OCM_FROZEN:
            default:
                new_level = old_level;
                break;
            }
        } else if (!tim2_ocref_level_at_counter(
                       s, channel,
                       tim2_center_aligned(s) ?
                       (counting_down ? (ccr < arr ? ccr + 1 : ccr) :
                        (ccr ? ccr - 1 : arr)) :
                       (ccr ? ccr - 1 : arr), &old_level) ||
                   !tim2_ocref_level_at_counter(s, channel, ccr,
                                                &new_level)) {
            continue;
        }
        tim2_set_ocref_level(s, channel, new_level);
        routed = ((output_masks >> ((channel - 1) * 2)) & 3u) != 0;
        if (!routed) {
            continue;
        }
        *valid_mask |= channel_bit;
        *old_level_mask |= old_level ? channel_bit : 0;
        *new_level_mask |= new_level ? channel_bit : 0;
    }
}

static void tim2_emit_ocref_snapshot(DmMc02Tim2 *s, uint16_t output_masks,
                                     uint8_t valid_mask,
                                     uint8_t old_level_mask,
                                     uint8_t new_level_mask,
                                     unsigned event_count,
                                     uint64_t timestamp_ns)
{
    if (!s || !event_count) {
        return;
    }
    for (unsigned channel = 1; channel <= 4; ++channel) {
        uint16_t channel_outputs;
        uint8_t channel_bit = 1u << (channel - 1);

        if (!(valid_mask & channel_bit) ||
            !((old_level_mask ^ new_level_mask) & channel_bit)) {
            continue;
        }
        channel_outputs = (output_masks >> ((channel - 1) * 2)) & 3u;
        tim2_emit_master_event(s, channel_outputs,
                               tim2_ocref_event_kind(channel),
                               (new_level_mask & channel_bit) != 0,
                               event_count, timestamp_ns);
    }
}

/* Mode writes can change OCREF without waiting for a compare deadline.  Keep
 * the persistent state coherent for subsequent stateful modes; PWM/forced
 * modes are still evaluated from the active counter configuration. */
static void tim2_refresh_ocref_state(DmMc02Tim2 *s, unsigned channel)
{
    unsigned mode;
    bool level;

    if (!s || !tim2_channel_is_output(s, channel)) {
        return;
    }
    mode = tim2_ocref_mode(s, channel);
    switch (mode) {
    case TIM_OCM_FORCE_INACTIVE:
        tim2_set_ocref_level(s, channel, false);
        break;
    case TIM_OCM_FORCE_ACTIVE:
        tim2_set_ocref_level(s, channel, true);
        break;
    case TIM_OCM_PWM1:
    case TIM_OCM_PWM2:
        if (tim2_ocref_level_at_counter(s, channel, tim2_counter(s),
                                        &level)) {
            tim2_set_ocref_level(s, channel, level);
        }
        break;
    default:
        break;
    }
}

/* Evaluate the two internal dead-time-generator gates.  The calculation is
 * phase based, so a high-rate PWM does not require a host timer per edge. */
static bool tim2_pwm_gate_levels(const DmMc02Tim2 *s, unsigned channel,
                                 bool *main_gate, bool *complementary_gate)
{
    hwaddr ccmr;
    hwaddr ccr_offset;
    unsigned ocm_shift;
    unsigned ccer_shift;
    uint32_t ccmr_value;
    uint32_t ccr;
    uint64_t arr;
    uint64_t cycle_ticks;
    uint64_t prescaler;
    uint64_t phase;
    uint64_t dead_time;
    uint64_t high_start;
    uint64_t high_end;
    uint64_t since;
    uint64_t high_ticks;
    bool high;
    unsigned mode;

    if (!s || !main_gate || !complementary_gate ||
        !tim2_pwm_registers(channel, &ccmr, &ocm_shift, &ccr_offset,
                            &ccer_shift)) {
        return false;
    }
    ccmr_value = tim2_load(s, ccmr, 4);
    if ((ccmr_value >> (ocm_shift - 4)) & 3u) {
        return false;
    }
    mode = tim2_decode_ocref_mode(s, ccmr_value, ocm_shift);
    if (mode == TIM_OCM_FORCE_INACTIVE || mode == TIM_OCM_FORCE_ACTIVE) {
        /* Forced OCREF has no transition for the dead-time generator to
         * delay.  Keep the complementary gate as the logical inverse so
         * both output-stage enables remain observable. */
        *main_gate = mode == TIM_OCM_FORCE_ACTIVE;
        *complementary_gate = !*main_gate;
        return true;
    }
    if (mode != TIM_OCM_PWM1 && mode != TIM_OCM_PWM2) {
        return false;
    }

    *main_gate = false;
    *complementary_gate = false;
    arr = tim2_active_arr_value(s);
    prescaler = (uint64_t)tim2_active_psc_value(s) + 1;
    cycle_ticks = tim2_counter_cycle_ticks(s) * prescaler;
    phase = tim2_phase_clock_cycles(s,
                                   qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    dead_time = tim2_dead_time_cycles(s, NULL);
    ccr = tim2_active_ccr_value(s, channel);
    (void)ccr_offset;
    (void)ccer_shift;

    if (tim2_center_aligned(s)) {
        uint64_t center_arr = arr;
        uint64_t compare = MIN((uint64_t)ccr, center_arr);

        if (!center_arr || !compare || compare >= center_arr) {
            /* A static reference has no transition for the dead-time
             * generator to delay. */
            high = compare == 0 ? mode == TIM_OCM_PWM2 :
                   mode == TIM_OCM_PWM1;
            *main_gate = high;
            *complementary_gate = !high;
            return true;
        }
        high_ticks = compare * prescaler;
        cycle_ticks = center_arr * 2 * prescaler;
        if (mode == TIM_OCM_PWM1) {
            if (phase < high_ticks) {
                high = true;
                since = phase + high_ticks;
            } else if (phase >= cycle_ticks - high_ticks) {
                high = true;
                since = phase - (cycle_ticks - high_ticks);
            } else {
                high = false;
                since = phase - high_ticks;
            }
        } else {
            if (phase >= high_ticks && phase < cycle_ticks - high_ticks) {
                high = true;
                since = phase - high_ticks;
            } else if (phase < high_ticks) {
                high = false;
                since = phase + high_ticks;
            } else {
                high = false;
                since = phase - (cycle_ticks - high_ticks);
            }
        }
    } else {
        uint64_t compare_ticks = MIN((uint64_t)ccr,
                                     tim2_counter_cycle_ticks(s));

        if (!compare_ticks || compare_ticks >= tim2_counter_cycle_ticks(s)) {
            high = compare_ticks == 0 ? mode == TIM_OCM_PWM2 :
                   mode == TIM_OCM_PWM1;
            *main_gate = high;
            *complementary_gate = !high;
            return true;
        }
        high_start = 0;
        high_end = compare_ticks * prescaler;
        if (mode == TIM_OCM_PWM1) {
            /* Keep the high interval at the start of the sawtooth. */
        } else {
            high_start = high_end;
            high_end = cycle_ticks;
        }
        if (phase >= high_start && phase < high_end) {
            high = true;
            since = phase - high_start;
        } else {
            high = false;
            since = phase >= high_end ? phase - high_end :
                                        cycle_ticks - high_end + phase;
        }
    }
    if (high) {
        *main_gate = since >= dead_time;
    } else {
        *complementary_gate = since >= dead_time;
    }
    return true;
}

bool dm_mc02_tim2_get_pwm_output_state(const DmMc02Tim2 *s, unsigned channel,
                                       DmMc02TimPwmOutputState *output)
{
    hwaddr ccmr;
    hwaddr ccr_offset;
    unsigned ocm_shift;
    unsigned ccer_shift;
    uint32_t cr1;
    uint32_t ccmr_value;
    uint32_t ccer;
    uint32_t bdtr;
    uint32_t dead_time_ticks;
    uint32_t dead_time_cycles;
    bool main_gate;
    bool complementary_gate;
    bool stage_enabled;
    bool main_enabled;
    bool complementary_enabled;

    if (!output) {
        return false;
    }
    memset(output, 0, sizeof(*output));
    if (!s || !tim2_pwm_registers(channel, &ccmr, &ocm_shift, &ccr_offset,
                                  &ccer_shift)) {
        return false;
    }
    cr1 = tim2_load(s, TIM_CR1, 4);
    ccmr_value = tim2_load(s, ccmr, 4);
    ccer = tim2_load(s, TIM_CCER, 4);
    bdtr = tim2_load(s, TIM_BDTR, 4);
    (void)ccr_offset;
    if (!tim2_pwm_gate_levels(s, channel, &main_gate,
                              &complementary_gate)) {
        return false;
    }
    dead_time_cycles = tim2_dead_time_cycles(s, &dead_time_ticks);
    output->dead_time_ticks = dead_time_ticks;
    output->dead_time_cycles = dead_time_cycles;
    output->dead_time_ns = tim2_cycles_to_ns(s, dead_time_cycles);
    output->complementary_supported = s->complementary_supported &&
                                      channel <= 3;
    stage_enabled = (cr1 & TIM_CR1_CEN) &&
                    !(ccmr_value & (3u << (ocm_shift - 4))) &&
                    (s->break_supported ?
                     ((bdtr & TIM_BDTR_MOE) && !tim2_break_active(s)) : true);
    main_enabled = stage_enabled && (ccer & (1u << ccer_shift));
    complementary_enabled = output->complementary_supported && stage_enabled &&
                            (ccer & (1u << (ccer_shift + 2)));
    output->main_enabled = main_enabled;
    output->main_level = main_enabled &&
                         (main_gate ^ ((ccer >> (ccer_shift + 1)) & 1u));
    output->complementary_enabled = complementary_enabled;
    output->complementary_level = complementary_enabled &&
                                  (complementary_gate ^
                                   ((ccer >> (ccer_shift + 3)) & 1u));
    return true;
}

bool dm_mc02_tim2_get_pwm_state(const DmMc02Tim2 *s, unsigned channel,
                                bool *enabled, bool *level,
                                uint64_t *frequency_hz,
                                uint32_t *duty_permille)
{
    hwaddr ccmr;
    hwaddr ccr_offset;
    unsigned ocm_shift;
    unsigned ccer_shift;
    uint32_t ccmr_value;
    uint32_t ccer;
    uint32_t arr;
    uint32_t ccr;
    uint32_t mode;
    uint64_t clock_hz;
    uint64_t period;
    DmMc02TimPwmOutputState output;

    if (!s || !tim2_pwm_registers(channel, &ccmr, &ocm_shift, &ccr_offset,
                                  &ccer_shift)) {
        return false;
    }
    ccmr_value = tim2_load(s, ccmr, 4);
    ccer = tim2_load(s, TIM_CCER, 4);
    arr = tim2_active_arr_value(s);
    ccr = tim2_active_ccr_value(s, channel);
    mode = tim2_decode_ocref_mode(s, ccmr_value, ocm_shift);
    period = tim2_counter_cycle_ticks(s);
    clock_hz = s->clock ? clock_get_hz(s->clock) : TIM2_CLOCK_HZ;
    (void)ccr_offset;
    if (!dm_mc02_tim2_get_pwm_output_state(s, channel, &output)) {
        return false;
    }

    if (frequency_hz) {
        uint64_t divider = (uint64_t)tim2_active_psc_value(s) + 1;

        *frequency_hz = output.main_enabled && clock_hz && period <=
                        UINT64_MAX / divider ? clock_hz / (divider * period) : 0;
    }
    if (duty_permille) {
        uint64_t high_ticks = MIN((uint64_t)ccr, period);

        if (tim2_center_aligned(s)) {
            uint64_t center_arr = arr;

            high_ticks = center_arr ? MIN((uint64_t)ccr, center_arr) * 1000 /
                                     center_arr : 0;
            if (mode == 7) {
                high_ticks = 1000 - high_ticks;
            }
            if (ccer & (1u << (ccer_shift + 1))) {
                high_ticks = 1000 - high_ticks;
            }
            *duty_permille = (uint32_t)high_ticks;
        } else {
            if (mode == 7) {
                high_ticks = period - high_ticks;
            }
            if (ccer & (1u << (ccer_shift + 1))) {
                high_ticks = period - high_ticks;
            }
            *duty_permille = (uint32_t)((high_ticks * 1000u) / period);
        }
    }
    if (enabled) {
        *enabled = output.main_enabled;
    }
    if (level) {
        *level = output.main_level;
    }
    return true;
}

static void tim2_generate_update_event(DmMc02Tim2 *s,
                                        uint64_t timestamp_ns,
                                        bool reset_counter,
                                        unsigned output_mask)
{
    uint16_t ocref_output_masks = tim2_master_ocref_output_masks(s);
    uint8_t old_valid_mask;
    uint8_t new_valid_mask;
    uint8_t ocref_old_level_mask;
    uint8_t ocref_new_level_mask;
    uint64_t old_counter = reset_counter ? tim2_counter(s) :
                                           (tim2_center_aligned(s) ? 0 :
                                            tim2_active_arr_value(s));
    bool active_changed;

    tim2_snapshot_ocref_levels(s, old_counter, &old_valid_mask,
                               &ocref_old_level_mask);
    active_changed = tim2_transfer_preloads(s);
    /* A qualified update reloads REP.  The scheduler accounts for this
     * value when selecting the next qualified update deadline. */
    s->repetition_remaining = tim2_active_rcr_value(s);
    tim2_refresh_break_state(s, true);
    tim2_snapshot_ocref_levels(s, 0, &new_valid_mask,
                               &ocref_new_level_mask);
    old_valid_mask &= new_valid_mask;
    if (reset_counter || tim2_center_aligned(s)) {
        tim2_store(s, TIM_CNT, 0, 4);
        s->start_ns = timestamp_ns;
        s->start_counting_down = false;
    } else if (active_changed) {
        /* An update resets the counter. Re-anchor only when the active clock
         * or period changed; unchanged configurations retain absolute phase. */
        tim2_store(s, TIM_CNT, 0, 4);
        s->start_ns = timestamp_ns;
        s->start_counting_down = false;
    }

    /* UIF is latched independently from UIE; polling firmware must see the
     * update event even when the interrupt is masked. */
    tim2_store(s, TIM_SR, tim2_load(s, TIM_SR, 4) | TIM_SR_UIF, 4);
    tim2_update_irq(s);
    if (s->update_callback) {
        s->update_callback(s->update_opaque);
    }
    tim2_emit_master_event(s, output_mask, DM_MC02_TIM_MASTER_UPDATE, true, 1,
                           timestamp_ns);
    tim2_emit_ocref_snapshot(s, ocref_output_masks, old_valid_mask,
                             ocref_old_level_mask, ocref_new_level_mask, 1,
                             timestamp_ns);
}

static void tim2_update_timer(void *opaque)
{
    DmMc02Tim2 *s = opaque;
    uint64_t timestamp_ns = s->next_update_ns;
    unsigned output_mask = tim2_master_output_mask(
        s, DM_MC02_TIM_MASTER_UPDATE);

    if (tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN) {
        if (!timestamp_ns) {
            timestamp_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        }
        tim2_generate_update_event(s, timestamp_ns, false, output_mask);
    }
    /* Advance from the previous absolute deadline.  If QEMU was delayed,
     * skip missed abstract update periods instead of firing a callback storm;
     * this is the useful timer contract for the board's DMA/ADC event model. */
    tim2_schedule_update(s, true);
}

static void tim2_schedule_update(DmMc02Tim2 *s, bool preserve_phase)
{
    uint32_t cr1;
    uint64_t interval;
    uint64_t now;
    uint64_t period;

    if (!s->update_timer) {
        return;
    }

    cr1 = tim2_load(s, TIM_CR1, 4);
    if (!tim2_update_required(s) || !(cr1 & TIM_CR1_CEN)) {
        timer_del(s->update_timer);
        s->next_update_ns = 0;
        return;
    }

    interval = tim2_update_interval_ns(s);
    if (!interval || interval > INT64_MAX) {
        timer_del(s->update_timer);
        s->next_update_ns = 0;
        return;
    }
    period = interval;
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (!preserve_phase || !s->next_update_ns) {
        uint64_t delay = preserve_phase ? tim2_remaining_interval_ns(s) :
                                         period;

        s->next_update_ns = tim2_add_deadline(now, delay);
    } else if (s->next_update_ns <= now) {
        /* The callback represents one bounded batch.  Preserve the periodic
         * phase, but do not replay events that elapsed while the emulator was
         * unable to run. */
        uint64_t missed = (now - s->next_update_ns) / period;

        if (missed == UINT64_MAX) {
            s->next_update_ns = 0;
        } else {
            missed++;
            if (missed > (uint64_t)INT64_MAX / period ||
                missed * period > (uint64_t)INT64_MAX -
                                   s->next_update_ns) {
                s->next_update_ns = tim2_add_deadline(now, period);
            } else {
                s->next_update_ns += missed * period;
            }
        }
    }
    if (!s->next_update_ns) {
        timer_del(s->update_timer);
        s->next_update_ns = 0;
        return;
    }
    timer_mod(s->update_timer, s->next_update_ns);
}

static void tim2_reschedule(DmMc02Tim2 *s)
{
    s->update_interval_ns = 0;
    tim2_schedule_update(s, false);
    tim2_schedule_compare(s);
}

static void tim2_reschedule_from_count(DmMc02Tim2 *s)
{
    uint64_t now;
    uint64_t delay;
    uint64_t period = tim2_counter_cycle_ticks(s);
    uint32_t counter = tim2_load(s, TIM_CNT, 4);

    s->update_interval_ns = 0;
    if (!s->update_timer ||
        !tim2_update_required(s) ||
        !(tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN)) {
        tim2_schedule_update(s, false);
        tim2_schedule_compare(s);
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (!tim2_center_aligned(s) && counter >= period) {
        tim2_store(s, TIM_CNT, counter % period, 4);
    }
    delay = tim2_remaining_interval_ns(s);
    s->next_update_ns = tim2_add_deadline(now, delay);
    if (s->next_update_ns) {
        timer_mod(s->update_timer, s->next_update_ns);
    } else {
        timer_del(s->update_timer);
        s->next_update_ns = 0;
    }
    tim2_schedule_compare(s);
}

static uint64_t tim2_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Tim2 *s = opaque;
    uint32_t value;

    if (offset + size > DM_MC02_TIM2_REGION_SIZE) {
        return 0;
    }
    value = tim2_load(s, offset, size);
    if (!s->repetition_supported) {
        for (unsigned i = 0; i < size; ++i) {
            if (offset + i >= TIM_RCR &&
                offset + i < TIM_RCR + sizeof(uint32_t)) {
                value &= ~(UINT32_C(0xff) << (i * 8));
            }
        }
    }
    if (!s->mms2_supported &&
        offset < TIM_CR2 + sizeof(uint32_t) &&
        offset + size > TIM_CR2) {
        for (unsigned i = 0; i < size; ++i) {
            if (offset + i == TIM_CR2 + 2) {
                value &= ~(UINT32_C(0x0f) << (i * 8));
            }
        }
    }
    if (offset == TIM_CNT && size <= 4) {
        value = tim2_counter(s);
    }
    return value;
}

static void tim2_write(void *opaque, hwaddr offset, uint64_t value,
                       unsigned size)
{
    DmMc02Tim2 *s = opaque;
    bool was_enabled;
    bool is_enabled;
    bool running_phase_write;
    bool ignore_rcr_write;
    bool bdtr_write;
    bool phase_counting_down = false;
    uint32_t latched_counter = 0;
    uint32_t old_rcr = 0;
    uint32_t old_bdtr = 0;
    uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (offset + size > DM_MC02_TIM2_REGION_SIZE) {
        return;
    }
    ignore_rcr_write = !s->repetition_supported &&
                       tim2_accesses(offset, size, TIM_RCR);
    bdtr_write = tim2_accesses(offset, size, TIM_BDTR);
    if (ignore_rcr_write) {
        old_rcr = tim2_load(s, TIM_RCR, sizeof(uint32_t));
    }
    if (bdtr_write) {
        old_bdtr = tim2_load(s, TIM_BDTR, sizeof(uint32_t));
    }
    was_enabled = tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN;
    running_phase_write = was_enabled &&
                          (tim2_accesses(offset, size, TIM_CR1) ||
                           tim2_accesses(offset, size, TIM_PSC) ||
                           tim2_accesses(offset, size, TIM_ARR) ||
                           tim2_accesses(offset, size, TIM_CR2));
    if (running_phase_write) {
        /* Capture the old virtual phase before replacing a live counter
         * configuration.  CCR writes change the next compare threshold but
         * do not reset the counter phase. */
        latched_counter = tim2_counter(s);
        phase_counting_down = tim2_counting_down_at(s, now);
        tim2_store(s, TIM_CNT, latched_counter, 4);
        s->start_ns = now;
        s->start_counting_down = phase_counting_down;
    }
    if (offset == TIM_CNT && size <= 4) {
        if (was_enabled) {
            phase_counting_down = tim2_counting_down_at(s, now);
        }
        tim2_store(s, TIM_CNT, value, size);
        s->start_ns = now;
        if (was_enabled) {
            s->start_counting_down = phase_counting_down;
        }
        if (tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN) {
            tim2_reschedule_from_count(s);
        } else {
            tim2_reschedule(s);
        }
        if (s->changed) {
            s->changed(s->changed_opaque);
        }
        return;
    }
    if (offset == TIM_SR && size == sizeof(uint32_t)) {
        /* STM32 timer status flags use write-zero-to-clear semantics. */
        tim2_store(s, TIM_SR,
                   tim2_load(s, TIM_SR, sizeof(uint32_t)) & (uint32_t)value,
                   sizeof(uint32_t));
        tim2_update_irq(s);
        if (s->changed) {
            s->changed(s->changed_opaque);
        }
        return;
    }
    tim2_store(s, offset, value, size);
    if (ignore_rcr_write) {
        tim2_store(s, TIM_RCR, old_rcr, sizeof(uint32_t));
    }
    if (bdtr_write) {
        if (!s->break_supported) {
            tim2_store(s, TIM_BDTR, old_bdtr, sizeof(uint32_t));
        } else {
            uint32_t bdtr = tim2_load(s, TIM_BDTR, sizeof(uint32_t));

            /* LOCK and the second break input are outside this reusable
             * slice.  Keep reserved/unsupported bits deterministic. */
            tim2_store(s, TIM_BDTR, bdtr & TIM_BDTR_SUPPORTED_MASK,
                       sizeof(uint32_t));
            tim2_refresh_break_state(s, false);
        }
    }
    (void)tim2_update_active_from_write(s, offset, size, was_enabled);
    if (!s->mms2_supported &&
        tim2_accesses(offset, size, TIM_CR2)) {
        uint32_t cr2 = tim2_load(s, TIM_CR2, 4);

        cr2 &= ~TIM_CR2_MMS2_MASK;
        tim2_store(s, TIM_CR2, cr2, 4);
    }
    if (tim2_accesses(offset, size, TIM_EGR) &&
        offset <= TIM_EGR && TIM_EGR < offset + size &&
        ((value >> ((TIM_EGR - offset) * 8)) & TIM_EGR_UG)) {
        uint64_t timestamp_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

        /* UG is the reset trigger input for MMS=0 and also generates an
         * update event for MMS/MMS2=2.  Keep both outputs independent. */
        unsigned reset_outputs = tim2_master_output_mask(
            s, DM_MC02_TIM_MASTER_RESET);
        unsigned update_outputs = tim2_master_output_mask(
            s, DM_MC02_TIM_MASTER_UPDATE);

        tim2_emit_master_event(s, reset_outputs, DM_MC02_TIM_MASTER_RESET,
                               true, 1, timestamp_ns);
        tim2_generate_update_event(s, timestamp_ns, true, update_outputs);
        tim2_reschedule_from_count(s);
    }
    if (running_phase_write) {
        s->start_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    }
    is_enabled = tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN;
    if (tim2_accesses(offset, size, TIM_CR1) &&
        is_enabled != was_enabled) {
        s->start_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        tim2_emit_master_event(
            s, tim2_master_output_mask(s, DM_MC02_TIM_MASTER_ENABLE),
            DM_MC02_TIM_MASTER_ENABLE, is_enabled, 1, s->start_ns);
    }
    if (tim2_accesses(offset, size, TIM_CR1) ||
        tim2_accesses(offset, size, TIM_PSC) ||
        tim2_accesses(offset, size, TIM_ARR)) {
        if (running_phase_write || is_enabled) {
            tim2_reschedule_from_count(s);
        } else {
            tim2_reschedule(s);
        }
    }
    if (tim2_accesses(offset, size, TIM_CR2) ||
        tim2_accesses(offset, size, TIM_CCMR1) ||
        tim2_accesses(offset, size, TIM_CCMR2) ||
        tim2_accesses(offset, size, TIM_CCR1) ||
        tim2_accesses(offset, size, TIM_CCR2) ||
        tim2_accesses(offset, size, TIM_CCR3) ||
        tim2_accesses(offset, size, TIM_CCR4)) {
        if (tim2_accesses(offset, size, TIM_CCR1)) {
            s->cc1_active = true;
        }
        if (tim2_accesses(offset, size, TIM_CCR2)) {
            s->cc2_active = true;
        }
        if (tim2_accesses(offset, size, TIM_CCR3)) {
            s->cc3_active = true;
        }
        if (tim2_accesses(offset, size, TIM_CCR4)) {
            s->cc4_active = true;
        }
        /* A non-frozen output-compare mode is itself sufficient to make the
         * channel's compare deadline observable.  This matters when firmware
         * programs CCMR before writing a non-zero CCR value. */
        if (tim2_accesses(offset, size, TIM_CCMR1)) {
            if (tim2_channel_is_output(s, 1) &&
                tim2_ocref_mode(s, 1) != TIM_OCM_FROZEN) {
                s->cc1_active = true;
            }
            if (tim2_channel_is_output(s, 2) &&
                tim2_ocref_mode(s, 2) != TIM_OCM_FROZEN) {
                s->cc2_active = true;
            }
        }
        if (tim2_accesses(offset, size, TIM_CCMR2)) {
            if (tim2_channel_is_output(s, 3) &&
                tim2_ocref_mode(s, 3) != TIM_OCM_FROZEN) {
                s->cc3_active = true;
            }
            if (tim2_channel_is_output(s, 4) &&
                tim2_ocref_mode(s, 4) != TIM_OCM_FROZEN) {
                s->cc4_active = true;
            }
        }
        if (tim2_accesses(offset, size, TIM_CR1) ||
            tim2_accesses(offset, size, TIM_CCMR1) ||
            tim2_accesses(offset, size, TIM_CCMR2) ||
            tim2_accesses(offset, size, TIM_CCR1) ||
            tim2_accesses(offset, size, TIM_CCR2) ||
            tim2_accesses(offset, size, TIM_CCR3) ||
            tim2_accesses(offset, size, TIM_CCR4)) {
            for (unsigned channel = 1; channel <= 4; ++channel) {
                tim2_refresh_ocref_state(s, channel);
            }
        }
        tim2_schedule_compare(s);
        tim2_schedule_update(s, true);
    }
    if (tim2_accesses(offset, size, TIM_DIER)) {
        tim2_update_irq(s);
    }
    if (s->changed) {
        s->changed(s->changed_opaque);
    }
}

static const MemoryRegionOps tim2_ops = {
    .read = tim2_read,
    .write = tim2_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_tim2_init(DmMc02Tim2 *s, Object *owner,
                       DmMc02TimChanged *changed, void *changed_opaque)
{
    memset(s, 0, sizeof(*s));
    s->start_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->break_input_level = true;
    s->update_batch = 1;
    s->compare_batch = 1;
    s->update_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, tim2_update_timer, s);
    s->compare_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, tim2_compare_timer, s);
    s->changed = changed;
    s->changed_opaque = changed_opaque;
    memory_region_init_io(&s->iomem, owner, &tim2_ops, s,
                          "dm-mc02.tim2", DM_MC02_TIM2_REGION_SIZE);
}

void dm_mc02_tim2_set_clock(DmMc02Tim2 *s, Clock *clock)
{
    bool running;
    bool counting_down = false;
    uint64_t now;

    if (!s) {
        return;
    }
    running = tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN;
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (running) {
        counting_down = tim2_counting_down_at(s, now);
    }
    if (s->clock && s->clock != clock &&
        running) {
        tim2_store(s, TIM_CNT, tim2_counter(s), 4);
    }
    if (running) {
        /* The caller may have changed the Clock object before notifying the
         * peripheral.  In that case dm_mc02_tim2_clock_changed() has already
         * latched CNT using the old frequency.  Re-anchor the counter at the
         * current virtual time and calculate the first deadline from the
         * remaining count after installing the new clock. */
        s->start_ns = now;
        s->start_counting_down = counting_down;
    }
    s->clock = clock;
    s->update_interval_ns = 0;
    if (running && s->update_timer &&
        tim2_update_required(s)) {
        uint64_t delay = tim2_remaining_interval_ns(s);

        s->next_update_ns = tim2_add_deadline(now, delay);
        if (s->next_update_ns) {
            timer_mod(s->update_timer, s->next_update_ns);
        } else {
            timer_del(s->update_timer);
            s->next_update_ns = 0;
        }
    } else {
        tim2_reschedule(s);
    }
    tim2_schedule_compare(s);
}

void dm_mc02_tim2_clock_changed(DmMc02Tim2 *s)
{
    bool counting_down = false;

    if (!s) {
        return;
    }
    if (tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN) {
        counting_down = tim2_counting_down_at(
            s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
        tim2_store(s, TIM_CNT, tim2_counter(s), 4);
        s->start_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        s->start_counting_down = counting_down;
    }
    s->update_interval_ns = 0;
    tim2_reschedule(s);
}

void dm_mc02_tim2_set_irq(DmMc02Tim2 *s, qemu_irq irq)
{
    if (!s) {
        return;
    }
    s->irq = irq;
    s->irq_level_valid = false;
    tim2_update_irq(s);
    tim2_reschedule(s);
}

void dm_mc02_tim2_sync_runtime(DmMc02Tim2 *s)
{
    uint64_t now;
    bool running;

    if (!s) {
        return;
    }
    if (s->update_timer) {
        timer_del(s->update_timer);
    }
    if (s->compare_timer) {
        timer_del(s->compare_timer);
    }
    s->update_interval_ns = 0;
    s->irq_level_valid = false;
    tim2_update_irq(s);

    running = tim2_load(s, TIM_CR1, 4) & TIM_CR1_CEN;
    if (!running) {
        s->next_update_ns = 0;
        s->next_compare_ns = 0;
        return;
    }

    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (s->update_timer && s->next_update_ns &&
        tim2_update_required(s)) {
        uint64_t deadline = s->next_update_ns;

        if (deadline < now) {
            deadline = now;
            s->next_update_ns = deadline;
        }
        timer_mod(s->update_timer, deadline);
    } else {
        tim2_schedule_update(s, true);
    }
    if (s->compare_timer && s->next_compare_ns &&
        tim2_compare_required(s)) {
        uint64_t deadline = s->next_compare_ns;

        if (deadline < now) {
            deadline = now;
            s->next_compare_ns = deadline;
        }
        timer_mod(s->compare_timer, deadline);
    } else {
        tim2_schedule_compare(s);
    }
}

bool dm_mc02_tim2_validate_vmstate(const DmMc02Tim2 *s)
{
    if (!s) {
        return false;
    }
    if ((s->next_update_ns && s->next_update_ns < s->start_ns) ||
        (s->next_compare_ns && s->next_compare_ns < s->start_ns)) {
        return false;
    }
    if (s->next_compare_ns) {
        for (unsigned channel = 1; channel <= 4; ++channel) {
            uint8_t bit = 1u << (channel - 1);

            if (!(s->compare_channel_mask & bit)) {
                continue;
            }
            if (!tim2_compare_channel_required(s, channel) ||
                s->active_ccr[channel - 1] > s->active_arr) {
                return false;
            }
        }
    }
    return true;
}

void dm_mc02_tim2_reset(DmMc02Tim2 *s)
{
    if (!s) {
        return;
    }
    memset(s->regs, 0, sizeof(s->regs));
    s->active_psc = 0;
    s->active_arr = 0;
    memset(s->active_ccr, 0, sizeof(s->active_ccr));
    s->active_rcr = 0;
    s->repetition_remaining = 0;
    s->start_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->start_counting_down = false;
    s->next_update_ns = 0;
    s->update_interval_ns = 0;
    s->next_compare_ns = 0;
    s->compare_channel_mask = 0;
    s->cc1_active = false;
    s->cc2_active = false;
    s->cc3_active = false;
    s->cc4_active = false;
    s->ocref_level_mask = 0;
    s->break_input_level = true;
    s->break_latched = false;
    s->update_batch = 1;
    s->compare_batch = 1;
    s->irq_level = false;
    s->irq_level_valid = false;
    if (s->update_timer) {
        timer_del(s->update_timer);
    }
    if (s->compare_timer) {
        timer_del(s->compare_timer);
    }
    tim2_set_irq(s, false);
}

void dm_mc02_tim2_set_update_callback(DmMc02Tim2 *s,
                                       DmMc02TimUpdateCallback *callback,
                                       void *opaque)
{
    if (!s) {
        return;
    }
    s->update_callback = callback;
    s->update_opaque = opaque;
    tim2_reschedule(s);
}

void dm_mc02_tim2_set_compare_callback(
    DmMc02Tim2 *s, DmMc02TimCompareCallback *callback, void *opaque)
{
    if (!s) {
        return;
    }
    s->compare_callback = callback;
    s->compare_opaque = opaque;
    tim2_reschedule(s);
}

void dm_mc02_tim2_set_event_callback(
    DmMc02Tim2 *s, DmMc02TimMasterEventCallback *callback, void *opaque)
{
    if (!s) {
        return;
    }
    s->event_callback = callback;
    s->event_opaque = opaque;
    tim2_reschedule(s);
}

void dm_mc02_tim2_set_mms2_supported(DmMc02Tim2 *s, bool supported)
{
    if (!s) {
        return;
    }
    s->mms2_supported = supported;
    tim2_reschedule(s);
}

void dm_mc02_tim2_set_repetition_supported(DmMc02Tim2 *s, bool supported)
{
    if (!s) {
        return;
    }
    s->repetition_supported = supported;
    if (supported) {
        s->active_rcr = tim2_load(s, TIM_RCR, 4) & 0xffffu;
        s->repetition_remaining = s->active_rcr;
    } else {
        s->active_rcr = 0;
        s->repetition_remaining = 0;
        tim2_store(s, TIM_RCR, 0, 4);
    }
    s->update_interval_ns = 0;
    tim2_reschedule(s);
}

void dm_mc02_tim2_set_break_supported(DmMc02Tim2 *s, bool supported)
{
    if (!s) {
        return;
    }
    s->break_supported = supported;
    if (!supported) {
        tim2_store(s, TIM_BDTR, 0, sizeof(uint32_t));
        s->break_latched = false;
    }
    tim2_refresh_break_state(s, false);
}

void dm_mc02_tim2_set_break_input(DmMc02Tim2 *s, bool level)
{
    if (!s) {
        return;
    }
    s->break_input_level = level;
    tim2_refresh_break_state(s, false);
    if (s->changed) {
        s->changed(s->changed_opaque);
    }
}

void dm_mc02_tim2_set_complementary_supported(DmMc02Tim2 *s, bool supported)
{
    if (!s) {
        return;
    }
    s->complementary_supported = supported;
    if (s->changed) {
        s->changed(s->changed_opaque);
    }
}

void dm_mc02_tim2_set_update_batch(DmMc02Tim2 *s, unsigned batch)
{
    if (!s) {
        return;
    }
    s->update_batch = MAX(batch, 1u);
    s->update_interval_ns = 0;
    tim2_reschedule(s);
}

unsigned dm_mc02_tim2_get_update_batch(const DmMc02Tim2 *s)
{
    return s ? s->update_batch : 0;
}

void dm_mc02_tim2_set_compare_batch(DmMc02Tim2 *s, unsigned batch)
{
    if (!s) {
        return;
    }
    s->compare_batch = MAX(batch, 1u);
    tim2_reschedule(s);
}

unsigned dm_mc02_tim2_get_compare_batch(const DmMc02Tim2 *s)
{
    return s ? s->compare_batch : 0;
}
