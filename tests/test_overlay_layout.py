import unittest

try:
    from src.overlay import (
        clamp_position, corner_position, ease_in_out_cubic, ease_out_cubic, elide, mix_color, pulse_level,
    )
except ImportError:  # tkinter missing on this interpreter
    corner_position = None


@unittest.skipIf(corner_position is None, "tkinter not available")
class VisualHelpersTest(unittest.TestCase):
    def test_easing_endpoints(self):
        for ease in (ease_out_cubic, ease_in_out_cubic):
            self.assertAlmostEqual(ease(0), 0)
            self.assertAlmostEqual(ease(1), 1)
            self.assertAlmostEqual(ease(5), 1)  # Clamped past the end.
        self.assertGreater(ease_out_cubic(0.5), 0.5)  # Fast start, gentle finish.
        self.assertAlmostEqual(ease_in_out_cubic(0.5), 0.5)

    def test_mix_color(self):
        self.assertEqual(mix_color("#000000", "#ffffff", 0), "#000000")
        self.assertEqual(mix_color("#000000", "#ffffff", 1), "#ffffff")
        self.assertEqual(mix_color("#000000", "#ff8000", 0.5), "#804000")

    def test_pulse_level_range(self):
        self.assertAlmostEqual(pulse_level(0.0, 2.0), 1.0)
        self.assertAlmostEqual(pulse_level(1.0, 2.0), 0.0)

    def test_elide_by_measured_width(self):
        measure = len  # One "pixel" per character keeps it deterministic.
        self.assertEqual(elide("short", 10, measure), "short")
        self.assertEqual(elide("a long session title", 10, measure), "a long se…")
        self.assertLessEqual(len(elide("a long session title", 10, measure)), 10)
        self.assertEqual(elide("abc", 1, measure), "…")


@unittest.skipIf(corner_position is None, "tkinter not available")
class LayoutTest(unittest.TestCase):
    AREA = (0, 0, 1920, 1040)  # work area above a 40px taskbar

    def test_corners(self):
        size = (300, 120)
        self.assertEqual(corner_position("top-right", size, self.AREA, 16), (1604, 16))
        self.assertEqual(corner_position("top-left", size, self.AREA, 16), (16, 16))
        self.assertEqual(corner_position("bottom-right", size, self.AREA, 16), (1604, 904))
        self.assertEqual(corner_position("bottom-left", size, self.AREA, 16), (16, 904))
        # Top centre ignores the margin: centred and flush with the top of the work area.
        self.assertEqual(corner_position("top-center", size, self.AREA, 16), (810, 0))
        self.assertEqual(corner_position("top-center", (300, 30), (-1920, 0, 0, 1040), 16), (-1110, 0))

    def test_clamp_to_virtual_screen(self):
        bounds = (-1920, 0, 1920, 1080)  # second monitor on the left
        self.assertEqual(clamp_position(-1800, 50, (300, 100), bounds), (-1800, 50))
        self.assertEqual(clamp_position(5000, 5000, (300, 100), bounds), (1620, 980))
        self.assertEqual(clamp_position(-9000, -10, (300, 100), bounds), (-1920, 0))


if __name__ == "__main__":
    unittest.main()
