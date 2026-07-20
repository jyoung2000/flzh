# L4D2 Flashlight Mod Builder

A self-contained Python desktop GUI that builds a Left 4 Dead 2 addon `.vpk`
customizing the player flashlight — brightness, range, and beam color — with a
live approximate beam preview.

## Run

```
python3 l4d2_flashlight_vpk_gui.py
```

Requires Python 3 with tkinter (bundled with the standard python.org installer
on Windows/macOS; `sudo apt install python3-tk` on Debian/Ubuntu). Pillow is
optional but needed for the hue tint:

```
pip install pillow
```

Without Pillow the tool still builds a working brightness/range VPK and tells
you the hue step was skipped.

Headless / scripted build:

```
python3 l4d2_flashlight_vpk_gui.py --nogui --brightness 70 --range 60 --hue 120 --out ./build
```

## What it generates

One `flashlight_custom.vpk` (plus a `flashlight_custom_README.txt` beside it)
containing:

| File in VPK | Purpose |
|---|---|
| `cfg/flashlight_bright.cfg` | Brightness/range cvars (`r_flashlightfar`, `r_flashlightlinear`, …). Apply in-game with `exec flashlight_bright.cfg` in the developer console. |
| `materials/effects/flashlight001.vtf` | Hue-tinted spotlight cookie (a real VTF v7.2 written in pure Python) that replaces the stock beam texture, coloring the beam automatically. |
| `addoninfo.txt`, `readme.txt` | Addon metadata + install/usage notes. |

Install by copying the `.vpk` into
`Steam/steamapps/common/Left 4 Dead 2/left4dead2/addons/`.

The VPK is packed by a built-in pure-Python VPK v1 packer (no `vpk.exe`, no
game files needed) and is re-parsed and CRC-verified after writing before
success is reported.

## Notes

* Client-side and local-only; other players don't see it, some servers
  disable addons, and some `r_flashlight*` cvars are cheat-flagged outside
  single player / local servers.
* The preview canvas is an approximation for tuning, not a game-accurate
  render.
* Brightness/near-plane clamps are enforced so you can't generate a config
  that whites out nearby walls — the tuning constants (and comments explaining
  them) are at the top of `l4d2_flashlight_vpk_gui.py`.
