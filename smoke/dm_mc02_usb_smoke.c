#include <stdint.h>

#define USB_BASE       0x40040000u
#define USB_GRSTCTL    (*(volatile uint32_t *)(USB_BASE + 0x010u))
#define USB_GRXFSIZ    (*(volatile uint32_t *)(USB_BASE + 0x024u))
#define USB_GSNPSID    (*(volatile uint32_t *)(USB_BASE + 0x040u))

#define RESULT         ((volatile uint32_t *)0x20000000u)

#define GRSTCTL_CSRST      (1u << 0)
#define GRSTCTL_AHBIDL     (1u << 31)
#define GRSTCTL_CSRSTDONE  (1u << 29)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    RESULT[0] = 0x55534231u; /* "USB1" */
    RESULT[1] = USB_GSNPSID;
    RESULT[2] = USB_GRXFSIZ;
    RESULT[3] = USB_GRSTCTL;

    USB_GRSTCTL = GRSTCTL_CSRST;
    RESULT[4] = USB_GRSTCTL;
    RESULT[5] = 1u;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
