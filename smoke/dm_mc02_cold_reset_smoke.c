#include <stdint.h>

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    volatile uint32_t *result = (volatile uint32_t *)0x20000000u;
    volatile uint32_t *boot_count = (volatile uint32_t *)0x20000010u;

    if (++*boot_count == 1) {
        result[0] = 0x434f4c44u; /* "COLD" */
    } else {
        result[0] = 0x5741524du; /* "WARM" */
        result[1] = *boot_count;
    }
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
