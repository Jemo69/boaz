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
    d = Path(os.environ.get("BOAZ_SESSION_DIR", str(Path.home() / ".cache" / "boaz" / "sessions")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def alloc_lock() -> Path:
    return sessions_dir().parent / ".alloc.lock"

FIRST_DISPLAY = 99
LAST_DISPLAY = 110
WIDTH, HEIGHT = 1280, 800

XVFB_CANDIDATES = ["Xvfb", str(Path.home() / ".local" / "bin" / "Xvfb")]
XVFB_ARGV0 = "boaz-Xvfb"  # ownership marker: sweep only kills OUR servers


def session_path(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return str(sessions_dir() / f"{safe}.json")


def load(name: str) -> dict:
    """Raw load. Returns {} for missing/unreadable files (kill/ps stay total)."""
    p = Path(session_path(name))
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def safe_stem(name: str) -> str:
    """Filesystem-safe stem for session-derived paths (logs, shots).
    Session names may contain slashes; paths must never escape their dir."""
    return Path(session_path(name)).stem


def need_session(name: str) -> dict:
    """Validated load for commands that drive the app. Loud, no KeyError ever."""
    st = load(name)
    disp = st.get("display")
    app_pid = st.get("app")
    if (not isinstance(disp, str) or not disp.startswith(":") or not disp[1:].isdigit()
            or not isinstance(app_pid, int) or isinstance(app_pid, bool) or app_pid <= 0):
        path = session_path(name)
        if Path(path).exists():
            raise SystemExit(
                f"corrupt session file for '{name}' (needs display + app pid). "
                f"`boaz kill --name {name}` clears it.")
        raise SystemExit("No session '%s'. Run: boaz launch --name %s '<cmd>' first." % (name, name))
    return st


def save(name: str, data: dict) -> None:
    """Atomic write: tmp + os.replace, so readers never see half a session."""
    p = Path(session_path(name))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
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
        if not Path(f"/proc/{lock_pid}").exists():
            return False  # stale lock from a SIGKILLed server; Xvfb itself ignores it
        return True
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
        try:
            data = json.loads(p.read_text())
            if not isinstance(data, dict):
                continue
            d = data.get("display", "")
            if isinstance(d, str) and d.startswith(":") and d[1:].isdigit():
                out.add(int(d[1:]))
        except (OSError, ValueError):
            continue
    return out


def alloc_display() -> int:
    """Locked allocation: global flock + global reservations + socket probes.
    The lock and reservations live in /tmp (shared by all pools) so two
    agents in different BOAZ_SESSION_DIRs can never take the same display."""
    GLOBAL_LOCK.touch(exist_ok=True)
    with open(GLOBAL_LOCK, "w") as lock:
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
    cmd = proc_cmdline(pid)
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


def wait_for_window(display: str, timeout: float = 30.0) -> str:
    """Poll until a non-root client window maps. Replaces the old blind sleep."""
    need_bins("xdotool", "xwininfo")
    env = display_env(display)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _cap_run(["xdotool", "search", "--onlyvisible", "--name", ".*"],
                           env=env, capture_output=True, text=True)
        for wid in r.stdout.split():
            q = _cap_run(["xwininfo", "-id", wid], env=env,
                               capture_output=True, text=True)
            if "Map State: IsViewable" in q.stdout and "root window" not in q.stdout:
                return wid
        time.sleep(0.5)
    raise SystemExit(f"No window mapped on {display} after {timeout}s. See `boaz log`.")


def _variance_ok(path: Path) -> bool:
    """Reject flat frames (dead app, unmapped window). Content gate: exit 0 must mean pixels."""
    try:
        from PIL import Image, ImageStat
        small = Image.open(path).convert("L").resize((160, 100))
        return (ImageStat.Stat(small).stddev[0] or 0) > 1.5
    except Exception:
        return False  # undecodable frames never pass the gate


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
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise SystemExit(f"cannot write {path}: {e}")
    tmp = path.with_name(path.name + ".tmp.png")
    xwd = path.with_name(path.name + ".tmp.xwd")
    errs: list[str] = []

    def done() -> Path | None:
        if _valid_png(tmp) and _variance_ok(tmp):
            os.replace(tmp, path)
            return path
        return None

    def blank() -> SystemExit:
        tmp.unlink(missing_ok=True)
        return SystemExit(f"capture on {display} is blank (no pixel variance) — is the app still running?")

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
