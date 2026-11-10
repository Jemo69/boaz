# Boaz — headless desktop tester. Mishpat's sibling: mishpat owns the browser, boaz owns the desktop.
"""Session store, display allocation, verified kill, capture chain.

Hard rules learned from review (agy 2/10, opencode 3/10 — both proven live):
- Never trust `typer.Option` defaults off-CLI (truthy OptionInfo) — this module
  has no typer dependency at all.
- Never signal a PID without cmdline AND starttime match.
- Never hand an agent a flat/black frame with exit 0.
- Never allocate a display without a lock + post-start verification.
"""
from __future__ import annotations

try:
    import fcntl  # noqa
except ImportError:  # Windows: no headless displays anyway
    fcntl = None  # type: ignore
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

GLOBAL_RESERVE = Path('/tmp/.boaz-reserve')
GLOBAL_LOCK = Path('/tmp/.boaz-alloc.lock')


def sessions_dir() -> Path:
    """Per-invocation override so parallel agents/CI never share a pool."""
    d = Path(os.environ.get("BOAZ_SESSION_DIR", str(Path.home() / ".cache" / "boaz" / "sessions"))).expanduser()
    try:
        d = d.resolve()
    except OSError:
        pass
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SystemExit(f"cannot create session dir {d}: {e} (check BOAZ_SESSION_DIR)")
    return d


FIRST_DISPLAY = 99
LAST_DISPLAY = 110
WIDTH, HEIGHT = 1280, 800

XVFB_CANDIDATES = ["Xvfb", str(Path.home() / ".local" / "bin" / "Xvfb")]
XVFB_ARGV0 = "boaz-Xvfb"  # ownership marker: sweep only kills OUR servers

# Compositor / window-manager support (P0-1). Xvfb has no compositor: GL,
# wgpu, SDL, GLFW, GPUI apps paint into a GL surface that never reaches the
# X framebuffer, so root captures come back blank while the window claims
# IsViewable. A lightweight WM with compositing (xfwm4 --compositor=on)
# fixes most of these. Order matters: xfwm4 first (has a real compositor),
# then picom/compton (compositors needing a WM beneath — still help),
# then plain WMs (focus + stacking, no compositing).
WM_CANDIDATES = ["xfwm4", "picom", "compton", "openbox", "mutter"]


def session_path(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return str(sessions_dir() / f"{safe}.json")


def _read_json(path: Path, limit: int = 1 << 20):
    """Parse a JSON file, hardened: size cap + RecursionError included.
    Returns {} on any problem (callers validate shape)."""
    try:
        if path.stat().st_size > limit:
            return {}
        return json.loads(path.read_text())
    except (OSError, ValueError, RecursionError):
        return {}


def load(name: str) -> dict:
    """Raw load. Returns {} for missing/unreadable files (kill/ps stay total)."""
    p = Path(session_path(name))
    data = _read_json(p)
    return data if isinstance(data, dict) else {}


def safe_stem(name: str) -> str:
    """Filesystem-safe stem for session-derived paths (logs, shots).
    Session names may contain slashes; paths must never escape their dir."""
    return Path(session_path(name)).stem


def need_session(name: str) -> dict:
    """Validated load for commands that drive the app. Loud, no KeyError ever.
    Also verifies the session's Xvfb is still alive and matches: a stale
    session must never screenshot/drive another pool's display (D1)."""
    raw = _read_json(Path(session_path(name)))
    st = raw if isinstance(raw, dict) else {}
    disp = st.get("display")
    app_pid = st.get("app")
    path = session_path(name)
    in_range = (isinstance(disp, str) and disp.startswith(":") and disp[1:].isdigit()
                and FIRST_DISPLAY <= int(disp[1:]) <= LAST_DISPLAY)
    valid_shape = (in_range and isinstance(app_pid, int)
                   and not isinstance(app_pid, bool) and app_pid > 0)
    if valid_shape:
        xvfb = st.get("xvfb")
        alive = (isinstance(xvfb, int) and not isinstance(xvfb, bool)
                 and XVFB_ARGV0 in proc_cmdline(xvfb)
                 and (not st.get("xvfb_starttime")
                      or proc_stat_starttime(xvfb) == st.get("xvfb_starttime"))
                 and st.get("boot_id") in (None, "", boot_id()))
        if alive:
            return st
        raise SystemExit(
            f"session '{name}' is stale: its Xvfb {xvfb} is gone/recycled. "
            f"`boaz kill --name {name}` and launch again.")
    if Path(path).exists():
        raise SystemExit(
            f"corrupt session file for '{name}' (needs display + app pid). "
            f"`boaz kill --name {name}` clears it.")
    raise SystemExit("No session '%s'. Run: boaz launch --name %s '<cmd>' first." % (name, name))


def save(name: str, data: dict) -> None:
    """Atomic write: tmp + os.replace, so readers never see half a session."""
    p = Path(session_path(name))
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2))
    except OSError as e:
        raise SystemExit(f"cannot write session {name}: {e}")
    os.replace(tmp, p)


