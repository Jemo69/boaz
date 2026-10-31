"""Boaz: headless desktop tester for AI agents.

Mishpat owns the browser; boaz owns the desktop. The agent drives an
invisible app on its own DISPLAY (:99+), reads screenshots with vision,
and the human only looks when the agent verdict is READY_FOR_HUMAN.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import typer
from rich import print as rprint

from desktop import (
    FIRST_DISPLAY, HEIGHT, LAST_DISPLAY, SESSIONS, WIDTH, XVFB_ARGV0,
    alloc_display, capture, display_env, drop_session, load,
    proc_cmdline, proc_stat_starttime, save, verified_kill,
    wait_for_window, which_or_hint, need_bins, xvfb_bin,
)

sys.stdout.reconfigure(line_buffering=True)
app = typer.Typer(help="Boaz — headless desktop tester. Agent sees the app, you don't (until ready).")


def _shots_dir() -> Path:
    d = Path("shots")  # CWD-relative by design: agents run boaz from the project root
    d.mkdir(exist_ok=True)
    return d


def _require_linux(cmd: str) -> None:
    if platform.system() != "Linux":
        raise SystemExit(
            f"`boaz {cmd}` needs Linux+Xvfb (this is {platform.system()}). "
            "WSL2 counts as Linux and works. macOS/Windows: not implemented — "
            "boaz refuses loudly instead of faking it.")


def _shot(name: str, out: str | None = None) -> str:
    st = load(name)
    if not st:
        raise SystemExit(f"No session '{name}'. `boaz launch --name {name} \"<cmd>\"` first.")
    if out is None:
        n = len(list(_shots_dir().glob(f"{name}-*.png")))
        out = str(_shots_dir() / f"{name}-{n:03d}.png")
    return str(capture(st["display"], Path(out)))


@app.command()
def launch(
    cmd: str = typer.Argument(..., help="App command, e.g. './my-electron --no-sandbox'"),
    name: str = typer.Option("app", help="Session name. NOTE: relaunching a name kills its old session first."),
    wait: float = typer.Option(30.0, help="Seconds to wait for a mapped window"),
    width: int = typer.Option(WIDTH, help="Headless screen width"),
    height: int = typer.Option(HEIGHT, help="Headless screen height"),
) -> None:
    """Launch an app headless on its own DISPLAY. Never touches your :0."""
    _require_linux("launch")
    need_bins("xdotool")
    _kill([name], sweep=False, quiet=True)
    num = alloc_display()
    display = f":{num}"
    xvfb_path = xvfb_bin()
    xvfb = subprocess.Popen(
        [XVFB_ARGV0, display, "-screen", "0", f"{width}x{height}x24"],
        executable=xvfb_path,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    if xvfb.poll() is not None:
        raise SystemExit(f"Xvfb died on startup for {display}. See `boaz doctor`.")
    log = Path(f"/tmp/boaz-{name}.log")
    with log.open("wb") as lf:
        proc = subprocess.Popen(cmd, shell=True, env=display_env(display),
                                stdout=lf, stderr=subprocess.STDOUT,
                                start_new_session=True)  # own process group: kill takes children too
    save(name, {"xvfb": xvfb.pid, "app": proc.pid,
                "app_starttime": proc_stat_starttime(proc.pid),
                "cmd": cmd, "display": display, "name": name})
    try:
        wid = wait_for_window(display, timeout=wait)
    except SystemExit as e:
        rprint(f"[yellow]launched but no window yet:[/] {e}")
        raise typer.Exit(2)
    if xvfb.poll() is not None:
        raise SystemExit(f"Xvfb died under session '{name}'.")
    shot = _shot(name)
    rprint(f"[bold green]Boaz running headless[/] session={name} display={display} pid={proc.pid} window={wid}")
    rprint(f"[dim]You see nothing. Agent reads: {shot}  Log: {log}[/]")


@app.command()
def screenshot(
    out: str = typer.Option(None, help="Output path (default shots/<name>-NNN.png, CWD-relative)"),
    name: str = typer.Option("app", help="Session name"),
    window: bool = typer.Option(False, help="Grab the app window only (xwd) instead of root"),
) -> None:
    """Capture the headless display to PNG. Agent reads this with vision."""
    _require_linux("screenshot")
    st = load(name)
    if not st:
        raise SystemExit(f"No session '{name}'.")
    wid = None
    if window:
        r = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", ".*"],
                           env=display_env(st["display"]), capture_output=True, text=True)
        ids = r.stdout.split()
        wid = ids[0] if ids else None
    if wid is None and window:
        raise SystemExit(f"No mapped window on {st['display']} yet.")
    rprint(_shot(name, out) if wid is None
           else str(capture(st["display"], Path(out) if out else
                            _shots_dir() / f"{name}-win-{len(list(_shots_dir().glob(f'{name}-win-*.png'))):03d}.png", wid)))


@app.command()
def run(
    script: str = typer.Argument(..., help="JSON steps file, e.g. tests/flow.json"),
    name: str = typer.Option("app", help="Session name"),
) -> None:
    """Drive the headless app (xdotool) + screenshot every step. Prints PASS/FAIL."""
    _run(script, name)


def _run(script: str, name: str) -> None:
    _require_linux("run")
    st = load(name)
    if not st:
        raise SystemExit(f"No session '{name}'.")
    try:
        steps = json.loads(Path(script).read_text())
    except OSError as e:
        raise SystemExit(f"bad script {script}: {e}")
    except ValueError as e:
        raise SystemExit(f"bad script {script}: invalid JSON: {e}")
    if not isinstance(steps, list):
        raise SystemExit(f"bad script {script}: top level must be a JSON list of steps")
    env = display_env(st["display"])
    ok = True
    for i, s in enumerate(steps):
        a = s.get("action") if isinstance(s, dict) else None
        try:
            if a == "wait":
                time.sleep(float(s.get("seconds", 1)))
            elif a == "key":
                subprocess.run(["xdotool", "key", s["keys"]], env=env, check=True)
            elif a == "type":
                subprocess.run(["xdotool", "type", s["text"]], env=env, check=True)
            elif a == "click":
                subprocess.run(["xdotool", "mousemove", str(s["x"]), str(s["y"]), "click", "1"],
                               env=env, check=True)
            elif a == "screenshot":
                pass  # every step screenshots anyway
            else:
                raise ValueError(f"unknown action {a!r} (want wait|key|type|click|screenshot)")
            rprint(f"  [{i}] {a} ok")
        except (KeyError, TypeError, ValueError) as e:
            rprint(f"[bold red]  [{i}] {a} FAIL: step needs keys {{action,seconds|keys|text|x,y}}: {e}[/]")
            ok = False
            break
        except subprocess.CalledProcessError as e:
            rprint(f"[bold red]  [{i}] {a} FAIL: xdotool exit {e.returncode}[/]")
            ok = False
            break
        _shot(name)
    rprint("[bold green]PASS[/]" if ok else "[bold red]FAIL[/]")
    raise typer.Exit(0 if ok else 1)


@app.command()
def record(
    out: str = typer.Option(None, help="MP4 output (default shots/<name>.mp4)"),
    name: str = typer.Option("app", help="Session name"),
    script: str = typer.Option(None, help="Optional JSON script to drive while recording"),
    seconds: float = typer.Option(5.0, help="Static capture length when no script"),
) -> None:
    """Record the headless display via ffmpeg x11grab. No window on your screen."""
    _require_linux("record")
    need_bins("ffmpeg", "ffprobe")
    st = load(name)
    if not st:
        raise SystemExit(f"No session '{name}'.")
    out = out or str(_shots_dir() / f"{name}.mp4")
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    errlog = Path(f"/tmp/boaz-{name}-ff.log")
    with errlog.open("wb") as ef:
        ff = subprocess.Popen(
            ["ffmpeg", "-y", "-f", "x11grab", "-r", "10", "-i", st["display"],
             "-vcodec", "libx264", "-preset", "ultrafast", out],
            stdout=subprocess.DEVNULL, stderr=ef)
        try:
            if script:
                _run(script, name)
            else:
                time.sleep(seconds)
                rprint(f"[dim]static capture {seconds}s[/]")
        finally:
            ff.terminate()
            ff.wait()
    frames = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", out],
                            capture_output=True, text=True)
    n = frames.stdout.strip()
    if not n or n == "0":
        tail = errlog.read_text(errors="replace")[-500:] if errlog.exists() else ""
        raise SystemExit(f"record produced no frames: {out}\nffmpeg: {tail.strip()[-300:]}")
    rprint(f"[bold green]saved[/] {out} ({n} frames)")


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
         sweep: bool = typer.Option(False, help="Also reap orphan boaz Xvfb servers (argv[0]-marked only)"),
         quiet: bool = typer.Option(False, help="No output")) -> None:
    """Kill headless session(s). Verifies cmdline+starttime — never kills strangers."""
    _kill([p.stem for p in SESSIONS.glob("*.json")] if all else [name], sweep, quiet)


def _kill(names: list[str], sweep: bool, quiet: bool) -> None:
    for n in names:
        st = load(n)
        if not st:
            continue
        app_ok = verified_kill(int(st["app"]), (st.get("cmd") or "")[:40],
                               st.get("app_starttime", "")) if st.get("app") else True
        if st.get("app") and not app_ok:
            if not quiet:
                rprint(f"[dim]{n}:app already gone or PID recycled — session kept so nothing is lost.[/]")
            continue  # spared process stays tracked; never forget a live one
        if st.get("app"):
            try:
                os.killpg(int(st["app"]), signal.SIGTERM)  # process group: reap shell children too
            except (ProcessLookupError, PermissionError, OSError):
                pass
        if st.get("xvfb"):
            verified_kill(int(st["xvfb"]), XVFB_ARGV0)
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
                continue
        for pid in _x11_server_pids():
            if pid not in claimed and verified_kill(pid, XVFB_ARGV0) and not quiet:
                rprint(f"[dim]swept orphan boaz Xvfb pid={pid}.[/]")
    if not quiet:
        live = [p.stem for p in SESSIONS.glob("*.json")]
        rprint(f"[dim]boaz stopped requested sessions. Still tracked: {', '.join(live) or 'none'}.[/]")


def _x11_server_pids() -> list[int]:
    """PIDs of OUR headless servers: argv[0] is boaz-Xvfb, display :99-:110."""
    import re as _re
    found: list[int] = []
    try:
        out = subprocess.run(["pgrep", "-a", "-f", XVFB_ARGV0 + r" :[0-9]+"],
                             capture_output=True, text=True).stdout
    except OSError:
        return found
    for line in out.splitlines():
        m = _re.match(r"(\d+)\s+" + XVFB_ARGV0 + r"\s+:(\d+)", line)
        if m and FIRST_DISPLAY <= int(m.group(2)) <= LAST_DISPLAY:
            found.append(int(m.group(1)))
    return found


@app.command()
def ps() -> None:
    """List live headless sessions (name, display, pid, cmd)."""
    found = False
    for p in sorted(SESSIONS.glob("*.json")):
        try:
            st = json.loads(p.read_text())
        except (ValueError, OSError):
            rprint(f"[dim]⚠ {p.stem}: unreadable session file — `boaz kill --name {p.stem}` to clear.[/]")
            continue
        alive = bool(proc_cmdline(int(st["app"]))) if st.get("app") else False
        rprint(f"{'🟢' if alive else '⚫'} {st.get('name')}  {st.get('display')}  pid={st.get('app')}  {st.get('cmd')}")
        found = True
    if not found:
        rprint("[dim]idle — no sessions.[/]")


@app.command()
def doctor() -> None:
    """Readiness check: what headless testing can do on THIS machine."""
    xlibs = str(Path.home() / ".local" / "bin" / "Xvfb")
    rows = [
        ("OS", platform.system() + (" (WSL2 — counts as Linux, Xvfb works)" if "microsoft" in platform.release().lower() else ""),
         "Linux+Xvfb is full support; macOS/Windows refused loudly"),
        ("Xvfb", shutil.which("Xvfb") or (xlibs if Path(xlibs).exists() else None), "sudo apt install xvfb"),
        ("xdotool", shutil.which("xdotool"), "sudo apt install xdotool"),
        ("xwininfo", shutil.which("xwininfo"), "sudo apt install x11-utils"),
        ("xwd", shutil.which("xwd"), "sudo apt install x11-apps"),
        ("ffmpeg", shutil.which("ffmpeg"), "sudo apt install ffmpeg"),
        ("ffprobe", shutil.which("ffprobe"), "ships with ffmpeg"),
        ("scrot", shutil.which("scrot"), "sudo apt install scrot"),
        ("convert", shutil.which("convert"), "sudo apt install imagemagick"),
        ("/dev/dri (GPU)", str(Path("/dev/dri/card0").exists()), "rootless sandboxes use Lavapipe software GL"),
        ("Lavapipe Vulkan", str(Path("/usr/share/vulkan/icd.d/lvp_icd.json").exists()), "mesa-vulkan-drivers"),
    ]
    for label, have, hint in rows:
        rprint(f"{'✅' if have else '❌'} {label}: {have if have else 'MISSING — ' + hint}")


if __name__ == "__main__":
    app()
