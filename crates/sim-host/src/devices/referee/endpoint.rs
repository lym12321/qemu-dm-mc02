//! One snapshot occupies one existing borrowed byte-transport admission slot.
//! No socket, RX parser, drain clock or engine owner; reset leaves physical tails.

use super::{encode_message, Message, RefereeState};
use sim_core::{Error, Failure, Result};
use std::sync::Arc;

fn state_error(message: &str) -> Failure {
    Arc::new(Error::State(message.into()))
}
fn capacity(error: &Failure) -> bool {
    matches!(error.downcast_ref::<Error>(), Some(Error::Capacity(_)))
        || matches!(
            error.downcast_ref::<crate::Error>(),
            Some(crate::Error::Capacity(_))
        )
}
pub type BatchSender<'a> = Box<dyn FnMut(Vec<u8>) -> Result<()> + 'a>;
/// Borrows only complete byte-batch admission. Capacity adds no new bytes or
/// sequence; other failures retain partial effects and freeze the original error.
pub struct RefereeUartEndpoint<'a> {
    send: BatchSender<'a>,
    next_sequence: u8,
    failure: Option<Failure>,
    closed: bool,
}
impl<'a> RefereeUartEndpoint<'a> {
    pub fn new(send: impl FnMut(Vec<u8>) -> Result<()> + 'a, sequence: u8) -> Self {
        Self {
            send: Box::new(send),
            next_sequence: sequence,
            failure: None,
            closed: false,
        }
    }
    pub fn next_sequence(&self) -> u8 {
        self.next_sequence
    }
    pub fn closed(&self) -> bool {
        self.closed
    }
    pub fn send_state(&mut self, state: &RefereeState) -> Result<Vec<u8>> {
        if self.closed {
            return Err(state_error("referee endpoint is closed"));
        }
        if let Some(error) = &self.failure {
            return Err(error.clone());
        }
        // Admit all four groups before touching even a previously queued suffix.
        let mut wire = Vec::with_capacity(82);
        let messages: [Message; 4] = [
            state.game.into(),
            state.robot.into(),
            state.power_heat.into(),
            state.buff.into(),
        ];
        for (offset, message) in messages.into_iter().enumerate() {
            wire.extend_from_slice(&encode_message(
                message,
                self.next_sequence.wrapping_add(offset as u8),
            )?);
        }
        match (self.send)(wire.clone()) {
            Ok(()) => {
                self.next_sequence = self.next_sequence.wrapping_add(4);
                Ok(wire)
            }
            Err(error) => {
                if !capacity(&error) {
                    self.failure = Some(error.clone());
                }
                Err(error)
            }
        }
    }
    pub fn reset(&mut self, sequence: u8) -> Result<()> {
        if self.closed {
            return Err(state_error("referee endpoint is closed"));
        }
        self.next_sequence = sequence;
        self.failure = None;
        Ok(())
    }
    pub fn close(&mut self) {
        self.closed = true;
    }
}
