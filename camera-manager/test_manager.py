import unittest

from manager import best_mode, render


class BestModeTests(unittest.TestCase):
    def test_fps_is_selected_from_native_resolution(self):
        modes = """
        Size: Discrete 640x480
          Interval: Discrete 0.033s (30.000 fps)
        Size: Discrete 1920x1080
          Interval: Discrete 0.200s (5.000 fps)
        """
        self.assertEqual((1920, 1080, 5.0), best_mode(modes))

    def test_highest_fps_wins_for_same_resolution(self):
        modes = """
        Size: Discrete 1280x720
          Interval: Discrete 0.066s (15.000 fps)
          Interval: Discrete 0.033s (30.000 fps)
        """
        self.assertEqual((1280, 720, 30.0), best_mode(modes))

    def test_usb_live_stream_is_h264_at_native_mode(self):
        config = render({'video0': {
            'id':'video0','kind':'usb','path':'/dev/video0','name':'video0',
            'enabled':True,'width':1920,'height':1080,'fps':30,
        }})
        self.assertIn('video_size=1920x1080&framerate=30#video=h264', config)
        self.assertIn('fps: 5', config)
        self.assertIn('video0: video0', config)


if __name__ == '__main__':
    unittest.main()
