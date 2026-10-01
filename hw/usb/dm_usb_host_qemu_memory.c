/* QEMU AddressSpace binding for the generic USB host DMA callbacks. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_qemu_memory.h"
#include "sysemu/dma.h"

void dm_usb_host_qemu_memory_init(DmUsbHostQemuMemory *memory,
                                  AddressSpace *address_space)
{
    memory->address_space = address_space;
}

bool dm_usb_host_qemu_memory_read(void *opaque, uint32_t address,
                                  uint8_t *data, uint32_t length)
{
    DmUsbHostQemuMemory *memory = opaque;

    return dma_memory_read(memory->address_space, address, data, length,
                           MEMTXATTRS_UNSPECIFIED) == MEMTX_OK;
}

bool dm_usb_host_qemu_memory_write(void *opaque, uint32_t address,
                                   const uint8_t *data, uint32_t length)
{
    DmUsbHostQemuMemory *memory = opaque;

    return dma_memory_write(memory->address_space, address, data, length,
                            MEMTXATTRS_UNSPECIFIED) == MEMTX_OK;
}
