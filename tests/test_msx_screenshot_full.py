import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from msx_screenshot import (  # noqa: E402
    SCREEN8_SPRITE_PALETTE,
    decode_palette,
    render_screen0,
    render_screen1,
    render_screen3,
    render_screen4,
    render_screen6,
    render_screen7,
    render_screen10,
    render_screen11,
    render_screen12,
    render_vram,
)


def palette():
    return [(index, index + 16, index + 32) for index in range(16)]


class FullScreenshotRendererTest(unittest.TestCase):
    def test_v9938_palette_byte_order_and_scaling(self):
        raw = bytearray(32)
        raw[2:4] = bytes((0x73, 0x05))
        decoded = decode_palette(raw)
        self.assertEqual(decoded[1], (255, 5 * 255 // 7, 3 * 255 // 7))
        with self.assertRaises(ValueError):
            decode_palette(b"too short")

    def test_screen0_uses_six_active_pixels_per_character(self):
        vram = bytearray(0x1000)
        vram[0] = 1
        vram[0x800 + 8] = 0x80
        colours = palette()
        rgb, width, height = render_screen0(vram, palette=colours, height=8,
                                            fg=15, bg=4)
        self.assertEqual((width, height), (240, 8))
        self.assertEqual(tuple(rgb[:3]), colours[15])
        self.assertEqual(tuple(rgb[3:6]), colours[4])

    def test_screen0_width80_and_blink_colour_table(self):
        vram = bytearray(0x3000)
        vram[0] = 1
        vram[0x1000 + 8] = 0x80
        vram[0x2000] = 0x80
        colours = palette()
        # Direct wrapper defaults R#12 to colour 0 for both blink colours.
        rgb, width, _ = render_screen0(vram, width=80, palette=colours,
                                       height=8, ct=0x2000, blink=True)
        self.assertEqual(width, 480)
        self.assertEqual(tuple(rgb[:3]), colours[0])

    def test_screen1_uses_one_colour_byte_per_eight_patterns(self):
        vram = bytearray(0x2100)
        vram[0x1800] = 8
        vram[8 * 8] = 0x80
        vram[0x2001] = 0xF4
        colours = palette()
        rgb, width, _ = render_screen1(vram, palette=colours, height=1)
        self.assertEqual(width, 256)
        self.assertEqual(tuple(rgb[:3]), colours[15])
        self.assertEqual(tuple(rgb[3:6]), colours[4])

    def test_screen3_decodes_two_four_by_four_blocks(self):
        vram = bytearray(0x1000)
        vram[0x800] = 1
        vram[8] = 0x24
        colours = palette()
        rgb, width, _ = render_screen3(vram, palette=colours, height=4)
        self.assertEqual(width, 256)
        self.assertEqual(tuple(rgb[0:3]), colours[2])
        self.assertEqual(tuple(rgb[3 * 3:4 * 3]), colours[2])
        self.assertEqual(tuple(rgb[4 * 3:5 * 3]), colours[4])

    def test_screen4_background_matches_graphic2_format(self):
        vram = bytearray(0x4000)
        vram[0x1800] = 1
        vram[8] = 0x80
        vram[0x2008] = 0xE3
        colours = palette()
        rgb, _, _ = render_screen4(vram, palette=colours, height=1)
        self.assertEqual(tuple(rgb[:3]), colours[14])
        self.assertEqual(tuple(rgb[3:6]), colours[3])

    def test_graphic2_register_table_masks_select_standard_bases(self):
        vram = bytearray(0x4000)
        vram[0x1800] = 1
        vram[8] = 0x80
        vram[0x2008] = 0xD3
        regs = [0] * 28
        regs[2] = 6
        regs[3] = 0xFF
        regs[4] = 3
        regs[8] = 0x20
        colours = palette()
        rgb, _, _ = render_vram(vram, 2, regs, colours, height=1,
                                sprites=False)
        self.assertEqual(tuple(rgb[:3]), colours[13])
        self.assertEqual(tuple(rgb[3:6]), colours[3])

    def test_screen6_decodes_four_two_bit_pixels(self):
        vram = bytearray(0x8000)
        vram[0] = 0b00011011
        colours = palette()
        rgb, width, _ = render_screen6(vram, colours, height=1)
        self.assertEqual(width, 512)
        self.assertEqual([tuple(rgb[i * 3:(i + 1) * 3]) for i in range(4)],
                         colours[:4])

    def test_screen7_decodes_four_bit_pixels_from_logical_vram(self):
        vram = bytearray(0x10000)
        vram[0] = 0xAB
        colours = palette()
        rgb, width, _ = render_screen7(vram, colours, height=1)
        self.assertEqual(width, 512)
        self.assertEqual(tuple(rgb[:3]), colours[10])
        self.assertEqual(tuple(rgb[3:6]), colours[11])

    def test_register_selected_and_explicit_bitmap_pages(self):
        vram = bytearray(0x20000)
        vram[0] = 0x12
        vram[0x10000] = 0x34
        regs = [0] * 28
        regs[2] = 0x40  # SCREEN 5 page 2: (R#2 & 0x60) << 10
        regs[8] = 0x20
        colours = palette()
        rgb, _, _ = render_vram(vram, 5, regs, colours, height=1,
                                sprites=False)
        self.assertEqual(tuple(rgb[:3]), colours[3])
        rgb, _, _ = render_vram(vram, 5, regs, colours, height=1, page=0,
                                sprites=False)
        self.assertEqual(tuple(rgb[:3]), colours[1])

    def test_screen8_register_selects_second_64k_page(self):
        vram = bytearray(0x20000)
        vram[0] = 0
        vram[0x10000] = 0xFF
        regs = [0] * 28
        regs[2] = 0x20
        regs[8] = 0x20
        rgb, _, _ = render_vram(vram, 8, regs, palette(), height=1,
                                sprites=False)
        self.assertEqual(tuple(rgb[:3]), (255, 255, 255))

    def test_screen12_decodes_signed_yjk_vector(self):
        vram = bytearray(0x10000)
        # Y=10, J=+5, K=-3.  J/K are shared by this group of four pixels.
        vram[:4] = bytes((0x55, 0x57, 0x55, 0x50))
        rgb, width, height = render_screen12(vram, height=1)
        expected = (15 * 255 // 31, 7 * 255 // 31, 11 * 255 // 31)
        self.assertEqual((width, height), (256, 1))
        for x in range(4):
            self.assertEqual(tuple(rgb[x * 3:(x + 1) * 3]), expected)

    def test_screen12_white_reference_group(self):
        vram = bytearray(0x10000)
        # Low bits zero encode J=K=0; Y=31 gives full white.
        vram[:4] = b"\xF8" * 4
        rgb, _, _ = render_screen12(vram, height=1)
        self.assertEqual(tuple(rgb[:12]), (255, 255, 255) * 4)

    def test_screen10_and_11_share_yae_format(self):
        vram = bytearray(0x10000)
        # Pixel 0 has A=1 and palette index 10.  The remaining pixels have
        # A=0 and decode as Y=10, J=K=0.
        vram[:4] = bytes((0xA8, 0x50, 0x50, 0x50))
        colours = palette()
        rgb10, _, _ = render_screen10(vram, colours, height=1)
        rgb11, _, _ = render_screen11(vram, colours, height=1)
        self.assertEqual(rgb10, rgb11)
        self.assertEqual(tuple(rgb10[:3]), colours[10])
        y10 = 10 * 255 // 31
        blue10 = 13 * 255 // 31  # (5*Y + 2) / 4, V9958 rounding
        self.assertEqual(tuple(rgb10[3:6]), (y10, y10, blue10))

    def test_yae_palette_bit_does_not_disable_shared_jk_bits(self):
        vram = bytearray(0x10000)
        # Pixel 0 is palette colour 1, but its low bits still encode K=5
        # for the YJK pixels that follow it.
        vram[:4] = bytes((0x1D, 0x50, 0x50, 0x50))
        colours = palette()
        rgb, _, _ = render_screen11(vram, colours, height=1)
        self.assertEqual(tuple(rgb[:3]), colours[1])
        self.assertEqual(tuple(rgb[3:6]),
                         (10 * 255 // 31, 15 * 255 // 31, 11 * 255 // 31))

    def test_yjk_second_page_selection(self):
        vram = bytearray(0x20000)
        vram[:4] = b"\x00" * 4
        vram[0x10000:0x10004] = b"\xF8" * 4
        regs = [0] * 28
        regs[2] = 0x20
        regs[8] = 0x20
        rgb, _, _ = render_vram(vram, 12, regs, palette(), height=1,
                                sprites=False)
        self.assertEqual(tuple(rgb[:3]), (255, 255, 255))

    def test_sprite_mode1_priority_pixel(self):
        vram = bytearray(0x4000)
        # Empty SCREEN 1 background, direct sprite tables.
        vram[0x3000:0x3008] = bytes((255, 0, 1, 2, 208, 0, 0, 0))
        vram[0x3800 + 8] = 0x80
        regs = [0] * 28
        regs[8] = 0x20
        colours = palette()
        rgb, _, _ = render_vram(
            vram, 1, regs, colours, height=1, sprites=True,
            bases={"name": 0x1800, "pattern": 0, "color": 0x2000,
                   "sprite_attribute": 0x3000,
                   "sprite_pattern": 0x3800})
        self.assertEqual(tuple(rgb[:3]), colours[2])

    def test_sprite_mode2_uses_per_line_colour(self):
        vram = bytearray(0x8000)
        # SAT points at its records; the colour table is independently based.
        vram[0x7600:0x7608] = bytes((255, 0, 1, 0, 216, 0, 0, 0))
        vram[0x7400] = 5
        vram[0x7800 + 8] = 0x80
        regs = [0] * 28
        regs[8] = 0x20
        colours = palette()
        rgb, _, _ = render_vram(
            vram, 5, regs, colours, height=1, sprites=True,
            bases={"bitmap": 0, "sprite_attribute": 0x7600,
                   "sprite_color": 0x7400, "sprite_pattern": 0x7800})
        self.assertEqual(tuple(rgb[:3]), colours[5])

    def test_vertical_scroll_moves_sprite_with_background(self):
        vram = bytearray(0x8000)
        # With R#23=1, a sprite whose nominal top is line 1 appears at line 0.
        vram[0x7600:0x7608] = bytes((0, 0, 1, 0, 216, 0, 0, 0))
        vram[0x7400] = 6
        vram[0x7800 + 8] = 0x80
        regs = [0] * 28
        regs[8] = 0x20
        regs[23] = 1
        colours = palette()
        rgb, _, _ = render_vram(
            vram, 5, regs, colours, height=1, sprites=True,
            bases={"bitmap": 0, "sprite_attribute": 0x7600,
                   "sprite_color": 0x7400, "sprite_pattern": 0x7800})
        self.assertEqual(tuple(rgb[:3]), colours[6])

    def test_screen8_sprites_use_their_fixed_palette(self):
        vram = bytearray(0x20000)
        vram[0x7600:0x7608] = bytes((255, 0, 1, 0, 216, 0, 0, 0))
        vram[0x7400] = 15
        vram[0x7800 + 8] = 0x80
        regs = [0] * 28
        regs[8] = 0x20
        rgb, _, _ = render_vram(
            vram, 8, regs, [(1, 2, 3)] * 16, height=1, sprites=True,
            bases={"bitmap": 0, "sprite_attribute": 0x7600,
                   "sprite_color": 0x7400, "sprite_pattern": 0x7800})
        self.assertEqual(tuple(rgb[:3]), SCREEN8_SPRITE_PALETTE[15])

    def test_yjk_sprites_use_programmable_palette(self):
        vram = bytearray(0x20000)
        vram[0x7600:0x7608] = bytes((255, 0, 1, 0, 216, 0, 0, 0))
        vram[0x7400] = 6
        vram[0x7800 + 8] = 0x80
        regs = [0] * 28
        regs[8] = 0x20
        colours = palette()
        rgb, _, _ = render_vram(
            vram, 12, regs, colours, height=1, sprites=True,
            bases={"bitmap": 0, "sprite_attribute": 0x7600,
                   "sprite_color": 0x7400, "sprite_pattern": 0x7800})
        self.assertEqual(tuple(rgb[:3]), colours[6])

    def test_invalid_mode_and_page_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Korean/Hangul"):
            render_vram(bytearray(1), 9)
        with self.assertRaises(ValueError):
            render_vram(bytearray(1), 13)
        with self.assertRaises(ValueError):
            render_vram(bytearray(0x20000), 8, [0] * 28, page=2,
                        sprites=False, height=1)


if __name__ == "__main__":
    unittest.main()
