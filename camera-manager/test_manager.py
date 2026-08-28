import json
import os
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch

from manager import (
    NIGHT_FILTERS,
    MODE_VALIDATION_VERSION,
    RUNTIME,
    apply_stream_state,
    best_mode,
    capabilities_hash,
    evaluate_light,
    is_capture_node,
    jpeg_luminance,
    merge_usb_state,
    monitor_once,
    parse_modes,
    parse_v4l2_groups,
    public_state,
    public_stream_source,
    ranked_modes,
    render,
    sample_luminance,
    select_verified_mode,
    usb_identity,
    validate_usb_mode,
    verify_usb_camera,
    with_night_defaults,
)


def camera(**overrides):
    value = {
        "id": "video0",
        "kind": "usb",
        "path": "/dev/video0",
        "name": "video0",
        "label": "Test Camera (usb-a)",
        "base_label": "Test Camera",
        "enabled": True,
        "status": "available",
        "width": 1920,
        "height": 1080,
        "fps": 30,
        "input_format": "mjpeg",
    }
    value.update(overrides)
    return with_night_defaults(value)


class NativeModeTests(unittest.TestCase):
    MODES = """
        [0]: 'YUYV'
          Size: Discrete 640x480
            Interval: Discrete 0.033s (30.000 fps)
          Size: Discrete 1280x720
            Interval: Discrete 0.133s (7.500 fps)
        [1]: 'MJPG'
          Size: Discrete 640x480
            Interval: Discrete 0.033s (30.000 fps)
          Size: Discrete 1280x720
            Interval: Discrete 0.033s (30.000 fps)
          Size: Discrete 1280x960
            Interval: Discrete 0.033s (30.000 fps)
    """

    def test_all_discrete_modes_are_parsed(self):
        modes = parse_modes(self.MODES)
        self.assertEqual(5, len(modes))
        self.assertIn({"width": 1280, "height": 720, "fps": 30.0, "input_format": "mjpeg"}, modes)

    def test_c270_is_capped_at_official_720p_limit(self):
        modes = ranked_modes(parse_modes(self.MODES), {"vendor": "046d", "product": "0825"})
        self.assertEqual((1280, 720, 30.0, "mjpeg"),
                         (modes[0]["width"], modes[0]["height"], modes[0]["fps"], modes[0]["input_format"]))
        self.assertNotIn((1280, 960), {(mode["width"], mode["height"]) for mode in modes})

    def test_corrupt_mjpeg_falls_back_to_highest_verified_yuyv(self):
        attempts = []

        def validator(_, mode):
            attempts.append(mode_tuple := (mode["width"], mode["height"], mode["fps"], mode["input_format"]))
            return (mode["input_format"] == "yuyv422", "malformed MJPEG")

        selected, reason = select_verified_mode(
            "/dev/video0", parse_modes(self.MODES), {"vendor": "046d", "product": "0825"}, validator)
        self.assertEqual((1280, 720, 7.5, "yuyv422"),
                         (selected["width"], selected["height"], selected["fps"], selected["input_format"]))
        self.assertEqual("mjpeg", attempts[0][3])
        self.assertIn("Fallback from 1280x720@30 mjpeg", reason)

    def test_verified_mode_is_persisted_without_retesting(self):
        modes = parse_modes(self.MODES)
        selected = {"width": 1280, "height": 720, "fps": 30.0, "input_format": "mjpeg"}
        value = camera(id="usb_cam", kind="usb", available_modes=modes, selected_mode=selected,
                       capabilities_hash=capabilities_hash(modes),
                       validated_capabilities_hash=capabilities_hash(modes), validation_status="verified")
        value["mode_validation_version"] = MODE_VALIDATION_VERSION
        validator = Mock(side_effect=AssertionError("healthy mode should not be retested"))
        self.assertFalse(verify_usb_camera(value, validator=validator))
        validator.assert_not_called()
        self.assertEqual((1280, 720, 30.0, "mjpeg"),
                         (value["width"], value["height"], value["fps"], value["input_format"]))

    def test_old_validation_version_is_retested_after_decoder_fix(self):
        modes = parse_modes(self.MODES)
        value = camera(id="usb_cam", kind="usb", available_modes=modes,
                       selected_mode=modes[-1], capabilities_hash=capabilities_hash(modes),
                       validated_capabilities_hash=capabilities_hash(modes), validation_status="verified",
                       mode_validation_version=MODE_VALIDATION_VERSION - 1, usb_attributes={})
        validator = Mock(return_value=(True, ""))
        self.assertTrue(verify_usb_camera(value, validator=validator))
        validator.assert_called()
        self.assertEqual(MODE_VALIDATION_VERSION, value["mode_validation_version"])

    def test_all_failed_modes_mark_only_camera_unhealthy(self):
        modes = parse_modes(self.MODES)
        value = camera(id="usb_bad", kind="usb", available_modes=modes,
                       capabilities_hash=capabilities_hash(modes), usb_attributes={})
        verify_usb_camera(value, force=True, validator=lambda *_: (False, "decode failed"))
        self.assertEqual("unhealthy", value["status"])
        self.assertEqual("failed", value["validation_status"])
        self.assertIn("cameras: {}", render({"usb_bad": value}))

    def test_fps_is_selected_from_native_resolution(self):
        modes = """
        Size: Discrete 640x480
          Interval: Discrete 0.033s (30.000 fps)
        Size: Discrete 1920x1080
          Interval: Discrete 0.200s (5.000 fps)
        """
        self.assertEqual((1920, 1080, 5.0, "mjpeg"), best_mode(modes))

    def test_highest_fps_wins_for_same_resolution(self):
        modes = """
        Size: Discrete 1280x720
          Interval: Discrete 0.066s (15.000 fps)
          Interval: Discrete 0.033s (30.000 fps)
        """
        self.assertEqual((1280, 720, 30.0, "mjpeg"), best_mode(modes))

    def test_compressed_format_wins_at_same_native_size(self):
        modes = """
        [0]: 'YUYV'
          Size: Discrete 1920x1080
            Interval: Discrete 0.033s (30.000 fps)
        [1]: 'MJPG'
          Size: Discrete 1920x1080
            Interval: Discrete 0.033s (30.000 fps)
        """
        self.assertEqual((1920, 1080, 30.0, "mjpeg"), best_mode(modes))


