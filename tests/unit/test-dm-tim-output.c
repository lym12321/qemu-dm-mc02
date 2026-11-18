/* SPDX-License-Identifier: GPL-2.0-or-later */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_tim2.h"

void dm_test_clock(int64_t target, bool late);
void dm_test_clock_reset(void);
void dm_test_clock_set(int64_t target);
void dm_test_clock_read_step(int64_t step);
unsigned dm_test_clock_dispatches(void);

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
    /* Compare/update events already sample both PWM transition deadlines. */
    g_assert_false(timer_pending(s.output_timer));
    if (GPOINTER_TO_INT(data) == 2) {
        dm_test_clock_set(10000);
        dm_mc02_tim2_sync_output(&s, 10000);
    } else {
        dm_test_clock(10000, late);
    }
    g_assert_cmpuint(edge_count, ==, 17);
    for (unsigned i = 0; i < edge_count; ++i) {
        g_assert_true(edges[i].driven);
        g_assert_cmpint(edges[i].level, ==, !(i & 1));
        g_assert_cmpuint(edges[i].ns, ==, (i / 2) * 1250 + (i & 1 ? 417 : 0));
    }
    g_assert_false(timer_pending(s.output_timer));
    wr(&s, 0x20, 3); /* inverted output, immediate same-time edge */
    g_assert_false(edges[edge_count - 1].level);
    wr(&s, 0, 0);
    g_assert_false(edges[edge_count - 1].driven);
    dm_mc02_tim2_cleanup(&s);
}

static void test_mmio_has_one_timestamp(void)
{
    DmMc02Tim2 s;

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_output(&s, 1, false, output, NULL);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x20, 1);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 0);
    edge_count = 0;
    dm_test_clock_set(100000);
    /* Host time keeps moving inside one MMIO transaction. Its counter and
     * all event deadlines must still share the transaction's entry time. */
    dm_test_clock_read_step(1000);
    wr(&s, 0, 1);
    dm_test_clock_read_step(0);
    g_assert_cmpuint(s.start_ns, ==, 100000);
    g_assert_cmpuint(s.next_update_ns, ==, 101250);
    g_assert_cmpuint(s.next_compare_ns, ==, 101250);
    g_assert_cmpuint(edges[0].ns, ==, 100000);
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
    wr(&s, 0x20, 5); /* Both enables select complementary dead-time gates. */
    wr(&s, 0x44, (1u << 15) | 2); /* 2 cycles dead time */
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    edge_count = 0;
    wr(&s, 0, 1);
    dm_test_clock(417, true);
    /* The delayed N turn-on has no matching compare/update event. */
    g_assert_true(timer_pending(s.output_timer));
    g_assert_cmpuint(s.next_output_ns, ==, 500);
    dm_test_clock(1250, true);
    g_assert_cmpuint(edge_count, ==, 3);
    g_assert_false(edges[0].level);
    g_assert_cmpuint(edges[1].ns, ==, 500);
    g_assert_true(edges[1].level);
    g_assert_cmpuint(edges[2].ns, ==, 1250);
    g_assert_false(edges[2].level);
    dm_mc02_tim2_cleanup(&s);
}

static void test_n_only_output(void)
{
    DmMc02Tim2 s;

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_complementary_supported(&s, true);
    dm_mc02_tim2_set_break_supported(&s, true);
    dm_mc02_tim2_set_output(&s, 1, true, output, NULL);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x20, 4); /* RM0468 Table 351: N alone follows OCREF. */
    wr(&s, 0x44, (1u << 15) | 2); /* No dead time with only one output. */
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    edge_count = 0;
    wr(&s, 0, 1);
    /* DTG does not create extra deadlines for a lone N output. */
    g_assert_false(timer_pending(s.output_timer));
    dm_test_clock(1250, true);
    g_assert_cmpuint(edge_count, ==, 3);
    g_assert_true(edges[0].level);
    g_assert_cmpuint(edges[1].ns, ==, 417);
    g_assert_false(edges[1].level);
    g_assert_cmpuint(edges[2].ns, ==, 1250);
    g_assert_true(edges[2].level);
    wr(&s, 0x34, 0); /* WS2812 reset suffix must drive low. */
    g_assert_false(edges[edge_count - 1].level);
    edge_count = 0;
    dm_test_clock(100000, true);
    g_assert_cmpuint(edge_count, ==, 0);
    wr(&s, 0x20, 12); /* NP still reverses the single output. */
    g_assert_true(edges[edge_count - 1].level);
    dm_mc02_tim2_cleanup(&s);
}

