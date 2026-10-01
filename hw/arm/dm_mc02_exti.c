/* Minimal STM32H723 EXTI model with the line 0..15 edge/pending path. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_exti.h"

#define EXTI_RTSR1  0x00
#define EXTI_FTSR1  0x04
#define EXTI_SWIER1 0x08
#define EXTI_PR1    0x14
#define EXTI_C1IMR1 0x80

static uint32_t exti_load(const DmMc02Exti *s, hwaddr offset)
{
    return ldl_le_p(s->regs + offset);
}

static void exti_store(DmMc02Exti *s, hwaddr offset, uint32_t value)
{
    stl_le_p(s->regs + offset, value);
}

static unsigned exti_group(unsigned line)
{
    return line < 5 ? line : line < 10 ? 5 : 6;
}

static uint32_t exti_group_mask(unsigned group)
{
    if (group < 5) {
        return UINT32_C(1) << group;
    }
    return group == 5 ? UINT32_C(0x03e0) : UINT32_C(0xfc00);
}

static void exti_update_irq(DmMc02Exti *s, unsigned group, bool force)
{
    uint32_t pending;
    bool level;

    if (!s || group >= ARRAY_SIZE(s->irq)) {
        return;
    }
    pending = exti_load(s, EXTI_PR1);
    level = (pending & exti_load(s, EXTI_C1IMR1) &
             exti_group_mask(group)) != 0;
    if (s->irq_level[group] != level) {
        s->irq_level[group] = level;
        if (s->irq[group]) {
            qemu_set_irq(s->irq[group], level);
        }
    } else if (force && s->irq[group]) {
        qemu_set_irq(s->irq[group], level);
    }
}

static uint64_t dm_mc02_exti_read(void *opaque, hwaddr offset,
                                  unsigned size)
{
    DmMc02Exti *s = opaque;
    uint32_t value = 0;

    if (size > sizeof(value) || offset >= DM_MC02_EXTI_REGION_SIZE ||
        size > DM_MC02_EXTI_REGION_SIZE - offset) {
        return 0;
    }
    for (unsigned i = 0; i < size; ++i) {
        value |= (uint32_t)s->regs[offset + i] << (i * 8);
    }
    return value;
}

static void dm_mc02_exti_write(void *opaque, hwaddr offset, uint64_t value,
                               unsigned size)
{
    DmMc02Exti *s = opaque;

    if (size == sizeof(uint32_t) && offset == EXTI_SWIER1) {
        exti_store(s, EXTI_SWIER1, value);
        exti_store(s, EXTI_PR1, exti_load(s, EXTI_PR1) | (uint32_t)value);
        for (unsigned group = 0; group < ARRAY_SIZE(s->irq); ++group) {
            exti_update_irq(s, group, false);
        }
        return;
    }
    if (size == sizeof(uint32_t) && offset == EXTI_PR1) {
        /* EXTI pending bits are cleared by writing one. */
        exti_store(s, EXTI_PR1, exti_load(s, EXTI_PR1) & ~(uint32_t)value);
        for (unsigned group = 0; group < ARRAY_SIZE(s->irq); ++group) {
            exti_update_irq(s, group, false);
        }
        return;
    }

    if (size > sizeof(uint32_t) || offset >= DM_MC02_EXTI_REGION_SIZE ||
        size > DM_MC02_EXTI_REGION_SIZE - offset) {
        return;
    }
    for (unsigned i = 0; i < size; ++i) {
        s->regs[offset + i] = value >> (i * 8);
    }
}

static const MemoryRegionOps dm_mc02_exti_ops = {
    .read = dm_mc02_exti_read,
    .write = dm_mc02_exti_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_exti_init(DmMc02Exti *state, Object *owner)
{
    memset(state, 0, sizeof(*state));
    memory_region_init_io(&state->iomem, owner, &dm_mc02_exti_ops, state,
                          "dm-mc02.exti", DM_MC02_EXTI_REGION_SIZE);
}

void dm_mc02_exti_reset(DmMc02Exti *state)
{
    if (state) {
        memset(state->regs, 0, sizeof(state->regs));
        state->line_level = 0;
        for (unsigned group = 0; group < ARRAY_SIZE(state->irq); ++group) {
            state->irq_level[group] = false;
            if (state->irq[group]) {
                qemu_set_irq(state->irq[group], false);
            }
        }
    }
}

void dm_mc02_exti_set_irq(DmMc02Exti *state, unsigned group, qemu_irq irq)
{
    if (!state || group >= ARRAY_SIZE(state->irq)) {
        return;
    }
    state->irq[group] = irq;
    state->irq_level[group] = false;
    exti_update_irq(state, group, false);
}

bool dm_mc02_exti_connect_nvic(DmMc02Exti *state,
                               const DmMc02ExtiIrqRoute *routes,
                               size_t route_count,
                               unsigned controller_input_count,
                               Error **errp)
{
    bool groups[DM_MC02_EXTI_IRQ_GROUP_COUNT] = { false };

    if (!state || !route_count ||
        route_count > DM_MC02_EXTI_IRQ_GROUP_COUNT || !routes) {
        error_setg(errp, "invalid EXTI/NVIC route table");
        return false;
    }

    /* Validate all entries before changing any borrowed IRQ binding.  This
     * prevents a malformed later entry from leaving a partially rewired
     * interrupt path. */
    for (size_t i = 0; i < route_count; ++i) {
        const DmMc02ExtiIrqRoute *route = &routes[i];

        if (route->exti_group >= DM_MC02_EXTI_IRQ_GROUP_COUNT ||
            route->controller_input >= controller_input_count ||
            !route->controller_irq || groups[route->exti_group]) {
            error_setg(errp, "invalid or duplicate EXTI/NVIC route %zu", i);
            return false;
        }
        for (size_t j = 0; j < i; ++j) {
            if (routes[j].controller_input == route->controller_input) {
                error_setg(errp,
                           "invalid or duplicate EXTI/NVIC route %zu", i);
                return false;
            }
        }
        groups[route->exti_group] = true;
    }

    for (size_t i = 0; i < route_count; ++i) {
        const DmMc02ExtiIrqRoute *route = &routes[i];

        dm_mc02_exti_set_irq(state, route->exti_group,
                             route->controller_irq);
    }
    return true;
}

void dm_mc02_exti_set_line(DmMc02Exti *state, unsigned line, bool level)
{
    uint32_t bit;
    bool old;

    if (!state || line >= 16) {
        return;
    }
    bit = UINT32_C(1) << line;
    old = (state->line_level & bit) != 0;
    if (old == level) {
        return;
    }
    if (level) {
        state->line_level |= bit;
    } else {
        state->line_level &= ~bit;
    }
    /* Update the sampled level before asserting the IRQ.  qemu_set_irq() can
     * synchronously re-enter the guest and its handler may remap the EXTI
     * source before this function returns. */
    if ((!old && level && (exti_load(state, EXTI_RTSR1) & bit)) ||
        (old && !level && (exti_load(state, EXTI_FTSR1) & bit))) {
        exti_store(state, EXTI_PR1, exti_load(state, EXTI_PR1) | bit);
        exti_update_irq(state, exti_group(line), false);
    }
}

void dm_mc02_exti_sync_runtime(DmMc02Exti *state)
{
    if (!state) {
        return;
    }
    for (unsigned group = 0; group < ARRAY_SIZE(state->irq); ++group) {
        exti_update_irq(state, group, true);
    }
}
