//! Referee engine and UART admission on the existing Simulation cut owner.
//! Enable starts publication at its input cut; subsequent deadlines are absolute.
//! Reverse UART bytes are ignored, without an uplink parser or echo.

use super::{RefereeConfiguration, RefereeEngine, RefereeState, RefereeUartEndpoint};
use crate::{
    fabric::uart::{UartAttachment, UartConnection, UartEndpointId, UartStamp},
    runtime::{HostContext, HostEvent, HostInput, HostOptions, HostRegistry},
};
use sim_core::{
    BuildContext, Capability, Component, Direction, Dispatcher, Error, EventTime, Factory, Failure,
    Input, InputValidator, NodeDescription, NodeSpec, PortCategory, PortSpec, Result, Session,
    StepContext, StepResult, Stepper, TimeQuality, Topology,
};
use std::{rc::Rc, sync::Arc};
fn admission(message: &str) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn state_error(message: &str) -> Failure {
    Arc::new(Error::State(message.into()))
}

#[derive(Clone, Debug)]
pub struct RefereeOptions {
    pub configuration: RefereeConfiguration,
    pub period_ns: u64,
}
impl RefereeOptions {
    pub fn validate(&self) -> Result<()> {
        self.configuration.validate()?;
        if !(1_000_000..=60_000_000_000).contains(&self.period_ns) {
            return Err(admission("referee period must be in 1ms..60s"));
        }
        Ok(())
    }
}
pub struct RefereeDevice {
    engine: RefereeEngine,
    endpoint: RefereeUartEndpoint<'static>,
    period_ns: u64,
    due: Option<u64>,
    initialized: bool,
    closed: bool,
    failure: Option<Failure>,
    publications: u64,
}
impl RefereeDevice {
    pub fn get_state(&self) -> Result<RefereeState> {
        self.engine.get_state()
    }
    pub fn publications(&self) -> u64 {
        self.publications
    }
    pub fn next_sequence(&self) -> u8 {
        self.endpoint.next_sequence()
    }
    fn check(&self) -> Result<()> {
        if let Some(error) = &self.failure {
            return Err(error.clone());
        }
        if self.closed || !self.initialized {
            return Err(state_error("referee device requires a successful reset"));
        }
        Ok(())
    }
    fn freeze<T>(&mut self, result: Result<T>, context: &HostContext) -> Result<T> {
        result.map_err(|error| context.fail(self.failure.get_or_insert(error).clone()))
    }
}
impl Stepper<HostInput, HostEvent> for RefereeDevice {
    fn step(&mut self, step: &StepContext, context: &HostContext) -> Result<StepResult<HostEvent>> {
        self.check()?;
        let result = self
            .engine
            .step(*step)
            .map(|_| StepResult::reached(step.t_sim_ns));
        self.freeze(result, context)
    }
}
impl Input<HostInput, HostEvent> for RefereeDevice {
    fn input(&mut self, port: &str, value: HostInput, context: &HostContext) -> Result<()> {
        self.check()?;
        if port != "enable" || !matches!(value, HostInput::RefereeEnable(true)) {
            return Err(admission("referee enable requires true"));
        }
        context.check_io()?;
        if self.due.is_none() {
            self.due = Some(context.t_sim_ns()?);
        }
        Ok(())
    }
}
impl Dispatcher<HostInput, HostEvent> for RefereeDevice {
    fn dispatch_budget(&self) -> usize {
        1
    }
    fn next_due_ns(&self) -> Result<Option<u64>> {
        if let Some(error) = &self.failure {
            return Err(error.clone());
        }
        Ok(self.due)
    }
    fn dispatch(&mut self, cut: u64, budget: usize, context: &HostContext) -> Result<usize> {
        self.check()?;
        let Some(due) = self.due.filter(|due| *due <= cut && budget != 0) else {
            return Ok(0);
        };
        let result = (|| {
            if self.engine.t_sim_ns() != cut {
                return Err(state_error(
                    "referee publication requires completed engine state at its cut",
                ));
            }
            let next_due = due
                .checked_add(self.period_ns)
                .ok_or_else(|| state_error("referee publication deadline overflow"))?;
            if next_due <= cut {
                return Err(state_error("referee publication deadline was missed"));
            }
            let publication = self
                .publications
                .checked_add(1)
                .ok_or_else(|| state_error("referee publication counter overflow"))?;
            let first_sequence = self.endpoint.next_sequence();
            let data = self.endpoint.send_state(&self.engine.get_state()?)?;
            self.due = Some(next_due);
            self.publications = publication;
            context.check_io()?;
            context.emit(
                Some("uart"),
                "referee.batch.admitted",
                HostEvent::RefereeBatch {
                    data,
                    first_sequence,
                    next_sequence: self.endpoint.next_sequence(),
                    publication,
                    period_ns: self.period_ns,
                    due_ns: due,
                    next_due_ns: next_due,
                },
                EventTime {
                    source_time_ns: Some(cut),
                    mapped_time_ns: Some(cut),
                    delivery_time_ns: Some(cut),
                    quality: Some(TimeQuality::Exact),
                },
            )?;
            context.check_io()?;
            context.check_deadline()?;
            Ok(1)
        })();
        self.freeze(result, context)
    }
}
impl Component<HostInput, HostEvent> for RefereeDevice {
    fn reset(&mut self, session: Session, context: &HostContext) -> Result<()> {
        if self.closed {
            return Err(state_error("referee device is closed"));
        }
        let result = (|| {
            self.engine.reset(session)?;
            self.endpoint.reset(0)?;
            Ok(())
        })();
        match result {
            Ok(()) => {
                self.initialized = true;
                self.due = None;
                self.publications = 0;
                self.failure = None;
                Ok(())
            }
            Err(error) => self.freeze(Err(error), context),
        }
    }
    fn close(&mut self) -> Result<()> {
        self.closed = true;
        self.endpoint.close();
        self.engine.close();
        Ok(())
    }
    fn stepper(&mut self) -> Option<&mut dyn Stepper<HostInput, HostEvent>> {
        Some(self)
    }
    fn input(&mut self) -> Option<&mut dyn Input<HostInput, HostEvent>> {
        Some(self)
    }
    fn dispatcher(&mut self) -> Option<&mut dyn Dispatcher<HostInput, HostEvent>> {
        Some(self)
    }
}
pub struct RefereeFactory;
impl InputValidator<HostOptions, HostInput> for RefereeFactory {
    fn validate_input(
        &self,
        node: &NodeSpec<HostOptions>,
        port: &str,
        value: HostInput,
        connected: &[String],
    ) -> Result<HostInput> {
        self.describe(node)?.port(port)?;
        if port != "enable"
            || !matches!(value, HostInput::RefereeEnable(true))
            || connected.iter().any(|port| port == "enable")
        {
            return Err(admission("referee enable accepts external true input only"));
        }
        Ok(value)
    }
}
impl Factory<HostOptions, HostInput, HostEvent> for RefereeFactory {
    fn describe(&self, node: &NodeSpec<HostOptions>) -> Result<NodeDescription> {
        let HostOptions::Referee(options) = &node.options else {
            return Err(admission("device.referee requires referee options"));
        };
        options.validate()?;
        let mut uart = PortSpec::new("uart", "uart");
        uart.min_connections = 1;
        let mut enable = PortSpec::new("enable", "referee-enable");
        enable.category = PortCategory::Signal;
        enable.direction = Direction::In;
        enable.capabilities.insert("external_input".into());
        Ok(NodeDescription::new(
            vec![uart, enable],
            [Capability::Step, Capability::Dispatch, Capability::Input],
        ))
    }
    fn create(
        &self,
        node: &NodeSpec<HostOptions>,
        build: &mut BuildContext<'_, HostOptions, HostInput, HostEvent>,
    ) -> Result<Box<dyn Component<HostInput, HostEvent>>> {
        let HostOptions::Referee(options) = &node.options else {
            return Err(admission("device.referee requires referee options"));
        };
        options.validate()?;
        let mut engine = RefereeEngine::new(options.configuration.clone())?;
        let attached = build.attach::<_, UartConnection>(
            "uart",
            UartAttachment {
                endpoint: UartEndpointId::new(),
                receive: Rc::new(|_, _| Ok(())),
            },
        );
        let connection = match attached {
            Ok(connection) => connection,
            Err(error) => {
                engine.close();
                return Err(error);
            }
        };
        let context = build.context();
        let endpoint = RefereeUartEndpoint::new(
            move |data| {
                let cut = context.t_sim_ns()?;
                connection.send(
                    &data,
                    UartStamp {
                        source_time_ns: Some(cut),
                        mapped_time_ns: Some(cut),
                        quality: TimeQuality::Exact,
                    },
                )
            },
            0,
        );
        Ok(Box::new(RefereeDevice {
            engine,
            endpoint,
            period_ns: options.period_ns,
            due: None,
            initialized: false,
            closed: false,
            failure: None,
            publications: 0,
        }))
    }
    fn input_validator(&self) -> Option<&dyn InputValidator<HostOptions, HostInput>> {
        Some(self)
    }
    fn validate_topology(
        &self,
        topology: &Topology<HostOptions>,
        _: &[NodeDescription],
        _: &HostRegistry,
    ) -> Result<()> {
        for node in &topology.nodes {
            if !matches!(node.options, HostOptions::Referee(_)) {
                continue;
            }
            if topology.links.iter().any(|link| {
                [&link.source, &link.target]
                    .iter()
                    .any(|endpoint| endpoint.node == node.name && endpoint.port == "enable")
            }) {
                return Err(admission("referee enable accepts external input only"));
            }
        }
        Ok(())
    }
}
