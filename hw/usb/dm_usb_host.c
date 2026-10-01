/* Board-independent synthetic USB upstream host. */
#include "hw/usb/dm_usb_host.h"

static DmUsbHostResult host_result(DmUsbTransactionStatus status,
                                    size_t actual_length,
                                    unsigned transactions)
{
    return (DmUsbHostResult) {
        .status = status,
        .actual_length = actual_length,
        .transactions = transactions,
    };
}

static DmUsbHostResult host_invalid(void)
{
    return host_result(DM_USB_TRANSACTION_INVALID, 0, 0);
}

static DmUsbHostResult host_submit(DmUsbHost *host,
                                   const DmUsbTransaction *transaction)
{
    DmUsbTransactionResult result;

    if (!host || !host->submit || !transaction) {
        return host_invalid();
    }
    result = host->submit(host->opaque, transaction);
    if (result.status == DM_USB_TRANSACTION_ACCEPTED &&
        transaction->token == DM_USB_TRANSACTION_IN &&
        result.actual_length > transaction->capacity) {
        return host_invalid();
    }
    return host_result(result.status, result.actual_length, 1);
}

static DmUsbHostResult host_in_transfer(
    DmUsbHost *host, uint8_t endpoint, uint16_t max_packet_size,
    uint8_t *data, size_t capacity, uint64_t timestamp_ns)
{
    size_t offset = 0;
    unsigned transactions = 0;

    if (!host || !host->submit || !max_packet_size ||
        (capacity && !data)) {
        return host_invalid();
    }
    do {
        DmUsbTransaction transaction = {
            .token = DM_USB_TRANSACTION_IN,
            .pid = DM_USB_TRANSACTION_PID_AUTO,
            .endpoint = endpoint,
            .in_data = capacity - offset ? data + offset : NULL,
            .capacity = capacity - offset < max_packet_size ?
                        capacity - offset : max_packet_size,
            .timestamp_ns = timestamp_ns,
        };
        DmUsbTransactionResult result = host->submit(host->opaque,
                                                     &transaction);

        ++transactions;
        if (result.status != DM_USB_TRANSACTION_ACCEPTED) {
            return host_result(result.status, offset, transactions);
        }
        if (result.actual_length > transaction.capacity) {
            return host_result(DM_USB_TRANSACTION_INVALID, offset,
                               transactions);
        }
        offset += result.actual_length;
        if (result.actual_length < max_packet_size || offset == capacity) {
            break;
        }
    } while (true);
    return host_result(DM_USB_TRANSACTION_ACCEPTED, offset, transactions);
}

static DmUsbHostResult host_out_transfer(
    DmUsbHost *host, uint8_t endpoint, uint16_t max_packet_size,
    const uint8_t *data, size_t length, uint64_t timestamp_ns)
{
    size_t offset = 0;
    unsigned transactions = 0;

    if (!host || !host->submit || !max_packet_size ||
        (length && !data)) {
        return host_invalid();
    }
    do {
        size_t packet_length = length - offset < max_packet_size ?
                               length - offset : max_packet_size;
        DmUsbTransaction transaction = {
            .token = DM_USB_TRANSACTION_OUT,
            .pid = DM_USB_TRANSACTION_PID_AUTO,
            .endpoint = endpoint,
            .out_data = packet_length ? data + offset : NULL,
            .length = packet_length,
            .timestamp_ns = timestamp_ns,
        };
        DmUsbTransactionResult result = host->submit(host->opaque,
                                                     &transaction);

        ++transactions;
        if (result.status != DM_USB_TRANSACTION_ACCEPTED) {
            return host_result(result.status, offset, transactions);
        }
        if (result.actual_length != packet_length) {
            return host_result(DM_USB_TRANSACTION_INVALID, offset,
                               transactions);
        }
        offset += packet_length;
        if (offset == length) {
            break;
        }
    } while (true);
    return host_result(DM_USB_TRANSACTION_ACCEPTED, offset, transactions);
}

void dm_usb_host_init(DmUsbHost *host,
                      DmUsbHostSubmitTransaction *submit,
                      void *opaque, uint16_t ep0_max_packet_size)
{
    if (!host) {
        return;
    }
    *host = (DmUsbHost) {
        .submit = submit,
        .opaque = opaque,
        .ep0_max_packet_size = ep0_max_packet_size ? ep0_max_packet_size : 64,
    };
}

