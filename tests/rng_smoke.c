#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_rng.h"

#include <stdio.h>
#include <stdlib.h>

/* The host smoke exercises the register contract without constructing the
 * QEMU object graph.  Capture the level-sensitive IRQ output without pulling
 * the QEMU IRQ graph into this focused chip-layer test. */
static int observed_irq_level;

void qemu_set_irq(qemu_irq irq, int level)
{
    (void)irq;
    observed_irq_level = level;
}

static void check(bool condition, const char *expression, unsigned line)
{
    if (!condition) {
        fprintf(stderr, "rng smoke: line %u: %s\n", line, expression);
        exit(EXIT_FAILURE);
    }
}

#define CHECK(expression) check((expression), #expression, __LINE__)

static uint32_t expected_next(uint32_t *state)
{
    uint32_t x = *state;

    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    *state = x;
    return x;
}

static void test_reset_enable_disable(void)
{
    DmMc02Rng rng = { 0 };

    dm_mc02_rng_reset(&rng);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_CR, 4) == 0);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) == 0);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_DR, 4) == 0);

    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR, DM_MC02_RNG_CR_RNGEN, 4);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
          DM_MC02_RNG_SR_DRDY);
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR, 0, 4);
    CHECK(!(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
            DM_MC02_RNG_SR_DRDY));
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_DR, 4) == 0);
}

static void test_deterministic_four_word_buffer(void)
{
    DmMc02Rng rng = { 0 };
    uint32_t expected = UINT32_C(0x6d2b79f5);
    uint32_t words[4];

    dm_mc02_rng_reset(&rng);
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR, DM_MC02_RNG_CR_RNGEN, 4);
    for (unsigned i = 0; i < ARRAY_SIZE(words); ++i) {
        words[i] = dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_DR, 4);
        CHECK(words[i] == expected_next(&expected));
        if (i != ARRAY_SIZE(words) - 1) {
            CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
                  DM_MC02_RNG_SR_DRDY);
        }
    }
    CHECK(!(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
            DM_MC02_RNG_SR_DRDY));
    CHECK(words[0] != words[1] && words[1] != words[2]);
}

static void test_buffer_refills_after_empty_poll(void)
{
    DmMc02Rng rng = { 0 };
    uint32_t expected = UINT32_C(0x6d2b79f5);

    dm_mc02_rng_reset(&rng);
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR, DM_MC02_RNG_CR_RNGEN, 4);
    for (unsigned i = 0; i < DM_MC02_RNG_FIFO_DEPTH; ++i) {
        CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_DR, 4) ==
              expected_next(&expected));
    }
    CHECK(!(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
            DM_MC02_RNG_SR_DRDY));
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
          DM_MC02_RNG_SR_DRDY);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_DR, 4) ==
          expected_next(&expected));
}

static void test_interrupt_rearm_refills_empty_buffer(void)
{
    DmMc02Rng rng = { 0 };
    uint32_t expected = UINT32_C(0x6d2b79f5);

    dm_mc02_rng_reset(&rng);
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR, DM_MC02_RNG_CR_RNGEN, 4);
    for (unsigned i = 0; i < DM_MC02_RNG_FIFO_DEPTH; ++i) {
        CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_DR, 4) ==
              expected_next(&expected));
    }
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR,
                          DM_MC02_RNG_CR_RNGEN | DM_MC02_RNG_CR_IE, 4);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
          DM_MC02_RNG_SR_DRDY);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_DR, 4) ==
          expected_next(&expected));
}

