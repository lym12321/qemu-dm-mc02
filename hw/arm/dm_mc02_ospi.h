/* Minimal STM32H723 OCTOSPI/OCTOSPIM register windows for DM-MC02. */
#ifndef HW_ARM_DM_MC02_OSPI_H
#define HW_ARM_DM_MC02_OSPI_H

#include "exec/memory.h"
#include "hw/arm/dm_mc02_dma_endpoint.h"
#include "hw/arm/dm_mc02_ssi_nor.h"
#include "../../../../cosim/dm_nor_flash.h"
#include "../../../../cosim/dm_nor_flash_persistence.h"
#include "migration/vmstate.h"

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_OSPI_REGION_SIZE 0x400
#define DM_MC02_OSPIM_REGION_SIZE 0x100
#define DM_MC02_OSPI_FLASH_SIZE (8 * 1024 * 1024)
#define DM_MC02_OSPI_PAGE_SIZE 256
#define DM_MC02_OSPI_MAX_PAGE_SIZE 256

typedef struct DmMc02OspiFlashConfig {
    const char *name;
    uint32_t storage_size;
    uint32_t page_size;
    uint32_t sector_size;
    uint8_t jedec_id[3];
} DmMc02OspiFlashConfig;

typedef struct DmMc02Ospi {
    MemoryRegion iomem;
    MemoryRegion flash_window;
    /* The embedded DmNorFlash is used only by the standalone controller
     * fixture.  Production QEMU builds use the independent dm-w25q64 through
     * the opaque SSI fields below. */
    uint32_t regs[DM_MC02_OSPI_REGION_SIZE / sizeof(uint32_t)];
    DmNorFlash flash;
    uint8_t *rx_data;
    uint32_t rx_size;
    uint32_t rx_pos;
    /* Direct P2M endpoint reservations are synchronous and never enter
     * component VMState.  They keep the source bytes at rx_pos until the
     * DMA guest-memory write commits. */
    uint32_t rx_dma_reserved_size;
    bool rx_dma_reserved;
    uint8_t tx_data[DM_MC02_OSPI_MAX_PAGE_SIZE];
    uint32_t tx_size;
    uint32_t tx_expected;
    uint32_t command_address;
    uint8_t command;
    bool command_valid;
    bool command_started;
    bool memory_mapped;
    bool has_flash;
    uint32_t flash_size;
    uint32_t page_size;
    uint32_t sector_size;
    uint8_t jedec_id[3];
    DmMc02SsiNor ssi_nor;
} DmMc02Ospi;

typedef struct DmMc02Ospim {
    MemoryRegion iomem;
    uint32_t regs[DM_MC02_OSPIM_REGION_SIZE / sizeof(uint32_t)];
} DmMc02Ospim;

void dm_mc02_ospi_init(DmMc02Ospi *state, Object *owner);
void dm_mc02_ospi_reset(DmMc02Ospi *state);
void dm_mc02_ospi_cleanup(DmMc02Ospi *state);
void dm_mc02_ospi_init_with_flash(DmMc02Ospi *state, Object *owner,
                                  bool with_flash);
void dm_mc02_ospi_init_with_config(DmMc02Ospi *state, Object *owner,
                                   const DmMc02OspiFlashConfig *config);
void dm_mc02_ospi_init_with_config_and_ssi(
    DmMc02Ospi *state, Object *owner, DeviceState *ssi_parent,
    const DmMc02OspiFlashConfig *config);
DmMc02DmaEndpoint dm_mc02_ospi_dma_endpoint(DmMc02Ospi *state);
/* Validate and re-project controller-owned state after a component restore.
 * Flash storage and the realized SSI device remain destination-owned. */
bool dm_mc02_ospi_state_valid(const DmMc02Ospi *state);
void dm_mc02_ospi_sync_runtime(DmMc02Ospi *state);
const VMStateDescription *dm_mc02_ospi_vmstate(void);
const VMStateDescription *dm_mc02_ospi_vmstate_raw(void);
const VMStateDescription *dm_mc02_ospim_vmstate(void);
const VMStateDescription *dm_mc02_ospim_vmstate_raw(void);
DmNorFlashPersistenceResult dm_mc02_ospi_load_persistence(
    DmMc02Ospi *state, const char *path);
DmNorFlashPersistenceResult dm_mc02_ospi_save_persistence(
    const DmMc02Ospi *state, const char *path);
void dm_mc02_ospim_init(DmMc02Ospim *state, Object *owner);

#endif
