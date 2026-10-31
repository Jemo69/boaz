import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Hermetic pool BEFORE any desktop import: parallel agents/CI never share state.
_pool = tempfile.mkdtemp(prefix="boaz-test-pool-")
os.environ["BOAZ_SESSION_DIR"] = _pool


def pytest_sessionfinish(session, exitstatus):
    import subprocess
    # Reap any live test sessions BEFORE deleting the pool dir, or their
    # Xvfb/app processes become permanently orphaned and unsweepable.
    try:
        subprocess.run([sys.executable, "main.py", "kill", "--all", "--quiet"],
                       capture_output=True, cwd=str(Path(__file__).resolve().parent.parent),
                       env={**os.environ, "PATH": os.path.join(os.path.expanduser("~/.local/bin"),
                                                        os.environ.get("PATH", ""))})
    except Exception:
        pass
    shutil.rmtree(_pool, ignore_errors=True)
