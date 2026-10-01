/* Focused VMState contract test for the board-independent SYSCFG component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_syscfg.h"
#include "migration/migration.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

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

static void save_state(const DmMc02Syscfg *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_syscfg_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int changed_count;

static void changed(void *opaque)
{
    int *count = opaque;

    (*count)++;
}

static int load_state(DmMc02Syscfg *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_syscfg_vmstate(), state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void test_round_trip_rebinds_consumer(void)
{
    DmMc02Syscfg source;
    DmMc02Syscfg restored;

    memset(&source, 0, sizeof(source));
    source.regs[0x08] = 0x21;
    source.regs[0x09] = 0x43;
    source.regs[0x14] = 0xa5;
    source.regs[DM_MC02_SYSCFG_REGION_SIZE - 1] = 0x5a;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    changed_count = 0;
    restored.changed = changed;
    restored.changed_opaque = &changed_count;
    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_cmpuint(restored.regs[0x08], ==, 0x21);
    g_assert_cmpint(changed_count, ==, 1);
}

static void test_rejects_truncated_state(void)
{
    DmMc02Syscfg state;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    g_assert_cmpint(load_state(&state), !=, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-syscfg-vmstate.XXXXXX",
                                                  g_get_tmp_dir());

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-syscfg-vmstate/round-trip-rebind",
                    test_round_trip_rebinds_consumer);
    g_test_add_func("/dm-syscfg-vmstate/reject-truncated",
                    test_rejects_truncated_state);
    int ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
