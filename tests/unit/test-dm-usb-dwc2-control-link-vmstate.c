/* Focused VMState test for the composite USB control/DWC2 device link. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_dwc2_control.h"
#include "migration/migration.h"
#include "migration/qemu-file-types.h"
#include "migration/qemu-file.h"
#include "migration/savevm.h"
#include "qemu/module.h"
#include "io/channel-file.h"

#include <fcntl.h>
#include <unistd.h>

typedef struct Dwc2ControlLinkFixture {
    DmUsbDwc2ControlLink link;
    uint8_t descriptor[12];
    uint8_t address;
    unsigned irq_calls;
    bool irq_level;
} Dwc2ControlLinkFixture;

static int temp_fd;

static bool fixture_get_descriptor(void *opaque, uint8_t type, uint8_t index,
                                   const uint8_t **data, size_t *length)
{
    Dwc2ControlLinkFixture *fixture = opaque;

    if (type != 1 || index != 0) {
        return false;
    }
    *data = fixture->descriptor;
    *length = sizeof(fixture->descriptor);
    return true;
}

static void fixture_set_address(void *opaque, uint8_t address)
{
    Dwc2ControlLinkFixture *fixture = opaque;

    fixture->address = address;
}

static void fixture_irq(void *opaque, bool level)
{
    Dwc2ControlLinkFixture *fixture = opaque;

    fixture->irq_calls++;
    fixture->irq_level = level;
}

static const DmUsbControlOps fixture_control_ops = {
    .get_descriptor = fixture_get_descriptor,
    .set_address = fixture_set_address,
};

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

static void fixture_init(Dwc2ControlLinkFixture *fixture)
{
    memset(fixture, 0, sizeof(*fixture));
    for (unsigned i = 0; i < sizeof(fixture->descriptor); ++i) {
        fixture->descriptor[i] = (uint8_t)(0xa0 + i);
    }
    dm_usb_dwc2_control_link_init(&fixture->link, &fixture_control_ops,
                                  fixture, fixture_irq, fixture, 4);
}

static void enable_ep0_irq(Dwc2ControlLinkFixture *fixture)
{
    dm_usb_dwc2_write(&fixture->link.dwc2, DM_USB_DWC2_GAHBCFG,
                      DM_USB_DWC2_GAHBCFG_GINT, 4);
    dm_usb_dwc2_write(&fixture->link.dwc2, DM_USB_DWC2_GINTMSK,
                      DM_USB_DWC2_GINTSTS_IEPINT |
                      DM_USB_DWC2_GINTSTS_OEPINT, 4);
    dm_usb_dwc2_write(&fixture->link.dwc2, DM_USB_DWC2_DIEPMSK,
                      DM_USB_DXEPINT_XFRC, 4);
    dm_usb_dwc2_write(&fixture->link.dwc2, DM_USB_DWC2_DOEPMSK,
                      DM_USB_DXEPINT_STUP | DM_USB_DXEPINT_XFRC, 4);
    dm_usb_dwc2_write(&fixture->link.dwc2, DM_USB_DWC2_DAINTMSK,
                      1u | (1u << 16), 4);
}

static DmUsbTransactionResult submit_setup(
    Dwc2ControlLinkFixture *fixture, const uint8_t setup[8])
{
    DmUsbTransaction transaction = {
        .token = DM_USB_TRANSACTION_SETUP,
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .endpoint = 0,
        .out_data = setup,
        .length = 8,
        .timestamp_ns = 100,
    };

    return dm_usb_dwc2_submit(&fixture->link.dwc2, &transaction);
}

static DmUsbTransactionResult submit_in(Dwc2ControlLinkFixture *fixture,
                                        uint8_t *data, size_t capacity)
{
    DmUsbTransaction transaction = {
        .token = DM_USB_TRANSACTION_IN,
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .endpoint = 0,
        .in_data = data,
        .capacity = capacity,
        .timestamp_ns = 101,
    };

    return dm_usb_dwc2_submit(&fixture->link.dwc2, &transaction);
}

static DmUsbTransactionResult submit_status_out(
    Dwc2ControlLinkFixture *fixture)
{
    DmUsbTransaction transaction = {
        .token = DM_USB_TRANSACTION_OUT,
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .endpoint = 0,
        .timestamp_ns = 102,
    };

    return dm_usb_dwc2_submit(&fixture->link.dwc2, &transaction);
}

static int save_link(const Dwc2ControlLinkFixture *fixture)
{
    QEMUFile *file = open_test_file(true);
    int ret = vmstate_save_state(file, dm_usb_dwc2_control_link_vmstate(),
                                 (void *)&fixture->link, NULL);

    if (!ret) {
        qemu_put_byte(file, QEMU_VM_EOF);
    }
    qemu_fclose(file);
    return ret;
}

static int save_reversed_children(const Dwc2ControlLinkFixture *fixture)
{
    QEMUFile *file = open_test_file(true);
    int ret;

    ret = vmstate_save_state(file, dm_usb_dwc2_vmstate_raw(),
                             (void *)&fixture->link.dwc2, NULL);
    if (!ret) {
        ret = vmstate_save_state(file, dm_usb_control_vmstate_raw(),
                                 (void *)&fixture->link.control, NULL);
    }
    if (!ret) {
        qemu_put_byte(file, QEMU_VM_EOF);
    }
    qemu_fclose(file);
    return ret;
}

static int load_link(Dwc2ControlLinkFixture *fixture, int version_id)
{
    QEMUFile *file = open_test_file(false);
    int ret = vmstate_load_state(file, dm_usb_dwc2_control_link_vmstate(),
                                 &fixture->link, version_id);

    if (!ret) {
        g_assert_cmpint(qemu_file_get_error(file), ==, 0);
    }
    qemu_fclose(file);
    return ret;
}

static void assert_runtime_wiring_unchanged(
    const Dwc2ControlLinkFixture *fixture,
    DmUsbControlDevice *control, DmUsbControlOps control_ops,
    void *control_opaque, DmUsbTransactionOps transaction_ops,
    void *transaction_opaque, DmUsbDwc2Irq *irq, void *irq_opaque,
    bool irq_level)
{
    const DmUsbControlDevice *actual_control = &fixture->link.control;
    const DmUsbDwc2Device *actual_dwc2 = &fixture->link.dwc2;

    g_assert_true(actual_control == control);
    g_assert_true(actual_control->ops.get_descriptor ==
                  control_ops.get_descriptor);
    g_assert_true(actual_control->ops.class_request ==
                  control_ops.class_request);
    g_assert_true(actual_control->ops.set_address == control_ops.set_address);
    g_assert_true(actual_control->ops.set_configuration ==
                  control_ops.set_configuration);
    g_assert_true(actual_control->opaque == control_opaque);
    g_assert_true(actual_dwc2->control == control);
    g_assert_true(actual_dwc2->transaction.control == control);
    g_assert_true(actual_dwc2->transaction.ops.in == transaction_ops.in);
    g_assert_true(actual_dwc2->transaction.ops.out == transaction_ops.out);
    g_assert_true(actual_dwc2->transaction.opaque == transaction_opaque);
    g_assert_true(actual_dwc2->irq == irq);
    g_assert_true(actual_dwc2->irq_opaque == irq_opaque);
    g_assert_cmpint(actual_dwc2->irq_level, ==, irq_level);
}

static void prepare_data_in_mid_transfer(Dwc2ControlLinkFixture *fixture)
{
    static const uint8_t setup[] = { 0x80, 6, 0, 1, 0, 0, 12, 0 };
    uint8_t first_packet[4];
    DmUsbTransactionResult result;

    fixture_init(fixture);
    enable_ep0_irq(fixture);
    result = submit_setup(fixture, setup);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, 8);
    result = submit_in(fixture, first_packet, sizeof(first_packet));
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, sizeof(first_packet));
    g_assert_cmpmem(first_packet, sizeof(first_packet), fixture->descriptor, 4);
    g_assert_cmpint(dm_usb_control_phase(&fixture->link.control), ==,
                    DM_USB_CONTROL_DATA_IN);
    g_assert_cmpuint(fixture->link.control.data_offset, ==, 4);
    g_assert_true(fixture->irq_level);
}

static void test_round_trip_control_phase_and_fifo(void)
{
    Dwc2ControlLinkFixture source;
    Dwc2ControlLinkFixture restored;
    uint8_t data[4];
    uint8_t result_data[12];
    DmUsbTransactionResult result;
    DmUsbControlDevice *control;
    DmUsbTransactionOps transaction_ops;

    prepare_data_in_mid_transfer(&source);
    source.link.dwc2.endpoint[1].in_fifo[0] = 0x5a;
    source.link.dwc2.endpoint[1].in_fifo_count = 1;
    source.link.dwc2.dcfg = 0x150;
    g_assert_cmpint(save_link(&source), ==, 0);

    fixture_init(&restored);
    control = restored.link.dwc2.control;
    transaction_ops = restored.link.dwc2.transaction.ops;
    g_assert_cmpint(load_link(&restored, 1), ==, 0);

    g_assert_true(restored.link.dwc2.control == &restored.link.control);
    g_assert_cmpuint(restored.link.dwc2.dcfg, ==, source.link.dwc2.dcfg);
    g_assert_cmpuint(restored.link.dwc2.endpoint[1].in_fifo_count, ==, 1);
    g_assert_cmpint(dm_usb_control_phase(&restored.link.control), ==,
                    DM_USB_CONTROL_DATA_IN);
    g_assert_cmpuint(restored.link.control.data_offset, ==, 4);
    g_assert_cmpuint(restored.irq_calls, ==, 1);
    g_assert_true(restored.irq_level);
    assert_runtime_wiring_unchanged(
        &restored, control, fixture_control_ops, &restored,
        transaction_ops, &restored.link.dwc2, fixture_irq, &restored, true);

    memcpy(result_data, restored.descriptor, sizeof(data));
    result = submit_in(&restored, data, sizeof(data));
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    memcpy(result_data + 4, data, sizeof(data));
    result = submit_in(&restored, data, sizeof(data));
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    memcpy(result_data + 8, data, sizeof(data));
    result = submit_status_out(&restored);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpint(dm_usb_control_phase(&restored.link.control), ==,
                    DM_USB_CONTROL_IDLE);
    g_assert_cmpmem(result_data, sizeof(result_data), restored.descriptor,
                    sizeof(restored.descriptor));
}

static void test_round_trip_pending_set_address(void)
{
    static const uint8_t setup[] = { 0, 5, 42, 0, 0, 0, 0, 0 };
    Dwc2ControlLinkFixture source;
    Dwc2ControlLinkFixture restored;
    DmUsbTransactionOps transaction_ops;
    DmUsbTransactionResult result;

    fixture_init(&source);
    g_assert_cmpint(submit_setup(&source, setup).status, ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    g_assert_true(source.link.control.address_pending);
    g_assert_cmpuint(source.address, ==, 0);
    g_assert_cmpint(save_link(&source), ==, 0);

    fixture_init(&restored);
    transaction_ops = restored.link.dwc2.transaction.ops;
    g_assert_cmpint(load_link(&restored, 1), ==, 0);
    g_assert_true(restored.link.control.address_pending);
    g_assert_cmpuint(restored.address, ==, 0);
    g_assert_cmpuint(restored.irq_calls, ==, 0);

    result = submit_in(&restored, NULL, 0);
    g_assert_cmpint(result.status, ==, DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(result.actual_length, ==, 0);
    g_assert_cmpuint(restored.address, ==, 42);
    g_assert_cmpuint(restored.link.control.address, ==, 42);
    g_assert_cmpint(dm_usb_control_phase(&restored.link.control), ==,
                    DM_USB_CONTROL_IDLE);
    assert_runtime_wiring_unchanged(
        &restored, &restored.link.control, fixture_control_ops, &restored,
        transaction_ops, &restored.link.dwc2, fixture_irq, &restored, false);
}

static DmUsbControlResult request_level_submit(
    void *opaque, const DmUsbControlRequest *request, const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    size_t *actual_length, uint64_t timestamp_ns)
{
    Dwc2ControlLinkFixture *fixture = opaque;

    (void)timestamp_ns;
    return dm_usb_control_execute_request(
        &fixture->link.control, request, out_data, out_length, in_data,
        in_capacity, actual_length);
}

static void test_request_level_consumer_after_restore(void)
{
    Dwc2ControlLinkFixture source;
    Dwc2ControlLinkFixture restored;
    DmUsbControlRequest request = {
        .request_type = 0x80,
        .request = 6,
        .value = 0x100,
        .length = sizeof(source.descriptor),
    };
    uint8_t data[sizeof(source.descriptor)];
    size_t actual_length = 0;

    fixture_init(&source);
    g_assert_cmpint(save_link(&source), ==, 0);
    fixture_init(&restored);
    g_assert_cmpint(load_link(&restored, 1), ==, 0);
    g_assert_cmpint(request_level_submit(
                        &restored, &request, NULL, 0, data, sizeof(data),
                        &actual_length, 900), ==,
                    DM_USB_CONTROL_ACCEPTED);
    g_assert_cmpuint(actual_length, ==, sizeof(data));
    g_assert_cmpmem(data, sizeof(data), restored.descriptor, sizeof(data));
    g_assert_cmpint(dm_usb_control_phase(&restored.link.control), ==,
                    DM_USB_CONTROL_IDLE);
}

static void capture_runtime_wiring(
    const Dwc2ControlLinkFixture *fixture, DmUsbControlDevice **control,
    DmUsbControlOps *control_ops, void **control_opaque,
    DmUsbTransactionOps *transaction_ops, void **transaction_opaque,
    DmUsbDwc2Irq **irq, void **irq_opaque, bool *irq_level)
{
    *control = fixture->link.dwc2.control;
    *control_ops = fixture->link.control.ops;
    *control_opaque = fixture->link.control.opaque;
    *transaction_ops = fixture->link.dwc2.transaction.ops;
    *transaction_opaque = fixture->link.dwc2.transaction.opaque;
    *irq = fixture->link.dwc2.irq;
    *irq_opaque = fixture->link.dwc2.irq_opaque;
    *irq_level = fixture->link.dwc2.irq_level;
}

static void test_rejects_invalid_control_without_runtime_sync(void)
{
    Dwc2ControlLinkFixture source;
    Dwc2ControlLinkFixture restored;
    DmUsbControlDevice *control;
    DmUsbControlOps control_ops;
    void *control_opaque;
    DmUsbTransactionOps transaction_ops;
    void *transaction_opaque;
    DmUsbDwc2Irq *irq;
    void *irq_opaque;
    bool irq_level;

    prepare_data_in_mid_transfer(&source);
    source.link.control.data_length = 0;
    source.link.control.zero_length_packet = false;
    g_assert_cmpint(save_link(&source), ==, 0);

    fixture_init(&restored);
    restored.link.dwc2.irq_level = true;
    capture_runtime_wiring(&restored, &control, &control_ops, &control_opaque,
                           &transaction_ops, &transaction_opaque, &irq,
                           &irq_opaque, &irq_level);
    g_assert_cmpint(load_link(&restored, 1), !=, 0);
    assert_runtime_wiring_unchanged(&restored, control, control_ops,
                                    control_opaque, transaction_ops,
                                    transaction_opaque, irq, irq_opaque,
                                    irq_level);
    g_assert_cmpuint(restored.irq_calls, ==, 0);
}

static void test_rejects_invalid_fifo_without_runtime_sync(void)
{
    Dwc2ControlLinkFixture source;
    Dwc2ControlLinkFixture restored;
    DmUsbControlDevice *control;
    DmUsbControlOps control_ops;
    void *control_opaque;
    DmUsbTransactionOps transaction_ops;
    void *transaction_opaque;
    DmUsbDwc2Irq *irq;
    void *irq_opaque;
    bool irq_level;

    fixture_init(&source);
    source.link.dwc2.endpoint[1].in_fifo_count = DM_USB_DWC2_FIFO_BYTES + 1;
    g_assert_cmpint(save_link(&source), ==, 0);

    fixture_init(&restored);
    capture_runtime_wiring(&restored, &control, &control_ops, &control_opaque,
                           &transaction_ops, &transaction_opaque, &irq,
                           &irq_opaque, &irq_level);
    g_assert_cmpint(load_link(&restored, 1), !=, 0);
    assert_runtime_wiring_unchanged(&restored, control, control_ops,
                                    control_opaque, transaction_ops,
                                    transaction_opaque, irq, irq_opaque,
                                    irq_level);
    g_assert_cmpuint(restored.irq_calls, ==, 0);
}

static void test_rejects_reversed_children_and_truncated_stream(void)
{
    Dwc2ControlLinkFixture source;
    Dwc2ControlLinkFixture restored;
    QEMUFile *file;

    fixture_init(&source);
    g_assert_cmpint(save_reversed_children(&source), ==, 0);
    fixture_init(&restored);
    g_assert_cmpint(load_link(&restored, 1), !=, 0);
    g_assert_cmpuint(restored.irq_calls, ==, 0);

    file = open_test_file(true);
    qemu_put_byte(file, 0x00);
    qemu_put_byte(file, QEMU_VM_EOF);
    qemu_fclose(file);
    fixture_init(&restored);
    g_assert_cmpint(load_link(&restored, 1), !=, 0);
    g_assert_cmpuint(restored.irq_calls, ==, 0);
}

static void test_rejects_unsupported_version_without_runtime_sync(void)
{
    Dwc2ControlLinkFixture source;
    Dwc2ControlLinkFixture restored;

    fixture_init(&source);
    g_assert_cmpint(save_link(&source), ==, 0);
    fixture_init(&restored);
    g_assert_cmpint(load_link(&restored, 2), !=, 0);
    g_assert_cmpuint(restored.irq_calls, ==, 0);
}

int main(int argc, char **argv)
{
    g_autofree char *temp_file = g_strdup_printf(
        "%s/dm-usb-dwc2-control-link-vmstate.XXXXXX", g_get_tmp_dir());
    int ret;

    temp_fd = mkstemp(temp_file);
    g_assert_cmpint(temp_fd, >=, 0);
    module_call_init(MODULE_INIT_QOM);
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/dwc2-control-link/round-trip-control-fifo",
                    test_round_trip_control_phase_and_fifo);
    g_test_add_func("/dm-usb/dwc2-control-link/round-trip-set-address",
                    test_round_trip_pending_set_address);
    g_test_add_func("/dm-usb/dwc2-control-link/request-level-consumer",
                    test_request_level_consumer_after_restore);
    g_test_add_func("/dm-usb/dwc2-control-link/reject-control",
                    test_rejects_invalid_control_without_runtime_sync);
    g_test_add_func("/dm-usb/dwc2-control-link/reject-fifo",
                    test_rejects_invalid_fifo_without_runtime_sync);
    g_test_add_func("/dm-usb/dwc2-control-link/reject-order-truncated",
                    test_rejects_reversed_children_and_truncated_stream);
    g_test_add_func("/dm-usb/dwc2-control-link/reject-version",
                    test_rejects_unsupported_version_without_runtime_sync);
    ret = g_test_run();
    close(temp_fd);
    unlink(temp_file);
    return ret;
}
