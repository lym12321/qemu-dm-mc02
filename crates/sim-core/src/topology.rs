use crate::{data::admission, Result};
use std::collections::BTreeSet;

fn identifier(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_alphabetic()
                || byte == b'_'
                || index > 0 && (byte.is_ascii_digit() || byte == b'-')
        })
}

fn port_name(value: &str) -> bool {
    value.len() <= 128 && value.split('.').all(identifier)
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub struct Endpoint {
    pub node: String,
    pub port: String,
}

impl Endpoint {
    pub fn new(node: impl Into<String>, port: impl Into<String>) -> Result<Self> {
        let endpoint = Self {
            node: node.into(),
            port: port.into(),
        };
        if !identifier(&endpoint.node) || !port_name(&endpoint.port) {
            return Err(admission(
                "endpoint requires an ASCII node identifier and dotted port identifiers",
            ));
        }
        Ok(endpoint)
    }

    pub fn parse(value: &str) -> Result<Self> {
        let (node, port) = value
            .split_once('.')
            .ok_or_else(|| admission("endpoint must be node.port"))?;
        Self::new(node, port)
    }
}

impl std::fmt::Display for Endpoint {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(formatter, "{}.{}", self.node, self.port)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Direction {
    In,
    Out,
    InOut,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum PortCategory {
    Protocol,
    Signal,
    Binding,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum Capability {
    Connection,
    BindingOwner,
    Machine,
    Step,
    Input,
    InputCommit,
    Dispatch,
    Sample,
    Health,
    Custom(String),
}

#[derive(Clone, Debug)]
pub struct PortSpec {
    pub name: String,
    pub protocol: String,
    pub direction: Direction,
    pub category: PortCategory,
    pub max_connections: Option<usize>,
    pub min_connections: usize,
    pub capabilities: BTreeSet<String>,
    pub required_capabilities: BTreeSet<String>,
}

impl PortSpec {
    pub fn new(name: impl Into<String>, protocol: impl Into<String>) -> Self {
        Self {
            name: name.into(),
            protocol: protocol.into(),
            direction: Direction::InOut,
            category: PortCategory::Protocol,
            max_connections: Some(1),
            min_connections: 0,
            capabilities: BTreeSet::new(),
            required_capabilities: BTreeSet::new(),
        }
    }

    pub(crate) fn validate(&self) -> Result<()> {
        if !port_name(&self.name) || self.protocol.is_empty() || self.protocol.contains('\0') {
            return Err(admission(
                "port requires a valid name and nonempty protocol",
            ));
        }
        if self.max_connections == Some(0)
            || self
                .max_connections
                .is_some_and(|maximum| self.min_connections > maximum)
            || self
                .capabilities
                .iter()
                .chain(&self.required_capabilities)
                .any(|value| value.is_empty() || value.contains('\0'))
        {
            return Err(admission("invalid port connection limits or capabilities"));
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub struct NodeSpec<O> {
    pub name: String,
    pub kind: String,
    pub options: O,
    pub required_capabilities: BTreeSet<Capability>,
}

impl<O> NodeSpec<O> {
    pub fn new(name: impl Into<String>, kind: impl Into<String>, options: O) -> Self {
        Self {
            name: name.into(),
            kind: kind.into(),
            options,
            required_capabilities: BTreeSet::new(),
        }
    }
}

#[derive(Clone, Debug, Default)]
pub struct NodeDescription {
    pub ports: Vec<PortSpec>,
    pub capabilities: BTreeSet<Capability>,
}

impl NodeDescription {
    pub fn new(ports: Vec<PortSpec>, capabilities: impl IntoIterator<Item = Capability>) -> Self {
        Self {
            ports,
            capabilities: capabilities.into_iter().collect(),
        }
    }

    pub fn port(&self, name: &str) -> Result<&PortSpec> {
        self.ports
            .iter()
            .find(|port| port.name == name)
            .ok_or_else(|| admission(format!("unknown port {name:?}")))
    }

    pub fn has(&self, capability: Capability) -> bool {
        self.capabilities.contains(&capability)
    }

    pub(crate) fn validate(&self) -> Result<()> {
        let mut names = BTreeSet::new();
        for port in &self.ports {
            port.validate()?;
            if !names.insert(&port.name) {
                return Err(admission("duplicate port name"));
            }
        }
        if self.capabilities.iter().any(|capability| {
            matches!(capability,
            Capability::Custom(value) if value.is_empty() || value.contains('\0'))
        }) {
            return Err(admission("node capabilities must be nonempty strings"));
        }
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub struct LinkSpec {
    pub source: Endpoint,
    pub target: Endpoint,
    pub required_capabilities: BTreeSet<String>,
}

impl LinkSpec {
    pub fn new(source: Endpoint, target: Endpoint) -> Self {
        Self {
            source,
            target,
            required_capabilities: BTreeSet::new(),
        }
    }
}

#[derive(Clone, Debug)]
pub struct Topology<O> {
    pub nodes: Vec<NodeSpec<O>>,
    pub links: Vec<LinkSpec>,
}

impl<O> Topology<O> {
    pub fn new(nodes: Vec<NodeSpec<O>>, links: Vec<LinkSpec>) -> Result<Self> {
        if nodes.is_empty() || nodes.len() > 64 || links.len() > 256 {
            return Err(admission(
                "topology requires 1..64 nodes and at most 256 links",
            ));
        }
        let mut names = BTreeSet::new();
        for node in &nodes {
            if !identifier(&node.name)
                || node.kind.is_empty()
                || node.kind.len() > 128
                || node.kind.contains('\0')
                || !names.insert(node.name.clone())
            {
                return Err(admission("invalid or duplicate node name/kind"));
            }
        }
        for link in &links {
            Endpoint::new(link.source.node.clone(), link.source.port.clone())?;
            Endpoint::new(link.target.node.clone(), link.target.port.clone())?;
            if link
                .required_capabilities
                .iter()
                .any(|value| value.is_empty() || value.contains('\0'))
            {
                return Err(admission("link capabilities must be nonempty strings"));
            }
        }
        Ok(Self { nodes, links })
    }
}

pub(crate) fn owner_capability(category: PortCategory) -> Capability {
    if category == PortCategory::Binding {
        Capability::BindingOwner
    } else {
        Capability::Connection
    }
}

pub(crate) fn construction_rank(description: &NodeDescription) -> u8 {
    if description.has(Capability::Connection) {
        0
    } else if description.has(Capability::BindingOwner) {
        1
    } else {
        2
    }
}
