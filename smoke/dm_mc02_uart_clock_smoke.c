#include <stdint.h>

#define RCC_BASE       0x58024400u
#define RCC_CR         (*(volatile uint32_t *)(RCC_BASE + 0x00u))
#define RCC_D2CFGR     (*(volatile uint32_t *)(RCC_BASE + 0x1cu))
#define RCC_PLLCKSELR  (*(volatile uint32_t *)(RCC_BASE + 0x28u))
#define RCC_PLLCFGR    (*(volatile uint32_t *)(RCC_BASE + 0x2cu))
#define RCC_PLL1DIVR   (*(volatile uint32_t *)(RCC_BASE + 0x30u))
#define RCC_PLL2DIVR   (*(volatile uint32_t *)(RCC_BASE + 0x38u))
#define RCC_PLL3DIVR   (*(volatile uint32_t *)(RCC_BASE + 0x40u))
#define RCC_D2CCIP2R   (*(volatile uint32_t *)(RCC_BASE + 0x54u))
#define RCC_BDCR       (*(volatile uint32_t *)(RCC_BASE + 0x70u))

#define RESULT ((volatile uint32_t *)0x20000000u)
#define MARKER 0x55434c4bu /* UCLK */

#define RCC_CR_HSION    (1u << 0)
#define RCC_CR_CSION    (1u << 7)
#define RCC_CR_HSEON    (1u << 16)
#define RCC_CR_PLL1ON   (1u << 24)
#define RCC_CR_PLL2ON   (1u << 26)
#define RCC_CR_PLL3ON   (1u << 28)
#define RCC_BDCR_LSEON  (1u << 0)
#define RCC_PLL1DIVQEN  (1u << 17)
#define RCC_PLL2DIVQEN  (1u << 20)
#define RCC_PLL3DIVQEN  (1u << 23)

#ifndef UART_SOURCE
#define UART_SOURCE 0u
#endif
#ifndef UART_HSI_DIV
#define UART_HSI_DIV 1u
#endif
#ifndef UART_APB1_DIV
#define UART_APB1_DIV 5u
#endif
#ifndef UART_APB2_DIV
#define UART_APB2_DIV 4u
#endif

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

static void configure_sources(void)
{
    /* HSE=24 MHz.  PLL2Q=96 MHz, PLL3Q=60 MHz. */
    RCC_PLLCKSELR = 2u | (2u << 4) | (2u << 12) | (2u << 20);
    RCC_PLL1DIVR = 39u | (3u << 16);
    RCC_PLL2DIVR = 15u | (1u << 16);
    RCC_PLL3DIVR = 19u | (3u << 16);
    RCC_PLLCFGR = RCC_PLL1DIVQEN | RCC_PLL2DIVQEN | RCC_PLL3DIVQEN;

    /* HCLK=32 MHz, APB1=/4 and APB2=/2 for the APB source case. */
    RCC_D2CFGR = (UART_APB1_DIV << 4) | (UART_APB2_DIV << 8);
    RCC_CR = RCC_CR_HSION | RCC_CR_CSION | RCC_CR_HSEON |
             RCC_CR_PLL1ON | RCC_CR_PLL2ON | RCC_CR_PLL3ON |
             (UART_HSI_DIV << 3);
    RCC_BDCR = RCC_BDCR_LSEON;
    RCC_D2CCIP2R = (UART_SOURCE << 3) | UART_SOURCE;
}

void Reset_Handler(void)
{
    RESULT[0] = MARKER;
    RESULT[1] = 0;
    configure_sources();
    RESULT[1] = RCC_D2CCIP2R;
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
