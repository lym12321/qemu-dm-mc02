/* Test-only SPI helpers moved unchanged from the existing BMI088 readback
 * consumer. The including fixture supplies the same register definitions. */
#ifndef DM_MC02_BMI088_SPI_FIXTURE_H
#define DM_MC02_BMI088_SPI_FIXTURE_H

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

#endif
