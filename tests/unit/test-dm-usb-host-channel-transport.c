/* Tests for the host-channel to transaction boundary. */
#include "qemu/osdep.h"
#include "hw/usb/dm_usb_host_channel_data_path.h"
#include "hw/usb/dm_usb_host_channel_transport.h"
#include "hw/usb/dm_usb_host_qemu_memory.h"

#define FIXTURE_DMA_BASE 0x24000000u

typedef struct TransportFixture {
    DmStm32H7OtgHost host;
    DmUsbHostChannelDataPath data_path;
    DmUsbHostQemuMemory qemu_memory;
    AddressSpace dma_address_space;
    DmUsbHostChannelTransport transport;
    DmUsbControlDevice control;
    DmUsbTransactionDevice device;
    DmUsbTransactionStatus in_status;
    DmUsbTransactionStatus out_status;
    uint8_t host_out[192];
    size_t host_out_length;
    uint8_t device_out[192];
    size_t device_out_length;
    uint8_t device_in[192];
    size_t device_in_length;
    uint8_t host_in[192];
    size_t host_in_length;
    uint8_t dma_memory[192];
    uint8_t device_out_log[192];
    size_t device_out_log_length;
    size_t device_out_call_lengths[8];
    unsigned device_out_calls;
    uint64_t timestamp_ns;
    unsigned submit_calls;
    DmUsbTransactionToken submitted_token;
    size_t submitted_length;
    size_t submitted_capacity;
    DmUsbTransactionResult submitted_result;
    unsigned route_calls;
    uint8_t routed_device_address;
    bool irq_level;
    unsigned irq_changes;
    bool completion_scheduled;
    DmStm32H7OtgHost *completion_host;
    unsigned completion_channel;
    uint64_t completion_token;
    DmStm32H7OtgHostChannelCompletion completion;
    uint32_t completion_actual_length;
} TransportFixture;

static TransportFixture *active_qemu_memory_fixture;

MemTxResult address_space_rw(AddressSpace *address_space, hwaddr address,
                             MemTxAttrs attrs, void *data, hwaddr length,
                             bool is_write)
{
    TransportFixture *fixture = active_qemu_memory_fixture;

    (void)attrs;
    g_assert_true(address_space == &fixture->dma_address_space);
    if (address > sizeof(fixture->dma_memory) ||
        length > sizeof(fixture->dma_memory) - address) {
        return MEMTX_DECODE_ERROR;
    }
    if (is_write) {
        memcpy(&fixture->dma_memory[address], data, length);
    } else {
        memcpy(data, &fixture->dma_memory[address], length);
    }
    return MEMTX_OK;
}

static DmUsbTransactionStatus fixture_in(void *opaque, uint8_t endpoint,
                                         uint8_t *data, size_t capacity,
                                         size_t *length,
                                         uint64_t timestamp_ns)
{
    TransportFixture *fixture = opaque;

    g_assert_cmpuint(endpoint, ==, 1);
    fixture->timestamp_ns = timestamp_ns;
    if (fixture->in_status != DM_USB_TRANSACTION_ACCEPTED) {
        return fixture->in_status;
    }
    g_assert_cmpuint(fixture->device_in_length, <=, capacity);
    memcpy(data, fixture->device_in, fixture->device_in_length);
    *length = fixture->device_in_length;
    return DM_USB_TRANSACTION_ACCEPTED;
}

static DmUsbTransactionStatus fixture_out(void *opaque, uint8_t endpoint,
                                          const uint8_t *data, size_t length,
                                          uint64_t timestamp_ns)
{
    TransportFixture *fixture = opaque;

    g_assert_cmpuint(endpoint, ==, 1);
    fixture->timestamp_ns = timestamp_ns;
    if (fixture->out_status != DM_USB_TRANSACTION_ACCEPTED) {
        return fixture->out_status;
    }
    g_assert_cmpuint(length, <=, sizeof(fixture->device_out));
    memcpy(fixture->device_out, data, length);
    fixture->device_out_length = length;
    g_assert_cmpuint(fixture->device_out_log_length + length, <=,
                     sizeof(fixture->device_out_log));
    memcpy(fixture->device_out_log + fixture->device_out_log_length, data,
           length);
    fixture->device_out_log_length += length;
    g_assert_cmpuint(fixture->device_out_calls, <,
                     G_N_ELEMENTS(fixture->device_out_call_lengths));
    fixture->device_out_call_lengths[fixture->device_out_calls++] = length;
    return DM_USB_TRANSACTION_ACCEPTED;
}

