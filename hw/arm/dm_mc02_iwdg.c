/* Minimal STM32H723 independent watchdog model for DM-MC02. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_iwdg.h"
#include "qemu/timer.h"
#include "sysemu/reset.h"
#include "sysemu/runstate.h"

#define IWDG_KR  DM_MC02_IWDG_KR_OFFSET
#define IWDG_PR  DM_MC02_IWDG_PR_OFFSET
#define IWDG_RLR DM_MC02_IWDG_RLR_OFFSET
#define IWDG_SR  DM_MC02_IWDG_SR_OFFSET
#define IWDG_WINR DM_MC02_IWDG_WINR_OFFSET

#define IWDG_KR_UNLOCK 0x5555
#define IWDG_KR_RELOAD 0xaaaa
#define IWDG_KR_START  0xcccc

enum {
    IWDG_UPDATE_PR,
    IWDG_UPDATE_RLR,
    IWDG_UPDATE_WINR,
};

static uint32_t dm_mc02_iwdg_update_flag(unsigned index)
{
    static const uint32_t flags[DM_MC02_IWDG_UPDATE_REGISTER_COUNT] = {
        DM_MC02_IWDG_SR_PVU,
        DM_MC02_IWDG_SR_RVU,
        DM_MC02_IWDG_SR_WVU,
    };

    return flags[index];
}

static unsigned dm_mc02_iwdg_update_index(hwaddr offset)
{
    switch (offset) {
    case IWDG_PR:
        return IWDG_UPDATE_PR;
    case IWDG_RLR:
        return IWDG_UPDATE_RLR;
    case IWDG_WINR:
        return IWDG_UPDATE_WINR;
    default:
        g_assert_not_reached();
    }
}

static uint32_t *dm_mc02_iwdg_pending_value(DmMc02Iwdg *s, unsigned index)
{
    switch (index) {
    case IWDG_UPDATE_PR:
        return &s->pending_pr;
    case IWDG_UPDATE_RLR:
        return &s->pending_rlr;
    case IWDG_UPDATE_WINR:
        return &s->pending_winr;
    default:
        g_assert_not_reached();
    }
}

static uint64_t dm_mc02_iwdg_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Iwdg *s = opaque;
    uint32_t value;
    uint32_t mask;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_IWDG_REGION_SIZE ||
        size > DM_MC02_IWDG_REGION_SIZE - offset) {
        return 0;
    }
    value = s->regs[offset / sizeof(uint32_t)];
    mask = size == sizeof(uint32_t) ? UINT32_MAX : (1u << (size * 8)) - 1u;
    return (value >> ((offset % sizeof(uint32_t)) * 8)) & mask;
}

static uint64_t dm_mc02_iwdg_timeout_ns(const DmMc02Iwdg *s)
{
    uint32_t pr = s->regs[IWDG_PR / sizeof(uint32_t)] & 7;
    uint32_t rlr = s->regs[IWDG_RLR / sizeof(uint32_t)] & 0xfff;

    return dm_mc02_iwdg_timeout_ns_for_config(s->lsi_hz,
                                              s->lsi_error_ppm, pr, rlr);
}

static uint64_t dm_mc02_iwdg_active_timeout_ns(const DmMc02Iwdg *s)
{
    if (s->boot_grace_pending && s->boot_grace_ns) {
        return s->boot_grace_ns;
    }
    return dm_mc02_iwdg_timeout_ns(s);
}

static bool dm_mc02_iwdg_window_enabled(const DmMc02Iwdg *s)
{
    uint32_t rlr = s->regs[IWDG_RLR / sizeof(uint32_t)] & 0xfff;
    uint32_t winr = s->regs[IWDG_WINR / sizeof(uint32_t)] & 0xfff;

    /* WINR >= RLR is the hardware-compatible disabled/default configuration
     * used by the board model.  The active window is the descending range
     * in which the counter is at or below WINR. */
    return winr < rlr;
}

