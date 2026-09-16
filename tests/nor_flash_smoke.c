#include "dm_nor_flash.h"

#include <assert.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

enum {
    STORAGE_SIZE = 128 * 1024,
    PAGE_SIZE = 256,
    SECTOR_SIZE = 4096,
    BLOCK_32K = 32 * 1024,
    BLOCK_64K = 64 * 1024,
};

static void assert_erased(const uint8_t *storage, size_t begin, size_t end)
{
    for (size_t i = begin; i < end; ++i) {
        assert(storage[i] == 0xff);
    }
}

int main(void)
{
    static uint8_t storage[STORAGE_SIZE];
    uint8_t first[4] = {0x0f, 0xf0, 0xaa, 0x55};
    uint8_t second[4] = {0xf0, 0x0f, 0x55, 0xaa};
    uint8_t page_plus_one[PAGE_SIZE + 1];
    DmNorFlash flash;

    memset(storage, 0xff, sizeof(storage));
    memset(page_plus_one, 0, sizeof(page_plus_one));
    assert(dm_nor_flash_init(&flash, storage, sizeof(storage), PAGE_SIZE,
                             SECTOR_SIZE));
    assert(dm_nor_flash_status(&flash) == 0);
    assert(dm_nor_flash_read_byte(&flash, STORAGE_SIZE) == 0xff);

    assert(dm_nor_flash_page_program(&flash, 0, first, sizeof(first)) ==
           DM_NOR_FLASH_WRITE_PROTECTED);
    assert(dm_nor_flash_status(&flash) == 0);
    assert_erased(storage, 0, sizeof(first));

    dm_nor_flash_write_enable(&flash);
    assert(dm_nor_flash_status(&flash) == DM_NOR_FLASH_STATUS_WEL);
    assert(dm_nor_flash_page_program(&flash, PAGE_SIZE - 2, first,
                                     sizeof(first)) == DM_NOR_FLASH_OK);
    assert(dm_nor_flash_status(&flash) == 0);
    assert(storage[PAGE_SIZE - 2] == 0x0f);
    assert(storage[PAGE_SIZE - 1] == 0xf0);
    assert(storage[0] == 0xaa);
    assert(storage[1] == 0x55);
    assert(dm_nor_flash_read_byte(&flash, PAGE_SIZE * 2) == 0xff);

    dm_nor_flash_write_enable(&flash);
    assert(dm_nor_flash_page_program(&flash, PAGE_SIZE - 2, second,
                                     sizeof(second)) == DM_NOR_FLASH_OK);
    assert(storage[PAGE_SIZE - 2] == 0x00);
    assert(storage[PAGE_SIZE - 1] == 0x00);
    assert(storage[0] == 0x00);
    assert(storage[1] == 0x00);

    dm_nor_flash_write_enable(&flash);
    assert(dm_nor_flash_page_program(&flash, 0, page_plus_one,
                                     sizeof(page_plus_one)) ==
           DM_NOR_FLASH_LENGTH_INVALID);
    assert(dm_nor_flash_status(&flash) == DM_NOR_FLASH_STATUS_WEL);
    assert(storage[0] == 0x00);

    flash.write_in_progress = true;
    assert(dm_nor_flash_status(&flash) ==
           (DM_NOR_FLASH_STATUS_WIP | DM_NOR_FLASH_STATUS_WEL));
    assert(dm_nor_flash_page_program(&flash, 0, first, sizeof(first)) ==
           DM_NOR_FLASH_BUSY);
    flash.write_in_progress = false;

    assert(dm_nor_flash_sector_erase(&flash, 0x100) == DM_NOR_FLASH_OK);
    assert(dm_nor_flash_status(&flash) == 0);
    assert_erased(storage, 0, SECTOR_SIZE);
    assert(storage[SECTOR_SIZE] == 0xff);

    dm_nor_flash_write_enable(&flash);
    assert(dm_nor_flash_sector_erase(&flash, STORAGE_SIZE) ==
           DM_NOR_FLASH_OUT_OF_RANGE);
    assert(dm_nor_flash_status(&flash) == DM_NOR_FLASH_STATUS_WEL);

    storage[SECTOR_SIZE] = 0x00;
    dm_nor_flash_reset(&flash);
    assert(dm_nor_flash_status(&flash) == 0);
    assert(storage[SECTOR_SIZE] == 0x00);
    assert(dm_nor_flash_sector_erase(&flash, SECTOR_SIZE) ==
           DM_NOR_FLASH_WRITE_PROTECTED);
    assert(storage[SECTOR_SIZE] == 0x00);

    /* Larger erase commands align to their own block boundary. */
    dm_nor_flash_write_enable(&flash);
    assert(dm_nor_flash_page_program(&flash, BLOCK_32K + 1, first,
                                     sizeof(first)) == DM_NOR_FLASH_OK);
    dm_nor_flash_write_enable(&flash);
    assert(dm_nor_flash_erase(&flash, BLOCK_32K + 1, BLOCK_32K) ==
           DM_NOR_FLASH_OK);
    assert_erased(storage, BLOCK_32K, BLOCK_32K * 2);

    dm_nor_flash_write_enable(&flash);
    assert(dm_nor_flash_page_program(&flash, BLOCK_64K + 1, first,
                                     sizeof(first)) == DM_NOR_FLASH_OK);
    dm_nor_flash_write_enable(&flash);
    assert(dm_nor_flash_erase(&flash, BLOCK_64K + 1, BLOCK_64K) ==
           DM_NOR_FLASH_OK);
    assert_erased(storage, BLOCK_64K, STORAGE_SIZE);

    dm_nor_flash_write_enable(&flash);
    assert(dm_nor_flash_erase(&flash, 0, PAGE_SIZE) ==
           DM_NOR_FLASH_LENGTH_INVALID);
    assert(dm_nor_flash_status(&flash) == DM_NOR_FLASH_STATUS_WEL);

    assert(dm_nor_flash_chip_erase(&flash) == DM_NOR_FLASH_OK);
    assert(dm_nor_flash_status(&flash) == 0);
    assert_erased(storage, 0, STORAGE_SIZE);
    return 0;
}
