/* Focused VMState contract test for the BMI088 SPI framing adapter. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_bmi088_spi.h"
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

static void save_state(const DmMc02Bmi088Spi *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_bmi088_spi_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Bmi088Spi *state, int version_id)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_bmi088_spi_vmstate(), state,
                                 version_id);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void consume_sentinel(void *opaque)
{
    g_assert_not_reached();
    (void)opaque;
}

static void prepare_read_source(DmMc02Bmi088Spi *state)
{
    memset(state, 0, sizeof(*state));
    state->command_seen = true;
    state->read_transfer = true;
    state->dummy_pending = true;
    state->reg = 0x19;
    state->read_start_reg = 0x12;
}

static void seed_runtime_sentinels(DmMc02Bmi088Spi *state,
                                    DmMc02Bmi088 *bmi,
                                    void **opaque)
{
    state->bmi = bmi;
    state->consume = consume_sentinel;
    *opaque = (void *)(uintptr_t)0xfeed1234;
    state->consume_opaque = *opaque;
}

static void test_round_trip_preserves_framing_and_runtime(void)
{
    DmMc02Bmi088 restored_bmi;
    DmMc02Bmi088Spi source;
    DmMc02Bmi088Spi restored;
    void *opaque;

    prepare_read_source(&source);
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    seed_runtime_sentinels(&restored, &restored_bmi, &opaque);
    g_assert_cmpint(load_state(&restored, 1), ==, 0);

    g_assert_true(restored.command_seen);
    g_assert_true(restored.read_transfer);
    g_assert_true(restored.dummy_pending);
    g_assert_cmpuint(restored.reg, ==, source.reg);
    g_assert_cmpuint(restored.read_start_reg, ==, source.read_start_reg);
    g_assert_true(restored.bmi == &restored_bmi);
    g_assert_true(restored.consume == consume_sentinel);
    g_assert_true(restored.consume_opaque == opaque);

    /* A write transaction is also legal when its dummy phase is clear. */
    source.command_seen = true;
    source.read_transfer = false;
    source.dummy_pending = false;
    source.reg = 0x40;
    source.read_start_reg = 0x40;
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    seed_runtime_sentinels(&restored, &restored_bmi, &opaque);
    g_assert_cmpint(load_state(&restored, 1), ==, 0);
    g_assert_true(restored.command_seen);
    g_assert_false(restored.read_transfer);
    g_assert_false(restored.dummy_pending);
    g_assert_cmpuint(restored.reg, ==, 0x40);
    g_assert_cmpuint(restored.read_start_reg, ==, 0x40);
    g_assert_true(restored.bmi == &restored_bmi);
    g_assert_true(restored.consume == consume_sentinel);
    g_assert_true(restored.consume_opaque == opaque);
}

static void test_rejects_unsupported_version_without_runtime_changes(void)
{
    DmMc02Bmi088Spi source;
    DmMc02Bmi088Spi restored;
    DmMc02Bmi088 bmi;
    void *opaque;

    prepare_read_source(&source);
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    seed_runtime_sentinels(&restored, &bmi, &opaque);

    g_assert_cmpint(load_state(&restored, 2), !=, 0);
    g_assert_true(restored.bmi == &bmi);
    g_assert_true(restored.consume == consume_sentinel);
    g_assert_true(restored.consume_opaque == opaque);
}

static void test_rejects_cursor_without_command(void)
{
    DmMc02Bmi088Spi source;
    DmMc02Bmi088Spi restored;
    DmMc02Bmi088 bmi;
    void *opaque;

    memset(&source, 0, sizeof(source));
    source.reg = 1;
    source.read_start_reg = 2;
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    seed_runtime_sentinels(&restored, &bmi, &opaque);

    g_assert_cmpint(load_state(&restored, 1), !=, 0);
    g_assert_true(restored.bmi == &bmi);
    g_assert_true(restored.consume == consume_sentinel);
    g_assert_true(restored.consume_opaque == opaque);
}

static void test_rejects_dummy_on_write_transaction(void)
{
    DmMc02Bmi088Spi source;
    DmMc02Bmi088Spi restored;
    DmMc02Bmi088 bmi;
    void *opaque;

    memset(&source, 0, sizeof(source));
    source.command_seen = true;
    source.read_transfer = false;
    source.dummy_pending = true;
    source.reg = 0x40;
    source.read_start_reg = 0x40;
    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    seed_runtime_sentinels(&restored, &bmi, &opaque);

    g_assert_cmpint(load_state(&restored, 1), !=, 0);
    g_assert_true(restored.bmi == &bmi);
    g_assert_true(restored.consume == consume_sentinel);
    g_assert_true(restored.consume_opaque == opaque);
}

static void test_rejects_truncated_stream(void)
{
    DmMc02Bmi088Spi state;
    DmMc02Bmi088 bmi;
    void *opaque;
    QEMUFile *file = open_test_file(true);
    static const uint8_t truncated[] = { 1, 1, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    seed_runtime_sentinels(&state, &bmi, &opaque);
    g_assert_cmpint(load_state(&state, 1), !=, 0);
    g_assert_true(state.bmi == &bmi);
    g_assert_true(state.consume == consume_sentinel);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file =
        g_strdup_printf("%s/dm-bmi088-spi-vmstate.XXXXXX", g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-bmi088-spi-vmstate/round-trip-runtime",
                    test_round_trip_preserves_framing_and_runtime);
    g_test_add_func("/dm-bmi088-spi-vmstate/reject-version",
                    test_rejects_unsupported_version_without_runtime_changes);
    g_test_add_func("/dm-bmi088-spi-vmstate/reject-no-command-cursor",
                    test_rejects_cursor_without_command);
    g_test_add_func("/dm-bmi088-spi-vmstate/reject-write-dummy",
                    test_rejects_dummy_on_write_transaction);
    g_test_add_func("/dm-bmi088-spi-vmstate/reject-truncated",
                    test_rejects_truncated_stream);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
