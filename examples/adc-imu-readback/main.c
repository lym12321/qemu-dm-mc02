/* Test-only joint ADC/BMI088 consumer. ADC regular configuration is retained
 * from smoke/dm_mc02_lab_smoke.c (STM32H723 CMSIS/RM0468). SPI transactions,
 * ranges and startup sequencing reuse examples/imu-readback (Bosch Rev1.9).
 * Actual readbacks and matching identities must be checked by the host.
 */
#include "board.h"

#define ADC1_ISR REG32(0x40022000u)
#define ADC1_CR REG32(0x40022008u)
#define ADC1_CFGR REG32(0x4002200cu)
#define ADC1_SMPR1 REG32(0x40022014u)
#define ADC1_SQR1 REG32(0x40022030u)
#define ADC1_DR REG32(0x40022040u)
#define SPI_CR1 REG32(0x40003800u)
#define SPI_SR REG32(0x40003814u)
#define SPI_TXDR REG32(0x40003820u)
#define SPI_RXDR REG32(0x40003830u)

#include "dm_mc02_bmi088_spi_fixture.h"

volatile uint32_t ticks_ms;
struct Readback {
    uint32_t adc;
    uint8_t gyro[6];
    uint8_t accel[6];
    uint32_t sensor_ticks;
};
static volatile struct Readback readbacks[2];

void SysTick_Handler(void) { ++ticks_ms; }

static void fail(void)
{
    uart_text("ADC_IMU_FAIL\n");
    for (;;) { idle(); }
}

static void check_watchdog(void)
{
    if (ticks_ms >= 140u) { fail(); }
}

static void wait_ms(uint32_t duration)
{
    const uint32_t start = ticks_ms;
    while (ticks_ms - start < duration) { check_watchdog(); idle(); }
}

static void uart_hex(uint32_t value, unsigned digits)
{
    char text[9];
    static const char hex[] = "0123456789abcdef";
    for (unsigned i = 0; i < digits; ++i) {
        text[digits - i - 1] = hex[value & 15u];
        value >>= 4;
    }
    text[digits] = 0;
    uart_text(text);
}

static void report(unsigned index)
{
    uart_text("ADC_IMU_READ n=");
    uart_hex(index + 1u, 1);
    uart_text(" adc=");
    uart_hex(readbacks[index].adc, 4);
    uart_text(" g=");
    for (unsigned i = 0; i < 6; ++i) { uart_hex(readbacks[index].gyro[i], 2); }
    uart_text(" a=");
    for (unsigned i = 0; i < 6; ++i) { uart_hex(readbacks[index].accel[i], 2); }
    uart_text(" t=");
    uart_hex(readbacks[index].sensor_ticks, 6);
    uart_text("\n");
}

void firmware_main(void)
{
    board_init();
    GPIOC_BSRR = 9u;
    GPIOC_MODER = (GPIOC_MODER & ~((3u << 0) | (3u << 6))) |
                  (1u << 0) | (1u << 6);
    SPI_CR1 = 1u;
    bmi_write_byte(1, 0x7c, 0x00);
    bmi_write_byte(1, 0x7d, 0x04);
    bmi_write_byte(8, 0x11, 0x00);
    bmi_write_byte(8, 0x0f, 0x01); /* +/-1000 degrees/s. */
    bmi_write_byte(8, 0x10, 0x05); /* 100Hz, 12Hz bandwidth. */
    bmi_write_byte(1, 0x40, 0x88); /* 100Hz, normal bandwidth. */
    bmi_write_byte(1, 0x41, 0x01); /* +/-6g. */

    ADC1_SQR1 = 4u << 6; /* One regular rank, channel4. */
    ADC1_SMPR1 = 7u << 12;
    ADC1_CFGR = (1u << 13) | (1u << 12); /* 16bit continuous, overwrite OVR. */
    ADC1_CR = 1u;
    ADC1_CR = 1u | (1u << 2);
    wait_ms(30u);
    uart_text("ADC_IMU_READY\n");

    const uint32_t expected[] = {12345u, 32768u};
    for (unsigned index = 0; index < 2; ++index) {
        uint8_t gyro[6] = {0}, accel[6], sensor[3], status;
        uint32_t adc;
        for (;;) {
            check_watchdog();
            if (ADC1_ISR & (1u << 2)) {
                adc = ADC1_DR; /* Actual completed conversion, not host ACK. */
                ADC1_ISR = (1u << 3) | (1u << 4);
                if (adc == expected[index]) { break; }
            }
            idle();
        }
        bmi_read_bytes(1, 0x03, 1, &status, 1);
        if (!(status & 0x80u)) { fail(); }
#ifndef SIM_ADC_IMU_SKIP_GYRO_TEST
        bmi_read_bytes(8, 0x02, 0, gyro, sizeof(gyro));
#endif
        bmi_read_bytes(1, 0x12, 1, accel, sizeof(accel));
        bmi_read_bytes(1, 0x18, 1, sensor, sizeof(sensor));
        readbacks[index].adc = adc;
        for (unsigned i = 0; i < 6; ++i) {
            readbacks[index].gyro[i] = gyro[i];
            readbacks[index].accel[i] = accel[i];
        }
        /* This final volatile store precedes UART, including in Release. */
        readbacks[index].sensor_ticks = (uint32_t)sensor[0] |
            ((uint32_t)sensor[1] << 8) | ((uint32_t)sensor[2] << 16);
        if (!readbacks[index].sensor_ticks || (index &&
                readbacks[index].sensor_ticks <= readbacks[index - 1].sensor_ticks)) {
            fail();
        }
        report(index);
    }
    check_watchdog();
    uart_text("ADC_IMU_PASS\n");
    for (;;) { idle(); }
}
