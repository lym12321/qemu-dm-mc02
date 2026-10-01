/* Minimal deterministic STM32H723 RNG register model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_rng.h"

#define DM_MC02_RNG_DEFAULT_SEED UINT32_C(0x6d2b79f5)
#define DM_MC02_RNG_SR_ERROR_MASK \
    (DM_MC02_RNG_SR_CECS | DM_MC02_RNG_SR_SECS | \
     DM_MC02_RNG_SR_CEIS | DM_MC02_RNG_SR_SEIS)
#define DM_MC02_RNG_CR_CONTROL_MASK \
    (DM_MC02_RNG_CR_RNGEN | DM_MC02_RNG_CR_IE)
#define DM_MC02_RNG_CR_CONDITION_MASK DM_MC02_RNG_CR_CONDRST
#define DM_MC02_RNG_CURRENT_ERROR_MASK \
    (DM_MC02_RNG_SR_CECS | DM_MC02_RNG_SR_SECS)

static uint32_t rng_next(DmMc02Rng *s)
{
    /* xorshift32: deterministic and inexpensive for repeatable tests. */
    uint32_t x = s->prng;

    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    s->prng = x;
    return x;
}

static bool dm_mc02_rng_is_enabled(const DmMc02Rng *s)
{
    return (s->regs[DM_MC02_RNG_CR / sizeof(uint32_t)] &
            (DM_MC02_RNG_CR_RNGEN | DM_MC02_RNG_CR_CONDRST)) ==
           DM_MC02_RNG_CR_RNGEN;
}

static bool dm_mc02_rng_has_error(const DmMc02Rng *s)
{
    /* CEIS/SEIS are latched interrupt causes, not current error state. */
    return (s->status & DM_MC02_RNG_CURRENT_ERROR_MASK) != 0;
}

static bool dm_mc02_rng_can_generate(const DmMc02Rng *s)
{
    return dm_mc02_rng_is_enabled(s) && !dm_mc02_rng_has_error(s);
}

static bool dm_mc02_rng_has_data(const DmMc02Rng *s)
{
    /* Existing words survive clock errors and a cleared-current seed error.
     * SEIS may remain latched after the hardware has auto-recovered; the HAL
     * clears that latch before consuming the word. */
    return dm_mc02_rng_is_enabled(s) && s->fifo_count != 0 &&
           (s->status & DM_MC02_RNG_SR_SECS) == 0;
}

static void dm_mc02_rng_update_irq(DmMc02Rng *s)
{
    bool level;

    if (!s->irq) {
        return;
    }
    level = (s->regs[DM_MC02_RNG_CR / sizeof(uint32_t)] &
             DM_MC02_RNG_CR_IE) != 0;
    level = level && (dm_mc02_rng_has_data(s) ||
                      (s->status & (DM_MC02_RNG_SR_CEIS |
                                    DM_MC02_RNG_SR_SEIS)) != 0);
    qemu_set_irq(s->irq, level);
}

static void dm_mc02_rng_clear_fifo(DmMc02Rng *s)
{
    memset(s->fifo, 0, sizeof(s->fifo));
    s->fifo_count = 0;
    s->fifo_index = 0;
    s->refill_armed = false;
}

static void dm_mc02_rng_fill_fifo(DmMc02Rng *s)
{
    if (!dm_mc02_rng_can_generate(s)) {
        return;
    }
    dm_mc02_rng_clear_fifo(s);
    for (unsigned i = 0; i < DM_MC02_RNG_FIFO_DEPTH; ++i) {
        s->fifo[i] = rng_next(s);
    }
    s->fifo_count = DM_MC02_RNG_FIFO_DEPTH;
}

static void dm_mc02_rng_refill_after_empty_poll(DmMc02Rng *s)
{
    if (!dm_mc02_rng_can_generate(s) || s->fifo_count != 0) {
        return;
    }
    if (!s->refill_armed) {
        s->refill_armed = true;
        return;
    }
    dm_mc02_rng_fill_fifo(s);
    dm_mc02_rng_update_irq(s);
}

static uint32_t dm_mc02_rng_status(const DmMc02Rng *s)
{
    uint32_t value = s->status & DM_MC02_RNG_SR_ERROR_MASK;

    if (dm_mc02_rng_has_data(s)) {
        value |= DM_MC02_RNG_SR_DRDY;
    }
    return value;
}

static bool dm_mc02_rng_valid_access(hwaddr offset, unsigned size)
{
    return size == sizeof(uint32_t) &&
           offset < DM_MC02_RNG_REGION_SIZE &&
           offset + size <= DM_MC02_RNG_REGION_SIZE &&
           (offset % sizeof(uint32_t)) == 0;
}

uint64_t dm_mc02_rng_read_reg(DmMc02Rng *s, hwaddr offset, unsigned size)
{
    uint32_t value;

    if (!s || !dm_mc02_rng_valid_access(offset, size)) {
        return 0;
    }

    switch (offset) {
    case DM_MC02_RNG_CR:
    case DM_MC02_RNG_HTCR:
        return s->regs[offset / sizeof(uint32_t)];
    case DM_MC02_RNG_SR:
        dm_mc02_rng_refill_after_empty_poll(s);
        return dm_mc02_rng_status(s);
    case DM_MC02_RNG_DR:
        if (!dm_mc02_rng_has_data(s)) {
            return 0;
        }
        value = s->fifo[s->fifo_index];
        s->fifo_index = (s->fifo_index + 1) % DM_MC02_RNG_FIFO_DEPTH;
        --s->fifo_count;
        dm_mc02_rng_update_irq(s);
        return value;
    default:
        return 0;
    }
}

