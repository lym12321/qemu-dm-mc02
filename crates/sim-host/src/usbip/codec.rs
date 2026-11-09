// Adapted from jiegec/usbip 0.9.0, commit
// 5561aa4abf372f5d3eb05c65b1474a9489b735f8 (MIT; LICENSE-usbip).
// Keep only wire structs/serializers and plain device metadata. No class
// emulation, host libusb, async server, unchecked network allocations or
// network debug assertions. bcdDevice preserves all original BCD bits.
/// USB/IP protocol version
///
/// This is currently the only supported version of USB/IP
/// for this library.
pub const USBIP_VERSION: u16 = 0x0111;

/// Command code: Retrieve the list of exported USB devices
pub const OP_REQ_DEVLIST: u16 = 0x8005;
/// Command code: import a remote USB device
pub const OP_REQ_IMPORT: u16 = 0x8003;
/// Reply code: The list of exported USB devices
pub const OP_REP_DEVLIST: u16 = 0x0005;
/// Reply code: Reply to import
pub const OP_REP_IMPORT: u16 = 0x0003;

/// Command code: Submit an URB
pub const USBIP_CMD_SUBMIT: u32 = 0x0001;
/// Command code: Unlink an URB
pub const USBIP_CMD_UNLINK: u32 = 0x0002;
/// Reply code: Reply for submitting an URB
pub const USBIP_RET_SUBMIT: u32 = 0x0003;
/// Reply code: Reply for URB unlink
pub const USBIP_RET_UNLINK: u32 = 0x0004;

/// Common header for all context sensitive packets
///
/// All commands/responses which rely on a device being attached
/// to a client use this header.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct UsbIpHeaderBasic {
    pub command: u32,
    pub seqnum: u32,
    pub devid: u32,
    pub direction: u32,
    pub ep: u32,
}

impl UsbIpHeaderBasic {
    /// Converts a byte array into a [UsbIpHeaderBasic].
    pub fn from_bytes(bytes: &[u8; 20]) -> Self {
        UsbIpHeaderBasic {
            command: u32::from_be_bytes(bytes[0..4].try_into().unwrap()),
            seqnum: u32::from_be_bytes(bytes[4..8].try_into().unwrap()),
            devid: u32::from_be_bytes(bytes[8..12].try_into().unwrap()),
            direction: u32::from_be_bytes(bytes[12..16].try_into().unwrap()),
            ep: u32::from_be_bytes(bytes[16..20].try_into().unwrap()),
        }
    }

    /// Converts the [UsbIpHeaderBasic] into a byte array.
    pub fn to_bytes(&self) -> [u8; 20] {
        let mut result = [0u8; 20];
        result[0..4].copy_from_slice(&self.command.to_be_bytes());
        result[4..8].copy_from_slice(&self.seqnum.to_be_bytes());
        result[8..12].copy_from_slice(&self.devid.to_be_bytes());
        result[12..16].copy_from_slice(&self.direction.to_be_bytes());
        result[16..20].copy_from_slice(&self.ep.to_be_bytes());
        result
    }
}
#[derive(Clone, Default)]
pub struct UsbInterface {
    pub interface_class: u8,
    pub interface_subclass: u8,
    pub interface_protocol: u8,
}
#[derive(Clone, Default)]
pub struct UsbDevice {
    pub path: String,
    pub bus_id: String,
    pub bus_num: u32,
    pub dev_num: u32,
    pub speed: u32,
    pub vendor_id: u16,
    pub product_id: u16,
    pub device_bcd: u16,
    pub device_class: u8,
    pub device_subclass: u8,
    pub device_protocol: u8,
    pub configuration_value: u8,
    pub num_configurations: u8,
    pub interfaces: Vec<UsbInterface>,
}
impl UsbDevice {
    pub(crate) fn to_bytes(&self) -> Vec<u8> {
        let mut result = Vec::with_capacity(312);

        let mut path = self.path.as_bytes().to_vec();
        debug_assert!(path.len() <= 256);
        path.resize(256, 0);
        result.extend_from_slice(path.as_slice());

        let mut bus_id = self.bus_id.as_bytes().to_vec();
        debug_assert!(bus_id.len() <= 32);
        bus_id.resize(32, 0);
        result.extend_from_slice(bus_id.as_slice());

        result.extend_from_slice(&self.bus_num.to_be_bytes());
        result.extend_from_slice(&self.dev_num.to_be_bytes());
        result.extend_from_slice(&self.speed.to_be_bytes());
        result.extend_from_slice(&self.vendor_id.to_be_bytes());
        result.extend_from_slice(&self.product_id.to_be_bytes());
        result.extend_from_slice(&self.device_bcd.to_be_bytes());
        result.push(self.device_class);
        result.push(self.device_subclass);
        result.push(self.device_protocol);
        result.push(self.configuration_value);
        result.push(self.num_configurations);
        result.push(self.interfaces.len() as u8);

        result
    }