static DmUsbTransactionResult fixture_submit(
    void *opaque, const DmUsbTransaction *transaction)
{
    TransportFixture *fixture = opaque;

    fixture->submit_calls++;
    fixture->submitted_token = transaction->token;
    fixture->submitted_length = transaction->length;
    fixture->submitted_capacity = transaction->capacity;
    fixture->submitted_result = dm_usb_transaction_submit(&fixture->device,
                                                          transaction);
    return fixture->submitted_result;
}

static DmUsbTransactionResult fixture_route(
    void *opaque, uint8_t device_address, const DmUsbTransaction *transaction)
{
    TransportFixture *fixture = opaque;

    fixture->route_calls++;
    fixture->routed_device_address = device_address;
    return fixture_submit(opaque, transaction);
}

static DmUsbTransactionResult fixture_submit_oversized_in(
    void *opaque, const DmUsbTransaction *transaction)
{
    if (transaction->token == DM_USB_TRANSACTION_IN) {
        return (DmUsbTransactionResult) {
            .status = DM_USB_TRANSACTION_ACCEPTED,
            .actual_length = transaction->capacity + 1,
        };
    }
    return fixture_submit(opaque, transaction);
}

static void fixture_irq(void *opaque, bool level)
{
    TransportFixture *fixture = opaque;

    fixture->irq_level = level;
    fixture->irq_changes++;
}

static void fixture_schedule_completion(
    void *opaque, DmStm32H7OtgHost *host, unsigned channel,
    uint64_t completion_token,
    DmStm32H7OtgHostChannelCompletion completion, uint32_t actual_length)
{
    TransportFixture *fixture = opaque;

    fixture->completion_scheduled = true;
    fixture->completion_host = host;
    fixture->completion_channel = channel;
    fixture->completion_token = completion_token;
    fixture->completion = completion;
    fixture->completion_actual_length = actual_length;
}

static bool fixture_read_out(void *opaque, unsigned channel, uint8_t *data,
                             uint32_t length)
{
    TransportFixture *fixture = opaque;

    g_assert_cmpuint(channel, ==, 0);
    if (length > fixture->host_out_length) {
        return false;
    }
    memcpy(data, fixture->host_out, length);
    return true;
}

static bool fixture_write_in(void *opaque, unsigned channel,
                             const uint8_t *data, uint32_t length)
{
    TransportFixture *fixture = opaque;

    g_assert_cmpuint(channel, ==, 0);
    g_assert_cmpuint(length, <=, sizeof(fixture->host_in));
    memcpy(fixture->host_in, data, length);
    fixture->host_in_length = length;
    return true;
}

static bool fixture_dma_read(void *opaque, uint32_t address, uint8_t *data,
                             uint32_t length)
{
    TransportFixture *fixture = opaque;
    uint32_t offset;

    if (address < FIXTURE_DMA_BASE) {
        return false;
    }
    offset = address - FIXTURE_DMA_BASE;
    if (offset > sizeof(fixture->dma_memory) ||
        length > sizeof(fixture->dma_memory) - offset) {
        return false;
    }
    memcpy(data, &fixture->dma_memory[offset], length);
    return true;
}

static bool fixture_dma_write(void *opaque, uint32_t address,
                              const uint8_t *data, uint32_t length)
{
    TransportFixture *fixture = opaque;
    uint32_t offset;

    if (address < FIXTURE_DMA_BASE) {
        return false;
    }
    offset = address - FIXTURE_DMA_BASE;
    if (offset > sizeof(fixture->dma_memory) ||
        length > sizeof(fixture->dma_memory) - offset) {
        return false;
    }
    memcpy(&fixture->dma_memory[offset], data, length);
    return true;
}

static void fixture_enable_port(TransportFixture *fixture)
{
    dm_stm32h7_otg_host_set_port_connected(
        &fixture->host, true, DM_STM32H7_OTG_PORT_FULL_SPEED);
    dm_stm32h7_otg_host_write(&fixture->host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR |
                               DM_STM32H7_OTG_HPRT0_RST, 10);
    dm_stm32h7_otg_host_write(&fixture->host, DM_STM32H7_OTG_HPRT0,
                               DM_STM32H7_OTG_HPRT0_PWR, 20);
}