static unsigned changed_count;
static void changed(void *opaque)
{
    changed_count++;
}

static void test_compare_preload(void)
{
    DmMc02Tim2 s;
    uint64_t deadline;

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, changed, NULL);
    dm_mc02_tim2_set_output(&s, 1, false, output, NULL);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x20, 1);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    wr(&s, 0x18, (6u << 4) | (1u << 3));
    edge_count = 0;
    wr(&s, 0, 1);
    dm_test_clock(100, false);
    deadline = s.next_compare_ns;
    changed_count = 0;
    s.iomem.ops->write(&s, 0x34, 20, 2); /* DMA halfword preload write. */
    g_assert_cmpuint(s.iomem.ops->read(&s, 0x34, 2), ==, 20);
    g_assert_cmpuint(s.active_ccr[0], ==, 10);
    g_assert_cmpuint(s.next_compare_ns, ==, deadline);
    g_assert_cmpuint(changed_count, ==, 0);
    dm_test_clock(1250, true);
    g_assert_cmpuint(s.active_ccr[0], ==, 20);
    g_assert_cmpuint(changed_count, ==, 1);
    g_assert_cmpuint(edges[1].ns, ==, 417); /* Old width until UEV. */
    dm_test_clock(2100, true);
    g_assert_cmpuint(edges[3].ns, ==, 2084); /* New width in next period. */
    dm_mc02_tim2_cleanup(&s);
}

typedef struct CompareTrace {
    DmMc02Tim2 *timer;
    unsigned count;
    uint64_t timestamps[4];
    unsigned pin_edge_counts[4];
    unsigned update_count;
    uint64_t updates[4];
    bool write_preload;
} CompareTrace;

static void preload_on_compare(void *opaque, unsigned events, uint64_t ns)
{
    static const uint32_t widths[] = { 20, 10, 20, 10 };
    CompareTrace *trace = opaque;

    /* This is the timer-to-DMA request seam: no request may be batched,
     * repeated, or delivered before its corresponding physical pin edge. */
    g_assert_cmpuint(events, ==, 1);
    g_assert_cmpuint(trace->count, <, G_N_ELEMENTS(widths));
    g_assert_cmpuint(edge_count, >, 0);
    g_assert_cmpuint(edges[edge_count - 1].ns, ==, ns);
    g_assert_true(edges[edge_count - 1].driven);
    g_assert_false(edges[edge_count - 1].level);
    trace->timestamps[trace->count] = ns;
    trace->pin_edge_counts[trace->count] = edge_count;
    trace->timer->iomem.ops->write(trace->timer, 0x34,
                                  widths[trace->count], 2);
    trace->count++;
}

static void test_compare_request_order(gconstpointer data)
{
    static const uint64_t expected_edges[] = {
        0, 417, 1250, 2084, 2500, 2917, 3750, 4584, 5000
    };
    static const uint64_t expected_requests[] = { 417, 2084, 2917, 4584 };
    DmMc02Tim2 s;
    CompareTrace trace = { .timer = &s };
    unsigned clock_mode = GPOINTER_TO_UINT(data);

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_compare_callback(&s, preload_on_compare, &trace);
    dm_mc02_tim2_set_output(&s, 1, false, output, NULL);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x20, 1);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    wr(&s, 0x18, (6u << 4) | (1u << 3));
    edge_count = 0;
    wr(&s, 0, 1);
    if (clock_mode == 2) {
        dm_test_clock_set(5000);
        dm_mc02_tim2_sync_output(&s, 5000);
    } else {
        dm_test_clock(5000, clock_mode);
    }
    g_assert_cmpuint(edge_count, ==, G_N_ELEMENTS(expected_edges));
    for (unsigned i = 0; i < edge_count; ++i) {
        g_assert_cmpuint(edges[i].ns, ==, expected_edges[i]);
        g_assert_true(edges[i].driven);
        g_assert_cmpint(edges[i].level, ==, !(i & 1));
    }
    g_assert_cmpuint(trace.count, ==, G_N_ELEMENTS(expected_requests));
    for (unsigned i = 0; i < trace.count; ++i) {
        g_assert_cmpuint(trace.timestamps[i], ==, expected_requests[i]);
        g_assert_cmpuint(trace.pin_edge_counts[i], ==, 2 * (i + 1));
    }
    g_assert_false(timer_pending(s.output_timer));
    dm_mc02_tim2_cleanup(&s);
}