    pub(crate) fn to_bytes_with_interfaces(&self) -> Vec<u8> {
        let mut result = self.to_bytes();
        result.reserve(4 * self.interfaces.len());

        for intf in &self.interfaces {
            result.push(intf.interface_class);
            result.push(intf.interface_subclass);
            result.push(intf.interface_protocol);
            result.push(0); // padding
        }

        result
    }
}
/// Server side responses from the USB Host
#[derive(Clone)]
pub enum UsbIpResponse {
    OpRepDevlist {
        status: u32,
        device_count: u32,
        devices: Vec<UsbDevice>,
    },
    OpRepImport {
        status: u32,
        device: Option<UsbDevice>,
    },
    UsbIpRetSubmit {
        header: UsbIpHeaderBasic,
        status: u32,
        actual_length: u32,
        start_frame: u32,
        number_of_packets: u32,
        error_count: u32,
        transfer_buffer: Vec<u8>,
        iso_packet_descriptor: Vec<u8>,
    },
    UsbIpRetUnlink {
        header: UsbIpHeaderBasic,
        status: u32,
    },
}

impl UsbIpResponse {
    /// Converts the [UsbIpResponse] into a byte vector
    pub fn to_bytes(&self) -> Vec<u8> {
        match *self {
            Self::OpRepDevlist {
                status,
                device_count,
                ref devices,
            } => {
                let mut result = Vec::with_capacity(
                    12 + devices.len() * 312
                        + devices
                            .iter()
                            .map(|d| d.interfaces.len() * 4)
                            .sum::<usize>(),
                );
                result.extend_from_slice(&USBIP_VERSION.to_be_bytes());
                result.extend_from_slice(&OP_REP_DEVLIST.to_be_bytes());
                result.extend_from_slice(&status.to_be_bytes());
                result.extend_from_slice(&device_count.to_be_bytes());
                for dev in devices {
                    result.extend_from_slice(&dev.to_bytes_with_interfaces());
                }
                result
            }
            Self::OpRepImport { status, ref device } => {
                let mut result = Vec::with_capacity(320);
                result.extend_from_slice(&USBIP_VERSION.to_be_bytes());
                result.extend_from_slice(&OP_REP_IMPORT.to_be_bytes());
                result.extend_from_slice(&status.to_be_bytes());
                if let Some(device) = device {
                    result.extend_from_slice(&device.to_bytes());
                }
                result
            }
            Self::UsbIpRetSubmit {
                ref header,
                status,
                actual_length,
                start_frame,
                number_of_packets,
                error_count,
                ref transfer_buffer,
                ref iso_packet_descriptor,
            } => {
                let mut result =
                    Vec::with_capacity(48 + transfer_buffer.len() + iso_packet_descriptor.len());

                debug_assert!(header.command == USBIP_RET_SUBMIT);
                if header.direction == 1 {
                    debug_assert!(actual_length == transfer_buffer.len() as u32);
                } else {
                    debug_assert!(transfer_buffer.is_empty());
                }

                result.extend_from_slice(&header.to_bytes());
                result.extend_from_slice(&status.to_be_bytes());
                result.extend_from_slice(&actual_length.to_be_bytes());
                result.extend_from_slice(&start_frame.to_be_bytes());
                result.extend_from_slice(&number_of_packets.to_be_bytes());
                result.extend_from_slice(&error_count.to_be_bytes());
                result.extend_from_slice(&[0; 8]);
                result.extend_from_slice(transfer_buffer);
                result.extend_from_slice(iso_packet_descriptor);
                result
            }
            Self::UsbIpRetUnlink { ref header, status } => {
                let mut result = Vec::with_capacity(48);

                debug_assert!(header.command == USBIP_RET_UNLINK);

                result.extend_from_slice(&header.to_bytes());
                result.extend_from_slice(&status.to_be_bytes());
                result.extend_from_slice(&[0; 24]);
                result
            }
        }
    }
}
