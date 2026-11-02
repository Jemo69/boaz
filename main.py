"""Boaz: headless desktop tester for AI agents.

Mishpat owns the browser; boaz owns the desktop. The agent drives an
invisible app on its own DISPLAY (:99+), reads screenshots with vision,
and the human only looks when the agent verdict is READY_FOR_HUMAN.
"""
from __future__ import annotations

import json
import os
import re
import platform
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import typer
from rich import print as rprint

from desktop import (
    FIRST_DISPLAY, HEIGHT, LAST_DISPLAY, WIDTH, XVFB_ARGV0,
    alloc_display, capture, confirm_display, display_env, display_taken, drop_session, load,
    need_session, proc_cmdline, proc_environ, proc_stat_starttime, release_display, safe_stem, save, session_path, sessions_dir, verified_kill, _read_json,
    wait_for_window, need_bins, xvfb_bin, GLOBAL_RESERVE,
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
    wait: float = typer.Option(30.0, help="Seconds to wait for a mapped window (alias: --timeout)"),
    timeout: float = typer.Option(None, help="Same as --wait. Overrides --wait when given."),
    cwd: str = typer.Option(None, help="Working dir for the app (default: your current $PWD)"),
    width: int = typer.Option(WIDTH, help="Headless screen width"),
    height: int = typer.Option(HEIGHT, help="Headless screen height"),
) -> None:
    """Launch an app headless on its own DISPLAY. Never touches your :0."""
    _require_linux("launch")
    need_bins("xdotool")
    if timeout is not None:
        wait = timeout
    if wait <= 0 or wait > 3600:
        raise SystemExit(f"bad --timeout={wait}: want 0-3600 seconds")
    run_cwd = str(Path(cwd).expanduser().resolve()) if cwd else os.getcwd()
    if not Path(run_cwd).is_dir():
        raise SystemExit(f"bad --cwd={run_cwd}: not a directory")
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
    log = sessions_dir() / f"{safe_stem(name)}.log"
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
            app_token = uuid.uuid4().hex
            app_env = dict(display_env(display), BOAZ_APP_TOKEN=app_token)
            proc = subprocess.Popen(cmd, shell=True, env=app_env, cwd=run_cwd,
                                    stdout=lf, stderr=subprocess.STDOUT,
                                    start_new_session=True)  # own process group: kill takes children too
        from desktop import boot_id as _boot
        save(name, {"xvfb": xvfb.pid, "xvfb_starttime": proc_stat_starttime(xvfb.pid),
                    "app": proc.pid, "app_starttime": proc_stat_starttime(proc.pid),
                    "boot_id": _boot(), "app_token": app_token,
                    "cmd": cmd, "display": display, "name": name, "cwd": run_cwd})
    except BaseException:
        try:
            xvfb.kill()
        except OSError:
            pass
        drop_session(name)
        raise
    try:
        wid = wait_for_window(display, timeout=wait)
    except SystemExit:
        from desktop import window_state as _ws
        rprint(f"[yellow]launched in {run_cwd} but no window mapped after {wait}s on {display}.[/]")
        rprint(f"[dim]{_ws(display)[1]}[/]")
        rprint(f"[dim]See `boaz log --name {name}`. App still starting? `boaz wait --name {name} --timeout 120`[/]")
        raise typer.Exit(2)
    if xvfb.poll() is not None:
        raise SystemExit(f"Xvfb died under session '{name}'.")
    shot = _shot(name)
    rprint(f"[bold green]Boaz running headless[/] session={name} display={display} pid={proc.pid} window={wid} cwd={run_cwd}")
    rprint(f"[dim]You see nothing. Agent reads: {shot}  Log: {log}[/]")


@app.command()
def wait(
    name: str = typer.Option("app", help="Session name"),
    for_: str = typer.Option("mapped", "--for", help="Condition: mapped|painted"),
    timeout: float = typer.Option(120.0, help="Max seconds to wait"),
) -> None:
    """Block until a window maps (or paints), for slow cold-starts. Exits 0/2."""
    _require_linux("wait")
    if for_ not in ("mapped", "painted"):
        raise SystemExit(f"bad --for={for_!r}: want mapped|painted")
    if not 0 < timeout <= 3600:
        raise SystemExit(f"bad --timeout={timeout}: want 0-3600")
    st = need_session(name)
    from desktop import window_state as _ws
    if for_ == "mapped":
        try:
            wid = wait_for_window(st["display"], timeout=timeout)
        except SystemExit:
            rprint(f"[yellow]no window mapped on {st['display']} after {timeout}s. {_ws(st['display'])[1]}[/]")
            raise typer.Exit(2)
        rprint(f"[bold green]mapped[/] window={wid} display={st['display']}")
        return
    # painted: mapped AND a non-blank capture succeeds
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            shot = _shot(name)
            rprint(f"[bold green]painted[/] {shot}")
            return
        except SystemExit as e:
            last = str(e)
        time.sleep(2.0)
    rprint(f"[yellow]no painted frame on {st['display']} after {timeout}s. {last}[/]")
    raise typer.Exit(2)


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
    """Drive the headless app (xdotool) + screenshot every step. Prints PASS/FAIL.

    x,y are root-display coordinates as seen in screenshots (window sits at +0+20 under root).
    """
    ok = _run(script, name)
    raise typer.Exit(0 if ok else 1)


