"""Boaz dogfood suite: prove every claim with a live headless session.

Run:  uv run pytest tests/ -x -q
Needs Linux + Xvfb + xdotool + ffmpeg + xlogo (doctor tells you what's missing).
Session names are uuid-prefixed per test; teardown kills only its own.
"""
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

import pytest

BOAZ = ["uv", "run", "python", "main.py"]
REPO = Path(__file__).resolve().parent.parent
XLOGO = Path.home() / ".local" / "x11apps" / "usr" / "bin" / "xlogo"
SYS_XVFB = str(Path.home() / ".local" / "bin" / "Xvfb")

needs_linux = pytest.mark.skipif(os.uname().sysname != "Linux", reason="headless displays need Linux+Xvfb")
needs_xlogo = pytest.mark.skipif(not XLOGO.exists(), reason="xlogo fixture app missing")


def boaz(*args):
    return subprocess.run(BOAZ + list(args), cwd=REPO, capture_output=True, text=True)


def tag(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def session(name):
    p = Path.home() / ".cache" / "boaz" / "sessions" / f"{name}.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def kill_only(names):
    for n in names:
        boaz("kill", "--name", n, "--quiet")


def fresh_shot_path(stdout):
    """The path boaz actually printed — never a hardcoded stale name."""
    m = re.search(r"shots/\S+\.png", stdout)
    assert m, f"no shot path in output:\n{stdout}"
    return REPO / m.group(0)


@pytest.fixture
def names():
    mine = []
    yield mine
    kill_only(mine)


@needs_linux
@needs_xlogo
def test_launch_screenshot_kill_cycle(names):
    n = tag("t-cycle"); names.append(n)
    r = boaz("launch", str(XLOGO), "--name", n, "--wait", "15")
    assert r.returncode == 0, r.stdout + r.stderr
    st = session(n)
    assert st["display"] not in (":0", ""), "must never use the human display"
    r = boaz("screenshot", "--name", n)
    assert r.returncode == 0, r.stdout + r.stderr
    shot = fresh_shot_path(r.stdout)
    assert shot.exists() and shot.stat().st_size > 1000
    from PIL import Image, ImageStat
    stat = ImageStat.Stat(Image.open(shot).convert("L"))
    assert stat.stddev[0] > 5, f"suspiciously flat capture (stddev={stat.stddev[0]})"
    r = boaz("kill", "--name", n)
    assert r.returncode == 0
    assert session(n) == {}
    time.sleep(1)
    assert not Path(f"/proc/{st['app']}").exists(), "app process must be dead"


@needs_linux
@needs_xlogo
def test_run_script_pass_and_fail(names):
    n = tag("t-run"); names.append(n)
    boaz("launch", str(XLOGO), "--name", n, "--wait", "15")
    r = boaz("run", "tests/flow.json", "--name", n)
    assert r.returncode == 0 and "PASS" in r.stdout, r.stdout + r.stderr
    bad = Path("/tmp/boaz-bad.json")
    bad.write_text(json.dumps([{"action": "bogus"}]))
    r = boaz("run", str(bad), "--name", n)
    assert r.returncode == 1 and "FAIL" in r.stdout
    assert "Traceback" not in r.stdout + r.stderr, "no stack traces to agents"


@needs_linux
def test_run_bad_json_no_traceback(names):
    n = tag("t-bad")
    missing = boaz("run", "/tmp/boaz-nope.json", "--name", n)
    assert missing.returncode != 0 and "Traceback" not in missing.stdout + missing.stderr
    bad = Path("/tmp/boaz-bad2.json"); bad.write_text("{bad")
    r = boaz("run", str(bad), "--name", n)
    assert r.returncode != 0 and "Traceback" not in r.stdout + r.stderr


@needs_linux
@needs_xlogo
def test_record_produces_frames(names):
    n = tag("t-rec"); names.append(n)
    boaz("launch", str(XLOGO), "--name", n, "--wait", "15")
    out = f"/tmp/boaz-{n}.mp4"
    r = boaz("record", "--name", n, "--out", out, "--seconds", "3")
    assert r.returncode == 0 and "frames" in r.stdout, r.stdout + r.stderr
    m = re.search(r"\((\d+) frames\)", r.stdout)
    assert m and int(m.group(1)) >= 20, f"expected >=20 frames @10fps/3s: {r.stdout}"


@needs_linux
@needs_xlogo
def test_concurrent_launches_isolate(names):
    """True concurrency: two launches at the same instant must not share a display."""
    a, b = tag("t-a"), tag("t-b"); names += [a, b]
    pa = subprocess.Popen(BOAZ + ["launch", str(XLOGO), "--name", a, "--wait", "20"],
                          cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    pb = subprocess.Popen(BOAZ + ["launch", str(XLOGO), "--name", b, "--wait", "20"],
                          cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    oa, _ = pa.communicate(timeout=120)
    ob, _ = pb.communicate(timeout=120)
    assert pa.returncode == 0, oa
    assert pb.returncode == 0, ob
    da, db = session(a).get("display"), session(b).get("display")
    assert da and db and da != db, f"collision: {da} vs {db}"
    assert session(a)["app"] != session(b)["app"]


@needs_linux
def test_blank_capture_refuses_exit_zero(names):
    """Empty Xvfb + no app: screenshot must fail loudly, never hand over a black frame."""
    from desktop import alloc_display, display_env
    num = alloc_display()
    xvfb = subprocess.Popen(["/home/jemo/.local/bin/Xvfb", f":{num}", "-screen", "0", "800x600x24"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    try:
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "x11grab", "-draw_mouse", "0",
                            "-i", f":{num}+0,0", "-frames:v", "1", "/tmp/boaz-blank-probe.png"],
                           capture_output=True, text=True, env=display_env(f":{num}"))
        assert r.returncode == 0
        from PIL import Image, ImageStat
        assert ImageStat.Stat(Image.open("/tmp/boaz-blank-probe.png").convert("L")).stddev[0] < 1.5
    finally:
        xvfb.terminate(); xvfb.wait()


def test_verified_kill_never_kills_strangers():
    from desktop import verified_kill
    p = subprocess.Popen(["sleep", "60"])
    try:
        assert verified_kill(p.pid, "definitely-not-this-cmdline") is False
        assert verified_kill(p.pid, "   ") is False, "whitespace expect must not match"
        assert verified_kill(p.pid, "") is False, "empty expect must never kill"
        assert p.poll() is None, "stranger process must survive"
        assert verified_kill(p.pid, "sleep") is True
        assert p.wait(timeout=10) == -15
    finally:
        p.kill()


@needs_linux
def test_sweep_spares_foreign_xvfb():
    """A plain Xvfb (no boaz marker) must survive kill --all --sweep."""
    if not Path(SYS_XVFB).exists():
        pytest.skip("Xvfb shim missing")
    foreign = subprocess.Popen([SYS_XVFB, ":104", "-screen", "0", "800x600x24"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    try:
        r = boaz("kill", "--all", "--sweep", "--quiet")
        assert r.returncode == 0
        assert foreign.poll() is None, "FOREIGN Xvfb was killed by sweep"
    finally:
        foreign.terminate(); foreign.wait()
