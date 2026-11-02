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
    pick_wm, start_wm, resolve_window, ensure_focus, list_windows, frame_delta, ocr_text, ocr_find, count_nonblank,
)

from typing import List

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
    p = str(capture(st["display"], Path(out)))
    _archive_evidence(name, Path(p))
    return p


def _jprint(obj: dict) -> None:
    print(json.dumps(obj))


def _parse_env(items: List[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item or item.startswith("="):
            raise SystemExit(f"bad --env {item!r}: want K=V (repeatable, no shell quoting needed)")
        k, v = item.split("=", 1)
        if not k.strip():
            raise SystemExit(f"bad --env {item!r}: empty key")
        out[k] = v
    return out


def _evidence_dir(name: str) -> Path:
    d = Path("evidence") / safe_stem(name)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return d


def _archive_evidence(name: str, path: Path) -> None:
    """Copy a capture/record into the per-session evidence dir (proof survives /tmp)."""
    try:
        if not path.is_file():
            return
        dest = _evidence_dir(name) / path.name
        if path.resolve() == dest.resolve():
            return
        import shutil as _sh
        _sh.copy2(path, dest)
    except Exception:
        pass


def _resolve_capture_wid(display: str, window: str | None) -> str | None:
    if not window:
        return None
    wid = resolve_window(display, window)
    if not wid:
        from desktop import _win_diag as _wd
        raise SystemExit(f"No window matching {window!r} on {display}. {_wd(display)}")
    return wid


@app.command()
def launch(
    cmd: str = typer.Argument(..., help="App command, e.g. './my-electron --no-sandbox'"),
    name: str = typer.Option("app", help="Session name. NOTE: relaunching a name kills its old session first."),
    wait: float = typer.Option(30.0, help="Seconds to wait for a mapped window (alias: --timeout)"),
    timeout: float = typer.Option(None, help="Same as --wait. Overrides --wait when given."),
    cwd: str = typer.Option(None, help="Working dir for the app (default: your current $PWD)"),
    width: int = typer.Option(WIDTH, help="Headless screen width"),
    height: int = typer.Option(HEIGHT, help="Headless screen height"),
    with_wm: str = typer.Option(None, "--with-wm", help="WM/compositor: xfwm4|picom|compton|openbox|mutter|auto|none"),
    wait_window: str = typer.Option(None, "--wait-window", help="Wait for window title:Sub instead of any window"),
    env: List[str] = typer.Option([], "--env", help="Env passthrough K=V (repeatable, no shell quoting needed)"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
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
    extra_env = _parse_env(env)
    wm_bin = pick_wm(with_wm) if with_wm else None
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
    wm_proc = None
    if wm_bin:
        try:
            wm_proc = start_wm(display, wm_bin, log)
            time.sleep(1.0)  # let the compositor claim the display first
            if wm_proc.poll() is not None:
                rprint(f"[yellow]WM {wm_bin} exited immediately; continuing without compositor.[/]")
                wm_proc = None
        except SystemExit:
            raise
        except Exception as e:
            rprint(f"[yellow]could not start WM {wm_bin}: {e}; continuing bare.[/]")
            wm_proc = None
    try:
        with lf:
            app_token = uuid.uuid4().hex
            app_env = dict(display_env(display), BOAZ_APP_TOKEN=app_token)
            app_env.update(extra_env)
            proc = subprocess.Popen(cmd, shell=True, env=app_env, cwd=run_cwd,
                                    stdout=lf, stderr=subprocess.STDOUT,
                                    start_new_session=True)  # own process group: kill takes children too
        from desktop import boot_id as _boot
        save(name, {"xvfb": xvfb.pid, "xvfb_starttime": proc_stat_starttime(xvfb.pid),
                    "app": proc.pid, "app_starttime": proc_stat_starttime(proc.pid),
                    "boot_id": _boot(), "app_token": app_token,
                    "cmd": cmd, "display": display, "name": name, "cwd": run_cwd,
                    "wm": (wm_proc.pid if wm_proc else None),
                    "wm_starttime": (proc_stat_starttime(wm_proc.pid) if wm_proc else ""),
                    "wm_name": (wm_bin or ""), "env": extra_env})
    except BaseException:
        try:
            xvfb.kill()
        except OSError:
            pass
        drop_session(name)
        raise
    try:
        wid = wait_for_window(display, timeout=wait, selector=wait_window)
    except SystemExit:
        from desktop import window_state as _ws
        rprint(f"[yellow]launched in {run_cwd} but no window mapped after {wait}s on {display}.[/]")
        rprint(f"[dim]{_ws(display)[1]}[/]")
        rprint(f"[dim]See `boaz log --name {name}`. App still starting? `boaz wait --name {name} --timeout 120`[/]")
        raise typer.Exit(2)
    if xvfb.poll() is not None:
        raise SystemExit(f"Xvfb died under session '{name}'.")
    shot = _shot(name)
    env_note = f" env={extra_env}" if extra_env else ""
    wm_note = f" wm={wm_bin}" if wm_bin else ""
    if json_out:
        _jprint({"session": name, "display": display, "pid": proc.pid, "window": wid,
                   "cwd": run_cwd, "shot": shot, "log": str(log),
                   "wm": wm_bin or None, "env": extra_env})
        return
    rprint(f"[bold green]Boaz running headless[/] session={name} display={display} pid={proc.pid} window={wid} cwd={run_cwd}{wm_note}{env_note}")
    rprint(f"[dim]You see nothing. Agent reads: {shot}  Log: {log}[/]")


@app.command()
def wait(
    name: str = typer.Option("app", help="Session name"),
    for_: str = typer.Option("mapped", "--for", help="Condition: mapped|painted"),
    timeout: float = typer.Option(120.0, help="Max seconds to wait"),
    window: str = typer.Option(None, "--window", help="Window selector title:Sub|id:...|first"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
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
            wid = wait_for_window(st["display"], timeout=timeout, selector=window)
        except SystemExit:
            rprint(f"[yellow]no window mapped on {st['display']} after {timeout}s. {_ws(st['display'])[1]}[/]")
            raise typer.Exit(2)
        if json_out:
            _jprint({"session": name, "display": st["display"], "window": wid, "for": for_})
            return
        rprint(f"[bold green]mapped[/] window={wid} display={st['display']}")
        return
    # painted: mapped AND a non-blank capture succeeds
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            shot = _shot(name)
            if json_out:
                _jprint({"session": name, "display": st["display"], "for": for_, "shot": shot})
                return
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
    window: str = typer.Option(None, "--window", help="Grab one window: title:Sub|id:...|first (was a bare flag)"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Capture the headless display to PNG. Agent reads this with vision."""
    _require_linux("screenshot")
    st = need_session(name)
    stem = safe_stem(name)
    wid = _resolve_capture_wid(st["display"], window) if window else None
    if window and not wid:
        raise SystemExit(f"No mapped window on {st['display']} yet.")
    path = _shot(name, out) if wid is None else str(capture(st["display"], Path(out) if out else _next_shot(stem, "-win"), wid))
    if wid is not None:
        _archive_evidence(name, Path(path))
    if json_out:
        from desktop import _win_diag as _wd
        _jprint({"session": name, "display": st["display"], "window": wid, "path": path,
                   "diag": _wd(st["display"])})
        return
    rprint(path)


@app.command()
def run(
    script: str = typer.Argument(..., help="JSON steps file, e.g. tests/flow.json"),
    name: str = typer.Option("app", help="Session name"),
    window: str = typer.Option(None, "--window", help="Focus target title:Sub|id:...|first"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Drive the headless app (xdotool) + screenshot every step. Prints PASS/FAIL.

    x,y are root-display coordinates as seen in screenshots (window sits at +0+20 under root).
    """
    ok = _run(script, name, window=window, json_out=json_out)
    raise typer.Exit(0 if ok else 1)


def _run(script: str, name: str, window: str | None = None, json_out: bool = False) -> bool:
    _EXAMPLES = {"wait": '{"action":"wait","seconds":2}', "key": '{"action":"key","keys":"Escape"}', "type": '{"action":"type","text":"hi"}', "click": '{"action":"click","x":640,"y":400}', "scroll": '{"action":"scroll","x":640,"y":400,"clicks":3,"dir":"down"}', "drag": '{"action":"drag","x1":100,"y1":200,"x2":300,"y2":400}', "screenshot": '{"action":"screenshot","as":"a"}', "assert_changed": '{"action":"assert_changed","vs":"a","region":[100,100,200,150],"min_delta":0.01}', "assert_text": '{"action":"assert_text","contains":"John 3:16"}'}
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
    aliases: dict[str, str] = {}  # screenshot "as" name -> shot path
    failures: list[dict] = []
    for i, s in enumerate(steps):
        a = s.get("action") if isinstance(s, dict) else None
        try:
            if a in ("key", "type", "click", "scroll", "drag"):
                # P0-4: focus first, fail loudly when there is nothing to drive.
                try:
                    ensure_focus(st["display"], (s.get("window") or window))
                except RuntimeError as e:
                    raise ValueError(f"no focus ({e})")
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
                alias = s.get("as")
                if alias is not None and not isinstance(alias, str):
                    raise ValueError("screenshot 'as' needs a string alias")
                pass  # every step screenshots anyway; alias wired after the shot
            elif a == "assert_changed":
                vs = s.get("vs")
                if not isinstance(vs, str) or vs not in aliases:
                    raise ValueError(f"assert_changed needs 'vs' naming an earlier screenshot as: (have {sorted(aliases) or 'none'})")
                region = s.get("region")
                if region is not None:
                    try:
                        region = [int(v) for v in region]
                    except (TypeError, ValueError):
                        raise ValueError(f"assert_changed needs region [x,y,w,h], got {region!r}")
                    if len(region) != 4:
                        raise ValueError(f"assert_changed needs region [x,y,w,h], got {region!r}")
                try:
                    min_delta = float(s.get("min_delta", 0.01))
                except (TypeError, ValueError):
                    raise ValueError(f"assert_changed needs numeric min_delta, got {s.get('min_delta')!r}")
                # compare against the CURRENT frame: capture now, diff vs alias.
                cur = _shot(name)
                aliases[f"__cur_{i}"] = cur
                delta = frame_delta(Path(aliases[vs]), Path(cur), region)
                if delta < min_delta:
                    raise ValueError(f"assert_changed FAIL: delta={delta:.4f} < min_delta={min_delta} region={region}")
                rprint(f"  [{i}] assert_changed ok delta={delta:.4f}")
            elif a == "assert_text":
                want = s.get("contains", s.get("text"))
                if not isinstance(want, str) or not want:
                    raise ValueError("assert_text needs 'contains' string")
                region = s.get("region")
                if region is not None:
                    try:
                        region = [int(v) for v in region]
                    except (TypeError, ValueError):
                        raise ValueError(f"assert_text needs region [x,y,w,h], got {region!r}")
                    if len(region) != 4:
                        raise ValueError(f"assert_text needs region [x,y,w,h], got {region!r}")
                cur = _shot(name)
                aliases[f"__cur_{i}"] = cur
                got = ocr_text(Path(cur), region)
                if want.lower() not in got.lower():
                    raise ValueError(f"assert_text FAIL: {want!r} not in OCR {got[:120]!r}")
                rprint(f"  [{i}] assert_text ok")
            else:
                raise ValueError(f"unknown action {a!r} (want wait|key|type|click|scroll|drag|screenshot|assert_changed|assert_text). Example: { _EXAMPLES.get(a) if isinstance(a, str) and a in _EXAMPLES else _EXAMPLES['click'] }")
            if a not in ("assert_changed", "assert_text"):
                rprint(f"  [{i}] {a} ok")
        except (KeyError, TypeError, ValueError) as e:
            ex = _EXAMPLES.get(a, _EXAMPLES["wait"]) if isinstance(a, str) else _EXAMPLES["wait"]
            msg = f"  [{i}] {a} FAIL: {e}. Example: {ex}"
            rprint(f"[bold red]{msg}[/]")
            failures.append({"index": i, "action": a, "error": str(e)})
            ok = False
            break
        except subprocess.CalledProcessError as e:
            msg = f"  [{i}] {a} FAIL: xdotool exit {e.returncode}"
            rprint(f"[bold red]{msg}[/]")
            failures.append({"index": i, "action": a, "error": msg})
            ok = False
            break
        except subprocess.TimeoutExpired:
            msg = f"  [{i}] {a} FAIL: xdotool timed out"
            rprint(f"[bold red]{msg}[/]")
            failures.append({"index": i, "action": a, "error": msg})
            ok = False
            break
        if a in ("assert_changed", "assert_text"):
            shot = aliases.get(f"__cur_{i}", "")
        else:
            try:
                shot = _shot(name)
            except SystemExit as e:
                msg = f"  [{i}] {a} FAIL: capture: {e}"
                rprint(f"[bold red]{msg}[/]")
                failures.append({"index": i, "action": a, "error": str(e)})
                ok = False
                break
            if isinstance(s, dict) and a == "screenshot" and isinstance(s.get("as"), str):
                aliases[s["as"]] = shot
        if i == 0:
            rprint(f"[dim]first frame: {shot}[/]")
    if json_out:
        _jprint({"session": name, "ok": ok, "aliases": aliases, "failures": failures})
    else:
        rprint("[bold green]PASS[/]" if ok else "[bold red]FAIL[/]")
    return ok


@app.command()
def record(
    out: str = typer.Option(None, help="MP4 output (default shots/<name>.mp4)"),
    name: str = typer.Option("app", help="Session name"),
    script: str = typer.Option(None, help="Optional JSON script to drive while recording"),
    seconds: float = typer.Option(5.0, help="Static capture length when no script"),
    window: str = typer.Option(None, "--window", help="Record one window: title:Sub|id:...|first (x11grab offset)"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Record the headless display via ffmpeg x11grab. No window on your screen."""
    _require_linux("record")
    need_bins("ffmpeg", "ffprobe")
    st = need_session(name)
    grab = st["display"]
    if window:
        wid = _resolve_capture_wid(st["display"], window)
        try:
            q = subprocess.run(["xwininfo", "-id", wid], env=display_env(st["display"]),
                                 capture_output=True, text=True, timeout=5).stdout
            import re as _re
            mx = _re.search(r"Absolute upper-left X:\s*(\d+)", q)
            my = _re.search(r"Absolute upper-left Y:\s*(\d+)", q)
            mw = _re.search(r"^\s*Width:\s*(\d+)", q, _re.M)
            mh = _re.search(r"^\s*Height:\s*(\d+)", q, _re.M)
            if mx and my and mw and mh:
                grab = f"{st['display']}+{mx.group(1)},{my.group(1)}"
                # size set via -video_size below
                st = dict(st, _grab_size=f"{mw.group(1)}x{mh.group(1)}")
        except Exception:
            pass
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
        ff_cmd = ["ffmpeg", "-y", "-f", "x11grab", "-draw_mouse", "0", "-r", "10"]
        if st.get("_grab_size"):
            ff_cmd += ["-video_size", st["_grab_size"]]
        ff_cmd += ["-i", grab, "-vcodec", "libx264", "-preset", "ultrafast", tmp_out]
        ff = subprocess.Popen(
            ff_cmd,
            stdout=subprocess.DEVNULL, stderr=ef)
        script_ok = True
        try:
            if script:
                script_ok = _run(script, name, window=window)
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
    nonblank = 0
    sampled = 0
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
            sampled += 1
            if (ImageStat.Stat(Image.open(_io.BytesIO(fr.stdout)).convert("L").resize((160, 100))).stddev[0] or 0) > 1.5:
                flat = False
                nonblank += 1
        except Exception:
            continue
    if flat:
        _fail(f"record is blank (no pixel variance — {n} frames, {nonblank} non-blank) — was the app running?")
    os.replace(tmp_out, out)
    _archive_evidence(name, Path(out))
    if json_out:
        _jprint({"session": name, "path": out, "frames": int(n) if str(n).strip().isdigit() else n,
                   "nonblank": nonblank, "sampled": sampled})
        return
    rprint(f"[bold green]saved[/] {out} ({n} frames) {nonblank}/{sampled} non-blank")


@app.command()
def find(
    name: str = typer.Option("app", help="Session name"),
    text: str = typer.Option(..., "--text", help="Substring to locate on the live frame"),
    window: str = typer.Option(None, "--window", help="Limit OCR to one window title:Sub|id:...|first"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Locate text on the live headless frame (tesseract). Prints x y w h."""
    _require_linux("find")
    st = need_session(name)
    wid = _resolve_capture_wid(st["display"], window) if window else None
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        capture(st["display"], tmp, wid)
        hit = ocr_find(tmp, text)
    finally:
        tmp.unlink(missing_ok=True)
    if not hit:
        raise SystemExit(f"text {text!r} not found on {st['display']} (window={wid or 'root'})")
    if json_out:
        _jprint({"session": name, "display": st["display"], "window": wid,
                   "text": text, **{k: hit[k] for k in ("x", "y", "w", "h")}})
        return
    rprint(f"{hit['x']} {hit['y']} {hit['w']} {hit['h']}")


@app.command()
def evidence(
    name: str = typer.Option("app", help="Session name"),
    out: str = typer.Option(None, help="Bundle path (default evidence/<name>.tar.gz)"),
    json_out: bool = typer.Option(False, "--json", help="Machine-readable output"),
) -> None:
    """Bundle per-session evidence (shots + log + session) into a tarball."""
    _require_linux("evidence")
    import tarfile
    stem = safe_stem(name)
    out = out or str(Path("evidence") / f"{stem}.tar.gz")
    try:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SystemExit(f"cannot write {out}: {e}")
    members: list[str] = []
    with tarfile.open(out, "w:gz") as tf:
        for pat in (f"{stem}-*.png", f"{stem}.mp4"):
            for f in _shots_dir().glob(pat):
                try:
                    tf.add(f, arcname=f"shots/{f.name}")
                    members.append(f"shots/{f.name}")
                except OSError:
                    pass
        for f in _evidence_dir(name).glob("*"):
            try:
                if f.is_file():
                    tf.add(f, arcname=f"evidence/{f.name}")
                    members.append(f"evidence/{f.name}")
            except OSError:
                pass
        for f in (sessions_dir() / f"{stem}.json", sessions_dir() / f"{stem}.log"):
            try:
                if f.is_file():
                    tf.add(f, arcname=f"session/{f.name}")
                    members.append(f"session/{f.name}")
            except OSError:
                pass
    if json_out:
        _jprint({"session": name, "bundle": out, "members": members})
        return
    rprint(f"[bold green]bundled[/] {out} ({len(members)} files)")


@app.command()
def show(name: str = typer.Option("app", help="Session name"),
         json_out: bool = typer.Option(False, "--json", help="Machine-readable output")) -> None:
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
    if json_out:
        _jprint({"session": name, "cmd": _cmd(st), "display": st.get("display")})
        return
    rprint(f"[yellow]Re-launching visibly for YOU to test:[/] {_cmd(st)}")
    rprint("[dim]Headless session keeps running untouched.[/]")
    subprocess.Popen(_cmd(st), shell=True)


@app.command()
def log(name: str = typer.Option("app", help="Session name"),
        lines: int = typer.Option(30, help="Tail lines"),
        json_out: bool = typer.Option(False, "--json", help="Machine-readable output")) -> None:
    """Show the headless app's stdout/stderr (why no window yet?)."""
    _require_linux("log")
    p = sessions_dir() / f"{safe_stem(name)}.log"
    if not p.is_file():
        raise SystemExit(f"No log for '{name}' (searched {sessions_dir()}). Launch first: boaz launch --name {name} '<cmd>'")
    try:
        text = p.read_text(errors="replace")
    except OSError as e:
        raise SystemExit(f"cannot read log {p}: {e}")
    tail = "".join(text.splitlines(True)[-lines:])
    if json_out:
        _jprint({"session": name, "log": str(p), "lines": lines, "text": tail})
        return
    rprint(tail)


@app.command()
def kill(name: str = typer.Option("app", help="Session name (or --all)"),
         all: bool = typer.Option(False, help="Kill every session"),
         sweep: bool = typer.Option(False, help="Also reap orphan boaz Xvfb servers (argv[0]-marked only)"),
         force: bool = typer.Option(False, help="Drop the session without signalling (stale/PID-recycled state)"),
         quiet: bool = typer.Option(False, help="No output"),
         json_out: bool = typer.Option(False, "--json", help="Machine-readable output")) -> None:
    """Kill headless session(s). Verifies cmdline+starttime — never kills strangers."""
    _require_linux("kill")
    kept = _kill([p.stem for p in sessions_dir().glob("*.json")] if all else [name], sweep, force, quiet and not json_out)
    if json_out:
        _jprint({"kept": kept, "ok": not kept})
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
        # P2: pid 1 is init — it can never be our app. A session claiming app pid 1
        # is a recycled/dead namespace leftover: forget it without signalling.
        if app_pid == 1 and not _app_is_ours(1, st):
            drop_session(n)
            if not quiet:
                rprint(f"[dim]{n}: already dead, forgot it (stale pid 1).[/]")
            continue
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
        # WM goes down with the session (verified like Xvfb, never strangers).
        wm_pid = _coerce_pid(st.get("wm"))
        if wm_pid and proc_cmdline(wm_pid):
            wm_name = st.get("wm_name", "") or "xfwm4"
            if proc_cmdline(wm_pid) and wm_name.split()[0] in proc_cmdline(wm_pid):
                if verified_kill(wm_pid, wm_name.split()[0], st.get("wm_starttime", "")):
                    pass
            if proc_cmdline(wm_pid):
                leftovers.append(f"WM pid={wm_pid}")
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
def ps(all: bool = typer.Option(False, help="Show dead/stale sessions too"),
       json_out: bool = typer.Option(False, "--json", help="Machine-readable output")) -> None:
    """List live headless sessions (name, display, pid, cmd)."""
    _require_linux("ps")
    # P2: ps never touches the display (no sockets, no xdotool) so a dead
    # display can't hang it. Only /proc + session files, each guarded.
    rows: list[dict] = []
    found = False
    for p in sorted(sessions_dir().glob("*.json")):
        try:
            st = json.loads(p.read_text())
            if not isinstance(st, dict):
                raise ValueError("not a dict")
        except (ValueError, OSError, RecursionError):
            if json_out:
                rows.append({"name": p.stem, "alive": False, "error": "unreadable"})
            else:
                rprint(f"[dim]⚠ {p.stem}: unreadable session file — `boaz kill --name {p.stem}` to clear.[/]")
            continue
        app_pid, _x = _coerce_pids(st)
        if st.get("app") is not None and app_pid is None:
            if json_out:
                rows.append({"name": p.stem, "alive": False, "error": "wrong-typed pids"})
            else:
                rprint(f"[dim]⚠ {p.stem}: wrong-typed pids — `boaz kill --name {p.stem}` to clear.[/]")
            found = True
            continue
        try:
            alive = bool(app_pid and proc_cmdline(app_pid)
                         and (not st.get("app_starttime") or proc_stat_starttime(app_pid) == st.get("app_starttime")))
        except Exception:
            alive = False
        if not alive and not all and not json_out:
            continue
        if not alive and not all and json_out:
            rows.append({"name": st.get("name"), "display": st.get("display"),
                           "pid": st.get("app"), "alive": False, "cmd": st.get("cmd")})
            found = True
            continue
        rows.append({"name": st.get("name"), "display": st.get("display"),
                       "pid": st.get("app"), "alive": alive, "cmd": st.get("cmd")})
        if not json_out:
            rprint(f"{'🟢' if alive else '⚫ gone'} {st.get('name')}  {st.get('display')}  pid={st.get('app')}  {st.get('cmd')}")
        found = True
    if json_out:
        _jprint({"sessions": rows})
        return
    if not found:
        rprint("[dim]idle — no sessions.[/]")


@app.command()
def doctor(json_out: bool = typer.Option(False, "--json", help="Machine-readable output")) -> None:
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
        ("tesseract", shutil.which("tesseract"), "sudo apt install tesseract-ocr (find/assert_text need it)"),
        ("xfwm4 (compositor)", shutil.which("xfwm4"), "sudo apt install xfwm4 (GL apps need a compositor)"),
        ("glxgears (GL probe)", shutil.which("glxgears"), "sudo apt install mesa-utils (doctor GL smoke-test)"),
        ("/dev/dri (GPU)", str(Path("/dev/dri/card0").exists()), "rootless sandboxes use Lavapipe software GL"),
        ("Lavapipe Vulkan", str(Path("/usr/share/vulkan/icd.d/lvp_icd.json").exists()), "mesa-vulkan-drivers"),
    ]
    # P0-2: smoke-test the actual render path, not just the toolchain. A green
    # doctor that can't render is worse than no doctor: spawn scratch Xvfbs and
    # prove X apps AND GL apps are separately capturable.
    render = _doctor_render_probe() if platform.system() == "Linux" else {"x_capturable": "skip", "gl_capturable": "skip", "detail": "non-Linux"}
    rows.append(("X apps capturable", render.get("x_capturable"), render.get("detail", "")))
    rows.append(("GL apps capturable", render.get("gl_capturable"), render.get("gl_detail", render.get("detail", ""))))
    if json_out:
        _jprint({"tools": [{"label": l, "have": bool(h), "detail": str(h)} for l, h, _ in rows],
                   "render": render})
        return
    for label, have, hint in rows:
        rprint(f"{'✅' if have else '❌'} {label}: {have if have else 'MISSING — ' + hint}")


def _doctor_render_probe() -> dict:
    """Scratch-Xvfb render smoke test. Returns x/gl capturable yes/no separately."""
    import tempfile
    try:
        num = alloc_display()
    except SystemExit as e:
        return {"x_capturable": "no", "gl_capturable": "unknown", "detail": f"no display: {e}"}
    display = f":{num}"
    try:
        xvfb_path = xvfb_bin()
    except SystemExit as e:
        release_display(num)
        return {"x_capturable": "no", "gl_capturable": "unknown", "detail": str(e)}
    xvfb = subprocess.Popen([XVFB_ARGV0, display, "-screen", "0", "800x600x24"],
                              executable=xvfb_path, env=dict(display_env(display), BOAZ_POOL=str(sessions_dir())),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if xvfb.poll() is not None:
                break
            if display_taken(num):
                break
            time.sleep(0.2)
        if xvfb.poll() is not None or not display_taken(num):
            return {"x_capturable": "no", "gl_capturable": "unknown", "detail": "Xvfb never came up"}
        env = display_env(display)
        x_ok, x_detail = False, ""
        xlogo = str(Path.home() / ".local" / "x11apps" / "usr" / "bin" / "xlogo")
        x_bin = xlogo if Path(xlogo).exists() else (shutil.which("xlogo") or shutil.which("xeyes") or shutil.which("xclock"))
        if not x_bin:
            x_detail = "no X test app (xlogo/xeyes/xclock) — toolchain only"
        else:
            xp = subprocess.Popen([x_bin], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            try:
                try:
                    wid = wait_for_window(display, timeout=8.0)
                except SystemExit as e:
                    x_detail = f"{Path(x_bin).name} never mapped: {e}"
                    wid = None
                if wid:
                    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                        tmp = Path(tf.name)
                    try:
                        capture(display, tmp)
                        x_ok = True
                        x_detail = f"{Path(x_bin).name} mapped {wid} and captured"
                    except SystemExit as e:
                        x_detail = f"mapped but blank: {str(e)[:160]}"
                    finally:
                        tmp.unlink(missing_ok=True)
            finally:
                try:
                    xp.terminate()
                except Exception:
                    pass
        gl_ok: str = "unknown"
        gl_detail = "glxgears missing — GL path unproven"
        if shutil.which("glxgears"):
            gp = subprocess.Popen(["glxgears"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            try:
                time.sleep(3.0)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
                    tmp = Path(tf.name)
                try:
                    try:
                        capture(display, tmp)
                        gl_ok, gl_detail = "yes", "glxgears frame captured (bare Xvfb composites it here)"
                    except SystemExit as e:
                        estr = str(e)
                        if "IsViewable" in estr or "compositor" in estr.lower() or "blank" in estr.lower():
                            gl_ok = "no"
                            gl_detail = f"glxgears blank on bare Xvfb — needs --with-wm xfwm4. {estr[:160]}"
                        else:
                            gl_ok = "no"
                            gl_detail = estr[:200]
                finally:
                    tmp.unlink(missing_ok=True)
            finally:
                try:
                    gp.terminate()
                    gp.wait(timeout=5)
                except Exception:
                    try:
                        gp.kill()
                    except Exception:
                        pass
        return {"x_capturable": "yes" if x_ok else "no", "gl_capturable": gl_ok,
                "detail": x_detail, "gl_detail": gl_detail}
    finally:
        try:
            xvfb.terminate()
            xvfb.wait(timeout=5)
        except Exception:
            try:
                xvfb.kill()
            except Exception:
                pass
        release_display(num)


if __name__ == "__main__":
    app()
