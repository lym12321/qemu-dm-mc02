#include <stdint.h>

#define PWR_BASE 0x58024800u
#define RCC_BASE 0x58024400u
#define REG(base, offset) (*(volatile uint32_t *)((base) + (offset)))

#define PWR_CR3   REG(PWR_BASE, 0x08u)
#define PWR_CSR1  REG(PWR_BASE, 0x04u)
#define PWR_CPUCR REG(PWR_BASE, 0x10u)
#define PWR_D3CR  REG(PWR_BASE, 0x18u)

#define RCC_CR      REG(RCC_BASE, 0x00u)
#define RCC_CFGR    REG(RCC_BASE, 0x10u)
#define RCC_D1CFGR  REG(RCC_BASE, 0x18u)
#define RCC_D2CFGR  REG(RCC_BASE, 0x1cu)
#define RCC_D3CFGR  REG(RCC_BASE, 0x20u)
#define RCC_PLLCKSELR REG(RCC_BASE, 0x28u)
#define RCC_PLLCFGR   REG(RCC_BASE, 0x2cu)
#define RCC_PLL1DIVR  REG(RCC_BASE, 0x30u)
#define RCC_AHB4ENR REG(RCC_BASE, 0xe0u)

#define MARKER_PASS  0x50575231u /* PWR1 */
#define MARKER_FAULT 0x46415531u /* FAU1 */

enum {
    RESULT_MARKER = 0,
    RESULT_PWR_CR3 = 1,
    RESULT_PWR_D3CR = 2,
    RESULT_PWR_CSR1 = 3,
    RESULT_PWR_CPUCR = 4,
    RESULT_RCC_CR = 5,
    RESULT_RCC_CFGR = 6,
    RESULT_RCC_D1CFGR = 7,
    RESULT_RCC_D2CFGR = 8,
    RESULT_RCC_D3CFGR = 9,
    RESULT_RCC_AHB4ENR = 10,
    RESULT_FAULT_IPSR = 11,
    RESULT_HSE_PENDING_CFGR = 12,
    RESULT_HSE_PENDING_CR = 13,
    RESULT_HSE_READY_CFGR = 14,
    RESULT_HSE_READY_CR = 15,
    RESULT_PLL_INVALID_CFGR = 16,
    RESULT_PLL_INVALID_CR = 17,
    RESULT_PLL_READY_CFGR = 18,
    RESULT_PLL_READY_CR = 19,
};

static volatile uint32_t *const result = (volatile uint32_t *)0x20000000u;

void Reset_Handler(void);
void HardFault_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
    0u,
    (uint32_t)(uintptr_t)HardFault_Handler,
};

void HardFault_Handler(void)
{
    uint32_t ipsr;

    __asm__ volatile ("mrs %0, ipsr" : "=r"(ipsr));
    result[RESULT_MARKER] = MARKER_FAULT;
    result[RESULT_FAULT_IPSR] = ipsr;
    for (;;) {
        __asm__ volatile ("wfi");
    }
}

void Reset_Handler(void)
{
    uint32_t value;

    result[RESULT_MARKER] = 0x50575230u; /* PWR0: handler started */

    /* Exercise control/configuration writes, then retain the readback. */
    PWR_CR3 = 0x00000001u;
    PWR_D3CR = 0x0000c000u;
    RCC_CR = 0x00000001u;
    RCC_CFGR = 0x00000000u;
    RCC_D1CFGR = 0x00000000u;
    RCC_D2CFGR = 0x00000000u;
    RCC_D3CFGR = 0x00000000u;
    RCC_AHB4ENR = 0x00000001u;

    /* SW is a request: a disabled HSE must not retime the system clock. */
    RCC_CFGR = 2u;
    result[RESULT_HSE_PENDING_CFGR] = RCC_CFGR;
    result[RESULT_HSE_PENDING_CR] = RCC_CR;
    RCC_CR = 0x00010001u; /* HSION | HSEON */
    result[RESULT_HSE_READY_CFGR] = RCC_CFGR;
    result[RESULT_HSE_READY_CR] = RCC_CR;

    /* Return to HSI, then prove an invalid PLL request remains pending. */
    RCC_CFGR = 0u;
    RCC_CR = 0x00000001u;
    RCC_CFGR = 3u;
    result[RESULT_PLL_INVALID_CFGR] = RCC_CFGR;
    result[RESULT_PLL_INVALID_CR] = RCC_CR;

    /* The saved PLL request takes effect when its source and dividers become
     * valid, without requiring a second CFGR write. */
    RCC_PLLCKSELR = 2u << 4; /* HSI, M=2 */
    RCC_PLL1DIVR = 39u;      /* N=40, P=1 */
    RCC_PLLCFGR = 1u << 16;  /* enable PLL1P */
    RCC_CR = 0x01000001u;    /* HSION | PLL1ON */
    result[RESULT_PLL_READY_CFGR] = RCC_CFGR;
    result[RESULT_PLL_READY_CR] = RCC_CR;

    /* Leave the fixture in its reset clock state for the ordinary checks. */
    RCC_CFGR = 0u;
    RCC_CR = 0x00000001u;

    result[RESULT_PWR_CR3] = PWR_CR3;
    result[RESULT_PWR_D3CR] = PWR_D3CR;
    result[RESULT_PWR_CSR1] = PWR_CSR1;
    result[RESULT_PWR_CPUCR] = PWR_CPUCR;
    result[RESULT_RCC_CR] = RCC_CR;
    result[RESULT_RCC_CFGR] = RCC_CFGR;
    result[RESULT_RCC_D1CFGR] = RCC_D1CFGR;
    result[RESULT_RCC_D2CFGR] = RCC_D2CFGR;
    result[RESULT_RCC_D3CFGR] = RCC_D3CFGR;
    value = RCC_AHB4ENR;
    result[RESULT_RCC_AHB4ENR] = value;

    /* CSR1/CR are status windows: a readable result is part of the contract. */
    if ((result[RESULT_PWR_CSR1] & (1u << 13)) == 0 ||
        (result[RESULT_RCC_CR] & (1u << 2)) == 0) {
        result[RESULT_MARKER] = 0x50575232u; /* PWR2: no ready status */
    } else {
        result[RESULT_MARKER] = MARKER_PASS;
    }
    for (;;) {
        __asm__ volatile ("wfi");
    }
}
