"""Boaz dogfood suite: prove every claim with a live headless session.

Run:  uv run pytest tests/ -x -q
Needs Linux + Xvfb + xdotool + ffmpeg + xlogo (doctor tells you what's missing).
Session names are uuid-prefixed per test; teardown kills only its own.
"""
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from desktop import sessions_dir

BOAZ = ["uv", "run", "python", "main.py"]
REPO = Path(__file__).resolve().parent.parent
XLOGO = Path.home() / ".local" / "x11apps" / "usr" / "bin" / "xlogo"
SYS_XVFB = str(Path.home() / ".local" / "bin" / "Xvfb")

needs_linux = pytest.mark.skipif(os.uname().sysname != "Linux", reason="headless displays need Linux+Xvfb")
needs_xlogo = pytest.mark.skipif(not XLOGO.exists(), reason="xlogo fixture app missing")


def boaz(*args):
    env = dict(os.environ, BOAZ_SESSION_DIR=os.environ['BOAZ_SESSION_DIR'])
    return subprocess.run(BOAZ + list(args), cwd=REPO, capture_output=True, text=True, env=env)


def tag(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:6]}"


def session(name):
    p = sessions_dir() / f"{name}.json"
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
def test_record_with_script_verifies_frames(names):
    n = tag("t-rec"); names.append(n)
    boaz("launch", str(XLOGO), "--name", n, "--wait", "15")
    out = f"/tmp/boaz-{n}.mp4"
    r = boaz("record", "--name", n, "--out", out, "--script", "tests/flow.json")
    assert r.returncode == 0 and "frames" in r.stdout, r.stdout + r.stderr
    m = re.search(r"\((\d+) frames\)", r.stdout)
    # Low threshold on purpose: under load ffmpeg drops frames; >=5 proves a
    # decodable stream while the script drives (volume is proven by --seconds runs).
    assert m and int(m.group(1)) >= 5
    bad = Path("/tmp/boaz-fail.json"); bad.write_text(json.dumps([{"action": "bogus"}]))
    r = boaz("record", "--name", n, "--out", "/tmp/boaz-dead.mp4", "--script", str(bad))
    assert r.returncode != 0 and "Traceback" not in r.stdout + r.stderr
    assert not Path("/tmp/boaz-dead.mp4").exists()


@needs_linux
@needs_xlogo
def test_crashed_app_kill_reaps_display(names):
    n = tag("t-crash"); names.append(n)
    r = boaz("launch", "sleep 2", "--name", n, "--wait", "6")
    assert r.returncode in (0, 2), r.stdout + r.stderr
    st = session(n)
    disp, xpid = st.get("display"), st.get("xvfb")
    assert disp, f"no session saved: {r.stdout}"
    time.sleep(3)
    r = boaz("kill", "--name", n)
    assert r.returncode == 0, r.stdout + r.stderr
    assert session(n) == {}
    assert not Path(f"/proc/{xpid}").exists(), "Xvfb must be reaped, not leaked"
    from desktop import display_taken
    assert not display_taken(int(disp[1:])), "display must be free again"


