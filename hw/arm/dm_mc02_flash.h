/*
 * Minimal STM32H723 FLASH_R peripheral model for the DM-MC02 machine.
 *
 * NVM bytes belong to the SoC's RAM-backed, read-only execution region. This
 * controller borrows it for unlock, sector erase and HAL-style 256-bit word
 * programming; timing, force-write and ECC remain out of scope.
 */
#ifndef HW_ARM_DM_MC02_FLASH_H
#define HW_ARM_DM_MC02_FLASH_H

#include "exec/memory.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_FLASH_REG_REGION_SIZE 0x400
#define DM_MC02_FLASH_WORD_SIZE 32

typedef struct DmMc02Flash {
    MemoryRegion iomem;
    uint8_t regs[DM_MC02_FLASH_REG_REGION_SIZE];
    bool key1_seen;
    bool optkey_seen;
    uint8_t *storage;
    size_t storage_size;
    MemoryRegion *storage_region;
    /* Enabled only while FLASH_CR1.PG is active.  Normal instruction/data
     * reads stay on the RAM-backed region; the overlay buffers program
     * writes until one complete flash word can be committed. */
    MemoryRegion program_window;
    bool program_enabled;
    /* HAL_FLASH_Program() writes an aligned H723 flash word as eight
     * consecutive 32-bit stores.  These bytes are not committed NVM until
     * the whole word is present.  Unlike runtime wiring, this pending
     * producer state belongs in component VMState. */
    uint32_t program_address;
    uint8_t program_count;
    uint8_t program_data[DM_MC02_FLASH_WORD_SIZE];
} DmMc02Flash;

void dm_mc02_flash_init(DmMc02Flash *state, Object *owner,
                        uint8_t *storage, size_t storage_size,
                        MemoryRegion *storage_region);
void dm_mc02_flash_reset(DmMc02Flash *state);
void dm_mc02_flash_set_program_enabled(DmMc02Flash *state, bool enabled);
void dm_mc02_flash_sync_runtime(DmMc02Flash *state);

const VMStateDescription *dm_mc02_flash_vmstate(void);

#endif
