/* Minimal STM32H723 system window watchdog model.
 *
 * The register layout and two-stage EWI/reset behavior follow the reusable
 * STM32H7 model used by Renode.  The component deliberately schedules only
 * the next observable event; it does not create one host event per watchdog
 * tick.
 */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_wwdg.h"
#include "hw/arm/dm_mc02_wwdg_timing.h"
#include "qemu/timer.h"
#include "sysemu/reset.h"
#include "sysemu/runstate.h"

static uint64_t dm_mc02_wwdg_tick_ns(const DmMc02Wwdg *s)
{
    uint32_t cfr;
    unsigned wdgtb;

    if (!s || !s->clock_hz) {
        return 0;
    }
    cfr = s->regs[DM_MC02_WWDG_CFR_OFFSET / sizeof(uint32_t)];
    wdgtb = (cfr & DM_MC02_WWDG_CFR_WDGTB_MASK) >>
            DM_MC02_WWDG_CFR_WDGTB_SHIFT;
    return dm_mc02_wwdg_tick_ns_for_config(s->clock_hz, wdgtb);
}

static uint64_t dm_mc02_wwdg_add_deadline(uint64_t now, uint64_t delay)
{
    if (delay > (uint64_t)INT64_MAX - now) {
        return INT64_MAX;
    }
    return now + delay;
}

static uint32_t dm_mc02_wwdg_counter_at(const DmMc02Wwdg *s, uint64_t now)
{
    uint64_t elapsed_ns;
    uint64_t tick_ns;
    uint64_t elapsed_ticks;

    if (!s || !s->started || now <= s->counter_start_ns) {
        return s ? s->counter : 0;
    }
    tick_ns = dm_mc02_wwdg_tick_ns(s);
    if (!tick_ns) {
        return s->counter;
    }
    elapsed_ns = now - s->counter_start_ns;
    elapsed_ticks = elapsed_ns / tick_ns;
    if (s->reset_stage) {
        return elapsed_ticks ? DM_MC02_WWDG_COUNTER_RESET :
                                DM_MC02_WWDG_COUNTER_EWI;
    }
    if (s->counter <= DM_MC02_WWDG_COUNTER_EWI) {
        return DM_MC02_WWDG_COUNTER_EWI;
    }
    return elapsed_ticks >= s->counter - DM_MC02_WWDG_COUNTER_EWI ?
           DM_MC02_WWDG_COUNTER_EWI : s->counter - elapsed_ticks;
}

static void dm_mc02_wwdg_update_irq(DmMc02Wwdg *s)
{
    bool level;

    if (!s || !s->irq) {
        return;
    }
    level = (s->regs[DM_MC02_WWDG_SR_OFFSET / sizeof(uint32_t)] &
             DM_MC02_WWDG_SR_EWIF) &&
            (s->regs[DM_MC02_WWDG_CFR_OFFSET / sizeof(uint32_t)] &
             DM_MC02_WWDG_CFR_EWI);
    qemu_set_irq(s->irq, level);
}

