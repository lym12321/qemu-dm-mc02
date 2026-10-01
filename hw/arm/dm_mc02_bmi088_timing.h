/* BMI088 output-data-rate tables shared by the chip model and host tests. */
#ifndef HW_ARM_DM_MC02_BMI088_TIMING_H
#define HW_ARM_DM_MC02_BMI088_TIMING_H

#include <stdint.h>

/* Return the exact virtual-time period represented by an accelerometer
 * ACC_CONF ODR code. Zero denotes a disabled or unsupported code. */
static inline uint64_t dm_mc02_bmi088_accel_period_ns(uint8_t code)
{
    static const uint64_t periods[] = {
        0, 0, 0, 0, 0, 80000000ULL, 40000000ULL, 20000000ULL,
        10000000ULL, 5000000ULL, 2500000ULL, 1250000ULL, 625000ULL,
    };

    return code < sizeof(periods) / sizeof(periods[0]) ? periods[code] : 0;
}

/* Return the exact virtual-time period represented by a BMI088 gyro
 * BANDWIDTH code. The two duplicated rates intentionally retain different
 * filter settings in the register model while sharing one sample period. */
static inline uint64_t dm_mc02_bmi088_gyro_period_ns(uint8_t code)
{
    static const uint64_t periods[] = {
        500000ULL, 500000ULL, 1000000ULL, 2500000ULL,
        5000000ULL, 10000000ULL, 5000000ULL, 10000000ULL,
    };

    return code < sizeof(periods) / sizeof(periods[0]) ? periods[code] : 0;
}

#endif
