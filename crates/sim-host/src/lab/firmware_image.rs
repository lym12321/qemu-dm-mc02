//! Bounded ELF snapshots; QEMU never maps the mutable linker output in Lab.
use object::{Object, ObjectSection, ObjectSegment};
#[cfg(feature = "lab-session")]
use sha2::{Digest, Sha256};
use sim_core::{Error, Failure, Result};
use std::{
    fs::{self, File, Metadata},
    io::{Read, Write},
    path::Path,
    sync::Arc,
};
#[cfg(feature = "lab-session")]
use std::{
    path::PathBuf,
    time::{Duration, Instant},
};

const FILE_LIMIT: u64 = 64 * 1024 * 1024;
#[cfg(feature = "lab-session")]
const SETTLE_INTERVAL: Duration = Duration::from_millis(500);

fn refused(message: impl Into<String>) -> Failure {
    Arc::new(Error::Admission(format!(
        "firmware ELF: {}",
        message.into()
    )))
}

pub(super) fn read_snapshot(path: &Path) -> Result<tempfile::NamedTempFile> {
    snapshot(&read_candidate(path)?)
}

#[cfg(feature = "lab-session")]
pub(super) struct FirmwareWatch {
    path: PathBuf,
    loaded: [u8; 32],
    candidate: Option<([u8; 32], Instant)>,
}

#[cfg(feature = "lab-session")]
impl FirmwareWatch {
    pub(super) fn from_snapshot(path: &Path, image: &tempfile::NamedTempFile) -> Result<Self> {
        let bytes = fs::read(image.path())
            .map_err(|error| refused(format!("cannot read admitted snapshot: {error}")))?;
        Ok(Self {
            path: path.to_owned(),
            loaded: Sha256::digest(bytes).into(),
            candidate: None,
        })
    }

    /// The initial snapshot is also the first image passed to QEMU. This ties
    /// the loaded digest to actual input bytes, even when the linker rewrites
    /// the source between launch admission and QEMU opening its kernel.
    pub(super) fn new(path: &Path) -> Result<(Self, tempfile::NamedTempFile)> {
        let bytes = read_candidate(path)?;
        let loaded = Sha256::digest(&bytes).into();
        let image = snapshot(&bytes)?;
        Ok((
            Self {
                path: path.to_owned(),
                loaded,
                candidate: None,
            },
            image,
        ))
    }

    /// Missing, unreadable or incomplete input never replaces the current
    /// image. The caller may display/deduplicate the transient admission error.
    pub(super) fn poll(&mut self, now: Instant) -> Result<Option<tempfile::NamedTempFile>> {
        let bytes = match read_candidate(&self.path) {
            Ok(bytes) => bytes,
            Err(error) => {
                self.candidate = None;
                return Err(error);
            }
        };
        let digest: [u8; 32] = Sha256::digest(&bytes).into();
        if digest == self.loaded {
            self.candidate = None;
            return Ok(None);
        }
        match self.candidate {
            Some((candidate, since)) if candidate == digest => {
                if now.saturating_duration_since(since) >= SETTLE_INTERVAL {
                    let image = snapshot(&bytes)?;
                    self.loaded = digest;
                    self.candidate = None;
                    return Ok(Some(image));
                }
            }
            _ => self.candidate = Some((digest, now)),
        }
        Ok(None)
    }
}

fn unchanged(before: &Metadata, after: &Metadata) -> bool {
    before.len() == after.len() && before.modified().ok() == after.modified().ok()
}

fn read_candidate(path: &Path) -> Result<Vec<u8>> {
    let read = || -> std::io::Result<(Vec<u8>, bool)> {
        let source_before = fs::metadata(path)?;
        if !source_before.is_file() || source_before.len() > FILE_LIMIT {
            return Ok((Vec::new(), false));
        }
        let file = File::open(path)?;
        let before = file.metadata()?;
        if !before.is_file() || before.len() > FILE_LIMIT {
            return Ok((Vec::new(), false));
        }
        let mut bytes = Vec::new();
        (&file).take(FILE_LIMIT + 1).read_to_end(&mut bytes)?;
        let after = file.metadata()?;
        let source = fs::metadata(path)?;
        let stable = unchanged(&source_before, &before)
            && unchanged(&before, &after)
            && unchanged(&after, &source)
            && after.len() == bytes.len() as u64;
        Ok((bytes, stable))
    };
    let (bytes, stable) =
        read().map_err(|error| refused(format!("cannot read {}: {error}", path.display())))?;
    if bytes.len() as u64 > FILE_LIMIT || !stable {
        return Err(refused(
            "ELF is changing, not a regular file, or exceeds 64 MiB",
        ));
    }
    validate_elf(&bytes)?;
    Ok(bytes)
}

