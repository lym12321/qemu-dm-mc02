/* Focused VMState contract test for the board-independent CORDIC component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_cordic.h"
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

static void save_state(const DmMc02Cordic *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_cordic_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Cordic *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_cordic_vmstate(), state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void test_round_trip(void)
{
    DmMc02Cordic source = {
        .csr = UINT32_C(0x00680001),
        .args = { UINT32_C(0x12345678), UINT32_C(0x87654321) },
        .arg_count = 1,
        .results = { UINT32_C(0xdeadbeef), UINT32_C(0x10203040) },
        .result_count = 2,
    };
    DmMc02Cordic restored;

    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_cmpuint(restored.csr, ==, source.csr);
    g_assert_cmpmem(restored.args, sizeof(restored.args), source.args,
                    sizeof(source.args));
    g_assert_cmpuint(restored.arg_count, ==, source.arg_count);
    g_assert_cmpmem(restored.results, sizeof(restored.results), source.results,
                    sizeof(source.results));
    g_assert_cmpuint(restored.result_count, ==, source.result_count);
}

static void test_rejects_invalid_queue_counts(void)
{
    /* Fields are serialized in declaration order, big-endian, and the
     * malformed arg_count is the fourth u32. */
    static const uint8_t invalid_arg_count[] = {
        0x00, 0x00, 0x00, 0x00, /* csr */
        0x00, 0x00, 0x00, 0x00, /* args[0] */
        0x00, 0x00, 0x00, 0x00, /* args[1] */
        0x00, 0x00, 0x00, 0x03, /* arg_count */
        0x00, 0x00, 0x00, 0x00, /* results[0] */
        0x00, 0x00, 0x00, 0x00, /* results[1] */
        0x00, 0x00, 0x00, 0x00, /* result_count */
        QEMU_VM_EOF,
    };
    DmMc02Cordic state;
    QEMUFile *file;

    file = open_test_file(true);
    qemu_put_buffer(file, invalid_arg_count, sizeof(invalid_arg_count));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    g_assert_cmpint(load_state(&state), !=, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-cordic-vmstate.XXXXXX",
                                                  g_get_tmp_dir());

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-cordic-vmstate/round-trip", test_round_trip);
    g_test_add_func("/dm-cordic-vmstate/reject-invalid-count",
                    test_rejects_invalid_queue_counts);
    int ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
