/* Board-independent fixed-capacity host PIO byte FIFO. */
#include "hw/usb/dm_usb_host_pio_fifo.h"

void dm_usb_host_pio_fifo_reset(DmUsbHostPioFifo *fifo)
{
    fifo->head = 0;
    fifo->count = 0;
}

size_t dm_usb_host_pio_fifo_count(const DmUsbHostPioFifo *fifo)
{
    return fifo->count;
}

size_t dm_usb_host_pio_fifo_write(DmUsbHostPioFifo *fifo,
                                  const uint8_t *data, size_t length)
{
    size_t written = 0;

    while (written < length && fifo->count < DM_USB_HOST_PIO_FIFO_CAPACITY) {
        fifo->data[(fifo->head + fifo->count) % DM_USB_HOST_PIO_FIFO_CAPACITY] =
            data[written++];
        ++fifo->count;
    }
    return written;
}

size_t dm_usb_host_pio_fifo_read(DmUsbHostPioFifo *fifo, uint8_t *data,
                                 size_t length)
{
    size_t read = 0;

    while (read < length && fifo->count) {
        data[read++] = fifo->data[fifo->head];
        fifo->head = (fifo->head + 1) % DM_USB_HOST_PIO_FIFO_CAPACITY;
        --fifo->count;
    }
    return read;
}

bool dm_usb_host_pio_fifo_read_exact(DmUsbHostPioFifo *fifo, uint8_t *data,
                                     size_t length)
{
    if (fifo->count < length) {
        return false;
    }
    dm_usb_host_pio_fifo_read(fifo, data, length);
    return true;
}
