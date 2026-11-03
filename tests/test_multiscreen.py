"""Multiscreen-via-sessions: prove the control+projector pattern works.

Apps that require multiscreen (e.g. VideoPsalm: control window + projector
window) are tested as a session PAIR — one session per display. Each session
owns its DISPLAY (:99-:110, lock + reservation isolated), so the pair never
collides. For a SINGLE app that opens two windows, prefer
`screenshot --window title:` + region asserts on one screen instead.
"""
import re
import uuid
from pathlib import Path

import pytest

from test_boaz import boaz, tag, session, kill_only, needs_linux, needs_xlogo, fresh_shot_path


def _launch(name: str, app: str, tries: int = 2) -> None:
    """Launch with one retry: on a loaded box the first wait can burn out."""
    last = None
    for _ in range(tries):
        last = boaz("launch", app, "--name", name, "--wait", "30")
        if last.returncode == 0:
            return
        kill_only([name])
    assert last is not None and last.returncode == 0, last.stdout + last.stderr if last else "no attempt"


@pytest.fixture
def names():
    mine = []
    yield mine
    kill_only(mine)


@needs_linux
@needs_xlogo
def test_multiscreen_pair_isolated_displays(names):
    """Control + projector sessions: different DISPLAYs, both capturable."""
    from test_boaz import XLOGO
    ctrl, proj = tag("t-ctrl"), tag("t-proj")
    names += [ctrl, proj]
    _launch(ctrl, str(XLOGO))
    _launch(proj, str(XLOGO))
    d1, d2 = session(ctrl).get("display"), session(proj).get("display")
    assert d1 and d2 and d1 != d2, f"pair must isolate displays: {d1} vs {d2}"
    for n in (ctrl, proj):
        r = boaz("screenshot", "--name", n)
        assert r.returncode == 0, r.stdout + r.stderr
        shot = fresh_shot_path(r.stdout)
        assert shot.exists() and shot.stat().st_size > 1000
    # pair drives independently: key into control must not fail the pair
    import json as _json
    flow = Path(f"/tmp/boaz-multi-{uuid.uuid4().hex[:6]}.json")
    flow.write_text(_json.dumps([{"action": "key", "keys": "F5"}]))
    try:
        r = boaz("run", str(flow), "--name", ctrl)
        assert r.returncode == 0 and "PASS" in r.stdout, r.stdout + r.stderr
        r = boaz("screenshot", "--name", proj, "--json")
        assert r.returncode == 0, r.stdout + r.stderr
        cur = _json.loads(r.stdout)
        assert cur["session"] == proj and cur["display"] == d2
    finally:
        flow.unlink(missing_ok=True)
