/*
 * Minimal STM32H723 FMC_R register model for DM-MC02.
 *
 * FMC_Bank1->BTCR[0] is at offset 0x00.  The complete 0x400-byte register
 * aperture is backed by harmless storage so firmware probing reserved or
 * currently unsupported registers cannot cause an emulation fault.
 */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_fmc.h"

#define FMC_BTCR0 0x00

static uint32_t dm_mc02_fmc_load(const uint8_t *regs, hwaddr offset,
                                 unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; ++i) {
        value |= (uint32_t)regs[offset + i] << (i * 8);
    }
    return value;
}

static void dm_mc02_fmc_store(uint8_t *regs, hwaddr offset, uint32_t value,
                              unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        regs[offset + i] = value >> (i * 8);
    }
}

static uint64_t dm_mc02_fmc_read(void *opaque, hwaddr offset, unsigned size)
{
    DmMc02Fmc *s = opaque;

    if (size > sizeof(uint32_t) || offset >= DM_MC02_FMC_REG_REGION_SIZE ||
        size > DM_MC02_FMC_REG_REGION_SIZE - offset) {
        return 0;
    }
    return dm_mc02_fmc_load(s->regs, offset, size);
}

static void dm_mc02_fmc_write(void *opaque, hwaddr offset, uint64_t value,
                               unsigned size)
{
    DmMc02Fmc *s = opaque;

    if (size > sizeof(uint32_t) || offset >= DM_MC02_FMC_REG_REGION_SIZE ||
        size > DM_MC02_FMC_REG_REGION_SIZE - offset) {
        return;
    }
    dm_mc02_fmc_store(s->regs, offset, (uint32_t)value, size);
}

static const MemoryRegionOps dm_mc02_fmc_ops = {
    .read = dm_mc02_fmc_read,
    .write = dm_mc02_fmc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_fmc_init(DmMc02Fmc *state, Object *owner)
{
    memset(state, 0, sizeof(*state));

    /* Keep the named register explicit as a reminder of the CMSIS layout. */
    (void)FMC_BTCR0;
    memory_region_init_io(&state->iomem, owner, &dm_mc02_fmc_ops, state,
                          "dm-mc02.fmc-regs",
                          DM_MC02_FMC_REG_REGION_SIZE);
}

void dm_mc02_fmc_reset(DmMc02Fmc *state)
{
    if (state) {
        memset(state->regs, 0, sizeof(state->regs));
    }
}
