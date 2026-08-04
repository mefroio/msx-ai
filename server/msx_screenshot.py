#!/usr/bin/env python3
"""Host-side renderer for the standard MSX SCREEN modes 0--8 and 10--12.

The renderer consumes the CPU-visible ("logical") VRAM plus the VDP register
and palette state.  This is the same representation returned by openMSX's
``VRAM`` debuggable and by the real agent's VRAM read command, including the
planar addressing used by SCREEN 7/8/10/11/12.

It reconstructs the active image, display pages, table base masks, vertical
scroll and sprites (mode 1 and mode 2).  A single snapshot cannot reproduce
mid-frame register/palette changes (raster effects), borders/overscan, analog
video artefacts or interlaced field timing.  The V9938 palette is write-only;
on real hardware an exact custom palette therefore requires a palette mirror
from the resident agent or an explicit ``palette=`` argument.  SCREEN 9 is a
vendor-specific Korean/Hangul mode rather than a baseline MSX V99x8 mode and
is rejected explicitly.
"""

from dataclasses import dataclass
import struct
import zlib


VRAM_SIZE = 0x20000
SUPPORTED_MODES = frozenset((*range(9), 10, 11, 12))


# ---- minimal PNG (8-bit RGB) -----------------------------------------------
def write_png(path, w, h, rgb):
    """Write an RGB byte buffer as a dependency-free PNG."""
    if len(rgb) != w * h * 3:
        raise ValueError(f"RGB buffer has {len(rgb)} bytes, expected {w * h * 3}")

    def chunk(kind, data):
        body = kind + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    raw = bytearray()
    for y in range(h):
        raw.append(0)  # PNG filter: none
        raw += rgb[y * w * 3:(y + 1) * w * 3]
    with open(path, "wb") as output:
        output.write(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h,
                                                   8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
                     + chunk(b"IEND", b""))


# TMS9918 fixed palette (SCREEN 0/1/2/3).  Entry 0 is transparent in the
# normal VDP configuration; its visible colour is then selected by R#7.
TMS = [(0, 0, 0), (0, 0, 0), (33, 200, 66), (94, 220, 120),
       (84, 85, 237), (125, 118, 252), (212, 82, 77), (66, 235, 245),
       (252, 85, 84), (255, 121, 120), (212, 193, 84), (230, 206, 128),
       (33, 176, 59), (201, 91, 186), (204, 204, 204), (255, 255, 255)]

# Safe fallback for a real MSX when no palette mirror is available.  Existing
# callers imported this name, so keep it stable.
V9938_DEFAULT = TMS

# SCREEN 8 has a fixed sprite palette (V9938 data book, page 98), independent
# of the programmable 16-colour palette used by the other MSX2 modes.
_SCREEN8_SPRITE_GRB = (
    0x000, 0x002, 0x030, 0x032, 0x300, 0x302, 0x330, 0x332,
    0x472, 0x007, 0x070, 0x077, 0x700, 0x707, 0x770, 0x777,
)


def _scale(value, maximum):
    return value * 255 // maximum


def _grb_word_to_rgb(value):
    return (_scale((value >> 4) & 7, 7),
            _scale((value >> 8) & 7, 7),
            _scale(value & 7, 7))


SCREEN8_SPRITE_PALETTE = tuple(map(_grb_word_to_rgb, _SCREEN8_SPRITE_GRB))


def _normalise_palette(palette):
    if palette is None:
        palette = V9938_DEFAULT
    if len(palette) != 16:
        raise ValueError("an MSX indexed palette must contain 16 RGB entries")
    result = []
    for entry in palette:
        if len(entry) != 3 or any(not 0 <= component <= 255 for component in entry):
            raise ValueError(f"invalid RGB palette entry: {entry!r}")
        result.append(tuple(entry))
    return result


def decode_palette(raw32):
    """Decode 32 V9938 palette bytes (16 x GRB 3:3:3) to 8-bit RGB."""
    if len(raw32) != 32:
        raise ValueError("a V9938 palette dump must contain exactly 32 bytes")
    palette = []
    for index in range(16):
        rb, g = raw32[index * 2:index * 2 + 2]
        palette.append((_scale((rb >> 4) & 7, 7),
                        _scale(g & 7, 7),
                        _scale(rb & 7, 7)))
    return palette


