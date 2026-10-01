/* Reusable composition of one SPI bus and the two BMI088 SPI targets. */
#ifndef HW_ARM_DM_MC02_BMI088_SPI_LINK_H
#define HW_ARM_DM_MC02_BMI088_SPI_LINK_H

#include "hw/arm/dm_mc02_bmi088.h"
#include "hw/arm/dm_mc02_bmi088_spi.h"
#include "hw/arm/dm_mc02_spi.h"
#include "migration/vmstate.h"

typedef struct DmMc02Bmi088SpiLink {
    DmMc02Spi spi;
    DmMc02Bmi088 accel;
    DmMc02Bmi088 gyro;
    DmMc02Bmi088Spi accel_spi;
    DmMc02Bmi088Spi gyro_spi;

    /* Snapshot-only copy of the GPIO-derived bus selection.  The live bus
     * field remains runtime state because GPIO is its producer. */
    uint32_t selected_mask_snapshot;
} DmMc02Bmi088SpiLink;

void dm_mc02_bmi088_spi_link_state_init(DmMc02Bmi088SpiLink *link);
void dm_mc02_bmi088_spi_link_init(DmMc02Bmi088SpiLink *link, Object *owner,
                                  const char *name, bool dma_timer);
void dm_mc02_bmi088_spi_link_cleanup(DmMc02Bmi088SpiLink *link);
void dm_mc02_bmi088_spi_link_reset(DmMc02Bmi088SpiLink *link);

void dm_mc02_bmi088_spi_link_set_consume_callbacks(
    DmMc02Bmi088SpiLink *link, DmMc02Bmi088SpiConsumeFn accel_consume,
    void *accel_opaque, DmMc02Bmi088SpiConsumeFn gyro_consume,
    void *gyro_opaque);
void dm_mc02_bmi088_spi_link_set_dma_channels(
    DmMc02Bmi088SpiLink *link, const DmMc02SpiDmaChannel *tx,
    const DmMc02SpiDmaChannel *rx);

/* Live board GPIO transitions use this path. */
bool dm_mc02_bmi088_spi_link_select_mask(DmMc02Bmi088SpiLink *link,
                                         uint32_t selected_mask);

/* Snapshot restore uses this no-callback path after child states load. */
bool dm_mc02_bmi088_spi_link_restore_selected_mask(
    DmMc02Bmi088SpiLink *link, uint32_t selected_mask);

/* Composite component contract.  This is not registered with the machine. */
const VMStateDescription *dm_mc02_bmi088_spi_link_vmstate(void);

#endif
