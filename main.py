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
    FIRST_DISPLAY, HEIGHT, LAST_DISPLAY, WIDTH, XVFB_ARGV0,
    alloc_display, capture, confirm_display, display_env, display_taken, drop_session, load,
    need_session, proc_cmdline, proc_environ, proc_stat_starttime, release_display, safe_stem, save, session_path, sessions_dir, verified_kill,
    wait_for_window, need_bins, xvfb_bin,
)

sys.stdout.reconfigure(line_buffering=True)
app = typer.Typer(help="Boaz — headless desktop tester. Agent sees the app, you don't (until ready).")


def _shots_dir() -> Path:
    d = Path("shots")  # CWD-relative by design: agents run boaz from the project root
    try:
        d.mkdir(exist_ok=True)
    except OSError as e:
        raise SystemExit(f"cannot write shots dir ./shots: {e}")
    if not d.is_dir():
        raise SystemExit("./shots exists as a file — remove it or run boaz from another directory")
    return d


def _require_linux(cmd: str) -> None:
    if platform.system() != "Linux":
        raise SystemExit(
            f"`boaz {cmd}` needs Linux+Xvfb (this is {platform.system()}). "
            "WSL2 counts as Linux and works. macOS/Windows: not implemented — "
            "boaz refuses loudly instead of faking it.")


def _next_shot(path_stem: str, suffix: str = "") -> Path:
    """Next free shot path by max numeric suffix, never reusing a deleted index."""
    import re as _re
    nums = [int(m.group(1)) for f in _shots_dir().glob(f"{path_stem}{suffix}-*.png")
            if (m := _re.search(r"-(\d+)\.png$", f.name))]
    return _shots_dir() / f"{path_stem}{suffix}-{max(nums, default=0) + 1:03d}.png"


def _shot(name: str, out: str | None = None) -> str:
    st = need_session(name)
    stem = safe_stem(name)
    if out is None:
        out = str(_next_shot(stem))
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
    for dim, val in (("width", width), ("height", height)):
        if not 320 <= val <= 7680:
            raise SystemExit(f"bad {dim}={val}: want 320-7680 (a real screen size)")
    kept = _kill([name], sweep=False, force=False, quiet=True)
    if name in kept:
        raise SystemExit(f"session '{name}' kept a live unverified process — refusing to overwrite it. Use --force with kill first.")
    num = alloc_display()
    display = f":{num}"
    xvfb_path = xvfb_bin()
    xvfb_env = dict(display_env(display), BOAZ_POOL=str(sessions_dir()))
    try:
        xvfb = subprocess.Popen(
            [XVFB_ARGV0, display, "-screen", "0", f"{width}x{height}x24"],
            executable=xvfb_path, env=xvfb_env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Wait for the server to actually answer before confirming + spawning.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if xvfb.poll() is not None:
                break
            if display_taken(num):
                break
            time.sleep(0.2)
        if xvfb.poll() is not None or not display_taken(num):
            try:
                xvfb.kill()
            except OSError:
                pass
            raise SystemExit(f"Xvfb never came up on {display}. See `boaz doctor`.")
    except BaseException:
        release_display(num)
        raise
    confirm_display(num)
    log = Path(f"/tmp/boaz-{safe_stem(name)}.log")
    try:
        lf = log.open("wb")
    except OSError as e:
        try:
            xvfb.kill()
        except OSError:
            pass
        release_display(num)
        raise SystemExit(f"cannot open log {log}: {e}")
    try:
        with lf:
            proc = subprocess.Popen(cmd, shell=True, env=display_env(display),
                                    stdout=lf, stderr=subprocess.STDOUT,
                                    start_new_session=True)  # own process group: kill takes children too
        from desktop import boot_id as _boot
        save(name, {"xvfb": xvfb.pid, "xvfb_starttime": proc_stat_starttime(xvfb.pid),
                    "app": proc.pid, "app_starttime": proc_stat_starttime(proc.pid),
                    "boot_id": _boot(),
                    "cmd": cmd, "display": display, "name": name})
    except BaseException:
        try:
            xvfb.kill()
        except OSError:
            pass
        drop_session(name)
        raise
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
    st = need_session(name)
    wid = None
    stem = safe_stem(name)
    if window:
        need_bins("xdotool")
        r = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", ".*"], timeout=10,
                           env=display_env(st["display"]), capture_output=True, text=True)
        ids = r.stdout.split()
        wid = ids[0] if ids else None
    if wid is None and window:
        raise SystemExit(f"No mapped window on {st['display']} yet.")
    rprint(_shot(name, out) if wid is None
           else str(capture(st["display"], Path(out) if out else
                            _next_shot(stem, "-win"), wid)))


