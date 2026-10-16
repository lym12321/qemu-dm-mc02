/* SPDX-License-Identifier: GPL-2.0-or-later */

#include "qemu/osdep.h"
#include "libqtest.h"
#include "qemu/bswap.h"
#include "qemu/sockets.h"
#include "qapi/qmp/qdict.h"
#include "qapi/qmp/qlist.h"
#include "qapi/qmp/qnum.h"
#include "qapi/qmp/qstring.h"
#include <glib/gstdio.h>

#define PPB_BASE          0xe000e000ull
#define SYST_CSR          (PPB_BASE + 0x10)
#define SYST_RVR          (PPB_BASE + 0x14)
#define SYST_CVR          (PPB_BASE + 0x18)
#define NVIC_ISER0        (PPB_BASE + 0x100)
#define NVIC_ICER0        (PPB_BASE + 0x180)
#define NVIC_ISPR0        (PPB_BASE + 0x200)
#define NVIC_ISPR1        (PPB_BASE + 0x204)
#define NVIC_ICPR0        (PPB_BASE + 0x280)
#define NVIC_ICPR1        (PPB_BASE + 0x284)
#define NVIC_IPR0         (PPB_BASE + 0x400)
#define SCB_ICSR          (PPB_BASE + 0xd04)
#define SCB_AIRCR         (PPB_BASE + 0xd0c)

#define EXTI_BASE         0x58000000ull
#define EXTI_FTSR1        (EXTI_BASE + 0x04)
#define EXTI_D3PCR1H      (EXTI_BASE + 0x14)
#define EXTI_PR1          (EXTI_BASE + 0x88)
#define EXTI_C1IMR1       (EXTI_BASE + 0x80)

#define SYST_CSR_ENABLE       (1u << 0)
#define SYST_CSR_TICKINT      (1u << 1)
#define SYST_CSR_CLKSOURCE    (1u << 2)
#define SYST_CSR_COUNTFLAG    (1u << 16)
#define SCB_ICSR_PENDSTSET    (1u << 26)
#define SCB_AIRCR_VECTKEY     (0x5fau << 16)
#define SCB_AIRCR_SYSRESETREQ (1u << 2)
#define RCC_RSR               0x580244d0ull
#define RCC_RSR_RMVF          (1u << 16)
#define RCC_RSR_BORRSTF       (1u << 21)
#define RCC_RSR_PINRSTF       (1u << 22)
#define RCC_RSR_PORRSTF       (1u << 23)
#define RCC_RSR_SFTRSTF       (1u << 24)
#define RCC_BASE              0x58024400ull
#define TEST_EXTERNAL_IRQ     28
#define TEST_EXTI_IRQ         40
#define EXECUTION_COUNTER     0x20000000ull
#define EXECUTION_SENTINEL    0xa55a5aa5u
#define EXECUTION_ICOUNT      "shift=0,align=off,sleep=off"

static QTestState *execution_start_image(const char *machine,
                                         const uint8_t *image, size_t size,
                                         const char *extra_args)
{
    g_autofree char *path = NULL;
    g_autofree char *quoted_path = NULL;
    QTestState *qts;
    int fd;

    fd = g_file_open_tmp("dm-qemu.vtime-XXXXXX", &path, NULL);
    g_assert_cmpint(fd, >=, 0);
    g_assert_cmpint(write(fd, image, size), ==, size);
    g_assert_cmpint(close(fd), ==, 0);
    quoted_path = g_shell_quote(path);
    qts = qtest_initf("-machine %s -nodefaults -accel tcg,thread=single "
                      "-S -icount " EXECUTION_ICOUNT " -kernel %s %s",
                      machine, quoted_path, extra_args ?: "");
    g_assert_cmpint(g_unlink(path), ==, 0);
    return qts;
}

/*
 * These native TCG cases use the qtest memory channel, never its dummy CPU or
 * clock_step. Upstream libqtest appends -accel qtest as a fallback; the
 * explicit first TCG accelerator must execute this guest. Fixed shift=0 is
 * one ns per
 * instruction, not a Cortex-M cycle timing claim (docs/devel/tcg-icount.rst).
 *
 * The literals below are independent Thumb instructions and Cortex-M vectors:
 * busy: ldr r0, counter; movs r1, #0; str r1, [r0]; then four-instruction
 *       ldr/adds/str/b loop.
 * idle: wfi; b idle.  SysTick: ldr r0, counter; ldr/adds/str; bx lr.
 * ARMv7-M exception 15 and ST CMSIS SysTick register definitions supply the IRQ
 * boundary; the real QEMU ARMv7M/NVIC/SysTick remain its only state owners.
 */
static QTestState *execution_guest(const char *machine, uint32_t flash_base,
                                   bool idle, const char *extra_args)
{
    uint8_t image[0x58] = { 0 };

    stl_le_p(image, 0x20002000); /* Initial MSP. */
    stl_le_p(image + 4, flash_base + 0x41);
    if (idle) {
        stl_le_p(image + 15 * 4, flash_base + 0x49);
        stw_le_p(image + 0x40, 0xbf30); /* wfi */
        stw_le_p(image + 0x42, 0xe7fd); /* b 0x40 */
        stw_le_p(image + 0x48, 0x4802); /* ldr r0, [pc, #8] */
        stw_le_p(image + 0x4a, 0x6801); /* ldr r1, [r0] */
        stw_le_p(image + 0x4c, 0x3101); /* adds r1, #1 */
        stw_le_p(image + 0x4e, 0x6001); /* str r1, [r0] */
        stw_le_p(image + 0x50, 0x4770); /* bx lr */
        stl_le_p(image + 0x54, EXECUTION_COUNTER);
    } else {
        stw_le_p(image + 0x40, 0x4803); /* ldr r0, [pc, #12] */
        stw_le_p(image + 0x42, 0x2100); /* movs r1, #0 */
        stw_le_p(image + 0x44, 0x6001); /* str r1, [r0] */
        stw_le_p(image + 0x46, 0x6801); /* ldr r1, [r0] */
        stw_le_p(image + 0x48, 0x3101); /* adds r1, #1 */
        stw_le_p(image + 0x4a, 0x6001); /* str r1, [r0] */
        stw_le_p(image + 0x4c, 0xe7fb); /* b 0x46 */
        stl_le_p(image + 0x50, EXECUTION_COUNTER);
    }
    return execution_start_image(machine, image, sizeof(image), extra_args);
}

static QDict *execution_query(QTestState *qts)
{
    QDict *response = qtest_qmp(qts,
                              "{'execute':'x-query-virtual-execution'}");
    QDict *result = qdict_get_qdict(response, "return");

    g_assert_nonnull(result);
    qobject_ref(result);
    qobject_unref(response);
    return result;
}

/* Only call at stopped, stable IO points; this is not an atomic snapshot. */
static QDict *execution_can_snapshot(QTestState *qts, const char *state,
                                     int64_t current, bool has_target,
                                     int64_t target, const char *run_state)
{
    static const char *fields[] = {
        "execution", "status", "fdcan-tx-state", "fdcan-rx-state",
    };
    static const char *commands[] = {
        "x-query-virtual-execution", "query-status",
    };
    QDict *response = qtest_qmp(qts,
        "{'execute':'qom-get','arguments':{'path':'/machine',"
        "'property':'can-execution-state'}}");
    QDict *snapshot = qdict_get_qdict(response, "return");
    QDict *execution, *status;

    g_assert_nonnull(snapshot);
    g_assert_cmpuint(qdict_size(snapshot), ==, 5);
    g_assert_cmpint(qdict_get_int(snapshot, "schema"), ==, 1);
    execution = qdict_get_qdict(snapshot, "execution");
    g_assert_nonnull(execution);
    g_assert_cmpuint(qdict_size(execution), ==, has_target ? 3 : 2);
    g_assert_cmpstr(qdict_get_str(execution, "state"), ==, state);
    g_assert_cmpint(qdict_get_int(execution, "current-ns"), ==, current);
    g_assert_cmpint(qdict_haskey(execution, "target-ns"), ==, has_target);
    if (has_target) {
        g_assert_cmpint(qdict_get_int(execution, "target-ns"), ==, target);
    }
    status = qdict_get_qdict(snapshot, "status");
    g_assert_nonnull(status);
    g_assert_cmpuint(qdict_size(status), ==, 3);
    g_assert_false(qdict_get_bool(status, "running"));
    g_assert_false(qdict_get_bool(status, "singlestep"));
    g_assert_cmpstr(qdict_get_str(status, "status"), ==, run_state);
    for (unsigned i = 2; i < G_N_ELEMENTS(fields); i++) {
        QDict *can = qdict_get_qdict(snapshot, fields[i]);

        g_assert_nonnull(can);
        g_assert_cmpuint(qdict_size(can), ==, 3);
        g_assert_cmpint(qdict_get_int(can, "schema"), ==, 1);
        g_assert_cmpint(qdict_get_int(can, "timestamp_ns"), ==, current);
        g_assert_nonnull(qdict_get_qdict(can, "controllers"));
    }

    /* Supplement independent literals with unchanged public-getter values. */
    for (unsigned i = 0; i < G_N_ELEMENTS(fields); i++) {
        QDict *separate = i < G_N_ELEMENTS(commands) ?
            qtest_qmp(qts, "{'execute':%s}", commands[i]) :
            qtest_qmp(qts,
                "{'execute':'qom-get','arguments':{'path':'/machine',"
                "'property':%s}}", fields[i]);

        g_assert_nonnull(qdict_get(separate, "return"));
        g_assert_true(qobject_is_equal(qdict_get(snapshot, fields[i]),
                                      qdict_get(separate, "return")));
        qobject_unref(separate);
    }
    qobject_ref(snapshot);
    qobject_unref(response);
    return snapshot;
}

static void execution_assert_stopped(QTestState *qts, const char *state)
{
    QDict *response = qtest_qmp(qts, "{'execute':'query-status'}");
    QDict *result = qdict_get_qdict(response, "return");

    g_assert_false(qdict_get_bool(result, "running"));
    g_assert_cmpstr(qdict_get_str(result, "status"), ==, state);
    qobject_unref(response);
}

static void execution_assert_state(QTestState *qts, const char *state,
                                   bool has_target, int64_t target)
{
    QDict *result = execution_query(qts);

    g_assert_cmpstr(qdict_get_str(result, "state"), ==, state);
    g_assert_cmpint(qdict_haskey(result, "target-ns"), ==, has_target);
    if (has_target) {
        g_assert_cmpint(qdict_get_int(result, "target-ns"), ==, target);
    }
    qobject_unref(result);
}

static void execution_wait_reached(QTestState *qts, int64_t target)
{
    int64_t deadline = g_get_monotonic_time() + 10 * G_TIME_SPAN_SECOND;

    for (;;) {
        QDict *result = execution_query(qts);
        bool reached = !strcmp(qdict_get_str(result, "state"), "reached");

        g_assert_cmpint(qdict_get_int(result, "target-ns"), ==, target);
        if (reached) {
            g_assert_cmpint(qdict_get_int(result, "current-ns"), ==, target);
            qobject_unref(result);
            execution_assert_stopped(qts, "paused");
            return;
        }
        g_assert_cmpstr(qdict_get_str(result, "state"), ==, "running");
        g_assert_cmpint(qdict_get_int(result, "current-ns"), <=, target);
        qobject_unref(result);
        g_assert_cmpint(g_get_monotonic_time(), <, deadline);
    }
}

static void execution_run_to(QTestState *qts, int64_t target)
{
    qtest_qmp_assert_success(qts, "{'execute':'x-run-to-virtual-time',"
                            "'arguments':{'target-ns':%" PRId64 "}}", target);
    execution_wait_reached(qts, target);
}

static void execution_assert_stable(QTestState *qts, int64_t current,
                                    uint32_t counter)
{
    /* QMP/memory IO cycles the native main loop while the CPU stays stopped. */
    for (unsigned i = 0; i < 4; i++) {
        QDict *result = execution_query(qts);

        g_assert_cmpint(qdict_get_int(result, "current-ns"), ==, current);
        qobject_unref(result);
        execution_assert_stopped(qts, "paused");
        g_assert_cmphex(qtest_readl(qts, EXECUTION_COUNTER), ==, counter);
    }
}