static void fixture_init(TransportFixture *fixture)
{
    static const DmUsbTransactionOps transaction_ops = {
        .in = fixture_in,
        .out = fixture_out,
    };

    memset(fixture, 0, sizeof(*fixture));
    fixture->in_status = DM_USB_TRANSACTION_ACCEPTED;
    fixture->out_status = DM_USB_TRANSACTION_ACCEPTED;
    dm_usb_control_init(&fixture->control, NULL, NULL, 64);
    dm_usb_transaction_init(&fixture->device, &fixture->control,
                            &transaction_ops, fixture, 64);
    g_assert_cmpint(dm_usb_transaction_configure_endpoint(
                        &fixture->device, 1, DM_USB_ENDPOINT_IN,
                        DM_USB_ENDPOINT_BULK, 64, true), ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpint(dm_usb_transaction_configure_endpoint(
                        &fixture->device, 1, DM_USB_ENDPOINT_OUT,
                        DM_USB_ENDPOINT_BULK, 64, true), ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    dm_stm32h7_otg_host_init(&fixture->host, NULL, NULL, fixture_irq,
                             fixture);
    dm_usb_host_channel_transport_init(
        &fixture->transport, &fixture->host, fixture_submit, fixture_read_out,
        fixture_write_in, fixture);
    fixture_enable_port(fixture);
}

static void fixture_init_with_host_fifo(TransportFixture *fixture)
{
    static const DmUsbTransactionOps transaction_ops = {
        .in = fixture_in,
        .out = fixture_out,
    };

    memset(fixture, 0, sizeof(*fixture));
    fixture->in_status = DM_USB_TRANSACTION_ACCEPTED;
    fixture->out_status = DM_USB_TRANSACTION_ACCEPTED;
    dm_usb_control_init(&fixture->control, NULL, NULL, 64);
    dm_usb_transaction_init(&fixture->device, &fixture->control,
                            &transaction_ops, fixture, 64);
    g_assert_cmpint(dm_usb_transaction_configure_endpoint(
                        &fixture->device, 1, DM_USB_ENDPOINT_IN,
                        DM_USB_ENDPOINT_BULK, 64, true), ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpint(dm_usb_transaction_configure_endpoint(
                        &fixture->device, 1, DM_USB_ENDPOINT_OUT,
                        DM_USB_ENDPOINT_BULK, 64, true), ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    dm_stm32h7_otg_host_init(&fixture->host, NULL, NULL, NULL, NULL);
    dm_usb_host_channel_transport_init_with_opaques(
        &fixture->transport, &fixture->host, fixture_submit, fixture,
        dm_stm32h7_otg_host_read_out_fifo, &fixture->host,
        dm_stm32h7_otg_host_write_in_fifo, &fixture->host);
    fixture_enable_port(fixture);
}

static void fixture_init_with_host_data_path(TransportFixture *fixture)
{
    fixture_init(fixture);
    dm_usb_host_channel_data_path_init(
        &fixture->data_path, &fixture->host, fixture_dma_read,
        fixture_dma_write, fixture);
    dm_usb_host_channel_transport_init_with_opaques(
        &fixture->transport, &fixture->host, fixture_submit, fixture,
        dm_usb_host_channel_data_path_read_out, &fixture->data_path,
        dm_usb_host_channel_data_path_write_in, &fixture->data_path);
}

static void fixture_init_with_host_qemu_memory(TransportFixture *fixture)
{
    fixture_init(fixture);
    dm_usb_host_qemu_memory_init(&fixture->qemu_memory,
                                 &fixture->dma_address_space);
    dm_usb_host_channel_data_path_init(
        &fixture->data_path, &fixture->host, dm_usb_host_qemu_memory_read,
        dm_usb_host_qemu_memory_write, &fixture->qemu_memory);
    dm_usb_host_channel_transport_init_with_opaques(
        &fixture->transport, &fixture->host, fixture_submit, fixture,
        dm_usb_host_channel_data_path_read_out, &fixture->data_path,
        dm_usb_host_channel_data_path_write_in, &fixture->data_path);
}

static void fixture_start_channel(TransportFixture *fixture, uint32_t hctsiz,
                                  uint32_t hcchar, uint64_t timestamp_ns)
{
    dm_stm32h7_otg_host_write(&fixture->host, DM_STM32H7_OTG_HCTSIZ(0),
                               hctsiz, timestamp_ns);
    dm_stm32h7_otg_host_write(&fixture->host, DM_STM32H7_OTG_HCCHAR(0),
                               hcchar | DM_STM32H7_OTG_HCCHAR_CHENA,
                               timestamp_ns);
}

static void test_bulk_out_exact_data(void)
{
    TransportFixture fixture;
    uint32_t hctsiz = 3 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;

    fixture_init(&fixture);
    memcpy(fixture.host_out, "OUT", 3);
    fixture.host_out_length = 3;
    fixture_start_channel(&fixture, hctsiz, hcchar, 1234);

    g_assert_cmpuint(fixture.device_out_length, ==, 3);
    g_assert_cmpmem(fixture.device_out, fixture.device_out_length,
                    fixture.host_out, fixture.host_out_length);
    g_assert_cmpuint(fixture.timestamp_ns, ==, 1234);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_XFRC |
                     DM_STM32H7_OTG_HCINT_CHHLTD);
}

static void test_legacy_submit_keeps_single_target(void)
{
    TransportFixture fixture;
    uint32_t hctsiz = 3 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = (42u << DM_STM32H7_OTG_HCCHAR_DEVADDR_SHIFT) |
                      (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;

    fixture_init(&fixture);
    memcpy(fixture.host_out, "OUT", 3);
    fixture.host_out_length = 3;
    fixture_start_channel(&fixture, hctsiz, hcchar, 1234);

    g_assert_cmpuint(fixture.submit_calls, ==, 1);
    g_assert_cmpuint(fixture.route_calls, ==, 0);
    g_assert_cmpmem(fixture.device_out, fixture.device_out_length,
                    fixture.host_out, fixture.host_out_length);
}

static void test_route_receives_device_address(void)
{
    TransportFixture fixture;
    uint32_t hctsiz = 3 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = (37u << DM_STM32H7_OTG_HCCHAR_DEVADDR_SHIFT) |
                      (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;

    fixture_init(&fixture);
    dm_usb_host_channel_transport_set_route(&fixture.transport, fixture_route,
                                            &fixture);
    memcpy(fixture.host_out, "OUT", 3);
    fixture.host_out_length = 3;
    fixture_start_channel(&fixture, hctsiz, hcchar, 1234);

    g_assert_cmpuint(fixture.route_calls, ==, 1);
    g_assert_cmpuint(fixture.submit_calls, ==, 1);
    g_assert_cmpuint(fixture.routed_device_address, ==, 37);
    g_assert_cmpmem(fixture.device_out, fixture.device_out_length,
                    fixture.host_out, fixture.host_out_length);
}

static void test_bulk_in_short_packet(void)
{
    TransportFixture fixture;
    uint32_t hctsiz = 16 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      DM_STM32H7_OTG_HCCHAR_EPDIR |
                      (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;

    fixture_init(&fixture);
    memcpy(fixture.device_in, "PONG", 4);
    fixture.device_in_length = 4;
    fixture_start_channel(&fixture, hctsiz, hcchar, 5678);

    g_assert_cmpuint(fixture.host_in_length, ==, 4);
    g_assert_cmpmem(fixture.host_in, fixture.host_in_length,
                    fixture.device_in, fixture.device_in_length);
    g_assert_cmpuint(fixture.timestamp_ns, ==, 5678);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCTSIZ(0)), ==, 12);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_XFRC |
                     DM_STM32H7_OTG_HCINT_CHHLTD);
}

static void test_setup_pid_on_bulk_uses_hcchar_direction(void)
{
    TransportFixture fixture;
    uint32_t hctsiz = 3 |
                      (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT) |
                      (DM_STM32H7_OTG_HOST_PID_SETUP <<
                       DM_STM32H7_OTG_HCTSIZ_PID_SHIFT);
    uint32_t hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;

    fixture_init(&fixture);
    memcpy(fixture.host_out, "OUT", 3);
    fixture.host_out_length = 3;
    fixture_start_channel(&fixture, hctsiz, hcchar, 100);

    g_assert_cmpuint(fixture.submit_calls, ==, 1);
    g_assert_cmpint(fixture.submitted_token, ==, DM_USB_TRANSACTION_OUT);
    g_assert_cmpuint(fixture.submitted_length, ==, 3);
    g_assert_cmpuint(fixture.submitted_capacity, ==, 0);
    g_assert_cmpint(fixture.submitted_result.status, ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(fixture.submitted_result.actual_length, ==, 3);
    g_assert_cmpmem(fixture.device_out, fixture.device_out_length,
                    fixture.host_out, fixture.host_out_length);

    fixture_init(&fixture);
    memcpy(fixture.host_out, "IN?", 3);
    fixture.host_out_length = 3;
    memcpy(fixture.device_in, "IN!", 3);
    fixture.device_in_length = 3;
    fixture_start_channel(&fixture, hctsiz,
                          hcchar | DM_STM32H7_OTG_HCCHAR_EPDIR, 200);

    g_assert_cmpuint(fixture.submit_calls, ==, 1);
    g_assert_cmpint(fixture.submitted_token, ==, DM_USB_TRANSACTION_IN);
    g_assert_cmpuint(fixture.submitted_length, ==, 0);
    g_assert_cmpuint(fixture.submitted_capacity, ==, 3);
    g_assert_cmpint(fixture.submitted_result.status, ==,
                    DM_USB_TRANSACTION_ACCEPTED);
    g_assert_cmpuint(fixture.submitted_result.actual_length, ==, 3);
    g_assert_cmpmem(fixture.host_in, fixture.host_in_length,
                    fixture.device_in, fixture.device_in_length);
}

static void test_deferred_completion_boundary(void)
{
    TransportFixture fixture;
    uint32_t hctsiz = 4 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      DM_STM32H7_OTG_HCCHAR_EPDIR |
                      (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;
    uint32_t completion_mask = DM_STM32H7_OTG_HCINT_XFRC |
                                DM_STM32H7_OTG_HCINT_CHHLTD;

    fixture_init(&fixture);
    dm_usb_host_channel_transport_set_completion_scheduler(
        &fixture.transport, fixture_schedule_completion, &fixture);
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_GINTMSK,
                               DM_STM32H7_OTG_GINTSTS_HCINT, 0);
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_HAINTMSK, 1, 0);
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_HCINTMSK(0),
                               completion_mask, 0);
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_GINT, 0);
    memcpy(fixture.device_in, "PONG", 4);
    fixture.device_in_length = 4;

    fixture_start_channel(&fixture, hctsiz, hcchar, 900);

    g_assert_true(fixture.completion_scheduled);
    g_assert_true(fixture.host.channel[0].waiting_completion);
    g_assert_cmpuint(fixture.completion_channel, ==, 0);
    g_assert_true(fixture.completion_host == &fixture.host);
    g_assert_cmpuint(fixture.completion_token, !=, 0);
    g_assert_cmpint(fixture.completion, ==,
                    DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED);
    g_assert_cmpuint(fixture.completion_actual_length, ==, 4);
    g_assert_cmpmem(fixture.host_in, fixture.host_in_length, "PONG", 4);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCINT(0)), ==, 0);
    g_assert_false(fixture.irq_level);

    dm_stm32h7_otg_host_complete_channel_with_token(
        fixture.completion_host, fixture.completion_channel,
        fixture.completion_token, fixture.completion,
        fixture.completion_actual_length);

    g_assert_false(fixture.host.channel[0].waiting_completion);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     completion_mask);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HAINT), ==, 1);
    g_assert_true(dm_stm32h7_otg_host_read(
                      &fixture.host, DM_STM32H7_OTG_GINTSTS) &
                  DM_STM32H7_OTG_GINTSTS_HCINT);
    g_assert_true(fixture.irq_level);
}

