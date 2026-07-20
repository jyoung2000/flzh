#!/usr/bin/env python3
"""
L4D2 Flashlight Mod Builder
===========================

A self-contained tkinter GUI that generates a Left 4 Dead 2 addon .vpk
customizing the player flashlight:

  * Brightness + Range  ->  flashlight cvars packed as cfg/flashlight_bright.cfg
                            (must be exec'd from the developer console; see the
                            README the tool writes next to the .vpk)
  * Hue                 ->  a tinted spotlight-cookie texture written as a real
                            Valve VTF (v7.2, uncompressed BGRA8888, built in
                            pure Python) packed as
                            materials/effects/flashlight001.vtf, overriding the
                            stock beam texture so the beam takes on the color.

The .vpk is written with a pure-Python VPK v1 packer -- no vpk.exe, no game
files needed -- and is re-parsed and CRC-checked after writing before success
is reported.

Dependencies: Python 3.8+ with tkinter (standard install). Pillow is needed
only for the hue tint; without it the tool still builds a working
brightness/range VPK and says so.

Run the GUI:      python3 l4d2_flashlight_vpk_gui.py
Headless build:   python3 l4d2_flashlight_vpk_gui.py --nogui \
                      --brightness 70 --range 60 --hue 120 --out ./build
"""

import argparse
import colorsys
import math
import os
import struct
import sys
import zlib

# Pillow is optional: only the hue-tinted beam texture needs it. Everything
# else (cvar cfg, VPK packing) is pure standard library.
try:
    from PIL import Image, ImageStat
    PIL_AVAILABLE = True
    PIL_IMPORT_ERROR = ""
except ImportError as _e:  # pragma: no cover - depends on environment
    Image = ImageStat = None
    PIL_AVAILABLE = False
    PIL_IMPORT_ERROR = str(_e)

APP_TITLE = "L4D2 Flashlight Mod Builder"
VPK_BASENAME = "flashlight_custom.vpk"

# ---------------------------------------------------------------------------
# Slider -> cvar tuning math
# ---------------------------------------------------------------------------
# Source projects the flashlight as a spotlight whose intensity falls off
# roughly like  1 / (constant + linear*d + quadratic*d^2)  with distance d.
# LOWER r_flashlightlinear therefore means a BRIGHTER beam, so the brightness
# slider is inverted when mapped. All clamps live here -- adjust to taste.

LINEAR_DIMMEST = 400.0   # r_flashlightlinear at brightness slider 0 (dim)
LINEAR_BRIGHTEST = 15.0  # hard floor at slider 100. Below ~15 the beam blows
                         # out nearby geometry to pure white; raise this floor
                         # for more safety margin, lower it at your own risk.

FAR_MIN = 400.0          # r_flashlightfar at range slider 0 (stock is 750)
FAR_MAX = 3000.0         # r_flashlightfar at range slider 100

NEAR_MIN = 8.0           # floor for r_flashlightnear. Stock is 4, which lets
                         # you blind yourself point-blank against a wall; 8+
                         # keeps the near field soft. The near plane is also
                         # pushed out slightly as range grows (see map below).
NEAR_MAX = 24.0

AMBIENT_MIN = 0.05       # r_flashlightambient floods the whole beam frustum
AMBIENT_MAX = 0.80       # with flat light -- the "fill the room" knob. >0.8
                         # washes out the scene completely.

FOV_WIDE = 58.0          # r_flashlightfov at range slider 0
FOV_NARROW = 50.0        # ...focusing slightly tighter at slider 100.
                         # Stock is 45; wider = the beam covers more of the
                         # room at the cost of per-area intensity. Past ~90
                         # the projection visibly distorts. (Cheat-gated:
                         # only applies where the cfg can run, e.g. maps or
                         # servers with sv_cheats 1.)


def map_sliders_to_cvars(brightness, range_pct):
    """Map brightness/range slider values (0-100) to clamped flashlight cvars.

    Returns a dict of cvar name -> numeric value.
    """
    b = max(0.0, min(100.0, float(brightness))) / 100.0
    r = max(0.0, min(100.0, float(range_pct))) / 100.0

    # Brightness: interpolate linear attenuation on a log curve so each slider
    # step feels like an even perceptual change (400 -> 15 spans ~27x; a
    # straight lerp would cram all the visible change into the last 20%).
    linear = LINEAR_DIMMEST * (LINEAR_BRIGHTEST / LINEAR_DIMMEST) ** b
    linear = max(LINEAR_BRIGHTEST, linear)  # enforce the anti-whiteout floor

    far = FAR_MIN + (FAR_MAX - FAR_MIN) * r

    # Near plane scales gently with range and never drops below NEAR_MIN, so
    # point-blank surfaces stay soft no matter what the sliders say.
    near = max(NEAR_MIN, min(NEAR_MAX, NEAR_MIN + far * 0.004))

    # A touch of ambient fill that grows with brightness.
    ambient = AMBIENT_MIN + (AMBIENT_MAX - AMBIENT_MIN) * b

    fov = FOV_WIDE + (FOV_NARROW - FOV_WIDE) * r

    return {
        "r_flashlightfar": round(far, 1),
        "r_flashlightnear": round(near, 1),
        # constant=1 caps the attenuation denominator at >=1, so intensity can
        # never diverge at zero distance -- the other half of whiteout safety.
        "r_flashlightconstant": 1,
        "r_flashlightlinear": round(linear, 1),
        "r_flashlightquadratic": 0,
        "r_flashlightambient": round(ambient, 2),
        "r_flashlightfov": round(fov, 1),
        # Not present on every Source branch (unknown cvars are just ignored
        # with a console note); where it exists it scales beam intensity
        # directly, so ride the brightness slider from stock-ish 0.25 to 1.
        "r_flashlightbrightness": round(0.25 + 0.75 * b, 2),
    }


