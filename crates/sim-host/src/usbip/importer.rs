//! Standard OS importer client. The Lab owner polls one helper at a time so
//! the USB/IP server keeps servicing enumeration and endpoint requests.
use std::{
    fs,
    io::{self, Read, Seek, SeekFrom},
    net::SocketAddr,
    process::{Child, Command, ExitStatus, Stdio},
    time::{Duration, Instant},
};
use wait_timeout::ChildExt;

fn failure(text: impl std::fmt::Display) -> io::Error {
    io::Error::other(text.to_string())
}
fn run(args: &[String]) -> io::Result<ChildOutput> {
    let executable = which::which("usbip").map_err(|_| {
        failure("Install usbip-win2 on Windows, or system usbip on Linux, and add usbip to PATH")
    })?;
    let output = tempfile::tempfile()?;
    #[cfg(target_os = "linux")]
    let mut command = {
        let mut c = Command::new("sudo");
        c.arg("-n").arg(executable);
        c
    };
    #[cfg(not(target_os = "linux"))]
    let mut command = Command::new(executable);
    let child = command
        .args(args)
        .stdin(Stdio::null())
        .stdout(output.try_clone()?)
        .stderr(output.try_clone()?)
        .spawn()?;
    Ok(ChildOutput { child, output })
}
struct ChildOutput {
    child: Child,
    output: fs::File,
}
impl ChildOutput {
    fn finish(&mut self, status: ExitStatus) -> io::Result<String> {
        self.output.seek(SeekFrom::Start(0))?;
        let mut bytes = Vec::new();
        (&mut self.output).take(65_537).read_to_end(&mut bytes)?;
        if bytes.len() > 65_536 {
            return Err(failure("usbip output exceeds 64 KiB"));
        }
        let text = String::from_utf8_lossy(&bytes).trim().to_owned();
        if !status.success() {
            return Err(failure(format!("usbip failed ({status}): {text}")));
        }
        Ok(text)
    }
    fn stop(&mut self) -> io::Result<ExitStatus> {
        if let Some(status) = self.child.try_wait()? {
            return Ok(status);
        }
        self.child.kill()?;
        self.child
            .wait_timeout(Duration::from_secs(1))?
            .ok_or_else(|| failure("usbip helper did not exit after termination"))
    }
}
impl Drop for ChildOutput {
    fn drop(&mut self) {
        let _ = self.stop();
    }
}

