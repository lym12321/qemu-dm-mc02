/* Board-independent SPI framing adapter for one BMI088 die. */
#ifndef HW_ARM_DM_MC02_BMI088_SPI_H
#define HW_ARM_DM_MC02_BMI088_SPI_H

#include "hw/arm/dm_mc02_bmi088.h"
#include "hw/arm/dm_mc02_spi_target.h"

#include <stdint.h>

typedef void (*DmMc02Bmi088SpiConsumeFn)(void *opaque);

typedef struct DmMc02Bmi088Spi {
    DmMc02Bmi088 *bmi;
    bool command_seen;
    bool read_transfer;
    bool dummy_pending;
    uint8_t reg;
    uint8_t read_start_reg;
    DmMc02Bmi088SpiConsumeFn consume;
    void *consume_opaque;
} DmMc02Bmi088Spi;

void dm_mc02_bmi088_spi_init(DmMc02Bmi088Spi *adapter,
                             DmMc02Bmi088 *bmi);
void dm_mc02_bmi088_spi_reset(DmMc02Bmi088Spi *adapter);
void dm_mc02_bmi088_spi_set_consume_callback(
    DmMc02Bmi088Spi *adapter, DmMc02Bmi088SpiConsumeFn consume,
    void *opaque);

/* Component-only transaction state contract.  The BMI088 pointer and
 * consume callback are destination-owned runtime wiring. */
struct VMStateDescription;
const struct VMStateDescription *dm_mc02_bmi088_spi_vmstate(void);
extern const struct VMStateDescription vmstate_dm_mc02_bmi088_spi;

DmMc02SpiTarget dm_mc02_bmi088_spi_target(DmMc02Bmi088Spi *adapter);

#endif
