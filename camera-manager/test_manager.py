import unittest
from unittest.mock import patch

from manager import best_mode, is_capture_node, parse_v4l2_groups, public_state, render


class BestModeTests(unittest.TestCase):
    def test_fps_is_selected_from_native_resolution(self):
        modes = """
        Size: Discrete 640x480
          Interval: Discrete 0.033s (30.000 fps)
        Size: Discrete 1920x1080
          Interval: Discrete 0.200s (5.000 fps)
        """
        self.assertEqual((1920, 1080, 5.0, 'mjpeg'), best_mode(modes))

    def test_highest_fps_wins_for_same_resolution(self):
        modes = """
        Size: Discrete 1280x720
          Interval: Discrete 0.066s (15.000 fps)
          Interval: Discrete 0.033s (30.000 fps)
        """
        self.assertEqual((1280, 720, 30.0, 'mjpeg'), best_mode(modes))

    def test_usb_live_stream_is_h264_at_native_mode(self):
        config = render({'video0': {
            'id':'video0','kind':'usb','path':'/dev/video0','name':'video0',
            'enabled':True,'width':1920,'height':1080,'fps':30,
        }})
        self.assertIn('input_format=mjpeg&video_size=1920x1080&framerate=30#video=h264', config)
        self.assertIn('fps: 5', config)
        self.assertIn('Native: video0', config)

    def test_native_h264_is_copied(self):
        config = render({'video0': {
            'id':'video0','kind':'usb','path':'/dev/video0','name':'video0',
            'enabled':True,'width':1920,'height':1080,'fps':30,'input_format':'h264',
        }})
        self.assertIn('input_format=h264', config)
        self.assertIn('#video=copy', config)

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
            {'label':'UVC Camera (usb-a)','nodes':['/dev/video0','/dev/video1']},
            {'label':'GENERAL WEBCAM (usb-b)','nodes':['/dev/video2','/dev/video3']},
        ], parse_v4l2_groups(listing))

    def test_compressed_format_wins_at_same_native_size(self):
        modes = """
        [0]: 'YUYV'
          Size: Discrete 1920x1080
            Interval: Discrete 0.033s (30.000 fps)
        [1]: 'MJPG'
          Size: Discrete 1920x1080
            Interval: Discrete 0.033s (30.000 fps)
        """
        self.assertEqual((1920,1080,30.0,'mjpeg'), best_mode(modes))

    def test_network_h264_uses_native_restream(self):
        config=render({'front':{'id':'front','kind':'network','path':'rtsp://camera/main','name':'front','enabled':True,'width':2560,'height':1440,'fps':25,'input_format':'h264'}})
        self.assertIn('ffmpeg:rtsp://camera/main#video=copy',config)
        self.assertIn('width: 2560',config)
        self.assertIn('height: 1440',config)

    def test_offline_camera_is_not_rendered(self):
        config=render({'old':{'id':'old','kind':'usb','path':'/dev/video9','name':'old','enabled':True,'status':'offline','width':640,'height':480,'fps':30,'input_format':'mjpeg'}})
        self.assertIn('cameras: {}',config)

    def test_credentials_are_redacted_from_api_state(self):
        state=public_state({'front':{'path':'rtsp://admin:secret@192.168.1.20/live'}})
        self.assertEqual('rtsp://***:***@192.168.1.20/live',state['front']['path'])

    @patch('manager.run', return_value="Format Video Capture:\n Width/Height : 1920/1080")
    def test_capture_node_uses_video_format_ioctl(self, mocked_run):
        self.assertTrue(is_capture_node('/dev/video0'))
        mocked_run.assert_called_once_with(['v4l2-ctl','--device','/dev/video0','--get-fmt-video'],5)

    @patch('manager.run', side_effect=RuntimeError('not a capture node'))
    def test_metadata_node_is_rejected(self, _):
        self.assertFalse(is_capture_node('/dev/video1'))


if __name__ == '__main__':
    unittest.main()