struct Attachment {
    address: SocketAddr,
    busid: String,
}
impl Attachment {
    fn args(&self) -> Vec<String> {
        let mut args = vec![
            "--tcp-port".into(),
            self.address.port().to_string(),
            "attach".into(),
            "--remote".into(),
            self.address.ip().to_string(),
        ];
        #[cfg(windows)]
        args.extend(["--bus-id".into(), self.busid.clone()]);
        #[cfg(not(windows))]
        args.extend(["--busid".into(), self.busid.clone()]);
        args
    }
    fn port(&self, output: &str) -> io::Result<Option<u16>> {
        // Linux usbip and pinned usbip-win2 both print Port N followed by the
        // remote URI. Match that whole identity, never an arbitrary new port.
        let uri = format!("usbip://{}/{}", self.address, self.busid);
        let raw_ipv6 = format!(
            "usbip://{}:{}/{}",
            self.address.ip(),
            self.address.port(),
            self.busid
        );
        let mut current = None;
        let mut found = None;
        for line in output.lines().map(str::trim) {
            if let Some(tail) = line.strip_prefix("Port ") {
                current = tail.split(':').next().and_then(|s| s.parse::<u16>().ok());
            } else if line.split_once("->").is_some_and(|(_, target)| {
                let target = target.trim();
                target == uri || target == raw_ipv6
            }) {
                let port =
                    current.ok_or_else(|| failure("usbip port record has no port number"))?;
                if found.replace(port).is_some() {
                    return Err(failure(
                        "Multiple USB imports match this exporter; cannot select an owned port",
                    ));
                }
            }
        }
        Ok(found)
    }
}
#[derive(Clone, Copy)]
enum Action {
    Attach,
    Locate(Instant),
    Detach,
    #[cfg(windows)]
    Cancel,
}
struct Job {
    // None is a scheduled port-list retry, not a detached process.
    process: Option<ChildOutput>,
    deadline: Instant,
    action: Action,
}
#[derive(Default)]
pub struct Importer {
    job: Option<Job>,
    target: Option<Attachment>,
    port: Option<u16>,
    message: Option<String>,
    closed: bool,
}
fn detached_args(port: u16) -> Vec<String> {
    vec!["detach".into(), "--port".into(), port.to_string()]
}
fn bounded(args: &[String]) -> io::Result<String> {
    let mut process = run(args)?;
    let Some(status) = process.child.wait_timeout(Duration::from_secs(5))? else {
        process.stop()?;
        return Err(failure("usbip cleanup command timed out"));
    };
    process.finish(status)
}
impl Importer {
    pub fn port(&self) -> Option<u16> {
        self.port
    }
    pub fn busy(&self) -> bool {
        self.job.is_some()
    }
    pub fn message(&self) -> Option<&str> {
        self.message.as_deref()
    }
    fn start(&mut self, action: Action, args: &[String]) -> io::Result<()> {
        self.job = Some(Job {
            process: Some(run(args)?),
            deadline: Instant::now() + Duration::from_secs(8),
            action,
        });
        Ok(())
    }
    pub fn attach(&mut self, address: SocketAddr, busid: &str) -> io::Result<()> {
        if self.closed || self.busy() || self.target.is_some() {
            return Err(failure("USB importer is already connected or busy"));
        }
        let target = Attachment {
            address,
            busid: busid.into(),
        };
        let args = target.args();
        #[cfg(windows)]
        let args = {
            let mut args = args;
            args.extend(["--once".into(), "--terse".into()]);
            args
        };
        self.start(Action::Attach, &args)?;
        self.target = Some(target);
        self.message = Some("connecting to this computer".into());
        Ok(())
    }
    pub fn detach(&mut self) -> io::Result<()> {
        if self.busy() {
            return Err(failure("USB importer is busy"));
        }
        if let Some(port) = self.port {
            self.start(Action::Detach, &detached_args(port))?;
            self.message = Some("disconnecting from this computer".into());
        }
        Ok(())
    }
    pub fn poll(&mut self) -> io::Result<()> {
        let Some(job) = self.job.as_mut() else {
            return Ok(());
        };
        if job.process.is_none() {
            if Instant::now() < job.deadline {
                return Ok(());
            }
            job.process = Some(run(&["port".into()])?);
            job.deadline = Instant::now() + Duration::from_secs(8);
        }
        // Retain ownership on try_wait failure so cleanup can still stop/reap it.
        let status = job.process.as_mut().unwrap().child.try_wait()?;
        if status.is_none() && Instant::now() < job.deadline {
            return Ok(());
        }
        let mut job = self.job.take().unwrap();
        let process = job.process.as_mut().unwrap();
        #[cfg(windows)]
        let timed_out = status.is_none();
        let result = match status {
            Some(status) => process.finish(status),
            None => process
                .stop()
                .and_then(|_| Err(failure("usbip command timed out"))),
        };
        let next = match job.action {
            Action::Attach => {
                self.message = result.err().map(|e| e.to_string());
                #[cfg(windows)]
                if timed_out {
                    Some(Action::Cancel)
                } else {
                    Some(Action::Locate(Instant::now() + Duration::from_secs(3)))
                }
                #[cfg(not(windows))]
                {
                    Some(Action::Locate(Instant::now() + Duration::from_secs(3)))
                }
            }
            #[cfg(windows)]
            Action::Cancel => {
                if let Err(e) = result {
                    self.message = Some(e.to_string());
                }
                Some(Action::Locate(Instant::now() + Duration::from_secs(3)))
            }
            Action::Locate(deadline) => {
                match result.and_then(|output| self.target.as_ref().unwrap().port(&output)) {
                    Ok(port) => {
                        self.port = port;
                        if port.is_none() {
                            // The attach IOCTL can finish before the OS makes
                            // the imported port visible in its device listing.
                            if Instant::now() < deadline {
                                self.retry_lookup(deadline);
                                return Ok(());
                            }
                            if self.message.is_none() {
                                self.message = Some("usbip did not report this attachment".into());
                            }
                        }
                    }
                    Err(_) if Instant::now() < deadline => {
                        self.retry_lookup(deadline);
                        return Ok(());
                    }
                    Err(e) => self.message = Some(e.to_string()),
                }
                None
            }
            Action::Detach => {
                match result {
                    Ok(_) => {
                        self.port = None;
                        self.target = None;
                        self.message = None;
                    }
                    Err(e) => self.message = Some(e.to_string()),
                }
                None
            }
        };
        if let Some(action) = next {
            let args = match action {
                #[cfg(windows)]
                Action::Cancel => {
                    let mut args = self.target.as_ref().unwrap().args();
                    args.push("--stop".into());
                    args
                }
                _ => vec!["port".into()],
            };
            if let Err(e) = self.start(action, &args) {
                self.message = Some(e.to_string());
            }
        }
        Ok(())
    }
    fn retry_lookup(&mut self, deadline: Instant) {
        self.job = Some(Job {
            process: None,
            deadline: Instant::now() + Duration::from_millis(100),
            action: Action::Locate(deadline),
        });
    }
    /// Independently reap the helper and detach only this exporter's saved or
    /// exactly matched port. A stopped attach may already have reached the OS.
    pub fn close(&mut self) -> io::Result<()> {
        if self.closed {
            return Ok(());
        }
        self.closed = true;
        let mut errors = Vec::new();
        if let Some(mut job) = self.job.take() {
            if let Some(mut process) = job.process.take() {
                match process.stop() {
                    Ok(status) if matches!(job.action, Action::Detach) && status.success() => {
                        self.port = None;
                        self.target = None;
                    }
                    Ok(_) => (),
                    Err(e) => errors.push(e.to_string()),
                }
            }
        }
        if self.port.is_none() {
            if let Some(target) = &self.target {
                #[cfg(windows)]
                {
                    let mut args = target.args();
                    args.push("--stop".into());
                    if let Err(e) = bounded(&args) {
                        errors.push(e.to_string());
                    }
                }
                match bounded(&["port".into()]).and_then(|output| target.port(&output)) {
                    Ok(port) => {
                        self.port = port;
                        if port.is_none() {
                            self.target = None;
                        }
                    }
                    Err(e) => errors.push(e.to_string()),
                }
            }
        }
        if let Some(port) = self.port {
            match bounded(&detached_args(port)) {
                Ok(_) => {
                    self.port = None;
                    self.target = None;
                }
                Err(e) => errors.push(e.to_string()),
            }
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(failure(errors.join("; ")))
        }
    }
}
impl Drop for Importer {
    fn drop(&mut self) {
        let _ = self.close();
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn port_listing_matches_the_complete_exporter_uri() {
        // Linux usbip and usbip-win2 v.0.9.8.1 cmd_port formats.
        let target = Attachment {
            address: "127.0.0.1:3240".parse().unwrap(),
            busid: "1-1".into(),
        };
        let unrelated =
            "Port 00: <Port in Use> at Full Speed(12Mbps)\n -> usbip://127.0.0.1:4321/1-1\n";
        assert_eq!(target.port(unrelated).unwrap(), None);
        let listing = format!("{unrelated}Port 03: device in use at full-speed\n product\n -> usbip://127.0.0.1:3240/1-1\n -> remote bus/dev: 001/001\n");
        assert_eq!(target.port(&listing).unwrap(), Some(3));
        let linux = "Port 00: <Port in Use> at Full Speed(12Mbps)\n product\n 3-1 -> usbip://127.0.0.1:3240/1-1\n -> remote bus/dev 001/001\n";
        assert_eq!(target.port(linux).unwrap(), Some(0));
        assert!(target.port(&(listing.clone() + &listing)).is_err());
    }
}