class RenderTests(unittest.TestCase):
    def test_usb_stream_has_hidden_native_source_and_public_h264(self):
        config = render({"video0": camera()})
        self.assertIn("video0__source:", config)
        self.assertIn("input_format=mjpeg&video_size=1920x1080&framerate=30#video=copy", config)
        self.assertIn("video0: # CasaGuard profile: day", config)
        self.assertIn('"ffmpeg:video0__source#video=h264"', config)
        self.assertNotIn("__casaguard_wait", config)
        self.assertIn("fps: 5", config)
        self.assertIn("enabled: true", config)
        self.assertIn("Native: video0", config)

    def test_night_filter_is_aggressive_without_scaling_or_fps_change(self):
        config = render({"video0": camera(night_active=True)})
        self.assertIn("video0: # CasaGuard profile: night", config)
        self.assertIn("video_size=1920x1080&framerate=30", config)
        source = public_stream_source(camera(night_active=True))
        self.assertIn(NIGHT_FILTERS["aggressive"], source)
        self.assertNotIn("scale=", source)
        self.assertNotIn("-r ", source)

    def test_native_h264_day_mode_is_copied(self):
        config = render({"video0": camera(input_format="h264")})
        self.assertIn("input_format=h264", config)
        self.assertEqual("ffmpeg:video0__source#video=copy", public_stream_source(camera(input_format="h264")))

    def test_raw_usb_format_is_encoded_without_resizing(self):
        config = render({"video0": camera(input_format="yuyv422")})
        self.assertIn("input_format=yuyv422&video_size=1920x1080&framerate=30#video=h264", config)
        self.assertEqual("ffmpeg:video0__source#video=copy",
                         public_stream_source(camera(input_format="yuyv422")))

    def test_network_camera_uses_hidden_source(self):
        value = camera(kind="network", path="rtsp://camera/main", name="front", input_format="h264")
        config = render({"front": value})
        self.assertIn('front__source:\n      - "rtsp://camera/main"', config)
        self.assertIn("front: # CasaGuard profile: day", config)

    def test_public_pipeline_is_managed_by_go2rtc_without_external_post(self):
        config = render({"video0": camera()})
        self.assertIn("ffmpeg:video0__source#video=h264", config)
        self.assertNotIn("api/stream.ts", config)
        self.assertNotIn("__casaguard_wait", config)

    def test_offline_camera_is_not_rendered(self):
        self.assertIn("cameras: {}", render({"old": camera(status="offline")}))

    def test_zero_camera_startup_is_valid(self):
        config = render({})
        self.assertIn("streams: {}", config)
        self.assertIn("cameras: {}", config)


