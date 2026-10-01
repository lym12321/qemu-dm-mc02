/* Minimal STM32H723 SYSCFG model: safe little-endian register storage. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_syscfg.h"

#define SYSCFG_EXTICR1 0x08

static bool syscfg_exticr_access(hwaddr offset, unsigned size)
{
    return offset < SYSCFG_EXTICR1 + 0x10 &&
           offset + size > SYSCFG_EXTICR1;
}

static uint64_t dm_mc02_syscfg_read(void *opaque, hwaddr offset,
                                    unsigned size)
{
    DmMc02Syscfg *s = opaque;
    uint32_t value = 0;

    if (size > sizeof(value) || offset >= DM_MC02_SYSCFG_REGION_SIZE ||
        size > DM_MC02_SYSCFG_REGION_SIZE - offset) {
        return 0;
    }
    for (unsigned i = 0; i < size; ++i) {
        value |= (uint32_t)s->regs[offset + i] << (i * 8);
    }
    return value;
}

static void dm_mc02_syscfg_write(void *opaque, hwaddr offset, uint64_t value,
                                  unsigned size)
{
    DmMc02Syscfg *s = opaque;
    bool exticr_changed = false;

    if (size > sizeof(uint32_t) || offset >= DM_MC02_SYSCFG_REGION_SIZE ||
        size > DM_MC02_SYSCFG_REGION_SIZE - offset) {
        return;
    }
    for (unsigned i = 0; i < size; ++i) {
        uint8_t next = value >> (i * 8);

        exticr_changed |= s->regs[offset + i] != next;
        s->regs[offset + i] = next;
    }
    if (exticr_changed && syscfg_exticr_access(offset, size) &&
        s->changed) {
        s->changed(s->changed_opaque);
    }
}

static const MemoryRegionOps dm_mc02_syscfg_ops = {
    .read = dm_mc02_syscfg_read,
    .write = dm_mc02_syscfg_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_syscfg_init(DmMc02Syscfg *state, Object *owner)
{
    memset(state, 0, sizeof(*state));
    memory_region_init_io(&state->iomem, owner, &dm_mc02_syscfg_ops, state,
                          "dm-mc02.syscfg", DM_MC02_SYSCFG_REGION_SIZE);
}

void dm_mc02_syscfg_set_changed(DmMc02Syscfg *state,
                                DmMc02SyscfgChanged *changed,
                                void *opaque)
{
    state->changed = changed;
    state->changed_opaque = opaque;
}

void dm_mc02_syscfg_reset(DmMc02Syscfg *state)
{
    if (state) {
        memset(state->regs, 0, sizeof(state->regs));
    }
}

unsigned dm_mc02_syscfg_get_exti_port(const DmMc02Syscfg *state,
                                      unsigned line)
{
    unsigned reg;
    unsigned shift;

    if (!state || line >= 16) {
        return 0;
    }
    reg = SYSCFG_EXTICR1 + (line / 4) * 4;
    shift = (line % 4) * 4;
    return (ldl_le_p(state->regs + reg) >> shift) & 0xfu;
}
