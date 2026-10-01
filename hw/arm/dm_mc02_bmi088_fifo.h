/* Small, side-effect-free FIFO capacity rules shared by the BMI088 model
 * and host-side regression tests. */
#ifndef HW_ARM_DM_MC02_BMI088_FIFO_H
#define HW_ARM_DM_MC02_BMI088_FIFO_H

#include <stdbool.h>
#include <stdint.h>

#define DM_MC02_BMI_ACCEL_FIFO_CAPACITY 1024u
#define DM_MC02_BMI_GYRO_FIFO_CAPACITY  (100u * 8u)

static inline uint16_t dm_mc02_bmi088_fifo_limit(bool accel,
                                                  bool stop_at_full)
{
    if (!accel && !stop_at_full) {
        return DM_MC02_BMI_GYRO_FIFO_CAPACITY - 8u;
    }
    return accel ? DM_MC02_BMI_ACCEL_FIFO_CAPACITY :
                   DM_MC02_BMI_GYRO_FIFO_CAPACITY;
}

/* Use an ordered comparison before subtraction.  The FIFO length can be
 * above a newly selected mode's limit during a mode transition. */
static inline bool dm_mc02_bmi088_fifo_needs_room(uint16_t length,
                                                   uint16_t limit,
                                                   uint8_t incoming)
{
    return length > limit || (uint16_t)(limit - length) < incoming;
}

#endif