@app.command()
def run(
    script: str = typer.Argument(..., help="JSON steps file, e.g. tests/flow.json"),
    name: str = typer.Option("app", help="Session name"),
) -> None:
    """Drive the headless app (xdotool) + screenshot every step. Prints PASS/FAIL."""
    ok = _run(script, name)
    raise typer.Exit(0 if ok else 1)


def _run(script: str, name: str) -> bool:
    _require_linux("run")
    need_bins("xdotool")
    st = need_session(name)
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
                secs = s.get("seconds", 1)
                try:
                    secs = float(secs)
                except (TypeError, ValueError):
                    raise ValueError(f"wait needs numeric seconds, got {s.get('seconds')!r}")
                if not 0 <= secs <= 3600:
                    raise ValueError(f"wait needs seconds 0-3600, got {secs}")
                time.sleep(secs)
            elif a == "key":
                if not isinstance(s.get("keys"), str):
                    raise ValueError("key needs a string 'keys'")
                subprocess.run(["xdotool", "key", s["keys"]], env=env, check=True, timeout=10)
            elif a == "type":
                if not isinstance(s.get("text"), str):
                    raise ValueError("type needs a string 'text'")
                subprocess.run(["xdotool", "type", s["text"]], env=env, check=True, timeout=10)
            elif a == "click":
                try:
                    cx, cy = int(s["x"]), int(s["y"])
                except (TypeError, ValueError):
                    raise ValueError(f"click needs integer x/y, got {s.get('x')!r},{s.get('y')!r}")
                subprocess.run(["xdotool", "mousemove", str(cx), str(cy), "click", "1"],
                               env=env, check=True, timeout=10)
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
        except subprocess.TimeoutExpired:
            rprint(f"[bold red]  [{i}] {a} FAIL: xdotool timed out[/]")
            ok = False
            break
        try:
            shot = _shot(name)
        except SystemExit as e:
            rprint(f"[bold red]  [{i}] {a} FAIL: capture: {e}[/]")
            ok = False
            break
        if i == 0:
            rprint(f"[dim]first frame: {shot}[/]")
    rprint("[bold green]PASS[/]" if ok else "[bold red]FAIL[/]")
    return ok


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
    st = need_session(name)
    out = out or str(_shots_dir() / f"{safe_stem(name)}.mp4")
    if Path(out).is_dir():
        raise SystemExit(f"cannot write {out}: is a directory")
    if not (0 < seconds <= 3600):
        raise SystemExit(f"bad --seconds={seconds}: want 0-3600")
    try:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SystemExit(f"cannot write {out}: {e}")
    tmp_out = out + '.tmp.mp4'
    if Path(tmp_out).is_dir():
        raise SystemExit(f"cannot write {tmp_out}: is a directory")
    try:
        Path(tmp_out).unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        raise SystemExit(f"cannot write {tmp_out}: {e}")
    errlog = Path(f"/tmp/boaz-{safe_stem(name)}-ff.log")
    try:
        ef = errlog.open("wb")
    except OSError as e:
        raise SystemExit(f"cannot open ffmpeg log {errlog}: {e}")
    with ef:
        ff = subprocess.Popen(
            ["ffmpeg", "-y", "-f", "x11grab", "-draw_mouse", "0", "-r", "10", "-i", st["display"],
             "-vcodec", "libx264", "-preset", "ultrafast", tmp_out],
            stdout=subprocess.DEVNULL, stderr=ef)
        script_ok = True
        try:
            if script:
                script_ok = _run(script, name)
            else:
                time.sleep(seconds)
                rprint(f"[dim]static capture {seconds}s[/]")
        finally:
            ff.terminate()
            try:
                ff.wait(timeout=10)
            except subprocess.TimeoutExpired:
                ff.kill()
                ff.wait()
        if script and not script_ok:
            try:
                Path(tmp_out).unlink()
            except OSError:
                pass
            raise SystemExit(f"record aborted: script {script} FAILED, no MP4 verified")
    frames = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", tmp_out],
                            capture_output=True, text=True, timeout=30)
    n = frames.stdout.strip()
    def _fail(msg: str) -> None:
        # Only tmp_out is ever disowned: out may be the user's pre-existing file.
        try:
            Path(tmp_out).unlink()
        except OSError:
            pass
        tail = errlog.read_text(errors="replace")[-500:] if errlog.exists() else ""
        raise SystemExit(f"{msg}: {out}\nffmpeg: {tail.strip()[-300:]}")
    if not n or n == "0":
        _fail("record produced no frames")
    # Content gate: pull one frame, refuse flat/black recordings.
    fr = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", tmp_out,
                           "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                          capture_output=True)
    try:
        from PIL import Image, ImageStat
        import io as _io
        flat = (ImageStat.Stat(Image.open(_io.BytesIO(fr.stdout)).convert("L")).stddev[0] or 0) < 1.5
    except Exception:
        flat = not fr.stdout
    if flat:
        _fail("record is blank (no pixel variance) — was the app running?")
    os.replace(tmp_out, out)
    rprint(f"[bold green]saved[/] {out} ({n} frames)")


