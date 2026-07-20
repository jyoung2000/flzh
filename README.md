# L4D2 Flashlight Mod Builder

A self-contained Python desktop GUI that builds a Left 4 Dead 2 addon `.vpk`
customizing the player flashlight — **Brightness** (intensity), **Hue**
(color), **Width** (cone angle), and **Range** (reach) — with a live
approximate beam preview.

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
python3 l4d2_flashlight_vpk_gui.py --nogui --brightness 90 --width 80 --range 70 --hue 120 --out ./build
```

## What it generates

One `flashlight_custom.vpk` (plus a `flashlight_custom_README.txt` beside it)
containing:

| File in VPK | Purpose |
|---|---|
| `materials/effects/flashlight001.vtf` | Spotlight cookie (a real VTF v7.2 written in pure Python) replacing the stock beam texture, with **width, brightness, and hue baked in** — Width grows the lit shape from a soft disc to a full-bleed rectangle filling the whole projection cone; Brightness sets its intensity. Applies automatically while the addon is enabled and works **even where cvars are cheat-gated**. |
| `cfg/flashlight_bright.cfg` | The cvars that change the cone *itself*: `r_flashlightfov` (Width → up to 90°) and `r_flashlightfar` (Range → up to 4000 units), plus the brightness/attenuation set. These are `FCVAR_CHEAT`, so they only apply where `sv_cheats` is on. |
| `addoninfo.txt`, `readme.txt` | Addon metadata + install/usage notes. |

Install by copying the `.vpk` into
`Steam/steamapps/common/Left 4 Dead 2/left4dead2/addons/`.

The VPK is packed by a built-in pure-Python VPK v1 packer (no `vpk.exe`, no
game files needed) and is re-parsed and CRC-verified after writing before
success is reported.

## Making it genuinely longer / wider

The beam's cone angle (`r_flashlightfov`, stock 53°) and reach
(`r_flashlightfar`, stock 750) are the only true "wider"/"longer" levers, and
both are `FCVAR_CHEAT`. To use them, load a map **from the console** so cheats
turn on, then apply the cfg:

```
map c1m1_hotel          # use the chapter's own name; lobby/menu keeps cheats OFF
exec flashlight_bright.cfg
```

Many custom/workshop maps auto-enable cheats when loaded via the console. Where
cheats stay off, the beam width is whatever the **cookie texture** fills — which
the Width slider drives up to the full (fixed) engine cone, no cheats needed.

## Notes

* Client-side and local-only; other players don't see it, some servers
  disable addons, and the cone-size cvars are cheat-flagged — which is why
  Width and Brightness are also baked into the beam texture, the one lever
  that works everywhere.
* The preview canvas is an approximation for tuning, not a game-accurate
  render.
* Brightness/near-plane clamps are enforced so you can't generate a config
  that whites out nearby walls — the tuning constants (and comments explaining
  them) are at the top of `l4d2_flashlight_vpk_gui.py`.
