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
#include "qemu/log.h"

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
#define FLASH_CR_FW           (1u << 6)
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

static void dm_mc02_flash_clear_program(DmMc02Flash *s)
{
    s->program_address = 0;
    s->program_count = 0;
    memset(s->program_data, 0xff, sizeof(s->program_data));
}

static void dm_mc02_flash_reject_program(DmMc02Flash *s, const char *reason)
{
    uint32_t sr = dm_mc02_flash_load(s->regs, FLASH_SR1, sizeof(sr));

    /* The modeled programming subset uses PGSERR for rejected sequences;
     * it does not claim silicon error classification for unsupported FW,
     * bus widths, ECC or programming an already programmed flash word. */
    qemu_log_mask(LOG_GUEST_ERROR, "dm-mc02 Flash: %s\n", reason);
    dm_mc02_flash_clear_program(s);
    dm_mc02_flash_store(s->regs, FLASH_SR1, sr | FLASH_SR_PGSERR, sizeof(sr));
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
    uint32_t sr = dm_mc02_flash_load(s->regs, FLASH_SR1, sizeof(sr));

    if (sr & FLASH_SR_PGSERR) {
        return;
    }
    /* ST stm32h7xx_hal_flash.c HAL_FLASH_Program(), H72x/3x branch:
     * one 256-bit-aligned flash word, eight consecutive 32-bit stores.
     * Other write-buffer/force-write sequences are deliberately rejected. */
    if (size != sizeof(uint32_t) || (offset & 3) ||
        offset > s->storage_size || size > s->storage_size - offset) {
        dm_mc02_flash_reject_program(s, "unsupported or unaligned bus write");
        return;
    }
    if (!s->program_count) {
        if ((offset & (DM_MC02_FLASH_WORD_SIZE - 1)) ||
            DM_MC02_FLASH_WORD_SIZE > s->storage_size - offset) {
            dm_mc02_flash_reject_program(s, "flash word is not 256-bit aligned");
            return;
        }
        s->program_address = offset;
    } else if (offset != s->program_address + s->program_count) {
        dm_mc02_flash_reject_program(s, "nonconsecutive or crossing flash word");
        return;
    }
    stl_le_p(s->program_data + s->program_count, value);
    s->program_count += sizeof(uint32_t);
    if (s->program_count != DM_MC02_FLASH_WORD_SIZE) {
        return;
    }

    /* Admit erased words only. This also rejects every 0->1 request before
     * changing NVM. No ECC is modeled, so repeated programming is rejected
     * rather than pretending that additional 1->0 writes are supported.
     * Without ECC metadata, an earlier all-0xff program is indistinguishable
     * from an erased word. */
    for (unsigned i = 0; i < DM_MC02_FLASH_WORD_SIZE; ++i) {
        if (s->storage[s->program_address + i] != 0xff) {
            dm_mc02_flash_reject_program(s, "programming would require erase");
            return;
        }
    }
    memcpy(s->storage + s->program_address, s->program_data,
           DM_MC02_FLASH_WORD_SIZE);
    dm_mc02_flash_clear_program(s);
    dm_mc02_flash_store(s->regs, FLASH_SR1, sr | FLASH_SR_EOP, sizeof(sr));
}

static const MemoryRegionOps dm_mc02_flash_program_ops = {
    .read = dm_mc02_flash_program_read,
    .write = dm_mc02_flash_program_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid.min_access_size = 1,
    .valid.max_access_size = 8,
    .valid.unaligned = true,
    .impl.min_access_size = 1,
    .impl.max_access_size = 8,
    .impl.unaligned = true,
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

    /* The modeled control path uses the HAL's aligned 32-bit accesses.
     * Smaller writes must not bypass LOCK or runtime overlay projection. */
    const hwaddr command_registers[] = {
        FLASH_KEYR1, FLASH_OPTKEYR, FLASH_CR1, FLASH_CCR1,
    };
    for (unsigned i = 0; i < ARRAY_SIZE(command_registers); ++i) {
        hwaddr reg = command_registers[i];

        if (offset < reg + sizeof(uint32_t) && offset + size > reg &&
            (offset != reg || size != sizeof(uint32_t))) {
            qemu_log_mask(LOG_UNIMP,
                          "dm-mc02 Flash: unsupported partial command write\n");
            return;
        }
    }

    if (size == sizeof(uint32_t) && offset == FLASH_KEYR1) {
        if (value32 == FLASH_KEY1) {
            s->key1_seen = true;
        } else if (value32 == FLASH_KEY2 && s->key1_seen) {
            uint32_t cr = dm_mc02_flash_load(s->regs, FLASH_CR1,
                                             sizeof(uint32_t));
            dm_mc02_flash_store(s->regs, FLASH_CR1, cr & ~FLASH_CR_LOCK,
                                sizeof(uint32_t));
            dm_mc02_flash_sync_runtime(s);
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

    if (offset >= FLASH_SR1 && offset < FLASH_SR1 + sizeof(uint32_t)) {
        /* SR1 is read-only. ST HAL __HAL_FLASH_CLEAR_FLAG_BANK1 writes
         * FLASH_CCR1, not FLASH_SR1. */
        return;
    }

    if (size == sizeof(uint32_t) && offset == FLASH_CCR1) {
        uint32_t old = dm_mc02_flash_load(s->regs, FLASH_SR1,
                                          sizeof(uint32_t));
        dm_mc02_flash_store(s->regs, FLASH_SR1,
                            old & ~(value32 & (FLASH_SR_EOP |
                                              FLASH_SR_PGSERR)),
                            sizeof(uint32_t));
        return;
    }

    if (size == sizeof(uint32_t) && offset == FLASH_CR1) {
        uint32_t old = dm_mc02_flash_load(s->regs, FLASH_CR1,
                                          sizeof(uint32_t));
        uint32_t next = value32;

        /* A locked control register cannot enable programming or erase. */
        if (old & FLASH_CR_LOCK) {
            return;
        }
        if ((next & FLASH_CR_FW) ||
            ((next & FLASH_CR_PG) && (next & FLASH_CR_SER))) {
            dm_mc02_flash_reject_program(s, "unsupported FW or PG+SER sequence");
            return;
        }
        if (s->program_count &&
            ((next & FLASH_CR_LOCK) || !(next & FLASH_CR_PG))) {
            dm_mc02_flash_reject_program(s, "incomplete flash word discarded");
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
    dm_mc02_flash_clear_program(state);

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
    dm_mc02_flash_clear_program(state);
    dm_mc02_flash_store(state->regs, FLASH_CR1, FLASH_CR_LOCK,
                        sizeof(uint32_t));
    dm_mc02_flash_store(state->regs, FLASH_OPTCR, FLASH_OPTCR_OPTLOCK,
                        sizeof(uint32_t));
    /* A reset must never leave the NOR write overlay active, even though the
     * backing Flash contents intentionally survive a warm system reset. */
    dm_mc02_flash_set_program_enabled(state, false);
}