@app.command()
def show(name: str = typer.Option("app", help="Session name")) -> None:
    """Relaunch the same command visibly for the human. Only when READY_FOR_HUMAN."""
    _require_linux("show")
    st = need_session(name)
    if not _cmd(st):
        raise SystemExit(f"session '{name}' has no command recorded — cannot show")
    rprint(f"[yellow]Re-launching visibly for YOU to test:[/] {_cmd(st)}")
    rprint("[dim]Headless session keeps running untouched.[/]")
    subprocess.Popen(_cmd(st), shell=True)


@app.command()
def log(name: str = typer.Option("app", help="Session name"),
        lines: int = typer.Option(30, help="Tail lines")) -> None:
    """Show the headless app's stdout/stderr (why no window yet?)."""
    _require_linux("log")
    p = Path(f"/tmp/boaz-{safe_stem(name)}.log")
    if not p.is_file():
        raise SystemExit(f"No log for '{name}'.")
    try:
        text = p.read_text(errors="replace")
    except OSError as e:
        raise SystemExit(f"cannot read log {p}: {e}")
    rprint("".join(text.splitlines(True)[-lines:]))


@app.command()
def kill(name: str = typer.Option("app", help="Session name (or --all)"),
         all: bool = typer.Option(False, help="Kill every session"),
         sweep: bool = typer.Option(False, help="Also reap orphan boaz Xvfb servers (argv[0]-marked only)"),
         force: bool = typer.Option(False, help="Drop the session without signalling (stale/PID-recycled state)"),
         quiet: bool = typer.Option(False, help="No output")) -> None:
    """Kill headless session(s). Verifies cmdline+starttime — never kills strangers."""
    _require_linux("kill")
    kept = _kill([p.stem for p in sessions_dir().glob("*.json")] if all else [name], sweep, force, quiet)
    if kept:
        raise typer.Exit(3)


def _cmd(st: dict) -> str:
    cmd = st.get('cmd', '')
    return cmd if isinstance(cmd, str) else ''


def _app_is_ours(pid: int, st: dict) -> bool:
    """Identity for apps: recorded starttime must match (pid reuse gives a
    new starttime, argv rewrites don't matter). Falls back to the cmd
    substring only for sessions predating starttime recording."""
    if not proc_cmdline(pid):
        return False
    recorded = st.get("app_starttime", "")
    if recorded:
        return proc_stat_starttime(pid) == recorded
    cmd = _cmd(st)
    return bool(cmd and cmd[:40] in proc_cmdline(pid))


def _coerce_pid(v) -> int | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _coerce_pids(st: dict) -> tuple[int | None, int | None]:
    """Valid JSON with wrong types must not traceback the recovery commands.
    Each field coerces independently: a corrupt xvfb must never discard a
    verifiable live app (D1)."""
    return _coerce_pid(st.get("app")), _coerce_pid(st.get("xvfb"))


