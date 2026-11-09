#![cfg(feature = "gazebo")]

use sim_host::physics::gazebo::{GazeboBindings, GazeboPhysics};

fn bindings() -> GazeboBindings {
    GazeboBindings {
        cameras: vec![(
            "front".into(),
            "sensors".into(),
            "body".into(),
            "camera".into(),
        )],
        lidars: vec![(
            "scan".into(),
            "sensors".into(),
            "body".into(),
            "lidar".into(),
        )],
        ..Default::default()
    }
}

#[test]
fn render_feature_and_binding_shape_admission_precedes_sdf_acquisition() {
    #[cfg(not(feature = "gazebo-rendering"))]
    assert!(GazeboPhysics::from_sdf_path("unused.sdf", bindings(), &[])
        .err()
        .unwrap()
        .to_string()
        .contains("gazebo-rendering"));
    #[cfg(feature = "gazebo-rendering")]
    {
        let mut duplicate = bindings();
        duplicate.cameras.push((
            "other".into(),
            "sensors".into(),
            "body".into(),
            "camera".into(),
        ));
        assert!(duplicate.validate().is_err());
        duplicate = bindings();
        duplicate.lidars[0].3.clear();
        assert!(GazeboPhysics::from_sdf_path("unused.sdf", duplicate, &[])
            .err()
            .unwrap()
            .to_string()
            .contains("nonempty"));
    }
}

#[cfg(feature = "gazebo-rendering")]
mod native {
    use super::*;
    use sim_host::physics::{CameraFrame, JointPhysics, LidarScan};
    use std::{
        path::{Path, PathBuf},
        sync::Mutex,
        time::{Duration, Instant},
    };

    static RENDERER: Mutex<()> = Mutex::new(());
    const SCENE: &str = include_str!("../../../sim/physics/gazebo_native/tests/rendering.sdf");

    fn file(xml: &str) -> (tempfile::TempDir, PathBuf) {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("world.sdf");
        std::fs::write(&path, xml).unwrap();
        (directory, path)
    }

    fn collect(world: &mut GazeboPhysics) -> (Vec<CameraFrame>, Vec<LidarScan>) {
        let deadline = Instant::now() + Duration::from_secs(8);
        let (mut frames, mut scans): (Vec<CameraFrame>, Vec<LidarScan>) = (vec![], vec![]);
        for _ in 0..800 {
            assert!(
                Instant::now() < deadline,
                "native rendering observation deadline"
            );
            world.step(1_000_000).unwrap();
            if let Some(frame) = world.get_camera("front").unwrap() {
                if frames
                    .last()
                    .is_none_or(|last| frame.timestamp_ns > last.timestamp_ns)
                {
                    frames.push(frame);
                }
            }
            if let Some(scan) = world.get_lidar("scan").unwrap() {
                if scans
                    .last()
                    .is_none_or(|last| scan.timestamp_ns > last.timestamp_ns)
                {
                    scans.push(scan);
                }
            }
            if frames.len() >= 2 && scans.len() >= 2 {
                return (frames, scans);
            }
            // Allow native transport/render workers to run; this host poll is
            // independent of the world's native clock and is not a barrier.
            std::thread::sleep(Duration::from_millis(1));
        }
        panic!("800 native ticks did not produce two rendering samples");
    }

    fn check_samples(frames: &[CameraFrame], scans: &[LidarScan]) {
        for frame in frames {
            assert_eq!(
                (frame.width, frame.height, frame.step, frame.data.len()),
                (64, 48, 192, 9216)
            );
            assert_eq!(frame.frame_id, "sensors::body::camera");
            let centre = (24 * 64 + 32) * 3;
            let rgb = &frame.data[centre..centre + 3];
            assert!(rgb[0] > 100 && rgb[1] < 30 && rgb[2] < 30);
            assert!(frame.data[..3].iter().all(|byte| *byte < 30));
        }
        for scan in scans {
            assert_eq!(scan.frame_id, "sensors::body::lidar");
            assert_eq!((scan.ranges_m.len(), scan.intensities.len()), (65, 65));
            assert_eq!(
                (scan.angle_min_rad, scan.angle_max_rad, scan.angle_step_rad),
                (-0.3, 0.3, 0.6 / 64.0)
            );
            assert_eq!((scan.range_min_m, scan.range_max_m), (0.1, 10.0));
            assert_eq!(
                (scan.vertical_angle_min_rad, scan.vertical_angle_max_rad),
                (0.0, 0.0)
            );
            assert!(scan.vertical_angle_step_rad.is_nan());
            // Independent primitive geometry: the front face is3 -1/2 metres.
            assert!((scan.ranges_m[32] - 2.5).abs() < 0.02);
            assert_eq!(
                (scan.ranges_m[0], scan.ranges_m[64]),
                (f64::INFINITY, f64::INFINITY)
            );
        }
    }

    #[test]
    fn native_render_profile_rejects_unsupported_shape_and_topic_before_server() {
        for (before, after, reason) in [
            (
                "<update_rate>20</update_rate>",
                "<update_rate>0</update_rate>",
                "1..1000Hz",
            ),
            ("R8G8B8", "L8", "plain RGB8"),
            ("/dm_render/scan", "/dm_render/image", "unique canonical"),
        ] {
            let (_directory, path) = file(&SCENE.replace(before, after));
            let error = GazeboPhysics::from_sdf_path(path, bindings(), &[])
                .err()
                .unwrap();
            assert!(error.to_string().contains(reason), "{error}");
            assert!(matches!(
                error.downcast_ref::<sim_core::Error>(),
                Some(sim_core::Error::Admission(_))
            ));
        }
    }