static void test_hardware_deadline_moves(void)
{
    DmMc02Tim2 s;

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_output(&s, 1, false, output, NULL);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x20, 1);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    edge_count = 0;
    wr(&s, 0, 1);
    dm_test_clock(1000, false);
    g_assert_false(timer_pending(s.output_timer));
    g_assert_cmpuint(s.next_update_ns, ==, 1250);
    /* Replacing a hardware consumer rebuilds its scheduler from now.  The
     * physical pin must retain its own counter phase and missing deadline. */
    dm_mc02_tim2_set_update_callback(&s, NULL, NULL);
    g_assert_cmpuint(s.next_update_ns, ==, 2250);
    g_assert_true(timer_pending(s.output_timer));
    g_assert_cmpuint(s.next_output_ns, ==, 1250);
    dm_test_clock(2000, true);
    g_assert_cmpuint(edge_count, ==, 4);
    g_assert_cmpuint(edges[2].ns, ==, 1250);
    g_assert_true(edges[2].level);
    g_assert_cmpuint(edges[3].ns, ==, 1667);
    g_assert_false(edges[3].level);
    dm_mc02_tim2_cleanup(&s);
}

static void precise_compare(void *opaque, unsigned events, uint64_t ns)
{
    static const uint32_t widths[] = { 20, 10, 20, 10 };
    CompareTrace *trace = opaque;

    g_assert_cmpuint(events, ==, 1);
    g_assert_cmpuint(trace->count, <, G_N_ELEMENTS(trace->timestamps));
    g_assert_cmpuint(dm_mc02_tim2_event_time(trace->timer), ==, ns);
    trace->timestamps[trace->count] = ns;
    if (trace->write_preload) {
        trace->timer->iomem.ops->write(trace->timer, 0x34,
                                      widths[trace->count], 2);
    }
    trace->count++;
}

static void precise_update(void *opaque)
{
    CompareTrace *trace = opaque;

    g_assert_cmpuint(trace->update_count, <, G_N_ELEMENTS(trace->updates));
    trace->updates[trace->update_count++] =
        dm_mc02_tim2_event_time(trace->timer);
}

static void test_precise_compare_without_pin(void)
{
    DmMc02Tim2 s;
    CompareTrace trace = { .timer = &s };

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_compare_callback(&s, precise_compare, &trace);
    dm_mc02_tim2_set_compare_batch(&s, 64);
    dm_mc02_tim2_set_compare_precise(&s, true);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    wr(&s, 0, 1);
    g_assert_false(dm_mc02_tim2_output_connected(&s));
    dm_test_clock(5000, true);
    g_assert_cmpuint(trace.count, ==, 4);
    for (unsigned i = 0; i < trace.count; ++i) {
        g_assert_cmpuint(trace.timestamps[i], ==, 417 + i * 1250);
    }
    g_assert_cmpuint(s.next_compare_ns, ==, 5417);
    dm_mc02_tim2_cleanup(&s);
}

static void zero_transition_compare(void *opaque, unsigned events, uint64_t ns)
{
    static const uint32_t widths[] = { 0, 20, 0, 20 };
    CompareTrace *trace = opaque;

    precise_compare(opaque, events, ns);
    wr(trace->timer, 0x34, widths[trace->count - 1]);
}

