/* Focused VMState contract test for the board-independent IWDG component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_iwdg.h"
#include "migration/migration.h"
#include "migration/savevm.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

extern unsigned dm_mc02_iwdg_sync_calls;

static int temp_fd;

/* Serialized shape from the component's version-1 contract.  Keep this
 * fixture local: production loading must go through the current description. */
static const VMStateDescription legacy_iwdg_vmstate_v1 = {
    .name = "dm-mc02-iwdg-v1-fixture",
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, DmMc02Iwdg,
                             DM_MC02_IWDG_REGION_SIZE / sizeof(uint32_t)),
        VMSTATE_BOOL(boot_grace_pending, DmMc02Iwdg),
        VMSTATE_BOOL(write_unlocked, DmMc02Iwdg),
        VMSTATE_BOOL(started, DmMc02Iwdg),
        VMSTATE_UINT64(next_timeout_ns, DmMc02Iwdg),
        VMSTATE_END_OF_LIST()
    },
};

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

static void save_state(const DmMc02Iwdg *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_iwdg_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static void save_legacy_v1_state(const DmMc02Iwdg *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, &legacy_iwdg_vmstate_v1,
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Iwdg *state, int version_id)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_iwdg_vmstate(), state,
                                 version_id);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void prepare_source(DmMc02Iwdg *state)
{
    memset(state, 0, sizeof(*state));
    for (unsigned i = 0; i < ARRAY_SIZE(state->regs); ++i) {
        state->regs[i] = UINT32_C(0x10203040) ^ i * UINT32_C(0x01010101);
    }
    state->regs[0x04 / sizeof(uint32_t)] = 6;
    state->regs[0x08 / sizeof(uint32_t)] = 31;
    state->regs[0x0c / sizeof(uint32_t)] = 0;
    state->regs[0x10 / sizeof(uint32_t)] = 0xfff;
    state->boot_grace_pending = true;
    state->write_unlocked = true;
    state->started = true;
    state->next_timeout_ns = 5000;
}

static void prepare_pending_source(DmMc02Iwdg *state)
{
    prepare_source(state);
    state->regs[0x0c / sizeof(uint32_t)] =
        DM_MC02_IWDG_SR_PVU | DM_MC02_IWDG_SR_WVU;
    state->pending_pr = 5;
    state->pending_winr = 30;
    state->update_deadline_ns[0] = 4000;
    state->update_deadline_ns[2] = 4500;
}

static void test_round_trip_restores_scheduler_state(void)
{
    DmMc02Iwdg source;
    DmMc02Iwdg restored;

    prepare_source(&source);
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    restored.lsi_hz = 32000;
    restored.boot_grace_ns = 123000000;
    restored.timeout_timer = NULL;
    dm_mc02_iwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, 2), ==, 0);
    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_true(restored.boot_grace_pending);
    g_assert_true(restored.write_unlocked);
    g_assert_true(restored.started);
    g_assert_cmpuint(restored.next_timeout_ns, ==, source.next_timeout_ns);
    g_assert_cmpuint(restored.lsi_hz, ==, 32000);
    g_assert_cmpuint(restored.boot_grace_ns, ==, 123000000);
    g_assert_nonnull(restored.timeout_timer);
    g_assert_cmpuint(dm_mc02_iwdg_sync_calls, ==, 1);
}

static void test_rejects_deadline_without_started_state(void)
{
    DmMc02Iwdg source;
    DmMc02Iwdg restored;

    prepare_source(&source);
    source.started = false;
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    dm_mc02_iwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, 2), !=, 0);
    g_assert_cmpuint(dm_mc02_iwdg_sync_calls, ==, 0);
}

static void test_loads_v1_without_deferred_update_state(void)
{
    DmMc02Iwdg source;
    DmMc02Iwdg restored;

    prepare_source(&source);
    save_legacy_v1_state(&source);
    memset(&restored, 0, sizeof(restored));
    restored.pending_pr = 6;
    restored.pending_rlr = 31;
    restored.pending_winr = 30;
    restored.update_deadline_ns[0] = 100;
    restored.update_deadline_ns[1] = 200;
    restored.update_deadline_ns[2] = 300;
    dm_mc02_iwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, 1), ==, 0);
    g_assert_cmphex(restored.regs[0x0c / sizeof(uint32_t)], ==, 0);
    g_assert_cmpuint(restored.pending_pr, ==, 0);
    g_assert_cmpuint(restored.pending_rlr, ==, 0);
    g_assert_cmpuint(restored.pending_winr, ==, 0);
    g_assert_cmpuint(restored.update_deadline_ns[0], ==, 0);
    g_assert_cmpuint(restored.update_deadline_ns[1], ==, 0);
    g_assert_cmpuint(restored.update_deadline_ns[2], ==, 0);
    g_assert_cmpuint(dm_mc02_iwdg_sync_calls, ==, 1);
}

