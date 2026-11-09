//! One Linux SocketCAN owner attached to the existing CAN fabric.
//! RX timestamps are the sampled Simulation cut, never a host wall clock.

use crate::{
    can::EndpointId,
    runtime::{CanAttachment, CanConnection, HostContext, HostEvent, HostInput, HostOptions},
    transports::{
        fixed84::CanFrameSink,
        socketcan::{validate_interface, NativeCanSocket, SocketCanTransport},
    },
};
use sim_core::{
    BuildContext, Capability, Component, Factory, NodeDescription, NodeSpec, PortSpec, Result,
    Sampler,
};
use std::{cell::RefCell, rc::Rc};
#[derive(Clone, Debug)]
pub struct SocketCanOptions {
    pub interface: String,
    pub enable_fd: bool,
    pub max_pending: usize,
    pub io_budget: usize,
}
impl SocketCanOptions {
    pub fn new(interface: impl Into<String>) -> Self {
        Self {
            interface: interface.into(),
            enable_fd: true,
            max_pending: 256,
            io_budget: 64,
        }
    }
    pub fn validate(&self) -> Result<()> {
        validate_interface(&self.interface).map_err(lower)?;
        if !(1..=65536).contains(&self.max_pending) || !(1..=4096).contains(&self.io_budget) {
            return Err(std::sync::Arc::new(sim_core::Error::Admission(
                "SocketCAN requires max_pending1..65536 and io_budget1..4096".into(),
            )));
        }
        Ok(())
    }
}
type Transport = Rc<RefCell<Option<SocketCanTransport<NativeCanSocket>>>>;
pub struct SocketCan {
    transport: Transport,
    connection: CanConnection,
    io_budget: usize,
}
impl SocketCan {
    pub fn pending(&self) -> usize {
        self.transport
            .borrow()
            .as_ref()
            .map_or(0, SocketCanTransport::pending)
    }
}
impl Sampler<HostInput, HostEvent> for SocketCan {
    fn sample(&mut self, cut: u64, context: &HostContext) -> Result<()> {
        let result = (|| {
            context.check_io()?;
            context.check_deadline()?;
            self.transport
                .borrow_mut()
                .as_mut()
                .expect("active component")
                .flush()
                .map_err(lower)?;
            for _ in 0..self.io_budget {
                context.check_io()?;
                context.check_deadline()?;
                let frame = self
                    .transport
                    .borrow_mut()
                    .as_mut()
                    .expect("active component")
                    .receive_once(cut)
                    .map_err(lower)?;
                let Some(frame) = frame else {
                    break;
                };
                self.connection.send(frame)?;
            }
            context.check_io()?;
            context.check_deadline()
        })();
        result.map_err(|error| context.fail(error))
    }
}
impl Component<HostInput, HostEvent> for SocketCan {
    fn close(&mut self) -> Result<()> {
        self.transport.borrow_mut().take();
        Ok(())
    }
    fn sampler(&mut self) -> Option<&mut dyn Sampler<HostInput, HostEvent>> {
        Some(self)
    }
}
pub struct SocketCanFactory;
impl Factory<HostOptions, HostInput, HostEvent> for SocketCanFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let HostOptions::SocketCan(options) = &node.options else {
            return Err(std::sync::Arc::new(sim_core::Error::Admission(
                "host.socketcan requires SocketCAN options".into(),
            )));
        };
        options.validate()?;
        let mut port = PortSpec::new("can", "can");
        port.min_connections = 1;
        Ok(NodeDescription::new(vec![port], [Capability::Sample]))
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let HostOptions::SocketCan(options) = &node.options else {
            return Err(std::sync::Arc::new(sim_core::Error::Admission(
                "host.socketcan requires SocketCAN options".into(),
            )));
        };
        options.validate()?;
        let transport = Rc::new(RefCell::new(Some(
            SocketCanTransport::new(
                NativeCanSocket::open(&options.interface, options.enable_fd).map_err(lower)?,
                options.enable_fd,
                options.max_pending,
                options.io_budget,
            )
            .map_err(lower)?,
        )));
        let output = transport.clone();
        let context = build.context();
        let connection = build.attach::<_, CanConnection>(
            "can",
            CanAttachment {
                endpoint: EndpointId::new(),
                receive: Rc::new(move |frame, _| {
                    context.check_io()?;
                    context.check_deadline()?;
                    output
                        .borrow_mut()
                        .as_mut()
                        .ok_or_else(|| {
                            std::sync::Arc::new(sim_core::Error::State(
                                "SocketCAN is closed".into(),
                            )) as sim_core::Failure
                        })?
                        .send_frame(frame)
                        .map_err(|error| context.fail(error))?;
                    context.check_io()?;
                    context.check_deadline()
                }),
            },
        )?;
        Ok(Box::new(SocketCan {
            transport,
            connection,
            io_budget: options.io_budget,
        }))
    }
}

fn lower(error: crate::Failure) -> sim_core::Failure {
    error
}
