/*
 * Minimal STM32H723 FLASH_R register model for DM-MC02.
 *
 * Register offsets follow FLASH_TypeDef in the STM32H723 CMSIS header.  The
 * model is intentionally fast: operations complete immediately in
 * virtual time, while preserving the register-level unlock and erase rules
 * that bootloader code commonly depends on.
 */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_flash.h"

#define FLASH_ACR             0x00
#define FLASH_KEYR1           0x04
#define FLASH_OPTKEYR         0x08
#define FLASH_CR1             0x0c
#define FLASH_SR1             0x10
#define FLASH_CCR1            0x14
#define FLASH_OPTCR           0x18
#define FLASH_OPTSR_CUR       0x1c
#define FLASH_OPTSR_PRG       0x20
#define FLASH_OPTCCR          0x24
#define FLASH_PRAR_CUR1       0x28
#define FLASH_PRAR_PRG1       0x2c
#define FLASH_SCAR_CUR1       0x30
#define FLASH_SCAR_PRG1       0x34
#define FLASH_WPSN_CUR1       0x38
#define FLASH_WPSN_PRG1       0x3c
#define FLASH_BOOT_CUR        0x40
#define FLASH_BOOT_PRG        0x44
#define FLASH_CRCCR1          0x50
#define FLASH_CRCSADD1        0x54
#define FLASH_CRCEADD1        0x58
#define FLASH_CRCDATA         0x5c
#define FLASH_ECC_FA1         0x60
#define FLASH_OPTSR2_CUR      0x70
#define FLASH_OPTSR2_PRG      0x74

#define FLASH_KEY1            0x45670123u
#define FLASH_KEY2            0xcdef89abu
#define FLASH_OPTKEY1         0x08192a3bu
#define FLASH_OPTKEY2         0x4c5d6e7fu

#define FLASH_CR_LOCK         (1u << 0)
#define FLASH_CR_PG           (1u << 1)
#define FLASH_OPTCR_OPTLOCK   (1u << 0)
#define FLASH_CR_SER          (1u << 2)
#define FLASH_CR_START        (1u << 7)
#define FLASH_CR_SNB_SHIFT    8
#define FLASH_CR_SNB_MASK     (0x7u << FLASH_CR_SNB_SHIFT)
#define FLASH_SR_EOP          (1u << 16)
#define FLASH_SR_WRPERR      (1u << 17)
#define FLASH_SR_PGSERR      (1u << 18)
#define FLASH_SR_ECCD        (1u << 21)
#define DM_MC02_FLASH_SECTOR_SIZE (128u * 1024u)

static uint32_t dm_mc02_flash_load(const uint8_t *regs, hwaddr offset,
                                   unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; ++i) {
        value |= (uint32_t)regs[offset + i] << (i * 8);
    }
    return value;
}

static void dm_mc02_flash_store(uint8_t *regs, hwaddr offset, uint32_t value,
                                unsigned size)
{
    for (unsigned i = 0; i < size; ++i) {
        regs[offset + i] = value >> (i * 8);
    }
}

static uint64_t dm_mc02_flash_program_read(void *opaque, hwaddr offset,
                                           unsigned size)
{
    DmMc02Flash *s = opaque;
    uint64_t value = 0;

    if (size > sizeof(value) || offset > s->storage_size ||
        size > s->storage_size - offset) {
        return 0;
    }
    for (unsigned i = 0; i < size; ++i) {
        value |= (uint64_t)s->storage[offset + i] << (i * 8);
    }
    return value;
}

static void dm_mc02_flash_program_write(void *opaque, hwaddr offset,
                                        uint64_t value, unsigned size)
{
    DmMc02Flash *s = opaque;
    bool invalid = false;

    if (size > sizeof(value) || offset > s->storage_size ||
        size > s->storage_size - offset) {
        return;
    }
    for (unsigned i = 0; i < size; ++i) {
        uint8_t requested = (uint8_t)(value >> (i * 8));
        uint8_t old = s->storage[offset + i];

        if ((requested & (uint8_t)~old) != 0) {
            invalid = true;
        }
        s->storage[offset + i] = old & requested;
    }
    if (invalid) {
        uint32_t sr = dm_mc02_flash_load(s->regs, FLASH_SR1,
                                          sizeof(uint32_t));

        dm_mc02_flash_store(s->regs, FLASH_SR1, sr | FLASH_SR_PGSERR,
                            sizeof(uint32_t));
    }
}