static void test_execution_reject_profiles(void)
{
    static const char *profiles[] = {
        "-machine dm-mc02 -S", /* Actual qtest accelerator. */
        "-machine dm-mc02 -S -accel tcg,thread=single",
        "-machine dm-mc02 -S -accel tcg,thread=single "
            "-icount shift=auto,align=off,sleep=on",
        "-machine dm-mc02 -S -accel tcg,thread=single "
            "-icount shift=1,align=off,sleep=off",
        "-machine dm-mc02 -S -accel tcg,thread=single "
            "-icount shift=0,align=off,sleep=on",
        "-machine dm-mc02 -S -accel tcg,thread=single "
            "-icount shift=0,align=on,sleep=on",
        "-machine virt -smp 2 -S -accel tcg,thread=single "
            "-icount " EXECUTION_ICOUNT,
        "-machine dm-mc02 -S -accel tcg,thread=single "
            "-icount " EXECUTION_ICOUNT " -object iothread,id=extra",
        "-machine dm-mc02 -S -accel tcg,thread=single "
            "-icount " EXECUTION_ICOUNT
            " -blockdev driver=null-co,node-name=extra",
        "-machine dm-mc02 -S -accel tcg,thread=single "
            "-icount " EXECUTION_ICOUNT
            " -drive if=none,id=extra,driver=null-co",
    };

    for (unsigned i = 0; i < G_N_ELEMENTS(profiles); i++) {
        QTestState *qts = qtest_init(profiles[i]);
        QDict *before = execution_query(qts);
        QDict *after;

        g_test_message("reject profile: %s", profiles[i]);
        qobject_unref(qtest_qmp_assert_failure_ref(qts,
            "{'execute':'x-run-to-virtual-time','arguments':{'target-ns':1}}"));
        after = execution_query(qts);
        g_assert_cmpstr(qdict_get_str(after, "state"), ==, "idle");
        g_assert_false(qdict_haskey(after, "target-ns"));
        g_assert_cmpint(qdict_get_int(after, "current-ns"), ==,
                        qdict_get_int(before, "current-ns"));
        qobject_unref(before);
        qobject_unref(after);
        /* Rejection must not resume the initially stopped VM. */
        before = qtest_qmp(qts, "{'execute':'query-status'}");
        g_assert_false(qdict_get_bool(qdict_get_qdict(before, "return"),
                                      "running"));
        qobject_unref(before);
        qtest_quit(qts);
    }
}

static void test_execution_reject_targets(void)
{
    static const char *arguments[] = {
        "{}", "{'target-ns':-1}", "{'target-ns':0}",
        "{'target-ns':18446744073709551615}", "{'target-ns':1003.5}",
        "{'target-ns':true}", "{'target-ns':'1003'}", "{'target-ns':null}",
        "{'target-ns':1003,'unknown':0}",
    };
    QTestState *qts = execution_guest("dm-mc02", 0x08000000, false, NULL);

    qtest_writel(qts, EXECUTION_COUNTER, EXECUTION_SENTINEL);
    for (unsigned i = 0; i < G_N_ELEMENTS(arguments); i++) {
        g_autofree char *request = g_strdup_printf(
            "{'execute':'x-run-to-virtual-time','arguments':%s}",
            arguments[i]);
        QDict *result;

        qtest_qmp_send_raw(qts, "%s", request);
        result = qtest_qmp_receive(qts);
        g_assert_true(qdict_haskey(result, "error"));
        g_assert_false(qdict_haskey(result, "return"));
        qobject_unref(result);
        execution_assert_state(qts, "idle", false, 0);
        result = execution_query(qts);
        g_assert_cmpint(qdict_get_int(result, "current-ns"), ==, 0);
        qobject_unref(result);
        g_assert_cmphex(qtest_readl(qts, EXECUTION_COUNTER), ==,
                        EXECUTION_SENTINEL);
    }
    /* Native cont is not an implicitly armed target request. */
    qtest_qmp_assert_success(qts, "{'execute':'cont'}");
    qobject_unref(qtest_qmp_assert_failure_ref(qts,
        "{'execute':'x-run-to-virtual-time',"
        "'arguments':{'target-ns':9223372036854775807}}"));
    execution_assert_state(qts, "idle", false, 0);
    qtest_qmp_assert_success(qts, "{'execute':'stop'}");
    execution_assert_stopped(qts, "paused");
    g_assert_cmphex(qtest_readl(qts, EXECUTION_COUNTER), !=,
                    EXECUTION_SENTINEL);
    qtest_quit(qts);
}

static void check_execution_busy_guest(const char *machine, uint32_t flash_base,
                                       const char *extra_args)
{
    QTestState *qts = execution_guest(machine, flash_base, false, extra_args);
    uint32_t counter;

    qtest_writel(qts, EXECUTION_COUNTER, EXECUTION_SENTINEL);
    /* Exactly two instructions may execute before the initial str at ns 2. */
    execution_run_to(qts, 2);
    execution_assert_stable(qts, 2, EXECUTION_SENTINEL);
    execution_run_to(qts, 3);
    execution_assert_stable(qts, 3, 0);
    execution_run_to(qts, 1003);
    counter = qtest_readl(qts, EXECUTION_COUNTER);
    g_assert_cmpuint(counter, >, 0);
    execution_assert_stable(qts, 1003, counter);
    /* A later interval resumes the same CPU without resetting it. */
    execution_run_to(qts, 2009);
    g_assert_cmpuint(qtest_readl(qts, EXECUTION_COUNTER), >, counter);
    execution_assert_stable(qts, 2009, qtest_readl(qts, EXECUTION_COUNTER));
    qtest_quit(qts);
}

static void test_execution_busy_guest(void)
{
    check_execution_busy_guest("dm-mc02", 0x08000000, NULL);
    /* Same generic QMP entry and guest, with upstream board memory geometry. */
    check_execution_busy_guest("mps2-an500", 0, "-only-migratable");
}

static void test_execution_can_snapshot_compat(void)
{
    QTestState *qts = execution_guest("dm-mc02", 0x08000000, false,
                                     "-compat deprecated-output=hide");
    QDict *response = qtest_qmp(qts,
        "{'execute':'qom-get','arguments':{'path':'/machine',"
        "'property':'can-execution-state'}}");
    QDict *snapshot = qdict_get_qdict(response, "return");
    QDict *status;
    QDict *separate = qtest_qmp(qts, "{'execute':'query-status'}");
    QDict *original = qdict_get_qdict(separate, "return");

    g_assert_nonnull(snapshot);
    g_assert_cmpuint(qdict_size(snapshot), ==, 5);
    g_assert_cmpint(qdict_get_int(snapshot, "schema"), ==, 1);
    status = qdict_get_qdict(snapshot, "status");
    g_assert_nonnull(status);
    g_assert_nonnull(original);
    g_assert_cmpuint(qdict_size(status), ==, 2);
    g_assert_false(qdict_haskey(status, "singlestep"));
    g_assert_false(qdict_haskey(original, "singlestep"));
    g_assert_false(qdict_get_bool(status, "running"));
    g_assert_cmpstr(qdict_get_str(status, "status"), ==, "prelaunch");
    g_assert_true(qobject_is_equal(QOBJECT(status), QOBJECT(original)));
    qobject_unref(separate);
    qobject_unref(response);
    qtest_quit(qts);
}

static void test_execution_wfi_systick(void)
{
    QTestState *qts = execution_guest("dm-mc02", 0x08000000, true, NULL);

    qtest_writel(qts, EXECUTION_COUNTER, 0);
    /*
     * ST CMSIS D1CPRE /8: 64 MHz -> 8 MHz, reload 7 -> 1000 ns.
     * The integer 125 ns period avoids the native ptimer's fractional first-
     * reload truncation (15 + 984 = 999 ns at 64 MHz with reload 63).
     * Configure before the target timer so the device owns its deadline.
     */
    qtest_writel(qts, RCC_BASE + 0x18, 0xau << 8);
    qtest_writel(qts, SYST_RVR, 7);
    qtest_writel(qts, SYST_CVR, 0);
    qtest_writel(qts, SYST_CSR,
                 SYST_CSR_ENABLE | SYST_CSR_TICKINT | SYST_CSR_CLKSOURCE);
    execution_run_to(qts, 999);
    execution_assert_stable(qts, 999, 0);
    g_assert_false(qtest_readl(qts, SCB_ICSR) & SCB_ICSR_PENDSTSET);
    execution_run_to(qts, 1000);
    execution_assert_stable(qts, 1000, 0);
    uint32_t icsr = qtest_readl(qts, SCB_ICSR);
    g_assert_true((icsr & SCB_ICSR_PENDSTSET) || (icsr & 0x1ff) == 15);
    for (unsigned repeat = 0; repeat < 2; repeat++) {
        qobject_unref(execution_can_snapshot(qts, "reached", 1000, true,
                                            1000, "paused"));
        g_assert_cmphex(qtest_readl(qts, SCB_ICSR), ==, icsr);
        execution_assert_stable(qts, 1000, 0);
    }
    /* Completion at T is a stopped CPU, not a guest IRQ-handler barrier. */
    execution_run_to(qts, 1001);
    execution_assert_stable(qts, 1001, 0);
    execution_run_to(qts, 1009);
    execution_assert_stable(qts, 1009, 1);
    g_assert_false(qtest_readl(qts, SCB_ICSR) & SCB_ICSR_PENDSTSET);
    qtest_quit(qts);

    qts = execution_guest("dm-mc02", 0x08000000, true, NULL);
    qtest_writel(qts, EXECUTION_COUNTER, 0);
    qtest_writel(qts, RCC_BASE + 0x18, 0xau << 8);
    qtest_writel(qts, SYST_RVR, 3); /* First IRQ 500 ns; then 1000 ns. */
    qtest_writel(qts, SYST_CVR, 0);
    qtest_writel(qts, SYST_CSR,
                 SYST_CSR_ENABLE | SYST_CSR_TICKINT | SYST_CSR_CLKSOURCE);
    /*
     * Target is queued before the first IRQ requeues its 1000 ns deadline:
     * exercise the opposite equal-deadline insertion order.
     */
    execution_run_to(qts, 1000);
    execution_assert_stable(qts, 1000, 1);
    execution_run_to(qts, 1009);
    execution_assert_stable(qts, 1009, 2);
    qtest_quit(qts);
}

static void execution_assert_migration_blocker(QTestState *qts, bool expected)
{
    QDict *response = qtest_qmp(qts, "{'execute':'query-migrate'}");
    QDict *result = qdict_get_qdict(response, "return");
    QList *reasons = qdict_haskey(result, "blocked-reasons") ?
        qdict_get_qlist(result, "blocked-reasons") : NULL;
    QListEntry *entry;
    bool found = false;

    if (reasons) {
        QLIST_FOREACH_ENTRY(reasons, entry) {
            const char *reason = qstring_get_str(
                qobject_to(QString, qlist_entry_obj(entry)));

            found |= !strcmp(reason, "Virtual execution target is outstanding");
        }
    }
    g_assert_cmpint(found, ==, expected);
    qobject_unref(response);
}

static void test_execution_cancel_and_reentry(void)
{
    QTestState *qts = execution_guest("dm-mc02", 0x08000000, false, NULL);
    const int64_t far_target = INT64_MAX;
    QDict *result;
    int64_t current;
    uint32_t counter;

    qtest_qmp_assert_success(qts,
        "{'execute':'x-run-to-virtual-time','arguments':{'target-ns':%"
        PRId64 "}}", far_target);
    execution_assert_state(qts, "running", true, far_target);
    execution_assert_migration_blocker(qts, true);
    qobject_unref(qtest_qmp_assert_failure_ref(qts,
        "{'execute':'x-run-to-virtual-time','arguments':{'target-ns':%"
        PRId64 "}}", far_target - 1));
    execution_assert_state(qts, "running", true, far_target);
    qtest_qmp_assert_success(qts, "{'execute':'stop'}");
    execution_assert_state(qts, "cancelled", true, far_target);
    execution_assert_migration_blocker(qts, false);
    result = execution_query(qts);
    current = qdict_get_int(result, "current-ns");
    qobject_unref(result);
    g_assert_cmpint(current, <, far_target);
    counter = qtest_readl(qts, EXECUTION_COUNTER);
    execution_assert_stable(qts, current, counter);
    qobject_unref(execution_can_snapshot(qts, "cancelled", current, true,
                                        far_target, "paused"));
    execution_assert_stable(qts, current, counter);
    execution_run_to(qts, current + 1003);
    execution_assert_migration_blocker(qts, false);
    /* Refusing a stale target preserves the latest successful result. */
    qobject_unref(qtest_qmp_assert_failure_ref(qts,
        "{'execute':'x-run-to-virtual-time','arguments':{'target-ns':%"
        PRId64 "}}", current + 1003));
    execution_assert_state(qts, "reached", true, current + 1003);
    qobject_unref(execution_can_snapshot(qts, "reached", current + 1003,
                                        true, current + 1003, "paused"));
    qtest_writel(qts, SYST_RVR, 0x1234);
    qtest_qmp_assert_success(qts,
        "{'execute':'x-run-to-virtual-time','arguments':{'target-ns':%"
        PRId64 "}}", far_target);
    qtest_qmp_assert_success(qts, "{'execute':'system_reset'}");
    /* Native reset need not stop a running VM; the caller explicitly stops. */
    qtest_qmp_assert_success(qts, "{'execute':'stop'}");
    execution_assert_state(qts, "cancelled", true, far_target);
    execution_assert_migration_blocker(qts, false);
    execution_assert_stopped(qts, "paused");
    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0);
    result = execution_query(qts);
    current = qdict_get_int(result, "current-ns");
    qobject_unref(result);
    qobject_unref(execution_can_snapshot(qts, "cancelled", current, true,
                                        far_target, "paused"));
    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0);
    execution_run_to(qts, current + 1003);
    qtest_qmp_assert_success(qts, "{'execute':'system_reset'}");
    execution_assert_state(qts, "cancelled", true, current + 1003);
    /* Native reset of a stopped VM returns to PRELAUNCH, not PAUSED. */
    execution_assert_stopped(qts, "prelaunch");
    result = execution_query(qts);
    qobject_unref(execution_can_snapshot(qts, "cancelled",
                                        qdict_get_int(result, "current-ns"),
                                        true, current + 1003, "prelaunch"));
    qobject_unref(result);
    execution_run_to(qts, current + 2006);
    qtest_quit(qts);
}

