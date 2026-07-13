import os
import shutil
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="agent-tests-"))
os.environ["AGENT_DATABASE_PATH"] = str(_TEST_ROOT / "agent-test.db")


def pytest_sessionfinish(session, exitstatus) -> None:
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
