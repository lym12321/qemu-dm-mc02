/* Runtime hooks for the isolated composite BMI088/SPI VMState contract. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_bmi088_spi_link.h"
#include "hw/arm/dm_mc02_spi.h"

unsigned dm_mc02_spi_sync_calls;
unsigned dm_mc02_spi_restore_selected_mask_calls;

void dm_mc02_spi_sync_runtime(DmMc02Spi *state)
{
    dm_mc02_spi_sync_calls++;
    state->dma_request_active = false;
}

void dm_mc02_spi_restore_selected_mask(DmMc02Spi *state,
                                       uint32_t selected_mask)
{
    dm_mc02_spi_restore_selected_mask_calls++;
    state->selected_mask = selected_mask;
}

bool dm_mc02_bmi088_spi_link_restore_selected_mask(
    DmMc02Bmi088SpiLink *link, uint32_t selected_mask)
{
    if (!link || selected_mask &
        ~((UINT32_C(1) << DM_MC02_SPI_MAX_TARGETS) - 1)) {
        return false;
    }
    link->selected_mask_snapshot = selected_mask;
    dm_mc02_spi_restore_selected_mask(&link->spi, selected_mask);
    return true;
}
