/* Board-independent SPI command framing for a small NOR Flash device. */
#ifndef HW_ARM_DM_MC02_SPI_NOR_H
#define HW_ARM_DM_MC02_SPI_NOR_H

#include "../../../../cosim/dm_nor_flash.h"
#include "dm_mc02_spi_target.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_SPI_NOR_FLASH_MAX_PAGE_SIZE 256u

typedef enum DmSpiNorFlashPhase {
    DM_SPI_NOR_FLASH_IDLE,
    DM_SPI_NOR_FLASH_IGNORE,
    DM_SPI_NOR_FLASH_STATUS,
    DM_SPI_NOR_FLASH_JEDEC,
    DM_SPI_NOR_FLASH_ADDRESS,
    DM_SPI_NOR_FLASH_READ,
    DM_SPI_NOR_FLASH_FAST_READ_DUMMY,
    DM_SPI_NOR_FLASH_PROGRAM,
    DM_SPI_NOR_FLASH_ERASE,
} DmSpiNorFlashPhase;

typedef struct DmSpiNorFlash {
    DmNorFlash *flash;
    uint32_t selected_mask;
    uint8_t jedec_id[3];
    DmSpiNorFlashPhase phase;
    DmSpiNorFlashPhase address_phase;
    size_t address;
    unsigned address_bytes;
    unsigned jedec_index;
    size_t program_size;
    uint8_t program_data[DM_SPI_NOR_FLASH_MAX_PAGE_SIZE + 1u];
    DmNorFlashResult last_result;
    bool selected;
    bool command_seen;
} DmSpiNorFlash;

/* The Flash core and its backing storage remain owned by the caller. */
bool dm_spi_nor_flash_init(DmSpiNorFlash *adapter, DmNorFlash *flash,
                           uint32_t selected_mask, const uint8_t jedec_id[3]);
void dm_spi_nor_flash_reset(DmSpiNorFlash *adapter);
DmNorFlashResult dm_spi_nor_flash_last_result(const DmSpiNorFlash *adapter);
DmMc02SpiTarget dm_spi_nor_flash_target(DmSpiNorFlash *adapter);

#endif /* HW_ARM_DM_MC02_SPI_NOR_H */
