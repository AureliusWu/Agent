import os
import logging
import shutil
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="agent-tests-"))
os.environ["AGENT_DATABASE_PATH"] = str(_TEST_ROOT / "agent-test.db")
os.environ["AGENT_LOG_PATH"] = str(_TEST_ROOT / "agent-test.log")


def pytest_sessionstart(session) -> None:
    from app.database import init_db

    init_db()


def pytest_sessionfinish(session, exitstatus) -> None:
    logging.shutdown()
    shutil.rmtree(_TEST_ROOT, ignore_errors=True)
