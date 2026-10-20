#include "board.h"

#define FDCAN_CCCR REG32(0x4000a018u)
#define FDCAN_GFC REG32(0x4000a080u)
#define FDCAN_SIDFC REG32(0x4000a084u)
#define FDCAN_RXF0C REG32(0x4000a0a0u)
#define FDCAN_RXF0S REG32(0x4000a0a4u)
#define FDCAN_RXF0A REG32(0x4000a0a8u)
#define FDCAN_RXESC REG32(0x4000a0bcu)
#define FDCAN_TXBC REG32(0x4000a0c0u)
#define FDCAN_TXESC REG32(0x4000a0c8u)
#define FDCAN_TXBAR REG32(0x4000a0d0u)
#define MSGRAM ((volatile uint32_t *)0x4000ac00u)

volatile uint32_t ticks_ms;

void SysTick_Handler(void)
{
    ++ticks_ms;
}

static void fail(void)
{
    uart_text("MOTOR_FAIL\n");
    for (;;) { idle(); }
}

void firmware_main(void)
{
    uint32_t command_at;
    unsigned phase = 0;
    board_init();
    RCC_APB1HENR |= 1u << 8; /* FDCANEN; CMSIS RCC_APB1HENR. */
    GPIOC_MODER = (GPIOC_MODER & ~(3u << 30)) | (1u << 30);
    GPIOC_BSRR = 1u << 15; /* Actual switched CAN-transceiver rail. */
    FDCAN_CCCR = 3u;
    FDCAN_TXBC = 1u << 24; /* TFQS1, NDTB0, FIFO at byte0. */
    FDCAN_TXESC = 0; /* Classic eight-byte elements. */
    FDCAN_RXF0C = 128u | (8u << 16); /* Eight elements at byte128. */
    FDCAN_RXESC = 0;
    FDCAN_SIDFC = 400u | (1u << 16);
    MSGRAM[100] = (2u << 30) | (1u << 27) | (0x201u << 16) | 0x7ffu;
    FDCAN_GFC = (2u << 4) | (2u << 2) | 3u;
    FDCAN_CCCR = 0;
    uart_text("SIM_READY can-motor\n");
    MSGRAM[0] = 0x200u << 18;
    MSGRAM[1] = 8u << 16;
    MSGRAM[2] = 0x00004000u; /* Wire 00 40 00 00: motor1 raw64. */
    MSGRAM[3] = 0;
    command_at = ticks_ms;
    FDCAN_TXBAR = 1u;
    for (;;) {
        while (FDCAN_RXF0S & 0x7fu) {
            const uint32_t index = (FDCAN_RXF0S >> 8) & 0x3fu;
            volatile uint32_t *rx = MSGRAM + 32u + index * 4u;
            const uint32_t low = rx[2], high = rx[3];
            const uint32_t raw = ((high & 0xffu) << 8) | ((high >> 8) & 0xffu);
            if (rx[0] != (0x201u << 18) ||
                (rx[1] & 0x003f0000u) != (8u << 16) ||
                low != 0 || (high >> 16) != 25u ||
                (raw != 0 && raw != 64)) { fail(); }
            FDCAN_RXF0A = index;
            if (!phase && raw == 64) {
                phase = 1;
                uart_text("MOTOR_ACTIVE current=64 encoder=0 rpm=0\n");
            } else if (phase == 1 && raw == 0) {
                if (ticks_ms - command_at < 100u) { fail(); }
                phase = 2;
                uart_text("MOTOR_TIMEOUT current=0\nMOTOR_PASS\n");
            }
        }
        if (ticks_ms > 175u && phase != 2) { fail(); }
        idle();
    }
}
