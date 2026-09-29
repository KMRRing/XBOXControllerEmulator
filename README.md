# XBOXControllerEmulator

A touch Xbox controller for a Surface. Two thumbsticks, a D-pad, the face and shoulder buttons and the
triggers are drawn, half-transparent, over whatever is on the screen; every game on the machine sees a
wired Xbox 360 controller.

Built on Basis (`BASIS.md`): one page, three ways in, one shared data folder.

    Start.bat                          a laptop with GitHub    newest release, data in and out
    Start-Offline.bat                  a laptop without        newest xboxcontrolleremulator-vN.zip in Downloads
    launcher.py in Pythonista          the phone               the page only — the pad itself is Windows

## How it works

Two things have to be true for a game to believe in a controller that is not there.

**Something has to be the controller.** That is the ViGEmBus driver (Virtual Gamepad Emulation Bus): a small
kernel driver by Nefarius that plugs virtual pads into Windows. Once it is installed, `app/vigem.py` asks it
for an Xbox 360 pad and feeds it the state — through the driver's own request codes, with no client DLL, so it
runs the same on an x64 machine and an ARM64 one. The pad shows up on XInput, which is what games read.

**Something has to draw on top and take the touches.** That is `app/overlay.py`: one borderless window over
the whole screen, never activated (touching it never takes focus from the game), layered with a colour key so
that everything not a control is simply not there — the game gets those touches — and driven by pointer
messages so that every finger is its own input. It re-asserts itself on top every two seconds.

`app/pad.py` is the part in between: what a finger on a stick means, how far the stick moves, how a finger
slides from A to B, which of eight directions a thumb on the D-pad points, what a tap, a two-finger tap and a
tap-then-drag on the trackpad ask of the mouse. Pure Python, tested anywhere.

The trackpad, the mouse buttons and the key buttons do not go through the controller: the overlay injects them
as mouse and keyboard input (`SendInput`) wherever the cursor is, which is how games whose own touch handling is
broken — Slay the Spire is one — are still played. Cursor moves are sent as absolute positions, so the pointer
speed is the pad's own and Windows' pointer acceleration does not double it. When the cursor happens to be over
one of the pad's own controls, the overlay makes itself click-through for the instant the click is injected.

Exclusive fullscreen is the one thing no overlay can draw over. Games go in borderless or windowed mode.

## The page

* **Show the pad / Take the pad down.** On the pad itself, *Hide* folds every control away so the game can be
  touched directly; *Pad* brings them back; holding it for a second closes the overlay.
* **Driver.** One click runs the bundled ViGEmBus installer, elevated. Windows asks; the installer wants Next
  and Install. Once.
* **What the game sees.** The live controller state, so a game's dead input is diagnosed here and not in the
  game.
* **Ghost mode.** *Idle controls* below 100 % fades every control to that much of the opacity until a finger is
  on it; at 0 % the pad is invisible until touched. Under the hood that is two windows: one carrying every
  control at the idle opacity (never fully transparent, because a transparent pixel cannot be touched), and one
  on top drawing only the controls in use at full opacity.
* **Trackpad, mouse buttons, keys.** Off by default; switched on per layout. Drag moves the cursor, tap clicks,
  two fingers tap for a right click, tap and go straight back down to drag, two fingers up and down scroll.
  LMB and RMB are for holding while the trackpad moves. K1–K4 each send one key, chosen from a list. *Pointer
  speed* and *Floating sticks* (a stick centres wherever the thumb lands) are settings.
* **Layout.** Drag controls, size them by number, switch any of them off. A snap grid (1, 2.5 or 5 % of the
  screen's short side) keeps positions on a lattice; arrow keys nudge by one step. With *Mirror* on, the right
  side follows the left: LT/RT, LB/RB, Back/Start and the stick clicks match in place and size; A B X Y move and
  size as one cluster with a *Spread* control, and their centre mirrors the left stick; the right stick mirrors
  the D-pad. Layouts are saved by name and one is active. Opacity is a slider.

## The files

    launcher.py        what every device runs. Self-updating. Do not edit.
    Start*.bat         the double-click files.
    app/
      app.py           the routes: status, driver, overlay, layouts, settings.
      pad.py           touches → XInput report. Layouts, hit-testing, the eight-way D-pad, the sticks.
      vigem.py         the virtual Xbox 360 pad, spoken to ViGEmBus directly.
      overlay.py       the transparent, never-focused, multi-touch window.
      driver/          ViGEmBus_1.22.0_x64_x86_arm64.exe — the installer the page runs.
      frame.py, datasync.py, appinfo.json, VERSION — the delivery system. Do not edit.
      templates/, static/   the page.
    test_system.py     the delivery system, end to end, against a stub GitHub (18 tests).
    test_app.py        the pad's rules, the driver's wire format, the routes (19 tests).

## The data

Under `<home>/data`, where home is `%LOCALAPPDATA%\XBOXControllerEmulator`:

    layouts/<id>.json   one file per layout — the sync is newest-wins per file, so layouts never clobber each
                        other. layouts/default.json, if present, overrides the built-in default.
                        A deleted layout is a tombstone {"deleted": true}: the sync carries files, never deletions.
    settings.json       the active layout, the opacity, the idle opacity, the pointer speed, floating sticks,
                        and the editor's snap and mirror.

Kilobytes, and nothing secret.

## What you need

Windows 10 or 11 with touch. Python 3 (`Start.bat` installs it if it is missing). The ViGEmBus driver, which the
page installs. A GitHub token on the online laptop, as for every Basis app; nothing on the offline one.

ViGEmBus 1.22.0 is the last release; its author retired it in 2023. It installs and runs on current Windows.

## Known limits

* Games with kernel anti-cheat may refuse a virtual pad. Single-player games are fine.
* No vibration: a Surface has nothing to vibrate.
* Touch has no feel. Fast action games will feel worse than a real pad; slower ones are where this shines.
* The phone can open the page, but the pad and the driver are Windows only.
