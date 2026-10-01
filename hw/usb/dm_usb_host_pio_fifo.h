/* Board-independent fixed-capacity host PIO byte FIFO. */
#ifndef HW_USB_DM_USB_HOST_PIO_FIFO_H
#define HW_USB_DM_USB_HOST_PIO_FIFO_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DM_USB_HOST_PIO_FIFO_CAPACITY 2048

typedef struct DmUsbHostPioFifo {
    uint8_t data[DM_USB_HOST_PIO_FIFO_CAPACITY];
    uint16_t head;
    uint16_t count;
} DmUsbHostPioFifo;

void dm_usb_host_pio_fifo_reset(DmUsbHostPioFifo *fifo);
size_t dm_usb_host_pio_fifo_count(const DmUsbHostPioFifo *fifo);
size_t dm_usb_host_pio_fifo_write(DmUsbHostPioFifo *fifo,
                                  const uint8_t *data, size_t length);
size_t dm_usb_host_pio_fifo_read(DmUsbHostPioFifo *fifo, uint8_t *data,
                                 size_t length);
bool dm_usb_host_pio_fifo_read_exact(DmUsbHostPioFifo *fifo, uint8_t *data,
                                     size_t length);

#endif /* HW_USB_DM_USB_HOST_PIO_FIFO_H */
