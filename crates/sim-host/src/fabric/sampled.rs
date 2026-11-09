//! Sole bounded sampled fanout owner shared by logical digital and SI IMU nets.

use sim_core::{Endpoint, Error, Failure, Result, Session};
use std::{collections::VecDeque, rc::Rc, sync::Arc};

fn admission(message: impl Into<String>) -> Failure {
    Arc::new(Error::Admission(message.into()))
}
fn state_error(message: impl Into<String>) -> Failure {
    Arc::new(Error::State(message.into()))
}

#[derive(Clone, Debug)]
pub struct NetOptions {
    pub max_pending: usize,
    pub dispatch_budget: usize,
}
impl Default for NetOptions {
    fn default() -> Self {
        Self {
            max_pending: 256,
            dispatch_budget: 256,
        }
    }
}
impl NetOptions {
    pub fn validate(&self) -> Result<()> {
        if !(1..=65536).contains(&self.max_pending) || !(1..=65536).contains(&self.dispatch_budget)
        {
            return Err(admission("sampled capacities require 1..65536"));
        }
        Ok(())
    }
}

pub(super) trait Policy: Default {
    const NAME: &'static str;
    type Sample: Copy;
    type Delivery: Copy;
    fn validate(
        &self,
        sample: Self::Sample,
        previous: Option<Self::Sample>,
        cut: u64,
    ) -> Result<bool>;
    fn commit(&mut self, _: Self::Sample) {}
    fn delivery(sample: Self::Sample) -> Self::Delivery;
}
pub(super) type Source<S> = Rc<dyn Fn() -> Result<S>>;
pub(super) type Sink<D> = Rc<dyn Fn(D) -> Result<()>>;
pub(super) enum Attachment<S, D> {
    Source(Source<S>),
    Sink(Sink<D>),
}

pub(super) enum Observation<S> {
    Sample {
        source: String,
        sample: S,
        enqueued: bool,
        cut_ns: u64,
    },
    Delivery {
        source: String,
        destination: String,
        sample: S,
        sequence: u64,
        due_ns: u64,
        cut_ns: u64,
    },
}
pub(super) struct State<S> {
    pub sample: Option<S>,
    pub sample_time_ns: Option<u64>,
    pub pending: usize,
    pub failed: bool,
}
struct Broadcast<S> {
    due_ns: u64,
    sequence: u64,
    sample: S,
}