static void dm_mc02_wwdg_schedule(DmMc02Wwdg *s)
{
    uint64_t now;
    uint64_t ticks;
    uint64_t tick_ns;
    __uint128_t delay;

    if (!s || !s->timer || !s->started || !s->clock_hz) {
        if (s && s->timer) {
            timer_del(s->timer);
        }
        if (s) {
            s->next_event_ns = 0;
        }
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    tick_ns = dm_mc02_wwdg_tick_ns(s);
    ticks = s->reset_stage ? 1 :
            (s->counter > DM_MC02_WWDG_COUNTER_EWI ?
             s->counter - DM_MC02_WWDG_COUNTER_EWI : 0);
    delay = (__uint128_t)tick_ns * MAX(ticks, UINT64_C(1));
    s->next_event_ns = dm_mc02_wwdg_add_deadline(
        now, delay > INT64_MAX ? INT64_MAX : (uint64_t)delay);
    timer_mod_ns(s->timer, (int64_t)s->next_event_ns);
}

void dm_mc02_wwdg_sync_runtime(DmMc02Wwdg *s)
{
    uint64_t now;
    int64_t deadline;

    if (!s) {
        return;
    }
    if (s->timer) {
        timer_del(s->timer);
    }
    dm_mc02_wwdg_update_irq(s);
    if (!s->started || !s->next_event_ns || !s->timer) {
        return;
    }

    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    /* An elapsed deadline remains an immediate event after restore.  Do not
     * restart a fresh watchdog interval merely because migration took time. */
    deadline = s->next_event_ns > INT64_MAX ? INT64_MAX :
               (int64_t)s->next_event_ns;
    timer_mod_ns(s->timer, MAX(deadline, (int64_t)now));
}

static void dm_mc02_wwdg_request_reset(DmMc02Wwdg *s, bool window_violation)
{
    if (!s) {
        return;
    }
    if (window_violation) {
        s->window_violation_count++;
    } else {
        s->timeout_count++;
    }
    s->started = false;
    s->reset_stage = false;
    s->next_event_ns = 0;
    timer_del(s->timer);
    if (s->reset_requested) {
        s->reset_requested(s->reset_requested_opaque);
    }
    qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
}

static void dm_mc02_wwdg_timer(void *opaque)
{
    DmMc02Wwdg *s = opaque;

    if (!s || !s->started) {
        return;
    }
    if (s->reset_stage) {
        dm_mc02_wwdg_request_reset(s, false);
        return;
    }

    s->counter = DM_MC02_WWDG_COUNTER_EWI;
    s->counter_start_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->reset_stage = true;
    s->ewi_count++;
    s->regs[DM_MC02_WWDG_SR_OFFSET / sizeof(uint32_t)] |=
        DM_MC02_WWDG_SR_EWIF;
    dm_mc02_wwdg_update_irq(s);
    dm_mc02_wwdg_schedule(s);
}

static uint32_t dm_mc02_wwdg_extract(uint64_t value, hwaddr offset,
                                     unsigned size)
{
    uint32_t mask = size == sizeof(uint32_t) ? UINT32_MAX :
                    (UINT32_C(1) << (size * 8)) - 1;

    return ((uint32_t)value & mask) << ((offset & 3) * 8);
}

static uint64_t dm_mc02_wwdg_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Wwdg *s = opaque;
    uint32_t value;
    uint32_t mask;
    hwaddr word_offset;

    if (!s || !size || size > sizeof(uint32_t) || (offset & 3) + size > 4 ||
        offset >= DM_MC02_WWDG_REGION_SIZE ||
        size > DM_MC02_WWDG_REGION_SIZE - offset) {
        return 0;
    }
    word_offset = offset & ~3u;
    value = s->regs[word_offset / sizeof(uint32_t)];
    if (word_offset == DM_MC02_WWDG_CR_OFFSET) {
        value = (value & ~DM_MC02_WWDG_CR_T_MASK) |
                (dm_mc02_wwdg_counter_at(
                    s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)) &
                 DM_MC02_WWDG_CR_T_MASK);
        if (s->started) {
            value |= DM_MC02_WWDG_CR_WDGA;
        } else {
            value &= ~DM_MC02_WWDG_CR_WDGA;
        }
    }
    mask = size == sizeof(uint32_t) ? UINT32_MAX :
           (UINT32_C(1) << (size * 8)) - 1;
    return (value >> ((offset & 3) * 8)) & mask;
}