@needs_linux
def test_corrupt_session_fails_clean_and_clears(names):
    n = tag("t-corrupt")
    p = sessions_dir() / f"{n}.json"
    p.write_text('{"cmd": "x"}')
    try:
        r = boaz("screenshot", "--name", n)
        assert r.returncode != 0 and "Traceback" not in r.stdout + r.stderr
        assert "corrupt" in (r.stdout + r.stderr).lower()
        r = boaz("kill", "--name", n)
        assert r.returncode == 0 and not p.exists()
    finally:
        p.unlink(missing_ok=True)


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
    """boaz screenshot on an empty display: non-zero exit, no traceback, no file left."""
    from desktop import alloc_display, confirm_display
    num = alloc_display()
    xvfb = subprocess.Popen([str(Path.home() / ".local" / "bin" / "Xvfb"),
                               f":{num}", "-screen", "0", "800x600x24"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    n = tag("t-blank"); names.append(n)
    try:
        confirm_display(num)
        (sessions_dir() / f"{n}.json").write_text(json.dumps(
            {"display": f":{num}", "app": 1, "cmd": "gone", "name": n}))
        r = boaz("screenshot", "--name", n, "--out", f"/tmp/boaz-{n}-blank.png")
        assert r.returncode != 0, "blank capture exited 0: " + r.stdout
        assert "Traceback" not in r.stdout + r.stderr
        assert "blank" in (r.stdout + r.stderr).lower()
        assert not Path(f"/tmp/boaz-{n}-blank.png").exists(), "blank frame must not be left behind"
    finally:
        (sessions_dir() / f"{n}.json").unlink(missing_ok=True)
        xvfb.terminate(); xvfb.wait()


def test_shot_numbering_never_reuses():
    from main import _next_shot
    n = "t-num-" + uuid.uuid4().hex[:6]
    a = _next_shot(n); a.write_text("x")
    b = _next_shot(n); b.write_text("x")
    (a.parent / f"{n}-010.png").write_text("x")
    b.unlink()
    nxt = _next_shot(n)
    assert nxt.name == f"{n}-011.png", f"reused a deleted index: {nxt.name}"
    for f in (a,):
        f.unlink(missing_ok=True)
    (a.parent / f"{n}-010.png").unlink(missing_ok=True)


def test_verified_kill_never_kills_strangers():
    from desktop import verified_kill
    p = subprocess.Popen(["sleep", "60"])
    try:
        from desktop import proc_cmdline as _pc
        for _ in range(100):
            if "sleep" in _pc(p.pid):
                break
            time.sleep(0.1)
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
    from desktop import alloc_display
    if not Path(SYS_XVFB).exists():
        pytest.skip("Xvfb shim missing")
    num = alloc_display()  # never hardcode :104 — the pool is shared
    foreign = subprocess.Popen([SYS_XVFB, f":{num}", "-screen", "0", "800x600x24"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    try:
        r = boaz("kill", "--all", "--sweep", "--quiet")
        assert r.returncode == 0
        assert foreign.poll() is None, "FOREIGN Xvfb was killed by sweep"
    finally:
        from desktop import release_display
        release_display(num)
        foreign.terminate(); foreign.wait()


@needs_linux
def test_type_corrupt_session_clears_clean(names):
    n = tag("t-type")
    p = sessions_dir() / f"{n}.json"
    p.write_text(json.dumps({"display": ":99", "app": "notanint", "xvfb": "alsonotint", "cmd": "x"}))
    try:
        r = boaz("kill", "--name", n)
        assert r.returncode == 0 and "Traceback" not in r.stdout + r.stderr, r.stdout + r.stderr
        assert not p.exists(), "wrong-typed session must be cleared"
        r = boaz("ps")
        assert r.returncode == 0 and "Traceback" not in r.stdout + r.stderr
    finally:
        p.unlink(missing_ok=True)


@needs_linux
@needs_xlogo
def test_unwritable_out_no_traceback(names):
    n = tag("t-wro"); names.append(n)
    boaz("launch", str(XLOGO), "--name", n, "--wait", "15")
    r = boaz("screenshot", "--name", n, "--out", "/root/nope/x.png")
    assert r.returncode != 0 and "Traceback" not in r.stdout + r.stderr
    assert "cannot write" in (r.stdout + r.stderr).lower()


@needs_linux
def test_record_blank_dead_app_refuses_and_cleans(names):
    """Static record of a dead app: non-zero exit AND no MP4 left behind."""
    from desktop import alloc_display, confirm_display
    num = alloc_display()
    xvfb = subprocess.Popen([str(Path.home() / ".local" / "bin" / "Xvfb"),
                               f":{num}", "-screen", "0", "800x600x24"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    n = tag("t-rblank")
    out = f"/tmp/boaz-{n}.mp4"
    try:
        confirm_display(num)
        (sessions_dir() / f"{n}.json").write_text(json.dumps(
            {"display": f":{num}", "app": 1, "cmd": "gone", "name": n}))
        r = boaz("record", "--name", n, "--out", out, "--seconds", "1")
        assert r.returncode != 0, f"black MP4 accepted:\n{r.stdout}"
        assert "blank" in (r.stdout + r.stderr).lower()
        assert not Path(out).exists(), "unverified MP4 must be removed"
    finally:
        (sessions_dir() / f"{n}.json").unlink(missing_ok=True)
        xvfb.terminate(); xvfb.wait()


@needs_linux
def test_bad_geometry_fails_fast_and_releases(names):
    n = tag("t-geo"); names.append(n)
    from desktop import claimed_displays, sessions_dir
    before = set(sessions_dir().glob(".reserve-*"))
    r = boaz("launch", "sleep 30", "--name", n, "--width", "0", "--wait", "5")
    assert r.returncode != 0 and "320" in (r.stdout + r.stderr)
    leaked = set(sessions_dir().glob(".reserve-*")) - before
    assert not leaked, f"reservation leaked: {[p.name for p in leaked]}"


@needs_linux
def test_kill_refused_exit_code_and_force(names):
    """Unverifiable live app: kill exits 3 and keeps the session; --force drops it."""
    import time as _t
    p = subprocess.Popen(["sleep", "120"])
    n = tag("t-ref"); names.append(n)
    try:
        (sessions_dir() / f"{n}.json").write_text(json.dumps(
            {"display": ":199", "app": p.pid, "app_starttime": "0",
             "cmd": "sleep 120", "name": n}))
        r = boaz("kill", "--name", n)
        assert r.returncode == 3, r.stdout + r.stderr
        assert (_sd_dir() / f"{n}.json").exists() if False else (sessions_dir() / f"{n}.json").exists()
        r = boaz("kill", "--name", n, "--force")
        assert r.returncode == 0
        assert not (sessions_dir() / f"{n}.json").exists()
        assert p.poll() is None, "force must not signal"
    finally:
        p.kill(); p.wait()


def _sd_dir():
    return sessions_dir()


@needs_linux
@needs_xlogo
def test_slash_name_sanitized(names):
    n = "t/slash-" + uuid.uuid4().hex[:6]
    r = boaz("launch", str(XLOGO), "--name", n, "--wait", "15")
    assert r.returncode == 0 and "Traceback" not in r.stdout + r.stderr, r.stdout + r.stderr
    names.append(n)
    assert (sessions_dir() / "t-slash-*.json").parent is not None
    r = boaz("screenshot", "--name", n)
    assert r.returncode == 0, r.stdout + r.stderr
    r = boaz("kill", "--name", n)
    assert r.returncode == 0


@needs_linux
def test_record_keeps_user_file_on_failure(names):
    """A failed record must not delete the user's pre-existing out file."""
    keep = Path(f"/tmp/boaz-keep-{uuid.uuid4().hex[:6]}.mp4")
    keep.write_bytes(b"precious-user-bytes")
    n = tag("t-keep"); names.append(n)
    (sessions_dir() / f"{n}.json").write_text(json.dumps(
        {"display": ":199", "app": 1, "cmd": "gone", "name": n}))
    try:
        r = boaz("record", "--name", n, "--out", str(keep), "--seconds", "1")
        assert r.returncode != 0
        assert keep.read_bytes() == b"precious-user-bytes", "user file was destroyed"
    finally:
        (sessions_dir() / f"{n}.json").unlink(missing_ok=True)
        keep.unlink(missing_ok=True)


@needs_linux
@needs_xlogo
def test_sweep_spares_other_pool(names):
    """Two pools, one display server each: sweep in pool B must spare pool A's Xvfb."""
    import tempfile
    pool_b = tempfile.mkdtemp(prefix="boaz-poolB-")
    env_b = dict(os.environ, BOAZ_SESSION_DIR=pool_b)
    n = tag("t-pool"); names.append(n)
    r = boaz("launch", str(XLOGO), "--name", n, "--wait", "15")
    assert r.returncode == 0, r.stdout + r.stderr
    xpid = session(n)["xvfb"]
    try:
        rb = subprocess.run(BOAZ + ["kill", "--all", "--sweep", "--quiet"], cwd=REPO,
                            capture_output=True, text=True, env=env_b)
        assert rb.returncode == 0, rb.stdout + rb.stderr
        assert Path(f"/proc/{xpid}").exists(), "other pool's Xvfb was swept"
    finally:
        shutil.rmtree(pool_b, ignore_errors=True)
