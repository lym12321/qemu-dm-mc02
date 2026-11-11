/* Board-independent synchronous NOR Flash storage semantics. */
#ifndef DM_NOR_FLASH_H
#define DM_NOR_FLASH_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_NOR_FLASH_STATUS_WIP (1u << 0)
#define DM_NOR_FLASH_STATUS_WEL (1u << 1)

typedef enum DmNorFlashResult {
    DM_NOR_FLASH_OK = 0,
    DM_NOR_FLASH_INVALID,
    DM_NOR_FLASH_BUSY,
    DM_NOR_FLASH_WRITE_PROTECTED,
    DM_NOR_FLASH_OUT_OF_RANGE,
    DM_NOR_FLASH_LENGTH_INVALID,
} DmNorFlashResult;

typedef struct DmNorFlash {
    uint8_t *storage;
    size_t storage_size;
    size_t page_size;
    size_t sector_size;
    bool write_enable_latch;
    bool write_in_progress;
} DmNorFlash;

/* The caller owns storage and initializes it to the desired power-on image. */
bool dm_nor_flash_init(DmNorFlash *flash, uint8_t *storage,
                       size_t storage_size, size_t page_size,
                       size_t sector_size);

/* Reset command state while preserving the non-volatile storage contents. */
void dm_nor_flash_reset(DmNorFlash *flash);

void dm_nor_flash_write_enable(DmNorFlash *flash);
uint8_t dm_nor_flash_status(const DmNorFlash *flash);

/* Out-of-range reads model erased bus data, as a memory-mapped NOR window does. */
uint8_t dm_nor_flash_read_byte(const DmNorFlash *flash, size_t address);

/* Page program wraps within the addressed page and applies only 1 -> 0 bits. */
DmNorFlashResult dm_nor_flash_page_program(DmNorFlash *flash, size_t address,
                                           const uint8_t *data, size_t length);

/* Sector erase aligns address down to sector_size and restores 0xff. */
DmNorFlashResult dm_nor_flash_sector_erase(DmNorFlash *flash, size_t address);

/* Erase an aligned block whose size is a whole-number multiple of the
 * configured sector size.  The caller must have set WEL. */
DmNorFlashResult dm_nor_flash_erase(DmNorFlash *flash, size_t address,
                                    size_t erase_size);

/* Erase the complete storage array.  The caller must have set WEL. */
DmNorFlashResult dm_nor_flash_chip_erase(DmNorFlash *flash);

#endif /* DM_NOR_FLASH_H */