    #[test]
    fn actual_ogre2_snapshots_preserve_values_reset_and_native_single_owner() {
        let _lease = RENDERER.lock().unwrap();
        let (_directory, path) = file(SCENE);
        let mut world = GazeboPhysics::from_sdf_path(&path, bindings(), &[]).unwrap();
        assert_eq!(world.time_ns().unwrap(), 0);
        let error = GazeboPhysics::from_sdf_path(&path, bindings(), &[])
            .err()
            .unwrap();
        assert!(error.to_string().contains("another Gazebo rendering owner"));
        let (frames, scans) = collect(&mut world);
        check_samples(&frames, &scans);
        assert!(!std::fs::read_to_string("/proc/self/maps")
            .unwrap()
            .contains("libpython"));
        let before = world.time_ns().unwrap();
        world.get_camera("front").unwrap();
        world.get_lidar("scan").unwrap();
        assert_eq!(world.time_ns().unwrap(), before);
        assert!(world.get_camera("absent").is_err());
        std::fs::remove_file(path).unwrap();
        world.reset().unwrap();
        assert_eq!(world.time_ns().unwrap(), 0);
        assert!(world
            .get_camera("front")
            .unwrap()
            .is_none_or(|frame| frame.timestamp_ns == 0));
        assert!(world
            .get_lidar("scan")
            .unwrap()
            .is_none_or(|scan| scan.timestamp_ns == 0));
        let (frames, scans) = collect(&mut world);
        check_samples(&frames, &scans);
        world.close().unwrap();
        assert!(world.get_camera("front").is_err());
        let (_directory, path) = file(SCENE);
        let mut replacement = GazeboPhysics::from_sdf_path(path, bindings(), &[]).unwrap();
        let (frames, scans) = collect(&mut replacement);
        check_samples(&frames, &scans);
        replacement.close().unwrap();
    }

    fn copy_tree(source: &Path, target: &Path) {
        std::fs::create_dir(target).unwrap();
        for entry in std::fs::read_dir(source).unwrap() {
            let entry = entry.unwrap();
            let destination = target.join(entry.file_name());
            if entry.file_type().unwrap().is_dir() {
                copy_tree(&entry.path(), &destination);
            } else {
                std::fs::copy(entry.path(), destination).unwrap();
            }
        }
    }
    fn channel_stats(frame: &CameraFrame) -> ([f64; 3], f64) {
        let n = (frame.data.len() / 3) as f64;
        let means = std::array::from_fn(|channel| {
            frame
                .data
                .iter()
                .skip(channel)
                .step_by(3)
                .map(|value| *value as f64)
                .sum::<f64>()
                / n
        });
        let variance = frame
            .data
            .iter()
            .step_by(3)
            .map(|value| (*value as f64 - means[0]).powi(2))
            .sum::<f64>()
            / n;
        (means, variance.sqrt())
    }

    #[test]
    fn actual_local_texture_and_independent_wall_geometry_survive_the_value_bridge() {
        let _lease = RENDERER.lock().unwrap();
        let models = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../models");
        let resources = models.join("vendor/rmua19/resource/models");
        let directory = tempfile::tempdir().unwrap();
        let control = directory.path().join("models");
        copy_tree(&resources, &control);
        let model = control.join("rmua19_battlefield/model.sdf");
        let mut xml = std::fs::read_to_string(&model).unwrap();
        let mut removed = 0;
        while let Some(start) = xml.find("<albedo_map>") {
            let end = xml[start..].find("</albedo_map>").unwrap() + start + "</albedo_map>".len();
            xml.replace_range(start..end, "");
            removed += 1;
        }
        assert_eq!(removed, 11);
        std::fs::write(model, xml).unwrap();
        let mut observations = vec![];
        for resource in [resources, control] {
            let mut world = GazeboPhysics::from_sdf_path(
                models.join("gazebo_rmua19_sensors.sdf"),
                bindings(),
                &[resource],
            )
            .unwrap();
            let (frames, scans) = collect(&mut world);
            for scan in scans {
                // Fixed source wall3 at x4.1 +/- .1; sensor x3 faces its x4 face.
                assert!((scan.ranges_m[32] - 1.0).abs() < 0.02);
                for index in [0, 64] {
                    assert!((scan.ranges_m[index] - 1.0 / 0.3_f64.cos()).abs() < 0.02);
                }
                assert!(scan.ranges_m.iter().all(|value| value.is_finite()));
            }
            observations.push(frames.last().unwrap().clone());
            world.close().unwrap();
        }
        let (texture, plain) = (&observations[0], &observations[1]);
        let (means, grain) = channel_stats(texture);
        let (_, plain_grain) = channel_stats(plain);
        assert!(means[0] > means[1] && means[1] > means[2]);
        assert!(plain
            .data
            .chunks_exact(3)
            .all(|pixel| pixel[0] == pixel[1] && pixel[1] == pixel[2]));
        assert!(grain > plain_grain);
        assert_ne!(texture.data, plain.data);
    }
}
