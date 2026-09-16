#include "dm_mc02_bmi088_spi.h"

#include <stdio.h>

static unsigned consumed;

static void on_consumed(void *opaque)
{
    unsigned *count = opaque;

    (*count)++;
}

static int check(int condition, const char *description)
{
    if (!condition) {
        fprintf(stderr, "bmi088 spi smoke: %s\n", description);
        return 1;
    }
    return 0;
}

static uint8_t transfer(const DmMc02SpiTarget *target, uint8_t value)
{
    return target->transfer(target->opaque, value, 0);
}

int main(void)
{
    DmMc02Bmi088 accel;
    DmMc02Bmi088 gyro;
    DmMc02Bmi088Spi accel_adapter;
    DmMc02Bmi088Spi gyro_adapter;
    DmMc02SpiTarget accel_target;
    DmMc02SpiTarget gyro_target;
    float input[3] = { 0.1f, 0.2f, 1.0f };
    uint8_t value;

    dm_mc02_bmi088_init(&accel, true);
    dm_mc02_bmi088_init(&gyro, false);
    dm_mc02_bmi088_spi_init(&accel_adapter, &accel);
    dm_mc02_bmi088_spi_init(&gyro_adapter, &gyro);
    dm_mc02_bmi088_spi_set_consume_callback(&gyro_adapter, on_consumed,
                                             &consumed);
    accel_target = dm_mc02_bmi088_spi_target(&accel_adapter);
    gyro_target = dm_mc02_bmi088_spi_target(&gyro_adapter);

    /* Gyro reads return data on the first byte after the command. */
    if (check(transfer(&gyro_target, 0x80) == 0, "gyro command response") ||
        check(transfer(&gyro_target, 0x00) == 0x0f,
              "gyro chip id framing")) {
        return 1;
    }
    gyro_target.select(gyro_target.opaque, false, 0);
    gyro_target.select(gyro_target.opaque, true, 1);

    /* Accel reads insert the BMI088-required dummy byte. */
    if (check(transfer(&accel_target, 0x80) == 0,
              "accel command response") ||
        check(transfer(&accel_target, 0x00) == 0,
              "accel dummy response") ||
        check(transfer(&accel_target, 0x00) == 0x1e,
              "accel chip id framing")) {
        return 1;
    }
    accel_target.select(accel_target.opaque, false, 0);
    accel_target.select(accel_target.opaque, true, 1);

    /* The adapter exposes the chip's auto-increment register path. */
    if (check(transfer(&gyro_target, 0x8f) == 0,
              "gyro register command") ||
        check(transfer(&gyro_target, 0x00) == 0,
              "gyro range register") ||
        check(transfer(&gyro_target, 0x00) == 0,
              "gyro next register")) {
        return 1;
    }
    gyro_target.select(gyro_target.opaque, false, 0);

    /* A complete direct-data read emits exactly one device-consumption event. */
    dm_mc02_bmi088_sample(&gyro, input, 1, 0, 0);
    gyro_target.select(gyro_target.opaque, true, 1);
    transfer(&gyro_target, 0x82);
    for (unsigned i = 0; i < 6; ++i) {
        value = transfer(&gyro_target, 0);
        (void)value;
    }
    if (check(consumed == 1, "gyro consumption callback")) {
        return 1;
    }

    /* A CS transition discards a partial command before the next command. */
    gyro_target.select(gyro_target.opaque, false, 0);
    transfer(&gyro_target, 0x80);
    gyro_target.select(gyro_target.opaque, false, 0);
    gyro_target.select(gyro_target.opaque, true, 1);
    if (check(transfer(&gyro_target, 0x00) == 0,
              "CS transition resets command framing")) {
        return 1;
    }

    puts("RESULT: BMI088 SPI adapter smoke passed");
    return 0;
}
