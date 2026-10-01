/* Board-independent SPI command framing for a small NOR Flash device. */
#include "dm_mc02_spi_nor.h"

#include <string.h>

enum {
    DM_SPI_NOR_CMD_WRITE_ENABLE = 0x06,
    DM_SPI_NOR_CMD_READ_STATUS = 0x05,
    DM_SPI_NOR_CMD_JEDEC_ID = 0x9f,
    DM_SPI_NOR_CMD_PAGE_PROGRAM = 0x02,
    DM_SPI_NOR_CMD_SECTOR_ERASE = 0x20,
    DM_SPI_NOR_CMD_READ = 0x03,
    DM_SPI_NOR_CMD_FAST_READ = 0x0b,
};

static void dm_spi_nor_flash_clear_transaction(DmSpiNorFlash *adapter)
{
    adapter->phase = DM_SPI_NOR_FLASH_IDLE;
    adapter->address_phase = DM_SPI_NOR_FLASH_IDLE;
    adapter->address = 0;
    adapter->address_bytes = 0;
    adapter->jedec_index = 0;
    adapter->program_size = 0;
    adapter->command_seen = false;
}

static void dm_spi_nor_flash_finish(DmSpiNorFlash *adapter)
{
    if (!adapter->selected || !adapter->flash) {
        return;
    }
    if (adapter->phase == DM_SPI_NOR_FLASH_PROGRAM) {
        adapter->last_result = dm_nor_flash_page_program(
            adapter->flash, adapter->address, adapter->program_data,
            adapter->program_size);
    } else if (adapter->phase == DM_SPI_NOR_FLASH_ERASE) {
        adapter->last_result = dm_nor_flash_sector_erase(
            adapter->flash, adapter->address);
    }
}

static void dm_spi_nor_flash_select(void *opaque, bool selected,
                                    uint32_t selected_mask)
{
    DmSpiNorFlash *adapter = opaque;

    if (!adapter) {
        return;
    }
    dm_spi_nor_flash_finish(adapter);
    adapter->selected = false;
    dm_spi_nor_flash_clear_transaction(adapter);
    if (selected) {
        adapter->selected = true;
        if (selected_mask != adapter->selected_mask) {
            adapter->phase = DM_SPI_NOR_FLASH_IGNORE;
        }
    }
}

static uint8_t dm_spi_nor_flash_transfer(void *opaque, uint8_t tx,
                                         uint64_t timestamp_ns)
{
    DmSpiNorFlash *adapter = opaque;

    (void)timestamp_ns;
    if (!adapter || !adapter->selected || !adapter->flash) {
        return 0;
    }
    if (adapter->phase == DM_SPI_NOR_FLASH_IGNORE) {
        return 0;
    }
    if (!adapter->command_seen) {
        adapter->command_seen = true;
        switch (tx) {
        case DM_SPI_NOR_CMD_WRITE_ENABLE:
            dm_nor_flash_write_enable(adapter->flash);
            adapter->phase = DM_SPI_NOR_FLASH_IGNORE;
            break;
        case DM_SPI_NOR_CMD_READ_STATUS:
            adapter->phase = DM_SPI_NOR_FLASH_STATUS;
            break;
        case DM_SPI_NOR_CMD_JEDEC_ID:
            adapter->phase = DM_SPI_NOR_FLASH_JEDEC;
            adapter->jedec_index = 0;
            break;
        case DM_SPI_NOR_CMD_READ:
            adapter->phase = DM_SPI_NOR_FLASH_ADDRESS;
            adapter->address_phase = DM_SPI_NOR_FLASH_READ;
            break;
        case DM_SPI_NOR_CMD_FAST_READ:
            adapter->phase = DM_SPI_NOR_FLASH_ADDRESS;
            adapter->address_phase = DM_SPI_NOR_FLASH_FAST_READ_DUMMY;
            break;
        case DM_SPI_NOR_CMD_PAGE_PROGRAM:
            adapter->phase = DM_SPI_NOR_FLASH_ADDRESS;
            adapter->address_phase = DM_SPI_NOR_FLASH_PROGRAM;
            break;
        case DM_SPI_NOR_CMD_SECTOR_ERASE:
            adapter->phase = DM_SPI_NOR_FLASH_ADDRESS;
            adapter->address_phase = DM_SPI_NOR_FLASH_ERASE;
            break;
        default:
            adapter->phase = DM_SPI_NOR_FLASH_IGNORE;
            break;
        }
        return 0;
    }

    switch (adapter->phase) {
    case DM_SPI_NOR_FLASH_STATUS:
        return dm_nor_flash_status(adapter->flash);
    case DM_SPI_NOR_FLASH_JEDEC:
        if (adapter->jedec_index < sizeof(adapter->jedec_id)) {
            return adapter->jedec_id[adapter->jedec_index++];
        }
        return 0xff;
    case DM_SPI_NOR_FLASH_ADDRESS:
        adapter->address = (adapter->address << 8) | tx;
        if (++adapter->address_bytes == 3) {
            adapter->phase = adapter->address_phase;
        }
        return 0;
    case DM_SPI_NOR_FLASH_READ: {
        uint8_t result = dm_nor_flash_read_byte(adapter->flash,
                                                adapter->address);

        adapter->address++;
        return result;
    }
    case DM_SPI_NOR_FLASH_FAST_READ_DUMMY:
        adapter->phase = DM_SPI_NOR_FLASH_READ;
        return 0;
    case DM_SPI_NOR_FLASH_PROGRAM:
        if (adapter->program_size < sizeof(adapter->program_data)) {
            adapter->program_data[adapter->program_size++] = tx;
        }
        return 0;
    case DM_SPI_NOR_FLASH_ERASE:
    case DM_SPI_NOR_FLASH_IGNORE:
    case DM_SPI_NOR_FLASH_IDLE:
        return 0;
    }
    return 0;
}

bool dm_spi_nor_flash_init(DmSpiNorFlash *adapter, DmNorFlash *flash,
                           uint32_t selected_mask, const uint8_t jedec_id[3])
{
    static const uint8_t default_jedec_id[3] = { 0xef, 0x40, 0x17 };

    if (!adapter || !flash || !selected_mask ||
        flash->page_size == 0 ||
        flash->page_size > DM_SPI_NOR_FLASH_MAX_PAGE_SIZE) {
        return false;
    }
    memset(adapter, 0, sizeof(*adapter));
    adapter->flash = flash;
    adapter->selected_mask = selected_mask;
    memcpy(adapter->jedec_id, jedec_id ? jedec_id : default_jedec_id,
           sizeof(adapter->jedec_id));
    adapter->last_result = DM_NOR_FLASH_INVALID;
    dm_spi_nor_flash_clear_transaction(adapter);
    return true;
}

void dm_spi_nor_flash_reset(DmSpiNorFlash *adapter)
{
    if (!adapter) {
        return;
    }
    adapter->selected = false;
    dm_spi_nor_flash_clear_transaction(adapter);
    adapter->last_result = DM_NOR_FLASH_INVALID;
}

DmNorFlashResult dm_spi_nor_flash_last_result(const DmSpiNorFlash *adapter)
{
    return adapter ? adapter->last_result : DM_NOR_FLASH_INVALID;
}

DmMc02SpiTarget dm_spi_nor_flash_target(DmSpiNorFlash *adapter)
{
    return (DmMc02SpiTarget) {
        .transfer = dm_spi_nor_flash_transfer,
        .select = dm_spi_nor_flash_select,
        .opaque = adapter,
    };
}
