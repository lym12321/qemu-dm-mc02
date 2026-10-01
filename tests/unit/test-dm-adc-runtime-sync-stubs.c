/* Narrow DMA and system-memory boundaries for the ADC runtime-sync test. */
#include "qemu/osdep.h"
#include "exec/memory.h"
#include "hw/arm/dm_mc02_adc.h"

AddressSpace address_space_memory;

void memory_region_init_io(MemoryRegion *mr, Object *owner,
                           const MemoryRegionOps *ops, void *opaque,
                           const char *name, uint64_t size)
{
    (void)mr;
    (void)owner;
    (void)ops;
    (void)opaque;
    (void)name;
    (void)size;
}

MemTxResult address_space_rw(AddressSpace *as, hwaddr addr, MemTxAttrs attrs,
                             void *buf, hwaddr len, bool is_write)
{
    (void)as;
    (void)addr;
    (void)attrs;
    (void)buf;
    (void)len;
    (void)is_write;
    return MEMTX_ERROR;
}

bool dm_mc02_dma_request(DmMc02Dma *state, const DmMc02Dmamux *dmamux,
                         uint32_t request_id, hwaddr peripheral_addr)
{
    (void)state;
    (void)dmamux;
    (void)request_id;
    (void)peripheral_addr;
    return false;
}

bool dm_mc02_dma_request_endpoint(
    DmMc02Dma *state, const DmMc02Dmamux *dmamux, uint32_t request_id,
    hwaddr peripheral_addr, const DmMc02DmaEndpoint *endpoint,
    uint64_t timestamp_ns)
{
    (void)state;
    (void)dmamux;
    (void)request_id;
    (void)peripheral_addr;
    (void)endpoint;
    (void)timestamp_ns;
    return false;
}
