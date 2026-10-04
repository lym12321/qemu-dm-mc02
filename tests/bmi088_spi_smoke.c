#include "dm_mc02_bmi088_spi.h"

#include <stdio.h>
#include <stdlib.h>

typedef struct Consumption {
    unsigned calls;
    uint64_t sequence;
} Consumption;

typedef struct SourceWatch {
    DmMc02Bmi088 *bmi;
    Consumption *consumed;
    unsigned notices;
    unsigned consumption_at_notice;
    bool retained;
    bool premature_loss;
} SourceWatch;

static void on_source_changed(void *opaque)
{
    SourceWatch *watch = opaque;

    watch->notices++;
    watch->consumption_at_notice = watch->consumed->calls;
    watch->retained = dm_mc02_bmi088_sample_retained(watch->bmi, 201);
    watch->premature_loss |= !watch->retained && !watch->consumed->calls;
}

static void on_consumed(void *opaque, uint64_t sequence)
{
    Consumption *consumed = opaque;

    consumed->calls++;
    consumed->sequence = sequence;
}

static void check(bool condition, const char *expression, unsigned line)
{
    if (!condition) {
        fprintf(stderr, "bmi088 spi smoke: line %u: %s\n", line, expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

static uint8_t transfer(const DmMc02SpiTarget *target, uint8_t value)
{
    return target->transfer(target->opaque, value, 0);
}

static void start_read(const DmMc02SpiTarget *target, uint8_t reg, bool accel)
{
    target->select(target->opaque, false, 0);
    target->select(target->opaque, true, 1);
    CHECK(transfer(target, reg | 0x80) == 0);
    if (accel) {
        CHECK(transfer(target, 0) == 0);
    }
}

static void read_bytes(const DmMc02SpiTarget *target, unsigned length)
{
    for (unsigned i = 0; i < length; ++i) {
        (void)transfer(target, 0);
    }
}

int main(void)
{
    DmMc02Bmi088 accel;
    DmMc02Bmi088 gyro;
    DmMc02Bmi088Spi accel_adapter;
    DmMc02Bmi088Spi gyro_adapter;
    DmMc02SpiTarget accel_target;
    DmMc02SpiTarget gyro_target;
    Consumption accel_consumed = { 0 };
    Consumption gyro_consumed = { 0 };
    SourceWatch watch = { .bmi = &accel, .consumed = &accel_consumed };
    const float input[3] = { 1000.0f, 500.0f, -1000.0f };
    const float changed[3] = { -500.0f, -1000.0f, 500.0f };
    const float accel_input[3] = { 0.25f, -0.5f, 1.0f };

    dm_mc02_bmi088_init(&accel, true);
    dm_mc02_bmi088_init(&gyro, false);
    dm_mc02_bmi088_spi_init(&accel_adapter, &accel);
    dm_mc02_bmi088_spi_init(&gyro_adapter, &gyro);
    dm_mc02_bmi088_spi_set_consume_callback(&accel_adapter, on_consumed,
                                            &accel_consumed);
    dm_mc02_bmi088_spi_set_consume_callback(&gyro_adapter, on_consumed,
                                            &gyro_consumed);
    accel_target = dm_mc02_bmi088_spi_target(&accel_adapter);
    gyro_target = dm_mc02_bmi088_spi_target(&gyro_adapter);

    /* Bosch BMI088 gyro needs no dummy byte; accel requires one. */
    start_read(&gyro_target, 0, false);
    CHECK(transfer(&gyro_target, 0) == 0x0f);
    start_read(&accel_target, 0, true);
    CHECK(transfer(&accel_target, 0) == 0x1e);
    start_read(&gyro_target, 0x0f, false);
    CHECK(transfer(&gyro_target, 0) == 0);
    CHECK(transfer(&gyro_target, 0) == 0);

    /* Default bytes have no accepted sample identity. */
    start_read(&gyro_target, 0x02, false);
    read_bytes(&gyro_target, 6);
    start_read(&accel_target, 0x12, true);
    read_bytes(&accel_target, 6);
    CHECK(gyro_consumed.calls == 0 && accel_consumed.calls == 0);

    /* Rejected power/ODR inputs must not replace the accepted identity. */
    dm_mc02_bmi088_write_reg(&gyro, 0x10, 2); /* Bosch 1000 Hz ODR. */
    CHECK(dm_mc02_bmi088_sample(&gyro, input, 101, 0, 0));
    CHECK(!dm_mc02_bmi088_sample(&gyro, changed, 102, 999999, 999999));
    dm_mc02_bmi088_write_reg(&gyro, 0x11, 0x80);
    CHECK(!dm_mc02_bmi088_sample(&gyro, changed, 103, 1000000, 1000000));
    start_read(&gyro_target, 0x02, false);
    read_bytes(&gyro_target, 6);
    CHECK(gyro_consumed.calls == 1 && gyro_consumed.sequence == 101);
    dm_mc02_bmi088_write_reg(&gyro, 0x11, 0);

    /* Ending or resetting a partial burst cannot consume a whole sample. */
    start_read(&gyro_target, 0x02, false);
    read_bytes(&gyro_target, 3);
    start_read(&gyro_target, 0x05, false);
    read_bytes(&gyro_target, 3);
    CHECK(gyro_consumed.calls == 1);
    start_read(&gyro_target, 0x02, false);
    read_bytes(&gyro_target, 3);
    dm_mc02_bmi088_spi_reset(&gyro_adapter);
    CHECK(transfer(&gyro_target, 0x80) == 0);
    CHECK(transfer(&gyro_target, 0) == 0x0f);
    CHECK(gyro_consumed.calls == 1);

    /* An update halfway through remains mixed bytes, without a fake snapshot. */
    dm_mc02_bmi088_reset(&gyro);
    CHECK(dm_mc02_bmi088_sample(&gyro, input, 111, 0, 0));
    start_read(&gyro_target, 0x02, false);
    /* Bosch GYRO_RANGE reset: 2000 dps / 32768 LSB, little endian. */
    CHECK(transfer(&gyro_target, 0) == 0x00);
    CHECK(transfer(&gyro_target, 0) == 0x40);
    CHECK(dm_mc02_bmi088_sample(&gyro, changed, 112, 1000000, 1000000));
    CHECK(!dm_mc02_bmi088_sample_retained(&gyro, 111));
    CHECK(transfer(&gyro_target, 0) == 0x00);
    CHECK(transfer(&gyro_target, 0) == 0xc0);
    CHECK(transfer(&gyro_target, 0) == 0x00);
    CHECK(transfer(&gyro_target, 0) == 0x20);
    CHECK(gyro_consumed.calls == 1);
    start_read(&gyro_target, 0x02, false);
    read_bytes(&gyro_target, 6);
    CHECK(gyro_consumed.calls == 2 && gyro_consumed.sequence == 112);

    /* FIFO callbacks identify old queued samples, not the current raw sample. */
    dm_mc02_bmi088_write_reg(&accel, 0x7c, 0);
    dm_mc02_bmi088_write_reg(&accel, 0x7d, 4);
    dm_mc02_bmi088_write_reg(&accel, 0x49, 0x50);
    dm_mc02_bmi088_write_reg(&accel, 0x40, 0x8c);
    CHECK(dm_mc02_bmi088_sample(&accel, accel_input, 201, 1000000, 1000000));
    CHECK(dm_mc02_bmi088_sample(&accel, accel_input, 202, 2000000, 2000000));
    start_read(&accel_target, 0x26, true);
    CHECK(transfer(&accel_target, 0) == 0x48);
    read_bytes(&accel_target, 1);
    CHECK(accel_consumed.calls == 0); /* Config frame. */
    dm_mc02_bmi088_set_source_callback(&accel, on_source_changed, &watch);
    CHECK(transfer(&accel_target, 0) == 0x84);
    read_bytes(&accel_target, 6);
    CHECK(accel_consumed.calls == 1 && accel_consumed.sequence == 201);
    CHECK(watch.notices != 0 && watch.consumption_at_notice == 1);
    CHECK(!watch.retained); /* Consumption must precede the source-loss notice. */
    CHECK(!watch.premature_loss);
    CHECK(transfer(&accel_target, 0) == 0x84);
    read_bytes(&accel_target, 6);
    CHECK(accel_consumed.calls == 2 && accel_consumed.sequence == 202);
    CHECK(transfer(&accel_target, 0) == 0x44);
    read_bytes(&accel_target, 3);
    CHECK(accel_consumed.calls == 2); /* Sensor-time frame. */
    start_read(&accel_target, 0x12, true);
    read_bytes(&accel_target, 6);
    CHECK(accel_consumed.calls == 3 && accel_consumed.sequence == 202);

    /* Stream overflow may replace a partially read head; never tag its mixture. */
    dm_mc02_bmi088_reset(&gyro);
    dm_mc02_bmi088_write_reg(&gyro, 0x3e, 0x80);
    for (unsigned i = 0; i < 99; ++i) {
        CHECK(dm_mc02_bmi088_sample(&gyro, input, 401 + i,
                                     i * 1000000ULL, i * 1000000ULL));
    }
    start_read(&gyro_target, 0x3f, false);
    read_bytes(&gyro_target, 3);
    CHECK(dm_mc02_bmi088_sample(&gyro, input, 500, 99000000, 99000000));
    CHECK(!dm_mc02_bmi088_sample_retained(&gyro, 401));
    CHECK(!dm_mc02_bmi088_sample_retained(&gyro, 402));
    read_bytes(&gyro_target, 5);
    CHECK(gyro_consumed.calls == 2);
    read_bytes(&gyro_target, 8);
    CHECK(gyro_consumed.calls == 3 && gyro_consumed.sequence == 403);

    puts("RESULT: BMI088 SPI adapter smoke passed");
    return 0;
}
