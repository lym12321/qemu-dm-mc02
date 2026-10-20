/* Digital DM-MC02 fixture, not complete silicon/physical initialization.
 * Addresses/fields: STM32H723 CMSIS (pinned third_party/stm32h7) and RM0468.
 * UART, GPIO and FDCAN sequencing reuses smoke/dm_mc02_lab_smoke.c and
 * smoke/dm_mc02_fdcan_smoke.c. Reset HSI/HCLK/kernel clocks are 64MHz.
 */
#ifndef SIM_EXAMPLE_BOARD_H
#define SIM_EXAMPLE_BOARD_H
#include <stdint.h>

#define REG32(address) (*(volatile uint32_t *)(address))
#define RCC_AHB4ENR REG32(0x580244e0u)
#define RCC_APB2ENR REG32(0x580244f0u)
#define RCC_APB1HENR REG32(0x580244ecu)
#define GPIOA_MODER REG32(0x58020000u)
#define GPIOA_PUPDR REG32(0x5802000cu)
#define GPIOA_IDR REG32(0x58020010u)
#define GPIOA_AFRH REG32(0x58020024u)
#define GPIOB_MODER REG32(0x58020400u)
#define GPIOB_BSRR REG32(0x58020418u)
#define GPIOC_MODER REG32(0x58020800u)
#define GPIOC_BSRR REG32(0x58020818u)
#define USART1_CR1 REG32(0x40011000u)
#define USART1_BRR REG32(0x4001100cu)
#define USART1_ISR REG32(0x4001101cu)
#define USART1_TDR REG32(0x40011028u)

extern volatile uint32_t ticks_ms;

static inline void board_init(void)
{
    RCC_AHB4ENR |= 7u; /* GPIOA/B/C clocks. */
    RCC_APB2ENR |= 1u << 4; /* USART1EN. */
    GPIOA_MODER = (GPIOA_MODER & ~((3u << 18) | (3u << 20) | (3u << 30))) |
                  (2u << 18) | (2u << 20); /* PA9/10 AF, PA15 input. */
    GPIOA_AFRH = (GPIOA_AFRH & ~((15u << 4) | (15u << 8))) |
                 (7u << 4) | (7u << 8); /* USART1 AF7. */
    GPIOA_PUPDR = (GPIOA_PUPDR & ~(3u << 30)) | (1u << 30);
    GPIOB_BSRR = 1u << 16;
    GPIOB_MODER = (GPIOB_MODER & ~3u) | 1u;
    USART1_CR1 = 0;
    USART1_BRR = 556u; /* round(64MHz/115200), oversampling16, 8N1. */
    USART1_CR1 = 1u | (1u << 2) | (1u << 3);
    REG32(0xe000e014u) = 63999u; /* SysTick reload: exactly 1ms at 64MHz. */
    REG32(0xe000e018u) = 0;
    REG32(0xe000e010u) = 7u; /* ENABLE, TICKINT, processor clock. */
}

static inline void uart_text(const char *text)
{
    while (*text) {
        while (!(USART1_ISR & (1u << 6))) { /* Previous byte completed. */ }
        USART1_TDR = (uint8_t)*text++;
    }
}

static inline void idle(void)
{
    __asm__ volatile ("wfi" ::: "memory");
}
#endif