static const MemoryRegionOps dm_mc02_flash_program_ops = {
    .read = dm_mc02_flash_program_read,
    .write = dm_mc02_flash_program_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 8,
};

static void dm_mc02_flash_erase_sector(DmMc02Flash *s, uint32_t cr)
{
    unsigned sector = (cr & FLASH_CR_SNB_MASK) >> FLASH_CR_SNB_SHIFT;
    size_t offset = (size_t)sector * DM_MC02_FLASH_SECTOR_SIZE;

    if (!s->storage || offset >= s->storage_size || sector >= 8) {
        return;
    }
    memset(s->storage + offset, 0xff,
           MIN((size_t)DM_MC02_FLASH_SECTOR_SIZE,
               s->storage_size - offset));
}

static uint64_t dm_mc02_flash_read(void *opaque, hwaddr offset,
                                   unsigned size)
{
    DmMc02Flash *s = opaque;

    if (size > sizeof(uint32_t) || offset >= DM_MC02_FLASH_REG_REGION_SIZE ||
        size > DM_MC02_FLASH_REG_REGION_SIZE - offset) {
        return 0;
    }
    return dm_mc02_flash_load(s->regs, offset, size);
}

static void dm_mc02_flash_write(void *opaque, hwaddr offset, uint64_t value,
                                unsigned size)
{
    DmMc02Flash *s = opaque;
    uint32_t value32 = (uint32_t)value;

    if (size > sizeof(uint32_t) || offset >= DM_MC02_FLASH_REG_REGION_SIZE ||
        size > DM_MC02_FLASH_REG_REGION_SIZE - offset) {
        return;
    }

    if (size == sizeof(uint32_t) && offset == FLASH_KEYR1) {
        if (value32 == FLASH_KEY1) {
            s->key1_seen = true;
        } else if (value32 == FLASH_KEY2 && s->key1_seen) {
            uint32_t cr = dm_mc02_flash_load(s->regs, FLASH_CR1,
                                             sizeof(uint32_t));
            dm_mc02_flash_store(s->regs, FLASH_CR1, cr & ~FLASH_CR_LOCK,
                                sizeof(uint32_t));
            s->key1_seen = false;
        } else {
            s->key1_seen = false;
        }
        /* KEYR is write-only on the device. */
        return;
    }

    if (size == sizeof(uint32_t) && offset == FLASH_OPTKEYR) {
        if (value32 == FLASH_OPTKEY1) {
            s->optkey_seen = true;
        } else if (value32 == FLASH_OPTKEY2 && s->optkey_seen) {
            uint32_t optcr = dm_mc02_flash_load(s->regs, FLASH_OPTCR,
                                                sizeof(uint32_t));
            dm_mc02_flash_store(s->regs, FLASH_OPTCR,
                                optcr & ~FLASH_OPTCR_OPTLOCK,
                                sizeof(uint32_t));
            s->optkey_seen = false;
        } else {
            s->optkey_seen = false;
        }
        return;
    }

    if (size == sizeof(uint32_t) && offset == FLASH_SR1) {
        /* Status flags are cleared by writing one; there are no synthetic
         * error/busy flags in this boot model. */
        uint32_t old = dm_mc02_flash_load(s->regs, FLASH_SR1,
                                          sizeof(uint32_t));
        dm_mc02_flash_store(s->regs, FLASH_SR1, old & ~value32,
                            sizeof(uint32_t));
        return;
    }

    if (size == sizeof(uint32_t) && offset == FLASH_CR1) {
        uint32_t old = dm_mc02_flash_load(s->regs, FLASH_CR1,
                                          sizeof(uint32_t));
        uint32_t next = value32;

        /* LOCK is sticky until the documented key sequence. */
        if (old & FLASH_CR_LOCK) {
            next |= FLASH_CR_LOCK;
        }
        dm_mc02_flash_store(s->regs, FLASH_CR1, next, sizeof(uint32_t));
        dm_mc02_flash_sync_runtime(s);
        if (!(next & FLASH_CR_LOCK) && (next & FLASH_CR_SER) &&
            (next & FLASH_CR_START)) {
            dm_mc02_flash_erase_sector(s, next);
            uint32_t sr = dm_mc02_flash_load(s->regs, FLASH_SR1,
                                             sizeof(uint32_t));
            dm_mc02_flash_store(s->regs, FLASH_SR1, sr | FLASH_SR_EOP,
                                sizeof(uint32_t));
            dm_mc02_flash_store(s->regs, FLASH_CR1,
                                next & ~FLASH_CR_START, sizeof(uint32_t));
        }
        return;
    }

    /* All other offsets, including reserved/unknown offsets, are harmless
     * storage. */
    dm_mc02_flash_store(s->regs, offset, value32, size);
}