def _rgb332(value):
    return (_scale((value >> 2) & 7, 7),
            _scale((value >> 5) & 7, 7),
            _scale(value & 3, 3))


def _clamp5(value):
    return max(0, min(31, value))


def _trunc_div(value, divisor):
    """C/Z80-style signed division (towards zero), without float rounding."""
    return value // divisor if value >= 0 else -((-value) // divisor)


def _yjk_rgb(y, j, k):
    """Convert one V9958 YJK triplet to 8-bit RGB.

    The blue-channel ``+2`` rounding term matches measured V9958 behaviour
    and openMSX, rather than the older formula printed in some data sheets.
    """
    red = _clamp5(y + j)
    green = _clamp5(y + k)
    blue = _clamp5(_trunc_div(5 * y - 2 * j - k + 2, 4))
    return (_scale(red, 31), _scale(green, 31), _scale(blue, 31))


def _decode_yjk_group(values, palette, yae):
    """Decode four adjacent SCREEN 10/11/12 bytes.

    K is stored across pixels 0/1 and J across pixels 2/3.  In YAE modes,
    bit 3 selects a programmable-palette colour for that individual pixel;
    its low J/K bits still participate in the other pixels of the group.
    """
    p0, p1, p2, p3 = values
    j = (p2 & 7) + ((p3 & 3) << 3) - ((p3 & 4) << 3)
    k = (p0 & 7) + ((p1 & 3) << 3) - ((p1 & 4) << 3)
    result = []
    for value in values:
        if yae and value & 0x08:
            result.append(palette[value >> 4])
        else:
            result.append(_yjk_rgb(value >> 3, j, k))
    return result


def _validate_mode(mode):
    if mode == 9:
        raise ValueError(
            "SCREEN 9 is a vendor-specific Korean/Hangul mode and is not "
            "part of the standard MSX V9938/V9958 renderer")
    if mode not in SUPPORTED_MODES:
        raise ValueError("host-side renderer supports SCREEN 0--8 and 10--12")


def _vbyte(vram, address):
    if not 0 <= address < len(vram):
        raise ValueError(f"VRAM address 0x{address:05X} is absent from the dump")
    return vram[address]


def _pixel(output, width, x, y, colour):
    offset = (y * width + x) * 3
    output[offset:offset + 3] = bytes(colour)


def _registers(regs):
    if regs is None:
        result = [0] * 28
        result[1] = 0x40                 # display enabled
        result[7] = 0xF4                 # white on blue / blue backdrop
        result[8] = 0x20                 # colour 0 opaque for raw render APIs
        result[9] = 0x80                 # 212 lines for bitmap wrappers
        return result
    if len(regs) < 24:
        raise ValueError("VDP register state must include at least R#0 through R#23")
    result = list(regs[:28])
    result.extend([0] * (28 - len(result)))
    return result


def _table_addr(base, low_bits, index, index_bits):
    """Apply the V99x8 table mask rather than assuming base + index.

    Some demos deliberately clear register bits that overlap the table index
    to mirror table data.  The AND-mask model is required to capture those
    layouts correctly.
    """
    low_mask = (1 << low_bits) - 1
    index_mask = (1 << index_bits) - 1
    base_mask = (base | low_mask) & (VRAM_SIZE - 1)
    extended_index = index | ((VRAM_SIZE - 1) & ~index_mask)
    return base_mask & extended_index


def _table_reader(vram, regs, bases):
    bases = bases or {}

    def name(index, bits=10, linear_index=None):
        if "name" in bases:
            return _vbyte(vram, bases["name"] +
                          (index if linear_index is None else linear_index))
        return _vbyte(vram, _table_addr(regs[2] << 10, 10, index, bits))

    def pattern(index, bits=11):
        if "pattern" in bases:
            return _vbyte(vram, bases["pattern"] + index)
        return _vbyte(vram, _table_addr(regs[4] << 11, 11, index, bits))

    def colour(index, bits=6):
        if "color" in bases:
            return _vbyte(vram, bases["color"] + index)
        base = (regs[10] << 14) | (regs[3] << 6)
        return _vbyte(vram, _table_addr(base, 6, index, bits))

    return name, pattern, colour


