/* Board-independent PIO/DMA data path for STM32H7 host channels. */
#ifndef HW_USB_DM_USB_HOST_CHANNEL_DATA_PATH_H
#define HW_USB_DM_USB_HOST_CHANNEL_DATA_PATH_H

#include "hw/usb/dm_stm32h7_otg_host.h"

#include <stdbool.h>
#include <stdint.h>

typedef bool DmUsbHostChannelMemoryRead(void *opaque, uint32_t address,
                                        uint8_t *data, uint32_t length);
typedef bool DmUsbHostChannelMemoryWrite(void *opaque, uint32_t address,
                                         const uint8_t *data,
                                         uint32_t length);

typedef struct DmUsbHostChannelDataPath {
    DmStm32H7OtgHost *host;
    DmUsbHostChannelMemoryRead *memory_read;
    DmUsbHostChannelMemoryWrite *memory_write;
    void *opaque;
} DmUsbHostChannelDataPath;

void dm_usb_host_channel_data_path_init(
    DmUsbHostChannelDataPath *path, DmStm32H7OtgHost *host,
    DmUsbHostChannelMemoryRead *memory_read,
    DmUsbHostChannelMemoryWrite *memory_write, void *opaque);
bool dm_usb_host_channel_data_path_read_out(void *opaque, unsigned channel,
                                            uint8_t *data, uint32_t length);
bool dm_usb_host_channel_data_path_write_in(void *opaque, unsigned channel,
                                            const uint8_t *data,
                                            uint32_t length);

#endif /* HW_USB_DM_USB_HOST_CHANNEL_DATA_PATH_H */
