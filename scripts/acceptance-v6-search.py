from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


async def run(output: Path) -> dict[str, object]:
    os.environ["AGENT_DATABASE_PATH"] = str(output / "v6-live-search.db")
    os.chdir(ROOT / "backend")

    from app.database import connect, init_db, now_iso
    from app.schemas import ChatRequest
    from app.task_runner import run_chat
    from app.web_search import SearchRequest, TavilySearchProvider

    key = os.environ.get("AGENT_TAVILY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("AGENT_TAVILY_API_KEY is required")

    output.mkdir(parents=True, exist_ok=True)
    database = Path(os.environ["AGENT_DATABASE_PATH"])
    database.unlink(missing_ok=True)

    provider = TavilySearchProvider(key)
    health = await provider.health()
    direct = await provider.search(
        SearchRequest(
            query="DeepSeek thinking mode tool calls official documentation",
            max_results=5,
        )
    )

    init_db()
    conversation_id = uuid.uuid4().int % 1_000_000_000
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (conversation_id, "V6 live search", "", "full", now_iso(), now_iso()),
        )

    task_id = uuid.uuid4().hex
    response = await run_chat(
        ChatRequest(
            conversation_id=conversation_id,
            content=(
                "这是联网能力验收：必须先调用 web_search，不能只依赖已有知识。"
                "请搜索 DeepSeek 官方关于思考模式结合工具调用的说明，"
                "用两句话总结，并引用本次搜索结果中至少一个可点击的真实来源链接。"
            ),
            task_id=task_id,
            interaction_mode="agent",
            preferred_model="deepseek-reasoner",
            reasoning_effort="high",
        ),
        search_credentials={"tavily": key, "brave": ""},
    )
    content = str(response.get("content") or "")
    with connect() as db:
        tool_rows = db.execute(
            "SELECT tool,status,output FROM tool_runs WHERE task_id=? ORDER BY id", (task_id,)
        ).fetchall()
        model_rows = [dict(row) for row in db.execute(
            "SELECT model,success,error_type,total_tokens FROM model_runs WHERE task_id=? ORDER BY id",
            (task_id,),
        )]
        reasoning_lengths = [int(row[0]) for row in db.execute(
            "SELECT LENGTH(reasoning_content) FROM messages WHERE task_id=? AND role='assistant' ORDER BY id",
            (task_id,),
        )]
    search_outputs = "\n".join(
        str(row["output"] or "") for row in tool_rows if row["tool"] == "web_search"
    )
    cited_urls = set(re.findall(r"https://[^\s\])}>\"']+", content))
    result_urls = set(re.findall(r"https://[^\s\])}>\"']+", search_outputs))
    verified_urls = sorted(cited_urls & result_urls)
    result = {
        "health": health,
        "direct_search": {
            "provider": direct.provider,
            "request_id_present": bool(direct.request_id),
            "result_count": len(direct.results),
            "urls": [item.url for item in direct.results],
        },
        "agent_search": {
            "task_status": response.get("task_status"),
            "tool_calls": len(tool_rows),
            "search_tool_calls": sum(row["tool"] == "web_search" for row in tool_rows),
            "has_https_source": "https://" in content,
            "verified_urls": verified_urls,
            "model_runs": model_rows,
            "reasoning_lengths": reasoning_lengths,
            "content": content,
        },
        "brave": {"status": "skipped", "reason": "credential_not_configured"},
    }
    (output / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "build" / "acceptance" / "v6-search",
    )
    arguments = parser.parse_args()
    result = asyncio.run(run(arguments.output.resolve()))
    print(json.dumps(result, ensure_ascii=False))
    agent = result["agent_search"]
    return 0 if (
        agent["task_status"] == "completed"
        and agent["search_tool_calls"] >= 1
        and bool(agent["verified_urls"])
        and all(item["success"] for item in agent["model_runs"])
        and any(length > 0 for length in agent["reasoning_lengths"])
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
