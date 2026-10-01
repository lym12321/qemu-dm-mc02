/* Focused VMState contract test for the board-independent USB control core. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_control.h"
#include "migration/migration.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

typedef struct ControlVmstateFixture {
    DmUsbControlDevice device;
    uint8_t descriptor[16];
    unsigned address_updates;
    unsigned configuration_updates;
} ControlVmstateFixture;

static int temp_fd;

static bool fixture_get_descriptor(void *opaque, uint8_t type, uint8_t index,
                                   const uint8_t **data, size_t *length)
{
    ControlVmstateFixture *fixture = opaque;

    if (type != 1 || index != 0) {
        return false;
    }
    *data = fixture->descriptor;
    *length = sizeof(fixture->descriptor);
    return true;
}

static void fixture_set_address(void *opaque, uint8_t address)
{
    ControlVmstateFixture *fixture = opaque;

    (void)address;
    ++fixture->address_updates;
}

static void fixture_set_configuration(void *opaque, uint8_t configuration)
{
    ControlVmstateFixture *fixture = opaque;

    (void)configuration;
    ++fixture->configuration_updates;
}

static void fixture_init(ControlVmstateFixture *fixture)
{
    static const DmUsbControlOps ops = {
        .get_descriptor = fixture_get_descriptor,
        .set_address = fixture_set_address,
        .set_configuration = fixture_set_configuration,
    };

    memset(fixture, 0, sizeof(*fixture));
    for (unsigned i = 0; i < ARRAY_SIZE(fixture->descriptor); ++i) {
        fixture->descriptor[i] = i + 1;
    }
    dm_usb_control_init(&fixture->device, &ops, fixture, 8);
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

static void save_state(const DmUsbControlDevice *state)
{
    QEMUFile *file = open_test_file(true);

    g_assert_cmpint(vmstate_save_state(file, dm_usb_control_vmstate(),
                                       (void *)state, NULL), ==, 0);
    qemu_put_byte(file, QEMU_VM_EOF);
    g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    qemu_fclose(file);
}

static int load_state(DmUsbControlDevice *state, int version_id)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_usb_control_vmstate(), state,
                                 version_id);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void assert_runtime_wiring_unchanged(
    const ControlVmstateFixture *fixture,
    DmUsbControlOps ops, void *opaque, uint16_t max_packet_size)
{
    g_assert_true(fixture->device.ops.get_descriptor == ops.get_descriptor);
    g_assert_true(fixture->device.ops.class_request == ops.class_request);
    g_assert_true(fixture->device.ops.set_address == ops.set_address);
    g_assert_true(fixture->device.ops.set_configuration ==
                  ops.set_configuration);
    g_assert_true(fixture->device.opaque == opaque);
    g_assert_cmpuint(fixture->device.max_packet_size, ==, max_packet_size);
}

static void prepare_descriptor_transfer(ControlVmstateFixture *fixture)
{
    static const uint8_t setup[] = { 0x80, 6, 0, 1, 0, 0, 16, 0 };
    uint8_t packet[8];
    size_t length;

    fixture_init(fixture);
    g_assert_cmpint(dm_usb_control_setup(&fixture->device, setup), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_in(&fixture->device, packet,
                                      sizeof(packet), &length), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(length, ==, sizeof(packet));
    g_assert_cmpmem(packet, length, fixture->descriptor, sizeof(packet));
    g_assert_cmpint(dm_usb_control_phase(&fixture->device), ==,
                    DM_USB_CONTROL_DATA_IN);
}

static void test_round_trip_preserves_transfer_and_callbacks(void)
{
    ControlVmstateFixture source;
    ControlVmstateFixture restored;
    DmUsbControlOps ops;
    uint8_t packet[8];
    size_t length;

    prepare_descriptor_transfer(&source);
    save_state(&source.device);

    fixture_init(&restored);
    ops = restored.device.ops;
    g_assert_cmpint(load_state(&restored.device, 1), ==, 0);
    assert_runtime_wiring_unchanged(&restored, ops, &restored, 8);
    g_assert_cmpint(dm_usb_control_phase(&restored.device), ==,
                    DM_USB_CONTROL_DATA_IN);
    g_assert_cmpuint(restored.device.data_length, ==, 16);
    g_assert_cmpuint(restored.device.data_offset, ==, 8);
    g_assert_cmpmem(restored.device.data, sizeof(restored.device.data),
                    source.device.data, sizeof(source.device.data));
    g_assert_cmpuint(restored.address_updates, ==, 0);
    g_assert_cmpuint(restored.configuration_updates, ==, 0);

    g_assert_cmpint(dm_usb_control_in(&restored.device, packet,
                                      sizeof(packet), &length), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(length, ==, sizeof(packet));
    g_assert_cmpmem(packet, length, restored.descriptor + 8,
                    sizeof(packet));
    g_assert_cmpint(dm_usb_control_phase(&restored.device), ==,
                    DM_USB_CONTROL_STATUS_OUT);
    g_assert_cmpint(dm_usb_control_out(&restored.device, NULL, 0), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpint(dm_usb_control_phase(&restored.device), ==,
                    DM_USB_CONTROL_IDLE);
}

static void test_round_trip_preserves_pending_status_commit(void)
{
    ControlVmstateFixture source;
    ControlVmstateFixture restored;
    static const uint8_t setup[] = { 0, 5, 42, 0, 0, 0, 0, 0 };
    DmUsbControlOps ops;
    uint8_t packet[8];
    size_t length;

    fixture_init(&source);
    g_assert_cmpint(dm_usb_control_setup(&source.device, setup), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_true(source.device.address_pending);
    save_state(&source.device);

    fixture_init(&restored);
    ops = restored.device.ops;
    g_assert_cmpint(load_state(&restored.device, 1), ==, 0);
    assert_runtime_wiring_unchanged(&restored, ops, &restored, 8);
    g_assert_cmpuint(restored.device.address, ==, 0);
    g_assert_cmpuint(restored.address_updates, ==, 0);
    g_assert_cmpint(dm_usb_control_in(&restored.device, packet,
                                      sizeof(packet), &length), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(length, ==, 0);
    g_assert_cmpuint(restored.device.address, ==, 42);
    g_assert_cmpuint(restored.address_updates, ==, 1);
}

static void test_rejects_invalid_data_cursor_without_runtime_sync(void)
{
    ControlVmstateFixture source;
    ControlVmstateFixture restored;
    DmUsbControlOps ops;

    prepare_descriptor_transfer(&source);
    source.device.data_offset = DM_USB_CONTROL_MAX_DATA + 1;
    save_state(&source.device);

    fixture_init(&restored);
    ops = restored.device.ops;
    g_assert_cmpint(load_state(&restored.device, 1), !=, 0);
    assert_runtime_wiring_unchanged(&restored, ops, &restored, 8);
}

static void assert_state_rejected(const ControlVmstateFixture *source)
{
    ControlVmstateFixture restored;
    DmUsbControlOps ops;

    save_state(&source->device);
    fixture_init(&restored);
    ops = restored.device.ops;
    g_assert_cmpint(load_state(&restored.device, 1), !=, 0);
    assert_runtime_wiring_unchanged(&restored, ops, &restored, 8);
}

static void test_rejects_inconsistent_transfer_and_pending_state(void)
{
    ControlVmstateFixture source;

    prepare_descriptor_transfer(&source);
    source.device.data_length = source.device.request.length + 1;
    assert_state_rejected(&source);

    prepare_descriptor_transfer(&source);
    source.device.data_length = 0;
    source.device.data_offset = 0;
    source.device.zero_length_packet = false;
    assert_state_rejected(&source);

    prepare_descriptor_transfer(&source);
    source.device.setup[0] = 0;
    source.device.setup[1] = 9;
    source.device.setup[2] = 1;
    source.device.setup[3] = 0;
    source.device.setup[6] = 0;
    source.device.setup[7] = 0;
    source.device.request = (DmUsbControlRequest) {
        .request_type = 0,
        .request = 9,
        .value = 1,
        .index = 0,
        .length = 0,
    };
    source.device.phase = DM_USB_CONTROL_STATUS_IN;
    source.device.address_pending = true;
    source.device.pending_address = 42;
    source.device.data_length = 0;
    source.device.data_offset = 0;
    assert_state_rejected(&source);
}

static void test_rejects_invalid_phase_and_version(void)
{
    ControlVmstateFixture source;
    ControlVmstateFixture restored;
    DmUsbControlOps ops;

    prepare_descriptor_transfer(&source);
    source.device.phase = (DmUsbControlPhase)99;
    save_state(&source.device);
    fixture_init(&restored);
    ops = restored.device.ops;
    g_assert_cmpint(load_state(&restored.device, 1), !=, 0);
    assert_runtime_wiring_unchanged(&restored, ops, &restored, 8);

    prepare_descriptor_transfer(&source);
    save_state(&source.device);
    fixture_init(&restored);
    ops = restored.device.ops;
    g_assert_cmpint(load_state(&restored.device, 2), !=, 0);
    assert_runtime_wiring_unchanged(&restored, ops, &restored, 8);
}

static void test_rejects_zero_length_packet_outside_data_in(void)
{
    ControlVmstateFixture source;
    ControlVmstateFixture restored;
    DmUsbControlOps ops;

    prepare_descriptor_transfer(&source);
    source.device.setup[6] = 24;
    source.device.request.length = 24;
    source.device.zero_length_packet = true;
    source.device.data_offset = source.device.data_length;
    source.device.phase = DM_USB_CONTROL_STATUS_OUT;
    g_assert_true(source.device.zero_length_packet);
    save_state(&source.device);

    fixture_init(&restored);
    ops = restored.device.ops;
    g_assert_cmpint(load_state(&restored.device, 1), !=, 0);
    assert_runtime_wiring_unchanged(&restored, ops, &restored, 8);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file =
        g_strdup_printf("%s/dm-usb-control-vmstate.XXXXXX", g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/control-vmstate/round-trip-transfer",
                    test_round_trip_preserves_transfer_and_callbacks);
    g_test_add_func("/dm-usb/control-vmstate/pending-status",
                    test_round_trip_preserves_pending_status_commit);
    g_test_add_func("/dm-usb/control-vmstate/reject-cursor",
                    test_rejects_invalid_data_cursor_without_runtime_sync);
    g_test_add_func("/dm-usb/control-vmstate/reject-inconsistent-state",
                    test_rejects_inconsistent_transfer_and_pending_state);
    g_test_add_func("/dm-usb/control-vmstate/reject-phase-version",
                    test_rejects_invalid_phase_and_version);
    g_test_add_func("/dm-usb/control-vmstate/reject-zlp-phase",
                    test_rejects_zero_length_packet_outside_data_in);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