#ifndef _WIN32
static void test_execution_requester_disconnect(void)
{
    g_autofree char *directory = g_strdup("/tmp/dm-qemu.vtime.XXXXXX");
    g_assert_nonnull(g_mkdtemp(directory));
    g_autofree char *path = g_build_filename(directory, "request.qmp", NULL);
    g_autofree char *quoted_path = g_shell_quote(path);
    g_autofree char *extra_args = g_strdup_printf(
        "-qmp unix:%s,server=on,wait=off", quoted_path);
    QTestState *qts = execution_guest("dm-mc02", 0x08000000, false, extra_args);
    const int64_t target = 100000003;
    struct timeval timeout = { .tv_sec = 2 };
    QDict *response;
    int fd = unix_connect(path, NULL);

    g_assert_nonnull(directory);
    g_assert_cmpint(fd, >=, 0);
    g_assert_cmpint(setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO,
                              &timeout, sizeof(timeout)), ==, 0);
    response = qmp_fd_receive(fd);
    g_assert_true(qdict_haskey(response, "QMP"));
    qobject_unref(response);
    response = qmp_fd(fd, "{'execute':'qmp_capabilities'}");
    g_assert_true(qdict_haskey(response, "return"));
    qobject_unref(response);
    qmp_fd_send(fd, "{'execute':'x-run-to-virtual-time',"
                "'arguments':{'target-ns':%" PRId64 "}}", target);
    do {
        response = qmp_fd_receive(fd);
        if (!qdict_haskey(response, "event")) {
            break;
        }
        qobject_unref(response);
    } while (true);
    g_assert_true(qdict_haskey(response, "return"));
    qobject_unref(response);
    /* The requester leaves while its finite process-owned request runs. */
    execution_assert_state(qts, "running", true, target);
    g_assert_cmpint(close(fd), ==, 0);
    execution_wait_reached(qts, target);
    execution_assert_stable(qts, target, qtest_readl(qts, EXECUTION_COUNTER));
    qtest_quit(qts);
    g_assert_false(g_file_test(path, G_FILE_TEST_EXISTS));
    g_assert_cmpint(g_rmdir(directory), ==, 0);
}
#endif

#ifdef CONFIG_LINUX
typedef struct ExecutionCanCleanup {
    int fd;
    const char *path;
    const char *directory;
} ExecutionCanCleanup;

static void execution_can_abort_cleanup(void *opaque)
{
    ExecutionCanCleanup *cleanup = opaque;

    if (cleanup->fd >= 0) {
        close(cleanup->fd);
    }
    g_unlink(cleanup->path);
    g_rmdir(cleanup->directory);
}

static void execution_can_recv(int fd, uint8_t *buffer, size_t size,
                               int64_t deadline)
{
    int64_t remaining = deadline - g_get_monotonic_time();
    struct timeval timeout;

    g_assert_cmpint(remaining, >, 0);
    timeout.tv_sec = remaining / G_TIME_SPAN_SECOND;
    timeout.tv_usec = remaining % G_TIME_SPAN_SECOND;
    g_assert_cmpint(setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO,
                              &timeout, sizeof(timeout)), ==, 0);
    g_assert_cmpint(recv(fd, buffer, size, MSG_WAITALL), ==, size);
    g_assert_cmpint(g_get_monotonic_time(), <=, deadline);
}

/*
 * Independent classic CAN RAM layout: ST HAL FDCAN_CopyMessageToRAM,
 * standard ID << 18, DLC << 16, then eight literal payload bytes.
 * The wire contract is <IIB3xQ64s; no production encoder supplies expected.
 */
static const uint8_t periodic_can_tx_ram[2][16] = {
    { 0x00, 0x00, 0x00, 0x08, 0x00, 0x00, 0x08, 0x00,
      0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88 },
    { 0x00, 0x00, 0xfc, 0x07, 0x00, 0x00, 0x08, 0x00,
      0x88, 0x77, 0x66, 0x55, 0x44, 0x33, 0x22, 0x11 },
};
static const uint8_t periodic_can_expected_wire[2][84] = {
    { [1] = 0x02, [8] = 0x08,
      [20] = 0x11, [21] = 0x22, [22] = 0x33, [23] = 0x44,
      [24] = 0x55, [25] = 0x66, [26] = 0x77, [27] = 0x88 },
    { [0] = 0xff, [1] = 0x01, [8] = 0x08,
      [20] = 0x88, [21] = 0x77, [22] = 0x66, [23] = 0x55,
      [24] = 0x44, [25] = 0x33, [26] = 0x22, [27] = 0x11 },
};

static void execution_periodic_can_image(uint8_t image[0x74])
{
    /*
     * Thumb handler submits the two frames, then increments the pair ledger.
     * Third invocation disables SysTick. The finite paths take 12/15 guest
     * instructions, plus at most b/WFI after return: <= 17 icount ns.
     * CMSIS exception 15 and register encodings, not Cortex-M cycle timing.
     */
    stl_le_p(image, 0x20002000);
    stl_le_p(image + 4, 0x08000041);
    stl_le_p(image + 15 * 4, 0x08000049);
    stw_le_p(image + 0x40, 0xbf30); /* wfi */
    stw_le_p(image + 0x42, 0xe7fd); /* b 0x40 */
    stw_le_p(image + 0x48, 0x4807); /* ldr r0, [pc, #28] */
    stw_le_p(image + 0x4a, 0x2101); /* movs r1, #1 */
    stw_le_p(image + 0x4c, 0x6001); /* str r1, [r0]: TXBAR slot 0 */
    stw_le_p(image + 0x4e, 0x2102); /* movs r1, #2 */
    stw_le_p(image + 0x50, 0x6001); /* str r1, [r0]: TXBAR slot 1 */
    stw_le_p(image + 0x52, 0x4806); /* ldr r0, [pc, #24] */
    stw_le_p(image + 0x54, 0x6801); /* ldr r1, [r0] */
    stw_le_p(image + 0x56, 0x3101); /* adds r1, #1 */
    stw_le_p(image + 0x58, 0x6001); /* str r1, [r0]: completed pair */
    stw_le_p(image + 0x5a, 0x2903); /* cmp r1, #3 */
    stw_le_p(image + 0x5c, 0xd102); /* bne 0x64 */
    stw_le_p(image + 0x5e, 0x4804); /* ldr r0, [pc, #16] */
    stw_le_p(image + 0x60, 0x2100); /* movs r1, #0 */
    stw_le_p(image + 0x62, 0x6001); /* str r1, [r0]: disable SysTick */
    stw_le_p(image + 0x64, 0x4770); /* bx lr */
    stw_le_p(image + 0x66, 0xbf00); /* unexecuted alignment nop */
    stl_le_p(image + 0x68, 0x4000a0d0);
    stl_le_p(image + 0x6c, EXECUTION_COUNTER);
    stl_le_p(image + 0x70, SYST_CSR);
}

static void test_execution_periodic_can(void)
{
    static const int64_t targets[] = {
        1000000, 2000000, 3000000, 3000200,
    };
    uint8_t image[0x74] = { 0 };
    g_autofree char *directory = g_strdup("/tmp/dm-qemu.vtime-can.XXXXXX");
    g_autofree char *path = NULL;
    g_autofree char *quoted_path = NULL;
    g_autofree char *extra_args = NULL;
    ExecutionCanCleanup cleanup = { .fd = -1 };
    QTestState *qts;
    int64_t connect_deadline;
    int64_t receive_deadline = 0;
    uint64_t previous_timestamp = 0;

    execution_periodic_can_image(image);

    g_assert_nonnull(g_mkdtemp(directory));
    path = g_build_filename(directory, "can.sock", NULL);
    quoted_path = g_shell_quote(path);
    extra_args = g_strdup_printf(
        "-chardev socket,id=can1,path=%s,server=on,wait=off "
        "-serial none -serial none -serial none -serial none "
        "-serial none -serial none -serial none -serial chardev:can1",
        quoted_path);
    cleanup.path = path;
    cleanup.directory = directory;
    /* Native kill hook is prepended during launch, before this path cleanup. */
    qtest_add_abrt_handler(execution_can_abort_cleanup, &cleanup);
    /* host ACK is model admission only, not physical CAN acknowledgement. */
    qts = execution_start_image(
        "dm-mc02,fdcan-host-ack=on,electrical-power=on",
        image, sizeof(image), extra_args);
    cleanup.fd = unix_connect(path, NULL);
    g_assert_cmpint(cleanup.fd, >=, 0);
    connect_deadline = g_get_monotonic_time() + 2 * G_TIME_SPAN_SECOND;
    while (!qtest_qom_get_bool(qts, "/chardevs/can1", "connected")) {
        g_assert_cmpint(g_get_monotonic_time(), <, connect_deadline);
    }
    g_assert_cmpint(g_get_monotonic_time(), <=, connect_deadline);

    qtest_writel(qts, EXECUTION_COUNTER, 0);
    /* CMSIS GPIOCEN bit 2 and real PC15 output feed the board's 5 V rail. */
    qtest_writel(qts, RCC_BASE + 0xe0,
                 qtest_readl(qts, RCC_BASE + 0xe0) | (1u << 2));
    qtest_writel(qts, 0x58020800,
                 (qtest_readl(qts, 0x58020800) & ~(3u << 30)) | (1u << 30));
    qtest_writel(qts, 0x58020818, 1u << 15);
    g_assert_cmphex(qtest_readl(qts, 0x58020814) & (1u << 15), ==, 1u << 15);
    g_assert_true(qtest_qom_get_bool(qts, "/machine", "system-5v-good"));

    /* CMSIS TFQS=2, NDTB=0, TFQM=0, byte offset 0; classic 8-byte slots. */
    qtest_writel(qts, 0x4000a018, 1);
    qtest_writel(qts, 0x4000a0c0, 0x02000000);
    qtest_writel(qts, 0x4000a0c8, 0);
    qtest_memwrite(qts, 0x4000ac00, periodic_can_tx_ram,
                   sizeof(periodic_can_tx_ram));
    qtest_writel(qts, 0x4000a018, 0);
    /* CMSIS D1CPRE /8: 64 MHz -> 8 MHz, integer 125 ns, exactly 1 ms. */
    qtest_writel(qts, RCC_BASE + 0x18, 0xau << 8);
    qtest_writel(qts, SYST_RVR, 7999);
    qtest_writel(qts, SYST_CVR, 0);
    qtest_writel(qts, SYST_CSR,
                 SYST_CSR_ENABLE | SYST_CSR_TICKINT | SYST_CSR_CLKSOURCE);

    for (unsigned pair = 0; pair < G_N_ELEMENTS(targets); pair++) {
        execution_run_to(qts, targets[pair]);
        execution_assert_stable(qts, targets[pair], pair);
        if (!pair) {
            continue; /* Handler at exactly 1 ms has not submitted a pair. */
        }
        if (pair == 1) {
            receive_deadline = g_get_monotonic_time() + 2 * G_TIME_SPAN_SECOND;
        }
        for (unsigned frame = 0; frame < 2; frame++) {
            uint8_t wire[84];
            uint64_t timestamp;

            /* Client read fragmentation does not force short writes. */
            execution_can_recv(cleanup.fd, wire, 17, receive_deadline);
            execution_can_recv(cleanup.fd, wire + 17, 67, receive_deadline);
            timestamp = ldq_le_p(wire + 12);
            g_test_message("CAN pair %u record %u timestamp %" PRIu64 " ns",
                           pair, 2 * (pair - 1) + frame, timestamp);
            g_assert_cmpuint(timestamp, >, previous_timestamp);
            g_assert_cmpuint(timestamp, >=, pair * 1000000);
            g_assert_cmpuint(timestamp, <=, pair * 1000000 + 17);
            g_assert_cmpuint(timestamp, <=, targets[pair]);
            previous_timestamp = timestamp;
            memset(wire + 12, 0, 8);
            g_assert_cmpmem(wire, sizeof(wire),
                            periodic_can_expected_wire[frame],
                            sizeof(periodic_can_expected_wire[frame]));
        }
    }
    g_assert_cmphex(qtest_readl(qts, SYST_CSR) &
                    (SYST_CSR_ENABLE | SYST_CSR_TICKINT | SYST_CSR_CLKSOURCE),
                    ==, 0);
    g_assert_cmphex(qtest_readl(qts, SCB_ICSR) &
                    (0x1ffu | SCB_ICSR_PENDSTSET), ==, 0);
    execution_assert_stable(qts, 3000200, 3);

    g_assert_cmpint(close(cleanup.fd), ==, 0);
    cleanup.fd = -1;
    qtest_quit(qts);
    g_assert_false(g_file_test(path, G_FILE_TEST_EXISTS));
    g_assert_cmpint(g_rmdir(directory), ==, 0);
    qtest_remove_abrt_handler(&cleanup);
}

