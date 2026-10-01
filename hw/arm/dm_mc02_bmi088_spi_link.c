/* Board-independent SPI/BMI088 link composition. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_bmi088_spi_link.h"

void dm_mc02_bmi088_spi_link_state_init(DmMc02Bmi088SpiLink *link)
{
    DmMc02SpiTarget target;

    if (!link) {
        return;
    }

    memset(link, 0, sizeof(*link));
    dm_mc02_spi_state_init(&link->spi);
    dm_mc02_bmi088_init(&link->accel, true);
    dm_mc02_bmi088_init(&link->gyro, false);
    dm_mc02_bmi088_spi_init(&link->accel_spi, &link->accel);
    dm_mc02_bmi088_spi_init(&link->gyro_spi, &link->gyro);
    target = dm_mc02_bmi088_spi_target(&link->accel_spi);
    dm_mc02_spi_set_target(&link->spi, 0, &target);
    target = dm_mc02_bmi088_spi_target(&link->gyro_spi);
    dm_mc02_spi_set_target(&link->spi, 1, &target);
}

void dm_mc02_bmi088_spi_link_init(DmMc02Bmi088SpiLink *link, Object *owner,
                                  const char *name, bool dma_timer)
{
    if (!link) {
        return;
    }
    dm_mc02_bmi088_spi_link_state_init(link);
    dm_mc02_spi_init(&link->spi, owner, name, dma_timer);
}

void dm_mc02_bmi088_spi_link_cleanup(DmMc02Bmi088SpiLink *link)
{
    if (link) {
        dm_mc02_spi_cleanup(&link->spi);
    }
}

void dm_mc02_bmi088_spi_link_reset(DmMc02Bmi088SpiLink *link)
{
    if (!link) {
        return;
    }
    dm_mc02_spi_reset(&link->spi);
    dm_mc02_bmi088_reset(&link->accel);
    dm_mc02_bmi088_reset(&link->gyro);
    dm_mc02_bmi088_spi_reset(&link->accel_spi);
    dm_mc02_bmi088_spi_reset(&link->gyro_spi);
    link->selected_mask_snapshot = 0;
}

void dm_mc02_bmi088_spi_link_set_consume_callbacks(
    DmMc02Bmi088SpiLink *link, DmMc02Bmi088SpiConsumeFn accel_consume,
    void *accel_opaque, DmMc02Bmi088SpiConsumeFn gyro_consume,
    void *gyro_opaque)
{
    if (!link) {
        return;
    }
    dm_mc02_bmi088_spi_set_consume_callback(&link->accel_spi,
                                            accel_consume, accel_opaque);
    dm_mc02_bmi088_spi_set_consume_callback(&link->gyro_spi,
                                            gyro_consume, gyro_opaque);
}

void dm_mc02_bmi088_spi_link_set_dma_channels(
    DmMc02Bmi088SpiLink *link, const DmMc02SpiDmaChannel *tx,
    const DmMc02SpiDmaChannel *rx)
{
    if (link) {
        dm_mc02_spi_set_dma_channels(&link->spi, tx, rx);
    }
}

static bool dm_mc02_bmi088_spi_link_mask_valid(uint32_t selected_mask)
{
    return !(selected_mask & ~((UINT32_C(1) << DM_MC02_SPI_MAX_TARGETS) - 1));
}

bool dm_mc02_bmi088_spi_link_select_mask(DmMc02Bmi088SpiLink *link,
                                         uint32_t selected_mask)
{
    if (!link || !dm_mc02_bmi088_spi_link_mask_valid(selected_mask)) {
        return false;
    }
    dm_mc02_spi_select_mask(&link->spi, selected_mask);
    link->selected_mask_snapshot = selected_mask;
    return true;
}

bool dm_mc02_bmi088_spi_link_restore_selected_mask(
    DmMc02Bmi088SpiLink *link, uint32_t selected_mask)
{
    if (!link || !dm_mc02_bmi088_spi_link_mask_valid(selected_mask)) {
        return false;
    }
    link->selected_mask_snapshot = selected_mask;
    dm_mc02_spi_restore_selected_mask(&link->spi, selected_mask);
    return true;
}