static void test_stale_deferred_completion_is_rejected(void)
{
    TransportFixture fixture;
    uint64_t stale_token;
    uint32_t hctsiz = 4 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t second_hctsiz = 4 |
                            (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT) |
                            (DM_STM32H7_OTG_HOST_PID_DATA1 <<
                             DM_STM32H7_OTG_HCTSIZ_PID_SHIFT);
    uint32_t hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      DM_STM32H7_OTG_HCCHAR_EPDIR |
                      (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;

    fixture_init(&fixture);
    dm_usb_host_channel_transport_set_completion_scheduler(
        &fixture.transport, fixture_schedule_completion, &fixture);
    memcpy(fixture.device_in, "OLD!", 4);
    fixture.device_in_length = 4;
    fixture_start_channel(&fixture, hctsiz, hcchar, 100);
    stale_token = fixture.completion_token;

    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_HCCHAR(0),
                               DM_STM32H7_OTG_HCCHAR_CHDIS, 110);
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_HCINT(0),
                               DM_STM32H7_OTG_HCINT_VALID_MASK, 110);
    memcpy(fixture.device_in, "NEW!", 4);
    fixture_start_channel(&fixture, second_hctsiz, hcchar, 120);
    g_assert_cmpuint(fixture.completion_token, !=, stale_token);
    g_assert_true(fixture.host.channel[0].waiting_completion);

    dm_stm32h7_otg_host_complete_channel_with_token(
        &fixture.host, 0, stale_token,
        DM_STM32H7_OTG_HOST_CHANNEL_ACCEPTED, 4);
    g_assert_true(fixture.host.channel[0].waiting_completion);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCINT(0)), ==, 0);

    dm_stm32h7_otg_host_complete_channel_with_token(
        &fixture.host, 0, fixture.completion_token,
        fixture.completion, fixture.completion_actual_length);
    g_assert_false(fixture.host.channel[0].waiting_completion);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_XFRC |
                     DM_STM32H7_OTG_HCINT_CHHLTD);
}