static void dm_mc02_wwdg_reload(DmMc02Wwdg *s, uint32_t value,
                                bool enabling)
{
    uint64_t now;
    uint32_t current;
    uint32_t window;

    if (!s) {
        return;
    }
    /* WDGA is set-only while running.  A CR write with WDGA=0 still
     * reloads T; software cannot stop an active WWDG. */
    if (!enabling && s->started) {
        enabling = true;
    }
    if (!enabling) {
        s->started = false;
        s->reset_stage = false;
        s->next_event_ns = 0;
        timer_del(s->timer);
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    current = dm_mc02_wwdg_counter_at(s, now);
    window = s->regs[DM_MC02_WWDG_CFR_OFFSET / sizeof(uint32_t)] &
             DM_MC02_WWDG_CFR_W_MASK;
    if (s->started && (value < DM_MC02_WWDG_COUNTER_EWI || current > window)) {
        dm_mc02_wwdg_request_reset(s, true);
        return;
    }
    if (!s->started) {
        s->start_count++;
    }
    s->started = true;
    s->reset_stage = false;
    s->counter = value & DM_MC02_WWDG_CR_T_MASK;
    s->counter_start_ns = now;
    s->reload_count++;
    dm_mc02_wwdg_schedule(s);
}

static void dm_mc02_wwdg_write(void *opaque, hwaddr offset, uint64_t value,
                               unsigned size)
{
    DmMc02Wwdg *s = opaque;
    hwaddr word_offset;
    uint32_t old;
    uint32_t next;
    uint32_t write_value;
    uint64_t now;
    uint32_t current;

    if (!s || !size || size > sizeof(uint32_t) || (offset & 3) + size > 4 ||
        offset >= DM_MC02_WWDG_REGION_SIZE ||
        size > DM_MC02_WWDG_REGION_SIZE - offset) {
        return;
    }
    word_offset = offset & ~3u;
    old = s->regs[word_offset / sizeof(uint32_t)];
    write_value = dm_mc02_wwdg_extract(value, offset, size);
    if (word_offset == DM_MC02_WWDG_CR_OFFSET) {
        next = old & ~(DM_MC02_WWDG_CR_T_MASK | DM_MC02_WWDG_CR_WDGA);
        next |= write_value & (DM_MC02_WWDG_CR_T_MASK | DM_MC02_WWDG_CR_WDGA);
        if (s->started) {
            next |= DM_MC02_WWDG_CR_WDGA;
        }
        s->regs[word_offset / sizeof(uint32_t)] = next;
        dm_mc02_wwdg_reload(s, next & DM_MC02_WWDG_CR_T_MASK,
                            !!(next & DM_MC02_WWDG_CR_WDGA));
        return;
    }
    if (word_offset == DM_MC02_WWDG_CFR_OFFSET) {
        /* Snapshot CNT under the old prescaler before committing WDGTB. */
        now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        current = s->started ? dm_mc02_wwdg_counter_at(s, now) : s->counter;
        next = old & ~(DM_MC02_WWDG_CFR_W_MASK |
                       DM_MC02_WWDG_CFR_EWI |
                       DM_MC02_WWDG_CFR_WDGTB_MASK);
        next |= write_value & (DM_MC02_WWDG_CFR_W_MASK |
                               DM_MC02_WWDG_CFR_EWI |
                               DM_MC02_WWDG_CFR_WDGTB_MASK);
        s->regs[word_offset / sizeof(uint32_t)] = next;
        if (s->started) {
            s->counter = current;
            s->counter_start_ns = now;
            dm_mc02_wwdg_schedule(s);
        }
        dm_mc02_wwdg_update_irq(s);
        return;
    }
    if (word_offset == DM_MC02_WWDG_SR_OFFSET) {
        /* EWIF is write-zero-to-clear; a written one leaves it unchanged. */
        if (!(write_value & DM_MC02_WWDG_SR_EWIF)) {
            s->regs[word_offset / sizeof(uint32_t)] &=
                ~DM_MC02_WWDG_SR_EWIF;
            dm_mc02_wwdg_update_irq(s);
        }
    }
}

static const MemoryRegionOps dm_mc02_wwdg_ops = {
    .read = dm_mc02_wwdg_read,
    .write = dm_mc02_wwdg_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
    /* Keep an invalid cross-register transaction whole; the callback also
     * rejects it instead of allowing the AddressSpace layer to split it. */
    .impl.unaligned = true,
};

void dm_mc02_wwdg_reset(void *opaque)
{
    DmMc02Wwdg *s = opaque;

    if (!s) {
        return;
    }
    timer_del(s->timer);
    memset(s->regs, 0, sizeof(s->regs));
    s->regs[DM_MC02_WWDG_CR_OFFSET / sizeof(uint32_t)] =
        DM_MC02_WWDG_CR_T_MASK;
    s->regs[DM_MC02_WWDG_CFR_OFFSET / sizeof(uint32_t)] =
        DM_MC02_WWDG_CFR_W_MASK;
    s->counter = DM_MC02_WWDG_CR_T_MASK;
    s->counter_start_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->next_event_ns = 0;
    s->started = false;
    s->reset_stage = false;
    dm_mc02_wwdg_update_irq(s);
}

void dm_mc02_wwdg_init(DmMc02Wwdg *state, Object *owner, uint64_t clock_hz)
{
    memset(state, 0, sizeof(*state));
    state->clock_hz = clock_hz;
    memory_region_init_io(&state->iomem, owner, &dm_mc02_wwdg_ops, state,
                          "dm-mc02.wwdg1", DM_MC02_WWDG_REGION_SIZE);
    state->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                dm_mc02_wwdg_timer, state);
    qemu_register_reset(dm_mc02_wwdg_reset, state);
    dm_mc02_wwdg_reset(state);
}

void dm_mc02_wwdg_set_irq(DmMc02Wwdg *state, qemu_irq irq)
{
    if (!state) {
        return;
    }
    state->irq = irq;
    dm_mc02_wwdg_update_irq(state);
}

void dm_mc02_wwdg_set_clock_hz(DmMc02Wwdg *state, uint64_t clock_hz)
{
    uint64_t now;

    if (!state || state->clock_hz == clock_hz) {
        return;
    }
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    if (state->started) {
        state->counter = dm_mc02_wwdg_counter_at(state, now);
        state->counter_start_ns = now;
    }
    state->clock_hz = clock_hz;
    dm_mc02_wwdg_schedule(state);
}

void dm_mc02_wwdg_set_reset_callback(
    DmMc02Wwdg *state, DmMc02WwdgResetRequested *callback, void *opaque)
{
    if (!state) {
        return;
    }
    state->reset_requested = callback;
    state->reset_requested_opaque = opaque;
}