fn validate_elf(bytes: &[u8]) -> Result<()> {
    let elf = object::read::elf::ElfFile32::<object::LittleEndian>::parse(bytes)
        .map_err(|error| refused(format!("incomplete or invalid ELF: {error}")))?;
    if elf.architecture() != object::Architecture::Arm
        || elf.kind() != object::ObjectKind::Executable
    {
        return Err(refused("expected a little-endian ARM ELF32 executable"));
    }
    let mut has_load_data = false;
    for segment in elf.segments() {
        let data = segment
            .data()
            .map_err(|error| refused(format!("incomplete ELF load segment: {error}")))?;
        if data.len() as u64 > segment.size()
            || segment.address().checked_add(segment.size()).is_none()
        {
            return Err(refused("invalid ELF load segment size"));
        }
        has_load_data |= !data.is_empty();
    }
    if !has_load_data {
        return Err(refused("ELF has no loadable data"));
    }
    for section in elf.sections() {
        section
            .data()
            .map_err(|error| refused(format!("incomplete ELF section: {error}")))?;
    }
    Ok(())
}

fn snapshot(bytes: &[u8]) -> Result<tempfile::NamedTempFile> {
    let mut image = tempfile::Builder::new()
        .prefix("sim-lab-")
        .suffix(".elf")
        .tempfile()
        .map_err(|error| refused(format!("cannot create ELF snapshot: {error}")))?;
    image
        .write_all(bytes)
        .and_then(|()| image.flush())
        .map_err(|error| refused(format!("cannot write ELF snapshot: {error}")))?;
    Ok(image)
}

#[cfg(all(test, feature = "lab-session"))]
pub(super) mod tests {
    use super::*;
    const POLL_INTERVAL: Duration = Duration::from_millis(250);

    // ELF32 header + one PT_LOAD, from the ELF gABI field layout, EM_ARM=40.
    // The payload stands in for vector-table bytes; no QEMU/board is involved.
    pub(in crate::lab) fn elf(payload: u8) -> Vec<u8> {
        let mut bytes = vec![0; 88];
        bytes[..16].copy_from_slice(b"\x7fELF\x01\x01\x01\0\0\0\0\0\0\0\0\0");
        for (offset, value) in [(16, 2u16), (18, 40), (40, 52), (42, 32), (44, 1)] {
            bytes[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
        }
        for (offset, value) in [
            (20, 1u32),
            (24, 0x0800_0001),
            (28, 52),
            (52, 1),
            (56, 84),
            (60, 0x0800_0000),
            (64, 0x0800_0000),
            (68, 4),
            (72, 4),
            (76, 5),
            (80, 4),
        ] {
            bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
        }
        bytes[84..].fill(payload);
        bytes
    }

    #[test]
    fn snapshots_are_immutable_and_equal_bytes_do_not_reload() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("firmware.elf");
        let original = elf(1);
        fs::write(&path, &original).unwrap();
        let now = Instant::now();
        let (mut watch, initial) = FirmwareWatch::new(&path).unwrap();
        fs::write(&path, &original).unwrap();
        assert!(watch.poll(now + POLL_INTERVAL).unwrap().is_none());
        fs::write(&path, elf(2)).unwrap();
        assert_eq!(fs::read(initial.path()).unwrap(), original);
        assert!(watch.poll(now + POLL_INTERVAL * 2).unwrap().is_none());
        assert!(watch.poll(now + POLL_INTERVAL * 3).unwrap().is_none());
        let replacement = watch.poll(now + POLL_INTERVAL * 4).unwrap().unwrap();
        assert_eq!(fs::read(replacement.path()).unwrap(), elf(2));
        assert!(watch.poll(now + POLL_INTERVAL * 8).unwrap().is_none());
    }

