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
    g_autofree char *path = NULL;
    g_autofree char *quoted_path = NULL;
    QTestState *qts;
    int fd;

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
    fd = g_file_open_tmp("dm-qemu.vtime-XXXXXX", &path, NULL);
    g_assert_cmpint(fd, >=, 0);
    g_assert_cmpint(write(fd, image, sizeof(image)), ==, sizeof(image));
    g_assert_cmpint(close(fd), ==, 0);
    quoted_path = g_shell_quote(path);
    qts = qtest_initf("-machine %s -nodefaults -accel tcg,thread=single "
                      "-S -icount " EXECUTION_ICOUNT " -kernel %s %s",
                      machine, quoted_path, extra_args ?: "");
    g_assert_cmpint(g_unlink(path), ==, 0);
    return qts;
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
    execution_run_to(qts, current + 1003);
    execution_assert_migration_blocker(qts, false);
    /* Refusing a stale target preserves the latest successful result. */
    qobject_unref(qtest_qmp_assert_failure_ref(qts,
        "{'execute':'x-run-to-virtual-time','arguments':{'target-ns':%"
        PRId64 "}}", current + 1003));
    execution_assert_state(qts, "reached", true, current + 1003);
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
    execution_run_to(qts, current + 1003);
    qtest_qmp_assert_success(qts, "{'execute':'system_reset'}");
    execution_assert_state(qts, "cancelled", true, current + 1003);
    /* Native reset of a stopped VM returns to PRELAUNCH, not PAUSED. */
    execution_assert_stopped(qts, "prelaunch");
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
    g_test_add_func("/dm-mc02/virtual-execution/wfi-systick",
                    test_execution_wfi_systick);
    g_test_add_func("/dm-mc02/virtual-execution/cancel-reentry",
                    test_execution_cancel_and_reentry);
#ifndef _WIN32
    g_test_add_func("/dm-mc02/virtual-execution/requester-disconnect",
                    test_execution_requester_disconnect);
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
