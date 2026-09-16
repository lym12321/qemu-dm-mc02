#include <stdint.h>

#define CORDIC_BASE       0x58004400u
#define CORDIC_CSR        (*(volatile uint32_t *)(CORDIC_BASE + 0x00u))
#define CORDIC_WDATA      (*(volatile uint32_t *)(CORDIC_BASE + 0x04u))
#define CORDIC_RDATA      (*(volatile uint32_t *)(CORDIC_BASE + 0x08u))
#define USB_GRXFSIZ       (*(volatile uint32_t *)0x40040024u)
#define RESULT            ((volatile uint32_t *)0x20000000u)
#define BOOT_COUNT        (*(volatile uint32_t *)0x20000040u)
#define CORDIC_NRES       (1u << 19)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    uint32_t boot = ++BOOT_COUNT;

    if (boot == 1) {
        RESULT[0] = 0x52535431u; /* RST1 */
        /* Leave a paired CORDIC result pending across the warm reset. */
        CORDIC_CSR = CORDIC_NRES;
        CORDIC_WDATA = 0x20000000u;
        USB_GRXFSIZ = 77u;
    } else {
        RESULT[0] = 0x52535432u; /* RST2 */
        RESULT[1] = CORDIC_CSR;
        RESULT[2] = CORDIC_RDATA;
        RESULT[3] = USB_GRXFSIZ;
        RESULT[4] = boot;
    }

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