static void test_rollover_compare(void)
{
    static const uint64_t expected[] = { 417, 1250, 3334, 3750 };
    static const uint64_t expected_edges[] = { 0, 417, 2500, 3334, 5000 };
    DmMc02Tim2 s;
    CompareTrace trace = { .timer = &s };

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    edge_count = 0;
    dm_mc02_tim2_set_output(&s, 1, false, output, NULL);
    dm_mc02_tim2_set_compare_callback(&s, zero_transition_compare, &trace);
    dm_mc02_tim2_set_compare_precise(&s, true);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    wr(&s, 0x18, (6u << 4) | (1u << 3));
    wr(&s, 0x20, 1);
    edge_count = 0;
    wr(&s, 0, 1);
    dm_test_clock(5000, true);
    g_assert_cmpuint(trace.count, ==, G_N_ELEMENTS(expected));
    for (unsigned i = 0; i < trace.count; ++i) {
        g_assert_cmpuint(trace.timestamps[i], ==, expected[i]);
    }
    g_assert_cmpuint(edge_count, ==, G_N_ELEMENTS(expected_edges));
    for (unsigned i = 0; i < edge_count; ++i) {
        g_assert_cmpuint(edges[i].ns, ==, expected_edges[i]);
        g_assert_cmpint(edges[i].level, ==, !(i & 1));
    }
    g_assert_cmpuint(s.next_compare_ns, ==, 5834);
    dm_mc02_tim2_cleanup(&s);
}

static void test_long_running_phase(void)
{
    DmMc02Tim2 s;
    DmMc02TimPwmOutputState state;

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x20, 1);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    wr(&s, 0, 1);
    /* 1000 seconds * 24MHz exceeds a 64-bit nanosecond product, while the
     * periodic pin phase must stay identical to the first timer cycle. */
    dm_test_clock_set(UINT64_C(1000000000200));
    g_assert_true(dm_mc02_tim2_get_pwm_output_state(&s, 1, &state));
    g_assert_true(state.main_level);
    dm_test_clock_set(UINT64_C(1000000000800));
    g_assert_true(dm_mc02_tim2_get_pwm_output_state(&s, 1, &state));
    g_assert_false(state.main_level);
    dm_mc02_tim2_cleanup(&s);
}

static void test_zero_compare_queue(void)
{
    DmMc02Tim2 s;
    CompareTrace trace = { .timer = &s };

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_compare_callback(&s, precise_compare, &trace);
    dm_mc02_tim2_set_compare_precise(&s, true);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 0);
    wr(&s, 0, 1);
    g_assert_true(s.compare_owned_by_update);
    g_assert_false(timer_pending(s.compare_timer));
    dm_test_clock(5000, true);
    g_assert_cmpuint(trace.count, ==, 4);
    for (unsigned i = 0; i < trace.count; ++i) {
        g_assert_cmpuint(trace.timestamps[i], ==, 1250 * (i + 1));
    }
    g_assert_true(s.compare_owned_by_update);
    g_assert_false(timer_pending(s.compare_timer));
    dm_mc02_tim2_cleanup(&s);
}

static void zero_compare_reconfigure(void *opaque, unsigned events, uint64_t ns)
{
    CompareTrace *trace = opaque;

    precise_compare(opaque, events, ns);
    if (trace->count == 1) {
        wr(trace->timer, 0x24, 6);
    }
}

static void test_zero_compare_reconfigure(void)
{
    static const uint64_t expected[] = { 1250, 2250, 3500, 4750 };
    DmMc02Tim2 s;
    CompareTrace trace = { .timer = &s };

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_compare_callback(&s, zero_compare_reconfigure, &trace);
    dm_mc02_tim2_set_compare_precise(&s, true);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 0);
    wr(&s, 0, 1);
    dm_test_clock(5000, true);
    g_assert_cmpuint(trace.count, ==, G_N_ELEMENTS(expected));
    for (unsigned i = 0; i < trace.count; ++i) {
        g_assert_cmpuint(trace.timestamps[i], ==, expected[i]);
    }
    g_assert_true(timer_pending(s.update_timer));
    g_assert_cmpuint(s.next_update_ns, ==, 6000);
    dm_mc02_tim2_cleanup(&s);
}

