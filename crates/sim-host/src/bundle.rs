//! Relocatable native library/plugin paths supplied by the directory package.
use crate::launch::LaunchError;
use serde::Deserialize;
use std::{
    collections::BTreeMap,
    env,
    fs::File,
    io::Read,
    path::{Component, Path, PathBuf},
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Environment {
    schema: u32,
    paths: BTreeMap<String, Vec<PathBuf>>,
}

const VARIABLES: &[&str] = &[
    "LD_LIBRARY_PATH",
    "AMENT_PREFIX_PATH",
    "GZ_SIM_SYSTEM_PLUGIN_PATH",
    "GZ_SIM_PHYSICS_ENGINE_PATH",
    "GZ_RENDERING_ENGINE_PATH",
    "GZ_RENDERING_PLUGIN_PATH",
    "GZ_RENDERING_RESOURCE_PATH",
    "OGRE2_RESOURCE_PATH",
    "GZ_SIM_RESOURCE_PATH",
    "SDF_PATH",
];

/// Apply a package's native search paths before CLI parsing or creating threads.
/// A normal Cargo executable has no adjacent manifest and needs no adjustment.
pub fn configure() -> Result<(), LaunchError> {
    let executable = env::current_exe().map_err(|e| LaunchError::backend(e.to_string()))?;
    configure_at(executable.parent().expect("executable has a parent"))
}

fn configure_at(root: &Path) -> Result<(), LaunchError> {
    let manifest = root.join("runtime-env.json");
    let file = match File::open(&manifest) {
        Ok(file) => file,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(e) => return Err(LaunchError::backend(format!("{}: {e}", manifest.display()))),
    };
    let invalid =
        |message: String| LaunchError::backend(format!("{}: {message}", manifest.display()));
    let mut bytes = Vec::new();
    file.take(65_537)
        .read_to_end(&mut bytes)
        .map_err(|e| invalid(e.to_string()))?;
    if bytes.len() > 65_536 {
        return Err(invalid("native environment exceeds 64 KiB".into()));
    }
    let config: Environment = serde_json::from_slice(&bytes).map_err(|e| invalid(e.to_string()))?;
    if config.schema != 1 {
        return Err(invalid("unsupported native environment schema".into()));
    }
    let mut updates = Vec::new();
    for (name, relative) in config.paths {
        if !VARIABLES.contains(&name.as_str()) {
            return Err(invalid(format!(
                "unsupported native search variable {name}"
            )));
        }
        // gz-rendering consumes this as one resource root, not a colon list.
        // Appending a host value turns even two identical roots into a missing
        // filename and can make Ogre fail during shader initialization.
        let single_root = name == "GZ_RENDERING_RESOURCE_PATH";
        if single_root && relative.len() != 1 {
            return Err(invalid(format!("{name} requires exactly one directory")));
        }
        let mut paths = Vec::new();
        for path in relative {
            if path.as_os_str().is_empty()
                || path
                    .components()
                    .any(|part| !matches!(part, Component::Normal(_) | Component::CurDir))
            {
                return Err(invalid(format!(
                    "{name} requires package-relative directories"
                )));
            }
            let path = root.join(path);
            if !path.is_dir() {
                return Err(invalid(format!(
                    "native directory is missing: {}",
                    path.display()
                )));
            }
            paths.push(path);
        }
        if !single_root {
            if let Some(inherited) = env::var_os(&name) {
                paths.extend(env::split_paths(&inherited));
            }
        }
        let value = env::join_paths(paths).map_err(|e| invalid(e.to_string()))?;
        updates.push((name, value));
    }
    for (name, value) in updates {
        env::set_var(name, value);
    }
    Ok(())
}
