/* Board-independent PIO/DMA data path for STM32H7 host channels. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_channel_data_path.h"

static bool dm_usb_host_channel_data_path_uses_dma(
    const DmUsbHostChannelDataPath *path)
{
    return dm_stm32h7_otg_host_read(path->host,
                                    DM_STM32H7_OTG_GAHBCFG) &
           DM_STM32H7_OTG_GAHBCFG_DMAEN;
}

void dm_usb_host_channel_data_path_init(
    DmUsbHostChannelDataPath *path, DmStm32H7OtgHost *host,
    DmUsbHostChannelMemoryRead *memory_read,
    DmUsbHostChannelMemoryWrite *memory_write, void *opaque)
{
    *path = (DmUsbHostChannelDataPath) {
        .host = host,
        .memory_read = memory_read,
        .memory_write = memory_write,
        .opaque = opaque,
    };
}

bool dm_usb_host_channel_data_path_read_out(void *opaque, unsigned channel,
                                            uint8_t *data, uint32_t length)
{
    DmUsbHostChannelDataPath *path = opaque;

    if (!dm_usb_host_channel_data_path_uses_dma(path)) {
        return dm_stm32h7_otg_host_read_out_fifo(path->host, channel, data,
                                                  length);
    }
    if (!path->memory_read) {
        return false;
    }
    return path->memory_read(path->opaque,
                             dm_stm32h7_otg_host_read(
                                 path->host, DM_STM32H7_OTG_HCDMA(channel)),
                             data, length);
}

bool dm_usb_host_channel_data_path_write_in(void *opaque, unsigned channel,
                                            const uint8_t *data,
                                            uint32_t length)
{
    DmUsbHostChannelDataPath *path = opaque;

    if (!dm_usb_host_channel_data_path_uses_dma(path)) {
        return dm_stm32h7_otg_host_write_in_fifo(path->host, channel, data,
                                                  length);
    }
    if (!path->memory_write) {
        return false;
    }
    return path->memory_write(path->opaque,
                              dm_stm32h7_otg_host_read(
                                  path->host, DM_STM32H7_OTG_HCDMA(channel)),
                              data, length);
}