def _run(script: str, name: str) -> bool:
    _EXAMPLES = {"wait": '{"action":"wait","seconds":2}', "key": '{"action":"key","keys":"Escape"}', "type": '{"action":"type","text":"hi"}', "click": '{"action":"click","x":640,"y":400}', "scroll": '{"action":"scroll","x":640,"y":400,"clicks":3,"dir":"down"}', "drag": '{"action":"drag","x1":100,"y1":200,"x2":300,"y2":400}', "screenshot": '{"action":"screenshot"}'}
    _require_linux("run")
    need_bins("xdotool")
    st = need_session(name)
    try:
        raw = Path(script).read_bytes()
        if len(raw) > (1 << 20):
            raise SystemExit(f"bad script {script}: file too large (>1MB)")
        steps = json.loads(raw.decode(errors="replace"))
    except OSError as e:
        raise SystemExit(f"bad script {script}: {e}")
    except (ValueError, RecursionError) as e:
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
            elif a == "scroll":
                try:
                    cx, cy = int(s["x"]), int(s["y"])
                except (TypeError, ValueError):
                    raise ValueError(f"scroll needs integer x/y, got {s.get('x')!r},{s.get('y')!r}")
                clicks = s.get("clicks", 3)
                try:
                    clicks = int(clicks)
                except (TypeError, ValueError):
                    raise ValueError(f"scroll needs integer clicks, got {s.get('clicks')!r}")
                if not 1 <= clicks <= 50:
                    raise ValueError(f"scroll needs clicks 1-50, got {clicks}")
                direction = str(s.get("dir", "down")).lower()
                if direction not in ("up", "down"):
                    raise ValueError(f"scroll needs dir up|down, got {s.get('dir')!r}")
                btn = "4" if direction == "up" else "5"
                subprocess.run(["xdotool", "mousemove", str(cx), str(cy)] + ["click", btn] * clicks,
                               env=env, check=True, timeout=10)
            elif a == "drag":
                try:
                    x1, y1, x2, y2 = int(s["x1"]), int(s["y1"]), int(s["x2"]), int(s["y2"])
                except (TypeError, ValueError):
                    raise ValueError(f"drag needs ints x1/y1/x2/y2, got {s.get('x1')!r},{s.get('y1')!r},{s.get('x2')!r},{s.get('y2')!r}")
                subprocess.run(["xdotool", "mousemove", str(x1), str(y1), "mousedown", "1",
                                "mousemove", str(x2), str(y2), "mouseup", "1"],
                               env=env, check=True, timeout=10)
            elif a == "screenshot":
                pass  # every step screenshots anyway
            else:
                raise ValueError(f"unknown action {a!r} (want wait|key|type|click|scroll|drag|screenshot). Example: { _EXAMPLES.get(a) if isinstance(a, str) and a in _EXAMPLES else _EXAMPLES['click'] }")
            rprint(f"  [{i}] {a} ok")
        except (KeyError, TypeError, ValueError) as e:
            ex = _EXAMPLES.get(a, _EXAMPLES["wait"]) if isinstance(a, str) else _EXAMPLES["wait"]
            rprint(f"[bold red]  [{i}] {a} FAIL: {e}. Example: {ex}[/]")
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
    errlog = sessions_dir() / f"{safe_stem(name)}-ff.log"
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
        except BaseException:
            try:
                Path(tmp_out).unlink()
            except OSError:
                pass
            raise
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
    try:
        frames = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                             "-show_entries", "stream=nb_read_frames", "-of", "csv=p=0", tmp_out],
                            capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        frames = subprocess.CompletedProcess([], 124, "", "ffprobe timed out")
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
    # Content gate: sample first/middle/last frames, refuse flat/black recordings.
    try:
        dur = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                "format=duration", "-of", "csv=p=0", tmp_out],
                               capture_output=True, text=True, timeout=15)
        total = max(float((dur.stdout or "0").strip() or 0), 0.1)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        total = 0.1
    flat = True
    for pos in (0.0, total / 2, max(total - 0.2, 0.0)):
        try:
            fr = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(round(pos, 2)),
                                   "-i", tmp_out, "-frames:v", "1", "-f", "image2pipe",
                                   "-vcodec", "png", "-"],
                                  capture_output=True, timeout=15)
        except subprocess.TimeoutExpired:
            continue
        try:
            from PIL import Image, ImageStat
            import io as _io
            if (ImageStat.Stat(Image.open(_io.BytesIO(fr.stdout)).convert("L").resize((160, 100))).stddev[0] or 0) > 1.5:
                flat = False
                break
        except Exception:
            continue
    if flat:
        _fail("record is blank (no pixel variance) — was the app running?")
    os.replace(tmp_out, out)
    rprint(f"[bold green]saved[/] {out} ({n} frames)")


