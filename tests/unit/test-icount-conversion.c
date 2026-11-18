/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "sysemu/icount-conversion.h"

static void test_forward(void)
{
    static const struct {
        int64_t count;
        uint16_t scale;
        int64_t ns;
    } cases[] = {
        { 0, 20, 0 },
        { 1, 20, 20 },
        { 50000000, 20, 1000000000 },
        { -50000000, 20, -1000000000 },
        { INT64_MAX, 1, INT64_MAX },
        { INT64_MIN, 1, INT64_MIN },
        { 461168601842738790LL, 20, 9223372036854775800LL },
        { -461168601842738790LL, 20, -9223372036854775800LL },
        { 9007199254740991LL, 1024, 9223372036854774784LL },
        { -9007199254740992LL, 1024, INT64_MIN },
    };

    for (unsigned i = 0; i < G_N_ELEMENTS(cases); i++) {
        int64_t ns;

        g_assert_true(icount_ns_per_insn_to_ns(cases[i].count,
                                              cases[i].scale, &ns));
        g_assert_cmpint(ns, ==, cases[i].ns);
    }
}

static void test_overflow(void)
{
    int64_t ns;

    g_assert_false(icount_ns_per_insn_to_ns(461168601842738791LL, 20, &ns));
    g_assert_false(icount_ns_per_insn_to_ns(-461168601842738791LL, 20, &ns));
    g_assert_false(icount_ns_per_insn_to_ns(9007199254740992LL, 1024, &ns));
    g_assert_false(icount_ns_per_insn_to_ns(-9007199254740993LL, 1024, &ns));
}

static void test_deadlines(void)
{
    static const struct {
        int64_t ns;
        uint16_t scale;
        int64_t count;
    } cases[] = {
        { 0, 20, 0 },
        { 1, 20, 1 },
        { 19, 20, 1 },
        { 20, 20, 1 },
        { 21, 20, 2 },
        { -1, 20, 0 },
        { -19, 20, 0 },
        { -20, 20, -1 },
        { -21, 20, -1 },
        { INT64_MAX, 1, INT64_MAX },
        { INT64_MIN, 1, INT64_MIN },
        { INT64_MAX, 20, 461168601842738791LL },
        { INT64_MIN, 20, -461168601842738790LL },
        { INT64_MAX, 1024, 9007199254740992LL },
        { INT64_MIN, 1024, -9007199254740992LL },
    };

    for (unsigned i = 0; i < G_N_ELEMENTS(cases); i++) {
        g_assert_cmpint(icount_ns_per_insn_round(cases[i].ns, cases[i].scale),
                        ==, cases[i].count);
    }
}

static void test_deadline_never_early(void)
{
    /* Every sub-instruction deadline must select the first sufficient budget. */
    for (uint16_t scale = 1; scale <= ICOUNT_MAX_NS_PER_INSN; scale++) {
        for (int64_t ns = -scale; ns <= 3 * scale; ns++) {
            int64_t count = icount_ns_per_insn_round(ns, scale);
            int64_t reached, previous;

            g_assert_true(icount_ns_per_insn_to_ns(count, scale, &reached));
            g_assert_true(icount_ns_per_insn_to_ns(count - 1, scale, &previous));
            g_assert_cmpint(reached, >=, ns);
            g_assert_cmpint(previous, <, ns);
        }
    }
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/icount/conversion/forward", test_forward);
    g_test_add_func("/icount/conversion/overflow", test_overflow);
    g_test_add_func("/icount/conversion/deadlines", test_deadlines);
    g_test_add_func("/icount/conversion/never-early", test_deadline_never_early);
    return g_test_run();
}
