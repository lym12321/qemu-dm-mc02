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
#ifdef SIM_JOINT_LOOP_TEST
    uart_text("JOINT_FAIL\n");
#elif defined(SIM_ROTOR_LOOP_TEST)
    uart_text("ROTOR_FAIL\n");
#else
    uart_text("MOTOR_FAIL\n");
#endif
    for (;;) { idle(); }
}

void firmware_main(void)
{
    uint32_t command_at;
    unsigned phase = 0;
#ifdef SIM_JOINT_LOOP_TEST
    unsigned moving = 0, coast = 0;
#endif
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
#ifdef SIM_JOINT_LOOP_TEST
    FDCAN_SIDFC = 400u | (2u << 16);
    MSGRAM[101] = (2u << 30) | (1u << 27) | (0x202u << 16) | 0x7ffu;
#endif
    FDCAN_GFC = (2u << 4) | (2u << 2) | 3u;
    FDCAN_CCCR = 0;
    uart_text("SIM_READY can-motor\n");
    MSGRAM[0] = 0x200u << 18;
    MSGRAM[1] = 8u << 16;
#ifdef SIM_JOINT_LOOP_TEST
    MSGRAM[2] = 0x00f00010u; /* Wire 10 00 f0 00: motors1/2 raw+4096/-4096. */
#elif defined(SIM_ROTOR_LOOP_TEST)
    MSGRAM[2] = 0x00000010u; /* Wire 10 00 00 00: motor1 raw4096 (5A). */
#else
    MSGRAM[2] = 0x00004000u; /* Wire 00 40 00 00: motor1 raw64. */
#endif
    MSGRAM[3] = 0;
    command_at = ticks_ms;
    FDCAN_TXBAR = 1u;
    for (;;) {
        while (FDCAN_RXF0S & 0x7fu) {
#if defined(SIM_ROTOR_LOOP_TEST) || defined(SIM_JOINT_LOOP_TEST)
            if (ticks_ms - command_at >= 75u && phase != 2) { fail(); }
#endif
            const uint32_t index = (FDCAN_RXF0S >> 8) & 0x3fu;
            volatile uint32_t *rx = MSGRAM + 32u + index * 4u;
            const uint32_t low = rx[2], high = rx[3];
            const uint32_t raw = ((high & 0xffu) << 8) | ((high >> 8) & 0xffu);
#ifdef SIM_JOINT_LOOP_TEST
            const uint32_t encoder = ((low & 0xffu) << 8) | ((low >> 8) & 0xffu);
            const int16_t rpm = (int16_t)(((low >> 8) & 0xff00u) | (low >> 24));
            const int16_t current = (int16_t)raw;
            const uint32_t header = rx[0];
            if (header != (0x201u << 18) && header != (0x202u << 18)) { fail(); }
            const unsigned motor = header == (0x201u << 18) ? 0u : 1u;
            const int sign = motor == 0 ? 1 : -1;
            if ((rx[1] & 0x003f0000u) != (8u << 16) || encoder > 8191u ||
                rpm * sign < 0 || (high >> 16) != 25u ||
                (current != 0 && current * sign != 4096)) { fail(); }
            FDCAN_RXF0A = index;
            if (!phase && current * sign == 4096 && rpm * sign >= 2 && encoder > 0) {
                moving |= 1u << motor;
                if (moving == 3u) {
                    /* Both actual feedback reads above precede the only zero
                     * command. Submit it before serializing the UART marker. */
                    MSGRAM[0] = 0x200u << 18;
                    MSGRAM[1] = 8u << 16;
                    MSGRAM[2] = 0;
                    MSGRAM[3] = 0;
                    FDCAN_TXBAR = 1u;
                    phase = 1;
                    uart_text("JOINT_MOVING ids=1,2\n");
                }
            } else if (phase == 1 && current == 0 && rpm * sign > 0 && encoder > 0) {
                coast |= 1u << motor;
                if (coast == 3u) {
                    phase = 2;
                    uart_text("JOINT_COAST ids=1,2\nJOINT_PASS\n");
                }
            }
#elif defined(SIM_ROTOR_LOOP_TEST)
            const uint32_t encoder = ((low & 0xffu) << 8) | ((low >> 8) & 0xffu);
            const int16_t rpm = (int16_t)(((low >> 8) & 0xff00u) | (low >> 24));
            if (rx[0] != (0x201u << 18) ||
                (rx[1] & 0x003f0000u) != (8u << 16) ||
                encoder > 8191u || rpm < 0 || (high >> 16) != 25u ||
                (raw != 0 && raw != 4096)) { fail(); }
            FDCAN_RXF0A = index;
            if (!phase && raw == 4096 && rpm >= 2 && encoder > 0) {
                /* The actual feedback read above causes the only zero command.
                 * Submit it before the UART log, whose bytes take guest time. */
                MSGRAM[0] = 0x200u << 18;
                MSGRAM[1] = 8u << 16;
                MSGRAM[2] = 0;
                MSGRAM[3] = 0;
                FDCAN_TXBAR = 1u;
                phase = 1;
                uart_text("ROTOR_MOVING rpm>=2\n");
            } else if (phase == 1 && raw == 0 && rpm > 0 && encoder > 0) {
                phase = 2;
                uart_text("ROTOR_COAST current=0 motion=1\nROTOR_PASS\n");
            }
#else
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
#endif
        }
#if defined(SIM_ROTOR_LOOP_TEST) || defined(SIM_JOINT_LOOP_TEST)
        if (ticks_ms - command_at >= 75u && phase != 2) { fail(); }
#else
        if (ticks_ms > 175u && phase != 2) { fail(); }
#endif
        idle();
    }
}
