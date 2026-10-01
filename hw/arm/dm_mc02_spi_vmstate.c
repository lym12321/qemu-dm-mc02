/* Component-only VMState contract for the board-independent SPI data path. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_spi.h"
#include "migration/vmstate.h"

static int dm_mc02_spi_post_load(void *opaque, int version_id)
{
    DmMc02Spi *state = opaque;

    if (version_id != 1 ||
        state->transfer_remaining > UINT16_MAX ||
        state->dma_tx_next_ns > INT64_MAX) {
        return -EINVAL;
    }

    /* Targets, DMA channels, GPIO selection, timer objects and endpoint
     * callbacks are destination-owned runtime wiring.  Rebuild only the
     * SPI-owned scheduler after all serialized fields have passed checks. */
    dm_mc02_spi_sync_runtime(state);
    return 0;
}

static const VMStateField vmstate_dm_mc02_spi_fields[] = {
    VMSTATE_UINT32(cr1, DmMc02Spi),
    VMSTATE_UINT32(cr2, DmMc02Spi),
    VMSTATE_UINT32(cfg1, DmMc02Spi),
    VMSTATE_UINT32(cfg2, DmMc02Spi),
    VMSTATE_UINT32(transfer_remaining, DmMc02Spi),
    VMSTATE_UINT8(rx, DmMc02Spi),
    VMSTATE_BOOL(rx_valid, DmMc02Spi),
    VMSTATE_BOOL(eot, DmMc02Spi),
    VMSTATE_UINT64(dma_tx_next_ns, DmMc02Spi),
    VMSTATE_END_OF_LIST()
};

const VMStateDescription vmstate_dm_mc02_spi_raw = {
    .name = "dm-mc02-spi-raw",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = vmstate_dm_mc02_spi_fields,
};

static const VMStateDescription vmstate_dm_mc02_spi = {
    .name = "dm-mc02-spi",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_spi_post_load,
    .fields = vmstate_dm_mc02_spi_fields,
};

const VMStateDescription *dm_mc02_spi_vmstate(void)
{
    return &vmstate_dm_mc02_spi;
}

const VMStateDescription *dm_mc02_spi_vmstate_raw(void)
{
    return &vmstate_dm_mc02_spi_raw;
}