def _foreground_palette(palette, regs, mode):
    foreground = list(palette)
    transparent = not (regs[8] & 0x20)
    if transparent and mode != 8:
        foreground[0] = palette[regs[7] & 15]
    return foreground, transparent


def _active_height(regs, _mode, requested):
    if requested is not None:
        if not 1 <= requested <= 256:
            raise ValueError("height must be between 1 and 256 scanlines")
        return requested
    return 212 if regs[9] & 0x80 else 192


def _render_characters(vram, mode, regs, palette, height, bases,
                       text_width=None, blink=False):
    name, pattern, colour = _table_reader(vram, regs, bases)
    foreground, _ = _foreground_palette(palette, regs, mode)
    scroll_y = regs[23]

    if mode == 0:
        width = text_width or (80 if regs[0] & 0x04 else 40)
        if width not in (40, 80):
            raise ValueError("SCREEN 0 supports 40 or 80 columns")
        w = width * 6
        output = bytearray(w * height * 3)
        plain_fg = foreground[regs[7] >> 4]
        plain_bg = foreground[regs[7] & 15]
        blink_fg = foreground[regs[12] >> 4]
        blink_bg = foreground[regs[12] & 15]
        for y in range(height):
            glyph_line = (y + scroll_y) & 7
            row = y // 8
            for column in range(width):
                if width == 40:
                    index = 0xC00 + row * 40 + column
                    char = name(index, 12, row * 40 + column)
                    use_blink = False
                else:
                    index = row * 80 + column
                    char = name(index, 12)
                    mask = colour(row * 10 + column // 8, 9)
                    use_blink = blink and bool(mask & (0x80 >> (column & 7)))
                bits = pattern(char * 8 + glyph_line, 11)
                fg = blink_fg if use_blink else plain_fg
                bg = blink_bg if use_blink else plain_bg
                for dot in range(6):
                    _pixel(output, w, column * 6 + dot, y,
                           fg if bits & (0x80 >> dot) else bg)
        return output, w, height

    w = 256
    output = bytearray(w * height * 3)
    scroll_x = regs[26] & 0x1F
    for y in range(height):
        line = (y + scroll_y) & 0xFF
        row = line // 8
        for column in range(32):
            source_column = (column + scroll_x) & 31
            char = name(row * 32 + source_column, 10)
            if mode == 1:
                bits = pattern(char * 8 + (line & 7), 11)
                colours = colour(char // 8, 6)
                fg = foreground[colours >> 4]
                bg = foreground[colours & 15]
                for dot in range(8):
                    _pixel(output, w, column * 8 + dot, y,
                           fg if bits & (0x80 >> dot) else bg)
            elif mode in (2, 4):
                table_index = ((line // 64) * 256 + char) * 8 + (line & 7)
                bits = pattern(table_index, 13)
                colours = colour(table_index, 13)
                fg = foreground[colours >> 4]
                bg = foreground[colours & 15]
                for dot in range(8):
                    _pixel(output, w, column * 8 + dot, y,
                           fg if bits & (0x80 >> dot) else bg)
            elif mode == 3:
                # A multicolour byte describes two 4x4 blocks.  The selected
                # byte cycles every 32 scanlines, as on the real TMS9918.
                colours = pattern(char * 8 + ((line // 4) & 7), 11)
                left, right = foreground[colours >> 4], foreground[colours & 15]
                for dot in range(8):
                    _pixel(output, w, column * 8 + dot, y,
                           left if dot < 4 else right)
            else:  # guarded by render_vram
                raise AssertionError(mode)
    return output, w, height


def _bitmap_page_base(mode, regs, page, bases):
    if bases and "bitmap" in bases:
        return bases["bitmap"]
    page_size = 0x8000 if mode in (5, 6) else 0x10000
    pages = VRAM_SIZE // page_size
    if page is not None:
        if not 0 <= page < pages:
            raise ValueError(f"SCREEN {mode} page must be in range 0..{pages - 1}")
        return page * page_size
    if mode in (5, 6):
        return (regs[2] & 0x60) << 10
    return (regs[2] & 0x20) << 11


def _render_bitmap(vram, mode, regs, palette, height, page, bases):
    base = _bitmap_page_base(mode, regs, page, bases)
    foreground, transparent = _foreground_palette(palette, regs, mode)
    width = 512 if mode in (6, 7) else 256
    stride = 128 if mode in (5, 6) else 256
    output = bytearray(width * height * 3)
    scroll_y = regs[23]
    scroll_x = (regs[26] & 0x1F) * (16 if width == 512 else 8)

    for y in range(height):
        source_line = (y + scroll_y) & 0xFF
        line = []
        address = base + source_line * stride
        if mode == 5:
            for byte_index in range(stride):
                value = _vbyte(vram, address + byte_index)
                line.extend((foreground[value >> 4], foreground[value & 15]))
        elif mode == 6:
            backdrop_even = palette[(regs[7] >> 2) & 3]
            backdrop_odd = palette[regs[7] & 3]
            for byte_index in range(stride):
                value = _vbyte(vram, address + byte_index)
                indices = ((value >> 6) & 3, (value >> 4) & 3,
                           (value >> 2) & 3, value & 3)
                for subpixel, index in enumerate(indices):
                    if index == 0 and transparent:
                        line.append(backdrop_even if subpixel % 2 == 0
                                    else backdrop_odd)
                    else:
                        line.append(palette[index])
        elif mode == 7:
            for byte_index in range(stride):
                value = _vbyte(vram, address + byte_index)
                line.extend((foreground[value >> 4], foreground[value & 15]))
        elif mode == 8:
            line.extend(_rgb332(_vbyte(vram, address + x)) for x in range(stride))
        else:  # SCREEN 10/11 (YJK+YAE) or SCREEN 12 (pure YJK)
            yae = mode in (10, 11)
            for byte_index in range(0, stride, 4):
                values = tuple(_vbyte(vram, address + byte_index + offset)
                               for offset in range(4))
                line.extend(_decode_yjk_group(values, foreground, yae))

        if scroll_x:
            line = line[scroll_x:] + line[:scroll_x]
        start = y * width * 3
        output[start:start + width * 3] = bytes(component
                                                for colour in line
                                                for component in colour)
    return output, width, height


def _sprite_table_access(vram, regs, bases, sprite_mode):
    bases = bases or {}
    sat_base = bases.get("sprite_attribute")
    colour_base = bases.get("sprite_color")
    pattern_base = bases.get("sprite_pattern")

    def attribute(index):
        if sat_base is not None:
            # Direct bases point at the four-byte attribute records, not at
            # the preceding mode-2 colour table.
            return _vbyte(vram, sat_base + index - (512 if sprite_mode == 2 else 0))
        base = (regs[11] << 15) | (regs[5] << 7)
        return _vbyte(vram, _table_addr(base, 7, index,
                                        10 if sprite_mode == 2 else 7))

    def line_colour(index):
        if colour_base is not None:
            return _vbyte(vram, colour_base + index)
        if sat_base is not None:
            return _vbyte(vram, sat_base - 512 + index)
        base = (regs[11] << 15) | (regs[5] << 7)
        return _vbyte(vram, _table_addr(base, 7, index, 10))

    def pattern(index):
        if pattern_base is not None:
            return _vbyte(vram, pattern_base + index)
        return _vbyte(vram, _table_addr(regs[6] << 11, 11, index, 11))

    return attribute, line_colour, pattern


def _sprite_bits(pattern, number, row, size, magnified):
    if magnified:
        row //= 2
    number &= 0xFC if size == 16 else 0xFF
    left = pattern(number * 8 + row)
    bits = [(left & (0x80 >> dot)) != 0 for dot in range(8)]
    if size == 16:
        right = pattern(number * 8 + row + 16)
        bits.extend((right & (0x80 >> dot)) != 0 for dot in range(8))
    if magnified:
        bits = [dot for value in bits for dot in (value, value)]
    return bits


def _sprite_colour(mode, colour, palette):
    return SCREEN8_SPRITE_PALETTE[colour] if mode == 8 else palette[colour]


def _composite_sprites(output, width, height, vram, mode, regs, palette, bases):
    if mode == 0 or regs[8] & 0x02:  # text modes / SPD bit
        return
    sprite_mode = 1 if mode in (1, 2, 3) else 2
    attribute, line_colour, pattern = _sprite_table_access(vram, regs, bases,
                                                            sprite_mode)
    size = 16 if regs[1] & 0x02 else 8
    magnified = bool(regs[1] & 0x01)
    visible_size = size * (2 if magnified else 1)
    transparent = not (regs[8] & 0x20)
    limit = 4 if sprite_mode == 1 else 8
    terminator = 208 if sprite_mode == 1 else 216
    x_scale = 2 if mode in (6, 7) else 1

    records = []
    for number in range(32):
        offset = number * 4 if sprite_mode == 1 else 512 + number * 4
        y = attribute(offset)
        if y == terminator:
            break
        records.append((number, y, attribute(offset + 1), attribute(offset + 2),
                        attribute(offset + 3)))

    for screen_y in range(height):
        visible = []
        for number, raw_y, raw_x, pattern_number, raw_colour in records:
            # R#23 scrolls the complete V9938 display vertically, sprites
            # included (unlike the V9958 horizontal background scroll).
            relative = ((screen_y + regs[23])
                        - ((raw_y + 1) & 0xFF)) & 0xFF
            if relative >= visible_size:
                continue
            row = relative // (2 if magnified else 1)
            colour_attr = (raw_colour if sprite_mode == 1
                           else line_colour(number * 16 + row))
            x = raw_x - (32 if colour_attr & 0x80 else 0)
            visible.append({"x": x,
                            "bits": _sprite_bits(pattern, pattern_number,
                                                  relative, size, magnified),
                            "colour": colour_attr & 15,
                            "cc": bool(colour_attr & 0x40)})
            if len(visible) == limit:
                break

        if sprite_mode == 1:
            # Reverse order makes lower sprite numbers win priority.
            for sprite in reversed(visible):
                colour = sprite["colour"]
                if colour == 0 and transparent:
                    continue
                rgb = _sprite_colour(mode, colour, palette)
                for sx, set_pixel in enumerate(sprite["bits"]):
                    if not set_pixel:
                        continue
                    x = sprite["x"] + sx
                    if 0 <= x < width:
                        _pixel(output, width, x, screen_y, rgb)
            continue

        # In sprite mode 2 a CC sprite augments the colour of the preceding
        # non-CC sprite.  It is not independently visible.
        bases_to_draw = [i for i, sprite in enumerate(visible) if not sprite["cc"]]
        for index in reversed(bases_to_draw):
            sprite = visible[index]
            for sx, set_pixel in enumerate(sprite["bits"]):
                if not set_pixel:
                    continue
                logical_x = sprite["x"] + sx
                colour = sprite["colour"]
                follower = index + 1
                while follower < len(visible) and visible[follower]["cc"]:
                    extra = visible[follower]
                    extra_x = logical_x - extra["x"]
                    if 0 <= extra_x < len(extra["bits"]) and extra["bits"][extra_x]:
                        colour |= extra["colour"]
                    follower += 1
                if colour == 0 and transparent:
                    continue
                host_x = logical_x * x_scale
                if mode == 6:
                    # SCREEN 6's four-bit sprite colour represents two
                    # adjacent two-bit background pixels.
                    colours = (palette[colour >> 2], palette[colour & 3])
                else:
                    rgb = _sprite_colour(mode, colour, palette)
                    colours = (rgb,) * x_scale
                for subpixel, rgb in enumerate(colours):
                    x = host_x + subpixel
                    if 0 <= x < width:
                        _pixel(output, width, x, screen_y, rgb)


def render_vram(vram, mode, regs=None, palette=None, *, height=None, page=None,
                sprites=True, text_width=None, blink=False, bases=None):
    """Render logical VRAM for SCREEN ``mode`` (0--8 or 10--12).

    ``regs`` contains VDP R#0..R#27.  ``page`` overrides the bitmap display
    page selected by R#2.  ``bases`` can override any of ``name``, ``pattern``,
    ``color``, ``bitmap``, ``sprite_attribute``, ``sprite_color`` and
    ``sprite_pattern`` with direct logical VRAM addresses; this is useful for
    raw assets that are not accompanied by register state.
    """
    _validate_mode(mode)
    regs = _registers(regs)
    palette = _normalise_palette(palette)
    height = _active_height(regs, mode, height)
    if mode <= 4:
        output, width, height = _render_characters(
            vram, mode, regs, palette, height, bases, text_width, blink)
    else:
        output, width, height = _render_bitmap(
            vram, mode, regs, palette, height, page, bases)
    if sprites and mode != 0:
        _composite_sprites(output, width, height, vram, mode, regs, palette, bases)
    return output, width, height


# ---- stable per-mode APIs --------------------------------------------------
def render_screen0(v, pnt=0x0000, pgt=None, width=40, palette=None,
                   height=192, fg=15, bg=4, *, ct=None, blink=False):
    if pgt is None:
        pgt = 0x1000 if width == 80 else 0x0800
    if ct is None and width == 80:
        ct = 0x0800
    regs = _registers(None)
    regs[7] = ((fg & 15) << 4) | (bg & 15)
    bases = {"name": pnt, "pattern": pgt}
    if ct is not None:
        bases["color"] = ct
    return render_vram(v, 0, regs, palette or TMS, height=height,
                       sprites=False, text_width=width, blink=blink, bases=bases)


def render_screen1(v, pnt=0x1800, pgt=0x0000, ct=0x2000, palette=None,
                   height=192):
    return render_vram(v, 1, _registers(None), palette or TMS, height=height,
                       sprites=False,
                       bases={"name": pnt, "pattern": pgt, "color": ct})


def render_screen2(v, pnt=0x1800, pgt=0x0000, ct=0x2000, palette=None,
                   height=192):
    return render_vram(v, 2, _registers(None), palette or TMS, height=height,
                       sprites=False,
                       bases={"name": pnt, "pattern": pgt, "color": ct})


def render_screen3(v, pnt=0x0800, pgt=0x0000, palette=None, height=192):
    return render_vram(v, 3, _registers(None), palette or TMS, height=height,
                       sprites=False, bases={"name": pnt, "pattern": pgt})


def render_screen4(v, pnt=0x1800, pgt=0x0000, ct=0x2000, palette=None,
                   height=192):
    return render_vram(v, 4, _registers(None), palette, height=height,
                       sprites=False,
                       bases={"name": pnt, "pattern": pgt, "color": ct})


def render_screen5(v, palette=None, height=212, base=0):
    return render_vram(v, 5, _registers(None), palette, height=height,
                       sprites=False, bases={"bitmap": base})


def render_screen6(v, palette=None, height=212, base=0):
    return render_vram(v, 6, _registers(None), palette, height=height,
                       sprites=False, bases={"bitmap": base})


def render_screen7(v, palette=None, height=212, base=0):
    return render_vram(v, 7, _registers(None), palette, height=height,
                       sprites=False, bases={"bitmap": base})


def render_screen8(v, height=212, base=0):
    return render_vram(v, 8, _registers(None), V9938_DEFAULT, height=height,
                       sprites=False, bases={"bitmap": base})


def render_screen10(v, palette=None, height=212, base=0):
    """Render SCREEN 10 (same YJK+YAE VRAM format as SCREEN 11)."""
    return render_vram(v, 10, _registers(None), palette, height=height,
                       sprites=False, bases={"bitmap": base})


def render_screen11(v, palette=None, height=212, base=0):
    """Render SCREEN 11 (YJK with per-pixel palette attributes)."""
    return render_vram(v, 11, _registers(None), palette, height=height,
                       sprites=False, bases={"bitmap": base})


def render_screen12(v, palette=None, height=212, base=0):
    """Render SCREEN 12 (pure YJK).  Palette is retained for sprite colours."""
    return render_vram(v, 12, _registers(None), palette, height=height,
                       sprites=False, bases={"bitmap": base})


def render_text_vram(names, font, width, palette=None, fg=15, bg=4):
    """Render SCREEN 0 name/font dumps (six active pixels per character)."""
    size = max(0x800 + len(font), len(names))
    vram = bytearray(size)
    vram[:len(names)] = names
    vram[0x800:0x800 + len(font)] = font
    return render_screen0(vram, 0, 0x800, width, palette or TMS,
                          height=max(1, len(names) // width) * 8, fg=fg, bg=bg)


# ---- capture helpers -------------------------------------------------------
def _rd(m, base, size):
    data = m.cmd(f'set d [debug read_block VRAM {base} {size}]; '
                 'binary scan $d H* h; set h')
    return bytes.fromhex(data.strip())


def _openmsx_registers(m):
    return [int(m.cmd(f'debug read "VDP regs" {index}')) for index in range(28)]


def _real_registers(m):
    regs = [0] * 28
    regs[:8] = m.peek(0xF3DF, 8)
    regs[8:24] = m.peek(0xFFE7, 16)
    regs[25:28] = m.peek(0xFFFA, 3)
    return regs


@dataclass(frozen=True)
class RealMSXCapturePlan:
    """Target state and byte ranges required for one real-MSX screenshot."""

    mode: int
    regs: tuple
    text_width: object
    height: int
    sprites: bool
    page: object
    ranges: tuple

    @property
    def vram_bytes(self):
        return sum(size for _base, size in self.ranges)

    @property
    def metadata_bytes(self):
        # SCRMOD, three register-shadow reads, and LINLEN in SCREEN 0.
        return 1 + 8 + 16 + 3 + (1 if self.mode == 0 else 0)

    @property
    def target_bytes(self):
        return self.metadata_bytes + self.vram_bytes

    @property
    def metadata_reads(self):
        return 4 + (1 if self.mode == 0 else 0)


@dataclass(frozen=True)
class RealMSXCapture:
    """Acquired target bytes, intentionally independent from PNG rendering."""

    plan: RealMSXCapturePlan
    vram: object


def _addresses_for_capture(mode, regs, height, sprites, page=None,
                           text_width=None):
    addresses = set()

    def add_table(base, low_bits, index_bits, indices):
        addresses.update(_table_addr(base, low_bits, index, index_bits)
                         for index in indices)

    if mode == 0:
        width = text_width or (80 if regs[0] & 0x04 else 40)
        if width == 40:
            add_table(regs[2] << 10, 10, 12,
                      (0xC00 + row * 40 + column
                       for row in range((height + 7) // 8)
                       for column in range(40)))
        else:
            add_table(regs[2] << 10, 10, 12,
                      range(((height + 7) // 8) * 80))
            colour_base = (regs[10] << 14) | (regs[3] << 6)
            add_table(colour_base, 6, 9,
                      range(((height + 7) // 8) * 10))
        add_table(regs[4] << 11, 11, 11, range(0x800))
    elif mode == 1:
        add_table(regs[2] << 10, 10, 10, range(0x400))
        add_table(regs[4] << 11, 11, 11, range(0x800))
        add_table((regs[10] << 14) | (regs[3] << 6), 6, 6, range(0x40))
    elif mode in (2, 4):
        add_table(regs[2] << 10, 10, 10, range(0x400))
        add_table(regs[4] << 11, 11, 13, range(0x2000))
        add_table((regs[10] << 14) | (regs[3] << 6), 6, 13, range(0x2000))
    elif mode == 3:
        add_table(regs[2] << 10, 10, 10, range(0x400))
        add_table(regs[4] << 11, 11, 11, range(0x800))
    else:
        base = _bitmap_page_base(mode, regs, page, None)
        stride = 128 if mode in (5, 6) else 256
        for y in range(height):
            line = (y + regs[23]) & 0xFF
            addresses.update(range(base + line * stride,
                                   base + (line + 1) * stride))

    if sprites and mode != 0 and not (regs[8] & 0x02):
        sprite_mode = 1 if mode in (1, 2, 3) else 2
        base = (regs[11] << 15) | (regs[5] << 7)
        add_table(base, 7, 10 if sprite_mode == 2 else 7,
                  range(0x400 if sprite_mode == 2 else 0x80))
        add_table(regs[6] << 11, 11, 11, range(0x800))
    return addresses


def _ranges(addresses):
    ordered = sorted(addresses)
    if not ordered:
        return
    start = previous = ordered[0]
    for address in ordered[1:]:
        if address != previous + 1:
            yield start, previous - start + 1
            start = address
        previous = address
    yield start, previous - start + 1


def _capture_vram(reader, addresses):
    vram = bytearray(VRAM_SIZE)
    for base, size in _ranges(addresses):
        vram[base:base + size] = reader(base, size)
    return vram


def capture_openmsx(m, path, *, palette=None, sprites=True, page=None):
    """Capture SCREEN 0--8 or 10--12 from openMSX logical VRAM."""
    mode = m.screen_mode()
    _validate_mode(mode)
    regs = _openmsx_registers(m)
    text_width = 80 if mode == 0 and regs[0] & 0x04 else None
    height = _active_height(regs, mode, None)
    if palette is None:
        try:
            raw = bytes.fromhex(m.cmd(
                'set d [debug read_block "VDP palette" 0 32]; '
                'binary scan $d H* h; set h').strip())
            palette = decode_palette(raw)
        except Exception:
            palette = V9938_DEFAULT
    addresses = _addresses_for_capture(mode, regs, height, sprites, page,
                                       text_width)
    vram = _capture_vram(lambda base, size: _rd(m, base, size), addresses)
    rgb, width, height = render_vram(vram, mode, regs, palette, height=height,
                                     page=page, sprites=sprites,
                                     text_width=text_width)
    write_png(path, width, height, rgb)
    return path, mode


def plan_realmsx_capture(m, *, sprites=True, page=None):
    """Read display metadata and plan the target bytes for a screenshot."""
    mode = m.peek(0xFCAF, 1)[0]  # SCRMOD
    _validate_mode(mode)
    regs = _real_registers(m)
    text_width = m.peek(0xF3B0, 1)[0] if mode == 0 else None  # LINLEN
    if text_width not in (40, 80):
        text_width = 80 if regs[0] & 0x04 else 40
    height = _active_height(regs, mode, None)
    addresses = _addresses_for_capture(mode, regs, height, sprites, page,
                                       text_width)
    return RealMSXCapturePlan(
        mode=mode, regs=tuple(regs), text_width=text_width, height=height,
        sprites=bool(sprites), page=page,
        ranges=tuple(_ranges(addresses)))


def acquire_realmsx_capture(m, *, sprites=True, page=None, plan=None):
    """Acquire only RAM/VRAM bytes; perform no rendering or file I/O."""
    if plan is None:
        plan = plan_realmsx_capture(m, sprites=sprites, page=page)
    vram = bytearray(VRAM_SIZE)
    for base, size in plan.ranges:
        vram[base:base + size] = m.vpeek(base, size)
    return RealMSXCapture(plan=plan, vram=vram)


def render_realmsx_capture(capture, path, *, palette=None):
    """Render previously acquired bytes and write their PNG on the host."""
    plan = capture.plan
    rgb, width, height = render_vram(
        capture.vram, plan.mode, plan.regs, palette or V9938_DEFAULT,
        height=plan.height, page=plan.page, sprites=plan.sprites,
        text_width=plan.text_width)
    write_png(path, width, height, rgb)
    return path, plan.mode


def capture_realmsx(m, path, *, palette=None, sprites=True, page=None):
    """Compatibility wrapper that acquires and renders a real-MSX screen.

    MCP callers that pause a running target must call
    :func:`acquire_realmsx_capture` inside the pause and
    :func:`render_realmsx_capture` only after resuming it.
    """
    capture = acquire_realmsx_capture(m, sprites=sprites, page=page)
    return render_realmsx_capture(capture, path, palette=palette)


def _render_text(m):
    """Compatibility helper used by older callers for openMSX SCREEN 0."""
    regs = _openmsx_registers(m)
    width = 80 if regs[0] & 0x04 else 40
    height = _active_height(regs, 0, None)
    addresses = _addresses_for_capture(0, regs, height, False, text_width=width)
    vram = _capture_vram(lambda base, size: _rd(m, base, size), addresses)
    return render_vram(vram, 0, regs, TMS, height=height, sprites=False,
                       text_width=width)