static uint32_t dm_mc02_iwdg_counter(const DmMc02Iwdg *s, uint64_t now)
{
    uint32_t pr = s->regs[IWDG_PR / sizeof(uint32_t)] & 7;
    uint32_t rlr = s->regs[IWDG_RLR / sizeof(uint32_t)] & 0xfff;
    if (!s->started || !s->next_timeout_ns || now >= s->next_timeout_ns) {
        return s->started && now >= s->next_timeout_ns ? 0 : rlr;
    }
    /* The timeout is the RLR+1-th tick; at the beginning the visible
     * down-counter is RLR, and it decrements once per watchdog tick. */
    return dm_mc02_iwdg_counter_for_deadline(s->lsi_hz, s->lsi_error_ppm,
                                             pr, rlr, s->next_timeout_ns,
                                             now);
}

static void dm_mc02_iwdg_arm(DmMc02Iwdg *s)
{
    if (s->started) {
        uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
        uint64_t delay = dm_mc02_iwdg_active_timeout_ns(s);

        /* The timeout calculation is bounded by the 64-bit QEMU virtual
         * clock.  Saturate instead of wrapping an armed deadline. */
        s->next_timeout_ns = delay > (uint64_t)INT64_MAX - now ?
                             (uint64_t)INT64_MAX : now + delay;
        timer_mod_ns(s->timeout_timer, (int64_t)s->next_timeout_ns);
    } else {
        timer_del(s->timeout_timer);
        s->next_timeout_ns = 0;
    }
}

static void dm_mc02_iwdg_schedule_updates(DmMc02Iwdg *s)
{
    uint64_t next = 0;

    timer_del(s->update_timer);
    for (unsigned i = 0; i < DM_MC02_IWDG_UPDATE_REGISTER_COUNT; ++i) {
        uint64_t deadline = s->update_deadline_ns[i];

        if (!deadline) {
            continue;
        }
        if (!next || deadline < next) {
            next = deadline;
        }
    }
    if (next) {
        timer_mod_ns(s->update_timer, (int64_t)next);
    }
}

static void dm_mc02_iwdg_update(void *opaque)
{
    DmMc02Iwdg *s = opaque;
    uint64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    bool window_updated = false;

    for (unsigned i = 0; i < DM_MC02_IWDG_UPDATE_REGISTER_COUNT; ++i) {
        uint32_t flag = dm_mc02_iwdg_update_flag(i);
        uint32_t *pending;

        if (!(s->regs[IWDG_SR / sizeof(uint32_t)] & flag) ||
            !s->update_deadline_ns[i] ||
            s->update_deadline_ns[i] > now) {
            continue;
        }
        pending = dm_mc02_iwdg_pending_value(s, i);
        switch (i) {
        case IWDG_UPDATE_PR:
            s->regs[IWDG_PR / sizeof(uint32_t)] = *pending;
            break;
        case IWDG_UPDATE_RLR:
            s->regs[IWDG_RLR / sizeof(uint32_t)] = *pending;
            break;
        case IWDG_UPDATE_WINR:
            s->regs[IWDG_WINR / sizeof(uint32_t)] = *pending;
            window_updated = true;
            break;
        default:
            g_assert_not_reached();
        }
        *pending = 0;
        s->update_deadline_ns[i] = 0;
        s->regs[IWDG_SR / sizeof(uint32_t)] &= ~flag;
    }

    /* ST's HAL relies on a WINR update to reload the counter.  PR/RLR only
     * change their committed configuration; an existing absolute deadline is
     * intentionally not reinterpreted until a later legal reload. */
    if (window_updated) {
        dm_mc02_iwdg_arm(s);
    }
    dm_mc02_iwdg_schedule_updates(s);
}

