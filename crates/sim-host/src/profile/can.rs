//! Original-peer paused CAN observations. Native counters attest transport
//! processing, including dropped input, rather than firmware consumption.
use super::{lower, Shared};
use crate::boards::uint;
use crate::machine::{native_time, state_error, validate_execution, NativeBackend};
use serde_json::Value;
use sim_core::Result;

pub(crate) fn execution(backend: &mut NativeBackend, ports: &[String]) -> Result<(Value, Value)> {
    let state = backend.property("can-execution-state")?;
    if state["schema"] != 1 {
        return Err(state_error("unsupported can-execution-state schema"));
    }
    for port in ports {
        let transport = &state["fdcan-transports"][format!("dm-mc02.{port}")];
        if transport["connected"] != true
            || transport["path"] != format!("/chardevs/{}", wire(port))
        {
            return Err(state_error(
                "CAN execution transport identity changed or disconnected",
            ));
        }
    }
    Ok((state["execution"].clone(), state["status"].clone()))
}
pub(crate) fn wire(port: &str) -> String {
    port.replace("fdcan", "can")
}
fn transfer<'a>(state: &'a Value, property: &str, port: &str, target: u64) -> Result<&'a Value> {
    let transfer = &state[property];
    if transfer["schema"] != 1 || native_time(&transfer["timestamp_ns"])? != target {
        return Err(state_error(
            "CAN transfer observation schema or time changed",
        ));
    }
    transfer["controllers"]
        .get(format!("dm-mc02.{port}"))
        .filter(|value| value.is_object())
        .ok_or_else(|| state_error("missing CAN controller observation"))
}
pub(crate) fn observe(
    backend: &mut NativeBackend,
    port: &str,
    target: u64,
    initial: bool,
) -> Result<(Value, Value)> {
    let state = backend.property("can-execution-state")?;
    if state["schema"] != 1 {
        return Err(state_error("unsupported can-execution-state schema"));
    }
    validate_execution(&state["execution"], &state["status"], target, initial)?;
    let transport = &state["fdcan-transports"][format!("dm-mc02.{port}")];
    if transport["connected"] != true || transport["path"] != format!("/chardevs/{}", wire(port)) {
        return Err(state_error(
            "CAN transport identity changed or disconnected",
        ));
    }
    let tx = transfer(&state, "fdcan-tx-state", port, target)?;
    let rx = transfer(&state, "fdcan-rx-state", port, target)?;
    let frames = uint(&tx["pending_frames"], 16, "CAN pending_frames")?;
    let bytes = uint(&tx["pending_bytes"], 1344, "CAN pending_bytes")?;
    if bytes < frames || bytes > frames * 84 {
        return Err(state_error("inconsistent CAN pending counts"));
    }
    for field in ["tx_dropped", "tx_short_writes"] {
        uint(&tx[field], u64::MAX, field)?;
    }
    uint(
        &rx["completed_frames"],
        u64::MAX - 1,
        "CAN completed_frames",
    )?;
    uint(&rx["partial_bytes"], 83, "CAN partial_bytes")?;
    uint(&rx["rx_dropped"], u64::MAX, "CAN rx_dropped")?;
    Ok((tx.clone(), rx.clone()))
}
fn empty_output(shared: &Shared, port: &str) -> Result<()> {
    let stream = &shared.streams[&wire(port)];
    if stream.buffered_bytes() != 0 {
        return Err(state_error("drain CAN output before input receipt"));
    }
    let mut byte = [0];
    match rustix::net::recv(stream.io(), &mut byte, rustix::net::RecvFlags::PEEK) {
        Err(error) if error == rustix::io::Errno::AGAIN => Ok(()),
        Ok((0, _)) => Err(state_error("QEMU CAN socket closed")),
        Ok(_) => Err(state_error("drain CAN output before input receipt")),
        Err(error) => Err(std::sync::Arc::new(std::io::Error::from(error))),
    }
}
pub(crate) fn initial(
    backend: &mut NativeBackend,
    shared: &Shared,
    port: &str,
) -> Result<(Value, Value)> {
    let target = backend.native_origin_ns()?;
    let mut first: Option<(Value, Value)> = None;
    for _ in 0..2 {
        backend.check()?;
        let stream = &shared.streams[&wire(port)];
        if stream.tx_pending() || stream.buffered_bytes() != 0 {
            return Err(state_error(
                "CAN initial baseline requires empty host queues",
            ));
        }
        let (tx, rx) = observe(backend, port, target, true)?;
        if tx["pending_frames"] != 0 || tx["pending_bytes"] != 0 || rx["partial_bytes"] != 0 {
            return Err(state_error(
                "CAN initial baseline requires empty native queues",
            ));
        }
        empty_output(shared, port)?;
        if let Some(first) = &first {
            if ["tx_dropped", "tx_short_writes"]
                .iter()
                .any(|field| first.0[*field] != tx[*field])
                || ["completed_frames", "rx_dropped"]
                    .iter()
                    .any(|field| first.1[*field] != rx[*field])
            {
                return Err(state_error("CAN baseline changed during observation"));
            }
        }
        first = Some((tx, rx));
    }
    backend.check()?;
    Ok(first.unwrap())
}
pub(crate) fn receipt(
    backend: &mut NativeBackend,
    shared: &mut Shared,
    port: &str,
    goal: u64,
    cut: u64,
) -> Result<Value> {
    let target = backend.to_native(cut)?;
    let initial = cut == backend.context().session()?.t_sim_ns;
    loop {
        backend.check()?;
        let (tx, rx) = observe(backend, port, target, initial)?;
        if initial && (tx["pending_frames"] != 0 || tx["pending_bytes"] != 0) {
            return Err(state_error(
                "initial CAN input requires empty native output",
            ));
        }
        empty_output(shared, port)?;
        let count = rx["completed_frames"].as_u64().unwrap();
        if count > goal {
            return Err(state_error(
                "CAN completed input count exceeds reserved goal",
            ));
        }
        if count == goal {
            if rx["partial_bytes"] != 0 || shared.streams[&wire(port)].tx_pending() {
                return Err(state_error("CAN input has bytes beyond completed goal"));
            }
            let (_, final_rx) = observe(backend, port, target, initial)?;
            empty_output(shared, port)?;
            if final_rx["completed_frames"] != goal
                || final_rx["partial_bytes"] != 0
                || shared.streams[&wire(port)].tx_pending()
            {
                return Err(state_error("CAN input changed during final observation"));
            }
            backend.check()?;
            return Ok(final_rx);
        }
        shared
            .streams
            .get_mut(&wire(port))
            .unwrap()
            .flush()
            .map_err(lower)?;
        backend.wait_poll()?;
    }
}
