#include <stdint.h>

extern uint32_t _sidata, _sdata, _edata, _sbss, _ebss, _stack_top;
void Reset_Handler(void);
void firmware_main(void);
void SysTick_Handler(void);

static void fault(void)
{
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}

/* ARMv7-M exception numbers and SysTick are the native CPU/NVIC boundary. */
__attribute__((used, section(".isr_vector")))
const uintptr_t vector_table[16] = {
    [0] = (uintptr_t)&_stack_top,
    [1] = (uintptr_t)Reset_Handler,
    [2] = (uintptr_t)fault, [3] = (uintptr_t)fault,
    [4] = (uintptr_t)fault, [5] = (uintptr_t)fault,
    [6] = (uintptr_t)fault,
    [15] = (uintptr_t)SysTick_Handler,
};

void Reset_Handler(void)
{
    uint32_t *source = &_sidata;
    for (uint32_t *target = &_sdata; target < &_edata; ++target) {
        *target = *source++;
    }
    for (uint32_t *target = &_sbss; target < &_ebss; ++target) {
        *target = 0;
    }
    firmware_main();
    fault();
}