pub(super) struct Fanout<P: Policy> {
    options: NetOptions,
    driver: Option<(String, Source<P::Sample>)>,
    sinks: Vec<(String, Sink<P::Delivery>)>,
    queue: VecDeque<Broadcast<P::Sample>>,
    initialized: bool,
    closed: bool,
    failure: Option<Failure>,
    time_ns: u64,
    sample_cut: Option<u64>,
    sample: Option<P::Sample>,
    sequence: u64,
    policy: P,
}
impl<P: Policy> Fanout<P> {
    pub fn new(options: NetOptions) -> Result<Self> {
        options.validate()?;
        Ok(Self {
            options,
            driver: None,
            sinks: vec![],
            queue: VecDeque::new(),
            initialized: false,
            closed: false,
            failure: None,
            time_ns: 0,
            sample_cut: None,
            sample: None,
            sequence: 0,
            policy: P::default(),
        })
    }
    pub fn attach(
        &mut self,
        name: String,
        endpoint: Attachment<P::Sample, P::Delivery>,
    ) -> Result<()> {
        if self.closed || self.initialized {
            return Err(state_error(format!(
                "{} wiring must attach before reset",
                P::NAME
            )));
        }
        Endpoint::parse(&name)?;
        if self
            .driver
            .as_ref()
            .is_some_and(|(known, _)| known == &name)
            || self.sinks.iter().any(|(known, _)| known == &name)
        {
            return Err(admission(format!(
                "{} endpoint is already attached",
                P::NAME
            )));
        }
        if self.sinks.len() + usize::from(self.driver.is_some()) >= 256 {
            return Err(Arc::new(Error::Capacity(format!(
                "{} endpoint capacity exhausted",
                P::NAME
            ))));
        }
        match endpoint {
            Attachment::Source(read) => {
                if self.driver.is_some() {
                    return Err(admission(format!(
                        "{} net permits exactly one driver",
                        P::NAME
                    )));
                }
                self.driver = Some((name, read));
            }
            Attachment::Sink(receive) => {
                if self
                    .sinks
                    .iter()
                    .any(|(_, known)| Rc::ptr_eq(known, &receive))
                {
                    return Err(admission(format!(
                        "{} endpoint is already attached",
                        P::NAME
                    )));
                }
                self.sinks.push((name, receive));
            }
        }
        Ok(())
    }
    pub fn reset(&mut self, session: Session) -> Result<()> {
        if self.closed {
            return Err(state_error(format!(
                "{} net cannot reset while closed",
                P::NAME
            )));
        }
        Session::new(session.session_id, session.t_sim_ns)?;
        if self.driver.is_none() || self.sinks.is_empty() {
            return Err(admission(format!(
                "{} net requires one driver and at least one sink",
                P::NAME
            )));
        }
        self.queue.clear();
        self.time_ns = session.t_sim_ns;
        self.sample_cut = None;
        self.sample = None;
        self.sequence = 0;
        self.failure = None;
        self.policy = P::default();
        self.initialized = true;
        Ok(())
    }
    fn check(&self, cut: u64) -> Result<()> {
        if let Some(error) = &self.failure {
            return Err(error.clone());
        }
        if self.closed || !self.initialized {
            return Err(state_error(format!(
                "{} net requires a successful reset and healthy open state",
                P::NAME
            )));
        }
        if cut < self.time_ns {
            return Err(admission(format!("{} cut must not decrease", P::NAME)));
        }
        Ok(())
    }
    fn freeze<T>(&mut self, result: Result<T>) -> Result<T> {
        result.map_err(|error| self.failure.get_or_insert(error).clone())
    }
    pub fn failure(&self) -> Option<&Failure> {
        self.failure.as_ref()
    }
    pub fn observe(&self) -> State<P::Sample> {
        State {
            sample: self.sample,
            sample_time_ns: self.sample_cut,
            pending: self.queue.len(),
            failed: self.failure.is_some(),
        }
    }
    pub fn dispatch_budget(&self) -> usize {
        self.options.dispatch_budget
    }
    pub fn next_due_ns(&self) -> Option<u64> {
        self.queue.front().map(|item| item.due_ns)
    }
    pub fn sample(
        &mut self,
        cut: u64,
        observe: &mut impl FnMut(Observation<P::Sample>) -> Result<()>,
    ) -> Result<()> {
        self.check(cut)?;
        if self.sample_cut.is_some_and(|previous| cut <= previous) {
            return Err(admission(format!(
                "{} sources may be sampled only once per completed cut",
                P::NAME
            )));
        }
        let result = (|| {
            let (source, read) = self.driver.as_ref().expect("reset admitted driver");
            let sample = read()?;
            let enqueued = self.policy.validate(sample, self.sample, cut)?;
            let next_sequence = if enqueued {
                if self.queue.len() >= self.options.max_pending {
                    return Err(Arc::new(Error::Capacity(format!(
                        "{} pending sample capacity exhausted",
                        P::NAME
                    ))) as Failure);
                }
                Some(
                    self.sequence
                        .checked_add(1)
                        .ok_or_else(|| state_error(format!("{} sequence exhausted", P::NAME)))?,
                )
            } else {
                None
            };
            self.time_ns = cut;
            self.sample_cut = Some(cut);
            self.sample = Some(sample);
            self.policy.commit(sample);
            if let Some(next_sequence) = next_sequence {
                self.queue.push_back(Broadcast {
                    due_ns: cut,
                    sequence: self.sequence,
                    sample,
                });
                self.sequence = next_sequence;
            }
            observe(Observation::Sample {
                source: source.clone(),
                sample,
                enqueued,
                cut_ns: cut,
            })
        })();
        self.freeze(result)
    }
    pub fn dispatch(
        &mut self,
        cut: u64,
        budget: usize,
        observe: &mut impl FnMut(Observation<P::Sample>) -> Result<()>,
    ) -> Result<usize> {
        self.check(cut)?;
        self.time_ns = cut;
        let result = (|| {
            let mut count = 0;
            while count < budget.min(self.options.dispatch_budget)
                && self.queue.front().is_some_and(|item| item.due_ns <= cut)
            {
                let item = self.queue.pop_front().expect("front exists");
                count += 1;
                for (destination, receive) in &self.sinks {
                    receive(P::delivery(item.sample))?;
                    observe(Observation::Delivery {
                        source: self
                            .driver
                            .as_ref()
                            .expect("reset admitted driver")
                            .0
                            .clone(),
                        destination: destination.clone(),
                        sample: item.sample,
                        sequence: item.sequence,
                        due_ns: item.due_ns,
                        cut_ns: cut,
                    })?;
                }
            }
            Ok(count)
        })();
        self.freeze(result)
    }
    pub fn close(&mut self) {
        self.closed = true;
        self.queue.clear();
        self.driver = None;
        self.sinks.clear();
    }
}