static void dm_mc02_rng_condition_reset(DmMc02Rng *s)
{
    s->prng = s->seed;
    dm_mc02_rng_clear_fifo(s);
    s->status &= ~(DM_MC02_RNG_SR_CECS | DM_MC02_RNG_SR_SECS |
                   DM_MC02_RNG_SR_CEIS | DM_MC02_RNG_SR_SEIS);
}

static void dm_mc02_rng_write_cr(DmMc02Rng *s, uint32_t value)
{
    uint32_t old = s->regs[DM_MC02_RNG_CR / sizeof(uint32_t)];
    uint32_t next = old;
    bool was_enabled = (old & (DM_MC02_RNG_CR_RNGEN |
                               DM_MC02_RNG_CR_CONDRST)) ==
                       DM_MC02_RNG_CR_RNGEN;
    bool was_irq_enabled = (old & DM_MC02_RNG_CR_IE) != 0;
    bool condition_reset;

    next &= ~(DM_MC02_RNG_CR_CONTROL_MASK | DM_MC02_RNG_CR_CONDITION_MASK);
    next |= value & (DM_MC02_RNG_CR_CONTROL_MASK |
                     DM_MC02_RNG_CR_CONDITION_MASK);
    condition_reset = (next & DM_MC02_RNG_CR_CONDITION_MASK) != 0 &&
                      (old & DM_MC02_RNG_CR_CONDITION_MASK) == 0;
    if (!(old & DM_MC02_RNG_CR_CONFIGLOCK)) {
        next &= ~DM_MC02_RNG_CR_CONFIG_MASK;
        next |= value & DM_MC02_RNG_CR_CONFIG_MASK;
        if (value & DM_MC02_RNG_CR_CONFIGLOCK) {
            next |= DM_MC02_RNG_CR_CONFIGLOCK;
        }
    }

    s->regs[DM_MC02_RNG_CR / sizeof(uint32_t)] = next;
    if (condition_reset) {
        dm_mc02_rng_condition_reset(s);
    }
    if (!dm_mc02_rng_is_enabled(s)) {
        dm_mc02_rng_clear_fifo(s);
    } else if ((!was_enabled && dm_mc02_rng_can_generate(s)) ||
               condition_reset ||
               (!was_irq_enabled &&
                (next & DM_MC02_RNG_CR_IE) != 0 && s->fifo_count == 0)) {
        dm_mc02_rng_fill_fifo(s);
    }
    dm_mc02_rng_update_irq(s);
}

void dm_mc02_rng_write_reg(DmMc02Rng *s, hwaddr offset, uint64_t value,
                           unsigned size)
{
    uint32_t value32 = (uint32_t)value;

    if (!s || !dm_mc02_rng_valid_access(offset, size)) {
        return;
    }

    switch (offset) {
    case DM_MC02_RNG_CR:
        dm_mc02_rng_write_cr(s, value32);
        break;
    case DM_MC02_RNG_SR:
        /* CEIS and SEIS are cleared by writing zero, as used by HAL. */
        s->status &= value32 | ~(DM_MC02_RNG_SR_CEIS |
                                  DM_MC02_RNG_SR_SEIS);
        dm_mc02_rng_update_irq(s);
        break;
    case DM_MC02_RNG_HTCR:
        if (!(s->regs[DM_MC02_RNG_CR / sizeof(uint32_t)] &
              DM_MC02_RNG_CR_CONFIGLOCK)) {
            s->regs[DM_MC02_RNG_HTCR / sizeof(uint32_t)] = value32;
        }
        break;
    case DM_MC02_RNG_DR:
    default:
        break;
    }
}

static uint64_t dm_mc02_rng_read(void *opaque, hwaddr offset, unsigned size)
{
    return dm_mc02_rng_read_reg(opaque, offset, size);
}

static void dm_mc02_rng_write(void *opaque, hwaddr offset, uint64_t value,
                              unsigned size)
{
    dm_mc02_rng_write_reg(opaque, offset, value, size);
}

static const MemoryRegionOps dm_mc02_rng_ops = {
    .read = dm_mc02_rng_read,
    .write = dm_mc02_rng_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 4,
    .valid.max_access_size = 4,
};

void dm_mc02_rng_init(DmMc02Rng *state, Object *owner)
{
    memset(state, 0, sizeof(*state));
    dm_mc02_rng_reset(state);
    memory_region_init_io(&state->iomem, owner, &dm_mc02_rng_ops, state,
                          "dm-mc02.rng", DM_MC02_RNG_REGION_SIZE);
}

void dm_mc02_rng_reset(DmMc02Rng *state)
{
    if (state) {
        memset(state->regs, 0, sizeof(state->regs));
        state->seed = DM_MC02_RNG_DEFAULT_SEED;
        state->prng = state->seed;
        dm_mc02_rng_clear_fifo(state);
        state->status = 0;
        dm_mc02_rng_update_irq(state);
    }
}

void dm_mc02_rng_set_irq(DmMc02Rng *state, qemu_irq irq)
{
    if (!state) {
        return;
    }
    if (state->irq && state->irq != irq) {
        qemu_set_irq(state->irq, 0);
    }
    state->irq = irq;
    dm_mc02_rng_update_irq(state);
}
