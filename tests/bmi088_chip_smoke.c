#include "dm_mc02_bmi088.h"

#include <float.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>

static void check(bool condition, const char *expression, unsigned line)
{
    if (!condition) {
        fprintf(stderr, "bmi088 chip smoke: line %u: %s\n", line,
                expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

static int16_t read_accel_temperature(DmMc02Bmi088 *bmi)
{
    DmMc02Bmi088ReadEvent event;
    uint16_t encoded;

    encoded = (uint16_t)dm_mc02_bmi088_read_reg(bmi, 0x22, 0, &event) << 3;
    encoded |= dm_mc02_bmi088_read_reg(bmi, 0x23, 0, &event) >> 5;
    return encoded & 0x400 ? (int16_t)(encoded | 0xf800) : (int16_t)encoded;
}

static void test_power_and_temperature(void)
{
    DmMc02Bmi088 accel;
    DmMc02Bmi088 gyro;
    DmMc02Bmi088ReadEvent event;
    float accel_input[3] = { 0.25f, 0.0f, 1.0f };
    float gyro_input[3] = { 10.0f, 0.0f, 0.0f };
    uint8_t old_raw;

    dm_mc02_bmi088_init(&accel, true);
    dm_mc02_bmi088_init(&gyro, false);
    CHECK(!dm_mc02_bmi088_is_powered(&accel));
    CHECK(dm_mc02_bmi088_is_powered(&gyro));
    CHECK(!dm_mc02_bmi088_sample(&accel, accel_input, 1, 0, 0));

    dm_mc02_bmi088_write_reg(&accel, 0x7c, 0x00);
    dm_mc02_bmi088_write_reg(&accel, 0x7d, 0x04);
    CHECK(dm_mc02_bmi088_is_powered(&accel));
    CHECK(dm_mc02_bmi088_sample(&accel, accel_input, 2, 0, 0));
    old_raw = dm_mc02_bmi088_read_reg(&accel, 0x12, 0, &event);
    dm_mc02_bmi088_write_reg(&accel, 0x7d, 0x00);
    CHECK(!dm_mc02_bmi088_is_powered(&accel));
    CHECK(!dm_mc02_bmi088_sample(&accel, accel_input, 3, 1000, 1000));
    CHECK(dm_mc02_bmi088_read_reg(&accel, 0x12, 0, &event) == old_raw);

    dm_mc02_bmi088_write_reg(&gyro, 0x11, 0x80);
    CHECK(!dm_mc02_bmi088_is_powered(&gyro));
    CHECK(!dm_mc02_bmi088_sample(&gyro, gyro_input, 1, 0, 0));
    dm_mc02_bmi088_write_reg(&gyro, 0x11, 0x00);
    CHECK(dm_mc02_bmi088_sample(&gyro, gyro_input, 2, 0, 0));

    dm_mc02_bmi088_set_temperature(&accel, DBL_MAX);
    CHECK(read_accel_temperature(&accel) == 1023);
    dm_mc02_bmi088_set_temperature(&accel, -DBL_MAX);
    CHECK(read_accel_temperature(&accel) == -1024);
    dm_mc02_bmi088_set_temperature(&accel, 25.0);
    dm_mc02_bmi088_set_temperature(&accel, NAN);
    CHECK(read_accel_temperature(&accel) == 16);
}

static void test_fifo_event_and_bounds(void)
{
    DmMc02Bmi088 accel;
    DmMc02Bmi088ReadEvent event;
    float input[3] = { 1.0f, 2.0f, 3.0f };
    uint8_t value;

    dm_mc02_bmi088_init(&accel, true);
    dm_mc02_bmi088_write_reg(&accel, 0x7c, 0x00);
    dm_mc02_bmi088_write_reg(&accel, 0x7d, 0x04);
    dm_mc02_bmi088_write_reg(&accel, 0x49, 0x50);
    dm_mc02_bmi088_write_reg(&accel, 0x40, 0x8c);
    CHECK(dm_mc02_bmi088_sample(&accel, input, 42, 1000000, 1000000));

    CHECK(dm_mc02_bmi088_read_reg(&accel, 0x100, 0, &event) == 0);
    CHECK(!event.fifo_frame_complete);
    value = dm_mc02_bmi088_read_reg(&accel, 0x26, 0, &event);
    CHECK(value == 0x48 && !event.fifo_frame_complete);
    dm_mc02_bmi088_end_read(&accel);
    value = dm_mc02_bmi088_read_reg(&accel, 0x26, 0, &event);
    CHECK(value == 0x48 && !event.fifo_frame_complete);
    dm_mc02_bmi088_end_read(&accel);

    /* Skip the config frame and read one complete data frame. */
    for (unsigned i = 0; i < 2; ++i) {
        (void)dm_mc02_bmi088_read_reg(&accel, 0x26, 0, &event);
    }
    for (unsigned i = 0; i < 7; ++i) {
        value = dm_mc02_bmi088_read_reg(&accel, 0x26, 0, &event);
        (void)value;
    }
    CHECK(event.fifo_frame_complete);
    CHECK(event.fifo_sample_sequence == 42);
}

int main(void)
{
    test_power_and_temperature();
    test_fifo_event_and_bounds();
    puts("bmi088 chip smoke: PASS");
    return 0;
}
