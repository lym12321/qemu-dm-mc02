/* Test-only two-board shared-world consumer. Register setup comes from the
 * existing Lab/FDCAN/BMI088 fixtures; fields follow H723 CMSIS/RM0468 and
 * Bosch BMI088 register definitions. No HAL or physical calibration claim. */
#include <stdint.h>

#ifndef PEER
#define PEER 0
#endif

#define REG32(address) (*(volatile uint32_t *)(address))
#define USART1_CR1  REG32(0x40011000u)
#define USART1_BRR  REG32(0x4001100cu)
#define USART1_ISR  REG32(0x4001101cu)
#define USART1_TDR  REG32(0x40011028u)
#define RCC_AHB4ENR REG32(0x580244e0u)
#define GPIOA_MODER REG32(0x58020000u)
#define GPIOA_IDR   REG32(0x58020010u)
#define GPIOC_MODER REG32(0x58020800u)
#define GPIOC_ODR   REG32(0x58020814u)
#define GPIOC_BSRR  REG32(0x58020818u)
#define SPI_CR1     REG32(0x40003800u)
#define SPI_SR      REG32(0x40003814u)
#define SPI_TXDR    REG32(0x40003820u)
#define SPI_RXDR    REG32(0x40003830u)
#define FDCAN1_CCCR REG32(0x4000a018u)
#define FDCAN1_GFC  REG32(0x4000a080u)
#define FDCAN1_SIDFC REG32(0x4000a084u)
#define FDCAN1_RXF0C REG32(0x4000a0a0u)
#define FDCAN1_RXF0S REG32(0x4000a0a4u)
#define FDCAN1_RXF0A REG32(0x4000a0a8u)
#define FDCAN1_RXESC REG32(0x4000a0bcu)
#define FDCAN1_TXBC REG32(0x4000a0c0u)
#define FDCAN1_TXESC REG32(0x4000a0c8u)
#define FDCAN1_TXBAR REG32(0x4000a0d0u)
#define FDCAN1_TXBTO REG32(0x4000a0d8u)
#define MSGRAM ((volatile uint32_t *)0x4000ac00u)

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

#include "dm_mc02_bmi088_spi_fixture.h"

static void uart_char(uint8_t value)
{
    while ((USART1_ISR & (1u << 6)) == 0) {
    }
    USART1_TDR = value;
}

static void uart_text(const char *text)
{
    while (*text) {
        uart_char((uint8_t)*text++);
    }
}

static void uart_hex(uint32_t value, unsigned nibbles)
{
    while (nibbles) {
        unsigned digit = (value >> (4u * --nibbles)) & 15u;
        uart_char((uint8_t)(digit < 10u ? '0' + digit : 'a' + digit - 10u));
    }
}

static void uart_bytes(const uint8_t *bytes, unsigned count)
{
    for (unsigned i = 0; i < count; ++i) {
        uart_hex(bytes[i], 2);
    }
}

void Reset_Handler(void)
{
    uint8_t gyro[6], accel[6], sensor_time[3], status;
    uint8_t feedback[2][8];
    volatile uint32_t *tx = MSGRAM;
    volatile uint32_t final_sensor_time = 0;

    USART1_BRR = 556u;
    USART1_CR1 = 1u | (1u << 2) | (1u << 3);
    RCC_AHB4ENR |= (1u << 0) | (1u << 2);
    GPIOA_MODER &= ~(3u << 30); /* Actual active-low A15 user key. */
    GPIOC_MODER = (GPIOC_MODER & ~((3u << 0) | (3u << 6) | (3u << 30))) |
                  (1u << 0) | (1u << 6) | (1u << 30);
    GPIOC_ODR |= 1u << 15; /* Switched transceiver rail. */
    SPI_CR1 = 1;
    bmi_write_byte(1, 0x7c, 0x00);
    bmi_write_byte(1, 0x7d, 0x04);
    bmi_write_byte(8, 0x11, 0x00);
    bmi_write_byte(8, 0x0f, 0x01); /* Gyro +/-1000 degrees/s. */
    bmi_write_byte(8, 0x10, 0x05); /* 100 Hz. */
    bmi_write_byte(1, 0x40, 0x88); /* Accelerometer 100 Hz. */
    bmi_write_byte(1, 0x41, 0x01); /* +/-6g. */

    FDCAN1_CCCR = 3u;
    FDCAN1_TXBC = 1u << 24;
    FDCAN1_TXESC = 0;
    FDCAN1_RXF0C = 128u | (2u << 16);
    FDCAN1_RXESC = 0;
    FDCAN1_SIDFC = 400u | (1u << 16);
    MSGRAM[100] = (1u << 27) | (0x201u << 16) | 0x202u; /* Range. */
    FDCAN1_GFC = (2u << 4) | (2u << 2) | 3u;
    FDCAN1_CCCR = 0;
    uart_text("WORLD_READY=");
    uart_hex(PEER, 1);
    uart_char('\n');
    /* Only an actual host key event releases this command. */
    while (GPIOA_IDR & (1u << 15)) {
    }
    tx[0] = 0x200u << 18;
    tx[1] = 8u << 16;
    tx[2] = PEER ? 0x00e000f0u : 0x00200010u;
    tx[3] = 0;
    FDCAN1_TXBAR = 1u;
    while ((FDCAN1_TXBTO & 1u) == 0) {
    }

    for (unsigned i = 0; i < 2; ++i) {
        while ((FDCAN1_RXF0S & 0x7fu) == 0) {
        }
        uint32_t slot = (FDCAN1_RXF0S >> 8) & 0x3fu;
        volatile uint32_t *rx = MSGRAM + 32u + 4u * slot;
        if (rx[0] != ((0x201u + i) << 18) ||
            (rx[1] & 0x003f0000u) != (8u << 16)) {
            uart_text("WORLD_BAD_CAN\n");
            for (;;) {
            }
        }
        uint32_t lo = rx[2], hi = rx[3];
        for (unsigned b = 0; b < 4; ++b) {
            feedback[i][b] = lo >> (8u * b);
            feedback[i][4u + b] = hi >> (8u * b);
        }
        FDCAN1_RXF0A = slot;
    }
    /* Accel DRDY remains latched until the complete raw read. The gyro's
     * short status pulse is deliberately not used as a host timing barrier. */
    do {
        bmi_read_bytes(1, 0x03, 1, &status, 1);
    } while (!(status & 0x80u));
    bmi_read_bytes(8, 0x02, 0, gyro, 6);
    bmi_read_bytes(1, 0x12, 1, accel, 6);
    bmi_read_bytes(1, 0x18, 1, sensor_time, 3);
    final_sensor_time = (uint32_t)sensor_time[0] |
                        ((uint32_t)sensor_time[1] << 8) |
                        ((uint32_t)sensor_time[2] << 16);
    /* This UART line follows the final volatile store and both raw bursts. */
    uart_text("WORLD_RESULT=");
    uart_hex(PEER, 1);
    uart_char(' ');
    uart_bytes(feedback[0], 8);
    uart_char(' ');
    uart_bytes(feedback[1], 8);
    uart_char(' ');
    uart_bytes(gyro, 6);
    uart_char(' ');
    uart_bytes(accel, 6);
    uart_char(' ');
    uart_hex(final_sensor_time, 6);
    uart_char('\n');
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