@app.command()
def show(name: str = typer.Option("app", help="Session name")) -> None:
    """Relaunch the same command visibly for the human. Only when READY_FOR_HUMAN."""
    _require_linux("show")
    st = load(name)
    if not st or not _cmd(st):
        raise SystemExit(f"session '{name}' has no command recorded — cannot show")
    human = os.environ.get("DISPLAY", "")
    if not human or human == st.get("display") or re.match(r":(99|10[0-9]|110)$", human):
        raise SystemExit(
            f"refusing show: DISPLAY={human!r} is empty or headless. "
            "Run `boaz show` from your visible desktop session.")
    rprint(f"[yellow]Re-launching visibly for YOU to test:[/] {_cmd(st)}")
    rprint("[dim]Headless session keeps running untouched.[/]")
    subprocess.Popen(_cmd(st), shell=True)


@app.command()
def log(name: str = typer.Option("app", help="Session name"),
        lines: int = typer.Option(30, help="Tail lines")) -> None:
    """Show the headless app's stdout/stderr (why no window yet?)."""
    _require_linux("log")
    p = sessions_dir() / f"{safe_stem(name)}.log"
    if not p.is_file():
        raise SystemExit(f"No log for '{name}' (searched {sessions_dir()}). Launch first: boaz launch --name {name} '<cmd>'")
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
        elif app_pid:
            # Leader already dead (crashed/exited): reap surviving group members
            # ONLY with proof of ownership — a recorded token present in a
            # member's environ, plus a recorded starttime. Otherwise a recycled
            # pgid could belong to strangers: keep the session and exit 3.
            from desktop import kill_group as _kg, pids_in_group as _pig, proc_environ as _penv
            members = _pig(app_pid)
            token = st.get("app_token", "")
            owned = [m for m in members
                     if token and _penv(m).get("BOAZ_APP_TOKEN") == token]
            if members and owned and st.get("app_starttime"):
                for survivor in _kg(app_pid):
                    leftovers.append(f"orphaned group member pid={survivor}")
            elif members:
                leftovers.append(f"unverified group pgid={app_pid} ({len(members)} members) kept")
                if not quiet:
                    rprint(f"[dim]{n}:group identity unverified — session kept. Use --force to drop.[/]")
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
        # Reap stale GLOBAL reservations (same 60s gate as the allocator).
        now = time.time()
        for r in GLOBAL_RESERVE.glob("*"):
            try:
                if now - r.stat().st_mtime > 60:
                    r.unlink()
            except OSError:
                pass
        for lk in Path("/tmp").glob(".X*-lock"):
            try:
                pid_txt = lk.read_text().strip().split()[0]
                int(pid_txt)
                if not Path(f"/proc/{pid_txt}").exists():
                    lk.unlink()
            except (OSError, IndexError, ValueError):
                pass
        # (Global reservations were pruned above alongside dead X-locks.)
        claimed: set[int] = set()
        for q in sessions_dir().glob("*.json"):
            try:
                st0 = _read_json(q)
                if not isinstance(st0, dict):
                    continue
                a, x = _coerce_pids(st0)
                if a:
                    claimed.add(a)
                if x:
                    claimed.add(x)
            except (ValueError, OSError, RecursionError):
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
def ps(all: bool = typer.Option(False, help="Show dead/stale sessions too")) -> None:
    """List live headless sessions (name, display, pid, cmd)."""
    _require_linux("ps")
    found = False
    for p in sorted(sessions_dir().glob("*.json")):
        try:
            st = json.loads(p.read_text())
            if not isinstance(st, dict):
                raise ValueError("not a dict")
        except (ValueError, OSError, RecursionError):
            rprint(f"[dim]⚠ {p.stem}: unreadable session file — `boaz kill --name {p.stem}` to clear.[/]")
            continue
        app_pid, _x = _coerce_pids(st)
        if st.get("app") is not None and app_pid is None:
            rprint(f"[dim]⚠ {p.stem}: wrong-typed pids — `boaz kill --name {p.stem}` to clear.[/]")
            found = True
            continue
        alive = bool(app_pid and proc_cmdline(app_pid)
                     and (not st.get("app_starttime") or proc_stat_starttime(app_pid) == st.get("app_starttime")))
        if not alive and not all:
            continue
        rprint(f"{'🟢' if alive else '⚫ gone'} {st.get('name')}  {st.get('display')}  pid={st.get('app')}  {st.get('cmd')}")
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
