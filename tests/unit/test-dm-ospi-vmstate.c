/* Focused VMState contract test for the STM32H723 OCTOSPI wrapper. */
#include "qemu/osdep.h"
#include "hw/arm/dm_mc02_ospi.h"
#include "migration/migration.h"
#include "migration/vmstate.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

extern unsigned dm_mc02_ospi_select_calls;
extern bool dm_mc02_ospi_selected;

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

static void save_vmsd(const void *state, const VMStateDescription *vmsd)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, vmsd, (void *)state, NULL), ==,
                    0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static void save_state(const DmMc02Ospi *state)
{
    save_vmsd(state, dm_mc02_ospi_vmstate());
}

static int load_state(void *state,
                      const VMStateDescription *vmsd)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, vmsd, state, vmsd->version_id);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void set_flash_config(DmMc02Ospi *state)
{
    state->has_flash = true;
    state->flash_size = DM_MC02_OSPI_FLASH_SIZE;
    state->page_size = DM_MC02_OSPI_PAGE_SIZE;
    state->sector_size = 0x1000;
    state->jedec_id[0] = 0xef;
    state->jedec_id[1] = 0x40;
    state->jedec_id[2] = 0x17;
}

static void prepare_source(DmMc02Ospi *state)
{
    memset(state, 0, sizeof(*state));
    set_flash_config(state);
    state->regs[0] = 3u << 28;
    state->regs[0x40 / sizeof(uint32_t)] = 4;
    state->rx_size = 5;
    state->rx_pos = 2;
    state->rx_data = g_malloc(state->rx_size);
    for (unsigned i = 0; i < state->rx_size; ++i) {
        state->rx_data[i] = (uint8_t)(0xa0u + i);
    }
    for (unsigned i = 0; i < sizeof(state->tx_data); ++i) {
        state->tx_data[i] = (uint8_t)(0x30u + i);
    }
    state->tx_size = 2;
    state->tx_expected = 5;
    state->command_address = 0x12345678;
    state->command = 0x02;
    state->command_valid = true;
    state->command_started = true;
}

static void test_round_trip_restores_controller_state(void)
{
    DmMc02Ospi source;
    DmMc02Ospi restored;
    uint8_t *runtime_rx;

    prepare_source(&source);
    memset(&restored, 0, sizeof(restored));
    set_flash_config(&restored);
    runtime_rx = g_malloc(3);
    memset(runtime_rx, 0x5a, 3);
    restored.rx_data = runtime_rx;
    restored.memory_mapped = false;
    dm_mc02_ospi_select_calls = 0;
    dm_mc02_ospi_selected = false;

    save_state(&source);
    g_assert_cmpint(load_state(&restored, dm_mc02_ospi_vmstate()), ==, 0);
    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
    g_assert_cmpmem(restored.rx_data, source.rx_size, source.rx_data,
                    source.rx_size);
    g_assert_cmpuint(restored.rx_size, ==, source.rx_size);
    g_assert_cmpuint(restored.rx_pos, ==, source.rx_pos);
    g_assert_cmpmem(restored.tx_data, sizeof(restored.tx_data),
                    source.tx_data, sizeof(source.tx_data));
    g_assert_cmpuint(restored.tx_size, ==, source.tx_size);
    g_assert_cmpuint(restored.tx_expected, ==, source.tx_expected);
    g_assert_cmpuint(restored.command_address, ==, source.command_address);
    g_assert_cmpuint(restored.command, ==, source.command);
    g_assert_true(restored.command_valid);
    g_assert_true(restored.command_started);
    g_assert_true(restored.memory_mapped);
    g_assert_cmpuint(dm_mc02_ospi_select_calls, ==, 1);
    g_assert_true(dm_mc02_ospi_selected);

    g_free(source.rx_data);
    g_free(restored.rx_data);
}

static void test_raw_restore_does_not_project_runtime(void)
{
    DmMc02Ospi source;
    DmMc02Ospi restored;

    prepare_source(&source);
    memset(&restored, 0, sizeof(restored));
    set_flash_config(&restored);
    restored.memory_mapped = false;
    dm_mc02_ospi_select_calls = 0;
    dm_mc02_ospi_selected = false;
    save_state(&source);

    g_assert_cmpint(load_state(&restored, dm_mc02_ospi_vmstate_raw()), ==, 0);
    g_assert_false(restored.memory_mapped);
    g_assert_cmpuint(dm_mc02_ospi_select_calls, ==, 0);
    g_assert_false(dm_mc02_ospi_selected);

    g_free(source.rx_data);
    g_free(restored.rx_data);
}