static void execution_can_check(QTestState *qts, int64_t deadline)
{
    g_assert_cmpint(g_get_monotonic_time(), <, deadline);
    g_assert_true(qtest_probe_child(qts));
}

static uint64_t execution_can_uint(QDict *state, const char *field)
{
    QNum *value = qobject_to(QNum, qdict_get(state, field));
    uint64_t result;

    g_assert_nonnull(value);
    g_assert_true(qnum_get_try_uint(value, &result));
    return result;
}

static QDict *execution_can_state(QTestState *qts, bool eval, int64_t time,
                                  int64_t deadline)
{
    static const char *routes[] = {
        "dm-mc02.fdcan1", "dm-mc02.fdcan2", "dm-mc02.fdcan3",
    };
    static const char *fields[] = {
        "pending_frames", "pending_bytes", "tx_dropped", "tx_short_writes",
    };
    QDict *response, *state, *controllers;
    QNum *timestamp;
    int64_t actual_time;

    execution_can_check(qts, deadline);
    response = qtest_qmp(qts,
        "{'execute':'qom-get','arguments':{'path':'/machine',"
        "'property':'fdcan-tx-state'}}");
    execution_can_check(qts, deadline);
    state = qdict_get_qdict(response, "return");
    g_assert_nonnull(state);
    g_assert_cmpuint(qdict_size(state), ==, 3);
    g_assert_cmpuint(execution_can_uint(state, "schema"), ==, 1);
    timestamp = qobject_to(QNum, qdict_get(state, "timestamp_ns"));
    g_assert_nonnull(timestamp);
    g_assert_true(qnum_get_try_int(timestamp, &actual_time));
    g_assert_cmpint(actual_time, ==, time);
    controllers = qdict_get_qdict(state, "controllers");
    g_assert_nonnull(controllers);
    g_assert_cmpuint(qdict_size(controllers), ==, eval ? 1 : 3);
    for (unsigned i = 0; i < (eval ? 1 : G_N_ELEMENTS(routes)); ++i) {
        QDict *controller = qdict_get_qdict(controllers,
            eval ? "stm32h723-eval.fdcan1" : routes[i]);

        g_assert_nonnull(controller);
        g_assert_cmpuint(qdict_size(controller), ==, 4);
        for (unsigned j = 0; j < G_N_ELEMENTS(fields); ++j) {
            uint64_t value = execution_can_uint(controller, fields[j]);

            if (eval || i || j == 2) {
                g_assert_cmpuint(value, ==, 0);
            } else if (j < 2) {
                g_assert_cmpuint(value, <=, j ? 1344 : 16);
            }
        }
    }
    qobject_ref(state);
    qobject_unref(response);
    return state;
}

static void execution_can_empty_readonly(QTestState *qts, bool eval,
                                        int64_t deadline)
{
    static const char *routes[] = {
        "dm-mc02.fdcan1", "dm-mc02.fdcan2", "dm-mc02.fdcan3",
    };
    static const char *tx_fields[] = {
        "pending_frames", "pending_bytes", "tx_dropped", "tx_short_writes",
    };
    static const char *rx_fields[] = {
        "completed_frames", "partial_bytes", "rx_dropped",
    };
    QDict *before = execution_can_state(qts, eval, 0, deadline);
    QDict *after;
    QDict *snapshot_before = NULL;
    QDict *controllers = qdict_get_qdict(before, "controllers");
    const QDictEntry *entry;

    /* Both profiles must start with all four producer observations empty. */
    for (entry = qdict_first(controllers); entry; entry = qdict_next(
             controllers, entry)) {
        QDict *controller = qobject_to(QDict, qdict_entry_value(entry));

        g_assert_cmpuint(execution_can_uint(controller, "pending_frames"),
                         ==, 0);
        g_assert_cmpuint(execution_can_uint(controller, "pending_bytes"),
                         ==, 0);
        g_assert_cmpuint(execution_can_uint(controller, "tx_short_writes"),
                         ==, 0);
    }
    after = execution_can_state(qts, eval, 0, deadline);
    g_assert_true(qobject_is_equal(QOBJECT(before), QOBJECT(after)));
    qobject_unref(after);
    execution_can_check(qts, deadline);
    qobject_unref(qtest_qmp_assert_failure_ref(qts,
        "{'execute':'qom-set','arguments':{'path':'/machine',"
        "'property':'fdcan-tx-state','value':{}}}"));
    execution_can_check(qts, deadline);
    after = execution_can_state(qts, eval, 0, deadline);
    g_assert_true(qobject_is_equal(QOBJECT(before), QOBJECT(after)));
    qobject_unref(before);
    qobject_unref(after);
    for (unsigned repeat = 0; repeat < 2; repeat++) {
        QDict *snapshot = execution_can_snapshot(qts, "idle", 0, false, 0,
                                                 "prelaunch");

        if (!repeat) {
            snapshot_before = snapshot;
            qobject_ref(snapshot_before);
        } else {
            g_assert_true(qobject_is_equal(QOBJECT(snapshot_before),
                                          QOBJECT(snapshot)));
        }
        for (unsigned direction = 0; direction < 2; direction++) {
            QDict *can = qdict_get_qdict(snapshot,
                direction ? "fdcan-rx-state" : "fdcan-tx-state");
            QDict *all = qdict_get_qdict(can, "controllers");
            const char *const *fields = direction ? rx_fields : tx_fields;
            unsigned field_count = direction ? G_N_ELEMENTS(rx_fields) :
                                               G_N_ELEMENTS(tx_fields);

            g_assert_nonnull(all);
            g_assert_cmpuint(qdict_size(all), ==, eval ? 1 : 3);
            for (unsigned i = 0; i < (eval ? 1 : G_N_ELEMENTS(routes)); i++) {
                QDict *controller = qdict_get_qdict(all,
                    eval ? "stm32h723-eval.fdcan1" : routes[i]);

                g_assert_nonnull(controller);
                g_assert_cmpuint(qdict_size(controller), ==, field_count);
                for (unsigned j = 0; j < field_count; j++) {
                    g_assert_cmpuint(execution_can_uint(controller, fields[j]),
                                     ==, 0);
                }
            }
        }
        qobject_unref(snapshot);
        execution_can_check(qts, deadline);
    }
    qobject_unref(qtest_qmp_assert_failure_ref(qts,
        "{'execute':'qom-set','arguments':{'path':'/machine',"
        "'property':'can-execution-state','value':{}}}"));
    after = execution_can_snapshot(qts, "idle", 0, false, 0, "prelaunch");
    g_assert_true(qobject_is_equal(QOBJECT(snapshot_before), QOBJECT(after)));
    qobject_unref(snapshot_before);
    qobject_unref(after);
    execution_assert_stopped(qts, "prelaunch");
    execution_can_check(qts, deadline);
}

static QDict *execution_can_rx_state(QTestState *qts, bool eval, int64_t time,
                                     int64_t deadline)
{
    static const char *routes[] = {
        "dm-mc02.fdcan1", "dm-mc02.fdcan2", "dm-mc02.fdcan3",
    };
    static const char *fields[] = {
        "completed_frames", "partial_bytes", "rx_dropped",
    };
    QDict *response, *state, *controllers;

    execution_can_check(qts, deadline);
    response = qtest_qmp(qts,
        "{'execute':'qom-get','arguments':{'path':'/machine',"
        "'property':'fdcan-rx-state'}}");
    execution_can_check(qts, deadline);
    state = qdict_get_qdict(response, "return");
    g_assert_nonnull(state);
    g_assert_cmpuint(qdict_size(state), ==, 3);
    g_assert_cmpuint(execution_can_uint(state, "schema"), ==, 1);
    g_assert_cmpuint(execution_can_uint(state, "timestamp_ns"), ==, time);
    controllers = qdict_get_qdict(state, "controllers");
    g_assert_nonnull(controllers);
    g_assert_cmpuint(qdict_size(controllers), ==, eval ? 1 : 3);
    for (unsigned i = 0; i < (eval ? 1 : G_N_ELEMENTS(routes)); i++) {
        QDict *controller = qdict_get_qdict(controllers,
            eval ? "stm32h723-eval.fdcan1" : routes[i]);

        g_assert_nonnull(controller);
        g_assert_cmpuint(qdict_size(controller), ==, 3);
        for (unsigned j = 0; j < G_N_ELEMENTS(fields); j++) {
            uint64_t value = execution_can_uint(controller, fields[j]);

            if (eval || i) {
                g_assert_cmpuint(value, ==, 0);
            } else if (j == 1) {
                g_assert_cmpuint(value, <, 84);
            }
        }
    }
    qobject_ref(state);
    qobject_unref(response);
    return state;
}

static void execution_can_rx_empty_readonly(QTestState *qts, bool eval,
                                            int64_t deadline)
{
    QDict *before = execution_can_rx_state(qts, eval, 0, deadline);
    QDict *after;
    QDict *controllers = qdict_get_qdict(before, "controllers");
    QDict *controller = qdict_get_qdict(controllers,
        eval ? "stm32h723-eval.fdcan1" : "dm-mc02.fdcan1");

    g_assert_cmpuint(execution_can_uint(controller, "completed_frames"), ==, 0);
    g_assert_cmpuint(execution_can_uint(controller, "partial_bytes"), ==, 0);
    g_assert_cmpuint(execution_can_uint(controller, "rx_dropped"), ==, 0);
    execution_can_check(qts, deadline);
    qobject_unref(qtest_qmp_assert_failure_ref(qts,
        "{'execute':'qom-set','arguments':{'path':'/machine',"
        "'property':'fdcan-rx-state','value':{}}}"));
    after = execution_can_rx_state(qts, eval, 0, deadline);
    g_assert_true(qobject_is_equal(QOBJECT(before), QOBJECT(after)));
    qobject_unref(before);
    qobject_unref(after);
    execution_assert_stopped(qts, "prelaunch");
    execution_can_check(qts, deadline);
}

static void execution_can_rx_wait(QTestState *qts, uint64_t completed,
                                  unsigned partial, uint64_t dropped,
                                  int64_t deadline)
{
    for (;;) {
        QDict *state = execution_can_rx_state(qts, false, 100, deadline);
        QDict *controllers = qdict_get_qdict(state, "controllers");
        QDict *controller = qdict_get_qdict(controllers, "dm-mc02.fdcan1");
        uint64_t count = execution_can_uint(controller, "completed_frames");
        uint64_t tail = execution_can_uint(controller, "partial_bytes");
        uint64_t drops = execution_can_uint(controller, "rx_dropped");

        g_assert_cmpuint(count, <=, completed);
        g_assert_cmpuint(drops, <=, dropped);
        qobject_unref(state);
        if (count == completed && tail == partial && drops == dropped) {
            return;
        }
        execution_can_check(qts, deadline);
    }
}