static void test_irq_level_tracks_data_and_rearm(void)
{
    DmMc02Rng rng = { 0 };

    observed_irq_level = -1;
    dm_mc02_rng_reset(&rng);
    dm_mc02_rng_set_irq(&rng, (qemu_irq)(uintptr_t)1);
    CHECK(observed_irq_level == 0);

    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR,
                          DM_MC02_RNG_CR_RNGEN | DM_MC02_RNG_CR_IE, 4);
    CHECK(observed_irq_level == 1);
    for (unsigned i = 0; i < DM_MC02_RNG_FIFO_DEPTH; ++i) {
        (void)dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_DR, 4);
    }
    CHECK(observed_irq_level == 0);
    CHECK(!(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
            DM_MC02_RNG_SR_DRDY));
    CHECK(observed_irq_level == 0);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
          DM_MC02_RNG_SR_DRDY);
    CHECK(observed_irq_level == 1);
}

static void test_errors_block_new_generation(void)
{
    DmMc02Rng rng = { 0 };
    const uint32_t errors = DM_MC02_RNG_SR_CECS |
                             DM_MC02_RNG_SR_SECS |
                             DM_MC02_RNG_SR_CEIS |
                             DM_MC02_RNG_SR_SEIS;

    dm_mc02_rng_reset(&rng);
    rng.status = errors;
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR,
                          DM_MC02_RNG_CR_RNGEN | DM_MC02_RNG_CR_IE, 4);
    CHECK((dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) & errors) == errors);
    CHECK(!(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
            DM_MC02_RNG_SR_DRDY));
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_DR, 4) == 0);

    /* Clearing only the interrupt latches does not clear current errors. */
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_SR,
                          UINT32_MAX & ~(DM_MC02_RNG_SR_CEIS |
                                          DM_MC02_RNG_SR_SEIS), 4);
    CHECK((dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
           (DM_MC02_RNG_SR_CECS | DM_MC02_RNG_SR_SECS)) ==
          (DM_MC02_RNG_SR_CECS | DM_MC02_RNG_SR_SECS));
    CHECK(rng.fifo_count == 0);
}

static void test_seed_error_latch_auto_recovery(void)
{
    DmMc02Rng rng = { 0 };
    unsigned before;

    dm_mc02_rng_reset(&rng);
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR, DM_MC02_RNG_CR_RNGEN, 4);
    (void)dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_DR, 4);
    before = rng.fifo_count;

    /* SEIS can be left latched after the current seed error is gone. */
    rng.status = DM_MC02_RNG_SR_SEIS;
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
          DM_MC02_RNG_SR_SEIS);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
          DM_MC02_RNG_SR_DRDY);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_DR, 4) != 0);
    CHECK(rng.fifo_count == before - 1);

    /* This is the clear path used by RNG_RecoverSeedError(). */
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_SR,
                          UINT32_MAX & ~DM_MC02_RNG_SR_SEIS, 4);
    CHECK(!(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
            DM_MC02_RNG_SR_SEIS));
}

static void test_conditioning_reset_is_observable_and_recovers(void)
{
    DmMc02Rng rng = { 0 };
    const uint32_t errors = DM_MC02_RNG_SR_CECS |
                             DM_MC02_RNG_SR_SECS |
                             DM_MC02_RNG_SR_CEIS |
                             DM_MC02_RNG_SR_SEIS;
    uint32_t cr;

    dm_mc02_rng_reset(&rng);
    rng.status = errors;
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR,
                          DM_MC02_RNG_CR_RNGEN | DM_MC02_RNG_CR_IE |
                          DM_MC02_RNG_CR_CONDRST, 4);
    cr = dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_CR, 4);
    CHECK((cr & DM_MC02_RNG_CR_CONDRST) != 0);
    CHECK((dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
           (DM_MC02_RNG_SR_DRDY | errors)) == 0);
    CHECK(rng.fifo_count == 0);

    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR,
                          DM_MC02_RNG_CR_RNGEN | DM_MC02_RNG_CR_IE, 4);
    cr = dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_CR, 4);
    CHECK((cr & DM_MC02_RNG_CR_CONDRST) == 0);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
          DM_MC02_RNG_SR_DRDY);
    CHECK(rng.fifo_count == DM_MC02_RNG_FIFO_DEPTH);
}

