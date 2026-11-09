"""Build-time collection of the Linux native runtime; never shipped as an entry point.

Use the system ELF tools for the dependency graph. The explicit extra roots are
the plugins loaded by the already admitted Gazebo/ROS profiles, not all SDKs.
"""
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess


# Ubuntu24.04 glibc and the host graphics-driver ABI remain system-owned.
# Drivers are selected by the host EGL/GLVND loader, never copied from a builder.
SYSTEM_LIBRARIES = (
    "ld-linux-x86-64.so.*", "libc.so.*", "libm.so.*", "libdl.so.*", "libpthread.so.*",
    "librt.so.*", "libresolv.so.*", "libutil.so.*", "libnss_*.so.*",
    "libEGL*.so.*", "libGL.so.*", "libGLX*.so.*", "libGLdispatch.so.*",
    "libOpenGL.so.*", "libglapi.so.*", "libdrm*.so.*", "libgbm.so.*",
)
ROS_MESSAGES = ("builtin_interfaces", "geometry_msgs", "sensor_msgs", "std_msgs",
                "std_srvs", "service_msgs", "rmw_dds_common", "rosgraph_msgs", "statistics_msgs",
                "rcl_interfaces", "type_description_interfaces")


def run(*argv, env=None):
    return subprocess.check_output(argv, env=env, text=True, encoding="utf-8").strip()


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def collect(output, *, gazebo_prefix, ros_prefix, mujoco_prefix):
    output = output.resolve()
    gazebo, ros, mujoco = (Path(path).resolve(strict=True)
                           for path in (gazebo_prefix, ros_prefix, mujoco_prefix))
    vendors = ros / "opt"
    env = dict(os.environ)
    search = [gazebo / "lib", ros / "lib", mujoco / "lib"]
    search += sorted(path / "lib" for path in vendors.iterdir() if (path / "lib").is_dir())
    env["LD_LIBRARY_PATH"] = ":".join(map(str, search))
    patchelf = shutil.which("patchelf")
    if not patchelf:
        raise ValueError("Linux packaging requires the standard patchelf tool")
    lib = output / "lib"
    lib.mkdir()
    native = output / "licenses/native"
    native.mkdir(parents=True)
    shutil.copytree("/usr/share/common-licenses", native / "common-licenses")
    records, system, owners, copied, visited = [], {}, {}, {}, set()
    resolved_targets = {}
    seeds = [output / "sim", output / "bin/qemu-system-arm"]
    for seed in seeds:
        listing = run("ldd", str(seed), env=env)
        if any(marker in listing.lower() for marker in ("/nix/", "libpython", "not found")):
            raise ValueError(f"Linux release executable has a nonportable/Python dependency: {seed}\n{listing}")

    def elf(path):
        with path.open("rb") as source:
            return source.read(4) == b"\x7fELF"

    def native_owner(path):
        if path.is_relative_to(gazebo):
            return "gz-sim-8.15.0-no-python"
        if path.is_relative_to(mujoco):
            return "mujoco-3.12.0"
        query = subprocess.run(["dpkg-query", "-S", str(path)], text=True,
                               capture_output=True)
        if query.returncode:
            raise ValueError(f"no Debian package or fixed native source owns {path}")
        return query.stdout.split(": ", 1)[0].splitlines()[0]

    def license_owner(owner):
        if owner in owners:
            return
        directory = native / owner.replace(":", "_")
        directory.mkdir()
        if owner == "gz-sim-8.15.0-no-python":
            source = gazebo.parent / "source"
            commit = run("git", "-C", str(source), "rev-parse", "HEAD")
            if commit != "446a44335a45b704b4d36dabcc5508ee34eeb3d8":
                raise ValueError("Gazebo SDK source must be the admitted pristine8.15.0 commit")
            if run("git", "-C", str(source), "status", "--porcelain"):
                raise ValueError("Gazebo SDK source has local modifications")
            shutil.copy2(source / "LICENSE", directory / "LICENSE")
            owners[owner] = dict(source="https://github.com/gazebosim/gz-sim.git", commit=commit,
                                 build_options=["SKIP_PYBIND11=ON", "BUILD_TESTING=OFF"],
                                 source_license_sha256=digest(source / "LICENSE"))
        elif owner == "mujoco-3.12.0":
            for name in ("LICENSE", "THIRD_PARTY_NOTICES"):
                shutil.copy2(mujoco / name, directory / name)
            owners[owner] = dict(version="3.12.0", source="https://github.com/google-deepmind/mujoco",
                                 release="https://github.com/google-deepmind/mujoco/releases/tag/3.12.0")
        else:
            metadata = run("dpkg-query", "-W", "-f=${Package}\n${Version}\n${source:Package}\n${source:Version}\n${Homepage}\n", owner)
            (directory / "PACKAGE.txt").write_text(metadata + "\n")
            copyright = Path("/usr/share/doc") / owner.split(":")[0] / "copyright"
            if not copyright.is_file():
                raise ValueError(f"missing installed copyright/license for {owner}")
            shutil.copy2(copyright, directory / "copyright")
            owners[owner] = dict(debian_metadata=metadata,
                                 source_repository="Ubuntu24.04 archive or ROS2 Jazzy apt source package")
        owners[owner]["licenses"] = str(directory.relative_to(output))

    def add(path, alias=None):
        path = path.resolve(strict=True)
        target = resolved_targets.get(path)
        if target is None:
            if "python" in path.name.lower() or not elf(path):
                raise ValueError(f"runtime dependency must be a Python-free ELF: {path}")
            if path.name.startswith("libgz-sim8.so") and not path.is_relative_to(gazebo):
                raise ValueError(f"Gazebo core did not resolve to the canonical no-Python SDK: {path}")
            owner = native_owner(path)
            license_owner(owner)
            checksum = digest(path)
            target = lib / path.name
            if path.name in copied and copied[path.name] != checksum:
                raise ValueError(f"native library basename collision: {path.name}")
            if path.name not in copied:
                shutil.copy2(path, target)
                copied[path.name] = checksum
                seeds.append(path)
                records.append(dict(file=str(target.relative_to(output)), source=str(path),
                                    original_sha256=checksum, package=owner))
            resolved_targets[path] = target
        if alias and alias != path.name:
            link = lib / alias
            if not link.exists():
                link.symlink_to(path.name)
            elif link.resolve() != target.resolve():
                raise ValueError(f"native SONAME collision: {alias}")
        return target

    def plugin(path, category):
        target = add(path)
        directory = output / "plugins" / category
        directory.mkdir(parents=True, exist_ok=True)
        names = {path.name, path.resolve().name}
        soname = run("patchelf", "--print-soname", str(path))
        if soname:
            names.add(soname)
        # Gazebo/Ogre lookup requests the unversioned .so.
        names.add(path.name.split(".so", 1)[0] + ".so")
        for name in names:
            link = directory / name
            if not link.exists():
                link.symlink_to(Path("../../lib") / target.name)

    for pattern in ("*physics-system.so*", "*sensors-system.so*",
                    "*user-commands-system.so*", "*scene-broadcaster-system.so*"):
        paths = sorted((gazebo / "lib/gz-sim-8/plugins").glob(pattern))
        if not paths:
            raise ValueError(f"missing canonical Gazebo plugin: {pattern}")
        plugin(paths[0], "systems")
    plugin(vendors / "gz_physics_vendor/lib/gz-physics-7/engine-plugins/libgz-physics-dartsim-plugin.so", "physics")
    plugin(vendors / "gz_rendering_vendor/lib/gz-rendering-8/engine-plugins/libgz-rendering-ogre2.so", "rendering")
    for name in ("RenderSystem_GL3Plus.so", "Plugin_ParticleFX.so"):
        plugin(vendors / "gz_ogre_next_vendor/lib/OGRE-Next" / name, "ogre2")

    # RMW and message type support are dynamically selected by the native ROS APIs.
    for name in ("librmw_fastrtps_cpp.so", "librosidl_typesupport_fastrtps_cpp.so",
                 "librosidl_typesupport_fastrtps_c.so", "librcl_logging_spdlog.so"):
        add(ros / "lib" / name, name)
    for package in ROS_MESSAGES:
        for path in sorted((ros / "lib").glob(f"lib{package}__rosidl_*.so")):
            if "generator_py" not in path.name:
                add(path, path.name)
    bridge_dir = output / "bin/ros_gz_bridge"
    bridge_dir.mkdir()
    for name in ("bridge_node", "parameter_bridge"):
        source = ros / "lib/ros_gz_bridge" / name
        target = bridge_dir / name
        shutil.copy2(source, target)
        owner = native_owner(source)
        license_owner(owner)
        records.append(dict(file=str(target.relative_to(output)), source=str(source),
                            original_sha256=digest(source), package=owner))
        seeds.append(target)

    while seeds:
        source = seeds.pop()
        identity = str(source.resolve())
        if identity in visited:
            continue
        visited.add(identity)
        listing = run("ldd", str(source), env=env)
        if "not found" in listing or "libpython" in listing.lower():
            raise ValueError(f"unresolved or Python native dependency of {source}:\n{listing}")
        for line in listing.splitlines():
            match = re.match(r"\s*(\S+)\s+=>\s+(/\S+)", line)
            direct = re.match(r"\s*(/\S+)\s+\(", line)
            if match:
                name, filename = match.groups()
            elif direct:
                filename = direct.group(1)
                name = Path(filename).name
            else:
                continue
            path = Path(filename).resolve(strict=True)
            if any(fnmatch.fnmatch(name, pattern) for pattern in SYSTEM_LIBRARIES):
                system[name] = dict(resolved_on_builder=str(path), sha256=digest(path))
            else:
                add(path, name)

    resources = [(gazebo / "share/gz/gz-sim8", output / "share/gz/gz-sim8"),
                 (vendors / "gz_rendering_vendor/share/gz/gz-rendering8", output / "share/gz/gz-rendering8"),
                 (vendors / "sdformat_vendor/share/sdformat14", output / "share/sdformat14")]
    for source, target in resources:
        if not source.is_dir():
            raise ValueError(f"missing native SDK resource directory: {source}")
        shutil.copytree(source, target, symlinks=False)

    # Keep native ament lookup metadata, not Python modules, environment hooks,
    # build files or the installed ROS command-line stack.
    ros_packages = {owner.removeprefix("ros-jazzy-").replace("-", "_")
                    for owner in owners if owner.startswith("ros-jazzy-")}
    ros_packages.update(ROS_MESSAGES)
    index = ros / "share/ament_index/resource_index"
    for kind in ("packages", "rmw_typesupport", "rmw_typesupport_c",
                 "rosidl_typesupport_c", "rosidl_typesupport_cpp", "rosidl_interfaces"):
        for source in (index / kind).iterdir():
            if source.name in ros_packages:
                target = output / "share/ament_index/resource_index" / kind / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
    for package in ros_packages:
        source = ros / "share" / package / "package.xml"
        if source.is_file():
            target = output / "share" / package / "package.xml"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)

    environment = {"LD_LIBRARY_PATH": ["lib"], "AMENT_PREFIX_PATH": ["."],
        "GZ_SIM_SYSTEM_PLUGIN_PATH": ["plugins/systems"],
        "GZ_SIM_PHYSICS_ENGINE_PATH": ["plugins/physics"],
        "GZ_RENDERING_ENGINE_PATH": ["plugins/rendering"],
        "GZ_RENDERING_PLUGIN_PATH": ["plugins/rendering"],
        "GZ_RENDERING_RESOURCE_PATH": ["share/gz/gz-rendering8"],
        "OGRE2_RESOURCE_PATH": ["plugins/ogre2"],
        "GZ_SIM_RESOURCE_PATH": ["share/gz/gz-sim8"], "SDF_PATH": ["share/sdformat14"]}
    (output / "runtime-env.json").write_text(json.dumps(dict(schema=1, paths=environment), indent=2) + "\n")
    for path in [output / "sim", *sorted((output / "bin").rglob("*")), *sorted(lib.iterdir())]:
        if not path.is_file() or path.is_symlink() or not elf(path):
            continue
        relative = os.path.relpath(lib, path.parent)
        # dlopen preserves the category symlink pathname when expanding ORIGIN.
        # The same one physical ELF therefore supports lib/ and plugins/<kind>/.
        rpath = "$ORIGIN:$ORIGIN/../../lib" if relative == "." else f"$ORIGIN/{relative}"
        subprocess.run([patchelf, "--set-rpath", rpath, str(path)], check=True)
    for record in records:
        record["sha256"] = digest(output / record["file"])
    result = dict(schema=1, platform="Ubuntu24.04 x86_64, glibc>=2.39, Linux pidfd support",
        system_boundary="host glibc/ELF loader and EGL/GLVND/Mesa or vendor GPU driver stack",
        system_library_patterns=SYSTEM_LIBRARIES, system_libraries=system,
        files=records, packages=owners, environment="runtime-env.json")
    (output / "native-runtime.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