static void test_invalid_runtime_state_is_rejected(void)
{
    DmMc02Ospi state;

    memset(&state, 0, sizeof(state));
    state.rx_size = 4;
    state.rx_pos = 5;
    g_assert_false(dm_mc02_ospi_state_valid(&state));

    memset(&state, 0, sizeof(state));
    state.command_valid = true;
    state.command = 0x02;
    state.tx_expected = DM_MC02_OSPI_MAX_PAGE_SIZE + 1;
    g_assert_false(dm_mc02_ospi_state_valid(&state));

    memset(&state, 0, sizeof(state));
    state.command_valid = true;
    state.command_started = true;
    state.command = 0x06;
    g_assert_false(dm_mc02_ospi_state_valid(&state));
}

static void test_invalid_incoming_state_is_rejected_without_projection(void)
{
    DmMc02Ospi source;
    DmMc02Ospi restored;
    VMStateDescription unchecked = *dm_mc02_ospi_vmstate();

    memset(&source, 0, sizeof(source));
    set_flash_config(&source);
    source.command_valid = true;
    source.command = 0x02;
    source.tx_expected = DM_MC02_OSPI_MAX_PAGE_SIZE + 1;
    /* The production pre-save guard is deliberately bypassed only to create
     * a malformed incoming stream for the post-load boundary test. */
    unchecked.pre_save = NULL;
    save_vmsd(&source, &unchecked);

    memset(&restored, 0, sizeof(restored));
    set_flash_config(&restored);
    restored.memory_mapped = true;
    dm_mc02_ospi_select_calls = 0;
    g_assert_cmpint(load_state(&restored, dm_mc02_ospi_vmstate()), !=, 0);
    g_assert_true(restored.memory_mapped);
    g_assert_cmpuint(dm_mc02_ospi_select_calls, ==, 0);
}

static void test_ospim_registers_round_trip(void)
{
    DmMc02Ospim source;
    DmMc02Ospim restored;

    memset(&source, 0, sizeof(source));
    source.regs[0] = 0x11223344;
    source.regs[ARRAY_SIZE(source.regs) - 1] = 0xaabbccdd;
    memset(&restored, 0, sizeof(restored));
    save_vmsd(&source, dm_mc02_ospim_vmstate());
    g_assert_cmpint(load_state(&restored, dm_mc02_ospim_vmstate_raw()), ==, 0);
    g_assert_cmpmem(restored.regs, sizeof(restored.regs), source.regs,
                    sizeof(source.regs));
}

static void test_truncated_state_is_rejected_without_projection(void)
{
    DmMc02Ospi state;
    QEMUFile *file = open_test_file(true);
    uint8_t truncated[] = { 0x12, 0x34, QEMU_VM_EOF };

    qemu_put_buffer(file, truncated, sizeof(truncated));
    qemu_fclose(file);
    memset(&state, 0, sizeof(state));
    set_flash_config(&state);
    state.memory_mapped = true;
    dm_mc02_ospi_select_calls = 0;

    g_assert_cmpint(load_state(&state, dm_mc02_ospi_vmstate()), !=, 0);
    g_assert_true(state.memory_mapped);
    g_assert_cmpuint(dm_mc02_ospi_select_calls, ==, 0);
    g_free(state.rx_data);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf("%s/dm-ospi-vmstate.XXXXXX",
                                                  g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-ospi-vmstate/round-trip",
                    test_round_trip_restores_controller_state);
    g_test_add_func("/dm-ospi-vmstate/raw-no-runtime-projection",
                    test_raw_restore_does_not_project_runtime);
    g_test_add_func("/dm-ospi-vmstate/reject-invalid",
                    test_invalid_runtime_state_is_rejected);
    g_test_add_func("/dm-ospi-vmstate/reject-incoming-invalid",
                    test_invalid_incoming_state_is_rejected_without_projection);
    g_test_add_func("/dm-ospi-vmstate/reject-truncated",
                    test_truncated_state_is_rejected_without_projection);
    g_test_add_func("/dm-ospi-vmstate/ospim-round-trip",
                    test_ospim_registers_round_trip);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
