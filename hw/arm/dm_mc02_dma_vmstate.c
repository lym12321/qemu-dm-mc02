/* Component-only VMState contract for the board-independent DMA model. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_dma.h"
#include "migration/vmstate.h"

static int dm_mc02_dma_post_load(void *opaque, int version_id)
{
    DmMc02Dma *state = opaque;

    if (version_id != 1 || !dm_mc02_dma_state_valid(state)) {
        return -EINVAL;
    }

    /* Request caches and IRQ levels are derived/runtime state.  Rebuild them
     * only after every serialized producer field has passed validation. */
    dm_mc02_dma_sync_runtime(state);
    return 0;
}

static const VMStateField vmstate_dm_mc02_dma_fields[] = {
        VMSTATE_UINT8_ARRAY(regs, DmMc02Dma, DM_MC02_DMA_REGION_SIZE),
        VMSTATE_UINT32_ARRAY(reload_ndtr, DmMc02Dma,
                             DM_MC02_DMA_STREAM_COUNT),
        VMSTATE_UINT64_ARRAY(reload_par, DmMc02Dma,
                             DM_MC02_DMA_STREAM_COUNT),
        VMSTATE_UINT64_ARRAY(reload_m0ar, DmMc02Dma,
                             DM_MC02_DMA_STREAM_COUNT),
        VMSTATE_UINT64_ARRAY(reload_m1ar, DmMc02Dma,
                             DM_MC02_DMA_STREAM_COUNT),
        VMSTATE_UINT64_ARRAY(cursor_m0ar, DmMc02Dma,
                             DM_MC02_DMA_STREAM_COUNT),
        VMSTATE_UINT64_ARRAY(cursor_m1ar, DmMc02Dma,
                             DM_MC02_DMA_STREAM_COUNT),
        VMSTATE_UINT8_2DARRAY(fifo, DmMc02Dma, DM_MC02_DMA_STREAM_COUNT,
                              DM_MC02_DMA_FIFO_BYTES),
        VMSTATE_UINT8_ARRAY(fifo_head, DmMc02Dma,
                            DM_MC02_DMA_STREAM_COUNT),
        VMSTATE_UINT8_ARRAY(fifo_length, DmMc02Dma,
                            DM_MC02_DMA_STREAM_COUNT),
        VMSTATE_END_OF_LIST()
};

static const VMStateDescription vmstate_dm_mc02_dma = {
    .name = "dm-mc02-dma",
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dm_mc02_dma_post_load,
    .fields = vmstate_dm_mc02_dma_fields,
};

const VMStateDescription vmstate_dm_mc02_dma_raw = {
    .name = "dm-mc02-dma-raw",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = vmstate_dm_mc02_dma_fields,
};

const VMStateDescription *dm_mc02_dma_vmstate(void)
{
    return &vmstate_dm_mc02_dma;
}

const VMStateDescription *dm_mc02_dma_vmstate_raw(void)
{
    return &vmstate_dm_mc02_dma_raw;
}
