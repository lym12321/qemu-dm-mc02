#include <stdint.h>

#define GPIOC_MODER (*(volatile uint32_t *)0x58020800u)
#define GPIOC_BSRR (*(volatile uint32_t *)0x58020818u)
#define SPI_CR1    (*(volatile uint32_t *)0x40003800u)
#define SPI_SR     (*(volatile uint32_t *)0x40003814u)
#define SPI_TXDR   (*(volatile uint32_t *)0x40003820u)
#define SPI_RXDR   (*(volatile uint32_t *)0x40003830u)

#define RESULT_MARKER 0x54494d31u /* "TIM1" */

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

static uint16_t read_axis_x(uint32_t deselect, uint8_t reg, int accel)
{
    uint8_t lo;
    uint8_t hi;

    GPIOC_BSRR = deselect == 1 ? (1u << 16) : (1u << 19);
    (void)spi_byte(0x80u | reg);
    if (accel) {
        (void)spi_byte(0);
    }
    lo = spi_byte(0);
    hi = spi_byte(0);
    GPIOC_BSRR = deselect;
    return (uint16_t)lo | ((uint16_t)hi << 8);
}

static void write_byte(uint32_t deselect, uint8_t reg, uint8_t value)
{
    GPIOC_BSRR = deselect == 1 ? (1u << 16) : (1u << 19);
    (void)spi_byte(reg & 0x7fu);
    (void)spi_byte(value);
    GPIOC_BSRR = deselect;
}

void Reset_Handler(void)
{
    volatile uint32_t *result = (volatile uint32_t *)0x20000000u;

    GPIOC_MODER = (GPIOC_MODER & ~((3u << 0) | (3u << 6))) |
                  (1u << 0) | (1u << 6);
    SPI_CR1 = 1;
    result[0] = RESULT_MARKER;
    write_byte(1, 0x7c, 0x00); /* accelerometer normal power mode */
    write_byte(1, 0x7d, 0x04); /* accelerometer enabled */
    write_byte(8, 0x11, 0x00); /* gyro normal mode */
    /* The host queues an IMU frame for virtual t=1 ms before this guest is
     * resumed. These reads must still observe the power-on sample. */
    result[1] = read_axis_x(8, 0x02, 0);
    result[2] = read_axis_x(1, 0x12, 1);

    /* At reset HSI is 64 MHz; this is deliberately far beyond 1 ms while
     * staying small enough for a bounded smoke. */
    for (volatile uint32_t wait = 0; wait < 1000000u; ++wait) {
        __asm__ volatile ("nop" ::: "memory");
    }
    result[3] = read_axis_x(8, 0x02, 0);
    result[4] = read_axis_x(1, 0x12, 1);
    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
