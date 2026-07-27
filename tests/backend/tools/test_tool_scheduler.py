import asyncio

from app.tools.scheduler import ToolScheduler


def _call(call_id: str, name: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def test_scheduler_preserves_result_order_and_serializes_writes(monkeypatch) -> None:
    policies = {"read_file": "parallel_safe", "search_files": "parallel_safe", "write_file": "serial", "run_command": "exclusive"}
    monkeypatch.setattr("app.tools.scheduler.concurrency_policy", lambda name: policies[name])
    monkeypatch.setattr("app.tools.scheduler.emit_task_event", lambda *_args, **_kwargs: None)
    active = 0
    max_active = 0
    timeline: list[str] = []

    async def invoke(call: dict) -> dict:
        nonlocal active, max_active
        call_id = call["id"]
        active += 1
        max_active = max(max_active, active)
        timeline.append(f"start:{call_id}")
        await asyncio.sleep(0.02 if call_id == "a" else 0.005)
        timeline.append(f"end:{call_id}")
        active -= 1
        return {"success": True, "id": call_id}

    async def run():
        scheduler = ToolScheduler("test-task", max_parallel=2)
        return await scheduler.execute(
            [_call("a", "read_file"), _call("b", "search_files"), _call("c", "write_file"), _call("d", "run_command")],
            invoke,
        )

    results = asyncio.run(run())
    assert [item.call_id for item in results] == ["a", "b", "c", "d"]
    assert max_active == 2
    assert timeline.index("start:c") > timeline.index("end:a")
    assert timeline.index("start:d") > timeline.index("end:c")
