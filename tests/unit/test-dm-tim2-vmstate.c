/* Focused VMState contract test for the board-independent timer component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_tim2.h"
#include "migration/migration.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

extern unsigned dm_mc02_tim2_sync_calls;

static int temp_fd;

static void changed_callback(void *opaque)
{
    unsigned *count = opaque;

    (*count)++;
}

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

static void save_state(const DmMc02Tim2 *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_tim2_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Tim2 *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_tim2_vmstate(), state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void prepare_source(DmMc02Tim2 *state)
{
    memset(state, 0, sizeof(*state));
    for (unsigned i = 0; i < ARRAY_SIZE(state->regs); ++i) {
        state->regs[i] = UINT32_C(0x10203040) ^ i * UINT32_C(0x01010101);
    }
    /* Make the channels represented by compare_channel_mask output channels
     * so the consistency check exercises the same producer contract as the
     * live timer model. */
    state->regs[0x18 / sizeof(uint32_t)] &= ~UINT32_C(0x00000303);
    state->regs[0x1c / sizeof(uint32_t)] &= ~UINT32_C(0x00000303);
    state->regs[0] |= 1u;
    state->active_psc = 0x1234;
    state->active_arr = UINT32_C(0x89abcdef);
    state->active_ccr[0] = 11;
    state->active_ccr[1] = 22;
    state->active_ccr[2] = 33;
    state->active_ccr[3] = 44;
    state->active_rcr = 7;
    state->repetition_remaining = 5;
    state->start_ns = 1000;
    state->start_counting_down = true;
    state->next_update_ns = 2000;
    state->next_compare_ns = 3000;
    state->compare_channel_mask = 0x05;
    state->cc1_active = true;
    state->cc2_active = false;
    state->cc3_active = true;
    state->cc4_active = true;
    state->ocref_level_mask = 0x09;
    state->break_input_level = false;
    state->break_latched = true;
    state->update_batch = 3;
    state->compare_batch = 4;
}

static void test_round_trip_restores_dynamic_state(void)
{
    DmMc02Tim2 source;
    DmMc02Tim2 restored;
    unsigned changed_count = 0;

    prepare_source(&source);
    source.update_interval_ns = 9876;
    source.irq_level_valid = false;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    restored.update_interval_ns = 1;
    restored.irq_level_valid = false;
    restored.changed = changed_callback;
    restored.changed_opaque = &changed_count;
    dm_mc02_tim2_sync_calls = 0;
    g_assert_cmpint(load_state(&restored), ==, 0);

    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_cmpuint(restored.active_psc, ==, source.active_psc);
    g_assert_cmpuint(restored.active_arr, ==, source.active_arr);
    g_assert_cmpmem(restored.active_ccr, sizeof(restored.active_ccr),
                    source.active_ccr, sizeof(source.active_ccr));
    g_assert_cmpuint(restored.active_rcr, ==, source.active_rcr);
    g_assert_cmpuint(restored.repetition_remaining, ==,
                     source.repetition_remaining);
    g_assert_cmpuint(restored.start_ns, ==, source.start_ns);
    g_assert_cmpuint(restored.start_counting_down, ==,
                     source.start_counting_down);
    g_assert_cmpuint(restored.next_update_ns, ==, source.next_update_ns);
    g_assert_cmpuint(restored.next_compare_ns, ==, source.next_compare_ns);
    g_assert_cmpuint(restored.compare_channel_mask, ==,
                     source.compare_channel_mask);
    g_assert_cmpuint(restored.cc1_active, ==, source.cc1_active);
    g_assert_cmpuint(restored.cc2_active, ==, source.cc2_active);
    g_assert_cmpuint(restored.cc3_active, ==, source.cc3_active);
    g_assert_cmpuint(restored.cc4_active, ==, source.cc4_active);
    g_assert_cmpuint(restored.ocref_level_mask, ==, source.ocref_level_mask);
    g_assert_cmpuint(restored.break_input_level, ==,
                     source.break_input_level);
    g_assert_cmpuint(restored.break_latched, ==, source.break_latched);
    g_assert_cmpuint(restored.update_batch, ==, source.update_batch);
    g_assert_cmpuint(restored.compare_batch, ==, source.compare_batch);
    g_assert_cmpuint(restored.update_interval_ns, ==, 0);
    g_assert_true(restored.irq_level_valid);
    g_assert_cmpuint(dm_mc02_tim2_sync_calls, ==, 1);
    g_assert_cmpuint(changed_count, ==, 1);
}

static void test_rejects_invalid_dynamic_state_without_sync(void)
{
    DmMc02Tim2 source;
    DmMc02Tim2 restored;

    prepare_source(&source);
    source.next_compare_ns = 4000;
    source.compare_channel_mask = 0;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    restored.irq_level_valid = false;
    dm_mc02_tim2_sync_calls = 0;
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_false(restored.irq_level_valid);
    g_assert_cmpuint(dm_mc02_tim2_sync_calls, ==, 0);
}

static void test_rejects_inconsistent_deadline_without_sync(void)
{
    DmMc02Tim2 source;
    DmMc02Tim2 restored;

    prepare_source(&source);
    source.next_update_ns = source.start_ns - 1;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    restored.irq_level_valid = false;
    dm_mc02_tim2_sync_calls = 0;
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_false(restored.irq_level_valid);
    g_assert_cmpuint(dm_mc02_tim2_sync_calls, ==, 0);
}

static void test_rejects_inactive_compare_channel_without_sync(void)
{
    DmMc02Tim2 source;
    DmMc02Tim2 restored;

    prepare_source(&source);
    source.compare_channel_mask = 0x02;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    restored.irq_level_valid = false;
    dm_mc02_tim2_sync_calls = 0;
    g_assert_cmpint(load_state(&restored), !=, 0);
    g_assert_false(restored.irq_level_valid);
    g_assert_cmpuint(dm_mc02_tim2_sync_calls, ==, 0);
}

static void test_rejects_truncated_state_without_sync(void)
{
    DmMc02Tim2 state;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    state.irq_level_valid = false;
    dm_mc02_tim2_sync_calls = 0;
    g_assert_cmpint(load_state(&state), !=, 0);
    g_assert_false(state.irq_level_valid);
    g_assert_cmpuint(dm_mc02_tim2_sync_calls, ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-tim2-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-tim2-vmstate/round-trip",
                    test_round_trip_restores_dynamic_state);
    g_test_add_func("/dm-tim2-vmstate/reject-invalid",
                    test_rejects_invalid_dynamic_state_without_sync);
    g_test_add_func("/dm-tim2-vmstate/reject-deadline-order",
                    test_rejects_inconsistent_deadline_without_sync);
    g_test_add_func("/dm-tim2-vmstate/reject-inactive-compare",
                    test_rejects_inactive_compare_channel_without_sync);
    g_test_add_func("/dm-tim2-vmstate/reject-truncated",
                    test_rejects_truncated_state_without_sync);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
