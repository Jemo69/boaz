---
name: boaz
description: Headless desktop tester — launch Electron/GPUI/Qt apps on an invisible DISPLAY, screenshot, script clicks/keys, record MP4s. Use when the user says boaz, headless desktop, test a GUI app, Electron test, GPUI test, or invisible app.
---

# Boaz

Test desktop apps without ever seeing them. Boaz launches the app on its own
invisible DISPLAY (`:99`+), the agent drives it and reads screenshots, and the
human only looks when the agent verdict is READY_FOR_HUMAN. Project lives at
`/home/jemo/projects/boaz/`, runnable as `boaz` (shim in `~/.local/bin/`).

Mishpat owns the browser; boaz owns the desktop. Never confuse them.

## 1 - Orient

Confirm the CLI answers: `boaz --help`. Commands: `launch`, `screenshot`,
`run`, `record`, `show`, `log`, `kill`, `ps`, `doctor`.

Run `boaz doctor` first — it reports exactly what this machine can do
(Xvfb/xdotool/ffmpeg/scrot/GPU/Lavapipe) and the install for each gap.

Done when: help lists all nine commands and doctor is green on Linux+Xvfb.

## 2 - Headless test loop (default)

```bash
boaz launch "<cmd>" --name app          # polls for a mapped window (no blind sleep)
boaz screenshot --name app             # -> shots/app-NNN.png, open it to SEE
boaz run tests/flow.json --name app    # [{"action":"wait"|"key"|"type"|"click"|"screenshot", ...}]
boaz record --name app --out shots/t.mp4 [--script tests/flow.json]
boaz log --name app                    # app stdout when no window maps
```

Every step auto-screenshots to `shots/`. `run` prints PASS/FAIL and exits
0/1 — a FAIL names the step index and reason, never a stack trace.
`record` verifies frame count with ffprobe and refuses empty MP4s.

Done when: every claim about the app is backed by a fresh screenshot or
`run` PASS, and `ps` shows the session isolated on its own DISPLAY.

## 3 - Human handoff (visible, gated)

```bash
boaz show --name app   # relaunches the same cmd on YOUR display for manual testing
```

Call `show` ONLY on agent verdict READY_FOR_HUMAN. The headless session keeps
running untouched. Never let the human click around while the agent drives.

Done when: the MP4 exists, plays, and the human confirms on the visible copy.

## 4 - Hygiene (parallel-agent safe)

Sessions are per-`--name` (own DISPLAY + state file). `kill` verifies
`/proc` cmdlines before signalling — it would rather leave a process than
kill a stranger. `kill --all --sweep` reaps orphans. Never `kill -9` by hand;
never bind `:99` yourself.

`kill` verifies cmdline AND starttime before signalling and keeps — never
deletes — a session it refused to kill. `kill --all --sweep` reaps only
boaz-marked Xvfb servers (`argv[0]=boaz-Xvfb`); a foreign plain `Xvfb` always
survives. Blank captures exit non-zero with "display is blank".

Internal rule for contributors: never call a typer command from another
command — `typer.Option` defaults are truthy `OptionInfo` objects off-CLI
(this once caused `launch` to `kill --all` every sibling session). Always go
through the `_shot` / `_run` / `_kill` helpers.
