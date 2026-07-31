"""api/routes_agents.py — エージェントタスク・実行・スケジュール。

実行の起動は scheduler.run_agent_now に任せる。ここで直接 executor を
呼ぶと、Streamlit 版と別経路になり「バックグラウンドで続く」という
前提（ブラウザを閉じても処理は続く）が崩れる。
"""

import json

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

import db
import graphs
import llm_profiles
import scheduler
from api.events import execution_stream
from api.schemas import AgentTaskIn, AgentTaskPatch, RunIn, ScheduleIn, SchedulePatch

router = APIRouter()


def _task_out(task: dict) -> dict:
    """allowed_tool_ids をJSON文字列から配列へ開いて返す。"""
    out = dict(task)
    raw = out.get("allowed_tool_ids")
    try:
        out["allowed_tool_ids"] = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        out["allowed_tool_ids"] = []
    return out


# ===== エージェントタスク =====

@router.get("/agent-tasks")
def list_agent_tasks():
    return [_task_out(t) for t in db.get_all_agent_tasks()]


@router.get("/agent-tasks/{task_id}")
def get_agent_task(task_id: str):
    task = db.get_agent_task(task_id)
    if not task:
        raise HTTPException(404, f"タスクが見つかりません: {task_id}")
    return _task_out(task)


@router.post("/agent-tasks", status_code=201)
def create_agent_task(body: AgentTaskIn):
    task_id = db.add_agent_task(
        body.name, body.description, body.task_prompt,
        allowed_tool_ids=body.allowed_tool_ids or None,
        graph_kind=body.graph_kind,
        llm_profile_id=body.llm_profile_id,
    )
    return {"id": task_id}


@router.patch("/agent-tasks/{task_id}")
def patch_agent_task(task_id: str, body: AgentTaskPatch):
    if not db.get_agent_task(task_id):
        raise HTTPException(404, f"タスクが見つかりません: {task_id}")
    fields = body.model_dump(exclude_unset=True)
    if fields:
        db.update_agent_task(task_id, **fields)
    return _task_out(db.get_agent_task(task_id))


@router.delete("/agent-tasks/{task_id}", status_code=204)
def remove_agent_task(task_id: str):
    if not db.get_agent_task(task_id):
        raise HTTPException(404, f"タスクが見つかりません: {task_id}")
    db.delete_agent_task(task_id)


@router.post("/agent-tasks/{task_id}/run")
def run_agent_task(task_id: str, body: RunIn | None = None):
    """
    即時バックグラウンド実行。exec_id をすぐ返し、続きは SSE で追う。

    graph_kind / llm_profile_id は「今回だけの上書き」。保存はしない
    （既定にしたい場合は PATCH /agent-tasks/{id} を別に呼ぶ）。
    """
    task = db.get_agent_task(task_id)
    if not task:
        raise HTTPException(404, f"タスクが見つかりません: {task_id}")
    body = body or RunIn()
    exec_id = scheduler.run_agent_now(
        task["id"], task["name"], task["task_prompt"],
        graph_kind=body.graph_kind,
        llm_profile_id=body.llm_profile_id,
    )
    return {"exec_id": exec_id}


# ===== 実行 =====

def _execution_out(row: dict) -> dict:
    """JSON列をパースして返す。フロントで JSON.parse を書かせない。"""
    out = dict(row)
    for key in ("history", "trace", "llm_info", "metrics"):
        empty = [] if key in ("history", "trace") else {}
        raw = out.get(key)
        if not raw:
            out[key] = empty
            continue
        try:
            out[key] = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            out[key] = empty
    out["graph_label"] = graphs.run_label(out.get("graph_kind"), out.get("trace"))
    out["model_label"] = llm_profiles.snapshot_label(out.get("llm_info"))
    # ステップ予算はグラフごとに違う（ReAct 10 / リサーチ 18）。画面で
    # 決め打ちすると「STEP 5 / 12」のように実在しない上限が出る
    kind = graphs.normalize_kind(out.get("graph_kind") or "")
    out["max_steps"] = graphs.make_state(kind, "").get("max_steps")
    return out


