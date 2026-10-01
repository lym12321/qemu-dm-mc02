/* Minimal STM32H723 DBGMCU model: safe little-endian register storage. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_dbgmcu.h"

static uint64_t dm_mc02_dbgmcu_read(void *opaque, hwaddr offset,
                                    unsigned size)
{
    DmMc02Dbgmcu *s = opaque;
    uint32_t value = 0;

    if (size != 1 && size != 2 && size != 4) {
        return 0;
    }
    if (offset >= DM_MC02_DBGMCU_REGION_SIZE ||
        size > DM_MC02_DBGMCU_REGION_SIZE - offset) {
        return 0;
    }
    for (unsigned i = 0; i < size; ++i) {
        hwaddr byte_offset = offset + i;
        uint8_t byte = byte_offset < sizeof(uint32_t) ?
                       (DM_MC02_DBGMCU_IDCODE_VALUE >> (byte_offset * 8)) :
                       s->regs[byte_offset];

        value |= (uint32_t)byte << (i * 8);
    }
    return value;
}

static void dm_mc02_dbgmcu_write(void *opaque, hwaddr offset, uint64_t value,
                                 unsigned size)
{
    DmMc02Dbgmcu *s = opaque;

    if (size != 1 && size != 2 && size != 4) {
        return;
    }
    if (offset >= DM_MC02_DBGMCU_REGION_SIZE ||
        size > DM_MC02_DBGMCU_REGION_SIZE - offset) {
        return;
    }
    for (unsigned i = 0; i < size; ++i) {
        /* IDCODE is a fixed, read-only silicon identity. */
        if (offset + i >= sizeof(uint32_t)) {
            s->regs[offset + i] = value >> (i * 8);
        }
    }
}

static const MemoryRegionOps dm_mc02_dbgmcu_ops = {
    .read = dm_mc02_dbgmcu_read,
    .write = dm_mc02_dbgmcu_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_dbgmcu_init(DmMc02Dbgmcu *state, Object *owner)
{
    memset(state->regs, 0, sizeof(state->regs));
    memory_region_init_io(&state->iomem, owner, &dm_mc02_dbgmcu_ops, state,
                          "dm-mc02.dbgmcu", DM_MC02_DBGMCU_REGION_SIZE);
}

void dm_mc02_dbgmcu_reset(DmMc02Dbgmcu *state)
{
    if (state) {
        memset(state->regs, 0, sizeof(state->regs));
    }
}
