/* Focused VMState contract test for the board-independent CRC component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_crc.h"
#include "migration/migration.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

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

static void save_state(const DmMc02Crc *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_crc_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static void load_state(DmMc02Crc *state)
{
    QEMUFile *file = open_test_file(false);

    g_assert_cmpint(vmstate_load_state(file, dm_mc02_crc_vmstate(), state, 1),
                    ==, 0);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static void test_round_trip(void)
{
    DmMc02Crc source;
    DmMc02Crc restored;

    memset(&source, 0, sizeof(source));
    source.regs[0] = UINT32_C(0x00000080);
    source.regs[1] = UINT32_C(0x01020304);
    source.regs[DM_MC02_CRC_REGION_SIZE / sizeof(uint32_t) - 1] =
        UINT32_C(0xa5a5a5a5);
    source.value = UINT32_C(0x89abcdef);

    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    load_state(&restored);
    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_cmpuint(restored.value, ==, source.value);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-crc-vmstate.XXXXXX",
                                                  g_get_tmp_dir());

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-crc-vmstate/round-trip", test_round_trip);
    int ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
