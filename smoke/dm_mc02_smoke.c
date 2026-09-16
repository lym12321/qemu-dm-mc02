#include <stdint.h>

#define DTCM_MARKER ((volatile uint32_t *)0x20000000u)
#define FLASH_VECTOR ((volatile uint32_t *)0x08000000u)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    /* These writes prove that QEMU loaded the vector table into physical
     * flash, selected its MSP/PC, and executed the handler on the M7 core. */
    DTCM_MARKER[0] = 0x444D4331u;
    DTCM_MARKER[1] = FLASH_VECTOR[0];
    DTCM_MARKER[2] = FLASH_VECTOR[1];
    DTCM_MARKER[3] = 0x20000000u;
    for (;;) {
        __asm__ volatile ("wfi");
    }
}
