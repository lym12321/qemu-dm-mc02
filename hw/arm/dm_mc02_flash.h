/*
 * Minimal STM32H723 FLASH_R peripheral model for the DM-MC02 machine.
 *
 * The memory itself is provided by the machine's ROM region at 0x08000000.
 * The mapped program region is backed by RAM so firmware self-programming and
 * bootloader tests can run without a second device model.  Register writes
 * implement the common unlock/sector-erase path; timing and ECC remain
 * intentionally out of scope.
 */
#ifndef HW_ARM_DM_MC02_FLASH_H
#define HW_ARM_DM_MC02_FLASH_H

#include "exec/memory.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_FLASH_REG_REGION_SIZE 0x400

typedef struct DmMc02Flash {
    MemoryRegion iomem;
    uint8_t regs[DM_MC02_FLASH_REG_REGION_SIZE];
    bool key1_seen;
    bool optkey_seen;
    uint8_t *storage;
    size_t storage_size;
    MemoryRegion *storage_region;
    /* Enabled only while FLASH_CR1.PG is active.  Normal instruction/data
     * reads stay on the RAM-backed region; this overlay makes programming
     * writes obey NOR 1->0 semantics without slowing the hot path. */
    MemoryRegion program_window;
    bool program_enabled;
} DmMc02Flash;

void dm_mc02_flash_init(DmMc02Flash *state, Object *owner,
                        uint8_t *storage, size_t storage_size,
                        MemoryRegion *storage_region);
void dm_mc02_flash_reset(DmMc02Flash *state);
void dm_mc02_flash_set_program_enabled(DmMc02Flash *state, bool enabled);
void dm_mc02_flash_sync_runtime(DmMc02Flash *state);

const VMStateDescription *dm_mc02_flash_vmstate(void);

#endif
