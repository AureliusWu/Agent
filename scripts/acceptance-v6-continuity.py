from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


PROMPTS = [
    "用一句话说明你是谁。",
    "司忆与你是什么关系？用一句话回答。",
    "当前基座模型与你本人是什么关系？",
    "没有工作区时，你的身份会改变吗？",
    "请复述管理员确认的 V6 验收代号。",
    "计算 17+25，只回答结果。",
    "换个说法介绍你自己，不超过两句。",
    "工作区会决定你的身份吗？为什么？",
    "你能否在没有工作区时进行普通聊天？",
    "再次复述 V6 验收代号，并说明信息来源。",
    "用一句话区分事实与推断。",
    "如果不确定管理员的个人资料，你应该怎么做？",
    "你和 DeepSeek 是同一个主体吗？",
    "司忆主要承载什么？",
    "请确认 V6 验收代号是否仍在长期记忆中。",
    "用一句话说明无工作区时不能做什么。",
    "用一句话说明无工作区时仍能做什么。",
    "更换兼容模型后你还是谁？",
    "管理员与你是什么协作关系？不要虚构个人资料。",
    "第三次复述 V6 验收代号。",
    "请用十个字以内描述稳定身份。",
    "上下文压缩是否应该改变你的身份？",
    "工具调用失败是否会让你变成基座模型？",
    "你是否会把未经确认的推断写成事实？",
    "第四次复述 V6 验收代号并标注为正式记忆。",
    "用一句话解释工作区只影响什么。",
    "现在没有项目文件，你能声称读过本地代码吗？",
    "请保持原身份，再做 9*9。",
    "最后一次复述 V6 验收代号。",
    "总结这 30 轮中你的身份、长期记忆和无工作区边界，不超过三句。",
]


async def run(output: Path) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=True)
    os.environ["AGENT_DATABASE_PATH"] = str(output / "v6-continuity.db")
    os.chdir(ROOT / "backend")

    from app.database import connect, init_db, now_iso
    from app.long_term_memory import create_memory
    from app.schemas import ChatRequest
    from app.task_runner import run_chat

    database = Path(os.environ["AGENT_DATABASE_PATH"])
    database.unlink(missing_ok=True)
    init_db()
    create_memory(
        memory_type="semantic",
        title="V6 continuity acceptance",
        content="管理员确认的 V6 验收代号是 ORCHID-600。",
        source_type="user_confirmed",
        confidence=1.0,
        importance=1.0,
        user_confirmed=True,
        is_locked=True,
        metadata={"subject": "管理员", "predicate": "v6验收代号"},
    )
    conversation_id = uuid.uuid4().int % 1_000_000_000
    with connect() as db:
        db.execute(
            "INSERT INTO conversations(id,title,workspace,permission_mode,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (conversation_id, "V6 30-turn continuity", "", "ask", now_iso(), now_iso()),
        )

    transcript: list[dict[str, object]] = []
    for index, prompt in enumerate(PROMPTS, 1):
        response = await run_chat(
            ChatRequest(
                conversation_id=conversation_id,
                content=prompt,
                task_id=uuid.uuid4().hex,
                interaction_mode="conversation",
                reasoning_effort="low",
            )
        )
        transcript.append(
            {
                "round": index,
                "input": prompt,
                "output": str(response.get("content") or ""),
                "status": response.get("task_status"),
                "usage": response.get("usage"),
            }
        )

    with connect() as db:
        tasks = [dict(row) for row in db.execute(
            "SELECT status,current_step,termination_reason,total_tokens FROM agent_tasks "
            "WHERE conversation_id=? ORDER BY created_at", (conversation_id,)
        )]
        segment_count = int(db.execute(
            "SELECT COUNT(*) FROM execution_segments s JOIN agent_tasks t ON t.id=s.task_id "
            "WHERE t.conversation_id=?", (conversation_id,)
        ).fetchone()[0])
        assembly_memory_hits = int(db.execute(
            "SELECT COUNT(*) FROM context_assemblies WHERE conversation_id=? AND memory_ids_json!='[]'",
            (conversation_id,),
        ).fetchone()[0])

    identity_rounds = (1, 7, 18, 30)
    memory_rounds = (5, 10, 15, 20, 25, 29)
    failures: list[str] = []
    if len(transcript) != 30 or any(item["status"] != "completed" for item in transcript):
        failures.append("not_all_30_rounds_completed")
    if any("夏目心" not in transcript[index - 1]["output"] for index in identity_rounds):
        failures.append("identity_continuity_failed")
    if any("ORCHID-600" not in transcript[index - 1]["output"] for index in memory_rounds):
        failures.append("long_term_memory_continuity_failed")
    if any("token_limit" in str(task.get("termination_reason") or "") for task in tasks):
        failures.append("token_limit_termination_detected")
    if assembly_memory_hits < len(memory_rounds):
        failures.append("memory_not_retrieved_through_context_assembly")
    if segment_count < 30:
        failures.append("execution_segments_missing")

    result = {
        "status": "passed" if not failures else "failed",
        "rounds": len(transcript),
        "conversation_id": conversation_id,
        "workspace": "",
        "segment_count": segment_count,
        "memory_assembly_hits": assembly_memory_hits,
        "total_tokens": sum(int(task.get("total_tokens") or 0) for task in tasks),
        "failures": failures,
        "transcript": transcript,
    }
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=ROOT / "build" / "acceptance" / "v6-continuity"
    )
    result = asyncio.run(run(parser.parse_args().output.resolve()))
    print(json.dumps({key: value for key, value in result.items() if key != "transcript"}, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
