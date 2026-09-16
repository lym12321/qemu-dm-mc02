/* H723 SOF-event adapter for a controller-independent periodic poller. */
#include "dm_stm32h7_usb_host_periodic_sof.h"

bool dm_stm32h7_usb_host_periodic_sof_init(
    DmStm32H7UsbHostPeriodicSof *event, DmStm32H7UsbHostSof *source,
    DmUsbHostPeriodicPoller *poller)
{
    if (!event || !source || !poller ||
        source->clock.speed != poller->schedule.speed) {
        return false;
    }
    *event = (DmStm32H7UsbHostPeriodicSof) {
        .source = source,
        .poller = poller,
    };
    return true;
}

DmUsbHostPeriodicPollResult dm_stm32h7_usb_host_periodic_sof_on_event(
    DmStm32H7UsbHostPeriodicSof *event, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    DmUsbHostEndpointCompletion *completion)
{
    return dm_usb_host_periodic_poller_poll(
        event->poller, dm_stm32h7_usb_host_sof_timestamp(event->source),
        out_data, out_length, in_data, in_capacity, completion);
}