static const MemoryRegionOps dm_mc02_flash_ops = {
    .read = dm_mc02_flash_read,
    .write = dm_mc02_flash_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 4,
};

void dm_mc02_flash_init(DmMc02Flash *state, Object *owner,
                        uint8_t *storage, size_t storage_size,
                        MemoryRegion *storage_region)
{
    memset(state, 0, sizeof(*state));
    state->storage = storage;
    state->storage_size = storage_size;
    state->storage_region = storage_region;

    /* Reset values relevant to HAL_FLASH_Unlock/Lock and option handling. */
    dm_mc02_flash_store(state->regs, FLASH_CR1, FLASH_CR_LOCK,
                        sizeof(uint32_t));
    dm_mc02_flash_store(state->regs, FLASH_OPTCR, FLASH_OPTCR_OPTLOCK,
                        sizeof(uint32_t));

    memory_region_init_io(&state->iomem, owner, &dm_mc02_flash_ops, state,
                          "dm-mc02.flash-regs",
                          DM_MC02_FLASH_REG_REGION_SIZE);
    memory_region_init_io(&state->program_window, owner,
                          &dm_mc02_flash_program_ops, state,
                          "dm-mc02.flash-program-window", storage_size);
    memory_region_set_enabled(&state->program_window, false);
    memory_region_set_readonly(storage_region, true);
}

void dm_mc02_flash_set_program_enabled(DmMc02Flash *state, bool enabled)
{
    if (!state) {
        return;
    }
    state->program_enabled = enabled;
    if (state->storage_region) {
        /* Keep the RAM-backed execution region read-only at all times.  The
         * higher-priority IO overlay is enabled only for PG writes. */
        memory_region_set_readonly(state->storage_region, true);
    }
    memory_region_set_enabled(&state->program_window, enabled);
}

void dm_mc02_flash_sync_runtime(DmMc02Flash *state)
{
    uint32_t cr;

    if (!state) {
        return;
    }
    cr = dm_mc02_flash_load(state->regs, FLASH_CR1, sizeof(cr));
    dm_mc02_flash_set_program_enabled(state,
                                      !(cr & FLASH_CR_LOCK) &&
                                      (cr & FLASH_CR_PG));
}

void dm_mc02_flash_reset(DmMc02Flash *state)
{
    if (!state) {
        return;
    }

    memset(state->regs, 0, sizeof(state->regs));
    state->key1_seen = false;
    state->optkey_seen = false;
    dm_mc02_flash_store(state->regs, FLASH_CR1, FLASH_CR_LOCK,
                        sizeof(uint32_t));
    dm_mc02_flash_store(state->regs, FLASH_OPTCR, FLASH_OPTCR_OPTLOCK,
                        sizeof(uint32_t));
    /* A reset must never leave the NOR write overlay active, even though the
     * backing Flash contents intentionally survive a warm system reset. */
    dm_mc02_flash_set_program_enabled(state, false);
}
