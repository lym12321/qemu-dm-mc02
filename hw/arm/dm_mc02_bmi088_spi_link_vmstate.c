/* Composite VMState for the reusable SPI/BMI088 link. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_bmi088_spi_link.h"

static int dm_mc02_bmi088_spi_link_pre_save(void *opaque)
{
    DmMc02Bmi088SpiLink *state = opaque;

    if (!state ||
        state->spi.selected_mask &
        ~((UINT32_C(1) << DM_MC02_SPI_MAX_TARGETS) - 1)) {
        return -EINVAL;
    }
    state->selected_mask_snapshot = state->spi.selected_mask;
    return 0;
}

static int dm_mc02_bmi088_spi_link_post_load(void *opaque, int version_id)
{
    DmMc02Bmi088SpiLink *state = opaque;

    if (!state || version_id != 1 ||
        state->selected_mask_snapshot &
        ~((UINT32_C(1) << DM_MC02_SPI_MAX_TARGETS) - 1) ||
        /* The composite has fixed accelerometer/gyro slots.  The die
         * identity and signal kind are intentionally static child config,
         * so validate the slot contract here before restoring bus state. */
        !state->accel.accel || state->gyro.accel ||
        state->accel.signal.kind != DM_MC02_BMI088_SIGNAL_ACCEL ||
        state->gyro.signal.kind != DM_MC02_BMI088_SIGNAL_GYRO ||
        state->accel_spi.bmi != &state->accel ||
        state->gyro_spi.bmi != &state->gyro) {
        return -EINVAL;
    }

    /* Child post-load validators have already checked both dies and both
     * framers.  Project CS without callbacks, then activate the SPI timer
     * only after the complete target graph is valid. */
    if (!dm_mc02_bmi088_spi_link_restore_selected_mask(
            state, state->selected_mask_snapshot)) {
        return -EINVAL;
    }
    dm_mc02_spi_sync_runtime(&state->spi);
    return 0;
}

static const VMStateDescription vmstate_dm_mc02_bmi088_spi_link = {
    .name = "dm-mc02-bmi088-spi-link",
    .version_id = 1,
    .minimum_version_id = 1,
    .pre_save = dm_mc02_bmi088_spi_link_pre_save,
    .post_load = dm_mc02_bmi088_spi_link_post_load,
    .fields = (VMStateField[]) {
        /* The raw SPI child has no post-load, so its DMA continuation is
         * loaded here but not armed until the parent post-load below. */
        VMSTATE_STRUCT(spi, DmMc02Bmi088SpiLink, 0,
                       vmstate_dm_mc02_spi_raw, DmMc02Spi),
        VMSTATE_STRUCT(accel, DmMc02Bmi088SpiLink, 0,
                       vmstate_dm_mc02_bmi088, DmMc02Bmi088),
        VMSTATE_STRUCT(gyro, DmMc02Bmi088SpiLink, 0,
                       vmstate_dm_mc02_bmi088, DmMc02Bmi088),
        VMSTATE_STRUCT(accel_spi, DmMc02Bmi088SpiLink, 0,
                       vmstate_dm_mc02_bmi088_spi, DmMc02Bmi088Spi),
        VMSTATE_STRUCT(gyro_spi, DmMc02Bmi088SpiLink, 0,
                       vmstate_dm_mc02_bmi088_spi, DmMc02Bmi088Spi),
        VMSTATE_UINT32(selected_mask_snapshot, DmMc02Bmi088SpiLink),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_bmi088_spi_link_vmstate(void)
{
    return &vmstate_dm_mc02_bmi088_spi_link;
}
