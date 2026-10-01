/* Focused VMState contract test for the board-independent RNG component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_rng.h"
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

static void save_state(const DmMc02Rng *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_rng_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Rng *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_rng_vmstate(), state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void test_round_trip(void)
{
    DmMc02Rng source;
    DmMc02Rng restored;

    memset(&source, 0, sizeof(source));
    source.regs[0] = UINT32_C(0x8000000c);
    source.regs[DM_MC02_RNG_HTCR / sizeof(uint32_t)] = UINT32_C(0x10203040);
    source.prng = UINT32_C(0x12345678);
    source.seed = UINT32_C(0x6d2b79f5);
    source.fifo[0] = UINT32_C(0xdeadbeef);
    source.fifo[1] = UINT32_C(0x01020304);
    source.fifo[2] = UINT32_C(0xa5a5a5a5);
    source.fifo[3] = UINT32_C(0xfeedface);
    source.status = DM_MC02_RNG_SR_SEIS;
    source.fifo_count = 2;
    source.fifo_index = 1;
    source.refill_armed = false;

    save_state(&source);
    memset(&restored, 0, sizeof(restored));
    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_cmpuint(restored.prng, ==, source.prng);
    g_assert_cmpuint(restored.seed, ==, source.seed);
    g_assert_cmpmem(restored.fifo, sizeof(restored.fifo), source.fifo,
                    sizeof(source.fifo));
    g_assert_cmpuint(restored.status, ==, source.status);
    g_assert_cmpuint(restored.fifo_count, ==, source.fifo_count);
    g_assert_cmpuint(restored.fifo_index, ==, source.fifo_index);
    g_assert_cmpint(restored.refill_armed, ==, source.refill_armed);
}

static void test_rejects_invalid_fifo_state(void)
{
    DmMc02Rng state;

    memset(&state, 0, sizeof(state));
    state.fifo_count = DM_MC02_RNG_FIFO_DEPTH + 1;
    save_state(&state);
    memset(&state, 0, sizeof(state));
    g_assert_cmpint(load_state(&state), !=, 0);

    memset(&state, 0, sizeof(state));
    state.fifo_index = DM_MC02_RNG_FIFO_DEPTH;
    save_state(&state);
    memset(&state, 0, sizeof(state));
    g_assert_cmpint(load_state(&state), !=, 0);

    memset(&state, 0, sizeof(state));
    state.fifo_count = 1;
    state.refill_armed = true;
    save_state(&state);
    memset(&state, 0, sizeof(state));
    g_assert_cmpint(load_state(&state), !=, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-rng-vmstate.XXXXXX",
                                                  g_get_tmp_dir());

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-rng-vmstate/round-trip", test_round_trip);
    g_test_add_func("/dm-rng-vmstate/reject-invalid-fifo",
                    test_rejects_invalid_fifo_state);
    int ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