static void test_precise_preload_without_pin(void)
{
    static const uint64_t expected[] = { 417, 2084, 2917, 4584 };
    DmMc02Tim2 s;
    CompareTrace trace = { .timer = &s, .write_preload = true };

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_compare_callback(&s, precise_compare, &trace);
    dm_mc02_tim2_set_update_callback(&s, precise_update, &trace);
    dm_mc02_tim2_set_compare_batch(&s, 64);
    dm_mc02_tim2_set_update_batch(&s, 64);
    dm_mc02_tim2_set_compare_precise(&s, true);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    wr(&s, 0x18, (6u << 4) | (1u << 3));
    wr(&s, 0, 1);
    dm_test_clock(5000, true);
    g_assert_cmpuint(trace.count, ==, G_N_ELEMENTS(expected));
    g_assert_cmpuint(trace.update_count, ==, 4);
    for (unsigned i = 0; i < trace.count; ++i) {
        g_assert_cmpuint(trace.timestamps[i], ==, expected[i]);
        g_assert_cmpuint(trace.updates[i], ==, 1250 * (i + 1));
    }
    g_assert_cmpuint(s.active_ccr[0], ==, 10);
    g_assert_cmpuint(s.next_compare_ns, ==, 5417);
    g_assert_cmpuint(s.next_update_ns, ==, 6250);
    dm_mc02_tim2_cleanup(&s);
}

static void invert_on_compare(void *opaque, unsigned events, uint64_t ns)
{
    CompareTrace *trace = opaque;

    if (!trace->count++) {
        wr(trace->timer, 0x20, 3);
    }
}

static void test_compare_changes_output(void)
{
    static const uint64_t expected[] = { 0, 417, 417, 1250, 1667 };
    DmMc02Tim2 s;
    CompareTrace trace = { .timer = &s };

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_compare_callback(&s, invert_on_compare, &trace);
    dm_mc02_tim2_set_output(&s, 1, false, output, NULL);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x20, 1);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    edge_count = 0;
    wr(&s, 0, 1);
    dm_test_clock(2000, true);
    g_assert_cmpuint(edge_count, ==, G_N_ELEMENTS(expected));
    for (unsigned i = 0; i < edge_count; ++i) {
        g_assert_cmpuint(edges[i].ns, ==, expected[i]);
        g_assert_true(edges[i].driven);
        g_assert_cmpint(edges[i].level, ==, !(i & 1));
    }
    dm_mc02_tim2_cleanup(&s);
}

typedef struct OutputReconfigure {
    DmMc02Tim2 *timer;
    bool rewound;
} OutputReconfigure;

static void rewind_on_output(void *opaque, uint64_t ns, bool driven, bool level)
{
    OutputReconfigure *state = opaque;

    output(NULL, ns, driven, level);
    if (ns == 417 && driven && !level && !state->rewound) {
        state->rewound = true;
        wr(state->timer, 0x24, 5);
    }
}

static void test_output_changes_counter_phase(void)
{
    static const uint64_t expected[] = { 0, 417, 417, 626, 1459, 1876 };
    DmMc02Tim2 s;
    OutputReconfigure state = { .timer = &s };

    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_output(&s, 1, false, rewind_on_output, &state);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x20, 1);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    edge_count = 0;
    wr(&s, 0, 1);
    /* The pin consumer rewinds CNT during the falling-edge callback. The
     * resulting nested sample must own the phase used for future edges. */
    dm_test_clock(2000, true);
    g_assert_true(state.rewound);
    g_assert_cmpuint(edge_count, ==, G_N_ELEMENTS(expected));
    for (unsigned i = 0; i < edge_count; ++i) {
        g_assert_cmpuint(edges[i].ns, ==, expected[i]);
        g_assert_true(edges[i].driven);
        g_assert_cmpint(edges[i].level, ==, !(i & 1));
    }
    dm_mc02_tim2_cleanup(&s);
}

static void restore_window_env(const char *saved)
{
    if (saved) {
        g_setenv("DM_TIM_EVENT_WINDOW_NS", saved, true);
    } else {
        g_unsetenv("DM_TIM_EVENT_WINDOW_NS");
    }
}

