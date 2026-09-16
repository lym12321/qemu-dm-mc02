#include <stdint.h>

#define USB_GINTSTS (*(volatile uint32_t *)0x40040014u)
#define USB_FIFO0   (*(volatile uint32_t *)0x40041000u)
#define RXFLVL      (1u << 4)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    volatile uint32_t *result = (volatile uint32_t *)0x20000000u;
    uint8_t received[4];
    unsigned count = 0;

    result[0] = 0x55534250u;
    /* Wait for the host side to attach and send a first packet. */
    while (count != sizeof(received)) {
        if (USB_GINTSTS & RXFLVL) {
            uint32_t word = USB_FIFO0;
            for (unsigned i = 0; i < 4 && count != sizeof(received); ++i) {
                received[count++] = word >> (i * 8);
            }
        }
    }
    result[1] = received[0] | ((uint32_t)received[1] << 8) |
               ((uint32_t)received[2] << 16) |
               ((uint32_t)received[3] << 24);
    /* Host sees these three bytes in little-endian FIFO order. */
    USB_FIFO0 = 0x2d4b4fu; /* "OK-" */
    for (;;) {
        __asm__ volatile ("wfi");
    }
}