static void dm_mc02_iwdg_stage_update(DmMc02Iwdg *s, hwaddr offset,
                                      uint32_t value)
{
    unsigned index = dm_mc02_iwdg_update_index(offset);
    uint32_t flag = dm_mc02_iwdg_update_flag(index);
    uint64_t delay;
    uint64_t now;
    uint32_t *pending;

    if (s->regs[IWDG_SR / sizeof(uint32_t)] & flag) {
        return;
    }
    switch (index) {
    case IWDG_UPDATE_PR:
        value &= 7;
        break;
    case IWDG_UPDATE_RLR:
    case IWDG_UPDATE_WINR:
        value &= 0xfff;
        break;
    default:
        g_assert_not_reached();
    }

    pending = dm_mc02_iwdg_pending_value(s, index);
    *pending = value;
    s->regs[IWDG_SR / sizeof(uint32_t)] |= flag;
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    delay = dm_mc02_iwdg_status_update_delay_ns_for_config(s->lsi_hz,
                                                            s->lsi_error_ppm);
    s->update_deadline_ns[index] = delay > (uint64_t)INT64_MAX - now ?
                                    INT64_MAX : now + delay;
    dm_mc02_iwdg_schedule_updates(s);
}

static void dm_mc02_iwdg_timeout(void *opaque)
{
    DmMc02Iwdg *s = opaque;

    s->timeout_count++;
    s->started = false;
    s->next_timeout_ns = 0;
    timer_del(s->timeout_timer);
    if (s->reset_requested) {
        s->reset_requested(s->reset_requested_opaque);
    }
    qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
}

static void dm_mc02_iwdg_window_violation(DmMc02Iwdg *s)
{
    s->window_violation_count++;
    s->started = false;
    s->next_timeout_ns = 0;
    timer_del(s->timeout_timer);
    if (s->reset_requested) {
        s->reset_requested(s->reset_requested_opaque);
    }
    qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
}

static void dm_mc02_iwdg_write(void *opaque, hwaddr offset, uint64_t value,
                               unsigned size)
{
    DmMc02Iwdg *s = opaque;
    uint32_t mask;
    uint32_t old;

    if (size == 0 || size > sizeof(uint32_t) ||
        offset >= DM_MC02_IWDG_REGION_SIZE ||
        size > DM_MC02_IWDG_REGION_SIZE - offset) {
        return;
    }
    if (offset == IWDG_SR) {
        return;
    }
    if (offset == IWDG_KR && size == sizeof(uint32_t)) {
        switch (value & 0xffff) {
        case IWDG_KR_UNLOCK:
            s->write_unlocked = true;
            break;
        case IWDG_KR_RELOAD:
            if (s->started && (!s->boot_grace_pending || !s->boot_grace_ns) &&
                dm_mc02_iwdg_window_enabled(s) &&
                dm_mc02_iwdg_counter(s,
                    qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)) >
                    (s->regs[IWDG_WINR / sizeof(uint32_t)] & 0xfff)) {
                dm_mc02_iwdg_window_violation(s);
                return;
            }
            s->reload_count++;
            /* The first explicit reload completes the simulation-only boot
             * grace period.  Subsequent reloads use the configured hardware
             * timeout exactly. */
            s->boot_grace_pending = false;
            dm_mc02_iwdg_arm(s);
            break;
        case IWDG_KR_START:
            s->start_count++;
            s->started = true;
            dm_mc02_iwdg_arm(s);
            break;
        default:
            break;
        }
        return;
    }
    if ((offset == IWDG_PR || offset == IWDG_RLR || offset == IWDG_WINR) &&
        !s->write_unlocked) {
        return;
    }

    if (offset != IWDG_PR && offset != IWDG_RLR && offset != IWDG_WINR) {
        return;
    }
    mask = size == sizeof(uint32_t) ? UINT32_MAX : (1u << (size * 8)) - 1u;
    old = s->regs[offset / sizeof(uint32_t)];
    old &= ~(mask << ((offset % sizeof(uint32_t)) * 8));
    old |= ((uint32_t)value & mask) << ((offset % sizeof(uint32_t)) * 8);
    dm_mc02_iwdg_stage_update(s, offset,
                              offset == IWDG_PR ? old & 7 : old & 0xfff);
}