def drop_session(name: str) -> None:
    try:
        Path(session_path(name)).unlink()
    except FileNotFoundError:
        pass


def which_or_hint(candidates: list[str], label: str, hint: str) -> str:
    for c in candidates:
        if "/" in c:
            if Path(c).exists():
                return c
        elif shutil.which(c):
            return c
    raise SystemExit(f"Missing {label}. Install: {hint}")


def xvfb_bin() -> str:
    return which_or_hint(XVFB_CANDIDATES, "Xvfb", "sudo apt install xvfb  (rootless: apt-get download xvfb && dpkg -x *.deb ~/.local/xvfb-root)")


def need_bins(*names: str) -> None:
    hints = {"xdotool": "sudo apt install xdotool", "ffmpeg": "sudo apt install ffmpeg",
             "ffprobe": "ships with ffmpeg", "scrot": "sudo apt install scrot",
             "xwd": "sudo apt install x11-apps", "convert": "sudo apt install imagemagick",
             "xwininfo": "sudo apt install x11-utils"}
    for n in names:
        if not shutil.which(n):
            raise SystemExit(f"Missing {n}. Install: {hints.get(n, n)}")


def display_taken(num: int) -> bool:
    """TCP 60xx + fs socket + abstract socket (Xvfb here binds abstract only)."""
    try:
        s = socket.create_connection(("127.0.0.1", 6000 + num), timeout=0.3)
        s.close()
        return True
    except OSError:
        pass
    if Path(f"/tmp/.X11-unix/X{num}").exists():
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.3)
            s.connect(f"/tmp/.X11-unix/X{num}")
            s.close()
            return True  # live listener behind the fs socket
        except OSError:
            try:
                s.close()
            except Exception:
                pass
            # stale socket file, no listener: fall through to other probes
    try:
        lock_pid = Path(f"/tmp/.X{num}-lock").read_text().strip().split()[0]
        int(lock_pid)  # garbage content: not a real lock
    except FileNotFoundError:
        pass  # no lock file: fall through to the socket probes
    except (OSError, IndexError, ValueError):
        return True  # unreadable lock: conservative, treat as taken
    else:
        if Path(f"/proc/{lock_pid}").exists():
            return True  # live owner: taken
        # Stale lock: do NOT return False — fall through to the socket probes.
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect("\0/tmp/.X11-unix/X" + str(num))
        s.close()
        return True
    except OSError:
        pass
    return False


def _cap_run(args, **kw):
    """Capture-tool runner with a hard timeout. A hung grabber must never hang
    the agent: timeouts come back as failures and the chain falls through."""
    kw.setdefault('timeout', 15)
    try:
        return subprocess.run(args, **kw)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(args, 124, '', f'timed out after {e.timeout}s')


def _prune_reservations() -> None:
    GLOBAL_RESERVE.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for r in list(GLOBAL_RESERVE.glob("*")) + list(sessions_dir().glob(".reserve-*")):
        try:
            if now - r.stat().st_mtime > 60:
                r.unlink()
        except OSError:
            pass


def claimed_displays() -> set[int]:
    _prune_reservations()
    out: set[int] = set()
    for r in GLOBAL_RESERVE.glob("*"):
        try:
            out.add(int(r.name))
        except (IndexError, ValueError):
            pass
    for p in sessions_dir().glob("*.json"):
        data = _read_json(p)
        if not isinstance(data, dict):
            continue
        d = data.get("display", "")
        if isinstance(d, str) and d.startswith(":") and d[1:].isdigit():
            out.add(int(d[1:]))
    return out