@router.get("/executions")
def list_executions(limit: int = 30):
    return [_execution_out(e) for e in db.get_executions(limit=limit)]


@router.get("/executions/{exec_id}")
def get_execution(exec_id: str):
    row = db.get_execution(exec_id)
    if not row:
        raise HTTPException(404, f"実行が見つかりません: {exec_id}")
    out = _execution_out(row)
    # 表示用に整形した履歴も添える。パース規則を Python 側に1本化するため
    from executor import parse_history_for_display
    out["steps"] = parse_history_for_display(out.get("history") or [])
    return out


@router.get("/executions/{exec_id}/stream")
async def stream_execution(exec_id: str):
    if not db.get_execution(exec_id):
        raise HTTPException(404, f"実行が見つかりません: {exec_id}")
    return EventSourceResponse(execution_stream(exec_id))


@router.post("/executions/{exec_id}/cancel")
def cancel_execution(exec_id: str):
    """
    実行中のジョブを止める。

    APScheduler のジョブを消せるのは「まだ始まっていない」場合だけ。
    走り出したグラフは途中で割り込めないので、状態を error にして
    画面と履歴の上では終わったことにする。実際のワーカーは今の
    ステップを終えたところで finish_execution を呼ぶが、そのときには
    既に終了済みなので上書きされない（status は最後の書き込みが勝つ）。
    """
    row = db.get_execution(exec_id)
    if not row:
        raise HTTPException(404, f"実行が見つかりません: {exec_id}")
    if row["status"] not in ("running",):
        return {"exec_id": exec_id, "status": row["status"], "cancelled": False}

    removed = False
    job_id = f"now_{exec_id}"
    try:
        if scheduler.scheduler.get_job(job_id):
            scheduler.scheduler.remove_job(job_id)
            removed = True
    except Exception:
        pass
    db.finish_execution(exec_id, "error", stderr="ユーザーによって停止されました")
    return {"exec_id": exec_id, "status": "error", "cancelled": True,
            "job_removed": removed}


# ===== スケジュール =====

def _schedule_out(s: dict) -> dict:
    out = dict(s)
    if s["exec_type"] == "tool":
        target = db.get_tool(s["target_id"])
    else:
        target = db.get_agent_task(s["target_id"])
    out["target_name"] = (target or {}).get("name", "（削除済み）")
    return out


@router.get("/schedules")
def list_schedules():
    return [_schedule_out(s) for s in db.get_all_schedules()]


@router.post("/schedules", status_code=201)
def create_schedule(body: ScheduleIn):
    try:
        CronTrigger.from_crontab(body.cron_expr)
    except Exception as e:
        raise HTTPException(400, f"cron式が不正です: {e}")
    sched_id = db.add_schedule(body.exec_type, body.target_id, body.cron_expr)
    scheduler.add_job(body.exec_type, body.target_id, body.cron_expr, sched_id)
    return {"id": sched_id}


@router.patch("/schedules/{sched_id}")
def patch_schedule(sched_id: str, body: SchedulePatch):
    target = next((s for s in db.get_all_schedules() if s["id"] == sched_id), None)
    if not target:
        raise HTTPException(404, f"スケジュールが見つかりません: {sched_id}")
    db.toggle_schedule(sched_id, body.enabled)
    if body.enabled:
        scheduler.add_job(target["exec_type"], target["target_id"],
                          target["cron_expr"], sched_id)
    else:
        scheduler.remove_job(sched_id)
    return {"id": sched_id, "enabled": body.enabled}


@router.delete("/schedules/{sched_id}", status_code=204)
def remove_schedule(sched_id: str):
    scheduler.remove_job(sched_id)
    db.delete_schedule(sched_id)
