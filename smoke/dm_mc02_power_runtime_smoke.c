#include <stdint.h>

#define TICKS ((volatile uint32_t *)0x20000000u)
#define BOOTS ((volatile uint32_t *)0x20000004u)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

void Reset_Handler(void)
{
    (*BOOTS)++;
    for (;;) {
        (*TICKS)++;
        __asm__ volatile ("nop" ::: "memory");
    }
}