static void test_round_trip_restores_pending_update_state(void)
{
    DmMc02Iwdg source;
    DmMc02Iwdg restored;

    prepare_pending_source(&source);
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    dm_mc02_iwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, 2), ==, 0);
    g_assert_cmphex(restored.regs[0x0c / sizeof(uint32_t)], ==,
                    DM_MC02_IWDG_SR_PVU | DM_MC02_IWDG_SR_WVU);
    g_assert_cmpuint(restored.pending_pr, ==, 5);
    g_assert_cmpuint(restored.pending_rlr, ==, 0);
    g_assert_cmpuint(restored.pending_winr, ==, 30);
    g_assert_cmpuint(restored.update_deadline_ns[0], ==, 4000);
    g_assert_cmpuint(restored.update_deadline_ns[1], ==, 0);
    g_assert_cmpuint(restored.update_deadline_ns[2], ==, 4500);
    g_assert_cmpuint(dm_mc02_iwdg_sync_calls, ==, 1);
}

static void test_rejects_pending_update_without_deadline(void)
{
    DmMc02Iwdg source;
    DmMc02Iwdg restored;

    prepare_pending_source(&source);
    source.update_deadline_ns[0] = 0;
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    dm_mc02_iwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, 2), !=, 0);
    g_assert_cmpuint(dm_mc02_iwdg_sync_calls, ==, 0);
}

static void test_rejects_reserved_prescaler_without_sync(void)
{
    DmMc02Iwdg source;
    DmMc02Iwdg restored;

    prepare_source(&source);
    source.regs[0x04 / sizeof(uint32_t)] = 7;
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    dm_mc02_iwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, 2), !=, 0);
    g_assert_cmpuint(dm_mc02_iwdg_sync_calls, ==, 0);
}

static void test_rejects_invalid_reload_without_sync(void)
{
    DmMc02Iwdg source;
    DmMc02Iwdg restored;

    prepare_source(&source);
    source.regs[0x08 / sizeof(uint32_t)] = 0x1000;
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    dm_mc02_iwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, 2), !=, 0);
    g_assert_cmpuint(dm_mc02_iwdg_sync_calls, ==, 0);
}

static void test_rejects_invalid_window_without_sync(void)
{
    DmMc02Iwdg source;
    DmMc02Iwdg restored;

    prepare_source(&source);
    source.regs[0x10 / sizeof(uint32_t)] = 0x1000;
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    dm_mc02_iwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&restored, 2), !=, 0);
    g_assert_cmpuint(dm_mc02_iwdg_sync_calls, ==, 0);
}

static void test_rejects_truncated_state_without_sync(void)
{
    DmMc02Iwdg state;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    dm_mc02_iwdg_sync_calls = 0;

    g_assert_cmpint(load_state(&state, 2), !=, 0);
    g_assert_cmpuint(dm_mc02_iwdg_sync_calls, ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-iwdg-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-iwdg-vmstate/round-trip",
                    test_round_trip_restores_scheduler_state);
    g_test_add_func("/dm-iwdg-vmstate/reject-deadline",
                    test_rejects_deadline_without_started_state);
    g_test_add_func("/dm-iwdg-vmstate/load-v1",
                    test_loads_v1_without_deferred_update_state);
    g_test_add_func("/dm-iwdg-vmstate/pending-round-trip",
                    test_round_trip_restores_pending_update_state);
    g_test_add_func("/dm-iwdg-vmstate/reject-pending-without-deadline",
                    test_rejects_pending_update_without_deadline);
    g_test_add_func("/dm-iwdg-vmstate/reject-reserved-prescaler",
                    test_rejects_reserved_prescaler_without_sync);
    g_test_add_func("/dm-iwdg-vmstate/reject-invalid-reload",
                    test_rejects_invalid_reload_without_sync);
    g_test_add_func("/dm-iwdg-vmstate/reject-invalid-window",
                    test_rejects_invalid_window_without_sync);
    g_test_add_func("/dm-iwdg-vmstate/reject-truncated",
                    test_rejects_truncated_state_without_sync);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