def alloc_display() -> int:
    """Locked allocation: global flock + global reservations + socket probes.
    The lock and reservations live in /tmp (shared by all pools) so two
    agents in different BOAZ_SESSION_DIRs can never take the same display."""
    try:
        fd = os.open(GLOBAL_LOCK, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o644)
    except OSError as e:
        raise SystemExit(f"cannot lock {GLOBAL_LOCK}: {e}")
    with os.fdopen(fd, "w") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            claimed = claimed_displays()
            for n in range(FIRST_DISPLAY, LAST_DISPLAY + 1):
                if n not in claimed and not display_taken(n):
                    GLOBAL_RESERVE.mkdir(parents=True, exist_ok=True)
                    Path(GLOBAL_RESERVE / str(n)).touch()
                    return n
        finally:
            if fcntl is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
    raise SystemExit(f"No free display in :{FIRST_DISPLAY}-:{LAST_DISPLAY}. `boaz kill --all`, `boaz ps`, or `boaz kill --name X --force` for stale sessions.")


def confirm_display(num: int) -> None:
    """Reservation fulfilled: the server answers, release the hold."""
    release_display(num)


def release_display(num: int) -> None:
    for r in (GLOBAL_RESERVE / str(num), sessions_dir() / f".reserve-{num}"):
        try:
            r.unlink()
        except FileNotFoundError:
            pass


def display_env(display: str) -> dict[str, str]:
    e = dict(os.environ)
    e["DISPLAY"] = display
    # Wayland escape hatch: Wayland-first toolkits (Electron/GTK/Qt/GPUI)
    # prefer WAYLAND_DISPLAY over DISPLAY and would pop onto the HUMAN's
    # compositor. D-Bus activation could even raise a visible instance.
    e.pop("WAYLAND_DISPLAY", None)
    e.pop("WAYLAND_SOCKET", None)
    e.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
    lvp = "/usr/share/vulkan/icd.d/lvp_icd.json"
    if Path(lvp).exists():
        e.setdefault("VK_ICD_FILENAMES", lvp)
    return e


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def proc_stat_starttime(pid: int) -> str:
    try:
        parts = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return parts[19]  # field 22: starttime (jiffies since boot)
    except (OSError, IndexError):
        return ""


def proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def proc_environ(pid: int) -> dict[str, str]:
    """Environ of a PID, for pool-ownership checks (D1.1)."""
    out: dict[str, str] = {}
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return out
    for item in raw:
        if b"=" in item:
            k, v = item.split(b"=", 1)
            try:
                out[k.decode()] = v.decode(errors="replace")
            except ValueError:
                continue
    return out


def verified_kill(pid: int, expect: str, starttime: str = "") -> bool:
    """Kill pid only if cmdline still contains `expect` (non-empty) and the
    starttime matches when given. Catches PermissionError. Never kills strangers.
    Escalates TERM -> KILL so TERM-ignoring processes don't survive (D8)."""
    if not expect or not expect.strip():
        return False
    cmd = ""
    for _ in range(10):
        cmd = proc_cmdline(pid)
        if cmd:
            break
        time.sleep(0.1)  # fork/execve race: cmdline momentarily empty
    if not cmd or expect not in cmd:
        return False
    if starttime and proc_stat_starttime(pid) != starttime:
        return False  # PID recycled onto a same-command process
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return sig == signal.SIGTERM or not proc_cmdline(pid)
        except PermissionError:
            return False
        for _ in range(20):
            time.sleep(0.25)
            if not proc_cmdline(pid):
                return True
    return not proc_cmdline(pid)


def pids_in_group(pgid: int) -> list[int]:
    """Live PIDs whose process group is pgid (children that outlived the leader)."""
    out: list[int] = []
    for p in Path("/proc").glob("[0-9]*"):
        try:
            parts = (p / "stat").read_text().rsplit(")", 1)[1].split()
            if parts[0] == "Z":
                continue  # zombie: dead, just unreaped — not a survivor
            if int(parts[2]) == pgid and int(p.name) != pgid:  # field 5 = pgrp (NOT session)
                out.append(int(p.name))
        except (OSError, IndexError, ValueError):
            continue
    return out


