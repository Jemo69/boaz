# Boaz — headless desktop tester

Mishpat owns the browser; **boaz owns the desktop.** Launch Electron/GPUI/Qt/any
X11 app on an invisible DISPLAY, drive it like an agent, read screenshots with
vision. Your `:0` stays yours — boaz never touches it.

```bash
boaz launch "./my-app --no-sandbox" --name vp    # headless :99+, polls for a mapped window
boaz screenshot --name vp                         # -> shots/vp-001.png (CWD-relative)
boaz run tests/flow.json --name vp                # xdotool script, PASS/FAIL + exit codes
boaz record --name vp --out shots/tour.mp4        # ffmpeg x11grab, frame-verified
boaz log --name vp                                # app stdout when no window appears
boaz show --name vp                               # relaunch VISIBLE — only when READY_FOR_HUMAN
boaz kill --name vp        # verified PIDs only; --all / --sweep for orphans
boaz ps                    # live sessions
boaz doctor                # readiness check for this machine
```

Sessions are isolated: each `--name` gets its own DISPLAY (`:99`–`:110`,
probed via TCP + filesystem + abstract sockets) and its own state file, so
parallel agents don't collide. `kill` verifies `/proc` cmdlines before
signalling and refuses to kill strangers; `--sweep` reaps orphan Xvfb servers.

Run scripts look like `tests/flow.json`:

```json
[{"action": "wait", "seconds": 0.5}, {"action": "key", "keys": "F5"},
 {"action": "type", "text": "hello"}, {"action": "click", "x": 640, "y": 400},
 {"action": "screenshot"}]
```

Notes: relaunching a `--name` kills its old session first; screenshots land in
`shots/` under your current directory; captures are content-gated — a blank
frame exits non-zero instead of lying to the agent.

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
uv run pytest tests/ -q   # 5 live-session tests: cycle, run PASS/FAIL, record frames, isolation, kill-safety
```