class LightEvaluationTests(unittest.TestCase):
    def tearDown(self):
        RUNTIME.clear()

    def test_six_dark_samples_enable_night(self):
        value, runtime = camera(last_transition=0), {}
        for _ in range(5):
            self.assertIsNone(evaluate_light(value, 20, runtime, now=1000))
        self.assertTrue(evaluate_light(value, 20, runtime, now=1000))

    def test_three_bright_samples_restore_day(self):
        value, runtime = camera(night_active=True, last_transition=0), {}
        self.assertIsNone(evaluate_light(value, 100, runtime, now=1000))
        self.assertIsNone(evaluate_light(value, 100, runtime, now=1000))
        self.assertFalse(evaluate_light(value, 100, runtime, now=1000))

    def test_five_minute_dwell_blocks_normal_transition(self):
        value, runtime = camera(night_active=True, last_transition=900), {}
        for _ in range(5):
            self.assertIsNone(evaluate_light(value, 100, runtime, now=1000))

    def test_extreme_light_bypasses_dwell_after_two_samples(self):
        value, runtime = camera(night_active=True, last_transition=999), {}
        self.assertIsNone(evaluate_light(value, 180, runtime, now=1000))
        self.assertFalse(evaluate_light(value, 180, runtime, now=1000))

    def test_manual_modes_switch_immediately(self):
        self.assertTrue(evaluate_light(camera(night_mode="night"), 200, {}, now=1))
        self.assertFalse(evaluate_light(camera(night_mode="day", night_active=True), 0, {}, now=1))

    def test_cameras_keep_independent_debounce_state(self):
        first, second = {}, {}
        value = camera()
        for _ in range(6):
            result = evaluate_light(value, 20, first, now=1000)
        self.assertTrue(result)
        self.assertIsNone(evaluate_light(value, 20, second, now=1000))

    @patch("manager.apply_stream_state")
    @patch("manager.sample_luminance", side_effect=RuntimeError("snapshot unavailable"))
    @patch("manager.load_state")
    def test_sampling_failure_freezes_current_mode(self, load, _, apply):
        load.return_value = {"video0": camera(night_active=True)}
        monitor_once()
        apply.assert_not_called()
        self.assertEqual("sample_error", RUNTIME["video0"]["status"])


class LuminanceTests(unittest.TestCase):
    @staticmethod
    def frame(color):
        return subprocess.check_output([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=32x18:d=0.1",
            "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ])

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for the synthetic frame test")
    def test_synthetic_dark_and_bright_frames(self):
        self.assertLess(jpeg_luminance(self.frame("black")), 30)
        self.assertGreater(jpeg_luminance(self.frame("white")), 220)

    @patch("manager.time.sleep")
    @patch("manager.jpeg_luminance", side_effect=[RuntimeError("truncated"), 73])
    @patch("manager.fetch_frame", return_value=b"jpeg")
    def test_transient_frame_decode_is_retried(self, _, luminance, sleep):
        self.assertEqual(73, sample_luminance(camera()))
        self.assertEqual(2, luminance.call_count)
        sleep.assert_called_once_with(0.25)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg tools are required")
    def test_night_filter_keeps_native_dimensions_and_fps(self):
        handle, path = tempfile.mkstemp(suffix=".mkv")
        os.close(handle)
        try:
            subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                "testsrc=size=1280x960:rate=30", "-t", "0.2", "-vf", NIGHT_FILTERS["aggressive"],
                "-c:v", "libx264", "-preset", "superfast", path,
            ], check=True)
            details = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=width,height,avg_frame_rate", "-of", "json", path,
            ]))["streams"][0]
            self.assertEqual((1280, 960, "30/1"),
                             (details["width"], details["height"], details["avg_frame_rate"]))
        finally:
            os.unlink(path)


