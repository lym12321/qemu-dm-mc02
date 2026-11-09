/* Test-only UART5 RX consumer. Register addresses/fields follow the pinned
 * STM32H723 CMSIS header and RM0468; common board/UART1 startup is reused. */
#include "board.h"
volatile uint32_t ticks_ms;
void SysTick_Handler(void) { ++ticks_ms; }
void firmware_main(void)
{
    board_init();
    RCC_AHB4ENR |= 1u << 3; /* GPIOD. */
    REG32(0x580244e8u) |= 1u << 20; /* RCC_APB1LENR.UART5EN. */
    REG32(0x58020c00u) = (REG32(0x58020c00u) & ~(3u << 4)) | (2u << 4);
    REG32(0x58020c20u) = (REG32(0x58020c20u) & ~(15u << 8)) | (8u << 8);
    REG32(0x4000500cu) = 556u;
    REG32(0x40005000u) = 1u | (1u << 2); /* UE, RE; no TX. */
    uart_text("REMOTE_READY\n");
    uint8_t bytes[4];
    for (unsigned i = 0; i < sizeof(bytes); ++i) {
        while (!(REG32(0x4000501cu) & (1u << 5))) {
            if (ticks_ms >= 30u) { uart_text("REMOTE_FAIL\n"); for (;;) { idle(); } }
            idle();
        }
        bytes[i] = REG32(0x40005024u);
    }
    uart_text("REMOTE_READ:");
    for (unsigned i = 0; i < sizeof(bytes); ++i) {
        while (!(USART1_ISR & (1u << 6))) { }
        USART1_TDR = bytes[i];
    }
    uart_text("\nREMOTE_PASS\n");
    for (;;) { idle(); }
}