def _kill(names: list[str], sweep: bool, force: bool, quiet: bool) -> list[str]:
    """Kill sessions. Returns names KEPT because a live process couldn't be verified."""
    from desktop import boot_id as _boot
    kept: list[str] = []
    for n in names:
        st = load(n)
        if not st:
            pth = Path(session_path(n))
            if pth.exists():
                try:
                    pth.unlink()
                    if not quiet:
                        rprint(f"[dim]{n}: unreadable session file cleared.[/]")
                except OSError:
                    pass
            continue
        app_pid, xvfb_pid = _coerce_pids(st)
        if app_pid is None and xvfb_pid is None:
            # Wholly unverifiable (non-int garbage or no pids at all): ints
            # would have coerced, so nothing here can reference a live process.
            # Drop unconditionally — keeping it only deadlocks the pool.
            try:
                Path(session_path(n)).unlink()
                if not quiet:
                    rprint(f"[dim]{n}: corrupt session file cleared (nothing verifiable).[/]")
            except OSError:
                pass
            continue
        if st.get("boot_id") not in (None, "", _boot()):
            drop_session(n)
            if not quiet:
                rprint(f"[dim]{n}: stale (older boot) — session dropped.[/]")
            continue
        if force:
            drop_session(n)
            if not quiet:
                rprint(f"[dim]{n}: force-dropped without signalling (PIDs left alone).[/]")
            continue
        # D6: handle each pid independently; drop only when nothing alive survives.
        leftovers: list[str] = []
        if app_pid and proc_cmdline(app_pid):
            if _app_is_ours(app_pid, st):
                if verified_kill(app_pid, _cmd(st)[:40], st.get("app_starttime", "")):
                    from desktop import kill_group as _kg
                    for survivor in _kg(app_pid):
                        leftovers.append(f"group member pid={survivor}")
                if proc_cmdline(app_pid):
                    leftovers.append(f"app pid={app_pid}")
            else:
                leftovers.append(f"unverified app pid={app_pid}")
                if not quiet:
                    rprint(f"[dim]{n}:app alive but identity unverified (PID reuse?) — session kept. Use --force to drop.[/]")
        if xvfb_pid and proc_cmdline(xvfb_pid):
            if verified_kill(xvfb_pid, XVFB_ARGV0, st.get("xvfb_starttime", "")):
                pass
            if proc_cmdline(xvfb_pid):
                leftovers.append(f"Xvfb pid={xvfb_pid}")
        if leftovers:
            kept.append(n)
            if not quiet:
                rprint(f"[dim]{n}: kept ({', '.join(leftovers)} still alive).[/]")
            continue
        drop_session(n)
    if sweep:
        # Reclaim dead X-lock files too (stale SIGKILL residue with no live pid).
        for lk in Path("/tmp").glob(".X*-lock"):
            try:
                pid_txt = lk.read_text().strip().split()[0]
                int(pid_txt)
                if not Path(f"/proc/{pid_txt}").exists():
                    lk.unlink()
            except (OSError, IndexError, ValueError):
                pass
        # Reclaim only STALE reservations (60s+): a concurrent launch may hold a fresh one.
        now = time.time()
        for r in sessions_dir().glob(".reserve-*"):
            try:
                if now - r.stat().st_mtime > 60:
                    r.unlink()
            except OSError:
                pass
        claimed: set[int] = set()
        for q in sessions_dir().glob("*.json"):
            try:
                st0 = json.loads(q.read_text())
                if not isinstance(st0, dict):
                    continue
                a, x = _coerce_pids(st0)
                if a:
                    claimed.add(a)
                if x:
                    claimed.add(x)
            except (ValueError, OSError):
                continue
        for pid in _x11_server_pids():
            if pid in claimed:
                continue
            # Cross-pool safety: only reap servers from THIS pool. Another
            # agent's boaz (different BOAZ_SESSION_DIR) is never our orphan.
            penv = proc_environ(pid)
            owner = penv.get("BOAZ_POOL", "") or penv.get("BOAZ_SESSION_DIR", "")
            if not owner or owner.rstrip("/") != str(sessions_dir()).rstrip("/"):
                continue
            st_match = proc_stat_starttime(pid)
            if verified_kill(pid, XVFB_ARGV0, st_match) and not quiet:
                rprint(f"[dim]swept orphan boaz Xvfb pid={pid}.[/]")
    if not quiet:
        live = [q.stem for q in sessions_dir().glob("*.json")]
        rprint(f"[dim]boaz stopped requested sessions. Still tracked: {', '.join(live) or 'none'}.[/]")
    return kept


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
    _require_linux("ps")
    found = False
    for p in sorted(sessions_dir().glob("*.json")):
        try:
            st = json.loads(p.read_text())
            if not isinstance(st, dict):
                raise ValueError("not a dict")
        except (ValueError, OSError):
            rprint(f"[dim]⚠ {p.stem}: unreadable session file — `boaz kill --name {p.stem}` to clear.[/]")
            continue
        app_pid, _x = _coerce_pids(st)
        if st.get("app") is not None and app_pid is None:
            rprint(f"[dim]⚠ {p.stem}: wrong-typed pids — `boaz kill --name {p.stem}` to clear.[/]")
            found = True
            continue
        alive = bool(app_pid and proc_cmdline(app_pid)
                     and (not st.get("app_starttime") or proc_stat_starttime(app_pid) == st.get("app_starttime")))
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