static const MemoryRegionOps dm_mc02_iwdg_ops = {
    .read = dm_mc02_iwdg_read,
    .write = dm_mc02_iwdg_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_iwdg_reset(void *opaque)
{
    DmMc02Iwdg *s = opaque;

    timer_del(s->timeout_timer);
    timer_del(s->update_timer);
    memset(s->regs, 0, sizeof(s->regs));
    s->regs[IWDG_RLR / sizeof(uint32_t)] = 0xfff;
    s->regs[IWDG_WINR / sizeof(uint32_t)] = 0xfff;
    s->lsi_hz = s->lsi_hz ?: DM_MC02_IWDG_DEFAULT_LSI_HZ;
    s->boot_grace_pending = true;
    s->write_unlocked = false;
    s->started = false;
    s->next_timeout_ns = 0;
    s->pending_pr = 0;
    s->pending_rlr = 0;
    s->pending_winr = 0;
    memset(s->update_deadline_ns, 0, sizeof(s->update_deadline_ns));
}

bool dm_mc02_iwdg_set_lsi_hz(DmMc02Iwdg *s, uint32_t lsi_hz)
{
    /* Configuration is intentionally not a live clock change.  It must be
     * selected before the next start/reload; the machine property rejects
     * changes while the watchdog is running. */
    if (s->started || !lsi_hz) {
        return false;
    }
    s->lsi_hz = lsi_hz;
    return true;
}

bool dm_mc02_iwdg_set_lsi_error_ppm(DmMc02Iwdg *s, int32_t error_ppm)
{
    /* See dm_mc02_iwdg_set_lsi_hz(): this is startup/next-arm configuration,
     * not a per-tick clock mutation. */
    if (s->started || !dm_mc02_iwdg_lsi_config_valid(s->lsi_hz, error_ppm)) {
        return false;
    }
    s->lsi_error_ppm = error_ppm;
    return true;
}

void dm_mc02_iwdg_set_boot_grace_ms(DmMc02Iwdg *s, uint32_t grace_ms)
{
    s->boot_grace_ns = (uint64_t)grace_ms * 1000000ULL;
    if (s->timeout_timer) {
        dm_mc02_iwdg_arm(s);
    }
}

void dm_mc02_iwdg_set_reset_callback(DmMc02Iwdg *s,
                                     DmMc02IwdgResetRequested *callback,
                                     void *opaque)
{
    s->reset_requested = callback;
    s->reset_requested_opaque = opaque;
}

void dm_mc02_iwdg_init(DmMc02Iwdg *state, Object *owner)
{
    memset(state, 0, sizeof(*state));
    memory_region_init_io(&state->iomem, owner, &dm_mc02_iwdg_ops, state,
                          "dm-mc02.iwdg1", DM_MC02_IWDG_REGION_SIZE);
    state->timeout_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                        dm_mc02_iwdg_timeout, state);
    state->update_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                       dm_mc02_iwdg_update, state);
    qemu_register_reset(dm_mc02_iwdg_reset, state);
    dm_mc02_iwdg_reset(state);
}

void dm_mc02_iwdg_sync_runtime(DmMc02Iwdg *s)
{
    uint64_t now;
    int64_t deadline;

    timer_del(s->timeout_timer);
    timer_del(s->update_timer);
    dm_mc02_iwdg_schedule_updates(s);
    if (!s->started) {
        s->next_timeout_ns = 0;
        return;
    }

    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    /* A deadline that elapsed while the state was in transit must remain
     * observable as an imminent watchdog reset; do not silently restart a
     * full timeout window on the destination. */
    deadline = s->next_timeout_ns > INT64_MAX ? INT64_MAX :
               (int64_t)s->next_timeout_ns;
    timer_mod_ns(s->timeout_timer, MAX(deadline, (int64_t)now));
}
