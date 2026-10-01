/* Component-only VMState for the STM32H723 OCTOSPI/OCTOSPIM wrapper. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_ospi.h"
#include "migration/vmstate.h"

static int dm_mc02_ospi_pre_save(void *opaque)
{
    return dm_mc02_ospi_state_valid(opaque) ? 0 : -EINVAL;
}

static int dm_mc02_ospi_pre_load(void *opaque)
{
    DmMc02Ospi *state = opaque;

    /* VMSTATE_VBUFFER_ALLOC_UINT32 replaces this pointer on load.  Release
     * the destination's old staging buffer first; it is not part of the
     * caller-owned Flash backing store. */
    g_free(state->rx_data);
    state->rx_data = NULL;
    return 0;
}

static int dm_mc02_ospi_validate_post_load(void *opaque, int version_id)
{
    if (version_id != 1 || !dm_mc02_ospi_state_valid(opaque)) {
        return -EINVAL;
    }
    return 0;
}

static int dm_mc02_ospi_post_load(void *opaque, int version_id)
{
    int ret = dm_mc02_ospi_validate_post_load(opaque, version_id);

    if (ret) {
        return ret;
    }
    /* MemoryRegion, SSI bus/device and DMA endpoint callbacks are destination
     * wiring.  Re-project the memory-mapped gate and interrupted program CS
     * only after every serialized controller field is valid. */
    dm_mc02_ospi_sync_runtime(opaque);
    return 0;
}

static const VMStateField vmstate_dm_mc02_ospi_fields[] = {
    VMSTATE_UINT32_ARRAY(regs, DmMc02Ospi,
                         DM_MC02_OSPI_REGION_SIZE / sizeof(uint32_t)),
    VMSTATE_UINT32(rx_size, DmMc02Ospi),
    VMSTATE_UINT32(rx_pos, DmMc02Ospi),
    VMSTATE_VBUFFER_ALLOC_UINT32(rx_data, DmMc02Ospi, 1, NULL, rx_size),
    VMSTATE_UINT8_ARRAY(tx_data, DmMc02Ospi, DM_MC02_OSPI_MAX_PAGE_SIZE),
    VMSTATE_UINT32(tx_size, DmMc02Ospi),
    VMSTATE_UINT32(tx_expected, DmMc02Ospi),
    VMSTATE_UINT32(command_address, DmMc02Ospi),
    VMSTATE_UINT8(command, DmMc02Ospi),
    VMSTATE_BOOL(command_valid, DmMc02Ospi),
    VMSTATE_BOOL(command_started, DmMc02Ospi),
    VMSTATE_END_OF_LIST()
};

static const VMStateDescription vmstate_dm_mc02_ospi_raw = {
    .name = "dm-mc02-ospi-raw",
    .version_id = 1,
    .minimum_version_id = 1,
    .pre_save = dm_mc02_ospi_pre_save,
    .pre_load = dm_mc02_ospi_pre_load,
    .post_load = dm_mc02_ospi_validate_post_load,
    .fields = vmstate_dm_mc02_ospi_fields,
};

static const VMStateDescription vmstate_dm_mc02_ospi = {
    .name = "dm-mc02-ospi",
    .version_id = 1,
    .minimum_version_id = 1,
    .pre_save = dm_mc02_ospi_pre_save,
    .pre_load = dm_mc02_ospi_pre_load,
    .post_load = dm_mc02_ospi_post_load,
    .fields = vmstate_dm_mc02_ospi_fields,
};

static const VMStateDescription vmstate_dm_mc02_ospim = {
    .name = "dm-mc02-ospim",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, DmMc02Ospim,
                             DM_MC02_OSPIM_REGION_SIZE / sizeof(uint32_t)),
        VMSTATE_END_OF_LIST()
    },
};

const VMStateDescription *dm_mc02_ospi_vmstate(void)
{
    return &vmstate_dm_mc02_ospi;
}

const VMStateDescription *dm_mc02_ospi_vmstate_raw(void)
{
    return &vmstate_dm_mc02_ospi_raw;
}

const VMStateDescription *dm_mc02_ospim_vmstate(void)
{
    return &vmstate_dm_mc02_ospim;
}

const VMStateDescription *dm_mc02_ospim_vmstate_raw(void)
{
    return &vmstate_dm_mc02_ospim;
}
