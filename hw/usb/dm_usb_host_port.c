/* Board-independent USB host-port lifecycle boundary. */
#include "hw/usb/dm_usb_host_port.h"

void dm_usb_host_port_init(DmUsbHostPort *port,
                           DmUsbHostPortReset *reset, void *opaque)
{
    if (!port) {
        return;
    }
    *port = (DmUsbHostPort) {
        .reset = reset,
        .opaque = opaque,
    };
}

void dm_usb_host_port_reset(DmUsbHostPort *port, uint64_t timestamp_ns)
{
    if (port && port->reset) {
        port->reset(port->opaque, timestamp_ns);
    }
}
