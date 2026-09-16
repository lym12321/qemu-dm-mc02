#include <stdint.h>

#define GPIOC_MODER (*(volatile uint32_t *)0x58020800u)
#define GPIOC_BSRR (*(volatile uint32_t *)0x58020818u)
#define GPIOA_IDR  (*(volatile uint32_t *)0x58020010u)
#define SPI_CR1    (*(volatile uint32_t *)0x40003800u)
#define SPI_SR     (*(volatile uint32_t *)0x40003814u)
#define SPI_TXDR   (*(volatile uint32_t *)0x40003820u)
#define SPI_RXDR   (*(volatile uint32_t *)0x40003830u)

#define RESULT_MARKER 0x4649464fu /* "FIFO" */
#define POWER_OFF_MARKER 0x4f46464fu /* "OFFO" */
#define CONFIG_MARKER 0x434f4e46u /* "CONF" */

void Reset_Handler(void);

__attribute__((used, section(".isr_vector")))
const uint32_t vector_table[] = {
    0x20020000u,
    (uint32_t)(uintptr_t)Reset_Handler,
};

static uint8_t spi_byte(uint8_t value)
{
    while ((SPI_SR & (1u << 1)) == 0) {
    }
    SPI_TXDR = value;
    while ((SPI_SR & (1u << 0)) == 0) {
    }
    return (uint8_t)SPI_RXDR;
}

static void bmi_write(uint32_t deselect, uint8_t reg, uint8_t value)
{
    GPIOC_BSRR = deselect == 1 ? (1u << 16) : (1u << 19);
    (void)spi_byte(reg);
    (void)spi_byte(value);
    GPIOC_BSRR = deselect;
}

static uint8_t bmi_read(uint32_t deselect, uint8_t reg, int accel)
{
    uint8_t value;

    GPIOC_BSRR = deselect == 1 ? (1u << 16) : (1u << 19);
    (void)spi_byte(0x80u | reg);
    if (accel) {
        (void)spi_byte(0);
    }
    value = spi_byte(0);
    GPIOC_BSRR = deselect;
    return value;
}

static void bmi_read_fifo(uint32_t deselect, uint8_t reg, int accel,
                          uint8_t *out, unsigned count)
{
    GPIOC_BSRR = deselect == 1 ? (1u << 16) : (1u << 19);
    (void)spi_byte(0x80u | reg);
    if (accel) {
        (void)spi_byte(0);
    }
    for (unsigned i = 0; i < count; ++i) {
        out[i] = spi_byte(0);
    }
    GPIOC_BSRR = deselect;
}

void Reset_Handler(void)
{
    volatile uint32_t *result = (volatile uint32_t *)0x20000000u;
    uint8_t config_frame[2];
    uint8_t accel_fifo[25];
    uint8_t gyro_fifo[24];

    GPIOC_MODER = (GPIOC_MODER & ~((3u << 0) | (3u << 6))) |
                  (1u << 0) | (1u << 6);
    SPI_CR1 = 1;
    result[0] = RESULT_MARKER;

    /* Use the highest useful ODRs so three 1 ms-spaced co-sim samples are
     * all accepted.  The FIFO enable bits are the real BMI088 registers. */
    bmi_write(1, 0x49, 0x50); /* accel FIFO enable, documented bit4 set */
    bmi_write(1, 0x40, 0x8c); /* accel normal, 1600 Hz */
    bmi_write(8, 0x10, 0x80); /* gyro 2000 Hz / 532 Hz bandwidth */
    bmi_write(8, 0x3e, 0x80); /* gyro stream FIFO */

    /* Samples must not enter a disabled die, even when its FIFO is enabled. */
    bmi_write(1, 0x7d, 0x00); /* accelerometer disabled */
    bmi_write(8, 0x11, 0x80); /* gyro suspend mode */
    result[1] = POWER_OFF_MARKER;
    while ((GPIOA_IDR & (1u << 14)) == 0) {
        __asm__ volatile ("nop" ::: "memory");
    }
    result[2] = bmi_read(1, 0x24, 1) |
                ((uint32_t)bmi_read(1, 0x25, 1) << 8);
    result[3] = bmi_read(8, 0x0e, 0);

    /* Match the real BMI088 power-up sequence before accepting data. */
    bmi_write(1, 0x7c, 0x00); /* accelerometer normal power mode */
    bmi_write(1, 0x7d, 0x04); /* accelerometer enabled */
    bmi_write(8, 0x11, 0x00); /* gyro normal mode */
    result[1] = CONFIG_MARKER; /* host may now inject samples */
    while ((GPIOA_IDR & (1u << 14)) != 0) {
        __asm__ volatile ("nop" ::: "memory");
    }

    /* A14 is a normal modelled external GPIO.  Holding it low lets the host
     * deliver deterministic co-sim samples without relying on host speed;
     * releasing it begins the FIFO read transaction. */
    while ((GPIOA_IDR & (1u << 14)) == 0) {
        __asm__ volatile ("nop" ::: "memory");
    }

    result[2] = bmi_read(1, 0x24, 1) |
                ((uint32_t)bmi_read(1, 0x25, 1) << 8);
    result[3] = bmi_read(8, 0x0e, 0);

    /* The configuration write above inserts the documented two-byte config
     * frame before the first data frame. */
    bmi_read_fifo(1, 0x26, 1, config_frame, sizeof(config_frame));
    result[4] = config_frame[0];
    result[5] = config_frame[1];
    /* An incomplete accel data frame must repeat on the following FIFO read. */
    result[6] = bmi_read(1, 0x26, 1);
    bmi_read_fifo(1, 0x26, 1, accel_fifo, sizeof(accel_fifo));
    bmi_read_fifo(8, 0x3f, 0, gyro_fifo, sizeof(gyro_fifo));
    for (unsigned i = 0; i < sizeof(accel_fifo); ++i) {
        result[7 + i] = accel_fifo[i];
    }
    for (unsigned i = 0; i < sizeof(gyro_fifo); ++i) {
        result[32 + i] = gyro_fifo[i];
    }
    result[56] = bmi_read(1, 0x24, 1) | ((uint32_t)bmi_read(1, 0x25, 1) << 8);
    result[57] = bmi_read(8, 0x0e, 0);
    result[58] = bmi_read(1, 0x26, 1); /* deterministic FIFO overread */
    result[59] = 0x444f4e45u; /* "DONE" */

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
