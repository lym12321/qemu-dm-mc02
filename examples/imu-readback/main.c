/* Actual polled SPI consumer, using the existing test-only BMI088 helpers.
 * BMI088 register/range/DRDY/sensortime fields: Bosch datasheet Rev1.9,
 * Jan2024, sections 5.3 and 5.5. This is a simulation fixture, not a board BSP.
 */
#include "board.h"

#define SPI_CR1 REG32(0x40003800u)
#define SPI_SR REG32(0x40003814u)
#define SPI_TXDR REG32(0x40003820u)
#define SPI_RXDR REG32(0x40003830u)

#include "dm_mc02_bmi088_spi_fixture.h"

volatile uint32_t ticks_ms;
struct Readback {
    uint8_t gyro[6];
    uint8_t accel[6];
    uint32_t sensor_ticks;
};
static volatile struct Readback readbacks[2];

void SysTick_Handler(void)
{
    ++ticks_ms;
}

static void fail(void)
{
    uart_text("IMU_FAIL\n");
    for (;;) { idle(); }
}

static void check_watchdog(void)
{
    if (ticks_ms >= 90u) { fail(); }
}

static void wait_ms(uint32_t duration)
{
    const uint32_t start = ticks_ms;
    while (ticks_ms - start < duration) {
        check_watchdog();
        idle();
    }
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
    uart_text("IMU_READ n=");
    uart_hex(index + 1u, 1);
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
    GPIOC_BSRR = 9u; /* Deassert PC0 accel and PC3 gyro before output mode. */
    GPIOC_MODER = (GPIOC_MODER & ~((3u << 0) | (3u << 6))) |
                  (1u << 0) | (1u << 6);
    SPI_CR1 = 1u;
    bmi_write_byte(1, 0x7c, 0x00); /* Accelerometer normal power mode. */
    bmi_write_byte(1, 0x7d, 0x04); /* Accelerometer enabled. */
    bmi_write_byte(8, 0x11, 0x00); /* Gyro normal mode. */
    bmi_write_byte(8, 0x0f, 0x01); /* +/-1000 degrees/s. */
    bmi_write_byte(8, 0x10, 0x05); /* 100Hz, 12Hz bandwidth. */
    bmi_write_byte(1, 0x40, 0x88); /* 100Hz, normal bandwidth. */
    bmi_write_byte(1, 0x41, 0x01); /* +/-6g. */
    /* Native SysTick, rather than an instruction-count window. The modeled
     * startup contract is not a physical power-up timing validation. */
    wait_ms(30u);
    uart_text("SIM_READY imu-readback\n");

    for (unsigned index = 0; index < 2; ++index) {
        uint8_t status, gyro[6] = {0}, accel[6], sensor[3];
        for (;;) {
            check_watchdog();
            bmi_read_bytes(1, 0x03, 1, &status, 1);
            if (status & 0x80u) { break; } /* Actual accelerometer DRDY. */
            idle();
        }
#ifndef SIM_IMU_SKIP_GYRO_TEST
        bmi_read_bytes(8, 0x02, 0, gyro, sizeof(gyro));
#endif
        bmi_read_bytes(1, 0x12, 1, accel, sizeof(accel));
        bmi_read_bytes(1, 0x18, 1, sensor, sizeof(sensor));
        for (unsigned i = 0; i < 6; ++i) {
            readbacks[index].gyro[i] = gyro[i];
            readbacks[index].accel[i] = accel[i];
        }
        /* This final volatile store precedes UART publication, including in
         * Release. DONE alone does not attest completion of this guest store. */
        readbacks[index].sensor_ticks = (uint32_t)sensor[0] |
            ((uint32_t)sensor[1] << 8) | ((uint32_t)sensor[2] << 16);
        if (!readbacks[index].sensor_ticks ||
            (index && readbacks[index].sensor_ticks <= readbacks[index - 1].sensor_ticks)) {
            fail();
        }
        report(index);
    }
    check_watchdog();
    uart_text("IMU_PASS\n");
    for (;;) { idle(); }
}