static void execution_can_rx_snapshot_readonly(QTestState *qts,
                                               uint64_t completed,
                                               unsigned partial,
                                               uint64_t dropped,
                                               int64_t deadline)
{
    uint8_t before[16], after[16];
    uint32_t fifo = qtest_readl(qts, 0x4000a0a4);
    uint32_t ir = qtest_readl(qts, 0x4000a050);
    uint32_t pending = qtest_readl(qts, NVIC_ISPR0);

    qtest_memread(qts, 0x4000ac00, before, sizeof(before));
    for (unsigned repeat = 0; repeat < 2; repeat++) {
        QDict *snapshot = execution_can_snapshot(qts, "reached", 100, true,
                                                 100, "paused");
        QDict *can = qdict_get_qdict(snapshot, "fdcan-rx-state");
        QDict *controller = qdict_get_qdict(qdict_get_qdict(can, "controllers"),
                                          "dm-mc02.fdcan1");

        g_assert_cmpuint(execution_can_uint(controller, "completed_frames"),
                         ==, completed);
        g_assert_cmpuint(execution_can_uint(controller, "partial_bytes"),
                         ==, partial);
        g_assert_cmpuint(execution_can_uint(controller, "rx_dropped"),
                         ==, dropped);
        qobject_unref(snapshot);
        g_assert_cmphex(qtest_readl(qts, EXECUTION_COUNTER), ==,
                        EXECUTION_SENTINEL);
        g_assert_cmphex(qtest_readl(qts, 0x4000a0a4), ==, fifo);
        g_assert_cmphex(qtest_readl(qts, 0x4000a050), ==, ir);
        g_assert_cmphex(qtest_readl(qts, NVIC_ISPR0), ==, pending);
        qtest_memread(qts, 0x4000ac00, after, sizeof(after));
        g_assert_cmpmem(after, sizeof(after), before, sizeof(before));
        execution_can_check(qts, deadline);
    }
}

static void test_execution_paused_can_receipt(void)
{
    /*
     * Thumb polling guest: wait for RXF0S.F0FL, copy the first payload word
     * to the counter, acknowledge FIFO0 index 0, then WFI. No ISR is needed.
     * The literal pool and instructions are independent of the CAN model.
     */
    uint8_t image[0x6c] = { 0 };
    /* Fixed84 ID0x321/DLC8, future metadata time1000000ns, literal payload. */
    static const uint8_t wire[84] = {
        [0] = 0x21, [1] = 0x03, [8] = 0x08,
        [12] = 0x40, [13] = 0x42, [14] = 0x0f,
        [20] = 0x11, [21] = 0x22, [22] = 0x33, [23] = 0x44,
        [24] = 0x55, [25] = 0x66, [26] = 0x77, [27] = 0x88,
    };
    uint8_t malformed[84], ram[16];
    g_autofree char *directory = g_strdup("/tmp/dm-qemu.vtime-can.XXXXXX");
    g_autofree char *path = NULL;
    g_autofree char *quoted_path = NULL;
    g_autofree char *extra_args = NULL;
    ExecutionCanCleanup cleanup = { .fd = -1 };
    QTestState *qts = qtest_init(
        "-machine dm-mc02,board-profile=STM32H723-EVAL -S");
    int64_t deadline = g_get_monotonic_time() + 10 * G_TIME_SPAN_SECOND;

    execution_can_rx_empty_readonly(qts, true, deadline);
    qtest_quit(qts);
    stl_le_p(image, 0x20002000);
    stl_le_p(image + 4, 0x08000041);
    stw_le_p(image + 0x40, 0x4806); /* ldr r0, [pc, #24]: RXF0S */
    stw_le_p(image + 0x42, 0x6801); /* ldr r1, [r0] */
    stw_le_p(image + 0x44, 0x227f); /* movs r2, #127 */
    stw_le_p(image + 0x46, 0x4211); /* tst r1, r2 */
    stw_le_p(image + 0x48, 0xd0fb); /* beq 0x42 */
    stw_le_p(image + 0x4a, 0x4805); /* ldr r0, [pc, #20]: payload */
    stw_le_p(image + 0x4c, 0x6801); /* ldr r1, [r0] */
    stw_le_p(image + 0x4e, 0x4805); /* ldr r0, [pc, #20]: counter */
    stw_le_p(image + 0x50, 0x6001); /* str r1, [r0]: actual payload */
    stw_le_p(image + 0x52, 0x4805); /* ldr r0, [pc, #20]: RXF0A */
    stw_le_p(image + 0x54, 0x2100); /* movs r1, #0 */
    stw_le_p(image + 0x56, 0x6001); /* str r1, [r0]: acknowledge */
    stw_le_p(image + 0x58, 0xbf30); /* wfi */
    stw_le_p(image + 0x5a, 0xe7fd); /* b 0x58 */
    stl_le_p(image + 0x5c, 0x4000a0a4);
    stl_le_p(image + 0x60, 0x4000ac08);
    stl_le_p(image + 0x64, EXECUTION_COUNTER);
    stl_le_p(image + 0x68, 0x4000a0a8);

    g_assert_nonnull(g_mkdtemp(directory));
    path = g_build_filename(directory, "can.sock", NULL);
    quoted_path = g_shell_quote(path);
    extra_args = g_strdup_printf(
        "-chardev socket,id=can1,path=%s,server=on,wait=off "
        "-serial none -serial none -serial none -serial none "
        "-serial none -serial none -serial none -serial chardev:can1",
        quoted_path);
    cleanup.path = path;
    cleanup.directory = directory;
    qtest_add_abrt_handler(execution_can_abort_cleanup, &cleanup);
    qts = execution_start_image("dm-mc02", image, sizeof(image), extra_args);
    execution_can_rx_empty_readonly(qts, false, deadline);
    cleanup.fd = unix_connect(path, NULL);
    g_assert_cmpint(cleanup.fd, >=, 0);
    while (!qtest_qom_get_bool(qts, "/chardevs/can1", "connected")) {
        execution_can_check(qts, deadline);
    }
    g_assert_true(qtest_qom_get_bool(qts, "/machine", "system-5v-good"));
    qtest_writel(qts, EXECUTION_COUNTER, EXECUTION_SENTINEL);
    /* RM0468/CMSIS RXF0C: two slots at byte offset0; classic8-byte data. */
    qtest_writel(qts, 0x4000a018, 1);
    qtest_writel(qts, 0x4000a0a0, 0x00020000);
    qtest_writel(qts, 0x4000a0bc, 0);
    qtest_writel(qts, 0x4000a018, 0);
    execution_run_to(qts, 100);
    execution_assert_stable(qts, 100, EXECUTION_SENTINEL);

    execution_can_check(qts, deadline);
    g_assert_cmpint(send(cleanup.fd, wire, 83, MSG_DONTWAIT), ==, 83);
    execution_can_rx_wait(qts, 0, 83, 0, deadline);
    execution_assert_stable(qts, 100, EXECUTION_SENTINEL);
    g_assert_cmphex(qtest_readl(qts, 0x4000a0a4) & 0x7f, ==, 0);
    g_assert_cmphex(qtest_readl(qts, 0x4000a050), ==, 0);
    execution_can_rx_snapshot_readonly(qts, 0, 83, 0, deadline);
    g_assert_cmpint(send(cleanup.fd, wire + 83, 1, MSG_DONTWAIT), ==, 1);
    execution_can_rx_wait(qts, 1, 0, 0, deadline);
    execution_assert_stable(qts, 100, EXECUTION_SENTINEL);
    g_assert_cmphex(qtest_readl(qts, 0x4000a0a4) & 0x7f, ==, 1);
    g_assert_cmphex(qtest_readl(qts, 0x4000a050), ==, 1); /* IR.RF0N */
    qtest_memread(qts, 0x4000ac00, ram, sizeof(ram));
    /* HAL FDCAN_GetRxMessage layout, independently decoded from message RAM. */
    g_assert_cmphex(ldl_le_p(ram), ==, 0x0c840000); /* ID0x321 << 18 */
    g_assert_cmphex(ldl_le_p(ram + 4) & 0x800f0000, ==, 0x80080000);
    g_assert_cmpmem(ram + 8, 8, wire + 20, 8);
    execution_can_rx_snapshot_readonly(qts, 1, 0, 0, deadline);

    /* Unknown wire flag is processed, but cannot publish another FIFO slot. */
    memcpy(malformed, wire, sizeof(malformed));
    malformed[4] = 0x80;
    g_assert_cmpint(send(cleanup.fd, malformed, sizeof(malformed),
                         MSG_DONTWAIT), ==,
                    sizeof(malformed));
    execution_can_rx_wait(qts, 2, 0, 1, deadline);
    execution_assert_state(qts, "reached", true, 100);
    execution_assert_stable(qts, 100, EXECUTION_SENTINEL);
    g_assert_cmphex(qtest_readl(qts, 0x4000a0a4) & 0x7f, ==, 1);
    g_assert_cmphex(qtest_readl(qts, 0x4000a050), ==, 1);
    g_assert_cmphex(qtest_readl(qts, 0x4000ac08), ==, 0x44332211);
    execution_can_rx_snapshot_readonly(qts, 2, 0, 1, deadline);

    /* Receipt at100ns is independent of this later real guest read/store. */
    execution_run_to(qts, 200);
    execution_assert_stable(qts, 200, 0x44332211);
    g_assert_cmphex(qtest_readl(qts, 0x4000a0a4) & 0x7f, ==, 0);
    execution_can_check(qts, deadline);
    g_assert_cmpint(close(cleanup.fd), ==, 0);
    cleanup.fd = -1;
    qtest_quit(qts);
    g_assert_false(g_file_test(path, G_FILE_TEST_EXISTS));
    g_assert_cmpint(g_rmdir(directory), ==, 0);
    qtest_remove_abrt_handler(&cleanup);
}

