/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_tim2.h"

void dm_test_clock(int64_t target, bool late);
void dm_test_clock_reset(void);

typedef struct Edge { uint64_t ns; bool driven, level; } Edge;
static Edge edges[100];
static unsigned edge_count;

static void output(void *opaque, uint64_t ns, bool driven, bool level)
{
    g_assert_cmpuint(edge_count, <, G_N_ELEMENTS(edges));
    edges[edge_count++] = (Edge) { ns, driven, level };
}

static void wr(DmMc02Tim2 *s, unsigned reg, uint32_t value)
{
    s->iomem.ops->write(s, reg, value, 4);
}

static void test_pwm_timeline(gconstpointer data)
{
    DmMc02Tim2 s;
    bool late = GPOINTER_TO_INT(data);

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_output(&s, 1, false, output, NULL);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x20, 1);
    wr(&s, 0x2c, 29); /* 24MHz/30 = 800kHz */
    wr(&s, 0x34, 10);
    edge_count = 0;
    wr(&s, 0, 1);
    dm_test_clock(10000, late);
    g_assert_cmpuint(edge_count, ==, 17);
    for (unsigned i = 0; i < edge_count; ++i) {
        g_assert_true(edges[i].driven);
        g_assert_cmpint(edges[i].level, ==, !(i & 1));
        g_assert_cmpuint(edges[i].ns, ==, (i / 2) * 1250 + (i & 1 ? 417 : 0));
    }
    wr(&s, 0x20, 3); /* inverted output, immediate same-time edge */
    g_assert_false(edges[edge_count - 1].level);
    wr(&s, 0, 0);
    g_assert_false(edges[edge_count - 1].driven);
    dm_mc02_tim2_cleanup(&s);
}

static void test_complementary_deadtime(void)
{
    DmMc02Tim2 s;

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_complementary_supported(&s, true);
    dm_mc02_tim2_set_break_supported(&s, true);
    dm_mc02_tim2_set_output(&s, 1, true, output, NULL);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x20, 4);
    wr(&s, 0x44, (1u << 15) | 2); /* 2 cycles dead time */
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    edge_count = 0;
    wr(&s, 0, 1);
    dm_test_clock(1250, true);
    g_assert_cmpuint(edge_count, ==, 3);
    g_assert_false(edges[0].level);
    g_assert_cmpuint(edges[1].ns, ==, 500);
    g_assert_true(edges[1].level);
    g_assert_cmpuint(edges[2].ns, ==, 1250);
    g_assert_false(edges[2].level);
    dm_mc02_tim2_cleanup(&s);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_data_func("/timer/output/pwm", GINT_TO_POINTER(false), test_pwm_timeline);
    g_test_add_data_func("/timer/output/late", GINT_TO_POINTER(true), test_pwm_timeline);
    g_test_add_func("/timer/output/complementary", test_complementary_deadtime);
    return g_test_run();
}
