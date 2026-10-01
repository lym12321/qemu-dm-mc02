/* Focused VMState contract test for the board-independent EXTI component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_exti.h"
#include "migration/migration.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

#define EXTI_PR1     0x14
#define EXTI_C1IMR1  0x80

static int temp_fd;
static unsigned sync_count;

/* The isolated test deliberately supplies only the public runtime-sync
 * boundary.  The production implementation additionally drives qemu_irq
 * outputs through the real EXTI model. */
void dm_mc02_exti_sync_runtime(DmMc02Exti *state)
{
    uint32_t pending = ldl_le_p(state->regs + EXTI_PR1);
    uint32_t enabled = ldl_le_p(state->regs + EXTI_C1IMR1);

    state->irq_level[0] = (pending & enabled & 0x1fu) != 0;
    sync_count++;
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

static void save_state(const DmMc02Exti *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_exti_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Exti *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_exti_vmstate(), state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void test_round_trip_restores_line_and_reprojects_irq(void)
{
    DmMc02Exti source;
    DmMc02Exti restored;
    qemu_irq irq;

    memset(&source, 0, sizeof(source));
    source.regs[EXTI_PR1] = 0x04;
    source.regs[EXTI_C1IMR1] = 0x1f;
    source.line_level = 0x0004;
    source.irq_level[0] = true;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    irq = (qemu_irq)(uintptr_t)0x1234;
    restored.irq[0] = irq;
    restored.irq_level[0] = false;
    sync_count = 0;

    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_cmpuint(restored.line_level, ==, source.line_level);
    g_assert_true(restored.irq[0] == irq);
    g_assert_true(restored.irq_level[0]);
    g_assert_cmpuint(sync_count, ==, 1);
}

static void test_rejects_truncated_state_without_reprojection(void)
{
    DmMc02Exti state;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    state.irq_level[0] = true;
    sync_count = 0;
    g_assert_cmpint(load_state(&state), !=, 0);
    g_assert_true(state.irq_level[0]);
    g_assert_cmpuint(sync_count, ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-exti-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-exti-vmstate/round-trip-reproject",
                    test_round_trip_restores_line_and_reprojects_irq);
    g_test_add_func("/dm-exti-vmstate/reject-truncated",
                    test_rejects_truncated_state_without_reprojection);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