static void test_execution_paused_can_drain(void)
{
    /* ST HAL classic slot0: ID0x200, DLC8; payload is an independent index. */
    uint8_t tx_ram[16] = {
        0x00, 0x00, 0x00, 0x08, 0x00, 0x00, 0x08, 0x00,
    };
    /* Literal <IIB3xQ64s at1000ns; unspecified padding/payload bytes are0. */
    static const uint8_t expected_wire[84] = {
        [1] = 0x02, [8] = 0x08, [12] = 0xe8, [13] = 0x03,
    };
    g_autofree char *directory = g_strdup("/tmp/dm-qemu.vtime-can.XXXXXX");
    g_autofree char *path = NULL;
    g_autofree char *quoted_path = NULL;
    g_autofree char *extra_args = NULL;
    ExecutionCanCleanup cleanup = { .fd = -1 };
    QTestState *qts = qtest_init(
        "-machine dm-mc02,board-profile=STM32H723-EVAL -S");
    QDict *state, *controller;
    int64_t deadline = g_get_monotonic_time() + 2 * G_TIME_SPAN_SECOND;
    unsigned submitted = 0, received = 0, partial_size = 0;
    uint32_t counter;
    uint8_t buffer[4096], partial[84], expected[84];
    uint64_t pending_frames, pending_bytes;
    ssize_t extra;

    execution_can_empty_readonly(qts, true, deadline);
    qtest_quit(qts);
    g_assert_nonnull(g_mkdtemp(directory));
    path = g_build_filename(directory, "can.sock", NULL);
    quoted_path = g_shell_quote(path);
    extra_args = g_strdup_printf(
        "-chardev socket,id=can1,path=%s,server=on,wait=off "
        "-serial none -serial none -serial none -serial none "
        "-serial none -serial none -serial none -serial chardev:can1",
        quoted_path);
    cleanup.path = path;
    cleanup.directory = directory;
    qtest_add_abrt_handler(execution_can_abort_cleanup, &cleanup);
    qts = execution_guest("dm-mc02,fdcan-host-ack=on,electrical-power=on",
                           0x08000000, false, extra_args);
    deadline = g_get_monotonic_time() + 2 * G_TIME_SPAN_SECOND;
    execution_can_empty_readonly(qts, false, deadline);
    execution_can_check(qts, deadline);
    cleanup.fd = unix_connect(path, NULL);
    execution_can_check(qts, deadline);
    g_assert_cmpint(cleanup.fd, >=, 0);
    while (!qtest_qom_get_bool(qts, "/chardevs/can1", "connected")) {
        execution_can_check(qts, deadline);
    }
    execution_can_check(qts, deadline);

    /* Real PC15 output powers the rail; no model-only power override. */
    qtest_writel(qts, RCC_BASE + 0xe0,
                 qtest_readl(qts, RCC_BASE + 0xe0) | (1u << 2));
    qtest_writel(qts, 0x58020800,
                 (qtest_readl(qts, 0x58020800) & ~(3u << 30)) | (1u << 30));
    qtest_writel(qts, 0x58020818, 1u << 15);
    g_assert_cmphex(qtest_readl(qts, 0x58020814) & (1u << 15), ==, 1u << 15);
    g_assert_true(qtest_qom_get_bool(qts, "/machine", "system-5v-good"));
    /* TFQS1, NDTB0, TFQM0, byte offset0, classic8-byte element. */
    qtest_writel(qts, 0x4000a018, 1);
    qtest_writel(qts, 0x4000a0c0, 0x01000000);
    qtest_writel(qts, 0x4000a0c8, 0);
    qtest_writel(qts, 0x4000a018, 0);
    qtest_writel(qts, EXECUTION_COUNTER, EXECUTION_SENTINEL);
    execution_run_to(qts, 1000);
    counter = qtest_readl(qts, EXECUTION_COUNTER);
    g_assert_cmpuint(counter, >, 0);
    g_assert_cmphex(counter, !=, EXECUTION_SENTINEL);
    execution_assert_stable(qts, 1000, counter);

    deadline = g_get_monotonic_time() + 2 * G_TIME_SPAN_SECOND;
    state = execution_can_state(qts, false, 1000, deadline);
    controller = qdict_get_qdict(qdict_get_qdict(state, "controllers"),
                                  "dm-mc02.fdcan1");
    g_assert_cmpuint(execution_can_uint(controller, "pending_frames"), ==, 0);
    g_assert_cmpuint(execution_can_uint(controller, "pending_bytes"), ==, 0);
    qobject_unref(state);
    do {
        execution_can_check(qts, deadline);
        tx_ram[8] = submitted;
        tx_ram[9] = submitted >> 8;
        qtest_memwrite(qts, 0x4000ac00, tx_ram, sizeof(tx_ram));
        execution_can_check(qts, deadline);
        qtest_writel(qts, 0x4000a0d0, 1);
        execution_can_check(qts, deadline);
        submitted++;
        state = execution_can_state(qts, false, 1000, deadline);
        controller = qdict_get_qdict(qdict_get_qdict(state, "controllers"),
                                      "dm-mc02.fdcan1");
        pending_frames = execution_can_uint(controller, "pending_frames");
        pending_bytes = execution_can_uint(controller, "pending_bytes");
        qobject_unref(state);
    } while (!pending_frames && submitted < 65536);
    g_assert_cmpuint(pending_frames, ==, 1);
    g_assert_cmpuint(pending_bytes, >, 0);
    g_assert_cmpuint(pending_bytes, <=, 84);
    g_test_message("Paused MMIO producer: %u records, pending bytes %" PRIu64,
                    submitted, pending_bytes);
    execution_can_check(qts, deadline);
    g_assert_true(qtest_qom_get_bool(qts, "/chardevs/can1", "connected"));
    execution_can_check(qts, deadline);
    execution_assert_stable(qts, 1000, counter);
    execution_can_check(qts, deadline);

    /* No producer/config/time writes after pressure. QEMU owns readiness. */
    deadline = g_get_monotonic_time() + 2 * G_TIME_SPAN_SECOND;
    do {
        ssize_t length;

        execution_can_check(qts, deadline);
        length = recv(cleanup.fd, buffer, sizeof(buffer), MSG_DONTWAIT);
        if (length < 0) {
            g_assert_true(errno == EAGAIN || errno == EINTR);
        } else {
            g_assert_cmpint(length, >, 0);
            for (unsigned used = 0; used < length;) {
                unsigned take = MIN(sizeof(partial) - partial_size,
                                    length - used);

                memcpy(partial + partial_size, buffer + used, take);
                used += take;
                partial_size += take;
                if (partial_size == sizeof(partial)) {
                    g_assert_cmpuint(received, <, submitted);
                    memcpy(expected, expected_wire, sizeof(expected));
                    expected[20] = received;
                    expected[21] = received >> 8;
                    g_assert_cmpmem(partial, sizeof(partial),
                                    expected, sizeof(expected));
                    received++;
                    partial_size = 0;
                }
            }
        }
        execution_can_check(qts, deadline);
        state = execution_can_state(qts, false, 1000, deadline);
        controller = qdict_get_qdict(qdict_get_qdict(state, "controllers"),
                                      "dm-mc02.fdcan1");
        pending_frames = execution_can_uint(controller, "pending_frames");
        pending_bytes = execution_can_uint(controller, "pending_bytes");
        qobject_unref(state);
        execution_assert_stable(qts, 1000, counter);
        execution_can_check(qts, deadline);
    } while (received < submitted || pending_frames);
    g_assert_cmpuint(partial_size, ==, 0);
    g_assert_cmpuint(pending_bytes, ==, 0);
    g_assert_true(qtest_qom_get_bool(qts, "/chardevs/can1", "connected"));
    execution_can_check(qts, deadline);
    do {
        execution_can_check(qts, deadline);
        extra = recv(cleanup.fd, buffer, sizeof(buffer), MSG_DONTWAIT);
    } while (extra < 0 && errno == EINTR);
    g_assert_cmpint(extra, ==, -1);
    g_assert_cmpint(errno, ==, EAGAIN);
    execution_can_check(qts, deadline);
    execution_assert_stable(qts, 1000, counter);
    execution_can_check(qts, deadline);

    /* Pending zero alone is not delivery; all-N reads prove this fixture. */
    g_assert_cmpint(close(cleanup.fd), ==, 0);
    cleanup.fd = -1;
    qtest_quit(qts);
    g_assert_false(g_file_test(path, G_FILE_TEST_EXISTS));
    g_assert_cmpint(g_rmdir(directory), ==, 0);
    qtest_remove_abrt_handler(&cleanup);
}

