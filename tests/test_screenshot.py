import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from msx_screenshot import render_screen2, render_screen5, render_screen8, write_png


class ScreenshotRendererTest(unittest.TestCase):
    def test_screen2_known_foreground_pixel(self):
        vram = bytearray(0x3800)
        vram[0x1800] = 1
        vram[8] = 0x80
        vram[0x2008] = 0xF4
        rgb, width, height = render_screen2(vram)
        self.assertEqual((width, height), (256, 192))
        self.assertEqual(tuple(rgb[0:3]), (255, 255, 255))

    def test_screen5_nibbles(self):
        palette = [(i, i + 1, i + 2) for i in range(16)]
        vram = bytearray(0x6A00)
        vram[0] = 0x12
        rgb, width, _ = render_screen5(vram, palette)
        self.assertEqual(width, 256)
        self.assertEqual(tuple(rgb[0:3]), palette[1])
        self.assertEqual(tuple(rgb[3:6]), palette[2])

    def test_screen8_grb332(self):
        vram = bytearray(0xD400)
        vram[0] = 0b11111111
        rgb, _, _ = render_screen8(vram)
        self.assertEqual(tuple(rgb[0:3]), (255, 255, 255))

    def test_png_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "screenshot.png"
            write_png(output, 1, 1, bytes([1, 2, 3]))
            self.assertEqual(output.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