static void test_nak_then_stall(void)
{
    TransportFixture fixture;
    uint32_t hctsiz = 3 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;

    fixture_init(&fixture);
    memcpy(fixture.host_out, "OUT", 3);
    fixture.host_out_length = 3;
    fixture.out_status = DM_USB_TRANSACTION_NAK;
    fixture_start_channel(&fixture, hctsiz, hcchar, 1);
    g_assert_true(dm_stm32h7_otg_host_read(
                          &fixture.host, DM_STM32H7_OTG_HCCHAR(0)) &
                  DM_STM32H7_OTG_HCCHAR_CHENA);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_NAK);

    fixture.out_status = DM_USB_TRANSACTION_STALL;
    g_assert_true(dm_stm32h7_otg_host_service_channel(&fixture.host, 0, 2));
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCCHAR(0)) &
                     DM_STM32H7_OTG_HCCHAR_CHENA, ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_NAK |
                     DM_STM32H7_OTG_HCINT_STALL |
                     DM_STM32H7_OTG_HCINT_CHHLTD);
}

static void test_host_pio_fifo_data_boundary(void)
{
    TransportFixture fixture;
    uint32_t out_hctsiz = 3 |
                          (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t in_hctsiz = 16 |
                         (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t out_hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                          (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;
    uint32_t in_hcchar = out_hcchar | DM_STM32H7_OTG_HCCHAR_EPDIR;

    fixture_init_with_host_fifo(&fixture);
    dm_stm32h7_otg_host_fifo_write(&fixture.host, 0, 0x0054554f, 3);
    fixture_start_channel(&fixture, out_hctsiz, out_hcchar, 100);
    g_assert_cmpuint(fixture.device_out_length, ==, 3);
    g_assert_cmpmem(fixture.device_out, fixture.device_out_length, "OUT", 3);

    memcpy(fixture.device_in, "PONG", 4);
    fixture.device_in_length = 4;
    fixture_start_channel(&fixture, in_hctsiz, in_hcchar, 200);
    g_assert_cmphex(dm_stm32h7_otg_host_fifo_read(&fixture.host, 0, 4), ==,
                    0x474e4f50);
    g_assert_cmphex(dm_stm32h7_otg_host_fifo_read(&fixture.host, 0, 4), ==, 0);
}

static void test_host_pio_fifo_full_is_transaction_error(void)
{
    TransportFixture fixture;
    uint32_t hctsiz = 4 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      DM_STM32H7_OTG_HCCHAR_EPDIR |
                      (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;
    uint8_t zero[4] = { 0 };
    unsigned index;

    fixture_init_with_host_fifo(&fixture);
    for (index = 0; index < DM_USB_HOST_PIO_FIFO_CAPACITY / 4; ++index) {
        g_assert_true(dm_stm32h7_otg_host_write_in_fifo(&fixture.host, 0,
                                                         zero, sizeof(zero)));
    }
    memcpy(fixture.device_in, "PONG", 4);
    fixture.device_in_length = 4;
    fixture_start_channel(&fixture, hctsiz, hcchar, 300);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_XACTERR |
                     DM_STM32H7_OTG_HCINT_CHHLTD);
}

static void test_host_dma_data_path_boundary(void)
{
    TransportFixture fixture;
    uint32_t out_hctsiz = 3 |
                          (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t in_hctsiz = 16 |
                         (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t out_hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                          (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;
    uint32_t in_hcchar = out_hcchar | DM_STM32H7_OTG_HCCHAR_EPDIR;

    fixture_init_with_host_data_path(&fixture);
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_DMAEN, 0);
    dm_stm32h7_otg_host_fifo_write(&fixture.host, 0, 0x00444142, 3);
    memcpy(fixture.dma_memory, "OUT", 3);
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_HCDMA(0),
                               FIXTURE_DMA_BASE, 0);
    fixture_start_channel(&fixture, out_hctsiz, out_hcchar, 100);
    g_assert_cmpmem(fixture.device_out, fixture.device_out_length, "OUT", 3);
    g_assert_cmpuint(dm_usb_host_pio_fifo_count(&fixture.host.out_fifo[0]), ==,
                     3);
    g_assert_cmphex(dm_stm32h7_otg_host_read(&fixture.host,
                                              DM_STM32H7_OTG_HCDMA(0)), ==,
                    FIXTURE_DMA_BASE + 3);

    memcpy(fixture.device_in, "PONG", 4);
    fixture.device_in_length = 4;
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_HCDMA(0),
                               FIXTURE_DMA_BASE + 16, 0);
    fixture_start_channel(&fixture, in_hctsiz, in_hcchar, 200);
    g_assert_cmpmem(&fixture.dma_memory[16], 4, "PONG", 4);
    g_assert_cmpuint(dm_usb_host_pio_fifo_count(&fixture.host.in_fifo[0]), ==,
                     0);
    g_assert_cmphex(dm_stm32h7_otg_host_read(&fixture.host,
                                              DM_STM32H7_OTG_HCDMA(0)), ==,
                    FIXTURE_DMA_BASE + 20);
}

static void test_host_pio_multi_packet_continuation(void)
{
    TransportFixture fixture;
    uint8_t expected[130];
    uint32_t hctsiz = sizeof(expected) |
                      (3u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;
    unsigned index;

    fixture_init_with_host_fifo(&fixture);
    for (index = 0; index < sizeof(expected); ++index) {
        expected[index] = index;
    }
    for (index = 0; index < sizeof(expected); index += 4) {
        unsigned size = sizeof(expected) - index;
        uint32_t word = 0;
        unsigned byte;

        if (size > 4) {
            size = 4;
        }
        for (byte = 0; byte < size; ++byte) {
            word |= (uint32_t)expected[index + byte] << (8 * byte);
        }
        dm_stm32h7_otg_host_fifo_write(&fixture.host, 0, word, size);
    }

    fixture_start_channel(&fixture, hctsiz, hcchar, 100);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCTSIZ(0)), ==,
                     66 | (2u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT));
    g_assert_true(dm_stm32h7_otg_host_service_channel(&fixture.host, 0, 200));
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCTSIZ(0)), ==,
                     2 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT));
    g_assert_true(dm_stm32h7_otg_host_service_channel(&fixture.host, 0, 300));

    g_assert_cmpuint(fixture.device_out_calls, ==, 3);
    g_assert_cmpuint(fixture.device_out_call_lengths[0], ==, 64);
    g_assert_cmpuint(fixture.device_out_call_lengths[1], ==, 64);
    g_assert_cmpuint(fixture.device_out_call_lengths[2], ==, 2);
    g_assert_cmpuint(fixture.device_out_log_length, ==, sizeof(expected));
    g_assert_cmpmem(fixture.device_out_log, sizeof(expected), expected,
                    sizeof(expected));
    g_assert_cmpuint(dm_usb_host_pio_fifo_count(&fixture.host.out_fifo[0]), ==,
                     0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCTSIZ(0)), ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCCHAR(0)) &
                     DM_STM32H7_OTG_HCCHAR_CHENA, ==, 0);
}

