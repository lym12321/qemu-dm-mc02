/* Focused VMState contract test for the board-independent GPIO component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_gpio.h"
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

static int callback_count;
static unsigned callback_bank;
static uint32_t callback_odr;

static void odr_changed(void *opaque, unsigned bank_index, uint32_t odr)
{
    int *count = opaque;

    (*count)++;
    callback_bank = bank_index;
    callback_odr = odr;
}

static void save_state(const DmMc02GpioBank *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_gpio_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02GpioBank *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_gpio_vmstate(), state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void test_round_trip_restores_registers_and_notifies(void)
{
    DmMc02GpioBank source;
    DmMc02GpioBank restored;

    memset(&source, 0, sizeof(source));
    source.bank_index = 3;
    source.moder = 0x01234567;
    source.otyper = 0x89abcdef;
    source.ospeedr = 0x10203040;
    source.pupdr = 0x50607080;
    source.idr = 0x90a0b0c0;
    source.odr = 0xd0e0f001;
    source.afr0 = 0x23456789;
    source.afr1 = 0xabcdef01;
    save_state(&source);

    memset(&restored, 0, sizeof(restored));
    restored.bank_index = 11;
    callback_count = 0;
    callback_bank = 0;
    callback_odr = 0;
    restored.odr_changed = odr_changed;
    restored.odr_changed_opaque = &callback_count;

    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_cmpuint(restored.bank_index, ==, 11);
    g_assert_cmpuint(restored.moder, ==, source.moder);
    g_assert_cmpuint(restored.otyper, ==, source.otyper);
    g_assert_cmpuint(restored.ospeedr, ==, source.ospeedr);
    g_assert_cmpuint(restored.pupdr, ==, source.pupdr);
    g_assert_cmpuint(restored.idr, ==, source.idr);
    g_assert_cmpuint(restored.odr, ==, source.odr);
    g_assert_cmpuint(restored.afr0, ==, source.afr0);
    g_assert_cmpuint(restored.afr1, ==, source.afr1);
    g_assert_cmpint(callback_count, ==, 1);
    g_assert_cmpuint(callback_bank, ==, 11);
    g_assert_cmpuint(callback_odr, ==, source.odr);
}

static void test_rejects_truncated_state_without_notification(void)
{
    DmMc02GpioBank state;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    callback_count = 0;
    state.odr_changed = odr_changed;
    state.odr_changed_opaque = &callback_count;
    g_assert_cmpint(load_state(&state), !=, 0);
    g_assert_cmpint(callback_count, ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-gpio-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-gpio-vmstate/round-trip-notify",
                    test_round_trip_restores_registers_and_notifies);
    g_test_add_func("/dm-gpio-vmstate/reject-truncated",
                    test_rejects_truncated_state_without_notification);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
