import unittest

from manager import best_mode


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


if __name__ == '__main__':
    unittest.main()