DmUsbHostResult dm_usb_host_control_transfer(
    DmUsbHost *host, const uint8_t setup[8], const uint8_t *out_data,
    size_t out_length, uint8_t *in_data, size_t in_capacity,
    uint64_t timestamp_ns)
{
    DmUsbHostResult result;
    DmUsbTransaction transaction;
    bool direction_in;
    uint16_t requested_length;
    size_t offset = 0;
    unsigned transactions = 0;

    if (!host || !host->submit || !setup ||
        (in_capacity && !in_data)) {
        return host_invalid();
    }
    direction_in = setup[0] & 0x80;
    requested_length = setup[6] | ((uint16_t)setup[7] << 8);
    if (direction_in) {
        if (out_length || out_data || in_capacity < requested_length) {
            return host_invalid();
        }
    } else if (out_length != requested_length ||
               (out_length && !out_data) || in_capacity || in_data) {
        return host_invalid();
    }

    transaction = (DmUsbTransaction) {
        .token = DM_USB_TRANSACTION_SETUP,
        .pid = DM_USB_TRANSACTION_PID_AUTO,
        .endpoint = 0,
        .out_data = setup,
        .length = 8,
        .timestamp_ns = timestamp_ns,
    };
    result = host_submit(host, &transaction);
    if (result.status != DM_USB_TRANSACTION_ACCEPTED) {
        return result;
    }
    transactions = result.transactions;

    if (direction_in) {
        while (offset < requested_length) {
            size_t packet_capacity = requested_length - offset <
                                      host->ep0_max_packet_size ?
                                      requested_length - offset :
                                      host->ep0_max_packet_size;

            transaction = (DmUsbTransaction) {
                .token = DM_USB_TRANSACTION_IN,
                .pid = DM_USB_TRANSACTION_PID_AUTO,
                .endpoint = 0,
                .in_data = in_data + offset,
                .capacity = packet_capacity,
                .timestamp_ns = timestamp_ns,
            };
            result = host_submit(host, &transaction);
            ++transactions;
            if (result.status != DM_USB_TRANSACTION_ACCEPTED) {
                return host_result(result.status, offset, transactions);
            }
            if (result.actual_length > packet_capacity) {
                return host_result(DM_USB_TRANSACTION_INVALID, offset,
                                   transactions);
            }
            offset += result.actual_length;
            if (result.actual_length < host->ep0_max_packet_size) {
                break;
            }
        }
        transaction = (DmUsbTransaction) {
            .token = DM_USB_TRANSACTION_OUT,
            .pid = DM_USB_TRANSACTION_PID_AUTO,
            .endpoint = 0,
            .timestamp_ns = timestamp_ns,
        };
    } else {
        if (out_length) {
            result = host_out_transfer(host, 0, host->ep0_max_packet_size,
                                       out_data, out_length, timestamp_ns);
            transactions += result.transactions;
            if (result.status != DM_USB_TRANSACTION_ACCEPTED) {
                return host_result(result.status, result.actual_length,
                                   transactions);
            }
            offset = result.actual_length;
        }
        transaction = (DmUsbTransaction) {
            .token = DM_USB_TRANSACTION_IN,
            .pid = DM_USB_TRANSACTION_PID_AUTO,
            .endpoint = 0,
            .timestamp_ns = timestamp_ns,
        };
    }

    result = host_submit(host, &transaction);
    ++transactions;
    if (result.status != DM_USB_TRANSACTION_ACCEPTED) {
        return host_result(result.status, offset, transactions);
    }
    return host_result(DM_USB_TRANSACTION_ACCEPTED, offset, transactions);
}

DmUsbHostResult dm_usb_host_bulk_out(
    DmUsbHost *host, uint8_t endpoint, uint16_t max_packet_size,
    const uint8_t *data, size_t length, uint64_t timestamp_ns)
{
    return host_out_transfer(host, endpoint, max_packet_size, data, length,
                             timestamp_ns);
}

DmUsbHostResult dm_usb_host_bulk_in(
    DmUsbHost *host, uint8_t endpoint, uint16_t max_packet_size,
    uint8_t *data, size_t capacity, uint64_t timestamp_ns)
{
    return host_in_transfer(host, endpoint, max_packet_size, data, capacity,
                            timestamp_ns);
}
