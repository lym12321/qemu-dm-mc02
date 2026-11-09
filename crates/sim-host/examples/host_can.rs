use sim_core::{Endpoint, EventPayload, SimulationOptions};
use sim_host::{
    can::{CanBusOptions, CanFrame},
    runtime::{host_can, HostEvent, HostInput},
};

fn main() -> sim_core::Result<()> {
    let mut simulation = host_can(
        CanBusOptions {
            delay_ns: 2,
            ..Default::default()
        },
        SimulationOptions {
            quantum_ns: 10,
            ..Default::default()
        },
    )?;
    simulation.start(None)?;
    for (at, id) in [(3, 0x123), (7, 0x124)] {
        let frame = CanFrame::new(id, 0, None, [1, 2, 3], 999)
            .map_err(|error| -> sim_core::Failure { error })?;
        simulation.schedule_input(
            Endpoint::parse("left.tx")?,
            HostInput::CanFrame(frame),
            Some(at),
        )?;
    }
    simulation.run_until(22, None)?;
    simulation.close()?;
    for event in simulation.events() {
        if let EventPayload::Observation(HostEvent::CanDelivery {
            destination, frame, ..
        }) = event.payload
        {
            println!(
                "{} -> {destination}: CAN {:03x}, source={}ns delivery={}ns",
                event.source_node,
                frame.can_id(),
                event.source_time_ns.unwrap(),
                event.delivery_time_ns
            );
        }
    }
    Ok(())
}