class IdentityAndMigrationTests(unittest.TestCase):
    def test_serial_identity_survives_port_change(self):
        first = usb_identity({"vendor": "046d", "product": "0825", "serial": "ABC"}, "Camera (usb-a)")
        second = usb_identity({"vendor": "046d", "product": "0825", "serial": "ABC"}, "Camera (usb-b)")
        self.assertEqual(first, second)
        self.assertTrue(first[1])

    def test_vendor_product_identity_survives_port_change_without_serial(self):
        first = usb_identity({"vendor": "046d", "product": "0825"}, "Camera")
        second = usb_identity({"vendor": "046d", "product": "0825"}, "Camera")
        self.assertEqual(first, second)
        self.assertFalse(first[1])

    def test_unique_stable_camera_migrates_settings_and_removes_old_ports(self):
        existing = {
            "usb_old1": camera(id="usb_old1", name="room", label="Test Camera (usb-old1)", status="offline", night_mode="night"),
            "usb_old2": camera(id="usb_old2", name="room", label="Test Camera (usb-old2)", status="available"),
        }
        discovered = [camera(id="usb_stable", hardware_id="stable", stable_hardware_id=True, label="Test Camera (usb-new)")]
        merged = merge_usb_state(existing, discovered)
        self.assertEqual(["usb_stable"], list(merged))
        self.assertEqual("room", merged["usb_stable"]["name"])

    def test_duplicate_same_model_cameras_are_not_ambiguously_migrated(self):
        existing = {"old": camera(id="old", name="saved")}
        discovered = [
            camera(id="one", hardware_id="one", stable_hardware_id=True),
            camera(id="two", hardware_id="two", stable_hardware_id=True, path="/dev/video2"),
        ]
        merged = merge_usb_state(existing, discovered)
        self.assertIn("old", merged)
        self.assertEqual("video0", merged["one"]["name"])


class ApiAndRecoveryTests(unittest.TestCase):
    def test_credentials_are_redacted_from_api_state(self):
        state = public_state({"front": {"path": "rtsp://admin:secret@192.168.1.20/live"}})
        self.assertEqual("rtsp://***:***@192.168.1.20/live", state["front"]["path"])

    def tearDown(self):
        RUNTIME.clear()

    @patch("manager.save_state")
    @patch("manager.write_config", return_value=True)
    @patch("manager.restart_frigate", return_value=True)
    def test_profile_change_writes_config_and_restarts_frigate(self, restart, _, save):
        cameras = {"video0": camera()}
        self.assertTrue(apply_stream_state(cameras, "video0", True, "test"))
        restart.assert_called_once()
        self.assertTrue(cameras["video0"]["night_active"])
        self.assertEqual("", cameras["video0"]["night_error"])
        self.assertGreaterEqual(save.call_count, 2)

    @patch("manager.subprocess.run")
    def test_zero_exit_malformed_mjpeg_is_rejected(self, run):
        run.return_value = subprocess.CompletedProcess(
            [], 0, stderr="unable to decode APP fields: Invalid data found when processing input")
        valid, error = validate_usb_mode("/dev/video0", {
            "width": 1280, "height": 720, "fps": 30, "input_format": "mjpeg"})
        self.assertFalse(valid)
        self.assertIn("Invalid data found", error)


class DiscoveryTests(unittest.TestCase):
    def test_physical_camera_groups_select_nodes(self):
        listing = """UVC Camera (usb-a):
        /dev/video0
        /dev/video1
        /dev/media0

GENERAL WEBCAM (usb-b):
        /dev/video2
        /dev/video3
        /dev/media1
"""
        self.assertEqual([
            {"label": "UVC Camera (usb-a)", "nodes": ["/dev/video0", "/dev/video1"]},
            {"label": "GENERAL WEBCAM (usb-b)", "nodes": ["/dev/video2", "/dev/video3"]},
        ], parse_v4l2_groups(listing))

    @patch("manager.run", return_value="Format Video Capture:\n Width/Height : 1920/1080")
    def test_capture_node_uses_video_format_ioctl(self, mocked_run):
        self.assertTrue(is_capture_node("/dev/video0"))
        mocked_run.assert_called_once_with(["v4l2-ctl", "--device", "/dev/video0", "--get-fmt-video"], 5)

    @patch("manager.run", side_effect=RuntimeError("not a capture node"))
    def test_metadata_node_is_rejected(self, _):
        self.assertFalse(is_capture_node("/dev/video1"))


if __name__ == "__main__":
    unittest.main()
