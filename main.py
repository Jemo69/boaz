"""Boaz: headless desktop tester for AI agents.

Mishpat owns the browser; boaz owns the desktop. The agent drives an
invisible app on its own DISPLAY (:99+), reads screenshots with vision,
and the human only looks when the agent verdict is READY_FOR_HUMAN.
"""
from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich import print as rprint

from desktop import (
    FIRST_DISPLAY, HEIGHT, LAST_DISPLAY, SESSIONS, WIDTH,
    alloc_display, capture, display_env, drop_session, load, proc_cmdline,
    save, verified_kill, wait_for_window, xvfb_bin,
)

sys.stdout.reconfigure(line_buffering=True)
app = typer.Typer(help="Boaz — headless desktop tester. Agent sees the app, you don't (until ready).")
SHOTS = Path("shots")
SHOTS.mkdir(exist_ok=True)


def _require_linux() -> None:
    if platform.system() != "Linux":
        raise SystemExit(
            f"boaz headless displays need Linux+Xvfb (this is {platform.system()}). "
            "macOS/Windows: launch visible and drive via `boaz run` (beta) — see README capability table.")


def _shot(name: str, out: str | None = None) -> str:
    st = load(name)
    if not st:
        raise SystemExit(f"No session '{name}'. `boaz launch --name {name} \"<cmd>\"` first.")
    n = len(list(SHOTS.glob(f"{name}-*.png")))
    path = capture(st["display"], SHOTS / f"{name}-{n:03d}.png" if out is None else Path(out))
    return str(path)