static void test_host_dma_multi_packet_continuation(void)
{
    TransportFixture fixture;
    uint8_t expected[130];
    uint32_t hctsiz = sizeof(expected) |
                      (3u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;
    unsigned index;

    fixture_init_with_host_data_path(&fixture);
    for (index = 0; index < sizeof(expected); ++index) {
        expected[index] = index ^ 0x5a;
    }
    memcpy(fixture.dma_memory, expected, sizeof(expected));
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_DMAEN, 0);
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_HCDMA(0),
                               FIXTURE_DMA_BASE, 0);

    fixture_start_channel(&fixture, hctsiz, hcchar, 400);
    g_assert_cmphex(dm_stm32h7_otg_host_read(
                        &fixture.host, DM_STM32H7_OTG_HCDMA(0)), ==,
                    FIXTURE_DMA_BASE + 64);
    g_assert_true(dm_stm32h7_otg_host_service_channel(&fixture.host, 0, 500));
    g_assert_cmphex(dm_stm32h7_otg_host_read(
                        &fixture.host, DM_STM32H7_OTG_HCDMA(0)), ==,
                    FIXTURE_DMA_BASE + 128);
    g_assert_true(dm_stm32h7_otg_host_service_channel(&fixture.host, 0, 600));

    g_assert_cmpuint(fixture.device_out_calls, ==, 3);
    g_assert_cmpuint(fixture.device_out_call_lengths[0], ==, 64);
    g_assert_cmpuint(fixture.device_out_call_lengths[1], ==, 64);
    g_assert_cmpuint(fixture.device_out_call_lengths[2], ==, 2);
    g_assert_cmpuint(fixture.device_out_log_length, ==, sizeof(expected));
    g_assert_cmpmem(fixture.device_out_log, sizeof(expected), expected,
                    sizeof(expected));
    g_assert_cmphex(dm_stm32h7_otg_host_read(
                        &fixture.host, DM_STM32H7_OTG_HCDMA(0)), ==,
                    FIXTURE_DMA_BASE + sizeof(expected));
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCTSIZ(0)), ==, 0);
}

