/* Common USB link-speed vocabulary for controller-independent host drivers. */
#ifndef DM_USB_HOST_SPEED_H
#define DM_USB_HOST_SPEED_H

typedef enum DmUsbHostSpeed {
    DM_USB_HOST_SPEED_LOW,
    DM_USB_HOST_SPEED_FULL,
    DM_USB_HOST_SPEED_HIGH,
} DmUsbHostSpeed;

#endif /* DM_USB_HOST_SPEED_H */
