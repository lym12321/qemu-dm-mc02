/* Board-independent USB device control-transfer core. */
#ifndef DM_USB_CONTROL_H
#define DM_USB_CONTROL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "qemu/typedefs.h"

#define DM_USB_CONTROL_MAX_DATA 4096

typedef struct DmUsbControlRequest {
    uint8_t request_type;
    uint8_t request;
    uint16_t value;
    uint16_t index;
    uint16_t length;
} DmUsbControlRequest;

typedef enum DmUsbControlResult {
    DM_USB_CONTROL_ACCEPTED,
    DM_USB_CONTROL_STALL,
    DM_USB_CONTROL_INVALID,
} DmUsbControlResult;

typedef enum DmUsbControlPhase {
    DM_USB_CONTROL_IDLE,
    DM_USB_CONTROL_DATA_IN,
    DM_USB_CONTROL_DATA_OUT,
    DM_USB_CONTROL_STATUS_IN,
    DM_USB_CONTROL_STATUS_OUT,
    DM_USB_CONTROL_STALLED,
} DmUsbControlPhase;

typedef bool DmUsbControlGetDescriptor(
    void *opaque, uint8_t type, uint8_t index,
    const uint8_t **data, size_t *length);

typedef DmUsbControlResult DmUsbControlClassRequest(
    void *opaque, const DmUsbControlRequest *request,
    const uint8_t *out_data, size_t out_length,
    uint8_t *in_data, size_t in_capacity, size_t *in_length);

typedef void DmUsbControlSetAddress(void *opaque, uint8_t address);
typedef void DmUsbControlSetConfiguration(void *opaque, uint8_t configuration);

typedef struct DmUsbControlOps {
    DmUsbControlGetDescriptor *get_descriptor;
    DmUsbControlClassRequest *class_request;
    DmUsbControlSetAddress *set_address;
    DmUsbControlSetConfiguration *set_configuration;
} DmUsbControlOps;

typedef struct DmUsbControlDevice {
    DmUsbControlOps ops;
    void *opaque;
    uint16_t max_packet_size;
    DmUsbControlRequest request;
    DmUsbControlPhase phase;
    uint8_t setup[8];
    uint8_t data[DM_USB_CONTROL_MAX_DATA];
    size_t data_length;
    size_t data_offset;
    bool zero_length_packet;
    bool address_pending;
    bool configuration_pending;
    uint8_t address;
    uint8_t pending_address;
    uint8_t configuration;
    uint8_t pending_configuration;
} DmUsbControlDevice;

void dm_usb_control_init(DmUsbControlDevice *device,
                         const DmUsbControlOps *ops, void *opaque,
                         uint16_t max_packet_size);
void dm_usb_control_reset(DmUsbControlDevice *device);
void dm_usb_control_bus_reset(DmUsbControlDevice *device);

DmUsbControlResult dm_usb_control_setup(DmUsbControlDevice *device,
                                        const uint8_t setup[8]);
DmUsbControlResult dm_usb_control_in(DmUsbControlDevice *device,
                                     uint8_t *data, size_t capacity,
                                     size_t *length);
DmUsbControlResult dm_usb_control_out(DmUsbControlDevice *device,
                                      const uint8_t *data, size_t length);

/* Execute one complete control request at a request-level boundary.  The
 * helper drives the same control state machine used by token consumers; it
 * does not introduce a second control state machine.  IN requests return
 * their data in in_data and OUT requests consume out_data.  On success the
 * control device is idle and actual_length contains only the IN payload. */
DmUsbControlResult dm_usb_control_execute_request(
    DmUsbControlDevice *device, const DmUsbControlRequest *request,
    const uint8_t *out_data, size_t out_length, uint8_t *in_data,
    size_t in_capacity, size_t *actual_length);

DmUsbControlPhase dm_usb_control_phase(const DmUsbControlDevice *device);
uint8_t dm_usb_control_address(const DmUsbControlDevice *device);
uint8_t dm_usb_control_configuration(const DmUsbControlDevice *device);

/* Validate serialized transfer state without touching runtime callbacks. */
bool dm_usb_control_state_valid(const DmUsbControlDevice *device);

/* Component-only state contract.  Control callbacks and opaque are
 * destination-owned runtime wiring and are not serialized. */
const VMStateDescription *dm_usb_control_vmstate(void);
const VMStateDescription *dm_usb_control_vmstate_raw(void);

/* Child description used by an enclosing composite state boundary. */
extern const VMStateDescription vmstate_dm_usb_control_raw;

#endif /* DM_USB_CONTROL_H */