static void test_event_window_requests(gconstpointer data)
{
    unsigned window = GPOINTER_TO_UINT(data);
    g_autofree char *saved = g_strdup(g_getenv("DM_TIM_EVENT_WINDOW_NS"));
    g_autofree char *value = g_strdup_printf("%u", window);

    g_setenv("DM_TIM_EVENT_WINDOW_NS", value, true);
    /* The existing fixture checks every request, preload transfer, pin edge
     * and original timestamp. Only global queue dispatch count may change. */
    test_compare_request_order(GUINT_TO_POINTER(0));
    g_assert_cmpuint(dm_test_clock_dispatches(), ==, 5000 / window);
    test_precise_compare_without_pin();
    g_assert_cmpuint(dm_test_clock_dispatches(), ==, 1);
    restore_window_env(saved);
}

static void test_event_window_mmio_sync(void)
{
    static const uint64_t expected[] = { 0, 417, 1250, 2084, 2500 };
    g_autofree char *saved = g_strdup(g_getenv("DM_TIM_EVENT_WINDOW_NS"));
    DmMc02Tim2 s;

    g_setenv("DM_TIM_EVENT_WINDOW_NS", "5000", true);
    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_output(&s, 1, false, output, NULL);
    wr(&s, 0x18, (6u << 4) | (1u << 3));
    wr(&s, 0x20, 1);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    wr(&s, 0x14, 1); /* Initial shadow-to-active transfer. */
    wr(&s, 0x10, 0);
    edge_count = 0;
    wr(&s, 0, 1);
    dm_test_clock(500, false);
    g_assert_cmpuint(edge_count, ==, 1);
    /* SR must expose the 417 ns compare even before the 5 us queue wakeup. */
    g_assert_cmpuint(s.iomem.ops->read(&s, 0x10, 4) & 2, ==, 2);
    g_assert_cmpuint(edge_count, ==, 2);
    wr(&s, 0x34, 20);
    dm_test_clock(2500, false);
    /* A stop/configuration write must first deliver all old-state events. */
    wr(&s, 0x20, 0);
    g_assert_cmpuint(edge_count, ==, G_N_ELEMENTS(expected) + 1);
    for (unsigned i = 0; i < G_N_ELEMENTS(expected); ++i) {
        g_assert_cmpuint(edges[i].ns, ==, expected[i]);
        g_assert_true(edges[i].driven);
        g_assert_cmpint(edges[i].level, ==, !(i & 1));
    }
    g_assert_cmpuint(edges[edge_count - 1].ns, ==, 2500);
    g_assert_false(edges[edge_count - 1].driven);
    g_assert_cmpuint(dm_test_clock_dispatches(), ==, 0);
    dm_mc02_tim2_cleanup(&s);
    restore_window_env(saved);
}

typedef struct WindowMasterTrace {
    unsigned count;
    DmMc02TimMasterOutput output;
    DmMc02TimMasterEventKind kind;
    uint64_t timestamp_ns;
} WindowMasterTrace;

static void window_master_event(void *opaque, const DmMc02TimMasterEvent *event)
{
    WindowMasterTrace *trace = opaque;

    trace->count++;
    trace->output = event->output;
    trace->kind = event->kind;
    trace->timestamp_ns = event->timestamp_ns;
}