static void test_host_qemu_memory_data_path_boundary(void)
{
    TransportFixture fixture;
    uint32_t out_hctsiz = 3 |
                          (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t in_hctsiz = 16 |
                         (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t out_hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                          (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;
    uint32_t in_hcchar = out_hcchar | DM_STM32H7_OTG_HCCHAR_EPDIR;

    fixture_init_with_host_qemu_memory(&fixture);
    active_qemu_memory_fixture = &fixture;
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_GAHBCFG,
                               DM_STM32H7_OTG_GAHBCFG_DMAEN, 0);
    memcpy(fixture.dma_memory, "OUT", 3);
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_HCDMA(0), 0, 0);
    fixture_start_channel(&fixture, out_hctsiz, out_hcchar, 100);
    g_assert_cmpmem(fixture.device_out, fixture.device_out_length, "OUT", 3);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&fixture.host,
                                               DM_STM32H7_OTG_HCDMA(0)), ==, 3);

    memcpy(fixture.device_in, "PONG", 4);
    fixture.device_in_length = 4;
    dm_stm32h7_otg_host_write(&fixture.host, DM_STM32H7_OTG_HCDMA(0), 16, 0);
    fixture_start_channel(&fixture, in_hctsiz, in_hcchar, 200);
    g_assert_cmpmem(&fixture.dma_memory[16], 4, "PONG", 4);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(&fixture.host,
                                               DM_STM32H7_OTG_HCDMA(0)), ==, 20);
    active_qemu_memory_fixture = NULL;
}