static void test_config_fields_and_lock(void)
{
    DmMc02Rng rng = { 0 };
    const uint32_t config_a = DM_MC02_RNG_CR_CED |
                               DM_MC02_RNG_CR_CONFIG3 |
                               DM_MC02_RNG_CR_NISTC |
                               (UINT32_C(5) << 13) |
                               (UINT32_C(9) << 16) |
                               (UINT32_C(0x2a) << 20);
    const uint32_t config_b = DM_MC02_RNG_CR_CED |
                               (UINT32_C(3) << 8) |
                               (UINT32_C(2) << 13) |
                               (UINT32_C(1) << 16) |
                               (UINT32_C(0x15) << 20);
    uint32_t cr;

    dm_mc02_rng_reset(&rng);
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR, config_a, 4);
    cr = dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_CR, 4);
    CHECK((cr & (DM_MC02_RNG_CR_CONFIG_MASK | DM_MC02_RNG_CR_CONFIGLOCK)) ==
          config_a);

    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR,
                          config_a | DM_MC02_RNG_CR_CONFIGLOCK, 4);
    cr = dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_CR, 4);
    CHECK((cr & (DM_MC02_RNG_CR_CONFIG_MASK |
                 DM_MC02_RNG_CR_CONFIGLOCK)) ==
          (config_a | DM_MC02_RNG_CR_CONFIGLOCK));

    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR, config_b, 4);
    cr = dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_CR, 4);
    CHECK((cr & (DM_MC02_RNG_CR_CONFIG_MASK |
                 DM_MC02_RNG_CR_CONFIGLOCK)) ==
          (config_a | DM_MC02_RNG_CR_CONFIGLOCK));

    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_HTCR, UINT32_C(0x00007274), 4);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_HTCR, 4) == 0);
    dm_mc02_rng_reset(&rng);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_CR, 4) == 0);
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR, config_b, 4);
    CHECK((dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_CR, 4) &
           DM_MC02_RNG_CR_CONFIG_MASK) == config_b);
}

static void test_error_clear_and_config_lock(void)
{
    DmMc02Rng rng = { 0 };
    uint32_t cr;

    dm_mc02_rng_reset(&rng);
    rng.status = DM_MC02_RNG_SR_CEIS | DM_MC02_RNG_SR_SEIS;
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR, DM_MC02_RNG_CR_IE, 4);
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
          (DM_MC02_RNG_SR_CEIS | DM_MC02_RNG_SR_SEIS));
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_SR,
                          UINT32_MAX & ~DM_MC02_RNG_SR_CEIS, 4);
    CHECK(!(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
            DM_MC02_RNG_SR_CEIS));
    CHECK(dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_SR, 4) &
          DM_MC02_RNG_SR_SEIS);

    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR,
                          DM_MC02_RNG_CR_CED | DM_MC02_RNG_CR_CONFIGLOCK, 4);
    cr = dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_CR, 4);
    CHECK((cr & (DM_MC02_RNG_CR_CED | DM_MC02_RNG_CR_CONFIGLOCK)) ==
          (DM_MC02_RNG_CR_CED | DM_MC02_RNG_CR_CONFIGLOCK));
    dm_mc02_rng_write_reg(&rng, DM_MC02_RNG_CR, 0, 4);
    cr = dm_mc02_rng_read_reg(&rng, DM_MC02_RNG_CR, 4);
    CHECK(cr & DM_MC02_RNG_CR_CED);
    CHECK(!(cr & DM_MC02_RNG_CR_IE));
}

int main(void)
{
    test_reset_enable_disable();
    test_deterministic_four_word_buffer();
    test_buffer_refills_after_empty_poll();
    test_interrupt_rearm_refills_empty_buffer();
    test_irq_level_tracks_data_and_rearm();
    test_errors_block_new_generation();
    test_seed_error_latch_auto_recovery();
    test_conditioning_reset_is_observable_and_recovers();
    test_config_fields_and_lock();
    test_error_clear_and_config_lock();
    puts("rng smoke: PASS");
    return 0;
}