@app.command()
def launch(
    cmd: str = typer.Argument(..., help="App command, e.g. './my-electron --no-sandbox'"),
    name: str = typer.Option("app", help="Session name (isolated DISPLAY + state)"),
    wait: float = typer.Option(30.0, help="Seconds to wait for a mapped window"),
    width: int = typer.Option(WIDTH, help="Headless screen width"),
    height: int = typer.Option(HEIGHT, help="Headless screen height"),
) -> None:
    """Launch an app headless on its own DISPLAY. Never touches your :0."""
    _require_linux()
    if not shutil.which("xdotool"):
        raise SystemExit("Missing xdotool. Install: sudo apt install xdotool")
    _kill([name], sweep=False, quiet=True)
    num = alloc_display()
    display = f":{num}"
    xvfb = subprocess.Popen(
        [xvfb_bin(), display, "-screen", "0", f"{width}x{height}x24"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    log = Path(f"/tmp/boaz-{name}.log")
    with log.open("wb") as lf:
        proc = subprocess.Popen(cmd, shell=True, env=display_env(display),
                                stdout=lf, stderr=subprocess.STDOUT)
    save(name, {"xvfb": xvfb.pid, "app": proc.pid, "cmd": cmd,
                "display": display, "name": name})
    try:
        wid = wait_for_window(display, timeout=wait)
    except SystemExit as e:
        rprint(f"[yellow]launched but no window yet:[/] {e}")
        raise typer.Exit(2)
    shot = _shot(name)
    rprint(f"[bold green]Boaz running headless[/] session={name} display={display} pid={proc.pid} window={wid}")
    rprint(f"[dim]You see nothing. Agent reads: {shot}  Log: {log}[/]")


@app.command()
def screenshot(
    out: str = typer.Option(None, help="Output path (default shots/<name>-NNN.png)"),
    name: str = typer.Option("app", help="Session name"),
    window: bool = typer.Option(False, help="Grab the app window only (xwd) instead of root"),
) -> None:
    """Capture the headless display to PNG. Agent reads this with vision."""
    st = load(name)
    if not st:
        raise SystemExit(f"No session '{name}'.")
    wid = None
    if window:
        from desktop import display_env as _env
        import subprocess as _sp
        r = _sp.run(["xdotool", "search", "--onlyvisible", "--name", ".*"],
                    env=_env(st["display"]), capture_output=True, text=True)
        ids = [i for i in r.stdout.split() if i.strip()]
        wid = ids[0] if ids else None
    rprint(_shot(name, out) if wid is None else str(capture(st["display"],
          SHOTS / f"{name}-win-{len(list(SHOTS.glob(f'{name}-win-*.png'))):03d}.png" if out is None else Path(out), wid)))


@app.command()
def run(
    script: str = typer.Argument(..., help="JSON steps file"),
    name: str = typer.Option("app", help="Session name"),
) -> None:
    """Drive the headless app (xdotool) + screenshot every step. Prints PASS/FAIL."""
    _run(script, name)


def _run(script: str, name: str) -> None:
    st = load(name)
    if not st:
        raise SystemExit(f"No session '{name}'.")
    steps = json.loads(Path(script).read_text())
    display = st["display"]
    from desktop import display_env
    env = display_env(display)
    ok = True
    for i, s in enumerate(steps):
        a = s.get("action")
        try:
            if a == "wait":
                time.sleep(float(s.get("seconds", 1)))
            elif a == "key":
                subprocess.run(["xdotool", "key", s["keys"]], env=env, check=True)
            elif a == "type":
                subprocess.run(["xdotool", "type", s["text"]], env=env, check=True)
            elif a == "click":
                subprocess.run(["xdotool", "mousemove",
                                str(s["x"]), str(s["y"]), "click", "1"], env=env, check=True)
            elif a == "screenshot":
                pass  # every step screenshots anyway
            else:
                raise ValueError(f"unknown action {a!r}")
            rprint(f"  [{i}] {a} ok")
        except Exception as e:
            rprint(f"[bold red]  [{i}] {a} FAIL: {e}[/]")
            ok = False
            break
        _shot(name)
    rprint("[bold green]PASS[/]" if ok else "[bold red]FAIL[/]")
    raise typer.Exit(0 if ok else 1)


@app.command()
def record(
    out: str = typer.Option("shots/boaz.mp4", help="MP4 output"),
    name: str = typer.Option("app", help="Session name"),
    script: str = typer.Option(None, help="Optional JSON script to drive while recording"),
    seconds: float = typer.Option(5.0, help="Static capture length when no script"),
) -> None:
    """Record the headless display via ffmpeg x11grab. No window on your screen."""
    st = load(name)
    if not st:
        raise SystemExit(f"No session '{name}'.")
    ff = subprocess.Popen(
        ["ffmpeg", "-y", "-v", "error", "-f", "x11grab", "-r", "10",
         "-i", st["display"], "-vcodec", "libx264", "-preset", "ultrafast", out],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if script:
            _run(script, name)
        else:
            time.sleep(seconds)
            rprint(f"[dim]static capture {seconds}s[/]")
    finally:
        ff.terminate()
        ff.wait()
    frames = subprocess.run(["ffprobe", "-v", "error", "-count_frames",
                             "-select_streams", "v:0", "-show_entries",
                             "stream=nb_read_frames", "-of", "csv=p=0", out],
                            capture_output=True, text=True)
    if not frames.stdout.strip() or frames.stdout.strip() == "0":
        raise SystemExit(f"record produced no frames: {out}")
    rprint(f"[bold green]saved[/] {out} ({frames.stdout.strip()} frames)")


@app.command()
def show(name: str = typer.Option("app", help="Session name")) -> None:
    """Relaunch the same command visibly for the human. Only when READY_FOR_HUMAN."""
    st = load(name)
    if not st:
        raise SystemExit(f"No session '{name}'.")
    rprint(f"[yellow]Re-launching visibly for YOU to test:[/] {st['cmd']}")
    rprint("[dim]Headless session keeps running untouched.[/]")
    subprocess.Popen(st["cmd"], shell=True)


@app.command()
def log(name: str = typer.Option("app", help="Session name"),
        lines: int = typer.Option(30, help="Tail lines")) -> None:
    """Show the headless app's stdout/stderr (why no window yet?)."""
    p = Path(f"/tmp/boaz-{name}.log")
    if not p.exists():
        raise SystemExit(f"No log for '{name}'.")
    rprint("".join(p.read_text(errors="replace").splitlines(True)[-lines:]))


@app.command()
def kill(name: str = typer.Option("app", help="Session name (or --all)"),
         all: bool = typer.Option(False, help="Kill every session"),
         sweep: bool = typer.Option(False, help="Also kill orphan Xvfb servers no session claims"),
         quiet: bool = typer.Option(False, help="No output")) -> None:
    """Kill headless session(s). Verifies PIDs before signalling — never kills strangers."""
    _kill([p.stem for p in SESSIONS.glob("*.json")] if all else [name], sweep, quiet)


def _kill(names: list[str], sweep: bool, quiet: bool) -> None:
    for n in names:
        st = load(n)
        for key, expect in (("app", st.get("cmd", "")[:40]), ("xvfb", "Xvfb")):
            if st.get(key) and not verified_kill(int(st[key]), expect):
                if not quiet:
                    rprint(f"[dim]{n}:{key} already gone or PID reused — left alone.[/]")
        drop_session(n)
    if sweep:
        claimed: set[int] = set()
        for q in SESSIONS.glob("*.json"):
            try:
                st0 = json.loads(q.read_text())
                for k in ("xvfb", "app"):
                    if st0.get(k):
                        claimed.add(int(st0[k]))
            except (ValueError, OSError):
                pass
        for pid in _x11_server_pids():
            if pid not in claimed and verified_kill(pid, "Xvfb") and not quiet:
                rprint(f"[dim]swept orphan Xvfb pid={pid}.[/]")
    if not quiet:
        rprint(f"[dim]boaz stopped: {', '.join(names) or name}. Displays free.[/]")


import re as _re


def _x11_server_pids() -> list[int]:
    """PIDs that look like OUR headless X servers (Xvfb :99-:110 only)."""
    found: list[int] = []
    try:
        out = subprocess.run(["pgrep", "-a", "-f", "Xvfb :[0-9]+"],
                             capture_output=True, text=True).stdout
    except OSError:
        return found
    for line in out.splitlines():
        m = _re.match(r"(\d+)\s+\S*Xvfb\s+:(\d+)", line)
        if m and FIRST_DISPLAY <= int(m.group(2)) <= LAST_DISPLAY:
            found.append(int(m.group(1)))
    return found


@app.command()
def ps() -> None:
    """List live headless sessions (name, display, pid, cmd)."""
    found = False
    for p in sorted(SESSIONS.glob("*.json")):
        st = json.loads(p.read_text())
        alive = bool(proc_cmdline(int(st["app"]))) if st.get("app") else False
        rprint(f"{'🟢' if alive else '⚫'} {st.get('name')}  {st.get('display')}  pid={st.get('app')}  {st.get('cmd')}")
        found = True
    if not found:
        rprint("[dim]idle — no sessions.[/]")


@app.command()
def doctor() -> None:
    """Readiness check: what headless testing can do on THIS machine."""
    rows = [
        ("OS", platform.system(), "Linux+Xvfb is full support; else beta"),
        ("Xvfb", shutil.which("Xvfb") or str(Path.home() / ".local/bin/Xvfb") if Path.home().joinpath(".local/bin/Xvfb").exists() else None, "sudo apt install xvfb"),
        ("xdotool", shutil.which("xdotool"), "sudo apt install xdotool"),
        ("ffmpeg", shutil.which("ffmpeg"), "sudo apt install ffmpeg"),
        ("scrot", shutil.which("scrot"), "sudo apt install scrot"),
        ("ffprobe", shutil.which("ffprobe"), "ships with ffmpeg"),
        ("xwd/convert", shutil.which("xwd") and shutil.which("convert"), "x11-apps + imagemagick"),
        ("/dev/dri (GPU)", str(Path("/dev/dri/card0").exists()), "rootless sandboxes use Lavapipe software GL"),
        ("Lavapipe Vulkan", str(Path("/usr/share/vulkan/icd.d/lvp_icd.json").exists()), "mesa-vulkan-drivers"),
    ]
    for label, have, hint in rows:
        rprint(f"{'✅' if have else '❌'} {label}: {have or 'MISSING — ' + hint}")


if __name__ == "__main__":
    app()
