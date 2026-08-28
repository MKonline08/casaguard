import json
import os
import subprocess
import shutil
import tempfile
import unittest
from unittest.mock import patch

from manager import (
    NIGHT_FILTERS,
    PUBLIC_WORKERS,
    RUNTIME,
    apply_stream_state,
    best_mode,
    encoder_args,
    evaluate_light,
    is_capture_node,
    jpeg_luminance,
    merge_usb_state,
    monitor_once,
    parse_v4l2_groups,
    public_state,
    public_worker_command,
    replace_public_worker,
    render,
    usb_identity,
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
        self.assertIn("video0: [] # CasaGuard profile: day", config)
        self.assertIn("fps: 5", config)
        self.assertIn("enabled: true", config)
        self.assertIn("Native: video0", config)

    def test_night_filter_is_aggressive_without_scaling_or_fps_change(self):
        config = render({"video0": camera(night_active=True)})
        self.assertIn("video0: [] # CasaGuard profile: night", config)
        self.assertIn("video_size=1920x1080&framerate=30", config)
        command = " ".join(public_worker_command(camera(night_active=True)))
        self.assertIn(NIGHT_FILTERS["aggressive"], command)
        self.assertNotIn("scale=", command)
        self.assertNotIn("-r ", command)

    def test_native_h264_day_mode_is_copied(self):
        config = render({"video0": camera(input_format="h264")})
        self.assertIn("input_format=h264", config)
        self.assertEqual(["-c:v", "copy"], encoder_args(camera(input_format="h264")))

    def test_raw_usb_format_is_encoded_without_resizing(self):
        config = render({"video0": camera(input_format="yuyv422")})
        self.assertIn("input_format=yuyv422&video_size=1920x1080&framerate=30#video=h264", config)

    def test_network_camera_uses_hidden_source(self):
        value = camera(kind="network", path="rtsp://camera/main", name="front", input_format="h264")
        config = render({"front": value})
        self.assertIn('front__source:\n      - "rtsp://camera/main"', config)
        self.assertIn("front: [] # CasaGuard profile: day", config)

    def test_public_pipeline_posts_to_loopback_go2rtc(self):
        command = public_worker_command(camera())
        self.assertIn("rtsp://127.0.0.1:8554/video0__source", command)
        self.assertIn("http://127.0.0.1:1984/api/stream.ts?dst=video0", command)
        self.assertEqual("POST", command[command.index("-method") + 1])

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

    class FakeProcess:
        pid = 123
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    def tearDown(self):
        PUBLIC_WORKERS.clear()

    @patch("manager.fetch_frame", return_value=b"jpeg")
    @patch("manager.subprocess.Popen")
    def test_live_switch_replaces_only_public_pipeline_and_verifies_frame(self, popen, frame):
        old = self.FakeProcess()
        PUBLIC_WORKERS["video0"] = {"process": old, "signature": ("old",)}
        new = self.FakeProcess()
        popen.return_value = new
        replace_public_worker("video0", camera(night_active=True))
        self.assertEqual(0, old.returncode)
        command = popen.call_args.args[0]
        self.assertIn(NIGHT_FILTERS["aggressive"], command)
        frame.assert_called_once_with("video0", timeout=4)

    @patch("manager.save_state")
    @patch("manager.write_config")
    @patch("manager.restart_frigate", return_value=True)
    @patch("manager.replace_public_worker", side_effect=RuntimeError("offline"))
    def test_failed_live_switch_falls_back_to_frigate_restart(self, _, restart, __, ___):
        cameras = {"video0": camera()}
        self.assertTrue(apply_stream_state(cameras, "video0", True, "test"))
        restart.assert_called_once()
        self.assertIn("live switch failed", cameras["video0"]["night_error"])


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
