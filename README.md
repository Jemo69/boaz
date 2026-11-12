# Boaz — headless desktop tester

Mishpat owns the browser; **boaz owns the desktop.** Launch Electron/GPUI/Qt/any
X11 app on an invisible DISPLAY, drive it like an agent, read screenshots with
vision. Your `:0` stays yours — boaz never touches it.

## Install

```bash
sudo apt install -y xvfb xdotool ffmpeg scrot tesseract-ocr  # toolchain (boaz doctor verifies)
git clone https://github.com/Jemo69/boaz.git ~/projects/boaz
cd ~/projects/boaz && uv sync
# shim (~/.local/bin/boaz):
#   #!/bin/sh
#   exec uv --directory /home/jemo/projects/boaz run python /home/jemo/projects/boaz/main.py "$@"
boaz doctor   # must report X apps capturable
```

```bash
boaz launch "./my-app --no-sandbox" --name vp    # headless :99+, polls for a mapped window
boaz launch "./gl-app" --name gl --with-wm xfwm4   # GL/wgpu/SDL/GPUI: compositor so paint reaches the framebuffer
boaz wait --name vp --for painted --timeout 120     # mapped|painted, or --window title:VideoPsalm
boaz screenshot --name vp                         # -> shots/vp-001.png (CWD-relative)
boaz screenshot --name vp --window title:VideoPsalm  # target a window, not the root
boaz run tests/flow.json --name vp                # xdotool script + assert_changed/assert_text, PASS/FAIL + exit codes
boaz find --name vp --text "Start"                 # -> x y w h (tesseract on the live frame)
boaz record --name vp --out shots/tour.mp4        # ffmpeg x11grab, frame-verified, reports (N frames) M/S non-blank
boaz evidence --name vp                           # bundle shots+log+session into evidence/vp.tar.gz
boaz log --name vp                                # app stdout when no window appears
boaz show --name vp                               # relaunch VISIBLE — only when READY_FOR_HUMAN
boaz kill --name vp        # verified PIDs only; --force drops stale state; --all / --sweep for orphans (exit 3 if a live process is spared)
boaz ps --json             # live sessions (machine-readable)
boaz doctor                # toolchain + live render smoke-test (X apps / GL apps capturable)
```

Every command takes `--json`. `launch` takes `--env K=V` (repeatable),
`--cwd`, `--wait-window title:`, and echoes `wm=`/`env=` in its line.

Sessions are isolated: each `--name` gets its own DISPLAY (set BOAZ_SESSION_DIR to fully isolate parallel agents/CI) (`:99`–`:110`,
probed via TCP + filesystem + abstract sockets) and its own state file, so
parallel agents don't collide. `kill` verifies `/proc` cmdlines before
signalling and refuses to kill strangers; `--sweep` reaps orphan Xvfb servers.

Sessions die after 60s idle: the next `screenshot`/`run`/`record`/`find`/`wait`
on a session untouched for over `BOAZ_TTL` seconds (default 60, `0` disables)
kills its processes, drops the session, and tells you to relaunch. Active use
refreshes the clock, so long runs survive; abandoned sessions never leak.
`ps` shows `idle=Ns/TTLs` per session.

Run scripts look like `tests/flow.json` (key/type/click steps focus-sync first;
assert_changed proves the UI is alive without hand-rolled PIL):

```json
[{"action": "wait", "seconds": 0.5}, {"action": "key", "keys": "F5"},
 {"action": "type", "text": "hello"}, {"action": "click", "x": 640, "y": 400},
 {"action": "screenshot", "as": "a"},
 {"action": "wait", "seconds": 1},
 {"action": "assert_changed", "vs": "a", "region": [1100, 50, 260, 50], "min_delta": 0.01},
 {"action": "assert_text", "contains": "John 3:16"}]
```

Notes: relaunching a `--name` kills its old session first; screenshots land in
`shots/` under your current directory (also archived to `evidence/<name>/`);
captures are content-gated — a blank frame exits non-zero with the window
diagnostic + toolkit hint (Electron/Qt/Java/GL-compositor) and the exact
`--with-wm xfwm4` retry instead of lying to the agent.

## Multiscreen via session pairs

Apps that require multiscreen (control + projector) run as a session pair —
one session per display, never colliding (`tests/test_multiscreen.py` proves
it: different DISPLAYs, both capturable, driven independently):

```bash
boaz launch "<control-cmd>" --name ctrl --wait 30
boaz launch "<projector-cmd>" --name proj --wait 30
boaz screenshot --name ctrl; boaz screenshot --name proj
boaz run tests/flow.json --name ctrl   # projector keeps running untouched
boaz kill --all                        # reaps the whole pair
```

A single app with two windows stays on one screen: `screenshot --window title:`
+ region asserts.

## Requirements (Linux)

`Xvfb xdotool xwininfo xwd ffmpeg ffprobe scrot convert` — rootless fallback works without sudo:

```bash
apt-get download xvfb scrot x11-apps   # no root needed
dpkg -x xvfb_*.deb ~/.local/xvfb-root
ln -s ~/.local/xvfb-root/usr/bin/Xvfb ~/.local/bin/
```

## Capability table (honest)

| Platform | launch | screenshot | run/record | Notes |
|---|---|---|---|---|
| Linux + Xvfb | ✅ | ✅ ffmpeg→scrot | ✅ xdotool | Full support. Vulkan apps need WSI; use software GL. |
| Linux, no Xvfb | ❌ clear error | — | — | `boaz doctor` tells you the exact install |
| macOS | ❌ | ❌ | ❌ | No X server to isolate on — refuses loudly instead of faking it |
| Windows native | ❌ | ❌ | ❌ | Refuses loudly. WSL2 counts as Linux: Xvfb works, this suite is green there |

A documented gap beats a silent lie.

## Why not just run it visibly?

Because your mouse and the agent's script share one screen. Boaz puts the app on a
display you cannot see or click, so a stray keystroke can never trash a test run.
The agent reads every step as PNG; you look only at the final MP4 or `boaz show`.

## Tests

```bash
uv run pytest tests/ -q   # 24 live-session tests (isolation, kill safety, capture honesty, failure honesty, pool scoping, numbering, stale gates)
```