static void test_event_window_master_fallback(void)
{
    g_autofree char *saved = g_strdup(g_getenv("DM_TIM_EVENT_WINDOW_NS"));
    DmMc02Tim2 s;
    WindowMasterTrace trace = { 0 };
    uint64_t compare_ns;

    g_setenv("DM_TIM_EVENT_WINDOW_NS", "5000", true);
    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_mms2_supported(&s, true);
    dm_mc02_tim2_set_event_callback(&s, window_master_event, &trace);
    dm_mc02_tim2_set_output(&s, 1, false, output, NULL);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x20, 1);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    edge_count = 0;
    wr(&s, 0, 1);
    /* Reset/enable routes do not prevent local window batching. */
    g_assert_cmpuint(dm_mc02_tim2_event_window_ns(&s), ==, 5000);
    g_assert_cmpuint(timer_expire_time_ns(s.compare_timer), ==, 5000);
    dm_test_clock(500, false);
    g_assert_cmpuint(edge_count, ==, 1);
    wr(&s, 0x04, 2u << 4); /* TRGO UPDATE: another device needs each event. */
    g_assert_cmpuint(edge_count, ==, 2); /* Old 417 ns edge flushed first. */
    g_assert_cmpuint(edges[1].ns, ==, 417);
    g_assert_cmpuint(dm_mc02_tim2_event_window_ns(&s), ==, 0);
    g_assert_cmpuint(timer_expire_time_ns(s.update_timer), ==, 1250);
    g_assert_cmpuint(timer_expire_time_ns(s.compare_timer), ==,
                     s.next_compare_ns);
    dm_test_clock(1250, false);
    g_assert_cmpuint(trace.count, ==, 1);
    g_assert_cmpuint(trace.kind, ==, DM_MC02_TIM_MASTER_UPDATE);
    g_assert_cmpuint(trace.timestamp_ns, ==, 1250);
    wr(&s, 0x04, 1u << 4); /* ENABLE routing restores the window. */
    g_assert_cmpuint(dm_mc02_tim2_event_window_ns(&s), ==, 5000);
    g_assert_cmpuint(timer_expire_time_ns(s.update_timer), ==, 5000);
    g_assert_cmpuint(timer_expire_time_ns(s.compare_timer), ==, 5000);
    wr(&s, 0x04, (1u << 4) | (3u << 20)); /* TRGO2 compare also excludes it. */
    g_assert_cmpuint(dm_mc02_tim2_event_window_ns(&s), ==, 0);
    compare_ns = s.next_compare_ns;
    g_assert_cmpuint(timer_expire_time_ns(s.compare_timer), ==, compare_ns);
    dm_test_clock(compare_ns, false);
    g_assert_cmpuint(trace.count, ==, 2);
    g_assert_cmpuint(trace.output, ==, DM_MC02_TIM_TRGO2);
    g_assert_cmpuint(trace.kind, ==, DM_MC02_TIM_MASTER_COMPARE1);
    g_assert_cmpuint(trace.timestamp_ns, ==, compare_ns);
    wr(&s, 0x04, 0);
    g_assert_cmpuint(dm_mc02_tim2_event_window_ns(&s), ==, 5000);
    g_assert_cmpuint(timer_expire_time_ns(s.update_timer), ==, 5000);
    g_assert_cmpuint(timer_expire_time_ns(s.compare_timer), ==, 5000);
    dm_mc02_tim2_cleanup(&s);
    restore_window_env(saved);
}

static bool window_allowed(void *opaque)
{
    return *(bool *)opaque;
}

