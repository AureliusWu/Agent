import os
from pathlib import Path

import uvicorn

local_data = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AureliusWu" / "Agent"
local_data.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("AGENT_DATABASE_PATH", str(local_data / "agent.db"))
os.environ.setdefault("AGENT_ALLOW_LOCAL_MCP", "true")
os.environ.setdefault("AGENT_LOG_PATH", str(local_data / "logs" / "agent.log"))

from app.main import app  # noqa: E402


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("AGENT_PORT", "8000")), log_level="warning")
