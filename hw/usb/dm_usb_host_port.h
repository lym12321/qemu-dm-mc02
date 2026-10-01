/* Board-independent USB host-port lifecycle boundary. */
#ifndef DM_USB_HOST_PORT_H
#define DM_USB_HOST_PORT_H

#include <stdint.h>

typedef void DmUsbHostPortReset(void *opaque, uint64_t timestamp_ns);

typedef struct DmUsbHostPort {
    DmUsbHostPortReset *reset;
    void *opaque;
} DmUsbHostPort;

void dm_usb_host_port_init(DmUsbHostPort *port,
                           DmUsbHostPortReset *reset, void *opaque);
void dm_usb_host_port_reset(DmUsbHostPort *port, uint64_t timestamp_ns);

#endif /* DM_USB_HOST_PORT_H */
