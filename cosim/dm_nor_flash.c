/* Board-independent synchronous NOR Flash storage semantics. */
#include "dm_nor_flash.h"

#include <string.h>

bool dm_nor_flash_init(DmNorFlash *flash, uint8_t *storage,
                       size_t storage_size, size_t page_size,
                       size_t sector_size)
{
    if (!flash || !storage || storage_size == 0 || page_size == 0 ||
        sector_size == 0 || sector_size < page_size ||
        storage_size % page_size != 0 || storage_size % sector_size != 0) {
        return false;
    }
    flash->storage = storage;
    flash->storage_size = storage_size;
    flash->page_size = page_size;
    flash->sector_size = sector_size;
    dm_nor_flash_reset(flash);
    return true;
}

void dm_nor_flash_reset(DmNorFlash *flash)
{
    if (!flash) {
        return;
    }
    flash->write_enable_latch = false;
    flash->write_in_progress = false;
}

void dm_nor_flash_write_enable(DmNorFlash *flash)
{
    if (flash && !flash->write_in_progress) {
        flash->write_enable_latch = true;
    }
}

uint8_t dm_nor_flash_status(const DmNorFlash *flash)
{
    if (!flash) {
        return 0;
    }
    return (flash->write_in_progress ? DM_NOR_FLASH_STATUS_WIP : 0) |
           (flash->write_enable_latch ? DM_NOR_FLASH_STATUS_WEL : 0);
}

uint8_t dm_nor_flash_read_byte(const DmNorFlash *flash, size_t address)
{
    if (!flash || !flash->storage || address >= flash->storage_size) {
        return 0xff;
    }
    return flash->storage[address];
}

DmNorFlashResult dm_nor_flash_page_program(DmNorFlash *flash, size_t address,
                                           const uint8_t *data, size_t length)
{
    size_t page_base;
    size_t page_offset;

    if (!flash || !flash->storage || !data) {
        return DM_NOR_FLASH_INVALID;
    }
    if (length == 0 || length > flash->page_size) {
        return DM_NOR_FLASH_LENGTH_INVALID;
    }
    if (address >= flash->storage_size) {
        return DM_NOR_FLASH_OUT_OF_RANGE;
    }
    if (flash->write_in_progress) {
        return DM_NOR_FLASH_BUSY;
    }
    if (!flash->write_enable_latch) {
        return DM_NOR_FLASH_WRITE_PROTECTED;
    }

    page_base = address - address % flash->page_size;
    page_offset = address % flash->page_size;
    flash->write_in_progress = true;
    for (size_t i = 0; i < length; ++i) {
        size_t target = page_base + (page_offset + i) % flash->page_size;

        flash->storage[target] &= data[i];
    }
    flash->write_in_progress = false;
    flash->write_enable_latch = false;
    return DM_NOR_FLASH_OK;
}

DmNorFlashResult dm_nor_flash_erase(DmNorFlash *flash, size_t address,
                                    size_t erase_size)
{
    size_t erase_base;

    if (!flash || !flash->storage) {
        return DM_NOR_FLASH_INVALID;
    }
    if (erase_size == 0 || erase_size < flash->sector_size ||
        erase_size % flash->sector_size != 0 ||
        flash->storage_size % erase_size != 0) {
        return DM_NOR_FLASH_LENGTH_INVALID;
    }
    if (address >= flash->storage_size) {
        return DM_NOR_FLASH_OUT_OF_RANGE;
    }
    if (flash->write_in_progress) {
        return DM_NOR_FLASH_BUSY;
    }
    if (!flash->write_enable_latch) {
        return DM_NOR_FLASH_WRITE_PROTECTED;
    }

    erase_base = address - address % erase_size;
    flash->write_in_progress = true;
    memset(flash->storage + erase_base, 0xff, erase_size);
    flash->write_in_progress = false;
    flash->write_enable_latch = false;
    return DM_NOR_FLASH_OK;
}

DmNorFlashResult dm_nor_flash_sector_erase(DmNorFlash *flash, size_t address)
{
    return dm_nor_flash_erase(flash, address,
                              flash ? flash->sector_size : 0);
}

DmNorFlashResult dm_nor_flash_chip_erase(DmNorFlash *flash)
{
    return dm_nor_flash_erase(flash, 0, flash ? flash->storage_size : 0);
}