static void test_execution_preblocked_cpu_can(void)
{
    /* Slot0 prefix: ID0x200/DLC8, independent uint16 index and fixed marker. */
    uint8_t tx_ram[16] = {
        0x00, 0x00, 0x00, 0x08, 0x00, 0x00, 0x08, 0x00,
        0x00, 0x00, 0xa5, 0x5a,
    };
    /* Literal <IIB3xQ64s at0ns, including zero padding and payload tail. */
    static const uint8_t prefix_wire[84] = {
        [1] = 0x02, [8] = 0x08, [22] = 0xa5, [23] = 0x5a,
    };
    static const int64_t targets[] = {
        1000000, 2000000, 3000000, 3000200,
    };
    /* Original periodic guest/shift0 regression values, not Cortex-M cycles. */
    static const uint64_t cpu_timestamps[] = {
        1000003, 1000005, 2000003, 2000005, 3000003, 3000005,
    };
    uint8_t image[0x74] = { 0 };
    g_autofree char *directory = g_strdup("/tmp/dm-qemu.vtime-can.XXXXXX");
    g_autofree char *path = NULL;
    g_autofree char *quoted_path = NULL;
    g_autofree char *extra_args = NULL;
    ExecutionCanCleanup cleanup = { .fd = -1 };
    QTestState *qts;
    QDict *state, *controller;
    int64_t deadline;
    unsigned submitted = 0, received = 0, partial_size = 0;
    uint8_t buffer[4096], partial[84], expected[84];
    uint64_t pending_frames, pending_bytes, blocked_bytes, short_writes;
    uint64_t previous_timestamp = 0;
    ssize_t extra;

    execution_periodic_can_image(image);
    g_assert_nonnull(g_mkdtemp(directory));
    path = g_build_filename(directory, "can.sock", NULL);
    quoted_path = g_shell_quote(path);
    extra_args = g_strdup_printf(
        "-chardev socket,id=can1,path=%s,server=on,wait=off "
        "-serial none -serial none -serial none -serial none "
        "-serial none -serial none -serial none -serial chardev:can1",
        quoted_path);
    cleanup.path = path;
    cleanup.directory = directory;
    qtest_add_abrt_handler(execution_can_abort_cleanup, &cleanup);
    qts = execution_start_image(
        "dm-mc02,fdcan-host-ack=on,electrical-power=on",
        image, sizeof(image), extra_args);
    deadline = g_get_monotonic_time() + 2 * G_TIME_SPAN_SECOND;
    execution_can_check(qts, deadline);
    cleanup.fd = unix_connect(path, NULL);
    execution_can_check(qts, deadline);
    g_assert_cmpint(cleanup.fd, >=, 0);
    while (!qtest_qom_get_bool(qts, "/chardevs/can1", "connected")) {
        execution_can_check(qts, deadline);
    }
    execution_can_check(qts, deadline);

    qtest_writel(qts, EXECUTION_COUNTER, 0);
    /* Real PC15 output powers the rail before any host or CPU submission. */
    qtest_writel(qts, RCC_BASE + 0xe0,
                 qtest_readl(qts, RCC_BASE + 0xe0) | (1u << 2));
    qtest_writel(qts, 0x58020800,
                 (qtest_readl(qts, 0x58020800) & ~(3u << 30)) | (1u << 30));
    qtest_writel(qts, 0x58020818, 1u << 15);
    g_assert_cmphex(qtest_readl(qts, 0x58020814) & (1u << 15), ==, 1u << 15);
    g_assert_true(qtest_qom_get_bool(qts, "/machine", "system-5v-good"));
    execution_can_check(qts, deadline);
    /* TFQS2 from the outset; NDTB0/TFQM0, offset0, classic8-byte slots. */
    qtest_writel(qts, 0x4000a018, 1);
    qtest_writel(qts, 0x4000a0c0, 0x02000000);
    qtest_writel(qts, 0x4000a0c8, 0);
    qtest_memwrite(qts, 0x4000ac00, periodic_can_tx_ram,
                   sizeof(periodic_can_tx_ram));
    qtest_writel(qts, 0x4000a018, 0);
    g_assert_cmphex(qtest_readl(qts, 0x4000a054), ==, 0); /* IE */
    g_assert_cmphex(qtest_readl(qts, 0x4000a05c), ==, 0); /* ILE */
    /* Same integer125ns SysTick input and 1ms period as periodic-can. */
    qtest_writel(qts, RCC_BASE + 0x18, 0xau << 8);
    qtest_writel(qts, SYST_RVR, 7999);
    qtest_writel(qts, SYST_CVR, 0);
    qtest_writel(qts, SYST_CSR,
                 SYST_CSR_ENABLE | SYST_CSR_TICKINT | SYST_CSR_CLKSOURCE);
    execution_can_check(qts, deadline);
    execution_can_empty_readonly(qts, false, deadline);
    g_assert_cmphex(qtest_readl(qts, EXECUTION_COUNTER), ==, 0);
    execution_can_check(qts, deadline);

    /* No peer reads: stop at the first pending record, not a fixed OS count. */
    deadline = g_get_monotonic_time() + 2 * G_TIME_SPAN_SECOND;
    do {
        execution_can_check(qts, deadline);
        tx_ram[8] = submitted;
        tx_ram[9] = submitted >> 8;
        qtest_memwrite(qts, 0x4000ac00, tx_ram, sizeof(tx_ram));
        execution_can_check(qts, deadline);
        qtest_writel(qts, 0x4000a0d0, 1);
        execution_can_check(qts, deadline);
        submitted++;
        state = execution_can_state(qts, false, 0, deadline);
        controller = qdict_get_qdict(qdict_get_qdict(state, "controllers"),
                                      "dm-mc02.fdcan1");
        pending_frames = execution_can_uint(controller, "pending_frames");
        pending_bytes = execution_can_uint(controller, "pending_bytes");
        short_writes = execution_can_uint(controller, "tx_short_writes");
        qobject_unref(state);
    } while (!pending_frames && submitted < 65536);
    g_assert_cmpuint(pending_frames, ==, 1);
    g_assert_cmpuint(pending_bytes, >, 0);
    g_assert_cmpuint(pending_bytes, <=, 84);
    blocked_bytes = pending_bytes;
    g_test_message("Preblocked prefix: %u records, pending bytes %" PRIu64
                   ", short writes %" PRIu64,
                   submitted, blocked_bytes, short_writes);

    /* Restore only slot0; the queued prefix must retain its original bytes. */
    execution_can_check(qts, deadline);
    qtest_memwrite(qts, 0x4000ac00, periodic_can_tx_ram[0],
                   sizeof(periodic_can_tx_ram[0]));
    execution_can_check(qts, deadline);
    state = execution_can_state(qts, false, 0, deadline);
    controller = qdict_get_qdict(qdict_get_qdict(state, "controllers"),
                                  "dm-mc02.fdcan1");
    g_assert_cmpuint(execution_can_uint(controller, "pending_frames"), ==, 1);
    g_assert_cmpuint(execution_can_uint(controller, "pending_bytes"),
                     ==, blocked_bytes);
    qobject_unref(state);
    g_assert_cmphex(qtest_readl(qts, EXECUTION_COUNTER), ==, 0);
    execution_assert_stopped(qts, "prelaunch");
    execution_can_check(qts, deadline);

    for (unsigned pair = 0; pair < G_N_ELEMENTS(targets); pair++) {
        g_assert_true(qtest_probe_child(qts));
        execution_run_to(qts, targets[pair]);
        g_assert_true(qtest_probe_child(qts));
        deadline = g_get_monotonic_time() + 2 * G_TIME_SPAN_SECOND;
        execution_assert_stable(qts, targets[pair], pair);
        execution_can_check(qts, deadline);
        state = execution_can_state(qts, false, targets[pair], deadline);
        controller = qdict_get_qdict(qdict_get_qdict(state, "controllers"),
                                      "dm-mc02.fdcan1");
        g_assert_cmpuint(execution_can_uint(controller, "pending_frames"),
                         ==, 1 + 2 * pair);
        g_assert_cmpuint(execution_can_uint(controller, "pending_bytes"),
                         ==, blocked_bytes + 168 * pair);
        short_writes = execution_can_uint(controller, "tx_short_writes");
        g_test_message("CPU fence %" PRId64 " ns: ledger %u, pending %u, "
                       "bytes %" PRIu64 ", short writes %" PRIu64,
                       targets[pair], pair, 1 + 2 * pair,
                       blocked_bytes + 168 * pair, short_writes);
        qobject_unref(state);
        for (unsigned repeat = 0; repeat < 2; repeat++) {
            uint8_t ram[sizeof(periodic_can_tx_ram)];
            QDict *snapshot = execution_can_snapshot(qts, "reached",
                targets[pair], true, targets[pair], "paused");
            QDict *can = qdict_get_qdict(snapshot, "fdcan-tx-state");

            controller = qdict_get_qdict(qdict_get_qdict(can, "controllers"),
                                         "dm-mc02.fdcan1");
            g_assert_cmpuint(execution_can_uint(controller, "pending_frames"),
                             ==, 1 + 2 * pair);
            g_assert_cmpuint(execution_can_uint(controller, "pending_bytes"),
                             ==, blocked_bytes + 168 * pair);
            g_assert_cmpuint(execution_can_uint(controller, "tx_dropped"),
                             ==, 0);
            qobject_unref(snapshot);
            g_assert_cmphex(qtest_readl(qts, EXECUTION_COUNTER), ==, pair);
            g_assert_cmphex(qtest_readl(qts, 0x4000a054), ==, 0); /* IE */
            g_assert_cmphex(qtest_readl(qts, 0x4000a05c), ==, 0); /* ILE */
            g_assert_cmphex(qtest_readl(qts, 0x4000a0c0), ==, 0x02000000);
            qtest_memread(qts, 0x4000ac00, ram, sizeof(ram));
            g_assert_cmpmem(ram, sizeof(ram), periodic_can_tx_ram,
                            sizeof(periodic_can_tx_ram));
            execution_can_check(qts, deadline);
        }
        execution_can_check(qts, deadline);
    }
    g_assert_cmphex(qtest_readl(qts, SYST_CSR) &
                    (SYST_CSR_ENABLE | SYST_CSR_TICKINT | SYST_CSR_CLKSOURCE),
                    ==, 0);
    g_assert_cmphex(qtest_readl(qts, SCB_ICSR) &
                    (0x1ffu | SCB_ICSR_PENDSTSET), ==, 0);
    execution_assert_stable(qts, 3000200, 3);
    execution_can_check(qts, deadline);

    /* Producer is finished. Only observation and this sole peer may drain. */
    deadline = g_get_monotonic_time() + 2 * G_TIME_SPAN_SECOND;
    do {
        ssize_t length;

        execution_can_check(qts, deadline);
        length = recv(cleanup.fd, buffer, sizeof(buffer), MSG_DONTWAIT);
        if (length < 0) {
            g_assert_true(errno == EAGAIN || errno == EINTR);
        } else {
            g_assert_cmpint(length, >, 0);
            for (unsigned used = 0; used < length;) {
                unsigned take = MIN(sizeof(partial) - partial_size,
                                    length - used);

                memcpy(partial + partial_size, buffer + used, take);
                used += take;
                partial_size += take;
                if (partial_size == sizeof(partial)) {
                    g_assert_cmpuint(received, <, submitted + 6);
                    if (received < submitted) {
                        memcpy(expected, prefix_wire, sizeof(expected));
                        expected[20] = received;
                        expected[21] = received >> 8;
                    } else {
                        unsigned cpu_record = received - submitted;
                        unsigned pair = cpu_record / 2 + 1;
                        unsigned frame = cpu_record % 2;
                        uint64_t timestamp = ldq_le_p(partial + 12);

                        g_test_message("Preblocked CPU record %u timestamp %"
                                       PRIu64 " ns", cpu_record, timestamp);
                        g_assert_cmpuint(timestamp, >, previous_timestamp);
                        g_assert_cmpuint(timestamp, ==,
                                         cpu_timestamps[cpu_record]);
                        g_assert_cmpuint(timestamp, >=, pair * 1000000);
                        g_assert_cmpuint(timestamp, <=, pair * 1000000 + 17);
                        g_assert_cmpuint(timestamp, <=, 3000200);
                        previous_timestamp = timestamp;
                        memcpy(expected, periodic_can_expected_wire[frame],
                               sizeof(expected));
                        stq_le_p(expected + 12, cpu_timestamps[cpu_record]);
                    }
                    g_assert_cmpmem(partial, sizeof(partial),
                                    expected, sizeof(expected));
                    received++;
                    partial_size = 0;
                }
            }
        }
        execution_can_check(qts, deadline);
        state = execution_can_state(qts, false, 3000200, deadline);
        controller = qdict_get_qdict(qdict_get_qdict(state, "controllers"),
                                      "dm-mc02.fdcan1");
        pending_frames = execution_can_uint(controller, "pending_frames");
        pending_bytes = execution_can_uint(controller, "pending_bytes");
        short_writes = execution_can_uint(controller, "tx_short_writes");
        qobject_unref(state);
        execution_assert_stable(qts, 3000200, 3);
        execution_can_check(qts, deadline);
    } while (received < submitted + 6 || pending_frames);
    g_assert_cmpuint(received, ==, submitted + 6);
    g_assert_cmpuint(partial_size, ==, 0);
    g_assert_cmpuint(pending_frames, ==, 0);
    g_assert_cmpuint(pending_bytes, ==, 0);
    g_test_message("Preblocked peer: %u complete records, short writes %"
                   PRIu64, received, short_writes);
    g_assert_true(qtest_qom_get_bool(qts, "/chardevs/can1", "connected"));
    execution_can_check(qts, deadline);
    do {
        execution_can_check(qts, deadline);
        extra = recv(cleanup.fd, buffer, sizeof(buffer), MSG_DONTWAIT);
    } while (extra < 0 && errno == EINTR);
    g_assert_cmpint(extra, ==, -1);
    g_assert_cmpint(errno, ==, EAGAIN);
    execution_can_check(qts, deadline);
    execution_assert_stable(qts, 3000200, 3);
    execution_can_check(qts, deadline);

    /* Exact bytes and record count verify this finite peer stream. */
    g_assert_cmpint(close(cleanup.fd), ==, 0);
    cleanup.fd = -1;
    qtest_quit(qts);
    g_assert_false(g_file_test(path, G_FILE_TEST_EXISTS));
    g_assert_cmpint(g_rmdir(directory), ==, 0);
    qtest_remove_abrt_handler(&cleanup);
}
#endif

static bool qom_list_has_child(QTestState *qts, const char *name,
                               const char *type)
{
    QDict *response;
    QList *properties;
    QListEntry *entry;
    bool found = false;

    response = qtest_qmp(
        qts, "{ 'execute': 'qom-list', 'arguments': { 'path': %s } }",
        "/machine/armv7m");
    g_assert_nonnull(response);
    properties = qdict_get_qlist(response, "return");
    g_assert_nonnull(properties);

    QLIST_FOREACH_ENTRY(properties, entry) {
        QDict *property = qobject_to(QDict, qlist_entry_obj(entry));
        const char *property_name = qdict_get_str(property, "name");
        const char *property_type = qdict_get_str(property, "type");

        if (!strcmp(property_name, name)) {
            g_assert_cmpstr(property_type, ==, type);
            found = true;
            break;
        }
    }

    qobject_unref(response);
    return found;
}

static unsigned qom_list_count_external_irq_inputs(QTestState *qts)
{
    QDict *response;
    QList *properties;
    QListEntry *entry;
    unsigned count = 0;

    response = qtest_qmp(
        qts, "{ 'execute': 'qom-list', 'arguments': { 'path': %s } }",
        "/machine/armv7m");
    g_assert_nonnull(response);
    g_assert_true(qdict_haskey(response, "return"));
    properties = qdict_get_qlist(response, "return");
    g_assert_nonnull(properties);

    QLIST_FOREACH_ENTRY(properties, entry) {
        QDict *property = qobject_to(QDict, qlist_entry_obj(entry));
        const char *name = qdict_get_str(property, "name");

        if (g_str_has_prefix(name, "unnamed-gpio-in[")) {
            count++;
        }
    }

    qobject_unref(response);
    return count;
}

static uint64_t qom_get_uint(QTestState *qts, const char *path,
                             const char *property)
{
    QDict *response;
    QNum *value;
    uint64_t result;

    response = qtest_qmp(
        qts, "{ 'execute': 'qom-get', 'arguments': { 'path': %s, "
        "'property': %s } }", path, property);
    g_assert_nonnull(response);
    value = qobject_to(QNum, qdict_get(response, "return"));
    g_assert_nonnull(value);
    g_assert_true(qnum_get_try_uint(value, &result));
    qobject_unref(response);
    return result;
}

static void test_armv7m_children_and_irq_width(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    /* These are QEMU-owned children.  Their DeviceClass VMState is registered
     * when each child is realized; the board does not duplicate their state. */
    g_assert_true(qom_list_has_child(qts, "cpu", "child<cortex-m7-arm-cpu>"));
    g_assert_true(qom_list_has_child(qts, "nvic", "child<armv7m_nvic>"));
    g_assert_true(qom_list_has_child(qts, "systick-reg-ns",
                                     "child<armv7m_systick>"));
    /* The alias exposes NVIC's complete vector count, including the 16
     * architecturally internal exceptions.  The board-facing input count is
     * checked separately from the QOM GPIO list. */
    g_assert_cmpuint(qom_get_uint(qts, "/machine/armv7m", "num-irq"),
                     ==, 179);
    g_assert_cmpuint(qom_list_count_external_irq_inputs(qts), ==, 163);
    g_assert_cmpuint(qom_get_uint(qts, "/machine/armv7m/nvic", "num-irq"),
                     ==, 179);

    qtest_quit(qts);
}

static void test_external_irq_and_systick_virtual_time(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    uint32_t irq_mask = UINT32_C(1) << TEST_EXTERNAL_IRQ;

    /* Observe the board-facing input GPIO and verify its NVIC-visible state.
     * The CPU exception output is a SysBus IRQ rather than a QOM GPIO-out,
     * so this qtest observes it through the architected pending register. */
    qtest_irq_intercept_in(qts, "/machine/armv7m");

    qtest_writeb(qts, NVIC_IPR0 + TEST_EXTERNAL_IRQ, 0x20);
    g_assert_cmphex(qtest_readb(qts, NVIC_IPR0 + TEST_EXTERNAL_IRQ), ==, 0x20);
    qtest_writel(qts, NVIC_ISER0, irq_mask);
    qtest_set_irq_in(qts, "/machine/armv7m", NULL, TEST_EXTERNAL_IRQ, 1);
    g_assert_true(qtest_get_irq(qts, TEST_EXTERNAL_IRQ));
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR0) & irq_mask, ==, irq_mask);

    qtest_set_irq_in(qts, "/machine/armv7m", NULL, TEST_EXTERNAL_IRQ, 0);
    g_assert_false(qtest_get_irq(qts, TEST_EXTERNAL_IRQ));
    qtest_writel(qts, NVIC_ICPR0, irq_mask);
    qtest_writel(qts, NVIC_ICER0, irq_mask);
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR0) & irq_mask, ==, 0);

    /* The DM-MC02 CPU clock is 64 MHz at reset.  A reload value of 63
     * therefore expires after 1 us when the SysTick CPU clock is selected. */
    qtest_writel(qts, SYST_RVR, 63);
    qtest_writel(qts, SYST_CVR, 0);
    qtest_writel(qts, SYST_CSR,
                 SYST_CSR_ENABLE | SYST_CSR_TICKINT | SYST_CSR_CLKSOURCE);
    qtest_clock_step(qts, 1000);
    g_assert_true(qtest_readl(qts, SYST_CSR) & SYST_CSR_COUNTFLAG);
    /* SysTick is an internal exception; its pending state is exposed by
     * SCB->ICSR.PENDSTSET, not the external ISPR bank. */
    g_assert_true(qtest_readl(qts, SCB_ICSR) & SCB_ICSR_PENDSTSET);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, SYST_CSR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, NVIC_ISER0), ==, 0);
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR0), ==, 0);

    qtest_quit(qts);
}

