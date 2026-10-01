/*
 * Board-independent USB endpoint packet queue.
 *
 * This interface deliberately models packet boundaries.  It does not split,
 * merge, or otherwise interpret packets; a controller or bus adapter owns
 * those policies.
 */
#ifndef DM_USB_ENDPOINT_QUEUE_H
#define DM_USB_ENDPOINT_QUEUE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef enum DmUsbEndpointDirection {
    DM_USB_ENDPOINT_OUT,
    DM_USB_ENDPOINT_IN,
} DmUsbEndpointDirection;

typedef enum DmUsbEndpointType {
    DM_USB_ENDPOINT_CONTROL,
    DM_USB_ENDPOINT_ISOCHRONOUS,
    DM_USB_ENDPOINT_BULK,
    DM_USB_ENDPOINT_INTERRUPT,
} DmUsbEndpointType;

typedef enum DmUsbEndpointQueueResult {
    DM_USB_ENDPOINT_QUEUE_ACCEPTED,
    DM_USB_ENDPOINT_QUEUE_EMPTY,
    DM_USB_ENDPOINT_QUEUE_FULL,
    DM_USB_ENDPOINT_QUEUE_OVERSIZE,
    DM_USB_ENDPOINT_QUEUE_INVALID,
} DmUsbEndpointQueueResult;

typedef struct DmUsbEndpointConfig {
    uint8_t number;
    DmUsbEndpointDirection direction;
    DmUsbEndpointType type;
    uint16_t max_packet_size;
    size_t capacity;
} DmUsbEndpointConfig;

/* A view into a packet owned by the queue. */
typedef struct DmUsbEndpointPacket {
    const uint8_t *data;
    size_t length;
    uint64_t timestamp_ns;
} DmUsbEndpointPacket;

typedef struct DmUsbEndpointQueue {
    uint8_t number;
    DmUsbEndpointDirection direction;
    DmUsbEndpointType type;
    uint16_t max_packet_size;
    size_t capacity;

    size_t head;
    size_t count;
    uint8_t *storage;
    uint64_t *timestamps;
    uint16_t *lengths;
} DmUsbEndpointQueue;

DmUsbEndpointQueueResult
dm_usb_endpoint_queue_init(DmUsbEndpointQueue *queue,
                           const DmUsbEndpointConfig *config);

void dm_usb_endpoint_queue_cleanup(DmUsbEndpointQueue *queue);

DmUsbEndpointQueueResult
dm_usb_endpoint_queue_enqueue(DmUsbEndpointQueue *queue,
                              const uint8_t *data,
                              size_t length,
                              uint64_t timestamp_ns);

DmUsbEndpointQueueResult
dm_usb_endpoint_queue_peek(const DmUsbEndpointQueue *queue,
                           DmUsbEndpointPacket *packet);

DmUsbEndpointQueueResult
dm_usb_endpoint_queue_dequeue(DmUsbEndpointQueue *queue,
                              DmUsbEndpointPacket *packet);

void dm_usb_endpoint_queue_reset(DmUsbEndpointQueue *queue);

#endif /* DM_USB_ENDPOINT_QUEUE_H */
