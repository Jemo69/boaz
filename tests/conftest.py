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
    shutil.rmtree(_pool, ignore_errors=True)