    #[test]
    fn deletion_and_incomplete_link_output_keep_the_last_good_image() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("firmware.elf");
        fs::write(&path, elf(1)).unwrap();
        let now = Instant::now();
        let (mut watch, initial) = FirmwareWatch::new(&path).unwrap();
        fs::remove_file(&path).unwrap();
        assert!(watch.poll(now + POLL_INTERVAL).is_err());
        fs::write(&path, &elf(2)[..86]).unwrap();
        assert!(watch.poll(now + POLL_INTERVAL * 2).is_err());
        fs::write(&path, elf(2)).unwrap();
        assert!(watch.poll(now + POLL_INTERVAL * 3).unwrap().is_none());
        fs::write(&path, b"\x7fELF").unwrap();
        assert!(watch.poll(now + POLL_INTERVAL * 5).is_err());
        let staged = directory.path().join("linked.elf");
        fs::write(&staged, elf(2)).unwrap();
        fs::rename(&staged, &path).unwrap();
        assert!(watch.poll(now + POLL_INTERVAL * 6).unwrap().is_none());
        let replacement = watch.poll(now + POLL_INTERVAL * 8).unwrap().unwrap();
        assert_eq!(fs::read(initial.path()).unwrap(), elf(1));
        assert_eq!(fs::read(replacement.path()).unwrap(), elf(2));
    }

    #[test]
    fn changed_bytes_are_detected_even_when_length_and_mtime_are_preserved() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("firmware.elf");
        fs::write(&path, elf(1)).unwrap();
        let modified = fs::metadata(&path).unwrap().modified().unwrap();
        let now = Instant::now();
        let (mut watch, _initial) = FirmwareWatch::new(&path).unwrap();
        fs::write(&path, elf(2)).unwrap();
        File::options()
            .write(true)
            .open(&path)
            .unwrap()
            .set_times(fs::FileTimes::new().set_modified(modified))
            .unwrap();
        assert!(watch.poll(now + POLL_INTERVAL).unwrap().is_none());
        let replacement = watch.poll(now + POLL_INTERVAL * 3).unwrap().unwrap();
        assert_eq!(fs::read(replacement.path()).unwrap(), elf(2));
    }

    #[test]
    fn malformed_and_incompatible_elf_never_become_initial_images() {
        for bytes in [
            vec![],
            b"\x7fELF".to_vec(),
            elf(1)[..86].to_vec(),
            {
                let mut bytes = elf(1);
                bytes[5] = 2;
                bytes
            },
            {
                let mut bytes = elf(1);
                bytes[18] = 3;
                bytes
            },
            {
                let mut bytes = elf(1);
                bytes[16] = 1;
                bytes
            },
            {
                let mut bytes = elf(1);
                bytes[72] = 2;
                bytes
            },
            {
                let mut bytes = elf(1);
                bytes[52] = 0;
                bytes
            },
        ] {
            assert!(validate_elf(&bytes).is_err());
        }
    }

    #[test]
    fn oversized_source_is_rejected_before_reading_it() {
        let file = tempfile::NamedTempFile::new().unwrap();
        file.as_file().set_len(FILE_LIMIT + 1).unwrap();
        assert!(FirmwareWatch::new(file.path()).is_err());
    }

    #[test]
    fn truncated_debug_tail_is_rejected_even_when_load_segments_are_complete() {
        let mut bytes = elf(1);
        bytes.resize(236, 0);
        // Three ELF32 section headers: null, .debug_info, .shstrtab. The
        // debug payload follows the complete header/string tables and PT_LOAD.
        for (offset, value) in [(46, 40u16), (48, 3), (50, 2)] {
            bytes[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
        }
        for (offset, value) in [
            (32, 88u32),
            (128, 1),
            (132, 1),
            (144, 232),
            (148, 4),
            (160, 1),
            (168, 13),
            (172, 3),
            (184, 208),
            (188, 23),
            (200, 1),
        ] {
            bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
        }
        bytes[208..231].copy_from_slice(b"\0.debug_info\0.shstrtab\0");
        validate_elf(&bytes).unwrap();
        bytes.pop();
        assert!(validate_elf(&bytes).is_err());
    }
}
