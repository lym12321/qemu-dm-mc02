/* Focused VMState contract test for the board-independent Flash component. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_flash.h"
#include "migration/migration.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

#define FLASH_CR1_OFFSET 0x0c
#define FLASH_CR_LOCK    (1u << 0)
#define FLASH_CR_PG      (1u << 1)

static int temp_fd;
static unsigned sync_count;

/* The component test intentionally does not link QEMU's full system memory
 * implementation.  This test-only implementation observes the public
 * runtime-sync boundary; the production implementation lives in
 * dm_mc02_flash.c and rebuilds the real overlay. */
void dm_mc02_flash_sync_runtime(DmMc02Flash *state)
{
    uint32_t cr = ldl_le_p(state->regs + FLASH_CR1_OFFSET);

    state->program_enabled = !(cr & FLASH_CR_LOCK) && (cr & FLASH_CR_PG);
    state->program_window.enabled = state->program_enabled;
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

static void save_state(const DmMc02Flash *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_mc02_flash_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmMc02Flash *state)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_mc02_flash_vmstate(), state, 1);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void init_target(DmMc02Flash *state, uint8_t *storage,
                        MemoryRegion *storage_region)
{
    memset(state, 0, sizeof(*state));
    state->storage = storage;
    state->storage_size = 0x1000;
    state->storage_region = storage_region;
}

static void test_round_trip_restores_register_state_and_overlay(void)
{
    DmMc02Flash source;
    DmMc02Flash restored;
    MemoryRegion storage_region;
    uint8_t storage[0x1000];
    uint8_t *storage_pointer;
    size_t storage_size;
    MemoryRegion *storage_region_pointer;

    memset(&source, 0, sizeof(source));
    source.regs[0] = 0x12;
    source.regs[0x157] = 0x34;
    source.regs[DM_MC02_FLASH_REG_REGION_SIZE - 1] = 0xa5;
    stl_le_p(source.regs + FLASH_CR1_OFFSET, FLASH_CR_PG);
    source.key1_seen = true;
    source.optkey_seen = true;
    save_state(&source);

    memset(storage, 0xff, sizeof(storage));
    init_target(&restored, storage, &storage_region);
    storage_pointer = restored.storage;
    storage_size = restored.storage_size;
    storage_region_pointer = restored.storage_region;
    restored.program_enabled = false;
    restored.program_window.enabled = false;
    sync_count = 0;

    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_true(restored.key1_seen);
    g_assert_true(restored.optkey_seen);
    g_assert_true(restored.program_enabled);
    g_assert_true(restored.program_window.enabled);
    g_assert_cmpuint(sync_count, ==, 1);
    g_assert_true(restored.storage == storage_pointer);
    g_assert_cmpuint(restored.storage_size, ==, storage_size);
    g_assert_true(restored.storage_region == storage_region_pointer);
}

static void test_locked_state_disables_program_overlay(void)
{
    DmMc02Flash source;
    DmMc02Flash restored;
    MemoryRegion storage_region;
    uint8_t storage[0x1000];

    memset(&source, 0, sizeof(source));
    stl_le_p(source.regs + FLASH_CR1_OFFSET, FLASH_CR_LOCK | FLASH_CR_PG);
    save_state(&source);

    init_target(&restored, storage, &storage_region);
    restored.program_enabled = true;
    restored.program_window.enabled = true;
    sync_count = 0;
    g_assert_cmpint(load_state(&restored), ==, 0);
    g_assert_false(restored.program_enabled);
    g_assert_false(restored.program_window.enabled);
}

static void test_rejects_truncated_state_without_rebuilding_overlay(void)
{
    DmMc02Flash state;
    MemoryRegion storage_region;
    uint8_t storage[0x1000];
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    init_target(&state, storage, &storage_region);
    state.program_enabled = true;
    state.program_window.enabled = true;
    sync_count = 0;
    g_assert_cmpint(load_state(&state), !=, 0);
    g_assert_true(state.program_enabled);
    g_assert_true(state.program_window.enabled);
    g_assert_cmpuint(sync_count, ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-flash-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-flash-vmstate/round-trip-overlay",
                    test_round_trip_restores_register_state_and_overlay);
    g_test_add_func("/dm-flash-vmstate/locked-overlay",
                    test_locked_state_disables_program_overlay);
    g_test_add_func("/dm-flash-vmstate/reject-truncated",
                    test_rejects_truncated_state_without_rebuilding_overlay);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
