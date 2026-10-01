/* QEMU AddressSpace binding for the generic USB host DMA callbacks. */
#ifndef HW_USB_DM_USB_HOST_QEMU_MEMORY_H
#define HW_USB_DM_USB_HOST_QEMU_MEMORY_H

#include "exec/memory.h"

#include <stdbool.h>
#include <stdint.h>

typedef struct DmUsbHostQemuMemory {
    AddressSpace *address_space;
} DmUsbHostQemuMemory;

void dm_usb_host_qemu_memory_init(DmUsbHostQemuMemory *memory,
                                  AddressSpace *address_space);
bool dm_usb_host_qemu_memory_read(void *opaque, uint32_t address,
                                  uint8_t *data, uint32_t length);
bool dm_usb_host_qemu_memory_write(void *opaque, uint32_t address,
                                   const uint8_t *data, uint32_t length);

#endif /* HW_USB_DM_USB_HOST_QEMU_MEMORY_H */
