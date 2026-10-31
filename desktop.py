# Boaz — headless desktop tester. Mishpat's sibling: mishpat owns the browser, boaz owns the desktop.
"""Session store, display allocation, process-verified kill, capture chain."""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

SESSIONS = Path.home() / ".cache" / "boaz" / "sessions"
SESSIONS.mkdir(parents=True, exist_ok=True)

FIRST_DISPLAY = 99
LAST_DISPLAY = 110
WIDTH, HEIGHT = 1280, 800

# Xvfb resolved at runtime: PATH first (so ~/.local/bin shims win), then the
# rootless extract location tonight's harness used.
XVFB_CANDIDATES = ["Xvfb", str(Path.home() / ".local" / "bin" / "Xvfb")]
SCROT_CANDIDATES = ["scrot", str(Path.home() / ".local" / "bin" / "scrot")]


def session_path(name: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
    return SESSIONS / f"{safe}.json"


def load(name: str) -> dict:
    p = session_path(name)
    return json.loads(p.read_text()) if p.exists() else {}


def save(name: str, data: dict) -> None:
    session_path(name).write_text(json.dumps(data, indent=2))


def drop_session(name: str) -> None:
    try:
        session_path(name).unlink()
    except FileNotFoundError:
        pass


def resolve(candidates: list[str], label: str, install_hint: str) -> str:
    for c in candidates:
        if "/" in c:
            if Path(c).exists():
                return c
        elif shutil.which(c):
            return c
    raise SystemExit(f"Missing {label}. Install: {install_hint}")


def xvfb_bin() -> str:
    return resolve(XVFB_CANDIDATES, "Xvfb", "sudo apt install xvfb  (rootless: apt-get download xvfb && dpkg -x *.deb ~/.local/xvfb-root)")


def display_taken(num: int) -> bool:
    """True if something answers on display :num.

    Probes TCP 60xx, the fs unix socket, AND the abstract unix socket
    (Xvfb in rootless sandboxes often binds only the abstract one)."""
    try:
        s = socket.create_connection(("127.0.0.1", 6000 + num), timeout=0.3)
        s.close()
        return True
    except OSError:
        pass
    if Path(f"/tmp/.X11-unix/X{num}").exists():
        return True
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.3)
        s.connect(f"\0/tmp/.X11-unix/X{num}")
        s.close()
        return True
    except OSError:
        pass
    return False


def alloc_display() -> int:
    for n in range(FIRST_DISPLAY, LAST_DISPLAY + 1):
        if not display_taken(n):
            return n
    raise SystemExit(f"No free display in :{FIRST_DISPLAY}-:{LAST_DISPLAY}. `boaz kill --all` or `boaz ps`.")


def display_env(display: str) -> dict[str, str]:
    """Headless-safe env: software GL + Lavapipe Vulkan when present, never :0."""
    e = dict(os.environ)
    e["DISPLAY"] = display
    e.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
    lvp = "/usr/share/vulkan/icd.d/lvp_icd.json"
    if Path(lvp).exists():
        e.setdefault("VK_ICD_FILENAMES", lvp)
    return e


def proc_cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return ""


def verified_kill(pid: int, expect: str) -> bool:
    """Kill pid only if its cmdline still matches what we launched. Kills nothing else."""
    cmd = proc_cmdline(pid)
    if not cmd or expect not in cmd:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    for _ in range(20):
        time.sleep(0.25)
        if not proc_cmdline(pid):
            return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return not proc_cmdline(pid)


def wait_for_window(display: str, timeout: float = 30.0) -> str:
    """Poll until a client window maps. Replaces the old blind sleep(2)."""
    env = display_env(display)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            ["xdotool", "search", "--onlyvisible", "--name", ".*"],
            env=env, capture_output=True, text=True)
        ids = [i for i in r.stdout.split() if i.strip()]
        # xdotool also sees the root window in some builds; require a child.
        for wid in ids:
            q = subprocess.run(["xwininfo", "-id", wid], env=env,
                               capture_output=True, text=True)
            if "Map State: IsViewable" in q.stdout and "root window" not in q.stdout:
                return wid
        time.sleep(0.5)
    raise SystemExit(f"No window mapped on {display} after {timeout}s. App log: check `boaz log <name>`.")


def capture(display: str, path: Path, window: str | None = None) -> Path:
    """ffmpeg x11grab first (proven), scrot second. Raises on failure — never silent black lies.

    Returns path. Verifies the file is a decodable PNG with nonzero size.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    target = f"{display}+0,0" if window is None else f"{display}"
    errs: list[str] = []
    ff = ["ffmpeg", "-y", "-v", "error", "-f", "x11grab",
          "-draw_mouse", "0", "-i", target, "-frames:v", "1", str(path)]
    if window:
        # Per-window grab via xwd (x11grab root grabs miss odd visuals).
        xwd = Path(str(path) + ".xwd")
        r = subprocess.run(["xwd", "-display", display, "-id", window, "-out", str(xwd)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            r = subprocess.run(["convert", str(xwd), str(path)], capture_output=True, text=True)
            xwd.unlink(missing_ok=True)
            if r.returncode == 0 and _valid_png(path):
                return path
        errs.append(f"xwd: {(r.stderr or '')[:120]}")
    else:
        r = subprocess.run(ff, capture_output=True, text=True)
        if r.returncode == 0 and _valid_png(path):
            return path
        errs.append(f"ffmpeg: {(r.stderr or '')[:160]}")
    scrot = resolve(SCROT_CANDIDATES, "scrot", "sudo apt install scrot")
    env = display_env(display)
    args = [scrot, "-z", str(path)] if window is None else [scrot, "-z", "-u", str(path)]
    r = subprocess.run(args, env=env, capture_output=True, text=True)
    if r.returncode == 0 and _valid_png(path):
        return path
    errs.append(f"scrot: {(r.stderr or '')[:160]}")
    raise SystemExit(f"screenshot failed on {display}: {' | '.join(errs)}")


def _valid_png(path: Path) -> bool:
    try:
        return path.stat().st_size > 500 and path.read_bytes()[1:4] == b"PNG"
    except OSError:
        return False
