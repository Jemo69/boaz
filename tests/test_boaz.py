"""Boaz dogfood suite: prove every claim with a live headless session.

Run:  uv run pytest tests/ -x -q
Needs Linux + Xvfb + xdotool + ffmpeg + xlogo (doctor tells you what's missing).
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

BOAZ = ["uv", "run", "python", "main.py"]
REPO = Path(__file__).resolve().parent.parent
XLOGO = Path.home() / ".local" / "x11apps" / "usr" / "bin" / "xlogo"

needs_linux = pytest.mark.skipif(os.uname().sysname != "Linux", reason="headless displays need Linux+Xvfb")
needs_xlogo = pytest.mark.skipif(not XLOGO.exists(), reason="xlogo fixture app missing")


def boaz(*args):
    return subprocess.run(BOAZ + list(args), cwd=REPO, capture_output=True, text=True)


def session(name):
    p = Path.home() / ".cache" / "boaz" / "sessions" / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def teardown_function():
    boaz("kill", "--all", "--quiet")


@needs_linux
@needs_xlogo
def test_launch_screenshot_kill_cycle():
    r = boaz("launch", str(XLOGO), "--name", "t-cycle", "--wait", "15")
    assert r.returncode == 0, r.stdout + r.stderr
    st = session("t-cycle")
    assert st["display"] not in (":0", ""), "must never use the human display"
    r = boaz("screenshot", "--name", "t-cycle")
    assert r.returncode == 0, r.stdout + r.stderr
    shot = Path("shots") / "t-cycle-001.png"
    assert shot.exists() and shot.stat().st_size > 1000
    # Dual channel: file exists AND pixels vary (xlogo is black-on-white).
    from PIL import Image, ImageStat
    stat = ImageStat.Stat(Image.open(shot).convert("L"))
    assert stat.stddev[0] > 5, f"suspiciously flat capture (stddev={stat.stddev[0]})"
    r = boaz("kill", "--name", "t-cycle")
    assert r.returncode == 0
    assert session("t-cycle") == {}
    time.sleep(1)
    assert not Path(f"/proc/{st['app']}").exists(), "app process must be dead"


@needs_linux
@needs_xlogo
def test_run_script_pass_and_fail():
    boaz("launch", str(XLOGO), "--name", "t-run", "--wait", "15")
    script = Path("/tmp/boaz-t-run.json")
    script.write_text(json.dumps([
        {"action": "wait", "seconds": 0.5},
        {"action": "key", "keys": "F5"},
        {"action": "screenshot"},
    ]))
    r = boaz("run", str(script), "--name", "t-run")
    assert r.returncode == 0 and "PASS" in r.stdout, r.stdout + r.stderr
    script.write_text(json.dumps([{"action": "bogus"}]))
    r = boaz("run", str(script), "--name", "t-run")
    assert r.returncode == 1 and "FAIL" in r.stdout


@needs_linux
@needs_xlogo
def test_record_produces_frames():
    boaz("launch", str(XLOGO), "--name", "t-rec", "--wait", "15")
    out = "/tmp/boaz-t-rec.mp4"
    Path(out).unlink(missing_ok=True)
    r = boaz("record", "--name", "t-rec", "--out", out, "--seconds", "3")
    assert r.returncode == 0 and "frames" in r.stdout, r.stdout + r.stderr
    assert Path(out).stat().st_size > 1000


@needs_linux
@needs_xlogo
def test_two_sessions_get_different_displays():
    r1 = boaz("launch", str(XLOGO), "--name", "t-a", "--wait", "20")
    assert r1.returncode == 0, "t-a: " + r1.stdout + r1.stderr
    r2 = boaz("launch", str(XLOGO), "--name", "t-b", "--wait", "20")
    assert r2.returncode == 0, "t-b: " + r2.stdout + r2.stderr
    assert session("t-a")["display"] != session("t-b")["display"], "sessions must be isolated"


def test_verified_kill_never_kills_strangers():
    from desktop import verified_kill
    p = subprocess.Popen(["sleep", "60"])
    try:
        assert verified_kill(p.pid, "definitely-not-this-cmdline") is False
        assert p.poll() is None, "stranger process must survive"
        assert verified_kill(p.pid, "sleep") is True
        assert p.wait(timeout=10) == -15
    finally:
        p.kill()