static void test_oversized_in_result_is_transaction_error(void)
{
    TransportFixture fixture;
    uint32_t hctsiz = 4 | (1u << DM_STM32H7_OTG_HCTSIZ_PKTCNT_SHIFT);
    uint32_t hcchar = (2u << DM_STM32H7_OTG_HCCHAR_EPTYPE_SHIFT) |
                      DM_STM32H7_OTG_HCCHAR_EPDIR |
                      (1u << DM_STM32H7_OTG_HCCHAR_EPNUM_SHIFT) | 64;

    fixture_init(&fixture);
    dm_usb_host_channel_transport_init(
        &fixture.transport, &fixture.host, fixture_submit_oversized_in,
        fixture_read_out, fixture_write_in, &fixture);
    fixture_start_channel(&fixture, hctsiz, hcchar, 400);
    g_assert_cmpuint(fixture.host_in_length, ==, 0);
    g_assert_cmpuint(dm_stm32h7_otg_host_read(
                         &fixture.host, DM_STM32H7_OTG_HCINT(0)), ==,
                     DM_STM32H7_OTG_HCINT_XACTERR |
                     DM_STM32H7_OTG_HCINT_CHHLTD);
}

int main(int argc, char **argv)
{
    g_test_init(&argc, &argv, NULL);
    g_test_add_func("/dm-usb/host-channel-transport/bulk-out-exact-data",
                    test_bulk_out_exact_data);
    g_test_add_func("/dm-usb/host-channel-transport/legacy-single-target",
                    test_legacy_submit_keeps_single_target);
    g_test_add_func("/dm-usb/host-channel-transport/device-address-route",
                    test_route_receives_device_address);
    g_test_add_func("/dm-usb/host-channel-transport/bulk-in-short-packet",
                    test_bulk_in_short_packet);
    g_test_add_func("/dm-usb/host-channel-transport/setup-pid-on-bulk-uses-hcchar-direction",
                    test_setup_pid_on_bulk_uses_hcchar_direction);
    g_test_add_func("/dm-usb/host-channel-transport/deferred-completion",
                    test_deferred_completion_boundary);
    g_test_add_func("/dm-usb/host-channel-transport/stale-deferred-completion",
                    test_stale_deferred_completion_is_rejected);
    g_test_add_func("/dm-usb/host-channel-transport/nak-then-stall",
                    test_nak_then_stall);
    g_test_add_func("/dm-usb/host-channel-transport/host-pio-fifo-boundary",
                    test_host_pio_fifo_data_boundary);
    g_test_add_func("/dm-usb/host-channel-transport/host-pio-fifo-full",
                    test_host_pio_fifo_full_is_transaction_error);
    g_test_add_func("/dm-usb/host-channel-transport/host-dma-data-path",
                    test_host_dma_data_path_boundary);
    g_test_add_func("/dm-usb/host-channel-transport/host-pio-multi-packet",
                    test_host_pio_multi_packet_continuation);
    g_test_add_func("/dm-usb/host-channel-transport/host-dma-multi-packet",
                    test_host_dma_multi_packet_continuation);
    g_test_add_func("/dm-usb/host-channel-transport/host-qemu-memory-data-path",
                    test_host_qemu_memory_data_path_boundary);
    g_test_add_func("/dm-usb/host-channel-transport/oversized-in-result",
                    test_oversized_in_result_is_transaction_error);
    return g_test_run();
}