def kill_group(pgid: int) -> list[int]:
    """TERM, grace, KILL the whole group; return surviving member PIDs (D8)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        for _ in range(20):
            time.sleep(0.25)
            if not pids_in_group(pgid) and not proc_cmdline(pgid):
                return []
    left = pids_in_group(pgid)
    if proc_cmdline(pgid):
        left.append(pgid)
    return left


def window_state(display: str) -> tuple[str | None, str]:
    """First mapped client window id, plus the diagnostic line. Shared by launch/wait."""
    env = display_env(display)
    r = _cap_run(["xdotool", "search", "--onlyvisible", ".*"],
                 env=env, capture_output=True, text=True)
    for wid in r.stdout.split():
        q = _cap_run(["xwininfo", "-id", wid], env=env,
                     capture_output=True, text=True)
        if "Map State: IsViewable" in q.stdout and "root window" not in q.stdout:
            return wid, _win_diag(display)
    return None, _win_diag(display)


def pick_wm(prefer: str | None) -> str | None:
    """Resolve a --with-wm value to a binary. 'auto' picks first available."""
    if not prefer or prefer in ("none", "off", "no"):
        return None
    if prefer == "auto":
        for c in WM_CANDIDATES:
            if shutil.which(c):
                return c
        return None
    if shutil.which(prefer):
        return prefer
    raise SystemExit(
        f"unknown/unavailable WM {prefer!r} (have: "
        f"{[c for c in WM_CANDIDATES if shutil.which(c)] or 'none'}). "
        f"Install one (e.g. sudo apt install xfwm4) or use --with-wm auto.")


def wm_argv(wm: str) -> list[str]:
    if wm == "xfwm4":
        return ["xfwm4", "--compositor=on"]
    return [wm]


def start_wm(display: str, wm: str, log_path: Path | None = None):
    """Start a WM/compositor on DISPLAY. Returns the Popen (caller tracks pid).

    The WM gets its OWN log (<session>-wm.log): sharing the app log buried
    app stdout under xfwm4/llvmpipe lines (issue #6) and made asset-path vs
    GL failures indistinguishable."""
    env = display_env(display)
    out = subprocess.DEVNULL
    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            wm_log = log_path.with_name(log_path.stem + "-wm" + log_path.suffix)
            out = open(wm_log, "ab")
        except OSError:
            out = subprocess.DEVNULL
    try:
        return subprocess.Popen(wm_argv(wm), env=env, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True)
    finally:
        if out is not subprocess.DEVNULL:
            try:
                out.close()
            except Exception:
                pass


def list_windows(display: str) -> list[dict]:
    """All client windows with title/geometry/map-state (for --window targeting)."""
    env = display_env(display)
    try:
        r = subprocess.run(["xdotool", "search", "--name", ".*"],
                             env=env, capture_output=True, text=True, timeout=5)
        ids = r.stdout.split()
    except Exception:
        return []
    out: list[dict] = []
    for wid in ids:
        try:
            q = subprocess.run(["xwininfo", "-id", wid], env=env,
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            continue
        if "root window" in q:
            continue
        title = next((l.split(":", 1)[1].strip().strip('"') for l in q.splitlines()
                        if 'Window id:' in l and '"' in l), "")
        ms = next((l.split(":", 1)[1].strip() for l in q.splitlines()
                     if "Map State:" in l), "?")
        x = y = w = h = ""
        for l in q.splitlines():
            s = l.strip()
            if s.startswith("Absolute upper-left X:"):
                x = s.split(":", 1)[1].strip()
            elif s.startswith("Absolute upper-left Y:"):
                y = s.split(":", 1)[1].strip()
            elif s.startswith("Width:"):
                w = s.split(":", 1)[1].strip()
            elif s.startswith("Height:"):
                h = s.split(":", 1)[1].strip()
        out.append({"id": wid, "title": title, "x": x, "y": y,
                      "width": w, "height": h, "map": ms})
    return out


def resolve_window(display: str, selector: str | None) -> str | None:
    """Resolve --window 'title:Sub' | 'id:0x...' | bare wid | 'first'."""
    if not selector or selector in ("first", "any", "onlyvisible"):
        wid, _ = window_state(display)
        return wid
    sel = selector.strip()
    low = sel.lower()
    if low.startswith(("title:", "title=", "name:")):
        want = sel.split(":", 1)[1].strip() if ":" in sel else sel.split("=", 1)[1].strip()
        if not want:
            raise SystemExit("bad --window: title: needs a substring (e.g. title:VideoPsalm)")
        for w in list_windows(display):
            if want.lower() in (w.get("title") or "").lower():
                return w["id"]
        return None
    if low.startswith(("id:", "id=")):
        want = sel.split(":", 1)[1].strip() if ":" in sel else sel.split("=", 1)[1].strip()
        return want or None
    # bare window id (xdotool prints decimal, xwininfo hex — accept either)
    return sel


def focused_window(display: str) -> str | None:
    env = display_env(display)
    try:
        r = subprocess.run(["xdotool", "getwindowfocus"], env=env,
                             capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    wid = r.stdout.strip().split()[-1] if r.stdout.strip() else ""
    if not wid or wid in ("0", "0x0"):
        return None
    return wid


def ensure_focus(display: str, selector: str | None = None) -> str:
    """Focus the target window (--sync) and fail LOUDLY when there is nothing to type into.

    Every key/type/click/scroll/drag step calls this first: 'typed into
    nothing' is a FAIL, never a silent no-op (P0-4)."""
    need_bins("xdotool", "xwininfo")
    wid = resolve_window(display, selector)
    if not wid:
        raise RuntimeError(
            f"no focusable window on {display} (selector={selector!r}). "
            f"Windows: {_win_diag(display)}")
    env = display_env(display)
    try:
        subprocess.run(["xdotool", "windowfocus", "--sync", wid], env=env,
                         check=True, capture_output=True, text=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"windowfocus --sync {wid} failed: {e}") from e
    got = focused_window(display)
    if not got:
        raise RuntimeError(
            f"no focused window after windowfocus {wid} on {display} "
            f"(no WM? try --with-wm xfwm4). Windows: {_win_diag(display)}")
    return got


def wait_for_window(display: str, timeout: float = 30.0, selector: str | None = None, ignore: set[str] | None = None) -> str:
    """Poll until a non-root client window maps. Replaces the old blind sleep.

    selector follows --window syntax (title:Sub | id:... | first)."""
    need_bins("xdotool", "xwininfo")
    # Fast path for title selectors: poll resolve_window directly.
    if selector and selector.strip().lower().startswith(("title:", "title=", "name:")):
        deadline = time.time() + timeout
        while time.time() < deadline:
            wid = resolve_window(display, selector)
            if wid:
                return wid
            time.sleep(0.5)
        raise SystemExit(f"No window matching {selector!r} on {display} after {timeout}s. See `boaz log`.")
    if selector and selector.strip().lower().startswith(("id:", "id=")):
        return resolve_window(display, selector) or selector
    if selector and selector not in ("first", "any", "onlyvisible"):
        # bare wid: trust but verify it exists
        return selector
    env = display_env(display)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _cap_run(["xdotool", "search", "--onlyvisible", ".*"],
                           env=env, capture_output=True, text=True)
        for wid in r.stdout.split():
            if ignore and wid in ignore:
                continue
            q = _cap_run(["xwininfo", "-id", wid], env=env,
                               capture_output=True, text=True)
            if "Map State: IsViewable" in q.stdout and "root window" not in q.stdout:
                return wid
        time.sleep(0.5)
    raise SystemExit(f"No window mapped on {display} after {timeout}s. See `boaz log`.")


def _win_diag(display: str) -> str:
    """One-line window state for blank-capture errors: id, title, map state, geometry."""
    env = display_env(display)
    try:
        r = subprocess.run(["xdotool", "search", "--name", ".*"],
                             env=env, capture_output=True, text=True, timeout=5)
        ids = r.stdout.split()
    except Exception:
        return "no window ids found"
    if not ids:
        return "no client windows exist"
    parts: list[str] = []
    for wid in ids[:4]:
        try:
            q = subprocess.run(["xwininfo", "-id", wid], env=env,
                               capture_output=True, text=True, timeout=5).stdout
            title = next((l.split(":", 1)[1].strip().strip('"') for l in q.splitlines()
                            if 'Window id:' in l and '"' in l), "")
            ms = next((l.split(":", 1)[1].strip() for l in q.splitlines()
                         if "Map State:" in l), "?")
            geo = ""
            w = h = ""
            for l in q.splitlines():
                s = l.strip()
                if s.startswith("Width:"):
                    w = s.split(":", 1)[1].strip()
                elif s.startswith("Height:"):
                    h = s.split(":", 1)[1].strip()
            if w and h:
                geo = f" {w}x{h}"
            parts.append(f'window {wid} "{title}"{geo} {ms}')
        except Exception:
            parts.append(f"window {wid} (unreadable)")
    return "; ".join(parts)


def _session_for_display(display: str) -> dict:
    """Session owning DISPLAY (for retry-command hints). {} when unknown."""
    for p in sessions_dir().glob("*.json"):
        data = _read_json(p)
        if isinstance(data, dict) and data.get("display") == display:
            return data
    return {}


def _toolkit_hint(display: str) -> str:
    """Suggest known flags when a blank screen matches a known toolkit.

    Includes the GL/compositor row (GPUI, wgpu, SDL, GLFW — anything that
    paints into a GL surface Xvfb never composites) plus the exact retry
    command (P0-1)."""
    st = _session_for_display(display)
    cmd = st.get("cmd", "<cmd>") if isinstance(st.get("cmd"), str) else "<cmd>"
    nm = st.get("name", "app") if isinstance(st.get("name"), str) else "app"
    retry = f'boaz launch "{cmd}" --name {nm} --with-wm xfwm4'
    for p in Path("/proc").glob("[0-9]*"):
        try:
            cmdline = (p / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").lower()
            env_raw = (p / "environ").read_bytes()
        except OSError:
            continue
        if display.encode() not in env_raw:
            continue
        if "electron" in cmdline:
            return (" Electron under Xvfb usually paints nothing without --disable-gpu"
                    " — try adding it to your launch command.")
        if "java" in cmdline:
            return (" Java apps under Xvfb may need -Dsun.java2d.xrender=false"
                    " — try adding it.")
        if "qt" in cmdline or "qpa" in cmdline:
            return (" Qt apps under Xvfb may need QT_QPA_PLATFORM=xcb and"
                    " QT_XCB_FORCE_SOFTWARE_OPENGL=1.")
        gl_keys = ("gpui", "wgpu", "sdl", "glfw", "glut", "godot", "unity",
                     "unreal", "vulkan", "glx", "libgl", "libegl", "gtk")
        if any(k in cmdline for k in gl_keys):
            return (" GL/wgpu/SDL/GLFW/GPUI app suspected: it paints into a GL surface"
                    f" that never reaches the X framebuffer on bare Xvfb. Retry with a compositor: {retry}")
    # Fallback: IsViewable-but-blank is the compositor signature. The old
    # message said 'IsViewable' and left the agent to work out that
    # viewable != rendering — now we say it.
    _wid, diag = window_state(display)
    if "IsViewable" in diag:
        return (" window is IsViewable but the frame is blank: viewable != rendering."
                " Suspected GL/compositor issue (common for GPUI/wgpu/SDL/GLFW)."
                f" Retry with a compositor: {retry}")
    return ""


THUMB_W, THUMB_H = 160, 100  # gate resolution: 16KiB gray buffer, not 1-8MiB PIL frames


def _gray_thumb(path: Path, region: list[int] | None = None,
                w: int = THUMB_W, h: int = THUMB_H) -> bytes | None:
    """Decode path to a w*h gray8 thumbnail via ffmpeg rawvideo (no PIL heap).
    Region [x,y,rw,rh] is cropped at decode time, so a 200x150 assert touches
    ~16KiB, never two full 1280x800 PIL frames + a diff frame (~8MiB).
    Returns None when ffmpeg is missing/undecodable (callers fall back to PIL)."""
    try:
        vf = "format=gray"
        if region:
            x, y, rw, rh = (int(v) for v in region)
            if rw <= 0 or rh <= 0:
                raise ValueError(f"bad region {region}: want positive w/h")
            vf = f"crop={rw}:{rh}:{max(x, 0)}:{max(y, 0)}," + vf
        vf += f",scale={w}:{h}:flags=fast_bilinear"
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(path),
                            "-vf", vf, "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                           capture_output=True, timeout=15)
        if r.returncode == 0 and len(r.stdout) == w * h:
            return r.stdout
        return None
    except Exception:
        return None


def _mean_stddev(buf: bytes) -> tuple[float, float]:
    n = len(buf)
    if not n:
        return 0.0, 0.0
    mean = sum(buf) / n
    var = sum((b - mean) ** 2 for b in buf) / n
    return mean, var ** 0.5


def _pil_thumb_stat(path: Path, floor: float, center_only: bool = False) -> bool:
    """PIL fallback for the variance gates (ffmpeg missing). Slower, more heap."""
    from PIL import Image, ImageStat
    small = Image.open(path).convert("L").resize((THUMB_W, THUMB_H))
    if center_only:
        w, h = small.size
        small = small.crop((w // 4, h // 4, 3 * w // 4, 3 * h // 4))
    return (ImageStat.Stat(small).stddev[0] or 0) >= floor


def _variance_ok(path: Path, floor: float = 1.5) -> bool:
    """Reject flat frames (dead app, unmapped window). Content gate: exit 0 must mean pixels."""
    raw = _gray_thumb(path)
    if raw is not None:
        try:
            return _mean_stddev(raw)[1] >= floor
        except Exception:
            return False
    try:
        return _pil_thumb_stat(path, floor)
    except Exception:
        return False  # undecodable frames never pass the gate


def painted_ok(path: Path) -> bool:
    """Strict gate for `wait --for painted`: whole frame AND center must vary,
    so a bare WM title-bar on a black root never reports painted (issue #5)."""
    raw = _gray_thumb(path)
    if raw is not None:
        try:
            _, sd = _mean_stddev(raw)
            if sd < 3.0:
                return False
            # center half of the 160x100 thumb = middle 80x50 bytes, no second decode
            w, h = THUMB_W, THUMB_H
            cx = bytearray()
            for row in range(h // 4, 3 * h // 4):
                cx += raw[row * w + w // 4: row * w + 3 * w // 4]
            return _mean_stddev(bytes(cx))[1] >= 3.0
        except Exception:
            return False
    try:
        from PIL import Image, ImageStat
        img = Image.open(path).convert("L").resize((THUMB_W, THUMB_H))
        if (ImageStat.Stat(img).stddev[0] or 0) < 3.0:
            return False
        w, h = img.size
        cx = img.crop((w // 4, h // 4, 3 * w // 4, 3 * h // 4))
        return (ImageStat.Stat(cx).stddev[0] or 0) >= 3.0
    except Exception:
        return False


def frame_delta(a: Path, b: Path, region: list[int] | None = None) -> float:
    """Mean absolute per-pixel difference in [0,1] for region [x,y,w,h] (or full frame)."""
    ra, rb = _gray_thumb(a, region), _gray_thumb(b, region)
    if ra is not None and rb is not None and len(ra) == len(rb) and ra:
        return sum(abs(x - y) for x, y in zip(ra, rb)) / (len(ra) * 255.0)
    from PIL import Image, ImageChops, ImageStat
    ia = Image.open(a).convert("L")
    ib = Image.open(b).convert("L")
    if region:
        x, y, w, h = (int(v) for v in region)
        if w <= 0 or h <= 0:
            raise ValueError(f"bad region {region}: want positive w/h")
        box = (max(x, 0), max(y, 0), max(x, 0) + w, max(y, 0) + h)
        ia = ia.crop(box)
        ib = ib.crop(box)
    if ia.size != ib.size:
        ib = ib.resize(ia.size)
    return (ImageStat.Stat(ImageChops.difference(ia, ib)).mean[0] or 0) / 255.0


def ocr_text(image: Path, region: list[int] | None = None) -> str:
    """OCR a frame (or region) via tesseract TSV. Raises SystemExit with install hint."""
    if not shutil.which("tesseract"):
        raise SystemExit("Missing tesseract (sudo apt install tesseract-ocr). Fallback: assert_changed region diffs need no OCR.")
    from PIL import Image
    import tempfile
    img = Image.open(image).convert("L")
    if region:
        x, y, w, h = (int(v) for v in region)
        img = img.crop((max(x, 0), max(y, 0), max(x, 0) + w, max(y, 0) + h))
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        img.save(tmp)
        r = subprocess.run(["tesseract", str(tmp), "stdout", "tsv"],
                             capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise SystemExit(f"tesseract failed: {(r.stderr or '').strip()[:200]}")
        lines = r.stdout.splitlines()
        if len(lines) < 2:
            return ""
        # TSV: level page_no block_no par_no line_no word_no left top width height conf text
        words = []
        for ln in lines[1:]:
            parts = ln.split("\t")
            if len(parts) >= 12 and parts[-1].strip():
                words.append(parts[-1].strip())
        return " ".join(words)
    finally:
        tmp.unlink(missing_ok=True)


def ocr_find(image: Path, text: str) -> dict | None:
    """Find text on a frame. Returns {x,y,w,h} of the first match, else None."""
    if not shutil.which("tesseract"):
        raise SystemExit("Missing tesseract (sudo apt install tesseract-ocr). Fallback: assert_changed region diffs need no OCR.")
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        tmp = Path(tf.name)
    try:
        from PIL import Image
        Image.open(image).convert("L").save(tmp)
        r = subprocess.run(["tesseract", str(tmp), "stdout", "tsv"],
                             capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise SystemExit(f"tesseract failed: {(r.stderr or '').strip()[:200]}")
        lines = r.stdout.splitlines()
        if len(lines) < 2:
            return None
        want = text.lower()
        for ln in lines[1:]:
            parts = ln.rstrip("\n").split("\t")
            if len(parts) < 12:
                continue
            word = parts[-1] if len(parts) >= 12 else ""
            # TSV word rows have 12 cols; line rows have 12 with empty word col? level col tells.
            try:
                left, top, w, h = (int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9]))
            except (ValueError, IndexError):
                continue
            if word and want in word.lower():
                return {"x": left, "y": top, "w": w, "h": h, "text": word}
        # fallback: substring across the whole page text (no box) — report full frame
        return None
    finally:
        tmp.unlink(missing_ok=True)


def count_nonblank(frames: list[Path]) -> int:
    """How many of the sampled frames pass the variance gate."""
    n = 0
    for f in frames:
        try:
            if _valid_png(f) and _variance_ok(f):
                n += 1
        except Exception:
            continue
    return n


def _valid_png(path: Path) -> bool:
    try:
        return path.stat().st_size > 500 and path.read_bytes()[1:4] == b"PNG"
    except OSError:
        return False


def capture(display: str, path: Path, window: str | None = None) -> Path:
    """ffmpeg x11grab first, xwd, scrot root last. Content-gated, atomic.

    Writes to a tmp sibling, validates (decodable PNG + pixel variance), then
    os.replace. A blank frame leaves NO file behind and exits non-zero.
    Raises SystemExit (no traceback to agents) with the failing tool's stderr.
    """
    need_bins("ffmpeg")
    if not path.name or path.is_dir():
        raise SystemExit(f"cannot write {path}: is a directory")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SystemExit(f"cannot write {path}: {e}")
    # Per-process tmp names: concurrent grabs of the same output never share a file.
    tag = f".{os.getpid()}.tmp"
    tmp = path.with_name(path.name + tag + ".png")
    xwd = path.with_name(path.name + tag + ".xwd")
    try:
        return _capture_inner(display, path, tmp, xwd, window)
    finally:
        tmp.unlink(missing_ok=True)
        xwd.unlink(missing_ok=True)


def _capture_inner(display: str, path: Path, tmp: Path, xwd: Path, window: str | None) -> None:
    errs: list[str] = []

    def done() -> Path | None:
        if _valid_png(tmp) and _variance_ok(tmp):
            os.replace(tmp, path)
            return path
        return None

    def blank() -> SystemExit:
        tmp.unlink(missing_ok=True)
        return SystemExit(f"capture on {display} is blank (no pixel variance) — is the app still running?\n{_win_diag(display)}.{_toolkit_hint(display)}")

    if window:
        need_bins("xwd", "convert")
        r = _cap_run(["xwd", "-display", display, "-id", window, "-out", str(xwd)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            r = _cap_run(["convert", str(xwd), str(tmp)], capture_output=True, text=True)
            xwd.unlink(missing_ok=True)
            if r.returncode == 0:
                if _valid_png(tmp):
                    hit = done()
                    if hit:
                        return hit
                    raise blank()
                raise SystemExit(f"screenshot failed on {display}: xwd produced no PNG")
        raise SystemExit(f"window capture failed on {display}: {(r.stderr or '').strip()[:160]}")
    else:
        r = _cap_run(["ffmpeg", "-y", "-v", "error", "-f", "x11grab", "-draw_mouse", "0",
                            "-i", f"{display}+0,0", "-frames:v", "1", str(tmp)],
                           capture_output=True, text=True)
        if r.returncode == 0 and _valid_png(tmp):
            hit = done()
            if hit:
                return hit
            raise blank()
        errs.append(f"ffmpeg: {(r.stderr or '').strip()[:160]}")
        need_bins("xwd", "convert")
        r = _cap_run(["xwd", "-display", display, "-root", "-out", str(xwd)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            r = _cap_run(["convert", str(xwd), str(tmp)], capture_output=True, text=True)
            xwd.unlink(missing_ok=True)
            if r.returncode == 0 and _valid_png(tmp):
                hit = done()
                if hit:
                    return hit
                raise blank()
        errs.append(f"xwd-root: {(r.stderr or '').strip()[:160]}")
    env = display_env(display)
    need_bins("scrot")
    r = _cap_run(["scrot", "-z", str(tmp)], env=env, capture_output=True, text=True)
    if r.returncode == 0 and _valid_png(tmp):
        hit = done()
        if hit:
            return hit
        raise blank()
    errs.append(f"scrot: {(r.stderr or '').strip()[:160]}")
    tmp.unlink(missing_ok=True)
    raise SystemExit(f"screenshot failed on {display}: {' | '.join(errs)}")
