/*
 * Board-independent USB endpoint packet queue.
 */
#include "hw/usb/dm_usb_endpoint_queue.h"

#include <stdlib.h>
#include <string.h>

static bool
dm_usb_endpoint_config_valid(const DmUsbEndpointConfig *config)
{
    return config->number <= 15 &&
           config->direction <= DM_USB_ENDPOINT_IN &&
           config->type <= DM_USB_ENDPOINT_INTERRUPT &&
           config->max_packet_size != 0 &&
           config->capacity != 0 &&
           config->capacity <= SIZE_MAX / config->max_packet_size;
}

static bool
dm_usb_endpoint_queue_initialized(const DmUsbEndpointQueue *queue)
{
    return queue->capacity != 0 && queue->max_packet_size != 0 &&
           queue->storage != NULL && queue->timestamps != NULL &&
           queue->lengths != NULL;
}

DmUsbEndpointQueueResult
dm_usb_endpoint_queue_init(DmUsbEndpointQueue *queue,
                           const DmUsbEndpointConfig *config)
{
    if (queue == NULL || config == NULL || !dm_usb_endpoint_config_valid(config)) {
        return DM_USB_ENDPOINT_QUEUE_INVALID;
    }

    uint8_t *storage = calloc(config->capacity, config->max_packet_size);
    uint64_t *timestamps = calloc(config->capacity, sizeof(*timestamps));
    uint16_t *lengths = calloc(config->capacity, sizeof(*lengths));

    if (!storage || !timestamps || !lengths) {
        free(storage);
        free(timestamps);
        free(lengths);
        return DM_USB_ENDPOINT_QUEUE_INVALID;
    }

    *queue = (DmUsbEndpointQueue) {
        .number = config->number,
        .direction = config->direction,
        .type = config->type,
        .max_packet_size = config->max_packet_size,
        .capacity = config->capacity,
        .storage = storage,
        .timestamps = timestamps,
        .lengths = lengths,
    };

    return DM_USB_ENDPOINT_QUEUE_ACCEPTED;
}

void
dm_usb_endpoint_queue_cleanup(DmUsbEndpointQueue *queue)
{
    if (queue == NULL) {
        return;
    }

    free(queue->storage);
    free(queue->timestamps);
    free(queue->lengths);
    *queue = (DmUsbEndpointQueue) { 0 };
}

DmUsbEndpointQueueResult
dm_usb_endpoint_queue_enqueue(DmUsbEndpointQueue *queue,
                              const uint8_t *data,
                              size_t length,
                              uint64_t timestamp_ns)
{
    size_t index;

    if (queue == NULL || !dm_usb_endpoint_queue_initialized(queue) ||
        (length != 0 && data == NULL)) {
        return DM_USB_ENDPOINT_QUEUE_INVALID;
    }
    if (length > queue->max_packet_size) {
        return DM_USB_ENDPOINT_QUEUE_OVERSIZE;
    }
    if (queue->count == queue->capacity) {
        return DM_USB_ENDPOINT_QUEUE_FULL;
    }

    index = (queue->head + queue->count) % queue->capacity;
    if (length != 0) {
        memcpy(queue->storage + index * queue->max_packet_size, data, length);
    }
    queue->lengths[index] = length;
    queue->timestamps[index] = timestamp_ns;
    queue->count++;

    return DM_USB_ENDPOINT_QUEUE_ACCEPTED;
}

static DmUsbEndpointQueueResult
dm_usb_endpoint_queue_peek_internal(const DmUsbEndpointQueue *queue,
                                    DmUsbEndpointPacket *packet)
{
    if (queue == NULL || packet == NULL ||
        !dm_usb_endpoint_queue_initialized(queue)) {
        return DM_USB_ENDPOINT_QUEUE_INVALID;
    }
    if (queue->count == 0) {
        return DM_USB_ENDPOINT_QUEUE_EMPTY;
    }

    packet->data = queue->storage + queue->head * queue->max_packet_size;
    packet->length = queue->lengths[queue->head];
    packet->timestamp_ns = queue->timestamps[queue->head];
    return DM_USB_ENDPOINT_QUEUE_ACCEPTED;
}

DmUsbEndpointQueueResult
dm_usb_endpoint_queue_peek(const DmUsbEndpointQueue *queue,
                           DmUsbEndpointPacket *packet)
{
    return dm_usb_endpoint_queue_peek_internal(queue, packet);
}

DmUsbEndpointQueueResult
dm_usb_endpoint_queue_dequeue(DmUsbEndpointQueue *queue,
                              DmUsbEndpointPacket *packet)
{
    DmUsbEndpointQueueResult result;

    result = dm_usb_endpoint_queue_peek_internal(queue, packet);
    if (result != DM_USB_ENDPOINT_QUEUE_ACCEPTED) {
        return result;
    }

    queue->head = (queue->head + 1) % queue->capacity;
    queue->count--;
    return DM_USB_ENDPOINT_QUEUE_ACCEPTED;
}

void
dm_usb_endpoint_queue_reset(DmUsbEndpointQueue *queue)
{
    if (queue == NULL || !dm_usb_endpoint_queue_initialized(queue)) {
        return;
    }

    queue->head = 0;
    queue->count = 0;
}
