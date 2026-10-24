/* Actual UART1 RX consumer. Register fields: pinned STM32H723 CMSIS/RM0468.
 * The host echo owns no parser. The checksum and B0 transition below require
 * all eight actual RDR reads; the explicit negative omits those reads.
 */
#include "board.h"

#define USART1_RDR REG32(0x40011024u)

volatile uint32_t ticks_ms;

void SysTick_Handler(void)
{
    ++ticks_ms;
}

static void uart_byte(uint8_t byte)
{
    while (!(USART1_ISR & (1u << 6))) { }
    USART1_TDR = byte;
}

#ifndef SIM_UART_SKIP_RX_TEST
static void uart_hex16(uint32_t value)
{
    static const char digits[] = "0123456789abcdef";
    for (int shift = 12; shift >= 0; shift -= 4) {
        uart_byte((uint8_t)digits[(value >> shift) & 15u]);
    }
}
#endif

void firmware_main(void)
{
    static const uint8_t challenge[] = {0x00u, 0x7fu, 0x80u, 0xffu,
                                        0x35u, 0xa6u, 0x10u, 0x53u};
    board_init();
    /* No ready text precedes the binary request: the independent echo would
     * correctly return that text as input, too. Establish a real tick baseline.
     */
    while (ticks_ms < 5u) { idle(); }
    for (unsigned i = 0; i < sizeof(challenge); ++i) { uart_byte(challenge[i]); }
#ifdef SIM_UART_SKIP_RX_TEST
    /* Deliberately misleading correct checksum/PASS; host must also require
     * the later GPIO state that only the normal RX-dependent path produces.
     */
    while (ticks_ms < 12u) { idle(); }
    uart_text("SIM_READY uart-link\nUART_RX sum=033c n=8\nUART_PASS\n");
#else
    uint32_t sum = 0u;
    unsigned received = 0u;
    while (received < sizeof(challenge)) {
        if (USART1_ISR & (1u << 5)) {
            const uint8_t byte = (uint8_t)USART1_RDR;
            if (byte != challenge[received]) {
                uart_text("UART_FAIL bytes\n");
                for (;;) { idle(); }
            }
            sum += byte;
            ++received;
        } else {
            if (ticks_ms >= 25u) {
                uart_text("UART_FAIL timeout\n");
                for (;;) { idle(); }
            }
            idle();
        }
    }
    GPIOB_BSRR = 1u;
    uart_text("SIM_READY uart-link\nUART_RX sum=");
    uart_hex16(sum);
    uart_text(" n=8\nUART_PASS\n");
#endif
    for (;;) { idle(); }
}
