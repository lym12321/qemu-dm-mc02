#include "dm_mc02_spi_nor.h"

#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

enum {
    STORAGE_SIZE = 8192,
    PAGE_SIZE = 256,
    SECTOR_SIZE = 4096,
};

static uint8_t transfer(DmMc02SpiTarget *target, uint8_t value)
{
    return target->transfer(target->opaque, value, 1234);
}

static void address(DmMc02SpiTarget *target, unsigned value)
{
    transfer(target, (uint8_t)(value >> 16));
    transfer(target, (uint8_t)(value >> 8));
    transfer(target, (uint8_t)value);
}

static void select_target(DmMc02SpiTarget *target, bool selected,
                          uint32_t mask)
{
    target->select(target->opaque, selected, mask);
}

static void assert_erased(const uint8_t *storage, size_t begin, size_t end)
{
    for (size_t i = begin; i < end; ++i) {
        assert(storage[i] == 0xff);
    }
}

int main(void)
{
    uint8_t storage[STORAGE_SIZE];
    uint8_t program[4] = { 0x0f, 0xf0, 0xaa, 0x55 };
    uint8_t page_plus_one[PAGE_SIZE + 1];
    const uint8_t jedec[3] = { 0xaa, 0xbb, 0xcc };
    DmNorFlash flash;
    DmSpiNorFlash adapter;
    DmMc02SpiTarget target;

    memset(storage, 0xff, sizeof(storage));
    memset(page_plus_one, 0, sizeof(page_plus_one));
    assert(dm_nor_flash_init(&flash, storage, sizeof(storage), PAGE_SIZE,
                             SECTOR_SIZE));
    assert(dm_spi_nor_flash_init(&adapter, &flash, 1u, jedec));
    target = dm_spi_nor_flash_target(&adapter);

    select_target(&target, true, 1u);
    assert(transfer(&target, 0x9f) == 0);
    assert(transfer(&target, 0) == 0xaa);
    assert(transfer(&target, 0) == 0xbb);
    assert(transfer(&target, 0) == 0xcc);
    assert(transfer(&target, 0) == 0xff);
    select_target(&target, false, 0);

    select_target(&target, true, 1u);
    transfer(&target, 0x06);
    select_target(&target, false, 0);
    select_target(&target, true, 1u);
    assert(transfer(&target, 0x05) == 0);
    assert(transfer(&target, 0) == DM_NOR_FLASH_STATUS_WEL);
    select_target(&target, false, 0);

    select_target(&target, true, 1u);
    transfer(&target, 0x02);
    address(&target, PAGE_SIZE - 2);
    for (size_t i = 0; i < sizeof(program); ++i) {
        transfer(&target, program[i]);
    }
    select_target(&target, false, 0);
    assert(dm_spi_nor_flash_last_result(&adapter) == DM_NOR_FLASH_OK);
    assert(storage[PAGE_SIZE - 2] == 0x0f);
    assert(storage[PAGE_SIZE - 1] == 0xf0);
    assert(storage[0] == 0xaa);
    assert(storage[1] == 0x55);

    select_target(&target, true, 1u);
    transfer(&target, 0x03);
    address(&target, PAGE_SIZE - 2);
    assert(transfer(&target, 0) == 0x0f);
    assert(transfer(&target, 0) == 0xf0);
    /* Page wrapping applies to PAGE PROGRAM; normal READ increments through
     * the linear Flash address space. */
    assert(transfer(&target, 0) == 0xff);
    assert(transfer(&target, 0) == 0xff);
    select_target(&target, false, 0);

    select_target(&target, true, 1u);
    transfer(&target, 0x06);
    select_target(&target, false, 0);
    select_target(&target, true, 1u);
    transfer(&target, 0x0b);
    address(&target, PAGE_SIZE - 2);
    assert(transfer(&target, 0) == 0);
    assert(transfer(&target, 0) == 0x0f);
    assert(transfer(&target, 0) == 0xf0);
    select_target(&target, false, 0);

    select_target(&target, true, 1u);
    transfer(&target, 0x06);
    select_target(&target, false, 0);
    select_target(&target, true, 1u);
    transfer(&target, 0x02);
    address(&target, 0x100);
    for (size_t i = 0; i < sizeof(page_plus_one); ++i) {
        transfer(&target, page_plus_one[i]);
    }
    select_target(&target, false, 0);
    assert(dm_spi_nor_flash_last_result(&adapter) ==
           DM_NOR_FLASH_LENGTH_INVALID);
    select_target(&target, true, 1u);
    assert(transfer(&target, 0x05) == 0);
    assert(transfer(&target, 0) == DM_NOR_FLASH_STATUS_WEL);
    select_target(&target, false, 0);
    assert_erased(storage, 0x100, 0x104);

    select_target(&target, true, 1u);
    transfer(&target, 0x20);
    address(&target, 0x100);
    select_target(&target, false, 0);
    assert(dm_spi_nor_flash_last_result(&adapter) == DM_NOR_FLASH_OK);
    assert_erased(storage, 0, SECTOR_SIZE);

    select_target(&target, true, 3u);
    transfer(&target, 0x06);
    select_target(&target, false, 0);
    select_target(&target, true, 1u);
    assert(transfer(&target, 0x05) == 0);
    assert(transfer(&target, 0) == 0);
    select_target(&target, false, 0);

    select_target(&target, true, 1u);
    transfer(&target, 0x03);
    address(&target, 0x00);
    select_target(&target, false, 0);
    select_target(&target, true, 1u);
    assert(transfer(&target, 0x00) == 0);
    select_target(&target, false, 0);

    dm_spi_nor_flash_reset(&adapter);
    puts("RESULT: SPI NOR Flash framing smoke passed");
    return 0;
}
