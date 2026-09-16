#include "dm_mc02_bmi088_timing.h"

#include <stdint.h>
#include <stdio.h>

static int expect(uint8_t code, uint64_t got, uint64_t wanted)
{
    if (got != wanted) {
        fprintf(stderr, "code %u: got %llu ns, expected %llu ns\n", code,
                (unsigned long long)got, (unsigned long long)wanted);
        return 1;
    }
    return 0;
}

int main(void)
{
    static const uint64_t accel[] = {
        0, 0, 0, 0, 0, 80000000ULL, 40000000ULL, 20000000ULL,
        10000000ULL, 5000000ULL, 2500000ULL, 1250000ULL, 625000ULL,
    };
    static const uint64_t gyro[] = {
        500000ULL, 500000ULL, 1000000ULL, 2500000ULL,
        5000000ULL, 10000000ULL, 5000000ULL, 10000000ULL,
    };

    for (uint8_t code = 0; code < sizeof(accel) / sizeof(accel[0]); ++code) {
        if (expect(code, dm_mc02_bmi088_accel_period_ns(code), accel[code])) {
            return 1;
        }
    }
    for (uint8_t code = 0; code < sizeof(gyro) / sizeof(gyro[0]); ++code) {
        if (expect(code, dm_mc02_bmi088_gyro_period_ns(code), gyro[code])) {
            return 1;
        }
    }
    if (expect(13, dm_mc02_bmi088_accel_period_ns(13), 0) ||
        expect(8, dm_mc02_bmi088_gyro_period_ns(8), 0)) {
        return 1;
    }
    puts("RESULT: BMI088 exact ODR timing smoke passed");
    return 0;
}