static void test_event_window_external_eligibility(void)
{
    static const uint64_t expected[] = { 0, 417, 1250, 1667, 2500 };
    g_autofree char *saved = g_strdup(g_getenv("DM_TIM_EVENT_WINDOW_NS"));
    DmMc02Tim2 s;
    bool allowed = true;
    bool denied = false;

    g_setenv("DM_TIM_EVENT_WINDOW_NS", "5000", true);
    dm_test_clock_reset();
    dm_mc02_tim2_init(&s, NULL, NULL, NULL);
    dm_mc02_tim2_set_window_allowed(&s, window_allowed, &allowed);
    dm_mc02_tim2_set_output(&s, 1, false, output, NULL);
    wr(&s, 0x18, 6u << 4);
    wr(&s, 0x20, 1);
    wr(&s, 0x2c, 29);
    wr(&s, 0x34, 10);
    edge_count = 0;
    wr(&s, 0, 1);
    g_assert_cmpuint(dm_mc02_tim2_event_window_ns(&s), ==, 5000);
    g_assert_cmpuint(timer_expire_time_ns(s.compare_timer), ==, 5000);
    /* Changing eligibility must retain even an already-due internal event;
     * it must not recompute the next match from the current clock. */
    dm_test_clock_set(500);
    allowed = false;
    dm_mc02_tim2_window_changed(&s);
    g_assert_cmpuint(dm_mc02_tim2_event_window_ns(&s), ==, 0);
    g_assert_cmpuint(s.next_compare_ns, ==, 417);
    g_assert_cmpuint(timer_expire_time_ns(s.compare_timer), ==, 417);
    dm_test_clock(500, false);
    g_assert_cmpuint(edge_count, ==, 2);
    g_assert_cmpuint(edges[1].ns, ==, 417);
    allowed = true;
    dm_mc02_tim2_window_changed(&s);
    g_assert_cmpuint(s.next_update_ns, ==, 1250);
    g_assert_cmpuint(s.next_compare_ns, ==, 1667);
    g_assert_cmpuint(timer_expire_time_ns(s.update_timer), ==, 5000);
    g_assert_cmpuint(timer_expire_time_ns(s.compare_timer), ==, 5000);
    dm_test_clock(2500, false);
    g_assert_cmpuint(edge_count, ==, 2);
    /* Replacing the callback synchronizes through now using its old policy. */
    dm_mc02_tim2_set_window_allowed(&s, window_allowed, &denied);
    g_assert_cmpuint(edge_count, ==, G_N_ELEMENTS(expected));
    for (unsigned i = 0; i < G_N_ELEMENTS(expected); ++i) {
        g_assert_cmpuint(edges[i].ns, ==, expected[i]);
        g_assert_true(edges[i].driven);
        g_assert_cmpint(edges[i].level, ==, !(i & 1));
    }
    g_assert_cmpuint(dm_mc02_tim2_event_window_ns(&s), ==, 0);
    g_assert_cmpuint(s.next_update_ns, ==, 3750);
    g_assert_cmpuint(s.next_compare_ns, ==, 2917);
    g_assert_cmpuint(timer_expire_time_ns(s.update_timer), ==, 3750);
    g_assert_cmpuint(timer_expire_time_ns(s.compare_timer), ==, 2917);
    dm_mc02_tim2_cleanup(&s);
    restore_window_env(saved);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_data_func("/timer/output/pwm", GINT_TO_POINTER(false), test_pwm_timeline);
    g_test_add_data_func("/timer/output/late", GINT_TO_POINTER(true), test_pwm_timeline);
    g_test_add_data_func("/timer/output/pad-sync", GINT_TO_POINTER(2), test_pwm_timeline);
    g_test_add_func("/timer/output/complementary", test_complementary_deadtime);
    g_test_add_func("/timer/output/n-only", test_n_only_output);
    g_test_add_func("/timer/output/preload", test_compare_preload);
    g_test_add_func("/timer/output/mmio-one-timestamp", test_mmio_has_one_timestamp);
    g_test_add_data_func("/timer/output/requests", GUINT_TO_POINTER(0), test_compare_request_order);
    g_test_add_data_func("/timer/output/requests-late", GUINT_TO_POINTER(1), test_compare_request_order);
    g_test_add_data_func("/timer/output/requests-pad-sync", GUINT_TO_POINTER(2), test_compare_request_order);
    g_test_add_func("/timer/output/hardware-deadline-moves", test_hardware_deadline_moves);
    g_test_add_func("/timer/compare/precise-without-pin", test_precise_compare_without_pin);
    g_test_add_func("/timer/compare/precise-preload-without-pin", test_precise_preload_without_pin);
    g_test_add_func("/timer/compare/rollover-preload", test_rollover_compare);
    g_test_add_func("/timer/output/long-running-phase", test_long_running_phase);
    g_test_add_func("/timer/compare/zero-shared-queue", test_zero_compare_queue);
    g_test_add_func("/timer/compare/zero-reconfigure", test_zero_compare_reconfigure);
    g_test_add_func("/timer/output/consumer-changes-stage", test_compare_changes_output);
    g_test_add_func("/timer/output/consumer-changes-phase", test_output_changes_counter_phase);
    g_test_add_data_func("/timer/window/1250ns", GUINT_TO_POINTER(1250), test_event_window_requests);
    g_test_add_data_func("/timer/window/2500ns", GUINT_TO_POINTER(2500), test_event_window_requests);
    g_test_add_data_func("/timer/window/5000ns", GUINT_TO_POINTER(5000), test_event_window_requests);
    g_test_add_func("/timer/window/mmio-sync", test_event_window_mmio_sync);
    g_test_add_func("/timer/window/master-fallback", test_event_window_master_fallback);
    g_test_add_func("/timer/window/external-eligibility", test_event_window_external_eligibility);
    return g_test_run();
}
