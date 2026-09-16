#include <stdint.h>

#define GPIOC_MODER (*(volatile uint32_t *)0x58020800u)
#define GPIOC_BSRR (*(volatile uint32_t *)0x58020818u)
#define GPIOA_ODR  (*(volatile uint32_t *)0x58020014u)
#define GPIOB_MODER (*(volatile uint32_t *)0x58020400u)
#define GPIOB_ODR  (*(volatile uint32_t *)0x58020414u)
#define GPIOC_ODR  (*(volatile uint32_t *)0x58020814u)
#define TIM12_CR1  (*(volatile uint32_t *)0x40001800u)
#define TIM12_CCR2 (*(volatile uint32_t *)0x40001838u)
#define SPI_CR1    (*(volatile uint32_t *)0x40003800u)
#define SPI_SR     (*(volatile uint32_t *)0x40003814u)
#define SPI_TXDR   (*(volatile uint32_t *)0x40003820u)
#define SPI_RXDR   (*(volatile uint32_t *)0x40003830u)

#define RESULT_MARKER 0x52424b31u /* "RBK1" */

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

static void bmi_read_bytes(uint32_t deselect, uint8_t reg, int accel,
                           uint8_t *out, unsigned count)
{
    unsigned i;

    GPIOC_BSRR = deselect == 1 ? (1u << 16) : (1u << 19);
    (void)spi_byte(0x80u | reg);
    if (accel) {
        (void)spi_byte(0);
    }
    for (i = 0; i < count; ++i) {
        out[i] = spi_byte(0);
    }
    GPIOC_BSRR = deselect;
}

static void bmi_write_byte(uint32_t deselect, uint8_t reg, uint8_t value)
{
    GPIOC_BSRR = deselect == 1 ? (1u << 16) : (1u << 19);
    (void)spi_byte(reg & 0x7fu);
    (void)spi_byte(value);
    GPIOC_BSRR = deselect;
}

void Reset_Handler(void)
{
    volatile uint32_t *result = (volatile uint32_t *)0x20000000u;
    uint8_t gyro_bytes[6];
    uint8_t accel_bytes[6];
    uint8_t sensor_time_bytes[3];
    uint8_t status;
    unsigned i;

    GPIOC_MODER = (GPIOC_MODER & ~((3u << 0) | (3u << 6))) |
                  (1u << 0) | (1u << 6);
    SPI_CR1 = 1;
    result[0] = RESULT_MARKER;
    /* Exercise range and ODR registers before consuming the host IMU sample.
     * The 100 Hz settings also make the host timestamp part of this test. */
    bmi_write_byte(1, 0x7c, 0x00); /* accelerometer normal power mode */
    bmi_write_byte(1, 0x7d, 0x04); /* accelerometer enabled */
    bmi_write_byte(8, 0x11, 0x00); /* gyro normal mode */
    bmi_write_byte(8, 0x0f, 0x01);
    bmi_write_byte(8, 0x10, 0x05);
    bmi_write_byte(1, 0x40, 0x88);
    bmi_write_byte(1, 0x41, 0x01);
    result[7] = 0x434f4e46u; /* "CONF": host may now publish a sample */
    for (volatile uint32_t wait = 0; wait < 100000000u; ++wait) {
        __asm__ volatile ("nop" ::: "memory");
    }
    /* A completed sample must assert each die's data-ready status. */
    bmi_read_bytes(8, 0x0a, 0, &status, 1);
    result[8] = status;
    bmi_read_bytes(1, 0x03, 1, &status, 1);
    result[9] = status;
    /* PC3 selects the gyro; read BMI088 gyro registers 0x02..0x07. */
    bmi_read_bytes(8, 0x02, 0, gyro_bytes, 6);
    /* PC0 selects the accelerometer; account for its SPI dummy byte. */
    bmi_read_bytes(1, 0x12, 1, accel_bytes, 6);

    for (i = 0; i < 3; ++i) {
        result[1 + i] = (uint32_t)gyro_bytes[i * 2] |
                        ((uint32_t)gyro_bytes[i * 2 + 1] << 8);
        result[4 + i] = (uint32_t)accel_bytes[i * 2] |
                        ((uint32_t)accel_bytes[i * 2 + 1] << 8);
    }
    bmi_read_bytes(8, 0x0a, 0, &status, 1);
    result[10] = status;
    bmi_read_bytes(1, 0x03, 1, &status, 1);
    result[11] = status;
    bmi_read_bytes(1, 0x18, 1, sensor_time_bytes, 3);
    result[12] = (uint32_t)sensor_time_bytes[0] |
                 ((uint32_t)sensor_time_bytes[1] << 8) |
                 ((uint32_t)sensor_time_bytes[2] << 16);

    /* Exercise the board-output telemetry projection after sensor bring-up. */
    GPIOA_ODR = 1u << 7;                  /* WS2812 data pin active */
    GPIOB_MODER = 1u << 30;               /* PB15 as a GPIO output */
    GPIOB_ODR = 1u << 15;                 /* direct buzzer fallback */
    GPIOC_ODR = (1u << 13) | (1u << 15); /* 24 V and 5 V enabled */
    TIM12_CCR2 = 1;
    TIM12_CR1 = 1;

    for (;;) {
        __asm__ volatile ("wfi" ::: "memory");
    }
}
