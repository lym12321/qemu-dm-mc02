/* Narrow runtime stubs for the isolated ADC-pair VMState contract test. */
#include "qemu/osdep.h"
#include "exec/memory.h"
#include "hw/arm/dm_mc02_adc.h"
#include "hw/arm/dm_mc02_adc_common.h"

unsigned dm_mc02_adc_pair_common_sync_calls;
unsigned dm_mc02_adc_pair_adc_sync_calls;

void memory_region_init_io(MemoryRegion *mr, Object *owner,
                           const MemoryRegionOps *ops, void *opaque,
                           const char *name, uint64_t size)
{
    (void)owner;
    mr->ops = ops;
    mr->opaque = opaque;
    mr->name = name;
    mr->size = int128_make64(size);
}

void dm_mc02_adc_common_sync_runtime(DmMc02AdcCommon *state)
{
    (void)state;
    dm_mc02_adc_pair_common_sync_calls++;
}

bool dm_mc02_adc_common_cdr2_dma_supported(
    const DmMc02AdcCommon *state)
{
    (void)state;
    return true;
}

unsigned dm_mc02_adc_common_get_dual(const DmMc02AdcCommon *state)
{
    return state ? state->ccr & DM_MC02_ADC_COMMON_CCR_DUAL_MASK : 0;
}

void dm_mc02_adc_sync_runtime(DmMc02Adc *state)
{
    (void)state;
    dm_mc02_adc_pair_adc_sync_calls++;
}