def build_cfg_text(cvars):
    """Render the cvars as cfg/flashlight_bright.cfg content."""
    lines = [
        "// Custom flashlight settings -- generated by " + APP_TITLE,
        "// Apply from the developer console:  exec flashlight_bright.cfg",
        "// (Enable the console: Options > Keyboard/Mouse > Allow Developer Console)",
        "",
    ]
    lines += ["{} {}".format(name, value) for name, value in cvars.items()]
    lines += ["", 'echo "Custom flashlight settings applied."', ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Beam texture (spotlight cookie) -- Pillow
# ---------------------------------------------------------------------------
# Hue is not a cvar: the beam color comes from the spotlight "cookie" texture
# the engine projects (materials/effects/flashlight001). We regenerate that
# cookie tinted to the chosen hue.
#
# Brightness is baked into this texture too, not just the cfg: most
# r_flashlight* cvars are cheat-gated outside single player / sv_cheats, so
# the cookie is the one brightness control that works everywhere. The engine
# multiplies the projected light by this texture, so a flat, near-white disc
# is the brightest legal flashlight there is ("fullbright-style"); a dark,
# steep gradient dims it no matter what the cvars say. Tuning knobs:

COOKIE_SIZE = 256        # texture resolution (power of two)
PLATEAU_MIN = 0.30       # radius fraction at FULL intensity, brightness 0...
PLATEAU_MAX = 0.94       # ...and brightness 100. The engine stretches this
                         # texture across the flashlight's whole projection
                         # cone, so dark edge texels are wasted beam angle --
                         # at max brightness the lit area runs nearly to the
                         # texture border and fills almost the entire cone.
                         # (1.0 would be a hard-edged shape with no soft rim.)
SHAPE_POW_MIN = 2.0      # superellipse exponent at brightness 0: 2 = circle
SHAPE_POW_MAX = 12.0     # ...at brightness 100: the frustum cross-section is
                         # SQUARE (texture mapped edge-to-edge), so a circle
                         # inscribes it and wastes the corners. Raising the
                         # exponent morphs the shape toward a rounded
                         # rectangle that covers ~97% of the frustum -- the
                         # widest beam possible without the cheat-gated fov.
V_STRETCH_MAX = 1.22     # vertical axis scale at brightness 100 (1.0 at 0).
                         # >1 stretches the lit shape past the top/bottom of
                         # the texture, so the beam runs full-bleed to the
                         # frustum's upper and lower edges. Kept > H_STRETCH so
                         # the beam stays a taller-than-wide portrait shape for
                         # more ceiling/floor. Raise toward ~1.3 for an even
                         # squarer top/bottom edge; 1.0 = no vertical stretch.
H_STRETCH_MAX = 1.12     # horizontal axis scale at brightness 100 (1.0 at 0).
                         # Same idea sideways: at max brightness the lit shape
                         # also overshoots the left/right texture edges, so the
                         # rectangle reaches ALL FOUR frustum edges -- ~100% of
                         # the projection cone lit, the absolute non-cheat cap.
                         # The frustum's angular width itself is fixed by the
                         # engine (r_flashlightfov, cheat-gated); nothing
                         # texture-side casts light beyond it.
GAIN_MIN = 0.55          # overall luminance scale at brightness 0
GAIN_MAX = 1.00          # at brightness 100 the center hits pure 255 white
CORE_WHITENESS = 0.70    # how strongly the hot region desaturates to white
                         # (color lives in the rim; raise TINT color at the
                         # cost of raw brightness by lowering this)
TINT_SAT = 0.80          # saturation of the tint at the beam rim (1.0 =
                         # fully saturated color, 0 = plain white beam)


def hue_to_rgb(hue_deg, sat=TINT_SAT, val=1.0):
    """Hue in degrees -> (r, g, b) floats 0..1."""
    return colorsys.hsv_to_rgb((hue_deg % 360.0) / 360.0, sat, val)


def build_cookie_image(hue_deg, brightness=100.0, size=COOKIE_SIZE):
    """Build the tinted spotlight cookie as a Pillow RGBA image.

    Shape: a flat full-intensity plateau (sized by the brightness slider)
    with a smoothstep roll-off toward the border. At low/mid brightness the
    rim rolls off to pure black, giving a soft-edged beam. At high brightness
    the plateau + axis stretch push the lit area past the texture edges, so
    the beam fills the whole projection frustum with a crisp rectangular
    edge. That is safe here: the engine bounds the flashlight to its frustum
    and the CLAMPS/CLAMPT flags stop the cookie tiling, so a bright edge just
    ends cleanly at the cone boundary (confirmed in-game) rather than
    smearing across the scene. The center is white-hot, with the hue tint
    strongest in the rim, matching the preview canvas.
    """
    if not PIL_AVAILABLE:
        raise RuntimeError("Pillow is not installed; cannot build the cookie")

    b = max(0.0, min(100.0, float(brightness))) / 100.0
    plateau = PLATEAU_MIN + (PLATEAU_MAX - PLATEAU_MIN) * b
    gain = GAIN_MIN + (GAIN_MAX - GAIN_MIN) * b
    shape_pow = SHAPE_POW_MIN + (SHAPE_POW_MAX - SHAPE_POW_MIN) * b
    v_stretch = 1.0 + (V_STRETCH_MAX - 1.0) * b
    h_stretch = 1.0 + (H_STRETCH_MAX - 1.0) * b
    tint = hue_to_rgb(hue_deg)
    half = size / 2.0
    pixels = []
    for y in range(size):
        for x in range(size):
            # Superellipse "radius", normalized so 1.0 lands just inside the
            # bitmap edge midpoints: exponent 2 is a plain circle, higher
            # exponents bulge toward the corners of the square frustum.
            # Dividing each term by its stretch elongates the shape so it
            # overshoots the bitmap and clips to a full-bleed rectangle on
            # both axes -- at max brightness the lit area reaches every
            # frustum edge. The soft roll-off survives only where a stretch
            # stays near 1.0.
            nx = abs(x - half + 0.5) / (half - 2.0) / h_stretch
            ny = abs(y - half + 0.5) / (half - 2.0) / v_stretch
            r = (nx ** shape_pow + ny ** shape_pow) ** (1.0 / shape_pow)
            if r >= 1.0:
                intensity = 0.0
            elif r <= plateau:
                intensity = 1.0
            else:
                u = (r - plateau) / (1.0 - plateau)
                intensity = 1.0 - (3.0 * u * u - 2.0 * u * u * u)
            w = CORE_WHITENESS * intensity * intensity   # white-hot center
            px = []
            for c in tint:
                col = c + (1.0 - c) * w                  # lerp tint -> white
                px.append(int(round(255.0 * col * intensity * gain)))
            pixels.append((px[0], px[1], px[2], 255))
    img = Image.new("RGBA", (size, size))
    img.putdata(pixels)
    return img


# ---------------------------------------------------------------------------
# VTF writer (Valve Texture Format v7.2, pure Python)
# ---------------------------------------------------------------------------
# A VTF is just a fixed header + optional DXT1 thumbnail + image data ordered
# smallest mipmap first. Uncompressed BGRA8888 needs no DXT encoder for the
# main image, and every Source branch (L4D2 included) loads v7.2 files.

VTF_FORMAT_BGRA8888 = 12
VTF_FORMAT_DXT1 = 13
VTF_FLAG_CLAMPS = 0x0004
VTF_FLAG_CLAMPT = 0x0008
VTF_FLAG_NOLOD = 0x0200


def _encode_dxt1_solid_blocks(img):
    """Encode an RGB(A) image as DXT1 where each 4x4 block is a solid color.

    Crude but valid -- only used for the low-res thumbnail the format wants.
    """
    w, h = img.size
    rgb = img.convert("RGB")
    out = bytearray()
    for by in range(0, h, 4):
        for bx in range(0, w, 4):
            rs = gs = bs = 0
            for y in range(by, by + 4):
                for x in range(bx, bx + 4):
                    pr, pg, pb = rgb.getpixel((x, y))
                    rs += pr
                    gs += pg
                    bs += pb
            r, g, b = rs // 16, gs // 16, bs // 16
            c565 = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            # color0 == color1 and all indices 0 -> every texel = that color.
            out += struct.pack("<HHI", c565, c565, 0)
    return bytes(out)


def build_vtf(img):
    """Serialize a Pillow image as a VTF v7.2 file (bytes)."""
    w, h = img.size
    if w & (w - 1) or h & (h - 1):
        raise ValueError("VTF dimensions must be powers of two")

    # Full mip chain, e.g. 256 -> 1 = 9 mips, stored smallest first.
    mips = [img.convert("RGBA")]
    while mips[-1].size[0] > 1 or mips[-1].size[1] > 1:
        mw, mh = mips[-1].size
        mips.append(mips[0].resize((max(1, mw // 2), max(1, mh // 2)),
                                   Image.LANCZOS))

    def rgba_to_bgra(im):
        r, g, b, a = im.split()
        return Image.merge("RGBA", (b, g, r, a)).tobytes()

    # Reflectivity metadata: mean color, 0..1 per channel.
    thumb = mips[0].resize((16, 16), Image.LANCZOS)
    refl = [c / 255.0 for c in ImageStat.Stat(thumb.convert("RGB")).mean]
    thumb_data = _encode_dxt1_solid_blocks(thumb)

    flags = VTF_FLAG_CLAMPS | VTF_FLAG_CLAMPT | VTF_FLAG_NOLOD
    header = struct.pack(
        "<4sIIIHHIHH4x3f4xfIBiBBH",
        b"VTF\0",
        7, 2,                    # version 7.2 (adds the depth field)
        80,                      # header size, 16-byte aligned
        w, h,
        flags,
        1, 0,                    # frames, first frame
        refl[0], refl[1], refl[2],
        0.0,                     # bumpmap scale (unused)
        VTF_FORMAT_BGRA8888,
        len(mips),               # mipmap count
        VTF_FORMAT_DXT1,         # low-res thumbnail format
        16, 16,                  # low-res thumbnail size
        1,                       # depth (v7.2 field)
    )
    header += b"\0" * (80 - len(header))

    body = bytearray(thumb_data)
    for mip in reversed(mips):   # smallest mip first
        body += rgba_to_bgra(mip)
    return bytes(header) + bytes(body)


# ---------------------------------------------------------------------------
# VPK v1 packer (pure Python)
# ---------------------------------------------------------------------------
# Layout: 12-byte header (magic, version=1, tree size), then a directory tree
# of nested null-terminated strings (extension -> path -> filename), each file
# carrying an 18-byte entry, then the file data. archive_index 0x7fff means
# "data lives in this same file, offset relative to the end of the tree".

VPK_MAGIC = 0x55AA1234


def _split_vpk_path(path):
    """'materials/effects/foo.vtf' -> ('vtf', 'materials/effects', 'foo').

    The VPK tree represents the root directory and empty extensions as a
    single space. Everything is lowercased (Source is case-insensitive and
    the format expects lowercase).
    """
    path = path.replace("\\", "/").strip("/").lower()
    directory, _, filename = path.rpartition("/")
    if not directory:
        directory = " "
    name, dot, ext = filename.rpartition(".")
    if not dot or not name or not ext:
        name, ext = filename, " "
    return ext, directory, name


def build_vpk(files):
    """Pack {'path/in/vpk': bytes} into a single-file VPK v1 (bytes)."""
    # Group as extension -> directory -> [(name, data)]
    tree = {}
    for path, data in files.items():
        ext, directory, name = _split_vpk_path(path)
        tree.setdefault(ext, {}).setdefault(directory, []).append((name, data))

    tree_buf = bytearray()
    data_buf = bytearray()
    for ext in sorted(tree):
        tree_buf += ext.encode("ascii") + b"\0"
        for directory in sorted(tree[ext]):
            tree_buf += directory.encode("ascii") + b"\0"
            for name, data in sorted(tree[ext][directory]):
                tree_buf += name.encode("ascii") + b"\0"
                tree_buf += struct.pack(
                    "<IHHIIH",
                    zlib.crc32(data) & 0xFFFFFFFF,
                    0,               # preload bytes
                    0x7FFF,          # archive index: data is in this file
                    len(data_buf),   # offset relative to end of tree
                    len(data),
                    0xFFFF,          # entry terminator
                )
                data_buf += data
            tree_buf += b"\0"        # end of files in this directory
        tree_buf += b"\0"            # end of directories for this extension
    tree_buf += b"\0"                # end of extensions

    header = struct.pack("<III", VPK_MAGIC, 1, len(tree_buf))
    return header + bytes(tree_buf) + bytes(data_buf)


def verify_vpk(path):
    """Independently re-parse a VPK v1 file and CRC-check every entry.

    Returns the list of file paths found inside. Raises ValueError on any
    header/tree/checksum problem -- callers report success only if this passes.
    """
    with open(path, "rb") as fh:
        blob = fh.read()
    if len(blob) < 12:
        raise ValueError("file too small to be a VPK")
    magic, version, tree_size = struct.unpack_from("<III", blob, 0)
    if magic != VPK_MAGIC:
        raise ValueError("bad VPK magic 0x%08x" % magic)
    if version != 1:
        raise ValueError("expected VPK version 1, got %d" % version)
    if 12 + tree_size > len(blob):
        raise ValueError("tree size exceeds file size")

    pos = 12
    data_base = 12 + tree_size
    found = []

    def read_cstr():
        nonlocal pos
        end = blob.index(b"\0", pos)
        s = blob[pos:end].decode("ascii")
        pos = end + 1
        return s

    while True:
        ext = read_cstr()
        if not ext:
            break
        while True:
            directory = read_cstr()
            if not directory:
                break
            while True:
                name = read_cstr()
                if not name:
                    break
                crc, preload, arch, off, length, term = struct.unpack_from(
                    "<IHHIIH", blob, pos)
                # The entry CRC covers the whole file: inline preload bytes
                # (stored right after the 18-byte entry) + archive data.
                preload_data = blob[pos + 18:pos + 18 + preload]
                pos += 18 + preload
                if len(preload_data) != preload:
                    raise ValueError("preload out of bounds for " + name)
                if term != 0xFFFF:
                    raise ValueError("bad entry terminator for " + name)
                if arch != 0x7FFF:
                    raise ValueError("unexpected external archive index")
                data = preload_data + \
                    blob[data_base + off:data_base + off + length]
                if len(data) != preload + length:
                    raise ValueError("data out of bounds for " + name)
                if zlib.crc32(data) & 0xFFFFFFFF != crc:
                    raise ValueError("CRC mismatch for " + name)
                full = name + ("." + ext if ext != " " else "")
                if directory != " ":
                    full = directory + "/" + full
                found.append(full)
    if pos != data_base:
        raise ValueError("directory tree size mismatch")
    if not found:
        raise ValueError("VPK contains no files")
    return found


# ---------------------------------------------------------------------------
# Addon metadata + README
# ---------------------------------------------------------------------------

def build_addoninfo(cvars, hue_deg, tinted):
    hue_txt = "hue %d deg" % round(hue_deg) if tinted else "stock color"
    desc = ("Custom flashlight: range %s, linear atten %s, %s. "
            "Open the console and run: exec flashlight_bright.cfg" % (
                cvars["r_flashlightfar"], cvars["r_flashlightlinear"], hue_txt))
    return (
        '"AddonInfo"\n'
        '{\n'
        '\taddonSteamAppID\t\t550\n'
        '\taddontitle\t\t"Custom Flashlight (brightness/range%s)"\n'
        '\taddonversion\t\t"1.0"\n'
        '\taddonauthor\t\t"%s"\n'
        '\taddonDescription\t"%s"\n'
        '}\n' % ("/hue" if tinted else "", APP_TITLE, desc)
    )


def build_readme(cvars, hue_deg, tinted, tint_note, brightness=None):
    lines = [
        "Custom Flashlight VPK -- generated by " + APP_TITLE,
        "=" * 60,
        "",
        "INSTALL",
        "  Copy %s into:" % VPK_BASENAME,
        "    Steam/steamapps/common/Left 4 Dead 2/left4dead2/addons/",
        "  Then enable it in the in-game Add-ons menu if it is not already.",
        "",
        "BRIGHTNESS + BEAM COLOR (texture override -- works automatically)",
    ]
    if tinted:
        lines += [
            "  The addon replaces the spotlight cookie texture",
            "  materials/effects/flashlight001.vtf with one baked at %d%%" %
            round(brightness if brightness is not None else 100),
            "  brightness and hue %d deg. The engine multiplies the beam" %
            round(hue_deg),
            "  by this texture, so the brighter/flatter cookie brightens",
            "  the flashlight everywhere -- no console commands, and it",
            "  cannot be blocked by cheat-gated cvars. At high brightness",
            "  the lit shape widens from a disc to a full-height portrait",
            "  rectangle spanning nearly the flashlight's entire projection",
            "  cone, top to bottom, for maximum ceiling and floor coverage:",
            "  the widest/tallest beam possible without cheats (the cone",
            "  itself is fixed by the engine at ~45 degrees; only the cfg",
            "  below can exceed that, where cheats allow).",
        ]
    else:
        lines += ["  " + ln for ln in tint_note.splitlines()]
    lines += [
        "",
        "EXTRA KICK: RANGE / ATTENUATION CVARS (optional, one command)",
        "  The VPK also ships cfg/flashlight_bright.cfg with:",
    ]
    lines += ["    %s %s" % (k, v) for k, v in cvars.items()]
    lines += [
        "  Cvars are not applied automatically by an addon. Open the",
        "  developer console (enable it under Options > Keyboard/Mouse)",
        "  and run:",
        "      exec flashlight_bright.cfg",
        "  To apply it every session, add that line to",
        "  left4dead2/cfg/autoexec.cfg. Note that most r_flashlight*",
        "  cvars are cheat-gated outside single player / local servers",
        "  with sv_cheats 1 -- if the console says 'cheat cvar', the",
        "  texture override above is still doing its job. On maps or",
        "  servers where sv_cheats IS on (many workshop maps enable it",
        "  when loaded via the console), exec the cfg to get the wider",
        "  beam fov and the ambient room fill on top of the texture.",
    ]
    lines += [
        "",
        "NOTES / LIMITATIONS",
        "  * Local-only, client-side tweak: it changes YOUR flashlight",
        "    rendering only. Other players never see it.",
        "  * Some servers disable addons entirely, and servers with",
        "    consistency checking may block the texture override.",
        "  * This is a tuned flashlight, not engine mat_fullbright: it",
        "    brightens the beam, it does not remove darkness everywhere.",
        "",
        "UNINSTALL",
        "  Delete %s from the addons folder. If you exec'd" % VPK_BASENAME,
        "  the cfg, the cvars reset when the game restarts.",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Generation pipeline (GUI-independent, used by the CLI and tests too)
# ---------------------------------------------------------------------------

def generate_vpk(brightness, range_pct, hue_deg, out_dir, tint_enabled=True):
    """Build and write the .vpk (and a README.txt beside it).

    Returns (vpk_path, message) on success; raises on failure.
    """
    out_dir = os.path.abspath(out_dir)
    if not os.path.isdir(out_dir):
        raise ValueError("Output folder does not exist: %s" % out_dir)

    cvars = map_sliders_to_cvars(brightness, range_pct)

    tinted = bool(tint_enabled) and PIL_AVAILABLE
    if tint_enabled and not PIL_AVAILABLE:
        tint_note = (
            "Hue tint was SKIPPED: Pillow is not installed on the machine\n"
            "that generated this VPK (pip install pillow), so the stock\n"
            "beam texture is untouched. Brightness/range still work."
        )
    elif not tint_enabled:
        tint_note = ("Hue tint was disabled at generation time; the stock\n"
                     "beam texture is untouched.")
    else:
        tint_note = ""

    files = {
        "addoninfo.txt": build_addoninfo(cvars, hue_deg, tinted)
        .encode("utf-8"),
        "cfg/flashlight_bright.cfg": build_cfg_text(cvars).encode("utf-8"),
    }
    if tinted:
        # Brightness is baked into the cookie as well as the cfg -- the
        # texture is the one control cheat-gated servers can't ignore.
        cookie = build_cookie_image(hue_deg, brightness)
        files["materials/effects/flashlight001.vtf"] = build_vtf(cookie)

    readme = build_readme(cvars, hue_deg, tinted, tint_note, brightness)
    files["readme.txt"] = readme.encode("utf-8")

    vpk_path = os.path.join(out_dir, VPK_BASENAME)
    blob = build_vpk(files)
    with open(vpk_path, "wb") as fh:
        fh.write(blob)
    # Tool-specific name so we never clobber a README.txt the user owns.
    readme_name = os.path.splitext(VPK_BASENAME)[0] + "_README.txt"
    with open(os.path.join(out_dir, readme_name), "w",
              encoding="utf-8") as fh:
        fh.write(readme)

    packed = verify_vpk(vpk_path)  # raises if the header/tree/CRCs are bad

    msg = "Wrote %s (%d files, VPK v1 verified)." % (vpk_path, len(packed))
    if tinted:
        msg += " Beam tinted to hue %d deg via flashlight001.vtf." % \
            round(hue_deg)
    elif tint_enabled:
        msg += " Hue tint skipped: Pillow not installed."
    return vpk_path, msg


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def run_gui():
    import tkinter as tk
    from tkinter import filedialog, ttk

    BG = "#15151a"
    FG = "#e8e8ee"
    MUTED = "#9a9aa8"
    ACCENT = "#3a3a46"

    class App:
        CANVAS_W = 480
        CANVAS_H = 260

        def __init__(self, root):
            self.root = root
            root.title(APP_TITLE)
            root.configure(bg=BG)
            root.resizable(False, False)

            self.brightness = tk.DoubleVar(value=70.0)
            self.hue = tk.DoubleVar(value=40.0)
            self.range_ = tk.DoubleVar(value=55.0)
            self.out_dir = tk.StringVar(value=os.getcwd())
            self.tint_on = tk.BooleanVar(value=PIL_AVAILABLE)
            self._redraw_queued = False

            pad = {"padx": 14}
            row = 0

            tk.Label(root, text="Flashlight preview", bg=BG, fg=MUTED,
                     anchor="w").grid(row=row, column=0, columnspan=3,
                                      sticky="we", pady=(12, 2), **pad)
            row += 1
            self.canvas = tk.Canvas(root, width=self.CANVAS_W,
                                    height=self.CANVAS_H, bg="#000000",
                                    highlightthickness=1,
                                    highlightbackground=ACCENT)
            self.canvas.grid(row=row, column=0, columnspan=3, **pad)
            row += 1

            row = self._add_slider(row, "Brightness", self.brightness,
                                   0, 100, "%")
            row = self._add_slider(row, "Hue", self.hue, 0, 360, "°")
            row = self._add_slider(row, "Range", self.range_, 0, 100, "%")

            self.cvar_line = tk.Label(root, text="", bg=BG, fg=MUTED,
                                      font=("TkFixedFont", 8), anchor="w")
            self.cvar_line.grid(row=row, column=0, columnspan=3, sticky="we",
                                pady=(2, 8), **pad)
            row += 1

            self.tint_check = tk.Checkbutton(
                root,
                text="Bake brightness + hue into beam texture (needs Pillow)",
                variable=self.tint_on, command=self._on_change,
                bg=BG, fg=FG, activebackground=BG, activeforeground=FG,
                selectcolor=ACCENT, anchor="w")
            self.tint_check.grid(row=row, column=0, columnspan=3, sticky="we",
                                 **pad)
            row += 1
            if not PIL_AVAILABLE:
                self.tint_on.set(False)
                self.tint_check.configure(state="disabled")
                tk.Label(root, fg="#e0a94a", bg=BG, anchor="w", justify="left",
                         text=("Pillow not found -- beam texture disabled "
                               "(install with:  pip install pillow).\n"
                               "The VPK still builds, but brightness then "
                               "relies on cvars that many servers "
                               "cheat-gate.")
                         ).grid(row=row, column=0, columnspan=3, sticky="we",
                                **pad)
                row += 1

            tk.Label(root, text="Output folder", bg=BG, fg=MUTED,
                     anchor="w").grid(row=row, column=0, sticky="w",
                                      pady=(10, 0), **pad)
            row += 1
            entry = tk.Entry(root, textvariable=self.out_dir, state="readonly",
                             readonlybackground="#232330", fg=FG,
                             relief="flat", width=48)
            entry.grid(row=row, column=0, columnspan=2, sticky="we",
                       padx=(14, 6))
            tk.Button(root, text="Browse…", command=self._browse,
                      bg=ACCENT, fg=FG, activebackground="#4a4a58",
                      activeforeground=FG, relief="flat").grid(
                row=row, column=2, sticky="we", padx=(0, 14))
            row += 1

            self.gen_btn = tk.Button(root, text="Generate VPK",
                                     command=self._generate, bg="#2e5e3a",
                                     fg="#ffffff", activebackground="#387146",
                                     activeforeground="#ffffff", relief="flat",
                                     font=("TkDefaultFont", 10, "bold"),
                                     pady=6)
            self.gen_btn.grid(row=row, column=0, columnspan=3, sticky="we",
                              pady=(12, 4), **pad)
            row += 1

            self.status = tk.Label(root, text="Ready.", bg=BG, fg=MUTED,
                                   anchor="w", justify="left",
                                   wraplength=self.CANVAS_W)
            self.status.grid(row=row, column=0, columnspan=3, sticky="we",
                             pady=(0, 12), **pad)

            root.grid_columnconfigure(0, weight=1)
            self._on_change()

        def _add_slider(self, row, label, var, lo, hi, unit):
            tk.Label(self.root, text=label, bg=BG, fg=FG, anchor="w",
                     width=10).grid(row=row, column=0, sticky="w", padx=14)
            scale = tk.Scale(self.root, variable=var, from_=lo, to=hi,
                             orient="horizontal", showvalue=False,
                             resolution=1, command=lambda _v: self._on_change(),
                             bg=BG, fg=FG, troughcolor="#232330",
                             highlightthickness=0, bd=0,
                             activebackground="#6a6a7a", length=300)
            scale.grid(row=row, column=1, sticky="we", padx=4, pady=3)
            readout = tk.Label(self.root, text="", bg=BG, fg=FG, width=6,
                               anchor="e")
            readout.grid(row=row, column=2, sticky="e", padx=(0, 14))
            var.trace_add("write", lambda *_: readout.configure(
                text="%d%s" % (round(var.get()), unit)))
            readout.configure(text="%d%s" % (round(var.get()), unit))
            return row + 1

        # -- events ---------------------------------------------------------

        def _on_change(self, *_):
            # Coalesce the flood of slider callbacks into one redraw per
            # event-loop pass.
            if not self._redraw_queued:
                self._redraw_queued = True
                self.root.after_idle(self._redraw)

        def _browse(self):
            chosen = filedialog.askdirectory(
                initialdir=self.out_dir.get() or os.getcwd(),
                title="Choose output folder")
            if chosen:
                self.out_dir.set(chosen)

        def _generate(self):
            self.status.configure(text="Generating…", fg=MUTED)
            self.gen_btn.configure(state="disabled")
            self.root.update_idletasks()
            try:
                _, msg = generate_vpk(self.brightness.get(),
                                      self.range_.get(), self.hue.get(),
                                      self.out_dir.get(),
                                      tint_enabled=self.tint_on.get())
                self.status.configure(text=msg, fg="#7fd18a")
            except Exception as exc:
                self.status.configure(text="Error: %s" % exc, fg="#e07a7a")
            finally:
                self.gen_btn.configure(state="normal")

        # -- preview rendering ---------------------------------------------

        def _beam_color(self, value, sat_scale=1.0):
            """Preview color for the current hue at a given intensity."""
            if self.tint_on.get():
                h, s = self.hue.get() % 360.0 / 360.0, TINT_SAT * sat_scale
            else:
                h, s = 40.0 / 360.0, 0.06 * sat_scale  # stock warm-white
            r, g, b = colorsys.hsv_to_rgb(h, s, max(0.0, min(1.0, value)))
            return "#%02x%02x%02x" % (round(r * 255), round(g * 255),
                                      round(b * 255))

        def _redraw(self):
            self._redraw_queued = False
            cv = self.canvas
            cv.delete("all")

            bright = self.brightness.get() / 100.0
            rng = self.range_.get() / 100.0
            cvars = map_sliders_to_cvars(self.brightness.get(),
                                         self.range_.get())
            self.cvar_line.configure(text="  ".join(
                "%s %s" % (k.replace("r_flashlight", ""), v)
                for k, v in cvars.items()))

            ox, oy = 36, self.CANVAS_H / 2
            # Beam length tracks the range slider; overall value tracks
            # brightness (floored so the beam never vanishes entirely).
            length = 90 + (self.CANVAS_W - 100 - 90) * rng
            value = 0.25 + 0.75 * bright
            # Brightness also widens the lit share of the projection cone
            # (the cookie plateau grows and stretches vertically), so the
            # preview cone -- a side view, where spread = vertical beam
            # extent -- opens up with it.
            v_stretch = 1.0 + (V_STRETCH_MAX - 1.0) * bright
            half_angle = math.radians(cvars["r_flashlightfov"]) / 2 \
                * (0.52 + 0.28 * bright) * v_stretch

            # Ambient spill: a dim layered glow around the flashlight itself.
            spill = 24 + 34 * bright
            for k in range(3, 0, -1):
                rad = spill * k / 3.0
                cv.create_oval(ox - rad, oy - rad, ox + rad, oy + rad,
                               fill=self._beam_color(
                                   (0.10 + 0.10 * bright) * (4 - k) / 3.0,
                                   0.7),
                               outline="")

            # The cone: nested pie slices, outer = wide and dim, inner =
            # narrow, longer and brighter, ending white-hot on the axis.
            # Overdrawing brighter over dimmer fakes a radial gradient
            # (tkinter has no real alpha blending).
            layers = 24
            for i in range(layers):
                t = i / (layers - 1)
                a = half_angle * (1.0 - 0.82 * t)
                ln = length * (0.72 + 0.28 * t)
                val = value * (0.16 + 0.84 * t ** 1.6)
                sat = 1.0 - 0.70 * t ** 2  # core desaturates toward white
                pts = [(ox, oy)]
                steps = 14
                for s in range(steps + 1):
                    th = -a + (2 * a) * s / steps
                    pts.append((ox + ln * math.cos(th),
                                oy + ln * math.sin(th)))
                cv.create_polygon(*[c for p in pts for c in p],
                                  fill=self._beam_color(val, sat), outline="")

            # Flashlight body.
            cv.create_rectangle(ox - 26, oy - 9, ox - 2, oy + 9,
                                fill="#3c3c44", outline="#565662")
            cv.create_oval(ox - 6, oy - 7, ox + 6, oy + 7,
                           fill=self._beam_color(value, 0.4),
                           outline="#565662")

            cv.create_text(8, self.CANVAS_H - 10, anchor="w",
                           fill="#666672", font=("TkDefaultFont", 8),
                           text=("Approximate preview for tuning — "
                                 "not a game-accurate render"))

    root = tk.Tk()
    App(root)
    root.mainloop()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(  # __doc__ is None under python -OO
        description=(__doc__ or APP_TITLE).strip().splitlines()[0])
    ap.add_argument("--nogui", action="store_true",
                    help="build a VPK from the flags below, no GUI")
    ap.add_argument("--brightness", type=float, default=70,
                    help="0-100 (default 70)")
    ap.add_argument("--range", dest="range_pct", type=float, default=55,
                    help="0-100 (default 55)")
    ap.add_argument("--hue", type=float, default=40,
                    help="0-360 degrees (default 40)")
    ap.add_argument("--out", default=".", help="output folder (default .)")
    ap.add_argument("--no-tint", action="store_true",
                    help="skip the beam texture, cvars only")
    args = ap.parse_args(argv)

    if args.nogui:
        _, msg = generate_vpk(args.brightness, args.range_pct, args.hue,
                              args.out, tint_enabled=not args.no_tint)
        print(msg)
        return 0

    try:
        import tkinter
    except ImportError:
        print("tkinter is not available in this Python. Install your "
              "distro's python3-tk package, or build headless with --nogui "
              "(see --help).", file=sys.stderr)
        return 1
    try:
        run_gui()
    except tkinter.TclError as exc:
        # tkinter present but no display (SSH session, bare server, WSL
        # without an X server) -- point at the headless path instead of
        # dumping a traceback.
        print("Could not open a display (%s). Build headless with --nogui "
              "(see --help)." % exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