static void test_d1_clock_chain(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    g_assert_cmpuint(qom_get_uint(qts, "/machine",
                        "usart16-kernel-clock-hz"), ==, 64000000);
    g_assert_cmpuint(qom_get_uint(qts, "/machine",
                        "usart234578-kernel-clock-hz"), ==, 64000000);
    /* ST CMSIS SystemCoreClockUpdate and LL_RCC_CALC_PCLK{1,2}_FREQ:
     * HSE 24 MHz / 2 * 40 = SYSCLK 480 MHz; D1CPRE /8, HPRE /2
     * give CPU 60 MHz, HCLK 30 MHz, PCLK1 15 MHz, PCLK2 7.5 MHz. */
    qtest_writel(qts, RCC_BASE + 0x28, 2u | (2u << 4));
    qtest_writel(qts, RCC_BASE + 0x30, 39u);
    qtest_writel(qts, RCC_BASE + 0x2c, 1u << 16); /* PLL1 DIVP enable. */
    qtest_writel(qts, RCC_BASE, 1u | (1u << 16) | (1u << 24));
    qtest_writel(qts, RCC_BASE + 0x10, 3u);
    qtest_writel(qts, RCC_BASE + 0x18, (0xau << 8) | 8u);
    qtest_writel(qts, RCC_BASE + 0x1c, (4u << 4) | (5u << 8));
    qtest_writel(qts, RCC_BASE + 0x54, 0); /* USART selects PCLK. */
    g_assert_cmpuint(qom_get_uint(qts, "/machine",
                        "usart16-kernel-clock-hz"), ==, 7500000);
    g_assert_cmpuint(qom_get_uint(qts, "/machine",
                        "usart234578-kernel-clock-hz"), ==, 15000000);
    g_assert_cmpuint(qom_get_uint(qts, "/machine",
                        "apb1-timer-clock-hz"), ==, 30000000);
    g_assert_cmpuint(qom_get_uint(qts, "/machine",
                        "apb2-timer-clock-hz"), ==, 15000000);
    qtest_writel(qts, SYST_RVR, 59);
    qtest_writel(qts, SYST_CVR, 0);
    qtest_writel(qts, SYST_CSR, SYST_CSR_ENABLE | SYST_CSR_CLKSOURCE);
    qtest_clock_step(qts, 1000);
    g_assert_true(qtest_readl(qts, SYST_CSR) & SYST_CSR_COUNTFLAG);
    qtest_quit(qts);
}

static void test_gpio_exti_reaches_native_nvic(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");
    uint32_t line_mask = UINT32_C(1) << 15;
    uint32_t irq_mask = UINT32_C(1) << (TEST_EXTI_IRQ % 32);

    qtest_irq_intercept_in(qts, "/machine/armv7m");

    /* EXTI15_10 is wired by the board profile to native NVIC input 40.
     * PA15 starts high (released user key), so driving the board input low
     * must create a falling-edge pending interrupt. */
    qtest_writel(qts, EXTI_FTSR1, line_mask);
    qtest_qmp_assert_success(
        qts, "{ 'execute': 'qom-set', 'arguments': { 'path': '/machine', "
        "'property': 'gpio-input', 'value': 'A15=0' } }");

    /* ST CMSIS/HAL PR1 is at 0x88. An edge latches while IMR1 masks IRQ. */
    g_assert_cmphex(qtest_readl(qts, EXTI_PR1), ==, line_mask);
    g_assert_false(qtest_get_irq(qts, TEST_EXTI_IRQ));
    qtest_writel(qts, EXTI_C1IMR1, line_mask);
    g_assert_true(qtest_get_irq(qts, TEST_EXTI_IRQ));
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR1) & irq_mask, ==, irq_mask);

    qtest_writel(qts, EXTI_D3PCR1H, line_mask);
    g_assert_cmphex(qtest_readl(qts, EXTI_PR1), ==, line_mask);
    g_assert_true(qtest_get_irq(qts, TEST_EXTI_IRQ));
    qtest_writel(qts, EXTI_C1IMR1, 0);
    g_assert_false(qtest_get_irq(qts, TEST_EXTI_IRQ));
    g_assert_cmphex(qtest_readl(qts, EXTI_PR1), ==, line_mask);
    qtest_writel(qts, EXTI_C1IMR1, line_mask);
    g_assert_true(qtest_get_irq(qts, TEST_EXTI_IRQ));
    qtest_writel(qts, EXTI_PR1, line_mask);
    g_assert_false(qtest_get_irq(qts, TEST_EXTI_IRQ));
    g_assert_cmphex(qtest_readl(qts, EXTI_PR1), ==, 0);
    /* Clearing the EXTI source deasserts the level, while NVIC pending is
     * architecturally cleared by ICPR. */
    qtest_writel(qts, NVIC_ICPR1, irq_mask);
    g_assert_cmphex(qtest_readl(qts, NVIC_ISPR1) & irq_mask, ==, 0);
    qtest_quit(qts);
}

static void test_sysresetreq_latches_software_reset_reason(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_SFTRSTF, ==, 0);
    /* CMSIS NVIC_SystemReset requests reset through AIRCR; native SysTick
     * must reset as well as recording the H723 RCC_RSR source. */
    qtest_writel(qts, SYST_RVR, 0x1234);
    qtest_writel(qts, SCB_AIRCR,
                 SCB_AIRCR_VECTKEY | SCB_AIRCR_SYSRESETREQ);
    /* The qtest MMIO command queues the reset request; a zero virtual-clock
     * step lets the normal runstate path consume it without depending on a
     * QMP event ordering detail. */
    qtest_clock_step(qts, 0);
    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_SFTRSTF, ==,
                    RCC_RSR_SFTRSTF);

    /* A subsequent ordinary reset keeps the source until firmware clears it. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_SFTRSTF, ==,
                    RCC_RSR_SFTRSTF);
    qtest_writel(qts, RCC_RSR, RCC_RSR_RMVF);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_SFTRSTF, ==, 0);
    qtest_quit(qts);
}

static void test_power_on_reset_latches_por_reason(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PORRSTF, ==,
                    RCC_RSR_PORRSTF);

    /* A normal system reset is not another power-on event. */
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PORRSTF, ==,
                    RCC_RSR_PORRSTF);

    qtest_writel(qts, RCC_RSR, RCC_RSR_RMVF);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PORRSTF, ==, 0);
    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PORRSTF, ==, 0);
    qtest_quit(qts);
}

static void test_power_brownout_latches_bor_reason(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02,vin-mv=11999");

    /* A low startup input is not a runtime BOR event. */
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_BORRSTF, ==, 0);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PORRSTF, ==,
                    RCC_RSR_PORRSTF);

    qtest_qmp_assert_success(
        qts, "{ 'execute': 'qom-set', 'arguments': { 'path': '/machine', "
        "'property': 'vin-mv', 'value': '24000' } }");
    qtest_writel(qts, SYST_RVR, 0x1234);
    qtest_qmp_assert_success(
        qts, "{ 'execute': 'qom-set', 'arguments': { 'path': '/machine', "
        "'property': 'vin-mv', 'value': '11999' } }");
    /* Let the queued QEMU reset request complete. */
    qtest_clock_step(qts, 0);
    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_BORRSTF, ==,
                    RCC_RSR_BORRSTF);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_BORRSTF, ==,
                    RCC_RSR_BORRSTF);
    qtest_writel(qts, RCC_RSR, RCC_RSR_RMVF);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_BORRSTF, ==, 0);
    qtest_quit(qts);
}

static void test_external_nrst_latches_pin_reason(void)
{
    QTestState *qts = qtest_init("-machine dm-mc02");

    /* POR is unrelated to an external pin reset; clear it so the assertion
     * below identifies only the pin producer. */
    qtest_writel(qts, RCC_RSR, RCC_RSR_RMVF);
    qtest_writel(qts, SYST_RVR, 0x1234);
    qtest_set_irq_in(qts, "/machine/reset-input", "NRST", 0, 1);
    qtest_set_irq_in(qts, "/machine/reset-input", "NRST", 0, 0);
    qtest_clock_step(qts, 0);

    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PINRSTF, ==,
                    RCC_RSR_PINRSTF);

    /* The active-low level is not edge-triggered repeatedly. */
    qtest_writel(qts, SYST_RVR, 0x5678);
    qtest_set_irq_in(qts, "/machine/reset-input", "NRST", 0, 0);
    qtest_clock_step(qts, 0);
    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0x5678);

    /* Releasing and asserting NRST creates a second, real pin event. */
    qtest_set_irq_in(qts, "/machine/reset-input", "NRST", 0, 1);
    qtest_set_irq_in(qts, "/machine/reset-input", "NRST", 0, 0);
    qtest_clock_step(qts, 0);
    g_assert_cmphex(qtest_readl(qts, SYST_RVR), ==, 0);

    qtest_qmp_assert_success(qts, "{ 'execute': 'system_reset' }");
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PINRSTF, ==,
                    RCC_RSR_PINRSTF);
    qtest_writel(qts, RCC_RSR, RCC_RSR_RMVF);
    g_assert_cmphex(qtest_readl(qts, RCC_RSR) & RCC_RSR_PINRSTF, ==, 0);
    qtest_quit(qts);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-mc02/virtual-execution/reject-profiles",
                    test_execution_reject_profiles);
    g_test_add_func("/dm-mc02/virtual-execution/reject-targets",
                    test_execution_reject_targets);
    g_test_add_func("/dm-mc02/virtual-execution/busy-guest",
                    test_execution_busy_guest);
    g_test_add_func("/dm-mc02/virtual-execution/can-snapshot-compat",
                    test_execution_can_snapshot_compat);
    g_test_add_func("/dm-mc02/virtual-execution/wfi-systick",
                    test_execution_wfi_systick);
    g_test_add_func("/dm-mc02/virtual-execution/cancel-reentry",
                    test_execution_cancel_and_reentry);
#ifndef _WIN32
    g_test_add_func("/dm-mc02/virtual-execution/requester-disconnect",
                    test_execution_requester_disconnect);
#endif
#ifdef CONFIG_LINUX
    g_test_add_func("/dm-mc02/virtual-execution/periodic-can",
                    test_execution_periodic_can);
    g_test_add_func("/dm-mc02/virtual-execution/paused-can-drain",
                    test_execution_paused_can_drain);
    g_test_add_func("/dm-mc02/virtual-execution/preblocked-cpu-can",
                    test_execution_preblocked_cpu_can);
    g_test_add_func("/dm-mc02/virtual-execution/paused-can-receipt",
                    test_execution_paused_can_receipt);
#endif
    g_test_add_func("/dm-mc02/armv7m/qom-children-and-irq-width",
                    test_armv7m_children_and_irq_width);
    g_test_add_func("/dm-mc02/armv7m/external-irq-and-systick-time",
                    test_external_irq_and_systick_virtual_time);
    g_test_add_func("/dm-mc02/armv7m/d1-clock-chain", test_d1_clock_chain);
    g_test_add_func("/dm-mc02/armv7m/gpio-exti-reaches-native-nvic",
                    test_gpio_exti_reaches_native_nvic);
    g_test_add_func("/dm-mc02/armv7m/sysresetreq-software-reset-reason",
                    test_sysresetreq_latches_software_reset_reason);
    g_test_add_func("/dm-mc02/armv7m/power-on-reset-reason",
                    test_power_on_reset_latches_por_reason);
    g_test_add_func("/dm-mc02/armv7m/power-brownout-reset-reason",
                    test_power_brownout_latches_bor_reason);
    g_test_add_func("/dm-mc02/armv7m/external-nrst-pin-reset-reason",
                    test_external_nrst_latches_pin_reason);
    return g_test_run();
}
