/* Focused VMState contract test for the board-independent WWDG component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_wwdg.h"
#include "migration/migration.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

extern unsigned dm_mc02_wwdg_sync_calls;

static int temp_fd;

static QEMUFile *open_test_file(bool write)
{
    int fd = dup(temp_fd);
    QIOChannel *ioc;
    QEMUFile *file;

    g_assert_cmpint(fd, >=, 0);
    g_assert_cmpint(lseek(fd, 0, SEEK_SET), ==, 0);
    if (write) {
        g_assert_cmpint(ftruncate(fd, 0), ==, 0);
    }
    ioc = QIO_CHANNEL(qio_channel_file_new_fd(fd));
    file = write ? qemu_file_new_output(ioc) : qemu_file_new_input(ioc);
    object_unref(OBJECT(ioc));
    return file;
}

static void save_state(const DmMc02Wwdg *state,
                       const VMStateDescription *description)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, description, (void *)state,
                                       NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Wwdg *state,
                      const VMStateDescription *description)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, description, state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void prepare_state(DmMc02Wwdg *state)
{
    memset(state, 0, sizeof(*state));
    state->clock_hz = UINT64_C(64000000);
    state->regs[DM_MC02_WWDG_CR_OFFSET / sizeof(uint32_t)] =
        DM_MC02_WWDG_CR_WDGA | UINT32_C(0x75);
    state->regs[DM_MC02_WWDG_CFR_OFFSET / sizeof(uint32_t)] =
        UINT32_C(0x65) | DM_MC02_WWDG_CFR_EWI |
        (UINT32_C(2) << DM_MC02_WWDG_CFR_WDGTB_SHIFT);
    state->regs[DM_MC02_WWDG_SR_OFFSET / sizeof(uint32_t)] =
        DM_MC02_WWDG_SR_EWIF;
    state->counter = UINT32_C(0x55);
    state->counter_start_ns = UINT64_C(1000);
    state->next_event_ns = UINT64_C(2000);
    state->started = true;
    state->start_count = UINT64_C(11);
    state->reload_count = UINT64_C(22);
    state->ewi_count = UINT64_C(33);
    state->timeout_count = UINT64_C(44);
    state->window_violation_count = UINT64_C(55);
}

static void test_round_trip_rebuilds_runtime(void)
{
    DmMc02Wwdg source;
    DmMc02Wwdg restored;

    prepare_state(&source);
    save_state(&source, dm_mc02_wwdg_vmstate());
    memset(&restored, 0, sizeof(restored));
    restored.clock_hz = source.clock_hz;
    dm_mc02_wwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, dm_mc02_wwdg_vmstate()), ==, 0);
    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_cmpuint(restored.counter, ==, source.counter);
    g_assert_cmpuint(restored.counter_start_ns, ==, source.counter_start_ns);
    g_assert_cmpuint(restored.next_event_ns, ==, source.next_event_ns);
    g_assert_true(restored.started);
    g_assert_false(restored.reset_stage);
    g_assert_cmpuint(restored.start_count, ==, source.start_count);
    g_assert_cmpuint(restored.reload_count, ==, source.reload_count);
    g_assert_cmpuint(restored.ewi_count, ==, source.ewi_count);
    g_assert_cmpuint(restored.timeout_count, ==, source.timeout_count);
    g_assert_cmpuint(restored.window_violation_count, ==,
                     source.window_violation_count);
    g_assert_nonnull(restored.timer);
    g_assert_cmpuint(dm_mc02_wwdg_sync_calls, ==, 1);
}

static void test_round_trip_preserves_reset_stage(void)
{
    DmMc02Wwdg source;
    DmMc02Wwdg restored;

    prepare_state(&source);
    source.counter = DM_MC02_WWDG_COUNTER_EWI;
    source.counter_start_ns = UINT64_C(3000);
    source.next_event_ns = UINT64_C(4000);
    source.reset_stage = true;
    save_state(&source, dm_mc02_wwdg_vmstate());
    memset(&restored, 0, sizeof(restored));
    restored.clock_hz = source.clock_hz;
    dm_mc02_wwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, dm_mc02_wwdg_vmstate()), ==, 0);
    g_assert_true(restored.started);
    g_assert_true(restored.reset_stage);
    g_assert_cmpuint(restored.counter, ==, DM_MC02_WWDG_COUNTER_EWI);
    g_assert_cmpuint(dm_mc02_wwdg_sync_calls, ==, 1);
}

static void test_raw_load_has_no_runtime_projection(void)
{
    DmMc02Wwdg source;
    DmMc02Wwdg restored;

    prepare_state(&source);
    save_state(&source, dm_mc02_wwdg_vmstate_raw());
    memset(&restored, 0, sizeof(restored));
    restored.clock_hz = source.clock_hz;
    dm_mc02_wwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, dm_mc02_wwdg_vmstate_raw()), ==, 0);
    g_assert_cmpuint(restored.counter, ==, source.counter);
    g_assert_null(restored.timer);
    g_assert_cmpuint(dm_mc02_wwdg_sync_calls, ==, 0);
}

static void test_rejects_deadline_without_started_state(void)
{
    DmMc02Wwdg source;
    DmMc02Wwdg restored;

    prepare_state(&source);
    source.started = false;
    save_state(&source, dm_mc02_wwdg_vmstate());
    memset(&restored, 0, sizeof(restored));
    restored.clock_hz = source.clock_hz;
    dm_mc02_wwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, dm_mc02_wwdg_vmstate()), !=, 0);
    g_assert_cmpuint(dm_mc02_wwdg_sync_calls, ==, 0);
}

static void test_rejects_inconsistent_reset_stage(void)
{
    DmMc02Wwdg source;
    DmMc02Wwdg restored;

    prepare_state(&source);
    source.reset_stage = true;
    source.counter = UINT32_C(0x41);
    save_state(&source, dm_mc02_wwdg_vmstate());
    memset(&restored, 0, sizeof(restored));
    restored.clock_hz = source.clock_hz;
    dm_mc02_wwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, dm_mc02_wwdg_vmstate()), !=, 0);
    g_assert_cmpuint(dm_mc02_wwdg_sync_calls, ==, 0);
}

static void test_rejects_destination_without_clock(void)
{
    DmMc02Wwdg source;
    DmMc02Wwdg restored;

    prepare_state(&source);
    save_state(&source, dm_mc02_wwdg_vmstate());
    memset(&restored, 0, sizeof(restored));
    dm_mc02_wwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, dm_mc02_wwdg_vmstate()), !=, 0);
    g_assert_cmpuint(dm_mc02_wwdg_sync_calls, ==, 0);
}

static void test_rejects_reserved_register_bits(void)
{
    DmMc02Wwdg source;
    DmMc02Wwdg restored;

    prepare_state(&source);
    source.regs[DM_MC02_WWDG_CFR_OFFSET / sizeof(uint32_t)] |= 1u << 10;
    save_state(&source, dm_mc02_wwdg_vmstate());
    memset(&restored, 0, sizeof(restored));
    restored.clock_hz = source.clock_hz;
    dm_mc02_wwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, dm_mc02_wwdg_vmstate()), !=, 0);
    g_assert_cmpuint(dm_mc02_wwdg_sync_calls, ==, 0);
}

static void test_rejects_truncated_state(void)
{
    DmMc02Wwdg state;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    state.clock_hz = UINT64_C(64000000);
    dm_mc02_wwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&state, dm_mc02_wwdg_vmstate()), !=, 0);
    g_assert_cmpuint(dm_mc02_wwdg_sync_calls, ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-wwdg-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-wwdg-vmstate/round-trip",
                    test_round_trip_rebuilds_runtime);
    g_test_add_func("/dm-wwdg-vmstate/reset-stage",
                    test_round_trip_preserves_reset_stage);
    g_test_add_func("/dm-wwdg-vmstate/raw-no-projection",
                    test_raw_load_has_no_runtime_projection);
    g_test_add_func("/dm-wwdg-vmstate/reject-deadline",
                    test_rejects_deadline_without_started_state);
    g_test_add_func("/dm-wwdg-vmstate/reject-reset-stage",
                    test_rejects_inconsistent_reset_stage);
    g_test_add_func("/dm-wwdg-vmstate/reject-no-clock",
                    test_rejects_destination_without_clock);
    g_test_add_func("/dm-wwdg-vmstate/reject-reserved",
                    test_rejects_reserved_register_bits);
    g_test_add_func("/dm-wwdg-vmstate/reject-truncated",
                    test_rejects_truncated_state);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
